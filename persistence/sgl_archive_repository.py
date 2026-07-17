"""Durable storage for Discord SGL case transcripts and restorations."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from persistence import core as _core
from persistence.activity_repository import get_meta, set_meta_value
from persistence.core import (
    SGLArchiveMessage,
    SGLArchiveRestoration,
    SGLCaseArchive,
    _db_lock,
    connect,
    utc_now_iso,
)


def sgl_archive_root() -> Path:
    root = _core.DATA_DIR / "sgl_case_archives"
    root.mkdir(parents=True, exist_ok=True)
    return root


def sgl_archive_case_directory(guild_id: int, case_number: int) -> Path:
    directory = sgl_archive_root() / str(int(guild_id)) / f"{int(case_number):03d}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _archive_from_row(row: sqlite3.Row | None) -> SGLCaseArchive | None:
    if row is None:
        return None
    return SGLCaseArchive(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        case_id=int(row["case_id"]) if row["case_id"] is not None else None,
        case_number=int(row["case_number"]),
        original_channel_id=int(row["original_channel_id"]),
        original_channel_name=str(row["original_channel_name"] or ""),
        original_topic=row["original_topic"],
        original_category_id=(
            int(row["original_category_id"])
            if row["original_category_id"] is not None
            else None
        ),
        status=str(row["status"] or "capturing"),
        message_count=int(row["message_count"] or 0),
        attachment_count=int(row["attachment_count"] or 0),
        total_bytes=int(row["total_bytes"] or 0),
        snapshot_started_at=str(row["snapshot_started_at"]),
        snapshot_completed_at=row["snapshot_completed_at"],
        source_deleted_at=row["source_deleted_at"],
        metadata_json=str(row["metadata_json"] or "{}"),
        last_error=row["last_error"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _message_from_row(row: sqlite3.Row) -> SGLArchiveMessage:
    return SGLArchiveMessage(
        id=int(row["id"]),
        archive_id=int(row["archive_id"]),
        original_message_id=int(row["original_message_id"]),
        container_id=int(row["container_id"]),
        container_type=str(row["container_type"] or "channel"),
        container_name=str(row["container_name"] or ""),
        author_id=int(row["author_id"]) if row["author_id"] is not None else None,
        author_name=str(row["author_name"] or ""),
        author_display=str(row["author_display"] or ""),
        author_avatar_url=row["author_avatar_url"],
        author_is_bot=bool(row["author_is_bot"]),
        content=str(row["content"] or ""),
        embeds_json=str(row["embeds_json"] or "[]"),
        attachments_json=str(row["attachments_json"] or "[]"),
        stickers_json=str(row["stickers_json"] or "[]"),
        reactions_json=str(row["reactions_json"] or "[]"),
        components_json=str(row["components_json"] or "[]"),
        reference_message_id=(
            int(row["reference_message_id"])
            if row["reference_message_id"] is not None
            else None
        ),
        created_at=str(row["created_at"]),
        edited_at=row["edited_at"],
        pinned=bool(row["pinned"]),
        position=int(row["position"]),
    )


def _restoration_from_row(row: sqlite3.Row | None) -> SGLArchiveRestoration | None:
    if row is None:
        return None
    return SGLArchiveRestoration(
        id=int(row["id"]),
        archive_id=int(row["archive_id"]),
        guild_id=int(row["guild_id"]),
        restored_channel_id=int(row["restored_channel_id"]),
        restored_by_id=int(row["restored_by_id"]),
        restored_by_display=str(row["restored_by_display"] or ""),
        status=str(row["status"] or "restoring"),
        restored_at=str(row["restored_at"]),
        completed_at=row["completed_at"],
        expires_at=str(row["expires_at"]),
        deleted_at=row["deleted_at"],
        last_error=row["last_error"],
    )


def _json(value: Any, fallback: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(fallback, ensure_ascii=False)


def get_sgl_case_archive(guild_id: int, case_number: int) -> SGLCaseArchive | None:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM sgl_case_archives WHERE guild_id = ? AND case_number = ?",
            (int(guild_id), int(case_number)),
        ).fetchone()
    return _archive_from_row(row)


def get_sgl_case_archive_by_source(
    guild_id: int, channel_id: int
) -> SGLCaseArchive | None:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT * FROM sgl_case_archives
            WHERE guild_id = ? AND original_channel_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (int(guild_id), int(channel_id)),
        ).fetchone()
    return _archive_from_row(row)


def save_sgl_case_archive_snapshot(
    *,
    guild_id: int,
    case_id: int | None,
    case_number: int,
    original_channel_id: int,
    original_channel_name: str,
    original_topic: str | None,
    original_category_id: int | None,
    metadata: dict[str, Any],
    messages: Iterable[dict[str, Any]],
    snapshot_started_at: str,
) -> SGLCaseArchive:
    """Atomically replace a not-yet-deleted source snapshot."""

    rows = list(messages)
    attachment_count = 0
    total_bytes = 0
    for item in rows:
        attachments = item.get("attachments") or []
        attachment_count += len(attachments)
        total_bytes += sum(int(part.get("size") or 0) for part in attachments)

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT * FROM sgl_case_archives WHERE guild_id = ? AND case_number = ?",
            (int(guild_id), int(case_number)),
        ).fetchone()
        if existing is not None and existing["source_deleted_at"] is not None:
            raise ValueError("sgl_archive_already_sealed")

        if existing is None:
            cur = con.execute(
                """
                INSERT INTO sgl_case_archives(
                    guild_id, case_id, case_number, original_channel_id,
                    original_channel_name, original_topic, original_category_id,
                    status, message_count, attachment_count, total_bytes,
                    snapshot_started_at, snapshot_completed_at, metadata_json,
                    created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(guild_id),
                    int(case_id) if case_id is not None else None,
                    int(case_number),
                    int(original_channel_id),
                    str(original_channel_name),
                    original_topic,
                    int(original_category_id) if original_category_id is not None else None,
                    len(rows),
                    attachment_count,
                    total_bytes,
                    str(snapshot_started_at),
                    now,
                    _json(metadata, {}),
                    now,
                    now,
                ),
            )
            archive_id = int(cur.lastrowid)
        else:
            archive_id = int(existing["id"])
            if int(existing["original_channel_id"]) != int(original_channel_id):
                raise ValueError("sgl_archive_case_number_collision")
            con.execute(
                """
                UPDATE sgl_case_archives
                SET case_id = ?, original_channel_name = ?, original_topic = ?,
                    original_category_id = ?, status = 'ready', message_count = ?,
                    attachment_count = ?, total_bytes = ?, snapshot_started_at = ?,
                    snapshot_completed_at = ?, metadata_json = ?, last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    int(case_id) if case_id is not None else None,
                    str(original_channel_name),
                    original_topic,
                    int(original_category_id) if original_category_id is not None else None,
                    len(rows),
                    attachment_count,
                    total_bytes,
                    str(snapshot_started_at),
                    now,
                    _json(metadata, {}),
                    now,
                    archive_id,
                ),
            )
            con.execute(
                "DELETE FROM sgl_case_archive_messages WHERE archive_id = ?",
                (archive_id,),
            )

        for position, item in enumerate(rows):
            con.execute(
                """
                INSERT INTO sgl_case_archive_messages(
                    archive_id, original_message_id, container_id, container_type,
                    container_name, author_id, author_name, author_display,
                    author_avatar_url, author_is_bot, content, embeds_json,
                    attachments_json, stickers_json, reactions_json, components_json,
                    reference_message_id, created_at, edited_at, pinned, position
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    archive_id,
                    int(item["original_message_id"]),
                    int(item.get("container_id") or original_channel_id),
                    str(item.get("container_type") or "channel"),
                    str(item.get("container_name") or original_channel_name),
                    int(item["author_id"]) if item.get("author_id") is not None else None,
                    str(item.get("author_name") or ""),
                    str(item.get("author_display") or ""),
                    item.get("author_avatar_url"),
                    1 if item.get("author_is_bot") else 0,
                    str(item.get("content") or ""),
                    _json(item.get("embeds") or [], []),
                    _json(item.get("attachments") or [], []),
                    _json(item.get("stickers") or [], []),
                    _json(item.get("reactions") or [], []),
                    _json(item.get("components") or [], []),
                    (
                        int(item["reference_message_id"])
                        if item.get("reference_message_id") is not None
                        else None
                    ),
                    str(item["created_at"]),
                    item.get("edited_at"),
                    1 if item.get("pinned") else 0,
                    position,
                ),
            )
        con.commit()
        saved = con.execute(
            "SELECT * FROM sgl_case_archives WHERE id = ?", (archive_id,)
        ).fetchone()
    archive = _archive_from_row(saved)
    if archive is None:
        raise RuntimeError("sgl_archive_snapshot_missing_after_save")
    return archive


