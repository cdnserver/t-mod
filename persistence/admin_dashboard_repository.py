"""Read-only projections for the authenticated administrative web console."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from persistence.core import connect_readonly


def _window(days: int) -> tuple[int, str]:
    selected = max(1, min(int(days), 365))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=selected)).isoformat()
    return selected, cutoff


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(int(limit), 100)), max(0, min(int(offset), 50_000))


def admin_dashboard_counts(guild_id: int, *, days: int = 30) -> dict[str, Any]:
    selected_days, cutoff = _window(days)
    now = datetime.now(timezone.utc).isoformat()
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM bot_actions
                WHERE guild_id = ? AND created_at >= ?) AS actions,
              (SELECT COUNT(*) FROM bot_actions
                WHERE guild_id = ? AND status = 'active') AS active_actions,
              (SELECT COUNT(*) FROM finance_events
                WHERE guild_id = ? AND created_at >= ?) AS finance_events,
              (SELECT COUNT(*) FROM craft_plans
                WHERE guild_id = ?
                  AND stage NOT IN ('completed', 'cancelled')) AS active_crafts,
              (SELECT COUNT(*) FROM craft_batches AS b
                JOIN craft_plans AS p ON p.id = b.plan_id
                WHERE p.guild_id = ? AND b.status = 'active'
                  AND b.undone_at IS NULL AND b.due_at < ?) AS overdue_batches,
              (SELECT COUNT(*) FROM activity_events
                WHERE guild_id = ? AND at >= ?) AS discord_events,
              (SELECT COUNT(DISTINCT user_id) FROM activity_events
                WHERE guild_id = ? AND at >= ?) AS discord_users,
              (SELECT COUNT(*) FROM finance_notifications AS n
                JOIN finance_events AS e ON e.id = n.event_id
                WHERE e.guild_id = ?
                  AND n.status = 'pending' AND n.attempts > 0) AS finance_retries,
              (SELECT COUNT(*) FROM delivery_outbox
                WHERE status IN ('pending', 'processing', 'retry')) AS outbox_open,
              (SELECT COUNT(*) FROM delivery_outbox
                WHERE status = 'dead') AS outbox_dead
            """,
            (
                int(guild_id),
                cutoff,
                int(guild_id),
                int(guild_id),
                cutoff,
                int(guild_id),
                int(guild_id),
                now,
                int(guild_id),
                cutoff,
                int(guild_id),
                cutoff,
                int(guild_id),
            ),
        ).fetchone()
    return {
        "days": selected_days,
        "cutoff": cutoff,
        **dict(row),
    }


