from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from persistence import core as _core
from persistence.core import (
    ClientProfile,
    LawyerProfile,
    SGLCase,
    SGLReceipt,
    _client_profile_from_row,
    _db_lock,
    _lawyer_profile_from_row,
    connect,
    utc_now_iso,
)
from persistence.activity_repository import _record_bot_action, get_meta, set_meta, upsert_member

def _case_from_row(row: sqlite3.Row) -> SGLCase:
    return SGLCase(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        case_number=int(row["case_number"]),
        channel_id=row["channel_id"],
        client_id=int(row["client_id"]),
        client_display=row["client_display"],
        lead_lawyer_id=int(row["lead_lawyer_id"]),
        lead_lawyer_display=row["lead_lawyer_display"],
        secretary_id=row["secretary_id"],
        secretary_display=row["secretary_display"],
        status=str(row["status"]),
        request_type=row["request_type"],
        client_nick=row["client_nick"],
        static_id=row["static_id"],
        bank_account=row["bank_account"],
        phone=row["phone"],
        passport_url=row["passport_url"],
        situation_text=row["situation_text"],
        situation_author_id=row["situation_author_id"],
        situation_author_display=row["situation_author_display"],
        claim_link=row["claim_link"],
        claim_message_id=row["claim_message_id"],
        portfolio_message_id=row["portfolio_message_id"],
        portfolio_description=row["portfolio_description"],
        created_by_id=row["created_by_id"],
        created_by_display=row["created_by_display"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        closed_at=row["closed_at"],
        archived_at=row["archived_at"] if "archived_at" in row.keys() else None,
        archive_category_id=row["archive_category_id"] if "archive_category_id" in row.keys() else None,
    )


def _record_case_event(
    con: sqlite3.Connection,
    *,
    case_id: int | None,
    guild_id: int,
    case_number: int,
    actor_id: int | None,
    actor_display: str | None,
    action: str,
    details: str | None = None,
) -> None:
    con.execute(
        """
        INSERT INTO sgl_case_events(case_id, guild_id, case_number, actor_id, actor_display, action, details, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (case_id, guild_id, case_number, actor_id, actor_display, action, details, utc_now_iso()),
    )


def set_sgl_case_seed(guild_id: int, first_case_number: int) -> str:
    """Set the initial case number only when no case exists yet."""
    first_case_number = max(1, int(first_case_number))
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT MAX(case_number) AS max_case FROM sgl_cases WHERE guild_id = ?", (guild_id,)).fetchone()
        has_cases = row is not None and row["max_case"] is not None
        if has_cases:
            set_meta(con, f"sgl_case_seed_ignored:{guild_id}:{utc_now_iso()}", str(first_case_number))
            con.commit()
            return "kept_existing_cases"
        set_meta(con, f"sgl_case_next_number:{guild_id}", str(first_case_number))
        con.commit()
        return "seed_set"


def _next_case_number(con: sqlite3.Connection, guild_id: int) -> int:
    row = con.execute("SELECT MAX(case_number) AS max_case FROM sgl_cases WHERE guild_id = ?", (guild_id,)).fetchone()
    if row is not None and row["max_case"] is not None:
        return int(row["max_case"]) + 1
    seed_row = con.execute("SELECT value FROM meta WHERE key = ?", (f"sgl_case_next_number:{guild_id}",)).fetchone()
    if seed_row:
        try:
            return max(1, int(seed_row["value"]))
        except (TypeError, ValueError):
            return 1
    return 1


def reserve_sgl_case(
    *,
    guild_id: int,
    client_id: int,
    client_display: str | None,
    lead_lawyer_id: int,
    lead_lawyer_display: str | None,
    secretary_id: int | None,
    secretary_display: str | None,
    created_by_id: int | None,
    created_by_display: str | None,
) -> SGLCase:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        case_number = _next_case_number(con, guild_id)
        cur = con.execute(
            """
            INSERT INTO sgl_cases(
                guild_id, case_number, channel_id, client_id, client_display,
                lead_lawyer_id, lead_lawyer_display, secretary_id, secretary_display,
                status, created_by_id, created_by_display, created_at, updated_at
            )
            VALUES(?, ?, NULL, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?)
            """,
            (
                guild_id,
                case_number,
                client_id,
                client_display,
                lead_lawyer_id,
                lead_lawyer_display,
                secretary_id,
                secretary_display,
                created_by_id,
                created_by_display,
                now,
                now,
            ),
        )
        case_id = int(cur.lastrowid)
        _record_case_event(con, case_id=case_id, guild_id=guild_id, case_number=case_number, actor_id=created_by_id, actor_display=created_by_display, action="reserved")
        row = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (case_id,)).fetchone()
        con.commit()
    return _case_from_row(row)


def attach_sgl_case_channel(case_id: int, channel_id: int) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute("UPDATE sgl_cases SET channel_id = ?, status = 'created', updated_at = ? WHERE id = ?", (channel_id, now, case_id))
        _record_case_event(con, case_id=case_id, guild_id=int(row["guild_id"]), case_number=int(row["case_number"]), actor_id=None, actor_display=None, action="channel_created", details=str(channel_id))
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (case_id,)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def mark_sgl_case_error(case_id: int, details: str) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (case_id,)).fetchone()
        if row is not None:
            con.execute("UPDATE sgl_cases SET status = 'error', updated_at = ? WHERE id = ?", (now, case_id))
            _record_case_event(con, case_id=case_id, guild_id=int(row["guild_id"]), case_number=int(row["case_number"]), actor_id=None, actor_display=None, action="error", details=details)
        con.commit()


def get_sgl_case_by_channel(guild_id: int, channel_id: int) -> SGLCase | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
    return _case_from_row(row) if row else None


def get_sgl_case_by_number(guild_id: int, case_number: int) -> SGLCase | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND case_number = ?", (guild_id, case_number)).fetchone()
    return _case_from_row(row) if row else None


def update_sgl_case_params(
    *,
    guild_id: int,
    channel_id: int,
    request_type: str,
    client_nick: str,
    static_id: str,
    bank_account: str,
    phone: str,
    passport_url: str,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_cases
            SET request_type = ?, client_nick = ?, static_id = ?, bank_account = ?, phone = ?, passport_url = ?,
                status = 'awaiting_situation', updated_at = ?
            WHERE id = ?
            """,
            (request_type, client_nick, static_id, bank_account, phone, passport_url, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="params_saved")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_case",
            target_id=int(row["id"]),
            summary=f"Изменены параметры дела №{int(row['case_number'])}",
            payload={"table": "sgl_cases", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def update_sgl_case_situation(
    guild_id: int,
    channel_id: int,
    situation_text: str,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_cases
            SET situation_text = ?, situation_author_id = ?, situation_author_display = ?, status = 'awaiting_link', updated_at = ?
            WHERE id = ?
            """,
            (situation_text, actor_id, actor_display, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="situation_saved")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_case",
            target_id=int(row["id"]),
            summary=f"Изменено описание ситуации в деле №{int(row['case_number'])}",
            payload={"table": "sgl_cases", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def set_sgl_case_link(
    guild_id: int,
    channel_id: int,
    claim_link: str,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute("UPDATE sgl_cases SET claim_link = ?, status = 'awaiting_close', updated_at = ? WHERE id = ?", (claim_link, now, int(row["id"])))
        _record_case_event(con, case_id=int(row["id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="claim_link_saved", details=claim_link)
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_case",
            target_id=int(row["id"]),
            summary=f"Изменена ссылка на иск в деле №{int(row['case_number'])}",
            payload={"table": "sgl_cases", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def set_sgl_case_link_message(guild_id: int, channel_id: int, message_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("UPDATE sgl_cases SET claim_message_id = ?, updated_at = ? WHERE guild_id = ? AND channel_id = ?", (message_id, now, guild_id, channel_id))
        con.commit()


def close_sgl_case(
    *,
    guild_id: int,
    channel_id: int,
    actor_id: int | None,
    actor_display: str | None,
    publish_portfolio: bool,
    portfolio_description: str | None,
    portfolio_message_id: int | None,
) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_cases
            SET status = 'closed', portfolio_description = ?, portfolio_message_id = ?, closed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (portfolio_description, portfolio_message_id, now, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="closed", details=f"publish_portfolio={publish_portfolio}")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_case",
            target_id=int(row["id"]),
            summary=f"Закрыто дело №{int(row['case_number'])}",
            payload={"table": "sgl_cases", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def list_sgl_cases_with_channels(guild_id: int) -> list[SGLCase]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_cases
            WHERE guild_id = ? AND channel_id IS NOT NULL
            ORDER BY case_number ASC
            """,
            (guild_id,),
        ).fetchall()
    return [_case_from_row(row) for row in rows]



def list_sgl_cases_for_client(guild_id: int, client_id: int, limit: int = 25) -> list[SGLCase]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_cases
            WHERE guild_id = ? AND client_id = ?
            ORDER BY case_number DESC
            LIMIT ?
            """,
            (guild_id, client_id, int(limit)),
        ).fetchall()
    return [_case_from_row(row) for row in rows]


def list_sgl_cases_for_participant(guild_id: int, user_id: int, limit: int = 25) -> list[SGLCase]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_cases
            WHERE guild_id = ?
              AND (client_id = ? OR lead_lawyer_id = ? OR secretary_id = ?)
            ORDER BY case_number DESC
            LIMIT ?
            """,
            (guild_id, user_id, user_id, user_id, int(limit)),
        ).fetchall()
    return [_case_from_row(row) for row in rows]


def get_sgl_case_events(case_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_case_events
            WHERE case_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (case_id, int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


_SGL_WEB_MANAGEABLE_STATUSES = frozenset(
    {
        "created",
        "awaiting_situation",
        "awaiting_link",
        "awaiting_close",
        "error",
    }
)
_SGL_CASE_MANAGEMENT_FIELDS = frozenset(
    {
        "client_id",
        "client_display",
        "lead_lawyer_id",
        "lead_lawyer_display",
        "secretary_id",
        "secretary_display",
        "request_type",
        "client_nick",
        "static_id",
        "bank_account",
        "phone",
        "passport_url",
        "situation_text",
        "claim_link",
        "status",
    }
)


def _case_message_attachments(value: Any) -> list[dict[str, Any]]:
    """Return a bounded, JSON-safe attachment projection for the web client."""

    if not isinstance(value, list):
        return []
    prepared: list[dict[str, Any]] = []
    for raw in value[:10]:
        if not isinstance(raw, dict):
            continue
        try:
            size = int(raw.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        prepared.append(
            {
                "id": int(raw["id"]) if str(raw.get("id") or "").isdigit() else None,
                "filename": str(raw.get("filename") or "attachment")[:255],
                "url": str(raw.get("url") or "")[:2000],
                "content_type": str(raw.get("content_type") or "")[:160] or None,
                "size": max(0, min(size, 100_000_000)),
            }
        )
    return prepared


def _case_message_snowflake(value: Any) -> str | None:
    """Return a Discord snowflake as a JSON-safe string.

    JavaScript cannot safely represent Discord's 64-bit identifiers as a
    number.  The database intentionally keeps them as INTEGER values for
    efficient lookups, while the browser-facing projection always uses a
    decimal string.
    """

    try:
        snowflake = int(value)
    except (TypeError, ValueError):
        return None
    return str(snowflake) if snowflake > 0 else None


def _case_message_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    try:
        parsed_attachments = json.loads(str(payload.pop("attachments_json") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed_attachments = []
    attachments = _case_message_attachments(parsed_attachments)
    for attachment in attachments:
        attachment["id"] = _case_message_snowflake(attachment.get("id"))
    payload["attachments"] = attachments
    payload["discord_message_id"] = _case_message_snowflake(
        payload.get("discord_message_id")
    )
    payload["reply_to_discord_message_id"] = _case_message_snowflake(
        payload.get("reply_to_discord_message_id")
    )
    payload["author_is_bot"] = bool(payload.get("author_is_bot"))
    return payload


def record_sgl_case_message(
    *,
    case: SGLCase,
    origin: str,
    discord_message_id: int | None,
    author_id: int | None,
    author_display: str | None,
    author_avatar_url: str | None,
    author_is_bot: bool,
    content: str,
    attachments: list[dict[str, Any]] | None = None,
    reply_to_discord_message_id: int | None = None,
    created_at: str | None = None,
    edited_at: str | None = None,
) -> dict[str, Any]:
    """Upsert one live Discord/web message into the canonical SGL transcript.

    Discord gateway events are at-least-once, and a web-originated message can
    later be observed by that gateway.  The unique Discord message id makes
    either delivery order safe while retaining ``web`` as the useful origin.
    """

    clean_origin = str(origin or "").strip().lower()
    if clean_origin not in {"discord", "web", "system"}:
        raise ValueError("sgl_case_message_origin_invalid")
    clean_content = str(content or "").strip()
    if len(clean_content) > 4_000:
        raise ValueError("sgl_case_message_too_long")
    clean_attachments = _case_message_attachments(attachments or [])
    message_id = int(discord_message_id) if discord_message_id else None
    now = str(created_at or utc_now_iso())
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cursor = con.execute(
            """
            INSERT INTO sgl_case_messages(
                case_id, guild_id, case_number, origin, discord_message_id,
                author_id, author_display, author_avatar_url, author_is_bot,
                content, attachments_json, reply_to_discord_message_id,
                created_at, edited_at, deleted_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(guild_id, discord_message_id) DO UPDATE SET
                case_id = excluded.case_id,
                case_number = excluded.case_number,
                origin = CASE
                    WHEN sgl_case_messages.origin = 'web' THEN 'web'
                    ELSE excluded.origin
                END,
                author_id = excluded.author_id,
                author_display = excluded.author_display,
                author_avatar_url = excluded.author_avatar_url,
                author_is_bot = excluded.author_is_bot,
                content = excluded.content,
                attachments_json = CASE
                    WHEN sgl_case_messages.origin = 'web'
                         AND excluded.origin = 'discord'
                         AND excluded.attachments_json = '[]'
                    THEN sgl_case_messages.attachments_json
                    ELSE excluded.attachments_json
                END,
                reply_to_discord_message_id = excluded.reply_to_discord_message_id,
                edited_at = COALESCE(excluded.edited_at, sgl_case_messages.edited_at),
                deleted_at = NULL
            """,
            (
                int(case.id),
                int(case.guild_id),
                int(case.case_number),
                clean_origin,
                message_id,
                int(author_id) if author_id else None,
                str(author_display or "")[:160],
                str(author_avatar_url or "")[:2000] or None,
                int(bool(author_is_bot)),
                clean_content,
                json.dumps(clean_attachments, ensure_ascii=False, separators=(",", ":")),
                int(reply_to_discord_message_id) if reply_to_discord_message_id else None,
                now,
                str(edited_at) if edited_at else None,
            ),
        )
        if message_id is not None:
            row = con.execute(
                "SELECT * FROM sgl_case_messages WHERE guild_id = ? AND discord_message_id = ?",
                (int(case.guild_id), message_id),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM sgl_case_messages WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
        con.commit()
    if row is None:  # pragma: no cover - SQLite insert always returns a row here
        raise RuntimeError("sgl_case_message_write_failed")
    return _case_message_payload(row)


def update_sgl_case_message_from_discord(
    *,
    guild_id: int,
    discord_message_id: int,
    content: str,
    attachments: list[dict[str, Any]] | None = None,
    edited_at: str | None = None,
) -> dict[str, Any] | None:
    clean_content = str(content or "").strip()
    if len(clean_content) > 4_000:
        clean_content = clean_content[:4_000]
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM sgl_case_messages WHERE guild_id = ? AND discord_message_id = ?",
            (int(guild_id), int(discord_message_id)),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_case_messages
            SET content = ?, attachments_json = ?, edited_at = ?, deleted_at = NULL
            WHERE id = ?
            """,
            (
                clean_content,
                json.dumps(_case_message_attachments(attachments or []), ensure_ascii=False, separators=(",", ":")),
                str(edited_at or utc_now_iso()),
                int(row["id"]),
            ),
        )
        updated = con.execute(
            "SELECT * FROM sgl_case_messages WHERE id = ?", (int(row["id"]),)
        ).fetchone()
        con.commit()
    return _case_message_payload(updated) if updated is not None else None


def mark_sgl_case_message_deleted(
    *, guild_id: int, discord_message_id: int, deleted_at: str | None = None
) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM sgl_case_messages WHERE guild_id = ? AND discord_message_id = ?",
            (int(guild_id), int(discord_message_id)),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            "UPDATE sgl_case_messages SET deleted_at = ? WHERE id = ?",
            (str(deleted_at or utc_now_iso()), int(row["id"])),
        )
        updated = con.execute(
            "SELECT * FROM sgl_case_messages WHERE id = ?", (int(row["id"]),)
        ).fetchone()
        con.commit()
    return _case_message_payload(updated) if updated is not None else None


def get_sgl_case_message_by_discord_id(
    *, guild_id: int, discord_message_id: int
) -> dict[str, Any] | None:
    """Return one transcript item by its Discord message id.

    This deliberately looks up by guild first instead of case id so callers
    can distinguish an unknown reply target from an attempt to reply across
    cases in the same guild.
    """

    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT * FROM sgl_case_messages
            WHERE guild_id = ? AND discord_message_id = ?
            """,
            (int(guild_id), int(discord_message_id)),
        ).fetchone()
    return _case_message_payload(row) if row is not None else None


def list_sgl_case_messages(
    case_id: int,
    *,
    after_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    selected_limit = max(1, min(int(limit), 200))
    with _db_lock, connect() as con:
        if after_id is not None and int(after_id) > 0:
            rows = con.execute(
                """
                SELECT * FROM sgl_case_messages
                WHERE case_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (int(case_id), int(after_id), selected_limit),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT * FROM (
                    SELECT * FROM sgl_case_messages
                    WHERE case_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id ASC
                """,
                (int(case_id), selected_limit),
            ).fetchall()
    return [_case_message_payload(row) for row in rows]


_SGL_FORUM_EDITABLE_STATUSES = frozenset({"draft", "failed"})


def _forum_publication_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    payload["attempts"] = int(payload.get("attempts") or 0)
    return payload


def list_sgl_case_forum_publications(
    case_id: int, *, limit: int = 25
) -> list[dict[str, Any]]:
    selected_limit = max(1, min(int(limit), 100))
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_case_forum_publications
            WHERE case_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(case_id), selected_limit),
        ).fetchall()
    return [_forum_publication_payload(row) for row in rows]


def get_sgl_case_forum_publication(
    *, guild_id: int, case_number: int, publication_id: int
) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT * FROM sgl_case_forum_publications
            WHERE guild_id = ? AND case_number = ? AND id = ?
            """,
            (int(guild_id), int(case_number), int(publication_id)),
        ).fetchone()
    return _forum_publication_payload(row) if row is not None else None


def _clean_forum_publication_values(
    *, target_url: Any, title: Any, body: Any
) -> tuple[str, str, str]:
    target = str(target_url or "").strip()
    headline = str(title or "").strip()
    copy = str(body or "").strip()
    if not target.startswith("https://") or len(target) > 2_000:
        raise ValueError("sgl_forum_target_invalid")
    if not headline or len(headline) > 180:
        raise ValueError("sgl_forum_title_invalid")
    if not copy or len(copy) > 20_000:
        raise ValueError("sgl_forum_body_invalid")
    return target, headline, copy


def create_sgl_case_forum_publication(
    *,
    case: SGLCase,
    target_url: Any,
    title: Any,
    body: Any,
    created_by_id: int | None,
    created_by_display: str | None,
) -> dict[str, Any]:
    """Save an externally publishable forum draft and audit its creation."""

    target, headline, copy = _clean_forum_publication_values(
        target_url=target_url, title=title, body=body
    )
    if case.archived_at is not None or case.status in {"closed", "reserved", "error"}:
        raise ValueError("sgl_forum_case_readonly")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cursor = con.execute(
            """
            INSERT INTO sgl_case_forum_publications(
                case_id, guild_id, case_number, target_url, title, body, status,
                created_by_id, created_by_display, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?)
            """,
            (
                int(case.id), int(case.guild_id), int(case.case_number), target,
                headline, copy, created_by_id,
                str(created_by_display or "")[:160], now, now,
            ),
        )
        publication_id = int(cursor.lastrowid)
        _record_case_event(
            con,
            case_id=int(case.id), guild_id=int(case.guild_id),
            case_number=int(case.case_number), actor_id=created_by_id,
            actor_display=created_by_display, action="forum_draft_created",
            details=f"publication_id={publication_id}",
        )
        row = con.execute(
            "SELECT * FROM sgl_case_forum_publications WHERE id = ?", (publication_id,)
        ).fetchone()
        con.commit()
    if row is None:  # pragma: no cover - SQLite insert always yields a row
        raise RuntimeError("sgl_forum_publication_write_failed")
    return _forum_publication_payload(row)


def update_sgl_case_forum_publication(
    *,
    guild_id: int,
    case_number: int,
    publication_id: int,
    target_url: Any,
    title: Any,
    body: Any,
    actor_id: int | None,
    actor_display: str | None,
    expected_updated_at: str | None = None,
) -> dict[str, Any] | None:
    """Edit a draft. Published content is deliberately immutable here."""

    target, headline, copy = _clean_forum_publication_values(
        target_url=target_url, title=title, body=body
    )
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT * FROM sgl_case_forum_publications
            WHERE guild_id = ? AND case_number = ? AND id = ?
            """,
            (int(guild_id), int(case_number), int(publication_id)),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        if expected_updated_at is not None and str(row["updated_at"]) != str(expected_updated_at):
            con.commit()
            raise ValueError("sgl_forum_revision_conflict")
        if str(row["status"]) not in _SGL_FORUM_EDITABLE_STATUSES:
            con.commit()
            raise ValueError("sgl_forum_publication_readonly")
        con.execute(
            """
            UPDATE sgl_case_forum_publications
            SET target_url = ?, title = ?, body = ?, status = 'draft',
                last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (target, headline, copy, now, int(row["id"])),
        )
        _record_case_event(
            con, case_id=int(row["case_id"]), guild_id=int(guild_id),
            case_number=int(case_number), actor_id=actor_id,
            actor_display=actor_display, action="forum_draft_updated",
            details=f"publication_id={int(row['id'])}",
        )
        updated = con.execute(
            "SELECT * FROM sgl_case_forum_publications WHERE id = ?", (int(row["id"]),)
        ).fetchone()
        con.commit()
    return _forum_publication_payload(updated) if updated is not None else None


def claim_sgl_case_forum_publication(
    *,
    guild_id: int,
    case_number: int,
    publication_id: int,
    actor_id: int | None,
    actor_display: str | None,
    expected_updated_at: str | None = None,
) -> dict[str, Any] | None:
    """Atomically lease a reviewed draft for a single forum submission attempt."""

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT p.*, c.status AS case_status, c.archived_at AS case_archived_at
            FROM sgl_case_forum_publications AS p
            JOIN sgl_cases AS c ON c.id = p.case_id
            WHERE p.guild_id = ? AND p.case_number = ? AND p.id = ?
            """,
            (int(guild_id), int(case_number), int(publication_id)),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        if expected_updated_at is not None and str(row["updated_at"]) != str(expected_updated_at):
            con.commit()
            raise ValueError("sgl_forum_revision_conflict")
        if row["case_archived_at"] is not None or str(row["case_status"]) in {"closed", "reserved", "error"}:
            con.commit()
            raise ValueError("sgl_forum_case_readonly")
        if str(row["status"]) not in _SGL_FORUM_EDITABLE_STATUSES:
            con.commit()
            raise ValueError("sgl_forum_publication_busy")
        con.execute(
            """
            UPDATE sgl_case_forum_publications
            SET status = 'publishing', reviewed_by_id = ?, reviewed_by_display = ?,
                reviewed_at = ?, attempts = attempts + 1, last_error = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (actor_id, str(actor_display or "")[:160], now, now, int(row["id"])),
        )
        _record_case_event(
            con, case_id=int(row["case_id"]), guild_id=int(guild_id),
            case_number=int(case_number), actor_id=actor_id,
            actor_display=actor_display, action="forum_publication_started",
            details=f"publication_id={int(row['id'])}",
        )
        updated = con.execute(
            "SELECT * FROM sgl_case_forum_publications WHERE id = ?", (int(row["id"]),)
        ).fetchone()
        con.commit()
    return _forum_publication_payload(updated) if updated is not None else None


def mark_sgl_case_forum_publication_published(
    *, publication_id: int, forum_url: Any
) -> dict[str, Any] | None:
    """Seal a successful post and attach its canonical URL to the SGL case."""

    url = str(forum_url or "").strip()
    if not url.startswith("https://") or len(url) > 2_000:
        raise ValueError("sgl_forum_result_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM sgl_case_forum_publications WHERE id = ?", (int(publication_id),)
        ).fetchone()
        if row is None:
            con.commit()
            return None
        if str(row["status"]) != "publishing":
            con.commit()
            raise ValueError("sgl_forum_publication_not_claimed")
        con.execute(
            """
            UPDATE sgl_case_forum_publications
            SET status = 'published', forum_url = ?, published_at = ?,
                last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (url, now, now, int(row["id"])),
        )
        con.execute(
            """
            UPDATE sgl_cases
            SET claim_link = ?,
                status = CASE WHEN status = 'awaiting_link' THEN 'awaiting_close' ELSE status END,
                updated_at = ?
            WHERE id = ?
            """,
            (url, now, int(row["case_id"])),
        )
        _record_case_event(
            con, case_id=int(row["case_id"]), guild_id=int(row["guild_id"]),
            case_number=int(row["case_number"]), actor_id=row["reviewed_by_id"],
            actor_display=row["reviewed_by_display"], action="forum_published",
            details=url,
        )
        updated = con.execute(
            "SELECT * FROM sgl_case_forum_publications WHERE id = ?", (int(row["id"]),)
        ).fetchone()
        con.commit()
    return _forum_publication_payload(updated) if updated is not None else None


def mark_sgl_case_forum_publication_failed(
    *, publication_id: int, error: Any
) -> dict[str, Any] | None:
    """Release a failed submission for correction and a later explicit retry."""

    detail = str(error or "forum_publication_failed").strip()[:1_500]
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM sgl_case_forum_publications WHERE id = ?", (int(publication_id),)
        ).fetchone()
        if row is None:
            con.commit()
            return None
        if str(row["status"]) != "publishing":
            con.commit()
            return _forum_publication_payload(row)
        con.execute(
            """
            UPDATE sgl_case_forum_publications
            SET status = 'failed', last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (detail, now, int(row["id"])),
        )
        _record_case_event(
            con, case_id=int(row["case_id"]), guild_id=int(row["guild_id"]),
            case_number=int(row["case_number"]), actor_id=row["reviewed_by_id"],
            actor_display=row["reviewed_by_display"], action="forum_publication_failed",
            details=f"publication_id={int(row['id'])};{detail[:500]}",
        )
        updated = con.execute(
            "SELECT * FROM sgl_case_forum_publications WHERE id = ?", (int(row["id"]),)
        ).fetchone()
        con.commit()
    return _forum_publication_payload(updated) if updated is not None else None


def _case_ai_note_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    try:
        citations = json.loads(str(payload.pop("citations_json") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        citations = []
    payload["citations"] = citations if isinstance(citations, list) else []
    return payload


def list_sgl_case_ai_notes(case_id: int, *, limit: int = 30) -> list[dict[str, Any]]:
    """Return the compact, durable Atlas journal attached to one case."""

    selected_limit = max(1, min(int(limit), 100))
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_case_ai_notes
            WHERE case_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(case_id), selected_limit),
        ).fetchall()
    return [_case_ai_note_payload(row) for row in rows]


def record_sgl_case_ai_note(
    *,
    case: SGLCase,
    kind: Any,
    question: Any,
    answer: Any,
    citations: Any,
    agent_id: Any,
    response_mode: Any,
    created_by_id: int | None,
    created_by_display: str | None,
) -> dict[str, Any]:
    """Persist an Atlas conclusion and create an auditable case event."""

    clean_kind = str(kind or "analysis").strip().lower()[:40] or "analysis"
    clean_question = str(question or "").strip()[:4_000]
    clean_answer = str(answer or "").strip()[:30_000]
    if not clean_question:
        raise ValueError("sgl_case_ai_question_empty")
    if not clean_answer:
        raise ValueError("sgl_case_ai_answer_empty")
    if not isinstance(citations, list):
        citations = []
    try:
        citations_json = json.dumps(citations[:40], ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        citations_json = "[]"
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cursor = con.execute(
            """
            INSERT INTO sgl_case_ai_notes(
                case_id, guild_id, case_number, kind, question, answer,
                citations_json, agent_id, response_mode, created_by_id,
                created_by_display, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(case.id), int(case.guild_id), int(case.case_number), clean_kind,
                clean_question, clean_answer, citations_json,
                str(agent_id or "atlas-claims")[:80],
                str(response_mode or "balanced")[:40],
                int(created_by_id) if created_by_id else None,
                str(created_by_display or "")[:160], now,
            ),
        )
        note_id = int(cursor.lastrowid)
        _record_case_event(
            con, case_id=int(case.id), guild_id=int(case.guild_id),
            case_number=int(case.case_number), actor_id=created_by_id,
            actor_display=created_by_display, action="atlas_analysis_saved",
            details=f"note_id={note_id};kind={clean_kind}",
        )
        row = con.execute("SELECT * FROM sgl_case_ai_notes WHERE id = ?", (note_id,)).fetchone()
        con.commit()
    if row is None:  # pragma: no cover - guarded by SQLite insert semantics
        raise RuntimeError("sgl_case_ai_note_write_failed")
    return _case_ai_note_payload(row)


_SGL_CASE_DECISION_KINDS = frozenset({"decision", "handoff", "risk", "note"})
_SGL_CASE_DECISION_STATUSES = frozenset({"open", "read", "acknowledged", "superseded"})


def _case_decision_payload(row: sqlite3.Row) -> dict[str, Any]:
    """Return a staff-only decision / handoff record as JSON-ready data."""

    return dict(row)


def list_sgl_case_decisions(
    case_id: int,
    *,
    include_resolved: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List the durable private decision journal for one SGL case.

    The caller is responsible for enforcing staff access.  Keeping this
    repository primitive case-scoped prevents a bureau-wide index from
    accidentally becoming a second surface for sensitive case reasoning.
    """

    selected_limit = max(1, min(int(limit), 200))
    where = "case_id = ?" if include_resolved else "case_id = ? AND status IN ('open', 'read')"
    with _db_lock, connect() as con:
        rows = con.execute(
            f"""
            SELECT * FROM sgl_case_decisions
            WHERE {where}
            ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'read' THEN 1
                              WHEN 'acknowledged' THEN 2 ELSE 3 END,
                     id DESC
            LIMIT ?
            """,
            (int(case_id), selected_limit),
        ).fetchall()
    return [_case_decision_payload(row) for row in rows]


def create_sgl_case_decision(
    *,
    case: SGLCase,
    kind: Any = "decision",
    title: Any,
    body: Any,
    target_user_id: Any = None,
    target_display: Any = None,
    created_by_id: int | None,
    created_by_display: str | None,
) -> dict[str, Any]:
    """Append an immutable internal decision or handoff to a case journal."""

    clean_kind = str(kind or "decision").strip().lower()
    if clean_kind not in _SGL_CASE_DECISION_KINDS:
        raise ValueError("sgl_case_decision_kind_invalid")
    clean_title = str(title or "").strip()[:240]
    if not clean_title:
        raise ValueError("sgl_case_decision_title_empty")
    clean_body = str(body or "").strip()[:8_000]
    if not clean_body:
        raise ValueError("sgl_case_decision_body_empty")
    try:
        clean_target_id = (
            int(target_user_id)
            if target_user_id not in (None, "", 0, "0")
            else None
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("sgl_case_decision_target_invalid") from exc
    if clean_target_id is not None and clean_target_id <= 0:
        raise ValueError("sgl_case_decision_target_invalid")
    clean_target_display = str(target_display or "").strip()[:160] or None
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cursor = con.execute(
            """
            INSERT INTO sgl_case_decisions(
                case_id, guild_id, case_number, kind, title, body, status,
                target_user_id, target_display, created_by_id,
                created_by_display, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
            """,
            (
                int(case.id), int(case.guild_id), int(case.case_number), clean_kind,
                clean_title, clean_body, clean_target_id, clean_target_display,
                int(created_by_id) if created_by_id else None,
                str(created_by_display or "").strip()[:160] or None, now, now,
            ),
        )
        decision_id = int(cursor.lastrowid)
        # Do not write the title or body to the shared event log: that log is
        # also used for a participant-facing timeline.  The ID/kind preserves
        # the audit trail without leaking staff-only reasoning.
        _record_case_event(
            con, case_id=int(case.id), guild_id=int(case.guild_id),
            case_number=int(case.case_number), actor_id=created_by_id,
            actor_display=created_by_display, action="internal_decision_logged",
            details=json.dumps(
                {"decision_id": decision_id, "kind": clean_kind, "target_user_id": clean_target_id},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        row = con.execute("SELECT * FROM sgl_case_decisions WHERE id = ?", (decision_id,)).fetchone()
        con.commit()
    if row is None:  # pragma: no cover - SQLite insert guarantees the row
        raise RuntimeError("sgl_case_decision_write_failed")
    return _case_decision_payload(row)


def update_sgl_case_decision_status(
    *,
    decision_id: int,
    case_id: int,
    guild_id: int,
    status: Any,
    actor_id: int | None,
    actor_display: str | None,
) -> dict[str, Any] | None:
    """Advance a private journal item without allowing its historical text to change."""

    clean_status = str(status or "").strip().lower()
    if clean_status not in _SGL_CASE_DECISION_STATUSES:
        raise ValueError("sgl_case_decision_status_invalid")
    now = utc_now_iso()
    clean_actor_display = str(actor_display or "").strip()[:160] or None
    clean_actor_id = int(actor_id) if actor_id else None
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT * FROM sgl_case_decisions
            WHERE id = ? AND case_id = ? AND guild_id = ?
            """,
            (int(decision_id), int(case_id), int(guild_id)),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        previous_status = str(row["status"])
        if previous_status != clean_status:
            assignments = [
                "status = ?",
                "updated_by_id = ?",
                "updated_by_display = ?",
                "updated_at = ?",
            ]
            params: list[Any] = [clean_status, clean_actor_id, clean_actor_display, now]
            if clean_status in {"read", "acknowledged"}:
                assignments.extend([
                    "read_at = COALESCE(read_at, ?)",
                    "read_by_id = COALESCE(read_by_id, ?)",
                    "read_by_display = COALESCE(read_by_display, ?)",
                ])
                params.extend([now, clean_actor_id, clean_actor_display])
            if clean_status == "acknowledged":
                assignments.extend([
                    "acknowledged_at = COALESCE(acknowledged_at, ?)",
                    "acknowledged_by_id = COALESCE(acknowledged_by_id, ?)",
                    "acknowledged_by_display = COALESCE(acknowledged_by_display, ?)",
                ])
                params.extend([now, clean_actor_id, clean_actor_display])
            params.append(int(decision_id))
            con.execute(
                f"UPDATE sgl_case_decisions SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            _record_case_event(
                con, case_id=int(row["case_id"]), guild_id=int(guild_id),
                case_number=int(row["case_number"]), actor_id=clean_actor_id,
                actor_display=clean_actor_display, action="internal_decision_status_updated",
                details=json.dumps(
                    {
                        "decision_id": int(decision_id),
                        "from": previous_status,
                        "to": clean_status,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        updated = con.execute("SELECT * FROM sgl_case_decisions WHERE id = ?", (int(decision_id),)).fetchone()
        con.commit()
    return _case_decision_payload(updated) if updated is not None else None


def list_sgl_forum_publications_for_guild(
    guild_id: int, *, limit: int = 200
) -> list[dict[str, Any]]:
    """List publishing work across the bureau without requiring N case reads."""

    selected_limit = max(1, min(int(limit), 500))
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT p.*, c.channel_id
            FROM sgl_case_forum_publications AS p
            JOIN sgl_cases AS c ON c.id = p.case_id
            WHERE p.guild_id = ?
            ORDER BY p.updated_at DESC, p.id DESC
            LIMIT ?
            """,
            (int(guild_id), selected_limit),
        ).fetchall()
    return [_forum_publication_payload(row) for row in rows]


def _forum_observation_payload(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def list_sgl_forum_observations(guild_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
    selected_limit = max(1, min(int(limit), 500))
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_case_forum_observations
            WHERE guild_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (int(guild_id), selected_limit),
        ).fetchall()
    return [_forum_observation_payload(row) for row in rows]


def list_sgl_case_forum_observations(case_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
    selected_limit = max(1, min(int(limit), 100))
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_case_forum_observations
            WHERE case_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (int(case_id), selected_limit),
        ).fetchall()
    return [_forum_observation_payload(row) for row in rows]


def observe_sgl_case_forum_publication(
    *,
    publication: dict[str, Any],
    thread_title: Any = None,
    thread_excerpt: Any = None,
    content_fingerprint: Any = None,
    error: Any = None,
) -> dict[str, Any]:
    """Upsert one forum observation and report whether Discord should alert.

    The initial snapshot establishes a baseline.  Only a later changed
    fingerprint requests a notification, which prevents a noisy first scan.
    """

    publication_id = int(publication["id"])
    case_id = int(publication["case_id"])
    guild_id = int(publication["guild_id"])
    case_number = int(publication["case_number"])
    forum_url = str(publication.get("forum_url") or "").strip()[:2_000]
    if not forum_url:
        raise ValueError("sgl_forum_observation_url_missing")
    fingerprint = str(content_fingerprint or "").strip()[:128] or None
    title = str(thread_title or "").strip()[:500] or None
    excerpt = str(thread_excerpt or "").strip()[:4_000] or None
    failure = str(error or "").strip()[:1_500] or None
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        before = con.execute(
            "SELECT * FROM sgl_case_forum_observations WHERE publication_id = ?",
            (publication_id,),
        ).fetchone()
        change_type = "initial"
        should_notify = False
        snapshot_kind: str | None = None
        if before is None:
            snapshot_kind = "error" if failure else "initial"
            con.execute(
                """
                INSERT INTO sgl_case_forum_observations(
                    publication_id, case_id, guild_id, case_number, forum_url,
                    thread_title, thread_excerpt, content_fingerprint, status,
                    last_checked_at, last_changed_at, last_error, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_id, case_id, guild_id, case_number, forum_url,
                    title, excerpt, fingerprint, "error" if failure else "tracked",
                    now, now if fingerprint else None, failure, now, now,
                ),
            )
        elif failure:
            change_type = "error"
            should_notify = str(before["status"] or "") != "error"
            snapshot_kind = "error" if should_notify else None
            con.execute(
                """
                UPDATE sgl_case_forum_observations
                SET status = 'error', last_checked_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, failure, now, int(before["id"])),
            )
        else:
            previous = str(before["content_fingerprint"] or "")
            changed = bool(previous and fingerprint and previous != fingerprint)
            baseline = bool(not previous and fingerprint)
            change_type = "changed" if changed else ("baseline" if baseline else "unchanged")
            should_notify = changed
            snapshot_kind = "changed" if changed else ("baseline" if baseline else None)
            con.execute(
                """
                UPDATE sgl_case_forum_observations
                SET forum_url = ?, thread_title = ?, thread_excerpt = ?,
                    content_fingerprint = COALESCE(?, content_fingerprint),
                    status = CASE WHEN ? THEN 'changed' ELSE 'tracked' END, last_checked_at = ?,
                    last_changed_at = CASE WHEN ? THEN ? ELSE last_changed_at END,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (forum_url, title, excerpt, fingerprint, int(changed), now, int(changed), now, now, int(before["id"])),
            )
            if changed:
                _record_case_event(
                    con, case_id=case_id, guild_id=guild_id, case_number=case_number,
                    actor_id=None, actor_display="SGL Forum Watch",
                    action="forum_topic_changed", details=forum_url,
                )
        if snapshot_kind is not None:
            con.execute(
                """
                INSERT INTO sgl_case_forum_snapshots(
                    publication_id, case_id, guild_id, case_number, snapshot_kind,
                    thread_title, thread_excerpt, content_fingerprint, captured_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_id, case_id, guild_id, case_number, snapshot_kind,
                    title, excerpt if not failure else failure, fingerprint, now,
                ),
            )
        row = con.execute(
            "SELECT * FROM sgl_case_forum_observations WHERE publication_id = ?",
            (publication_id,),
        ).fetchone()
        con.commit()
    if row is None:  # pragma: no cover
        raise RuntimeError("sgl_forum_observation_write_failed")
    payload = _forum_observation_payload(row)
    payload["change_type"] = change_type
    payload["should_notify"] = should_notify
    return payload


def list_sgl_case_forum_snapshots(case_id: int, *, limit: int = 30) -> list[dict[str, Any]]:
    selected_limit = max(1, min(int(limit), 100))
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_case_forum_snapshots
            WHERE case_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(case_id), selected_limit),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_sgl_forum_observation_notified(observation_id: int) -> dict[str, Any] | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            UPDATE sgl_case_forum_observations
            SET last_notified_at = ?,
                status = CASE WHEN last_error IS NOT NULL THEN 'error' ELSE 'tracked' END,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, int(observation_id)),
        )
        row = con.execute(
            "SELECT * FROM sgl_case_forum_observations WHERE id = ?",
            (int(observation_id),),
        ).fetchone()
        con.commit()
    return _forum_observation_payload(row) if row is not None else None


_SGL_TASK_PRIORITIES = frozenset({"critical", "high", "normal", "low"})
_SGL_TASK_STATUSES = frozenset({"open", "done", "cancelled"})
_SGL_NOTIFICATION_SEVERITIES = frozenset({"info", "success", "warning", "critical"})
_SGL_NOTIFICATION_DELIVERY = frozenset({"not_applicable", "pending", "sent", "failed"})


def _case_task_payload(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _case_notification_payload(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def list_sgl_case_tasks(
    case_id: int,
    *,
    include_closed: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return durable human/Atlas work items for one case."""

    selected_limit = max(1, min(int(limit), 300))
    where = "case_id = ?" if include_closed else "case_id = ? AND status = 'open'"
    with _db_lock, connect() as con:
        rows = con.execute(
            f"""
            SELECT * FROM sgl_case_tasks
            WHERE {where}
            ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'done' THEN 1 ELSE 2 END,
                     CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                     CASE WHEN due_at IS NULL OR due_at = '' THEN 1 ELSE 0 END,
                     due_at ASC, id DESC
            LIMIT ?
            """,
            (int(case_id), selected_limit),
        ).fetchall()
    return [_case_task_payload(row) for row in rows]


def list_sgl_case_tasks_for_guild(
    guild_id: int,
    *,
    status: str | None = None,
    owner_id: int | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return the bureau-wide task queue with its current case context.

    The task table intentionally denormalizes ``case_number`` for auditing,
    but the operations view also needs the current case title and lifecycle
    state.  Keep that projection in the repository so every management
    surface applies the same guild boundary and ordering.
    """

    clean_status = str(status or "").strip().lower() or None
    if clean_status is not None and clean_status not in _SGL_TASK_STATUSES:
        raise ValueError("sgl_task_status_invalid")
    clean_owner_id = int(owner_id) if owner_id is not None else None
    if clean_owner_id is not None and clean_owner_id <= 0:
        raise ValueError("sgl_task_owner_invalid")
    selected_limit = max(1, min(int(limit), 500))
    clauses = ["t.guild_id = ?"]
    params: list[Any] = [int(guild_id)]
    if clean_status is not None:
        clauses.append("t.status = ?")
        params.append(clean_status)
    if clean_owner_id is not None:
        clauses.append("t.owner_id = ?")
        params.append(clean_owner_id)
    where = " AND ".join(clauses)
    with _db_lock, connect() as con:
        rows = con.execute(
            f"""
            SELECT t.*,
                   c.status AS case_status,
                   c.request_type AS case_request_type,
                   COALESCE(NULLIF(c.client_display, ''), NULLIF(c.client_nick, ''),
                            'Клиент не указан') AS case_title
            FROM sgl_case_tasks AS t
            JOIN sgl_cases AS c ON c.id = t.case_id AND c.guild_id = t.guild_id
            WHERE {where}
            ORDER BY CASE t.status WHEN 'open' THEN 0 WHEN 'done' THEN 1 ELSE 2 END,
                     CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                                     WHEN 'normal' THEN 2 ELSE 3 END,
                     CASE WHEN t.due_at IS NULL OR t.due_at = '' THEN 1 ELSE 0 END,
                     t.due_at ASC, t.id DESC
            LIMIT ?
            """,
            [*params, selected_limit],
        ).fetchall()
    return [_case_task_payload(row) for row in rows]


def create_sgl_case_task(
    *,
    case: SGLCase,
    title: Any,
    description: Any = None,
    priority: Any = "normal",
    owner_id: Any = None,
    owner_display: Any = None,
    due_at: Any = None,
    source: Any = "manual",
    created_by_id: int | None,
    created_by_display: str | None,
) -> dict[str, Any]:
    """Create an auditable work item tied to an SGL case."""

    clean_title = str(title or "").strip()[:240]
    if not clean_title:
        raise ValueError("sgl_task_title_empty")
    clean_priority = str(priority or "normal").strip().lower()
    if clean_priority not in _SGL_TASK_PRIORITIES:
        raise ValueError("sgl_task_priority_invalid")
    try:
        clean_owner_id = int(owner_id) if owner_id not in {None, "", 0, "0"} else None
    except (TypeError, ValueError) as exc:
        raise ValueError("sgl_task_owner_invalid") from exc
    clean_due = str(due_at or "").strip()[:64] or None
    clean_description = str(description or "").strip()[:4_000] or None
    clean_source = str(source or "manual").strip()[:60] or "manual"
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cursor = con.execute(
            """
            INSERT INTO sgl_case_tasks(
                case_id, guild_id, case_number, title, description, priority, status,
                owner_id, owner_display, due_at, source, created_by_id,
                created_by_display, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(case.id), int(case.guild_id), int(case.case_number), clean_title,
                clean_description, clean_priority, clean_owner_id,
                str(owner_display or "").strip()[:160] or None, clean_due,
                clean_source, int(created_by_id) if created_by_id else None,
                str(created_by_display or "").strip()[:160] or None, now, now,
            ),
        )
        task_id = int(cursor.lastrowid)
        _record_case_event(
            con, case_id=int(case.id), guild_id=int(case.guild_id),
            case_number=int(case.case_number), actor_id=created_by_id,
            actor_display=created_by_display, action="task_created",
            details=f"task_id={task_id};priority={clean_priority};source={clean_source}",
        )
        row = con.execute("SELECT * FROM sgl_case_tasks WHERE id = ?", (task_id,)).fetchone()
        con.commit()
    if row is None:  # pragma: no cover - SQLite insert guarantees the row
        raise RuntimeError("sgl_task_write_failed")
    return _case_task_payload(row)


def update_sgl_case_task(
    *,
    task_id: int,
    guild_id: int,
    values: dict[str, Any],
    actor_id: int | None,
    actor_display: str | None,
) -> dict[str, Any] | None:
    """Update or complete a task without exposing arbitrary storage columns."""

    requested = {
        key: value for key, value in dict(values or {}).items()
        if key in {"title", "description", "priority", "owner_id", "owner_display", "due_at", "status"}
    }
    if not requested:
        raise ValueError("sgl_task_change_empty")
    if "title" in requested:
        requested["title"] = str(requested["title"] or "").strip()[:240]
        if not requested["title"]:
            raise ValueError("sgl_task_title_empty")
    if "description" in requested:
        requested["description"] = str(requested["description"] or "").strip()[:4_000] or None
    if "priority" in requested:
        requested["priority"] = str(requested["priority"] or "").strip().lower()
        if requested["priority"] not in _SGL_TASK_PRIORITIES:
            raise ValueError("sgl_task_priority_invalid")
    if "status" in requested:
        requested["status"] = str(requested["status"] or "").strip().lower()
        if requested["status"] not in _SGL_TASK_STATUSES:
            raise ValueError("sgl_task_status_invalid")
    if "owner_id" in requested:
        try:
            requested["owner_id"] = int(requested["owner_id"]) if requested["owner_id"] not in {None, "", 0, "0"} else None
        except (TypeError, ValueError) as exc:
            raise ValueError("sgl_task_owner_invalid") from exc
    if "owner_display" in requested:
        requested["owner_display"] = str(requested["owner_display"] or "").strip()[:160] or None
    if "due_at" in requested:
        requested["due_at"] = str(requested["due_at"] or "").strip()[:64] or None
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM sgl_case_tasks WHERE id = ? AND guild_id = ?",
            (int(task_id), int(guild_id)),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        assignments = [f"{field} = ?" for field in requested]
        params: list[Any] = list(requested.values())
        task_status = str(requested.get("status") or row["status"])
        if "status" in requested:
            if task_status == "done":
                assignments.extend(["completed_at = ?", "completed_by_id = ?", "completed_by_display = ?"])
                params.extend([now, int(actor_id) if actor_id else None, str(actor_display or "")[:160] or None])
            elif str(row["status"]) == "done":
                assignments.extend(["completed_at = NULL", "completed_by_id = NULL", "completed_by_display = NULL"])
        assignments.append("updated_at = ?")
        params.extend([now, int(task_id)])
        con.execute(f"UPDATE sgl_case_tasks SET {', '.join(assignments)} WHERE id = ?", params)
        action = "task_completed" if requested.get("status") == "done" else "task_updated"
        _record_case_event(
            con, case_id=int(row["case_id"]), guild_id=int(guild_id),
            case_number=int(row["case_number"]), actor_id=actor_id,
            actor_display=actor_display, action=action,
            details=json.dumps({"task_id": int(task_id), "fields": sorted(requested)}, ensure_ascii=False),
        )
        updated = con.execute("SELECT * FROM sgl_case_tasks WHERE id = ?", (int(task_id),)).fetchone()
        con.commit()
    return _case_task_payload(updated) if updated is not None else None


def list_sgl_case_notifications(
    *,
    guild_id: int,
    case_id: int | None = None,
    case_number: int | None = None,
    severity: str | None = None,
    acknowledged: bool | None = None,
    external_status: str | None = None,
    source: str | None = None,
    include_acknowledged: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List a guild-scoped notification inbox or an acknowledged history.

    ``include_acknowledged`` remains for compatibility with the original
    inbox API.  New explicit filters make the distinction between an
    acknowledgement state and an external delivery state unambiguous for
    management clients.
    """

    clean_severity = str(severity or "").strip().lower() or None
    if clean_severity is not None and clean_severity not in _SGL_NOTIFICATION_SEVERITIES:
        raise ValueError("sgl_notification_severity_invalid")
    if acknowledged is not None and not isinstance(acknowledged, bool):
        raise ValueError("sgl_notification_acknowledged_invalid")
    clean_delivery = str(external_status or "").strip().lower() or None
    if clean_delivery is not None and clean_delivery not in _SGL_NOTIFICATION_DELIVERY:
        raise ValueError("sgl_notification_delivery_invalid")
    clean_source = str(source or "").strip()[:80] or None
    if source is not None and not clean_source:
        raise ValueError("sgl_notification_source_invalid")
    clean_case_id = int(case_id) if case_id is not None else None
    clean_case_number = int(case_number) if case_number is not None else None
    if clean_case_id is not None and clean_case_id <= 0:
        raise ValueError("sgl_notification_case_invalid")
    if clean_case_number is not None and clean_case_number <= 0:
        raise ValueError("sgl_notification_case_invalid")
    selected_limit = max(1, min(int(limit), 500))
    clauses = ["n.guild_id = ?"]
    params: list[Any] = [int(guild_id)]
    if clean_case_id is not None:
        clauses.append("n.case_id = ?")
        params.append(clean_case_id)
    if clean_case_number is not None:
        clauses.append("n.case_number = ?")
        params.append(clean_case_number)
    if clean_severity is not None:
        clauses.append("n.severity = ?")
        params.append(clean_severity)
    if acknowledged is True:
        clauses.append("n.acknowledged_at IS NOT NULL")
    elif acknowledged is False or not include_acknowledged:
        clauses.append("n.acknowledged_at IS NULL")
    if clean_delivery is not None:
        clauses.append("n.external_status = ?")
        params.append(clean_delivery)
    if clean_source is not None:
        clauses.append("n.source = ?")
        params.append(clean_source)
    with _db_lock, connect() as con:
        rows = con.execute(
            f"""
            SELECT n.*, c.client_display, c.client_nick, c.status AS case_status
            FROM sgl_case_notifications AS n
            LEFT JOIN sgl_cases AS c ON c.id = n.case_id
            WHERE {' AND '.join(clauses)}
            ORDER BY CASE n.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 WHEN 'info' THEN 2 ELSE 3 END,
                     n.id DESC
            LIMIT ?
            """,
            [*params, selected_limit],
        ).fetchall()
    return [_case_notification_payload(row) for row in rows]


def create_sgl_case_notification(
    *,
    guild_id: int,
    case: SGLCase | None,
    kind: Any,
    severity: Any,
    title: Any,
    body: Any = None,
    tab: Any = None,
    source: Any = "system",
    external_status: Any = "not_applicable",
    dedupe_key: Any = None,
) -> dict[str, Any]:
    """Persist an inbox item, optionally de-duplicating one external signal."""

    clean_kind = str(kind or "system").strip()[:80] or "system"
    clean_severity = str(severity or "info").strip().lower()
    if clean_severity not in _SGL_NOTIFICATION_SEVERITIES:
        raise ValueError("sgl_notification_severity_invalid")
    clean_title = str(title or "").strip()[:300]
    if not clean_title:
        raise ValueError("sgl_notification_title_empty")
    clean_delivery = str(external_status or "not_applicable").strip().lower()
    if clean_delivery not in _SGL_NOTIFICATION_DELIVERY:
        raise ValueError("sgl_notification_delivery_invalid")
    clean_key = str(dedupe_key or "").strip()[:200] or None
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if clean_key:
            con.execute(
                """
                INSERT INTO sgl_case_notifications(
                    guild_id, case_id, case_number, kind, severity, title, body,
                    tab, source, external_status, dedupe_key, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, dedupe_key) DO UPDATE SET
                    severity = excluded.severity, title = excluded.title,
                    body = excluded.body, tab = excluded.tab, source = excluded.source,
                    external_status = excluded.external_status, updated_at = excluded.updated_at
                """,
                (
                    int(guild_id), int(case.id) if case else None,
                    int(case.case_number) if case else None, clean_kind, clean_severity,
                    clean_title, str(body or "").strip()[:4_000] or None,
                    str(tab or "").strip()[:40] or None,
                    str(source or "system").strip()[:80] or "system", clean_delivery,
                    clean_key, now, now,
                ),
            )
            row = con.execute(
                "SELECT * FROM sgl_case_notifications WHERE guild_id = ? AND dedupe_key = ?",
                (int(guild_id), clean_key),
            ).fetchone()
        else:
            cursor = con.execute(
                """
                INSERT INTO sgl_case_notifications(
                    guild_id, case_id, case_number, kind, severity, title, body,
                    tab, source, external_status, dedupe_key, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    int(guild_id), int(case.id) if case else None,
                    int(case.case_number) if case else None, clean_kind, clean_severity,
                    clean_title, str(body or "").strip()[:4_000] or None,
                    str(tab or "").strip()[:40] or None,
                    str(source or "system").strip()[:80] or "system", clean_delivery,
                    now, now,
                ),
            )
            row = con.execute("SELECT * FROM sgl_case_notifications WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
        con.commit()
    if row is None:  # pragma: no cover - guarded by inserts above
        raise RuntimeError("sgl_notification_write_failed")
    return _case_notification_payload(row)


def update_sgl_case_notification_delivery(
    notification_id: int,
    *,
    guild_id: int,
    external_status: Any,
) -> dict[str, Any] | None:
    clean_status = str(external_status or "").strip().lower()
    if clean_status not in _SGL_NOTIFICATION_DELIVERY:
        raise ValueError("sgl_notification_delivery_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "UPDATE sgl_case_notifications SET external_status = ?, updated_at = ? WHERE id = ? AND guild_id = ?",
            (clean_status, now, int(notification_id), int(guild_id)),
        )
        row = con.execute(
            "SELECT * FROM sgl_case_notifications WHERE id = ? AND guild_id = ?",
            (int(notification_id), int(guild_id)),
        ).fetchone()
        con.commit()
    return _case_notification_payload(row) if row is not None else None


def acknowledge_sgl_case_notification(
    notification_id: int,
    *,
    guild_id: int,
    actor_id: int | None,
    actor_display: str | None,
) -> dict[str, Any] | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            UPDATE sgl_case_notifications
            SET acknowledged_at = COALESCE(acknowledged_at, ?),
                acknowledged_by_id = COALESCE(acknowledged_by_id, ?),
                acknowledged_by_display = COALESCE(acknowledged_by_display, ?),
                updated_at = ?
            WHERE id = ? AND guild_id = ?
            """,
            (now, int(actor_id) if actor_id else None, str(actor_display or "")[:160] or None, now, int(notification_id), int(guild_id)),
        )
        row = con.execute(
            "SELECT * FROM sgl_case_notifications WHERE id = ? AND guild_id = ?",
            (int(notification_id), int(guild_id)),
        ).fetchone()
        if row is not None and row["case_id"] is not None:
            _record_case_event(
                con, case_id=int(row["case_id"]), guild_id=int(guild_id),
                case_number=int(row["case_number"] or 0), actor_id=actor_id,
                actor_display=actor_display, action="notification_acknowledged",
                details=f"notification_id={int(notification_id)}",
            )
        con.commit()
    return _case_notification_payload(row) if row is not None else None


def list_sgl_recent_case_events(guild_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
    selected_limit = max(1, min(int(limit), 300))
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT e.*, c.client_display, c.client_nick, c.lead_lawyer_display,
                   c.status AS case_status
            FROM sgl_case_events AS e
            LEFT JOIN sgl_cases AS c ON c.id = e.case_id
            WHERE e.guild_id = ?
            ORDER BY e.id DESC
            LIMIT ?
            """,
            (int(guild_id), selected_limit),
        ).fetchall()
    return [dict(row) for row in rows]


def build_sgl_operations_snapshot(guild_id: int, *, limit: int = 80) -> dict[str, Any]:
    """Build the server-side work queue from durable SGL state.

    This is intentionally not a client-side approximation over a paginated
    case list.  It combines explicit tasks with real data gaps, payment state
    and Forum Watch observations so the operator can act from one queue.
    """

    selected_limit = max(10, min(int(limit), 200))
    with _db_lock, connect() as con:
        cases = con.execute(
            """
            SELECT * FROM sgl_cases
            WHERE guild_id = ? AND status NOT IN ('closed', 'archived')
            ORDER BY updated_at DESC, case_number DESC
            """,
            (int(guild_id),),
        ).fetchall()
        task_rows = con.execute(
            """
            SELECT t.*, c.client_display, c.client_nick, c.status AS case_status
            FROM sgl_case_tasks AS t
            JOIN sgl_cases AS c ON c.id = t.case_id
            WHERE t.guild_id = ? AND t.status = 'open'
            ORDER BY CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                     CASE WHEN t.due_at IS NULL OR t.due_at = '' THEN 1 ELSE 0 END,
                     t.due_at ASC, t.id DESC
            LIMIT ?
            """,
            (int(guild_id), selected_limit),
        ).fetchall()
        receipt_rows = con.execute(
            """
            SELECT r.*, c.client_display, c.client_nick, c.lead_lawyer_display
            FROM sgl_receipts AS r
            JOIN sgl_cases AS c ON c.id = r.case_id
            WHERE r.guild_id = ? AND r.status != 'confirmed'
            ORDER BY r.updated_at ASC, r.id DESC
            LIMIT ?
            """,
            (int(guild_id), selected_limit),
        ).fetchall()
        observation_rows = con.execute(
            """
            SELECT o.*, c.client_display, c.client_nick, p.title AS publication_title
            FROM sgl_case_forum_observations AS o
            JOIN sgl_cases AS c ON c.id = o.case_id
            LEFT JOIN sgl_case_forum_publications AS p ON p.id = o.publication_id
            WHERE o.guild_id = ? AND o.status IN ('changed', 'error')
            ORDER BY o.updated_at DESC, o.id DESC
            LIMIT ?
            """,
            (int(guild_id), selected_limit),
        ).fetchall()
        event_rows = con.execute(
            """
            SELECT e.*, c.client_display, c.client_nick, c.lead_lawyer_display,
                   c.status AS case_status
            FROM sgl_case_events AS e
            LEFT JOIN sgl_cases AS c ON c.id = e.case_id
            WHERE e.guild_id = ?
            ORDER BY e.id DESC
            LIMIT ?
            """,
            (int(guild_id), 30),
        ).fetchall()
        note_count = int(
            con.execute(
                "SELECT COUNT(*) AS n FROM sgl_case_ai_notes WHERE guild_id = ?",
                (int(guild_id),),
            ).fetchone()["n"] or 0
        )

    queue: list[dict[str, Any]] = []
    priority_rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}
    for row in task_rows:
        item = _case_task_payload(row)
        queue.append({
            "id": f"task:{int(item['id'])}", "kind": "task", "priority": item["priority"],
            "case_number": int(item["case_number"]), "case_id": int(item["case_id"]),
            "title": item["title"], "detail": item.get("description") or "Ручная задача команды",
            "tab": "overview", "due_at": item.get("due_at"), "owner_display": item.get("owner_display"),
            "created_at": item.get("created_at"), "task_id": int(item["id"]),
            "client_display": item.get("client_display") or item.get("client_nick"),
        })
    for row in cases:
        case = _case_from_row(row)
        name = case.client_display or case.client_nick or "Клиент не указан"
        if case.status == "error":
            queue.append({"id": f"state:{case.id}:error", "kind": "case_health", "priority": "critical", "case_number": case.case_number, "case_id": case.id, "title": "Проверить ошибку создания или синхронизации", "detail": name, "tab": "overview", "created_at": case.updated_at, "client_display": name})
        if not case.situation_text:
            queue.append({"id": f"state:{case.id}:situation", "kind": "case_health", "priority": "high", "case_number": case.case_number, "case_id": case.id, "title": "Запросить обстоятельства дела", "detail": name, "tab": "evidence", "created_at": case.updated_at, "client_display": name})
        if case.status == "awaiting_link" and not case.claim_link:
            queue.append({"id": f"state:{case.id}:claim", "kind": "case_health", "priority": "high", "case_number": case.case_number, "case_id": case.id, "title": "Подготовить или привязать ссылку на иск", "detail": name, "tab": "forum", "created_at": case.updated_at, "client_display": name})
        if case.status == "awaiting_close":
            queue.append({"id": f"state:{case.id}:close", "kind": "case_health", "priority": "normal", "case_number": case.case_number, "case_id": case.id, "title": "Провести финальную проверку перед закрытием", "detail": name, "tab": "overview", "created_at": case.updated_at, "client_display": name})
    for row in receipt_rows:
        receipt = dict(row)
        proof_ready = str(receipt.get("status")) == "proofs_submitted"
        queue.append({
            "id": f"receipt:{int(receipt['id'])}", "kind": "receipt", "priority": "high",
            "case_number": int(receipt["case_number"]), "case_id": int(receipt["case_id"]),
            "title": "Подтвердить оплату" if proof_ready else "Получить два подтверждения оплаты",
            "detail": f"Квитанция #{int(receipt['id'])} · {int(receipt['total_amount']):,}$".replace(",", " "),
            "tab": "finance", "created_at": receipt.get("updated_at"),
            "client_display": receipt.get("client_display") or receipt.get("client_nick"),
            "receipt_id": int(receipt["id"]),
        })
    for row in observation_rows:
        observation = dict(row)
        failed = str(observation.get("status")) == "error"
        queue.append({
            "id": f"forum:{int(observation['id'])}", "kind": "forum", "priority": "critical" if failed else "high",
            "case_number": int(observation["case_number"]), "case_id": int(observation["case_id"]),
            "title": "Восстановить доступ Forum Watch" if failed else "Разобрать изменение опубликованного иска",
            "detail": str(observation.get("last_error") or observation.get("thread_title") or observation.get("publication_title") or "Форумная тема"),
            "tab": "forum", "created_at": observation.get("updated_at"),
            "client_display": observation.get("client_display") or observation.get("client_nick"),
            "observation_id": int(observation["id"]),
        })
    queue.sort(key=lambda item: (priority_rank.get(str(item.get("priority")), 9), str(item.get("due_at") or "9999"), str(item.get("created_at") or "")))
    return {
        "queue": queue[:selected_limit],
        "events": [dict(row) for row in event_rows],
        "counts": {
            "open_cases": len(cases), "open_tasks": len(task_rows),
            "pending_receipts": len(receipt_rows), "forum_alerts": len(observation_rows),
            "atlas_notes": note_count,
        },
    }


def update_sgl_case_management(
    *,
    guild_id: int,
    case_number: int,
    values: dict[str, Any],
    actor_id: int | None,
    actor_display: str | None,
    expected_updated_at: str | None = None,
) -> SGLCase | None:
    """Apply an audited web-management change without exposing raw SQL fields."""

    requested = {
        key: value for key, value in dict(values or {}).items()
        if key in _SGL_CASE_MANAGEMENT_FIELDS
    }
    if not requested:
        raise ValueError("sgl_case_change_empty")
    unknown_status = requested.get("status")
    if unknown_status is not None:
        status = str(unknown_status).strip().lower()
        if status not in _SGL_WEB_MANAGEABLE_STATUSES:
            raise ValueError("sgl_case_status_invalid")
        requested["status"] = status
    for field in ("client_id", "lead_lawyer_id"):
        if field in requested:
            try:
                requested[field] = int(requested[field])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"sgl_case_{field}_invalid") from exc
            if requested[field] <= 0:
                raise ValueError(f"sgl_case_{field}_invalid")
    if "secretary_id" in requested:
        try:
            requested["secretary_id"] = (
                int(requested["secretary_id"])
                if requested["secretary_id"] not in {None, "", 0, "0"}
                else None
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("sgl_case_secretary_id_invalid") from exc
    limits = {
        "client_display": 160,
        "lead_lawyer_display": 160,
        "secretary_display": 160,
        "request_type": 160,
        "client_nick": 160,
        "static_id": 100,
        "bank_account": 200,
        "phone": 100,
        "passport_url": 1_000,
        "situation_text": 8_000,
        "claim_link": 1_000,
    }
    for field, limit in limits.items():
        if field in requested:
            value = requested[field]
            requested[field] = str(value).strip()[:limit] if value is not None else None

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM sgl_cases WHERE guild_id = ? AND case_number = ?",
            (int(guild_id), int(case_number)),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        if expected_updated_at is not None and str(row["updated_at"]) != str(expected_updated_at):
            con.commit()
            raise ValueError("sgl_case_revision_conflict")
        if row["archived_at"] is not None:
            con.commit()
            raise ValueError("sgl_case_archived_readonly")
        assignments = ", ".join(f"{field} = ?" for field in requested)
        params = list(requested.values())
        reset_closed_at = str(row["status"]) == "closed" and requested.get("status") != "closed"
        if reset_closed_at:
            assignments += ", closed_at = NULL"
        con.execute(
            f"UPDATE sgl_cases SET {assignments}, updated_at = ? WHERE id = ?",
            [*params, now, int(row["id"])],
        )
        _record_case_event(
            con,
            case_id=int(row["id"]),
            guild_id=int(guild_id),
            case_number=int(case_number),
            actor_id=actor_id,
            actor_display=actor_display,
            action="web_case_updated",
            details=json.dumps({"fields": sorted(requested)}, ensure_ascii=False),
        )
        _record_bot_action(
            con,
            guild_id=int(guild_id),
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_case",
            target_id=int(row["id"]),
            summary=f"Изменено дело №{int(case_number)} через SGL Web",
            payload={"table": "sgl_cases", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute(
            "SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)
        ).fetchone()
        con.commit()
    return _case_from_row(updated) if updated is not None else None

def list_sgl_closed_cases_pending_archive(guild_id: int | None = None) -> list[SGLCase]:
    params: list[Any] = []
    where = "status = 'closed' AND channel_id IS NOT NULL AND archived_at IS NULL"
    if guild_id is not None:
        where += " AND guild_id = ?"
        params.append(guild_id)
    with _db_lock, connect() as con:
        rows = con.execute(f"SELECT * FROM sgl_cases WHERE {where} ORDER BY closed_at ASC, case_number ASC", params).fetchall()
    return [_case_from_row(row) for row in rows]


def mark_sgl_case_archived(guild_id: int, channel_id: int, archive_category_id: int) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_cases
            SET archived_at = ?, archive_category_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, archive_category_id, now, int(row["id"])),
        )
        _record_case_event(
            con,
            case_id=int(row["id"]),
            guild_id=guild_id,
            case_number=int(row["case_number"]),
            actor_id=None,
            actor_display=None,
            action="archived",
            details=str(archive_category_id),
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def _receipt_from_row(row: sqlite3.Row) -> SGLReceipt:
    return SGLReceipt(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        case_id=int(row["case_id"]),
        case_number=int(row["case_number"]),
        channel_id=row["channel_id"],
        client_id=int(row["client_id"]),
        lead_lawyer_id=int(row["lead_lawyer_id"]),
        created_by_id=row["created_by_id"],
        created_by_display=row["created_by_display"],
        court_code=str(row["court_code"]),
        court_label=str(row["court_label"]),
        court_suffix=str(row["court_suffix"]),
        total_amount=int(row["total_amount"]),
        lawyer_amount=int(row["lawyer_amount"]),
        duty_amount=int(row["duty_amount"]),
        lawyer_bank=str(row["lawyer_bank"]),
        duty_bank=str(row["duty_bank"]),
        invoice_message_id=row["invoice_message_id"],
        proof_services_url=row["proof_services_url"],
        proof_duty_url=row["proof_duty_url"],
        proof_submitted_by_id=row["proof_submitted_by_id"],
        proof_submitted_by_display=row["proof_submitted_by_display"],
        proof_submitted_at=row["proof_submitted_at"],
        confirmation_message_id=row["confirmation_message_id"],
        confirmed_by_id=row["confirmed_by_id"],
        confirmed_by_display=row["confirmed_by_display"],
        confirmed_at=row["confirmed_at"],
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def create_sgl_receipt(
    *,
    guild_id: int,
    case: SGLCase,
    created_by_id: int | None,
    created_by_display: str | None,
    court_code: str,
    court_label: str,
    court_suffix: str,
    total_amount: int,
    lawyer_amount: int,
    duty_amount: int,
    lawyer_bank: str,
    duty_bank: str,
) -> SGLReceipt:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute(
            """
            INSERT INTO sgl_receipts(
                guild_id, case_id, case_number, channel_id, client_id, lead_lawyer_id,
                created_by_id, created_by_display, court_code, court_label, court_suffix,
                total_amount, lawyer_amount, duty_amount, lawyer_bank, duty_bank,
                status, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', ?, ?)
            """,
            (
                guild_id, case.id, case.case_number, case.channel_id, case.client_id, case.lead_lawyer_id,
                created_by_id, created_by_display, court_code, court_label, court_suffix,
                int(total_amount), int(lawyer_amount), int(duty_amount), lawyer_bank, duty_bank,
                now, now,
            ),
        )
        receipt_id = int(cur.lastrowid)
        _record_case_event(con, case_id=case.id, guild_id=guild_id, case_number=case.case_number, actor_id=created_by_id, actor_display=created_by_display, action="receipt_created", details=f"receipt_id={receipt_id};{court_suffix};total={total_amount}")
        row = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (receipt_id,)).fetchone()
        con.commit()
    return _receipt_from_row(row)


def set_sgl_receipt_invoice_message(receipt_id: int, message_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("UPDATE sgl_receipts SET invoice_message_id = ?, updated_at = ? WHERE id = ?", (message_id, now, receipt_id))
        con.commit()


def get_sgl_receipt_by_id(receipt_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (receipt_id,)).fetchone()
    return _receipt_from_row(row) if row else None


def get_sgl_receipt_by_invoice_message(guild_id: int, message_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE guild_id = ? AND invoice_message_id = ?", (guild_id, message_id)).fetchone()
    return _receipt_from_row(row) if row else None


def get_sgl_receipt_by_confirmation_message(guild_id: int, message_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE guild_id = ? AND confirmation_message_id = ?", (guild_id, message_id)).fetchone()
    return _receipt_from_row(row) if row else None


def get_sgl_receipt_by_confirmation_message_any(message_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE confirmation_message_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1", (message_id,)).fetchone()
    return _receipt_from_row(row) if row else None


def list_sgl_receipts_for_case(case_id: int, limit: int = 10) -> list[SGLReceipt]:
    with _db_lock, connect() as con:
        rows = con.execute("SELECT * FROM sgl_receipts WHERE case_id = ? ORDER BY created_at DESC, id DESC LIMIT ?", (case_id, int(limit))).fetchall()
    return [_receipt_from_row(row) for row in rows]


def get_latest_sgl_receipt_for_case(case_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE case_id = ? ORDER BY CASE WHEN status = 'confirmed' THEN 0 ELSE 1 END, created_at DESC, id DESC LIMIT 1", (case_id,)).fetchone()
    return _receipt_from_row(row) if row else None


def get_latest_confirmed_sgl_receipt_for_case(case_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE case_id = ? AND status = 'confirmed' ORDER BY confirmed_at DESC, id DESC LIMIT 1", (case_id,)).fetchone()
    return _receipt_from_row(row) if row else None


def submit_sgl_receipt_proofs_by_invoice_message(
    *,
    guild_id: int,
    invoice_message_id: int,
    services_url: str,
    duty_url: str,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLReceipt | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_receipts WHERE guild_id = ? AND invoice_message_id = ?", (guild_id, invoice_message_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_receipts
            SET proof_services_url = ?, proof_duty_url = ?, proof_submitted_by_id = ?, proof_submitted_by_display = ?, proof_submitted_at = ?, status = 'proofs_submitted', updated_at = ?
            WHERE id = ?
            """,
            (services_url, duty_url, actor_id, actor_display, now, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["case_id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="receipt_proofs_submitted", details=f"receipt_id={int(row['id'])}")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_receipt",
            target_id=int(row["id"]),
            summary=f"Добавлены доказательства оплаты по квитанции #{int(row['id'])}",
            payload={"table": "sgl_receipts", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _receipt_from_row(updated) if updated else None


def set_sgl_receipt_confirmation_message(receipt_id: int, message_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("UPDATE sgl_receipts SET confirmation_message_id = ?, updated_at = ? WHERE id = ?", (message_id, now, receipt_id))
        con.commit()


def confirm_sgl_receipt_by_id(
    *,
    receipt_id: int,
    guild_id: int,
    case_number: int,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLReceipt | None:
    """Confirm a web-visible receipt without relying on a Discord DM id."""

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT * FROM sgl_receipts
            WHERE id = ? AND guild_id = ? AND case_number = ?
            """,
            (int(receipt_id), int(guild_id), int(case_number)),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        if str(row["status"]) == "confirmed":
            con.commit()
            return _receipt_from_row(row)
        con.execute(
            """
            UPDATE sgl_receipts
            SET confirmed_by_id = ?, confirmed_by_display = ?, confirmed_at = ?,
                status = 'confirmed', updated_at = ?
            WHERE id = ?
            """,
            (actor_id, str(actor_display or "")[:160], now, now, int(row["id"])),
        )
        _record_case_event(
            con, case_id=int(row["case_id"]), guild_id=int(guild_id),
            case_number=int(case_number), actor_id=actor_id,
            actor_display=actor_display, action="receipt_confirmed",
            details=f"receipt_id={int(row['id'])}",
        )
        _record_bot_action(
            con,
            guild_id=int(guild_id), actor_id=actor_id,
            actor_display=actor_display, module="sgl",
            action_kind="generic_row_restore", target_type="sgl_receipt",
            target_id=int(row["id"]),
            summary=f"Подтверждена квитанция #{int(row['id'])}",
            payload={"table": "sgl_receipts", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _receipt_from_row(updated) if updated else None


def confirm_sgl_receipt_by_confirmation_message(
    *,
    guild_id: int,
    confirmation_message_id: int,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLReceipt | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_receipts WHERE guild_id = ? AND confirmation_message_id = ?", (guild_id, confirmation_message_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_receipts
            SET confirmed_by_id = ?, confirmed_by_display = ?, confirmed_at = ?, status = 'confirmed', updated_at = ?
            WHERE id = ?
            """,
            (actor_id, actor_display, now, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["case_id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="receipt_confirmed", details=f"receipt_id={int(row['id'])}")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_receipt",
            target_id=int(row["id"]),
            summary=f"Подтверждена квитанция #{int(row['id'])}",
            payload={"table": "sgl_receipts", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _receipt_from_row(updated) if updated else None


def confirm_sgl_receipt_by_confirmation_message_any(
    *,
    confirmation_message_id: int,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLReceipt | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_receipts WHERE confirmation_message_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1", (confirmation_message_id,)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_receipts
            SET confirmed_by_id = ?, confirmed_by_display = ?, confirmed_at = ?, status = 'confirmed', updated_at = ?
            WHERE id = ?
            """,
            (actor_id, actor_display, now, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["case_id"]), guild_id=int(row["guild_id"]), case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="receipt_confirmed", details=f"receipt_id={int(row['id'])}")
        _record_bot_action(
            con,
            guild_id=int(row["guild_id"]),
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_receipt",
            target_id=int(row["id"]),
            summary=f"Подтверждена квитанция #{int(row['id'])}",
            payload={"table": "sgl_receipts", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _receipt_from_row(updated) if updated else None


def _read_legacy_activity_json() -> dict[str, Any] | None:
    if not _core.LEGACY_ACTIVITY_FILE.exists() or not _core.LEGACY_ACTIVITY_FILE.is_file():
        return None
    try:
        with _core.LEGACY_ACTIVITY_FILE.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def migrate_legacy_activity_json() -> dict[str, int | str]:
    result: dict[str, int | str] = {"users": 0, "events": 0, "counters": 0, "status": "skipped", "reason": "legacy file not found"}
    if not _core.LEGACY_ACTIVITY_FILE.exists():
        return result

    raw = _core.LEGACY_ACTIVITY_FILE.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    meta_key = f"legacy_activity_json_sha256:{sha}"
    if get_meta(meta_key) == "imported":
        result["reason"] = "same legacy file already imported"
        return result

    data = _read_legacy_activity_json()
    if not data:
        result["reason"] = "legacy file empty or invalid"
        return result

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        users = 0
        events = 0
        counters = 0
        for guild_key, guild_data in data.items():
            if not isinstance(guild_data, dict):
                continue
            try:
                guild_id = int(guild_key)
            except (TypeError, ValueError):
                continue
            for user_key, item in guild_data.items():
                if not isinstance(item, dict):
                    continue
                try:
                    user_id = int(item.get("user_id") or user_key)
                except (TypeError, ValueError):
                    continue
                upsert_member(
                    con,
                    guild_id,
                    user_id,
                    item.get("display_name"),
                    item.get("name"),
                    item.get("mention"),
                    False,
                )
                users += 1

                counters_data = item.get("counters") if isinstance(item.get("counters"), dict) else {}
                total_events = int(counters_data.get("total", 0) or 0)
                for event_type, count in counters_data.items():
                    if event_type == "total":
                        continue
                    try:
                        count_int = int(count)
                    except (TypeError, ValueError):
                        continue
                    con.execute(
                        """
                        INSERT INTO activity_counters(guild_id, user_id, event_type, count, updated_at)
                        VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(guild_id, user_id, event_type) DO UPDATE SET
                            count = MAX(activity_counters.count, excluded.count),
                            updated_at = excluded.updated_at
                        """,
                        (guild_id, user_id, str(event_type), count_int, now),
                    )
                    counters += 1

                recent_events = item.get("recent_events") if isinstance(item.get("recent_events"), list) else []
                for event in reversed(recent_events):
                    if not isinstance(event, dict):
                        continue
                    con.execute(
                        """
                        INSERT INTO activity_events(
                            guild_id, user_id, event_type, event_text, at,
                            channel_id, channel_name, category_id, category_name,
                            details, message_id, created_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, ?)
                        """,
                        (
                            guild_id,
                            user_id,
                            str(event.get("event") or "legacy_event"),
                            event.get("event_text"),
                            str(event.get("at") or now),
                            event.get("channel_id"),
                            event.get("channel_name"),
                            event.get("details"),
                            now,
                        ),
                    )
                    events += 1

                con.execute(
                    """
                    INSERT INTO activity_summary(
                        guild_id, user_id, last_activity_at, last_event, last_event_text,
                        last_channel_id, last_channel_name, last_category_id, last_category_name,
                        last_details, total_events, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                        last_activity_at = CASE
                            WHEN activity_summary.last_activity_at IS NULL THEN excluded.last_activity_at
                            WHEN excluded.last_activity_at IS NULL THEN activity_summary.last_activity_at
                            WHEN excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_activity_at
                            ELSE activity_summary.last_activity_at
                        END,
                        last_event = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_event
                            ELSE activity_summary.last_event
                        END,
                        last_event_text = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_event_text
                            ELSE activity_summary.last_event_text
                        END,
                        last_channel_id = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_channel_id
                            ELSE activity_summary.last_channel_id
                        END,
                        last_channel_name = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_channel_name
                            ELSE activity_summary.last_channel_name
                        END,
                        last_details = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_details
                            ELSE activity_summary.last_details
                        END,
                        total_events = MAX(activity_summary.total_events, excluded.total_events),
                        updated_at = excluded.updated_at
                    """,
                    (
                        guild_id,
                        user_id,
                        item.get("last_activity_at"),
                        item.get("last_event"),
                        item.get("last_event_text"),
                        item.get("last_channel_id"),
                        item.get("last_channel_name"),
                        item.get("last_details"),
                        total_events,
                        now,
                        now,
                    ),
                )

        set_meta(con, meta_key, "imported")
        set_meta(con, "legacy_activity_json_last_sha256", sha)
        con.commit()

    result.update({"users": users, "events": events, "counters": counters, "status": "imported", "reason": "ok"})
    return result


def create_audio_generation(
    *,
    guild_id: int,
    channel_id: int,
    user_id: int,
    user_display: str | None,
    prompt: str,
    model: str,
) -> int:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        cur = con.execute(
            """
            INSERT INTO audio_generations(
                guild_id, channel_id, user_id, user_display, prompt, model,
                status, seconds_elapsed, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, 'running', 0, ?, ?)
            """,
            (guild_id, channel_id, user_id, user_display, prompt, model, now, now),
        )
        con.commit()
        return int(cur.lastrowid)


def update_audio_generation(record_id: int, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "status",
        "seconds_elapsed",
        "output_filename",
        "output_mime",
        "output_size_bytes",
        "dm_message_id",
        "error",
        "completed_at",
        "updated_at",
    }
    clean = {key: value for key, value in fields.items() if key in allowed}
    if not clean:
        return
    clean["updated_at"] = clean.get("updated_at") or utc_now_iso()
    assignments = ", ".join(f"{key} = ?" for key in clean.keys())
    params = [*clean.values(), record_id]
    with _db_lock, connect() as con:
        con.execute(f"UPDATE audio_generations SET {assignments} WHERE id = ?", params)
        con.commit()


def count_recent_running_audio_generations(guild_id: int, within_minutes: int = 30) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(1, int(within_minutes)))).isoformat()
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT COUNT(*) AS count
            FROM audio_generations
            WHERE guild_id = ? AND status = 'running' AND updated_at >= ?
            """,
            (guild_id, cutoff),
        ).fetchone()
    return int(row["count"] or 0) if row else 0


def save_client_profile(
    *,
    guild_id: int,
    discord_user_id: int,
    client_nick: str,
    static_id: str,
    bank_account: str,
    phone: str,
    passport_url: str,
    notes: str | None = None,
    last_case_id: int | None = None,
) -> int:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        existing = con.execute(
            "SELECT id FROM client_profiles WHERE guild_id = ? AND discord_user_id = ? AND lower(client_nick) = lower(?) AND static_id = ?",
            (guild_id, discord_user_id, client_nick, static_id),
        ).fetchone()
        if existing:
            profile_id = int(existing["id"])
            con.execute(
                """
                UPDATE client_profiles
                SET bank_account = ?, phone = ?, passport_url = ?, notes = COALESCE(?, notes), last_case_id = ?, updated_at = ?, last_used_at = ?
                WHERE id = ?
                """,
                (bank_account, phone, passport_url, notes, last_case_id, now, now, profile_id),
            )
        else:
            cur = con.execute(
                """
                INSERT INTO client_profiles(
                    guild_id, discord_user_id, client_nick, static_id, bank_account, phone, passport_url, notes, last_case_id, created_at, updated_at, last_used_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, discord_user_id, client_nick, static_id, bank_account, phone, passport_url, notes, last_case_id, now, now, now),
            )
            profile_id = int(cur.lastrowid)
        con.commit()
        return profile_id


def list_client_profiles_for_user(guild_id: int, discord_user_id: int, limit: int = 20) -> list[ClientProfile]:
    with _db_lock, connect() as con:
        rows = con.execute(
            "SELECT * FROM client_profiles WHERE guild_id = ? AND discord_user_id = ? ORDER BY COALESCE(last_used_at, updated_at) DESC, id DESC LIMIT ?",
            (guild_id, discord_user_id, limit),
        ).fetchall()
    return [_client_profile_from_row(r) for r in rows if r is not None]


def get_client_profile(profile_id: int, guild_id: int | None = None) -> ClientProfile | None:
    with _db_lock, connect() as con:
        if guild_id is None:
            row = con.execute("SELECT * FROM client_profiles WHERE id = ?", (profile_id,)).fetchone()
        else:
            row = con.execute("SELECT * FROM client_profiles WHERE id = ? AND guild_id = ?", (profile_id, guild_id)).fetchone()
    return _client_profile_from_row(row)


def search_client_profiles(guild_id: int, query: str, limit: int = 25) -> list[ClientProfile]:
    like = f"%{query.strip().lower()}%"
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM client_profiles
            WHERE guild_id = ? AND (
                lower(client_nick) LIKE ? OR lower(static_id) LIKE ? OR lower(bank_account) LIKE ? OR lower(phone) LIKE ? OR CAST(discord_user_id AS TEXT) LIKE ?
            )
            ORDER BY COALESCE(last_used_at, updated_at) DESC, id DESC
            LIMIT ?
            """,
            (guild_id, like, like, like, like, like, limit),
        ).fetchall()
    return [_client_profile_from_row(r) for r in rows if r is not None]


def save_lawyer_profile(
    *,
    guild_id: int,
    lawyer_nick: str,
    discord_user_id: int | None = None,
    static_id: str | None = None,
    bank_account: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    passport_url: str | None = None,
    notes: str | None = None,
) -> int:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        existing = None
        if discord_user_id:
            existing = con.execute("SELECT id FROM lawyer_profiles WHERE guild_id = ? AND discord_user_id = ?", (guild_id, discord_user_id)).fetchone()
        if existing is None:
            existing = con.execute("SELECT id FROM lawyer_profiles WHERE guild_id = ? AND lower(lawyer_nick) = lower(?) AND COALESCE(static_id,'') = COALESCE(?, '')", (guild_id, lawyer_nick, static_id)).fetchone()
        if existing:
            profile_id = int(existing["id"])
            con.execute(
                """UPDATE lawyer_profiles SET lawyer_nick=?, discord_user_id=?, static_id=?, bank_account=?, phone=?, email=?, passport_url=?, notes=?, updated_at=? WHERE id=?""",
                (lawyer_nick, discord_user_id, static_id, bank_account, phone, email, passport_url, notes, now, profile_id),
            )
        else:
            cur = con.execute(
                """INSERT INTO lawyer_profiles(guild_id, discord_user_id, lawyer_nick, static_id, bank_account, phone, email, passport_url, notes, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (guild_id, discord_user_id, lawyer_nick, static_id, bank_account, phone, email, passport_url, notes, now, now),
            )
            profile_id = int(cur.lastrowid)
        con.commit()
        return profile_id



def get_lawyer_profile_for_user(guild_id: int, discord_user_id: int) -> LawyerProfile | None:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM lawyer_profiles WHERE guild_id = ? AND discord_user_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
            (guild_id, discord_user_id),
        ).fetchone()
    return _lawyer_profile_from_row(row) if row else None

def search_lawyer_profiles(guild_id: int, query: str | None = None, limit: int = 25) -> list[LawyerProfile]:
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            rows = con.execute(
                """SELECT * FROM lawyer_profiles WHERE guild_id = ? AND (lower(lawyer_nick) LIKE ? OR lower(COALESCE(static_id,'')) LIKE ? OR lower(COALESCE(phone,'')) LIKE ? OR CAST(COALESCE(discord_user_id,'') AS TEXT) LIKE ?) ORDER BY updated_at DESC, id DESC LIMIT ?""",
                (guild_id, like, like, like, like, limit),
            ).fetchall()
        else:
            rows = con.execute("SELECT * FROM lawyer_profiles WHERE guild_id = ? ORDER BY updated_at DESC, id DESC LIMIT ?", (guild_id, limit)).fetchall()
    return [_lawyer_profile_from_row(r) for r in rows if r is not None]


# Registry pagination helpers for /sg inline panels.
def count_client_profiles(guild_id: int, query: str | None = None) -> int:
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            row = con.execute(
                """
                SELECT COUNT(*) AS c FROM client_profiles
                WHERE guild_id = ? AND (
                    lower(client_nick) LIKE ? OR lower(static_id) LIKE ? OR lower(bank_account) LIKE ? OR lower(phone) LIKE ? OR CAST(discord_user_id AS TEXT) LIKE ?
                )
                """,
                (guild_id, like, like, like, like, like),
            ).fetchone()
        else:
            row = con.execute("SELECT COUNT(*) AS c FROM client_profiles WHERE guild_id = ?", (guild_id,)).fetchone()
    return int(row["c"] or 0) if row else 0


def list_client_profiles_page(guild_id: int, page: int = 0, per_page: int = 10, query: str | None = None) -> list[ClientProfile]:
    page = max(0, int(page))
    per_page = max(1, min(25, int(per_page)))
    offset = page * per_page
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            rows = con.execute(
                """
                SELECT * FROM client_profiles
                WHERE guild_id = ? AND (
                    lower(client_nick) LIKE ? OR lower(static_id) LIKE ? OR lower(bank_account) LIKE ? OR lower(phone) LIKE ? OR CAST(discord_user_id AS TEXT) LIKE ?
                )
                ORDER BY lower(client_nick) ASC, CAST(static_id AS INTEGER) ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (guild_id, like, like, like, like, like, per_page, offset),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT * FROM client_profiles
                WHERE guild_id = ?
                ORDER BY lower(client_nick) ASC, CAST(static_id AS INTEGER) ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (guild_id, per_page, offset),
            ).fetchall()
    return [_client_profile_from_row(r) for r in rows if r is not None]


def count_lawyer_profiles(guild_id: int, query: str | None = None) -> int:
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            row = con.execute(
                """
                SELECT COUNT(*) AS c FROM lawyer_profiles
                WHERE guild_id = ? AND (
                    lower(lawyer_nick) LIKE ? OR lower(COALESCE(static_id,'')) LIKE ? OR lower(COALESCE(phone,'')) LIKE ? OR lower(COALESCE(bank_account,'')) LIKE ? OR CAST(COALESCE(discord_user_id,'') AS TEXT) LIKE ?
                )
                """,
                (guild_id, like, like, like, like, like),
            ).fetchone()
        else:
            row = con.execute("SELECT COUNT(*) AS c FROM lawyer_profiles WHERE guild_id = ?", (guild_id,)).fetchone()
    return int(row["c"] or 0) if row else 0


def list_lawyer_profiles_page(guild_id: int, page: int = 0, per_page: int = 10, query: str | None = None) -> list[LawyerProfile]:
    page = max(0, int(page))
    per_page = max(1, min(25, int(per_page)))
    offset = page * per_page
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            rows = con.execute(
                """
                SELECT * FROM lawyer_profiles
                WHERE guild_id = ? AND (
                    lower(lawyer_nick) LIKE ? OR lower(COALESCE(static_id,'')) LIKE ? OR lower(COALESCE(phone,'')) LIKE ? OR lower(COALESCE(bank_account,'')) LIKE ? OR CAST(COALESCE(discord_user_id,'') AS TEXT) LIKE ?
                )
                ORDER BY lower(lawyer_nick) ASC, CAST(COALESCE(static_id,'0') AS INTEGER) ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (guild_id, like, like, like, like, like, per_page, offset),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT * FROM lawyer_profiles
                WHERE guild_id = ?
                ORDER BY lower(lawyer_nick) ASC, CAST(COALESCE(static_id,'0') AS INTEGER) ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (guild_id, per_page, offset),
            ).fetchall()
    return [_lawyer_profile_from_row(r) for r in rows if r is not None]


def count_open_sgl_cases(guild_id: int) -> int:
    with _db_lock, connect() as con:
        row = con.execute("SELECT COUNT(*) AS c FROM sgl_cases WHERE guild_id = ? AND status != 'closed'", (guild_id,)).fetchone()
    return int(row["c"] or 0) if row else 0


def count_closed_sgl_cases(guild_id: int) -> int:
    with _db_lock, connect() as con:
        row = con.execute("SELECT COUNT(*) AS c FROM sgl_cases WHERE guild_id = ? AND status = 'closed'", (guild_id,)).fetchone()
    return int(row["c"] or 0) if row else 0

# Manual SGL admin editor helpers.

__all__ = ['_case_from_row', '_record_case_event', 'set_sgl_case_seed', '_next_case_number', 'reserve_sgl_case', 'attach_sgl_case_channel', 'mark_sgl_case_error', 'get_sgl_case_by_channel', 'get_sgl_case_by_number', 'update_sgl_case_params', 'update_sgl_case_situation', 'set_sgl_case_link', 'set_sgl_case_link_message', 'close_sgl_case', 'list_sgl_cases_with_channels', 'list_sgl_cases_for_client', 'list_sgl_cases_for_participant', 'get_sgl_case_events', 'record_sgl_case_message', 'update_sgl_case_message_from_discord', 'mark_sgl_case_message_deleted', 'list_sgl_case_messages', 'list_sgl_case_forum_publications', 'get_sgl_case_forum_publication', 'create_sgl_case_forum_publication', 'update_sgl_case_forum_publication', 'claim_sgl_case_forum_publication', 'mark_sgl_case_forum_publication_published', 'mark_sgl_case_forum_publication_failed', 'update_sgl_case_management', 'list_sgl_closed_cases_pending_archive', 'mark_sgl_case_archived', '_receipt_from_row', 'create_sgl_receipt', 'set_sgl_receipt_invoice_message', 'get_sgl_receipt_by_id', 'get_sgl_receipt_by_invoice_message', 'get_sgl_receipt_by_confirmation_message', 'get_sgl_receipt_by_confirmation_message_any', 'list_sgl_receipts_for_case', 'get_latest_sgl_receipt_for_case', 'get_latest_confirmed_sgl_receipt_for_case', 'submit_sgl_receipt_proofs_by_invoice_message', 'set_sgl_receipt_confirmation_message', 'confirm_sgl_receipt_by_id', 'confirm_sgl_receipt_by_confirmation_message', 'confirm_sgl_receipt_by_confirmation_message_any', '_read_legacy_activity_json', 'migrate_legacy_activity_json', 'create_audio_generation', 'update_audio_generation', 'count_recent_running_audio_generations', 'save_client_profile', 'list_client_profiles_for_user', 'get_client_profile', 'search_client_profiles', 'save_lawyer_profile', 'get_lawyer_profile_for_user', 'search_lawyer_profiles', 'count_client_profiles', 'list_client_profiles_page', 'count_lawyer_profiles', 'list_lawyer_profiles_page', 'count_open_sgl_cases', 'count_closed_sgl_cases']

__all__.extend([
    'get_sgl_case_message_by_discord_id',
    'list_sgl_case_ai_notes', 'record_sgl_case_ai_note',
    'list_sgl_case_decisions', 'create_sgl_case_decision',
    'update_sgl_case_decision_status',
    'list_sgl_forum_publications_for_guild', 'list_sgl_forum_observations',
    'list_sgl_case_forum_observations',
    'observe_sgl_case_forum_publication', 'list_sgl_case_forum_snapshots',
    'mark_sgl_forum_observation_notified',
    'list_sgl_case_tasks', 'list_sgl_case_tasks_for_guild',
    'create_sgl_case_task', 'update_sgl_case_task',
    'list_sgl_case_notifications', 'create_sgl_case_notification',
    'update_sgl_case_notification_delivery', 'acknowledge_sgl_case_notification',
    'list_sgl_recent_case_events', 'build_sgl_operations_snapshot',
])