def list_sgl_archive_messages(archive_id: int) -> list[SGLArchiveMessage]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_case_archive_messages
            WHERE archive_id = ? ORDER BY position ASC, id ASC
            """,
            (int(archive_id),),
        ).fetchall()
    return [_message_from_row(row) for row in rows]


def mark_sgl_archive_source_deleted(archive_id: int) -> SGLCaseArchive | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM sgl_case_archives WHERE id = ?", (int(archive_id),)
        ).fetchone()
        if row is None:
            con.rollback()
            return None
        con.execute(
            """
            UPDATE sgl_case_archives
            SET status = 'sealed', source_deleted_at = COALESCE(source_deleted_at, ?),
                last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (now, now, int(archive_id)),
        )
        case_id = row["case_id"]
        if case_id is not None:
            con.execute(
                "UPDATE sgl_cases SET channel_id = NULL, updated_at = ? WHERE id = ? AND channel_id = ?",
                (now, int(case_id), int(row["original_channel_id"])),
            )
            con.execute(
                """
                INSERT INTO sgl_case_events(
                    case_id, guild_id, case_number, action, details, created_at
                ) VALUES(?, ?, ?, 'transcript_archived', ?, ?)
                """,
                (
                    int(case_id),
                    int(row["guild_id"]),
                    int(row["case_number"]),
                    f"messages={int(row['message_count'])};bytes={int(row['total_bytes'])}",
                    now,
                ),
            )
        con.commit()
        updated = con.execute(
            "SELECT * FROM sgl_case_archives WHERE id = ?", (int(archive_id),)
        ).fetchone()
    return _archive_from_row(updated)