def admin_audit_actions(
    guild_id: int,
    *,
    module: str | None = None,
    status: str | None = None,
    actor_id: int | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    page_limit, page_offset = _page(limit, offset)
    clauses = ["guild_id = ?"]
    params: list[Any] = [int(guild_id)]
    if module:
        clauses.append("module = ?")
        params.append(str(module).strip()[:80])
    if status:
        clauses.append("status = ?")
        params.append(str(status).strip()[:40])
    if actor_id is not None:
        clauses.append("actor_id = ?")
        params.append(int(actor_id))
    clean_query = str(query or "").strip().casefold()[:160]
    if clean_query:
        clauses.append(
            """
            (
              T_CASEFOLD(COALESCE(summary, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(actor_display, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(target_type, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(target_id, '')) LIKE ?
            )
            """
        )
        needle = f"%{clean_query}%"
        params.extend([needle, needle, needle, needle])
    where = " AND ".join(clauses)
    with connect_readonly() as con:
        total = int(
            con.execute(
                f"SELECT COUNT(*) AS n FROM bot_actions WHERE {where}",
                params,
            ).fetchone()["n"]
            or 0
        )
        rows = con.execute(
            f"""
            SELECT id, actor_id, actor_display, module, action_kind,
                   target_type, target_id, summary, reversible, status,
                   undone_by_id, undone_by_display, undone_at, undo_reason,
                   created_at
            FROM bot_actions
            WHERE {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_limit, page_offset],
        ).fetchall()
        modules = [
            str(row["module"])
            for row in con.execute(
                """
                SELECT DISTINCT module FROM bot_actions
                WHERE guild_id = ? ORDER BY module
                """,
                (int(guild_id),),
            ).fetchall()
        ]
    items = []
    for row in rows:
        item = dict(row)
        item["reversible"] = bool(item.get("reversible"))
        items.append(item)
    return {
        "items": items,
        "total": total,
        "limit": page_limit,
        "offset": page_offset,
        "modules": modules,
    }


def admin_discord_stats(guild_id: int, *, days: int = 7) -> dict[str, Any]:
    selected_days, cutoff = _window(days)
    with connect_readonly() as con:
        summary = con.execute(
            """
            SELECT COUNT(*) AS events,
                   COUNT(DISTINCT user_id) AS users,
                   COUNT(DISTINCT channel_id) AS channels,
                   MAX(at) AS last_event_at
            FROM activity_events
            WHERE guild_id = ? AND at >= ?
            """,
            (int(guild_id), cutoff),
        ).fetchone()
        top_types = con.execute(
            """
            SELECT event_type, COUNT(*) AS events
            FROM activity_events
            WHERE guild_id = ? AND at >= ?
            GROUP BY event_type
            ORDER BY events DESC, event_type
            LIMIT 10
            """,
            (int(guild_id), cutoff),
        ).fetchall()
        top_channels = con.execute(
            """
            SELECT channel_id, MAX(channel_name) AS channel_name,
                   COUNT(*) AS events
            FROM activity_events
            WHERE guild_id = ? AND at >= ? AND channel_id IS NOT NULL
            GROUP BY channel_id
            ORDER BY events DESC, channel_id
            LIMIT 8
            """,
            (int(guild_id), cutoff),
        ).fetchall()
        top_users = con.execute(
            """
            SELECT e.user_id,
                   COALESCE(MAX(m.display_name), MAX(m.name), CAST(e.user_id AS TEXT))
                     AS display_name,
                   MAX(m.is_bot) AS is_bot,
                   COUNT(*) AS events
            FROM activity_events AS e
            LEFT JOIN members AS m
              ON m.guild_id = e.guild_id AND m.user_id = e.user_id
            WHERE e.guild_id = ? AND e.at >= ?
            GROUP BY e.user_id
            ORDER BY events DESC, e.user_id
            LIMIT 8
            """,
            (int(guild_id), cutoff),
        ).fetchall()
        timeline = con.execute(
            """
            SELECT substr(at, 1, 10) AS day, COUNT(*) AS events
            FROM activity_events
            WHERE guild_id = ? AND at >= ?
            GROUP BY substr(at, 1, 10)
            ORDER BY day
            """,
            (int(guild_id), cutoff),
        ).fetchall()
    return {
        "days": selected_days,
        "cutoff": cutoff,
        **dict(summary),
        "top_types": [dict(row) for row in top_types],
        "top_channels": [dict(row) for row in top_channels],
        "top_users": [
            {**dict(row), "is_bot": bool(row["is_bot"])}
            for row in top_users
        ],
        "timeline": [dict(row) for row in timeline],
    }


def admin_discord_events(
    guild_id: int,
    *,
    days: int = 7,
    event_type: str | None = None,
    user_id: int | None = None,
    channel_id: int | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    selected_days, cutoff = _window(days)
    page_limit, page_offset = _page(limit, offset)
    clauses = ["e.guild_id = ?", "e.at >= ?"]
    params: list[Any] = [int(guild_id), cutoff]
    if event_type:
        clauses.append("e.event_type = ?")
        params.append(str(event_type).strip()[:80])
    if user_id is not None:
        clauses.append("e.user_id = ?")
        params.append(int(user_id))
    if channel_id is not None:
        clauses.append("e.channel_id = ?")
        params.append(int(channel_id))
    clean_query = str(query or "").strip().casefold()[:160]
    if clean_query:
        clauses.append(
            """
            (
              T_CASEFOLD(COALESCE(e.event_text, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(e.details, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(e.channel_name, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(m.display_name, m.name, '')) LIKE ?
            )
            """
        )
        needle = f"%{clean_query}%"
        params.extend([needle, needle, needle, needle])
    where = " AND ".join(clauses)
    with connect_readonly() as con:
        total = int(
            con.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM activity_events AS e
                LEFT JOIN members AS m
                  ON m.guild_id = e.guild_id AND m.user_id = e.user_id
                WHERE {where}
                """,
                params,
            ).fetchone()["n"]
            or 0
        )
        rows = con.execute(
            f"""
            SELECT e.id, e.user_id, e.event_type, e.event_text, e.at,
                   e.channel_id, e.channel_name, e.category_id,
                   e.category_name, e.details, e.message_id,
                   COALESCE(m.display_name, m.name, CAST(e.user_id AS TEXT))
                     AS display_name,
                   COALESCE(m.is_bot, 0) AS is_bot
            FROM activity_events AS e
            LEFT JOIN members AS m
              ON m.guild_id = e.guild_id AND m.user_id = e.user_id
            WHERE {where}
            ORDER BY e.at DESC, e.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_limit, page_offset],
        ).fetchall()
    return {
        "items": [
            {**dict(row), "is_bot": bool(row["is_bot"])}
            for row in rows
        ],
        "total": total,
        "limit": page_limit,
        "offset": page_offset,
        "days": selected_days,
        "cutoff": cutoff,
    }


def admin_craft_events(
    guild_id: int,
    *,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    page_limit, page_offset = _page(limit, offset)
    clauses = ["e.guild_id = ?"]
    params: list[Any] = [int(guild_id)]
    clean_query = str(query or "").strip().casefold()[:160]
    if clean_query:
        clauses.append(
            """
            (
              T_CASEFOLD(COALESCE(e.event_kind, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(e.actor_display, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(p.product_name_snapshot, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(e.details_json, '')) LIKE ?
            )
            """
        )
        needle = f"%{clean_query}%"
        params.extend([needle, needle, needle, needle])
    where = " AND ".join(clauses)
    with connect_readonly() as con:
        total = int(
            con.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM craft_events AS e
                JOIN craft_plans AS p ON p.id = e.plan_id
                WHERE {where}
                """,
                params,
            ).fetchone()["n"]
            or 0
        )
        rows = con.execute(
            f"""
            SELECT e.id, e.plan_id, e.event_kind, e.actor_id,
                   e.actor_display, e.details_json, e.thread_message_id,
                   e.created_at, p.product_name_snapshot AS product_name,
                   p.channel_id, p.thread_id, p.stage
            FROM craft_events AS e
            JOIN craft_plans AS p ON p.id = e.plan_id
            WHERE {where}
            ORDER BY e.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_limit, page_offset],
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            details = json.loads(str(item.pop("details_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            details = {}
        item["details"] = details if isinstance(details, dict) else {}
        items.append(item)
    return {
        "items": items,
        "total": total,
        "limit": page_limit,
        "offset": page_offset,
    }


def admin_finance_events(
    guild_id: int,
    *,
    days: int = 30,
    event_kind: str | None = None,
    actor_id: int | None = None,
    code: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    selected_days, cutoff = _window(days)
    page_limit, page_offset = _page(limit, offset)
    clauses = ["e.guild_id = ?", "e.created_at >= ?"]
    params: list[Any] = [int(guild_id), cutoff]
    if event_kind:
        selected_kind = str(event_kind).strip().lower()
        if selected_kind not in {"daily", "interim", "deposit", "withdraw", "undo"}:
            raise ValueError("finance_bad_audit_kind")
        clauses.append("e.event_kind = ?")
        params.append(selected_kind)
    if actor_id is not None:
        clauses.append("e.actor_id = ?")
        params.append(int(actor_id))
    if code:
        selected_code = re.sub(r"[^A-Za-z]", "", str(code)).upper()
        if len(selected_code) != 4:
            raise ValueError("finance_bad_game_code")
        clauses.append("e.game_code = ?")
        params.append(selected_code)
    clean_query = str(query or "").strip().casefold()[:160]
    if clean_query:
        clauses.append(
            """
            (
              T_CASEFOLD(COALESCE(e.reason, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(e.actor_display, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(e.game_code, '')) LIKE ?
              OR CAST(e.id AS TEXT) LIKE ?
            )
            """
        )
        needle = f"%{clean_query}%"
        params.extend([needle, needle, needle, needle])
    where = " AND ".join(clauses)
    with connect_readonly() as con:
        total = int(
            con.execute(
                f"SELECT COUNT(*) AS n FROM finance_events AS e WHERE {where}",
                params,
            ).fetchone()["n"]
            or 0
        )
        rows = con.execute(
            f"""
            SELECT e.*,
                   EXISTS(
                     SELECT 1 FROM finance_events AS u
                     WHERE u.reversed_event_id = e.id
                   ) AS is_undone,
                   (
                     SELECT u.id FROM finance_events AS u
                     WHERE u.reversed_event_id = e.id LIMIT 1
                   ) AS undo_event_id,
                   (
                     SELECT p.plan_id FROM craft_purchases AS p
                     WHERE p.finance_event_id = e.id
                       AND p.undone_at IS NULL LIMIT 1
                   ) AS craft_purchase_plan_id,
                   (
                     SELECT b.plan_id FROM craft_batches AS b
                     WHERE b.finance_event_id = e.id
                       AND b.undone_at IS NULL LIMIT 1
                   ) AS craft_batch_plan_id
            FROM finance_events AS e
            WHERE {where}
            ORDER BY e.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_limit, page_offset],
        ).fetchall()
    return {
        "items": [
            {**dict(row), "is_undone": bool(row["is_undone"])}
            for row in rows
        ],
        "total": total,
        "limit": page_limit,
        "offset": page_offset,
        "days": selected_days,
        "cutoff": cutoff,
    }


__all__ = [
    "admin_audit_actions",
    "admin_craft_events",
    "admin_dashboard_counts",
    "admin_discord_events",
    "admin_discord_stats",
    "admin_finance_events",
]
