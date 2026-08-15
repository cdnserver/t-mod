"""Durable projections and preferences for the T-Mod Reactor surfaces."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


ADMIN_WIDGETS = (
    "attention",
    "health",
    "treasury",
    "craft",
    "events",
    "discord",
    "minecraft",
)
MEMBER_WIDGETS = (
    "identity",
    "treasury",
    "legislation",
    "my_bills",
    "editor",
    "tasks",
    "games",
    "consensus",
    "notifications",
)
_SURFACE_WIDGETS = {
    "admin": ADMIN_WIDGETS,
    "member": MEMBER_WIDGETS,
}
_SEVERITIES = frozenset({"info", "success", "warning", "critical"})


def _clean_surface(surface: str) -> str:
    selected = str(surface or "").strip().lower()
    if selected not in _SURFACE_WIDGETS:
        raise ValueError("reactor_surface_invalid")
    return selected


def _clean_layout(surface: str, layout: Iterable[str]) -> list[str]:
    allowed = _SURFACE_WIDGETS[_clean_surface(surface)]
    selected: list[str] = []
    for raw in layout:
        widget = str(raw or "").strip().lower()
        if widget in allowed and widget not in selected:
            selected.append(widget)
    if not selected:
        return list(allowed)
    return selected


def reactor_get_layout(guild_id: int, user_id: int, surface: str) -> list[str]:
    selected_surface = _clean_surface(surface)
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT layout_json FROM reactor_preferences
            WHERE guild_id = ? AND user_id = ? AND surface = ?
            """,
            (int(guild_id), int(user_id), selected_surface),
        ).fetchone()
    if row is None:
        return list(_SURFACE_WIDGETS[selected_surface])
    try:
        parsed = json.loads(str(row["layout_json"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = []
    if selected_surface == "member" and isinstance(parsed, list) and any(
        str(item) in {"market", "characters"} for item in parsed
    ):
        # Stored layouts from the first Reactor version referenced retired
        # widgets. Upgrade them atomically in projection so treasury and the
        # legislation editor cannot silently disappear after deployment.
        return list(MEMBER_WIDGETS)
    if selected_surface == "member" and isinstance(parsed, list):
        for widget in ("games", "my_bills", "tasks"):
            if widget not in parsed:
                parsed.append(widget)
    return _clean_layout(
        selected_surface,
        parsed if isinstance(parsed, list) else [],
    )


def reactor_set_layout(
    guild_id: int,
    user_id: int,
    surface: str,
    layout: Iterable[str],
) -> list[str]:
    selected_surface = _clean_surface(surface)
    selected = _clean_layout(selected_surface, layout)
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            INSERT INTO reactor_preferences(
                guild_id, user_id, surface, layout_json, updated_at
            )
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, surface) DO UPDATE SET
                layout_json = excluded.layout_json,
                updated_at = excluded.updated_at
            """,
            (
                int(guild_id),
                int(user_id),
                selected_surface,
                json.dumps(selected, ensure_ascii=False),
                now,
            ),
        )
        con.commit()
    return selected


def reactor_put_notification(
    *,
    guild_id: int,
    user_id: int,
    severity: str,
    kind: str,
    title: str,
    body: str,
    dedupe_key: str,
    route: str | None = None,
    source_key: str | None = None,
    expires_at: str | None = None,
) -> int:
    selected_severity = str(severity or "info").strip().lower()
    if selected_severity not in _SEVERITIES:
        raise ValueError("reactor_notification_severity_invalid")
    clean_kind = str(kind or "system").strip().lower()[:80] or "system"
    clean_title = str(title or "").strip()[:180]
    clean_body = str(body or "").strip()[:2000]
    clean_dedupe = str(dedupe_key or "").strip()[:180]
    if not clean_title or not clean_body or not clean_dedupe:
        raise ValueError("reactor_notification_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            INSERT INTO reactor_notifications(
                guild_id, user_id, severity, kind, title, body, route,
                source_key, dedupe_key, expires_at, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, dedupe_key) DO UPDATE SET
                severity = excluded.severity,
                kind = excluded.kind,
                title = excluded.title,
                body = excluded.body,
                route = excluded.route,
                source_key = excluded.source_key,
                expires_at = excluded.expires_at,
                read_at = CASE
                    WHEN reactor_notifications.title != excluded.title
                      OR reactor_notifications.body != excluded.body
                      OR reactor_notifications.severity != excluded.severity
                    THEN NULL ELSE reactor_notifications.read_at END,
                updated_at = excluded.updated_at
            """,
            (
                int(guild_id),
                int(user_id),
                selected_severity,
                clean_kind,
                clean_title,
                clean_body,
                str(route or "").strip()[:500] or None,
                str(source_key or "").strip()[:180] or None,
                clean_dedupe,
                expires_at,
                now,
                now,
            ),
        )
        row = con.execute(
            """
            SELECT id FROM reactor_notifications
            WHERE guild_id = ? AND user_id = ? AND dedupe_key = ?
            """,
            (int(guild_id), int(user_id), clean_dedupe),
        ).fetchone()
        con.commit()
    return int(row["id"])


def reactor_list_notifications(
    guild_id: int,
    user_id: int,
    *,
    unread_only: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    page_limit = max(1, min(int(limit), 100))
    now = datetime.now(timezone.utc).isoformat()
    clauses = ["guild_id = ?", "user_id = ?", "(expires_at IS NULL OR expires_at > ?)"]
    params: list[Any] = [int(guild_id), int(user_id), now]
    if unread_only:
        clauses.append("read_at IS NULL")
    where = " AND ".join(clauses)
    with connect_readonly() as con:
        unread = int(
            con.execute(
                """
                SELECT COUNT(*) AS n FROM reactor_notifications
                WHERE guild_id = ? AND user_id = ? AND read_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (int(guild_id), int(user_id), now),
            ).fetchone()["n"]
            or 0
        )
        rows = con.execute(
            f"""
            SELECT id, severity, kind, title, body, route, source_key,
                   read_at, created_at, updated_at
            FROM reactor_notifications
            WHERE {where}
            ORDER BY CASE severity
                       WHEN 'critical' THEN 0 WHEN 'warning' THEN 1
                       WHEN 'success' THEN 2 ELSE 3 END,
                     id DESC
            LIMIT ?
            """,
            [*params, page_limit],
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "unread": unread,
    }


def reactor_mark_notifications_read(
    guild_id: int,
    user_id: int,
    notification_ids: Iterable[int] | None = None,
) -> int:
    ids: list[int] = []
    for value in notification_ids or ():
        try:
            selected = int(value)
        except (TypeError, ValueError):
            continue
        if selected > 0 and selected not in ids:
            ids.append(selected)
    ids.sort()
    now = utc_now_iso()
    with _db_lock, connect() as con:
        if ids:
            placeholders = ",".join("?" for _ in ids)
            cursor = con.execute(
                f"""
                UPDATE reactor_notifications SET read_at = ?, updated_at = ?
                WHERE guild_id = ? AND user_id = ? AND read_at IS NULL
                  AND id IN ({placeholders})
                """,
                [now, now, int(guild_id), int(user_id), *ids],
            )
        else:
            cursor = con.execute(
                """
                UPDATE reactor_notifications SET read_at = ?, updated_at = ?
                WHERE guild_id = ? AND user_id = ? AND read_at IS NULL
                """,
                (now, now, int(guild_id), int(user_id)),
            )
        con.commit()
    return max(0, int(cursor.rowcount))


def reactor_resolve_notifications(
    guild_id: int,
    user_id: int,
    kind: str,
    active_dedupe_keys: Iterable[str],
) -> int:
    """Mark projections that disappeared from the current attention set as read."""
    selected_kind = str(kind or "").strip().lower()[:80]
    active = sorted(
        {
            str(value or "").strip()[:180]
            for value in active_dedupe_keys
            if str(value or "").strip()
        }
    )
    now = utc_now_iso()
    params: list[Any] = [now, now, int(guild_id), int(user_id), selected_kind]
    exclusion = ""
    if active:
        exclusion = f" AND dedupe_key NOT IN ({','.join('?' for _ in active)})"
        params.extend(active)
    with _db_lock, connect() as con:
        cursor = con.execute(
            f"""
            UPDATE reactor_notifications SET read_at = ?, updated_at = ?
            WHERE guild_id = ? AND user_id = ? AND kind = ?
              AND read_at IS NULL{exclusion}
            """,
            params,
        )
        con.commit()
    return max(0, int(cursor.rowcount))


def reactor_sync_notifications(
    guild_id: int,
    user_id: int,
    kind: str,
    notifications: Iterable[dict[str, Any]],
) -> int:
    """Upsert one projection and resolve stale entries in one transaction."""

    clean_kind = str(kind or "system").strip().lower()[:80] or "system"
    now = utc_now_iso()
    prepared: list[tuple[Any, ...]] = []
    active: list[str] = []
    for item in notifications:
        severity = str(item.get("severity") or "info").strip().lower()
        title = str(item.get("title") or "").strip()[:180]
        body = str(item.get("body") or "").strip()[:2000]
        dedupe_key = str(item.get("dedupe_key") or "").strip()[:180]
        if severity not in _SEVERITIES:
            raise ValueError("reactor_notification_severity_invalid")
        if not title or not body or not dedupe_key:
            raise ValueError("reactor_notification_invalid")
        active.append(dedupe_key)
        prepared.append(
            (
                int(guild_id),
                int(user_id),
                severity,
                clean_kind,
                title,
                body,
                str(item.get("route") or "").strip()[:500] or None,
                str(item.get("source_key") or "").strip()[:180] or None,
                dedupe_key,
                item.get("expires_at"),
                now,
                now,
            )
        )

    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if prepared:
            con.executemany(
                """
                INSERT INTO reactor_notifications(
                    guild_id, user_id, severity, kind, title, body, route,
                    source_key, dedupe_key, expires_at, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, dedupe_key) DO UPDATE SET
                    severity = excluded.severity,
                    kind = excluded.kind,
                    title = excluded.title,
                    body = excluded.body,
                    route = excluded.route,
                    source_key = excluded.source_key,
                    expires_at = excluded.expires_at,
                    read_at = CASE
                        WHEN reactor_notifications.title != excluded.title
                          OR reactor_notifications.body != excluded.body
                          OR reactor_notifications.severity != excluded.severity
                        THEN NULL ELSE reactor_notifications.read_at END,
                    updated_at = excluded.updated_at
                """,
                prepared,
            )
        params: list[Any] = [now, now, int(guild_id), int(user_id), clean_kind]
        exclusion = ""
        if active:
            exclusion = f" AND dedupe_key NOT IN ({','.join('?' for _ in active)})"
            params.extend(active)
        con.execute(
            f"""
            UPDATE reactor_notifications SET read_at = ?, updated_at = ?
            WHERE guild_id = ? AND user_id = ? AND kind = ?
              AND read_at IS NULL{exclusion}
            """,
            params,
        )
        con.commit()
    return len(prepared)


def reactor_event_feed(
    guild_id: int,
    *,
    after_id: int = 0,
    limit: int = 50,
) -> list[dict[str, Any]]:
    page_limit = max(1, min(int(limit), 100))
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT id, actor_id, actor_display, module, action_kind,
                   target_type, target_id, summary, status, created_at
            FROM bot_actions
            WHERE guild_id = ? AND id > ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(guild_id), max(0, int(after_id)), page_limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def reactor_database_health() -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    try:
        with connect_readonly() as con:
            con.execute("SELECT 1").fetchone()
            journal_mode = str(con.execute("PRAGMA journal_mode").fetchone()[0])
            page_count = int(con.execute("PRAGMA page_count").fetchone()[0])
            sequence = con.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'bot_actions'"
            ).fetchone()
            actions = int(sequence["seq"] or 0) if sequence is not None else 0
        elapsed = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        return {
            "status": "ok",
            "latency_ms": round(elapsed, 1),
            "integrity": "available",
            "journal_mode": journal_mode,
            "page_count": page_count,
            "actions": actions,
        }
    except Exception as exc:  # health boundary must always return a payload
        return {
            "status": "critical",
            "latency_ms": None,
            "integrity": "unavailable",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }


def reactor_global_search(
    guild_id: int,
    query: str,
    *,
    limit: int = 30,
) -> list[dict[str, Any]]:
    clean_query = str(query or "").strip().casefold()[:160]
    if len(clean_query) < 2:
        return []
    needle = f"%{clean_query}%"
    per_kind = max(2, min(8, int(limit)))
    results: list[dict[str, Any]] = []
    with connect_readonly() as con:
        members = con.execute(
            """
            SELECT user_id, COALESCE(display_name, name, CAST(user_id AS TEXT)) AS title,
                   COALESCE(name, 'Участник Discord') AS subtitle
            FROM members
            WHERE guild_id = ? AND (
                T_CASEFOLD(COALESCE(display_name, '')) LIKE ?
                OR T_CASEFOLD(COALESCE(name, '')) LIKE ?
                OR CAST(user_id AS TEXT) LIKE ?
            )
            ORDER BY updated_at DESC LIMIT ?
            """,
            (int(guild_id), needle, needle, needle, per_kind),
        ).fetchall()
        bills = con.execute(
            """
            SELECT id, bill_number, title, author_display, status
            FROM tvrs_bills
            WHERE guild_id = ? AND (
                CAST(bill_number AS TEXT) LIKE ?
                OR T_CASEFOLD(title) LIKE ?
                OR T_CASEFOLD(summary) LIKE ?
                OR T_CASEFOLD(COALESCE(author_display, '')) LIKE ?
            )
            ORDER BY bill_number DESC LIMIT ?
            """,
            (int(guild_id), needle, needle, needle, needle, per_kind),
        ).fetchall()
        crafts = con.execute(
            """
            SELECT id, COALESCE(product_name_snapshot, 'Крафт') AS title,
                   stage, responsible_display
            FROM craft_plans
            WHERE guild_id = ? AND (
                CAST(id AS TEXT) LIKE ?
                OR T_CASEFOLD(COALESCE(product_name_snapshot, '')) LIKE ?
                OR T_CASEFOLD(COALESCE(responsible_display, '')) LIKE ?
            )
            ORDER BY id DESC LIMIT ?
            """,
            (int(guild_id), needle, needle, needle, per_kind),
        ).fetchall()
        cases = con.execute(
            """
            SELECT id, case_number, COALESCE(client_nick, client_display, 'Клиент') AS title,
                   status, lead_lawyer_display
            FROM sgl_cases
            WHERE guild_id = ? AND (
                CAST(case_number AS TEXT) LIKE ?
                OR T_CASEFOLD(COALESCE(client_nick, '')) LIKE ?
                OR T_CASEFOLD(COALESCE(client_display, '')) LIKE ?
                OR T_CASEFOLD(COALESCE(lead_lawyer_display, '')) LIKE ?
            )
            ORDER BY case_number DESC LIMIT ?
            """,
            (int(guild_id), needle, needle, needle, needle, per_kind),
        ).fetchall()
        market = con.execute(
            """
            SELECT server_id, category, item_id, item_name, average_price
            FROM market_items
            WHERE active = 1 AND (
                T_CASEFOLD(item_name) LIKE ? OR T_CASEFOLD(external_id) LIKE ?
            )
            ORDER BY updated_at DESC LIMIT ?
            """,
            (needle, needle, per_kind),
        ).fetchall()
        actions = con.execute(
            """
            SELECT id, summary, module, actor_display
            FROM bot_actions
            WHERE guild_id = ? AND (
                T_CASEFOLD(summary) LIKE ?
                OR T_CASEFOLD(COALESCE(actor_display, '')) LIKE ?
            )
            ORDER BY id DESC LIMIT ?
            """,
            (int(guild_id), needle, needle, per_kind),
        ).fetchall()
    results.extend(
        {
            "kind": "member",
            "key": str(row["user_id"]),
            "title": str(row["title"]),
            "subtitle": str(row["subtitle"]),
            "section": "members",
            "route": f"#/members/member/{row['user_id']}",
        }
        for row in members
    )
    results.extend(
        {
            "kind": "bill",
            "key": str(row["id"]),
            "title": f"№{int(row['bill_number']):03d} · {row['title']}",
            "subtitle": f"{row['author_display'] or 'Автор не указан'} · {row['status']}",
            "section": "bills",
            "route": f"#/bills/bill/{row['id']}",
        }
        for row in bills
    )
    results.extend(
        {
            "kind": "craft",
            "key": str(row["id"]),
            "title": f"Крафт #{row['id']} · {row['title']}",
            "subtitle": f"{row['stage']} · {row['responsible_display'] or 'не назначен'}",
            "section": "craft",
            "route": f"#/craft/craft-plan/{row['id']}",
        }
        for row in crafts
    )
    results.extend(
        {
            "kind": "case",
            "key": str(row["id"]),
            "title": f"Кейс СГЛ №{int(row['case_number']):03d} · {row['title']}",
            "subtitle": f"{row['status']} · {row['lead_lawyer_display'] or 'без сотрудника'}",
            "section": "sgl",
            "route": f"#/sgl/case/{row['id']}",
        }
        for row in cases
    )
    results.extend(
        {
            "kind": "market",
            "key": f"{row['server_id']}.{row['category']}.{row['item_id']}",
            "title": str(row["item_name"]),
            "subtitle": f"{row['category']} · {row['average_price'] or 'цена неизвестна'}",
            "section": "market",
            "route": f"#/market/market/{row['server_id']}.{row['category']}.{row['item_id']}",
        }
        for row in market
    )
    results.extend(
        {
            "kind": "audit",
            "key": str(row["id"]),
            "title": str(row["summary"]),
            "subtitle": f"{row['module']} · {row['actor_display'] or 'T-Mod'}",
            "section": "audit",
            "route": f"#/audit/audit/{row['id']}",
        }
        for row in actions
    )
    return results[: max(1, min(int(limit), 60))]


__all__ = [
    "ADMIN_WIDGETS",
    "MEMBER_WIDGETS",
    "reactor_database_health",
    "reactor_event_feed",
    "reactor_get_layout",
    "reactor_global_search",
    "reactor_list_notifications",
    "reactor_mark_notifications_read",
    "reactor_put_notification",
    "reactor_resolve_notifications",
    "reactor_set_layout",
    "reactor_sync_notifications",
]