def mark_sgl_archive_error(archive_id: int, error: str) -> None:
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE sgl_case_archives
            SET status = CASE WHEN source_deleted_at IS NULL THEN 'error' ELSE status END,
                last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(error)[:2000], utc_now_iso(), int(archive_id)),
        )
        con.commit()


def list_sgl_archives_pending_source_reconcile(
    guild_id: int | None = None,
) -> list[SGLCaseArchive]:
    where = "source_deleted_at IS NULL AND status IN ('ready', 'error')"
    params: tuple[Any, ...] = ()
    if guild_id is not None:
        where += " AND guild_id = ?"
        params = (int(guild_id),)
    with _db_lock, connect() as con:
        rows = con.execute(
            f"SELECT * FROM sgl_case_archives WHERE {where} ORDER BY id ASC", params
        ).fetchall()
    return [item for row in rows if (item := _archive_from_row(row)) is not None]


def _backfill_key(guild_id: int, category_id: int) -> str:
    return f"sgl-case-archive-backfill-v1:{int(guild_id)}:{int(category_id)}"


def is_sgl_archive_backfill_complete(guild_id: int, category_id: int) -> bool:
    return get_meta(_backfill_key(guild_id, category_id)) == "complete"


def mark_sgl_archive_backfill_complete(guild_id: int, category_id: int) -> None:
    set_meta_value(_backfill_key(guild_id, category_id), "complete")


