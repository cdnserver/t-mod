from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from persistence.core import ActivityEvent, ActivitySummary, _db_lock, connect, utc_now_iso

def get_meta(key: str) -> str | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    now = utc_now_iso()
    con.execute(
        """
        INSERT INTO meta(key, value, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now),
    )


def set_meta_value(key: str, value: str) -> None:
    """Persist one metadata value using the shared connection policy."""

    with _db_lock, connect() as con:
        set_meta(con, key, value)
        con.commit()


def _record_bot_action(
    con: sqlite3.Connection,
    *,
    guild_id: int,
    actor_id: int | None,
    actor_display: str | None,
    module: str,
    action_kind: str,
    target_type: str,
    target_id: int | str | None,
    summary: str,
    payload: dict[str, Any] | None = None,
    reversible: bool = True,
    now: str | None = None,
) -> int:
    cur = con.execute(
        """
        INSERT INTO bot_actions(
            guild_id, actor_id, actor_display, module, action_kind,
            target_type, target_id, summary, payload_json,
            reversible, status, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (
            guild_id,
            actor_id,
            actor_display,
            str(module),
            str(action_kind),
            str(target_type),
            None if target_id is None else str(target_id),
            str(summary)[:1000],
            json.dumps(payload or {}, ensure_ascii=False),
            1 if reversible else 0,
            now or utc_now_iso(),
        ),
    )
    return int(cur.lastrowid)


def bot_record_action(
    *,
    guild_id: int,
    actor_id: int | None,
    actor_display: str | None,
    module: str,
    action_kind: str,
    target_type: str,
    target_id: int | str | None,
    summary: str,
    payload: dict[str, Any] | None = None,
    reversible: bool = False,
) -> int:
    with _db_lock, connect() as con:
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module=module,
            action_kind=action_kind,
            target_type=target_type,
            target_id=target_id,
            summary=summary,
            payload=payload,
            reversible=reversible,
        )
        con.commit()
        return action_id


