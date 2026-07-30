"""Read-optimized projections for the role-aware T-Mod web portal."""

from __future__ import annotations

import json
from typing import Any

from persistence.core import _db_lock, connect


MARKET_CATEGORIES = frozenset({"items", "vehicles", "clothes"})


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(int(limit), 100)), max(0, min(int(offset), 50_000))


def _query(value: str | None) -> str:
    return str(value or "").strip().casefold()[:160]


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def portal_linked_record(
    guild_id: int,
    kind: str,
    record_key: str,
) -> dict[str, Any] | None:
    """Resolve one stable, guild-scoped record used by an admin deep link."""
    selected_kind = str(kind or "").strip().lower()
    clean_key = str(record_key or "").strip()
    if not clean_key or len(clean_key) > 160:
        return None

    with _db_lock, connect() as con:
        if selected_kind == "market":
            parts = clean_key.split(".", 2)
            if len(parts) != 3:
                return None
            server_id, category, raw_item_id = parts
            if category not in MARKET_CATEGORIES:
                return None
            try:
                item_id = int(raw_item_id)
            except (TypeError, ValueError):
                return None
            row = con.execute(
                """
                SELECT server_id, category, item_id, external_id, item_name,
                       metadata_json, total_count, sold_count, average_price,
                       min_price, max_price, source_updated_at, fetched_at
                FROM market_items
                WHERE server_id = ? AND category = ? AND item_id = ?
                  AND active = 1
                """,
                (server_id.upper()[:20], category, item_id),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["metadata"] = _json_object(item.pop("metadata_json", "{}"))
            return item

        try:
            numeric_key = int(clean_key)
        except (TypeError, ValueError):
            numeric_key = None

        if selected_kind == "audit" and numeric_key is not None:
            row = con.execute(
                """
                SELECT id, actor_id, actor_display, module, action_kind,
                       target_type, target_id, summary, reversible, status,
                       undone_by_id, undone_by_display, undone_at, undo_reason,
                       created_at
                FROM bot_actions
                WHERE guild_id = ? AND id = ?
                """,
                (int(guild_id), numeric_key),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["reversible"] = bool(item.get("reversible"))
            return item

        if selected_kind == "finance" and numeric_key is not None:
            row = con.execute(
                "SELECT * FROM finance_events WHERE guild_id = ? AND id = ?",
                (int(guild_id), numeric_key),
            ).fetchone()
            return dict(row) if row is not None else None

        if selected_kind == "craft-event" and numeric_key is not None:
            row = con.execute(
                """
                SELECT e.id, e.plan_id, e.event_kind, e.actor_id,
                       e.actor_display, e.details_json, e.thread_message_id,
                       e.created_at, p.product_name_snapshot AS product_name,
                       p.channel_id, p.thread_id, p.stage
                FROM craft_events AS e
                JOIN craft_plans AS p ON p.id = e.plan_id
                WHERE e.guild_id = ? AND e.id = ?
                """,
                (int(guild_id), numeric_key),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["details"] = _json_object(item.pop("details_json", "{}"))
            return item

        if selected_kind == "discord-event" and numeric_key is not None:
            row = con.execute(
                """
                SELECT e.id, e.user_id, e.event_type, e.event_text, e.at,
                       e.channel_id, e.channel_name, e.category_id,
                       e.category_name, e.details, e.message_id,
                       COALESCE(m.display_name, m.name, CAST(e.user_id AS TEXT))
                         AS display_name,
                       COALESCE(m.is_bot, 0) AS is_bot
                FROM activity_events AS e
                LEFT JOIN members AS m
                  ON m.guild_id = e.guild_id AND m.user_id = e.user_id
                WHERE e.guild_id = ? AND e.id = ?
                """,
                (int(guild_id), numeric_key),
            ).fetchone()
            return (
                {**dict(row), "is_bot": bool(row["is_bot"])}
                if row is not None
                else None
            )

        if selected_kind == "case" and numeric_key is not None:
            row = con.execute(
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM sgl_case_events AS e
                         WHERE e.case_id = c.id) AS event_count,
                       (SELECT COUNT(*) FROM sgl_receipts AS r
                         WHERE r.case_id = c.id) AS receipt_count
                FROM sgl_cases AS c
                WHERE c.guild_id = ? AND c.id = ?
                """,
                (int(guild_id), numeric_key),
            ).fetchone()
            return dict(row) if row is not None else None

        if selected_kind == "archive" and numeric_key is not None:
            row = con.execute(
                """
                SELECT id, case_id, case_number, original_channel_id,
                       original_channel_name, status, message_count,
                       attachment_count, total_bytes, snapshot_completed_at,
                       source_deleted_at, last_error, created_at, updated_at
                FROM sgl_case_archives
                WHERE guild_id = ? AND id = ?
                """,
                (int(guild_id), numeric_key),
            ).fetchone()
            return dict(row) if row is not None else None

        if selected_kind == "member" and numeric_key is not None:
            row = con.execute(
                """
                SELECT m.guild_id, m.user_id, m.display_name, m.name, m.mention,
                       m.is_bot, m.updated_at,
                       p.status, p.status_note, p.visibility, p.show_directory,
                       p.biography, p.contribution, p.responsibilities,
                       p.membership_since, p.directory_completed_at,
                       s.last_activity_at, s.last_event, s.last_event_text,
                       COALESCE(s.total_events, 0) AS total_events,
                       (SELECT COUNT(*) FROM profile_characters AS c
                         WHERE c.guild_id = m.guild_id AND c.user_id = m.user_id)
                         AS character_count
                FROM members AS m
                LEFT JOIN member_profiles AS p
                  ON p.guild_id = m.guild_id AND p.user_id = m.user_id
                LEFT JOIN activity_summary AS s
                  ON s.guild_id = m.guild_id AND s.user_id = m.user_id
                WHERE m.guild_id = ? AND m.user_id = ?
                """,
                (int(guild_id), numeric_key),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["is_bot"] = bool(item.get("is_bot"))
            item["show_directory"] = bool(item.get("show_directory"))
            return item

        if selected_kind == "broadcast" and numeric_key is not None:
            row = con.execute(
                """
                SELECT b.*,
                       SUM(CASE WHEN r.status = 'delivered' THEN 1 ELSE 0 END)
                         AS delivered_count,
                       SUM(CASE WHEN r.status = 'queued' THEN 1 ELSE 0 END)
                         AS queued_count,
                       SUM(CASE WHEN r.status IN ('skipped', 'failed') THEN 1 ELSE 0 END)
                         AS problem_count
                FROM admin_broadcasts AS b
                LEFT JOIN admin_broadcast_recipients AS r
                  ON r.broadcast_id = b.id
                WHERE b.guild_id = ? AND b.id = ?
                GROUP BY b.id
                """,
                (int(guild_id), numeric_key),
            ).fetchone()
            return dict(row) if row is not None else None
    return None


def portal_registry(guild_id: int) -> dict[str, Any]:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM members WHERE guild_id = ?) AS members,
              (SELECT COUNT(*) FROM member_profiles WHERE guild_id = ?) AS profiles,
              (SELECT COUNT(*) FROM profile_characters WHERE guild_id = ?) AS characters,
              (SELECT COUNT(*) FROM tvrs_bills WHERE guild_id = ?) AS bills,
              (SELECT COUNT(*) FROM tvrs_bills
                WHERE guild_id = ? AND status IN ('submitted', 'voting')) AS bills_open,
              (SELECT COUNT(*) FROM sgl_cases WHERE guild_id = ?) AS sgl_cases,
              (SELECT COUNT(*) FROM sgl_cases
                WHERE guild_id = ? AND status != 'closed') AS sgl_open,
              (SELECT COUNT(*) FROM sgl_case_archives WHERE guild_id = ?) AS sgl_archives,
              (SELECT COUNT(*) FROM sgl_receipts WHERE guild_id = ?) AS sgl_receipts,
              (SELECT COUNT(*) FROM admin_broadcasts WHERE guild_id = ?) AS broadcasts,
              (SELECT COUNT(*) FROM audio_generations WHERE guild_id = ?) AS audio_jobs,
              (SELECT COUNT(*) FROM market_alerts WHERE guild_id = ?) AS market_alerts,
              (SELECT COUNT(*) FROM tvrs_bill_workspaces
                WHERE guild_id = ? AND status IN ('draft', 'review'))
                AS bill_workspaces,
              (SELECT COUNT(*) FROM delivery_outbox
                WHERE status IN ('pending', 'processing', 'retry')) AS deliveries_open,
              (SELECT COUNT(*) FROM delivery_outbox
                WHERE status = 'dead') AS deliveries_dead
            """,
            (int(guild_id),) * 13,
        ).fetchone()
        catalogs = con.execute(
            """
            SELECT server_id, category, server_name, record_count,
                   source_updated_at, last_success_at, last_error,
                   consecutive_failures
            FROM market_catalogs
            ORDER BY server_id, category
            """
        ).fetchall()
    return {
        **dict(row),
        "market_catalogs": [dict(item) for item in catalogs],
    }


def portal_market_search(
    *,
    server_id: str = "RU15",
    category: str = "items",
    query: str | None = None,
    sort: str = "popular",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    selected_server = str(server_id or "RU15").strip().upper()[:20]
    selected_category = str(category or "items").strip().lower()
    if selected_category not in MARKET_CATEGORIES:
        raise ValueError("market_category_invalid")
    order_by = {
        "popular": "sold_count DESC, total_count DESC, item_name COLLATE NOCASE",
        "name": "item_name COLLATE NOCASE, item_id",
        "price_asc": "average_price IS NULL, average_price ASC, item_name COLLATE NOCASE",
        "price_desc": "average_price IS NULL, average_price DESC, item_name COLLATE NOCASE",
        "supply": "total_count DESC, sold_count DESC, item_name COLLATE NOCASE",
    }.get(str(sort or "").strip().lower())
    if order_by is None:
        raise ValueError("market_sort_invalid")
    page_limit, page_offset = _page(limit, offset)
    clauses = ["server_id = ?", "category = ?", "active = 1"]
    params: list[Any] = [selected_server, selected_category]
    clean_query = _query(query)
    if clean_query:
        clauses.append(
            """
            (
              T_CASEFOLD(item_name) LIKE ?
              OR T_CASEFOLD(external_id) LIKE ?
              OR T_CASEFOLD(metadata_json) LIKE ?
            )
            """
        )
        needle = f"%{clean_query}%"
        params.extend([needle, needle, needle])
    where = " AND ".join(clauses)
    with _db_lock, connect() as con:
        total = int(
            con.execute(
                f"SELECT COUNT(*) AS n FROM market_items WHERE {where}",
                params,
            ).fetchone()["n"]
            or 0
        )
        rows = con.execute(
            f"""
            SELECT server_id, category, item_id, external_id, item_name,
                   metadata_json, total_count, sold_count, average_price,
                   min_price, max_price, source_updated_at, fetched_at
            FROM market_items
            WHERE {where}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            [*params, page_limit, page_offset],
        ).fetchall()
        catalog = con.execute(
            """
            SELECT * FROM market_catalogs
            WHERE server_id = ? AND category = ?
            """,
            (selected_server, selected_category),
        ).fetchone()
    items = []
    for row in rows:
        item = dict(row)
        item["metadata"] = _json_object(item.pop("metadata_json", "{}"))
        items.append(item)
    return {
        "items": items,
        "total": total,
        "limit": page_limit,
        "offset": page_offset,
        "server_id": selected_server,
        "category": selected_category,
        "catalog": dict(catalog) if catalog is not None else {},
    }


def portal_sgl_cases(
    guild_id: int,
    *,
    status: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    page_limit, page_offset = _page(limit, offset)
    clauses = ["c.guild_id = ?"]
    params: list[Any] = [int(guild_id)]
    if status:
        clauses.append("c.status = ?")
        params.append(str(status).strip()[:40])
    clean_query = _query(query)
    if clean_query:
        clauses.append(
            """
            (
              CAST(c.case_number AS TEXT) LIKE ?
              OR T_CASEFOLD(COALESCE(c.client_display, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(c.lead_lawyer_display, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(c.secretary_display, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(c.request_type, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(c.client_nick, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(c.static_id, '')) LIKE ?
            )
            """
        )
        needle = f"%{clean_query}%"
        params.extend([needle] * 7)
    where = " AND ".join(clauses)
    with _db_lock, connect() as con:
        total = int(
            con.execute(
                f"SELECT COUNT(*) AS n FROM sgl_cases AS c WHERE {where}",
                params,
            ).fetchone()["n"]
            or 0
        )
        rows = con.execute(
            f"""
            SELECT c.*,
                   (
                     SELECT COUNT(*) FROM sgl_case_events AS e
                     WHERE e.case_id = c.id
                   ) AS event_count,
                   (
                     SELECT COUNT(*) FROM sgl_receipts AS r
                     WHERE r.case_id = c.id
                   ) AS receipt_count,
                   (
                     SELECT a.status FROM sgl_case_archives AS a
                     WHERE a.case_id = c.id
                     ORDER BY a.id DESC LIMIT 1
                   ) AS archive_status
            FROM sgl_cases AS c
            WHERE {where}
            ORDER BY c.case_number DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_limit, page_offset],
        ).fetchall()
        statuses = [
            str(row["status"])
            for row in con.execute(
                """
                SELECT DISTINCT status FROM sgl_cases
                WHERE guild_id = ? ORDER BY status
                """,
                (int(guild_id),),
            ).fetchall()
        ]
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": page_limit,
        "offset": page_offset,
        "statuses": statuses,
    }


def portal_sgl_archives(
    guild_id: int,
    *,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    page_limit, page_offset = _page(limit, offset)
    clauses = ["guild_id = ?"]
    params: list[Any] = [int(guild_id)]
    clean_query = _query(query)
    if clean_query:
        clauses.append(
            """
            (
              CAST(case_number AS TEXT) LIKE ?
              OR T_CASEFOLD(original_channel_name) LIKE ?
              OR T_CASEFOLD(COALESCE(last_error, '')) LIKE ?
            )
            """
        )
        needle = f"%{clean_query}%"
        params.extend([needle, needle, needle])
    where = " AND ".join(clauses)
    with _db_lock, connect() as con:
        total = int(
            con.execute(
                f"SELECT COUNT(*) AS n FROM sgl_case_archives WHERE {where}",
                params,
            ).fetchone()["n"]
            or 0
        )
        rows = con.execute(
            f"""
            SELECT id, case_id, case_number, original_channel_id,
                   original_channel_name, status, message_count,
                   attachment_count, total_bytes, snapshot_completed_at,
                   source_deleted_at, last_error, created_at, updated_at
            FROM sgl_case_archives
            WHERE {where}
            ORDER BY case_number DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_limit, page_offset],
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": page_limit,
        "offset": page_offset,
    }


def portal_members(
    guild_id: int,
    *,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    page_limit, page_offset = _page(limit, offset)
    clauses = ["m.guild_id = ?"]
    params: list[Any] = [int(guild_id)]
    clean_query = _query(query)
    if clean_query:
        clauses.append(
            """
            (
              T_CASEFOLD(COALESCE(m.display_name, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(m.name, '')) LIKE ?
              OR CAST(m.user_id AS TEXT) LIKE ?
              OR T_CASEFOLD(COALESCE(p.biography, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(p.contribution, '')) LIKE ?
              OR T_CASEFOLD(COALESCE(p.responsibilities, '')) LIKE ?
            )
            """
        )
        needle = f"%{clean_query}%"
        params.extend([needle] * 6)
    where = " AND ".join(clauses)
    with _db_lock, connect() as con:
        total = int(
            con.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM members AS m
                LEFT JOIN member_profiles AS p
                  ON p.guild_id = m.guild_id AND p.user_id = m.user_id
                WHERE {where}
                """,
                params,
            ).fetchone()["n"]
            or 0
        )
        rows = con.execute(
            f"""
            SELECT m.guild_id, m.user_id, m.display_name, m.name, m.mention,
                   m.is_bot, m.updated_at,
                   p.status, p.status_note, p.visibility, p.show_directory,
                   p.biography, p.contribution, p.responsibilities,
                   p.membership_since, p.directory_completed_at,
                   s.last_activity_at, s.last_event, s.last_event_text,
                   COALESCE(s.total_events, 0) AS total_events,
                   (
                     SELECT COUNT(*) FROM profile_characters AS c
                     WHERE c.guild_id = m.guild_id AND c.user_id = m.user_id
                   ) AS character_count
            FROM members AS m
            LEFT JOIN member_profiles AS p
              ON p.guild_id = m.guild_id AND p.user_id = m.user_id
            LEFT JOIN activity_summary AS s
              ON s.guild_id = m.guild_id AND s.user_id = m.user_id
            WHERE {where}
            ORDER BY COALESCE(s.last_activity_at, m.updated_at) DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_limit, page_offset],
        ).fetchall()
    return {
        "items": [
            {
                **dict(row),
                "is_bot": bool(row["is_bot"]),
                "show_directory": bool(row["show_directory"])
                if row["show_directory"] is not None
                else False,
            }
            for row in rows
        ],
        "total": total,
        "limit": page_limit,
        "offset": page_offset,
    }


def portal_broadcasts(
    guild_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    page_limit, page_offset = _page(limit, offset)
    with _db_lock, connect() as con:
        total = int(
            con.execute(
                """
                SELECT COUNT(*) AS n FROM admin_broadcasts
                WHERE guild_id = ?
                """,
                (int(guild_id),),
            ).fetchone()["n"]
            or 0
        )
        rows = con.execute(
            """
            SELECT b.*,
                   SUM(CASE WHEN r.status = 'delivered' THEN 1 ELSE 0 END)
                     AS delivered_count,
                   SUM(CASE WHEN r.status = 'queued' THEN 1 ELSE 0 END)
                     AS queued_count,
                   SUM(CASE WHEN r.status IN ('skipped', 'failed') THEN 1 ELSE 0 END)
                     AS problem_count
            FROM admin_broadcasts AS b
            LEFT JOIN admin_broadcast_recipients AS r
              ON r.broadcast_id = b.id
            WHERE b.guild_id = ?
            GROUP BY b.id
            ORDER BY b.id DESC
            LIMIT ? OFFSET ?
            """,
            (int(guild_id), page_limit, page_offset),
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": page_limit,
        "offset": page_offset,
    }


def portal_system(guild_id: int, *, limit: int = 50) -> dict[str, Any]:
    page_limit = max(1, min(int(limit), 100))
    with _db_lock, connect() as con:
        outbox_status = con.execute(
            """
            SELECT status, COUNT(*) AS items,
                   COALESCE(SUM(attempts), 0) AS attempts
            FROM delivery_outbox
            GROUP BY status
            ORDER BY items DESC
            """
        ).fetchall()
        outbox_topics = con.execute(
            """
            SELECT topic, COUNT(*) AS items,
                   SUM(CASE WHEN status = 'dead' THEN 1 ELSE 0 END) AS dead
            FROM delivery_outbox
            GROUP BY topic
            ORDER BY items DESC
            LIMIT 12
            """
        ).fetchall()
        recent_deliveries = con.execute(
            """
            SELECT id, topic, status, attempts, max_attempts, priority,
                   available_at, last_error, delivered_at, created_at, updated_at
            FROM delivery_outbox
            WHERE status IN ('retry', 'dead', 'processing')
            ORDER BY id DESC
            LIMIT ?
            """,
            (page_limit,),
        ).fetchall()
        audio = con.execute(
            """
            SELECT id, user_id, user_display, prompt, model, status,
                   seconds_elapsed, output_filename, output_size_bytes,
                   error, created_at, updated_at
            FROM audio_generations
            WHERE guild_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (int(guild_id),),
        ).fetchall()
        consensus_sessions = con.execute(
            """
            SELECT session_key, plenary_number, stage, leader_id,
                   current_bill_id, revision, created_at, updated_at, finished_at
            FROM tvrs_consensus_sessions
            WHERE guild_id = ?
            ORDER BY updated_at DESC
            LIMIT 12
            """,
            (int(guild_id),),
        ).fetchall()
        workspaces = con.execute(
            """
            SELECT id, author_id, author_display, status, title, updated_at,
                   submitted_bill_id, ai_model, ai_revision, revision
            FROM tvrs_bill_workspaces
            WHERE guild_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (int(guild_id),),
        ).fetchall()
    return {
        "outbox_status": [dict(row) for row in outbox_status],
        "outbox_topics": [dict(row) for row in outbox_topics],
        "recent_deliveries": [dict(row) for row in recent_deliveries],
        "audio_generations": [dict(row) for row in audio],
        "consensus_sessions": [dict(row) for row in consensus_sessions],
        "bill_workspaces": [dict(row) for row in workspaces],
    }


__all__ = [
    "MARKET_CATEGORIES",
    "portal_broadcasts",
    "portal_linked_record",
    "portal_market_search",
    "portal_members",
    "portal_registry",
    "portal_sgl_archives",
    "portal_sgl_cases",
    "portal_system",
]