def create_sgl_archive_restoration(
    *,
    archive_id: int,
    guild_id: int,
    restored_channel_id: int,
    restored_by_id: int,
    restored_by_display: str,
    expires_at: str,
) -> SGLArchiveRestoration:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        active = con.execute(
            """
            SELECT * FROM sgl_case_archive_restorations
            WHERE archive_id = ? AND deleted_at IS NULL
              AND status IN ('restoring', 'complete')
            ORDER BY id DESC LIMIT 1
            """,
            (int(archive_id),),
        ).fetchone()
        if active is not None:
            con.rollback()
            existing = _restoration_from_row(active)
            if existing is None:
                raise RuntimeError("sgl_archive_active_restoration_missing")
            return existing
        cur = con.execute(
            """
            INSERT INTO sgl_case_archive_restorations(
                archive_id, guild_id, restored_channel_id, restored_by_id,
                restored_by_display, status, restored_at, expires_at
            ) VALUES(?, ?, ?, ?, ?, 'restoring', ?, ?)
            """,
            (
                int(archive_id),
                int(guild_id),
                int(restored_channel_id),
                int(restored_by_id),
                str(restored_by_display),
                now,
                str(expires_at),
            ),
        )
        restoration_id = int(cur.lastrowid)
        con.commit()
        row = con.execute(
            "SELECT * FROM sgl_case_archive_restorations WHERE id = ?",
            (restoration_id,),
        ).fetchone()
    restoration = _restoration_from_row(row)
    if restoration is None:
        raise RuntimeError("sgl_archive_restoration_missing_after_create")
    return restoration


def get_active_sgl_archive_restoration(
    archive_id: int,
) -> SGLArchiveRestoration | None:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT * FROM sgl_case_archive_restorations
            WHERE archive_id = ? AND deleted_at IS NULL
              AND status IN ('restoring', 'complete')
            ORDER BY id DESC LIMIT 1
            """,
            (int(archive_id),),
        ).fetchone()
    return _restoration_from_row(row)


def get_active_sgl_restoration_by_channel(
    guild_id: int, channel_id: int
) -> SGLArchiveRestoration | None:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT * FROM sgl_case_archive_restorations
            WHERE guild_id = ? AND restored_channel_id = ? AND deleted_at IS NULL
              AND status IN ('restoring', 'complete', 'error')
            ORDER BY id DESC LIMIT 1
            """,
            (int(guild_id), int(channel_id)),
        ).fetchone()
    return _restoration_from_row(row)


def mark_sgl_archive_restoration_complete(restoration_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE sgl_case_archive_restorations
            SET status = 'complete', completed_at = ?, last_error = NULL
            WHERE id = ?
            """,
            (now, int(restoration_id)),
        )
        con.commit()


def mark_sgl_archive_restoration_error(restoration_id: int, error: str) -> None:
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE sgl_case_archive_restorations
            SET status = 'error', last_error = ? WHERE id = ?
            """,
            (str(error)[:2000], int(restoration_id)),
        )
        con.commit()


def list_expired_sgl_archive_restorations(
    *, now: str | None = None
) -> list[SGLArchiveRestoration]:
    cutoff = str(now or datetime.now(timezone.utc).isoformat())
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_case_archive_restorations
            WHERE deleted_at IS NULL AND expires_at <= ?
              AND status IN ('restoring', 'complete', 'error')
            ORDER BY expires_at ASC, id ASC
            """,
            (cutoff,),
        ).fetchall()
    return [
        item for row in rows if (item := _restoration_from_row(row)) is not None
    ]


def mark_sgl_archive_restoration_deleted(restoration_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE sgl_case_archive_restorations
            SET status = 'deleted', deleted_at = COALESCE(deleted_at, ?)
            WHERE id = ?
            """,
            (now, int(restoration_id)),
        )
        con.commit()


__all__ = [
    "sgl_archive_root",
    "sgl_archive_case_directory",
    "get_sgl_case_archive",
    "get_sgl_case_archive_by_source",
    "save_sgl_case_archive_snapshot",
    "list_sgl_archive_messages",
    "mark_sgl_archive_source_deleted",
    "mark_sgl_archive_error",
    "list_sgl_archives_pending_source_reconcile",
    "is_sgl_archive_backfill_complete",
    "mark_sgl_archive_backfill_complete",
    "create_sgl_archive_restoration",
    "get_active_sgl_archive_restoration",
    "get_active_sgl_restoration_by_channel",
    "mark_sgl_archive_restoration_complete",
    "mark_sgl_archive_restoration_error",
    "list_expired_sgl_archive_restorations",
    "mark_sgl_archive_restoration_deleted",
]