def _bot_action_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    try:
        result["payload"] = json.loads(str(result.get("payload_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        result["payload"] = {}
    return result


def bot_get_action(action_id: int, guild_id: int | None = None) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        if guild_id is None:
            row = con.execute("SELECT * FROM bot_actions WHERE id = ?", (action_id,)).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM bot_actions WHERE id = ? AND guild_id = ?",
                (action_id, guild_id),
            ).fetchone()
    return _bot_action_dict(row)


def bot_list_actions(
    guild_id: int,
    *,
    actor_id: int | None = None,
    module: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    clauses = ["guild_id = ?"]
    params: list[Any] = [guild_id]
    if actor_id is not None:
        clauses.append("actor_id = ?")
        params.append(actor_id)
    if module:
        clauses.append("module = ?")
        params.append(str(module))
    if status:
        clauses.append("status = ?")
        params.append(str(status))
    params.append(max(1, min(int(limit), 100)))
    with _db_lock, connect() as con:
        rows = con.execute(
            f"SELECT * FROM bot_actions WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return [_bot_action_dict(row) for row in rows if row is not None]


def upsert_member(
    con: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    display_name: str | None,
    name: str | None,
    mention: str | None,
    is_bot: bool = False,
) -> None:
    now = utc_now_iso()
    con.execute(
        """
        INSERT INTO members(guild_id, user_id, display_name, name, mention, is_bot, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            display_name = excluded.display_name,
            name = excluded.name,
            mention = excluded.mention,
            is_bot = excluded.is_bot,
            updated_at = excluded.updated_at
        """,
        (guild_id, user_id, display_name, name, mention, 1 if is_bot else 0, now, now),
    )


def should_debounce(
    con: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    event_type: str,
    channel_id: int | None,
    debounce_seconds: int,
) -> bool:
    row = con.execute(
        """
        SELECT last_event, last_channel_id, last_activity_at
        FROM activity_summary
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id),
    ).fetchone()
    if row is None:
        return False
    if row["last_event"] != event_type:
        return False
    if row["last_channel_id"] != channel_id:
        return False
    try:
        last_dt = datetime.fromisoformat(row["last_activity_at"])
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    return (datetime.now(timezone.utc) - last_dt.astimezone(timezone.utc)).total_seconds() < debounce_seconds


def remember_activity(
    *,
    guild_id: int,
    user_id: int,
    display_name: str | None,
    name: str | None,
    mention: str | None,
    is_bot: bool,
    event_type: str,
    event_text: str,
    channel_id: int | None = None,
    channel_name: str | None = None,
    category_id: int | None = None,
    category_name: str | None = None,
    details: str | None = None,
    message_id: int | None = None,
    debounce_seconds: int = 30,
    force: bool = False,
) -> bool:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        upsert_member(con, guild_id, user_id, display_name, name, mention, is_bot)
        if not force and should_debounce(con, guild_id, user_id, event_type, channel_id, debounce_seconds):
            con.commit()
            return False

        con.execute(
            """
            INSERT INTO activity_events(
                guild_id, user_id, event_type, event_text, at,
                channel_id, channel_name, category_id, category_name,
                details, message_id, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, event_type, event_text, now, channel_id, channel_name, category_id, category_name, details, message_id, now),
        )
        con.execute(
            """
            INSERT INTO activity_counters(guild_id, user_id, event_type, count, updated_at)
            VALUES(?, ?, ?, 1, ?)
            ON CONFLICT(guild_id, user_id, event_type) DO UPDATE SET
                count = activity_counters.count + 1,
                updated_at = excluded.updated_at
            """,
            (guild_id, user_id, event_type, now),
        )
        con.execute(
            """
            INSERT INTO activity_summary(
                guild_id, user_id, last_activity_at, last_event, last_event_text,
                last_channel_id, last_channel_name, last_category_id, last_category_name,
                last_details, total_events, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                last_activity_at = excluded.last_activity_at,
                last_event = excluded.last_event,
                last_event_text = excluded.last_event_text,
                last_channel_id = excluded.last_channel_id,
                last_channel_name = excluded.last_channel_name,
                last_category_id = excluded.last_category_id,
                last_category_name = excluded.last_category_name,
                last_details = excluded.last_details,
                total_events = activity_summary.total_events + 1,
                updated_at = excluded.updated_at
            """,
            (guild_id, user_id, now, event_type, event_text, channel_id, channel_name, category_id, category_name, details, now, now),
        )
        con.commit()
        return True


def _summary_from_row(row: sqlite3.Row) -> ActivitySummary:
    return ActivitySummary(
        guild_id=int(row["guild_id"]),
        user_id=int(row["user_id"]),
        display_name=row["display_name"],
        name=row["name"],
        mention=row["mention"],
        last_activity_at=row["last_activity_at"],
        last_event=row["last_event"],
        last_event_text=row["last_event_text"],
        last_channel_id=row["last_channel_id"],
        last_channel_name=row["last_channel_name"],
        last_category_id=row["last_category_id"] if "last_category_id" in row.keys() else None,
        last_category_name=row["last_category_name"] if "last_category_name" in row.keys() else None,
        last_details=row["last_details"],
        total_events=int(row["total_events"] or 0),
    )


def get_summaries(guild_id: int, user_ids: list[int]) -> dict[int, ActivitySummary]:
    if not user_ids:
        return {}
    placeholders = ",".join("?" for _ in user_ids)
    params: list[Any] = [guild_id, *user_ids]
    query = f"""
        SELECT
            s.guild_id,
            s.user_id,
            m.display_name,
            m.name,
            m.mention,
            s.last_activity_at,
            s.last_event,
            s.last_event_text,
            s.last_channel_id,
            s.last_channel_name,
            s.last_category_id,
            s.last_category_name,
            s.last_details,
            s.total_events
        FROM activity_summary s
        LEFT JOIN members m ON m.guild_id = s.guild_id AND m.user_id = s.user_id
        WHERE s.guild_id = ? AND s.user_id IN ({placeholders})
    """
    with _db_lock, connect() as con:
        rows = con.execute(query, params).fetchall()
    return {int(row["user_id"]): _summary_from_row(row) for row in rows}


def _build_activity_filter(
    *,
    event_types: Iterable[str] | None = None,
    category_id: int | None = None,
    channel_ids: Iterable[int] | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if event_types is not None:
        event_types = list(event_types)
        if not event_types:
            clauses.append("1 = 0")
        else:
            placeholders = ",".join("?" for _ in event_types)
            clauses.append(f"e.event_type IN ({placeholders})")
            params.extend(event_types)

    scope_clauses: list[str] = []
    if category_id is not None:
        scope_clauses.append("e.category_id = ?")
        params.append(category_id)
    if channel_ids is not None:
        ids = [int(x) for x in channel_ids]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            scope_clauses.append(f"e.channel_id IN ({placeholders})")
            params.extend(ids)
    if scope_clauses:
        clauses.append("(" + " OR ".join(scope_clauses) + ")")

    return (" AND ".join(clauses), params)


def get_filtered_summaries(
    guild_id: int,
    user_ids: list[int],
    *,
    event_types: Iterable[str] | None = None,
    category_id: int | None = None,
    channel_ids: Iterable[int] | None = None,
) -> dict[int, ActivitySummary]:
    if not user_ids:
        return {}

    user_placeholders = ",".join("?" for _ in user_ids)
    filter_sql, filter_params = _build_activity_filter(event_types=event_types, category_id=category_id, channel_ids=channel_ids)
    if filter_sql:
        filter_sql = " AND " + filter_sql

    params: list[Any] = [guild_id, *user_ids, *filter_params, guild_id, *user_ids, *filter_params]
    query = f"""
        WITH filtered AS (
            SELECT e.*
            FROM activity_events e
            WHERE e.guild_id = ? AND e.user_id IN ({user_placeholders}) {filter_sql}
        ),
        latest AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY at DESC, id DESC) AS rn
            FROM filtered
        ),
        counts AS (
            SELECT e.user_id, COUNT(*) AS total_events
            FROM activity_events e
            WHERE e.guild_id = ? AND e.user_id IN ({user_placeholders}) {filter_sql}
            GROUP BY e.user_id
        )
        SELECT
            l.guild_id,
            l.user_id,
            m.display_name,
            m.name,
            m.mention,
            l.at AS last_activity_at,
            l.event_type AS last_event,
            l.event_text AS last_event_text,
            l.channel_id AS last_channel_id,
            l.channel_name AS last_channel_name,
            l.category_id AS last_category_id,
            l.category_name AS last_category_name,
            l.details AS last_details,
            COALESCE(c.total_events, 0) AS total_events
        FROM latest l
        LEFT JOIN counts c ON c.user_id = l.user_id
        LEFT JOIN members m ON m.guild_id = l.guild_id AND m.user_id = l.user_id
        WHERE l.rn = 1
    """
    with _db_lock, connect() as con:
        rows = con.execute(query, params).fetchall()
    return {int(row["user_id"]): _summary_from_row(row) for row in rows}


def get_recent_events(
    guild_id: int,
    user_id: int,
    *,
    limit: int = 15,
    event_types: Iterable[str] | None = None,
    category_id: int | None = None,
    channel_ids: Iterable[int] | None = None,
) -> list[ActivityEvent]:
    filter_sql, filter_params = _build_activity_filter(event_types=event_types, category_id=category_id, channel_ids=channel_ids)
    if filter_sql:
        filter_sql = " AND " + filter_sql
    query = f"""
        SELECT id, guild_id, user_id, event_type, event_text, at,
               channel_id, channel_name, category_id, category_name, details, message_id
        FROM activity_events e
        WHERE e.guild_id = ? AND e.user_id = ? {filter_sql}
        ORDER BY e.at DESC, e.id DESC
        LIMIT ?
    """
    params: list[Any] = [guild_id, user_id, *filter_params, max(1, min(int(limit), 50))]
    with _db_lock, connect() as con:
        rows = con.execute(query, params).fetchall()
    return [
        ActivityEvent(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            event_type=str(row["event_type"]),
            event_text=row["event_text"],
            at=str(row["at"]),
            channel_id=row["channel_id"],
            channel_name=row["channel_name"],
            category_id=row["category_id"],
            category_name=row["category_name"],
            details=row["details"],
            message_id=row["message_id"],
        )
        for row in rows
    ]


def get_counters(guild_id: int, user_id: int) -> dict[str, int]:
    with _db_lock, connect() as con:
        rows = con.execute(
            "SELECT event_type, count FROM activity_counters WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchall()
    return {str(row["event_type"]): int(row["count"] or 0) for row in rows}


def record_bureau_announcement(
    *,
    guild_id: int,
    author_id: int,
    author_display: str,
    source_channel_id: int | None,
    target_channel_id: int,
    message_id: int | None,
    content: str,
    created_at: str,
    title: str | None = None,
    note: str | None = None,
    image_url: str | None = None,
    publish_global: bool = False,
    global_channel_id: int | None = None,
    global_message_id: int | None = None,
) -> int:
    with _db_lock, connect() as con:
        cur = con.execute(
            """
            INSERT INTO bureau_announcements(
                guild_id, author_id, author_display, source_channel_id,
                target_channel_id, message_id, global_channel_id, global_message_id,
                title, content, note, image_url, publish_global, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                author_id,
                author_display,
                source_channel_id,
                target_channel_id,
                message_id,
                global_channel_id,
                global_message_id,
                title,
                content,
                note,
                image_url,
                1 if publish_global else 0,
                created_at,
            ),
        )
        con.commit()
        return int(cur.lastrowid)

__all__ = ['get_meta', 'set_meta', 'set_meta_value', '_record_bot_action', 'bot_record_action', '_bot_action_dict', 'bot_get_action', 'bot_list_actions', 'upsert_member', 'should_debounce', 'remember_activity', '_summary_from_row', 'get_summaries', '_build_activity_filter', 'get_filtered_summaries', 'get_recent_events', 'get_counters', 'record_bureau_announcement']
