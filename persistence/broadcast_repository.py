"""Durable administrator broadcasts and per-recipient delivery state."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from persistence.core import _db_lock, connect, utc_now_iso
from persistence.outbox_repository import delivery_outbox_enqueue_in_connection


BROADCAST_KINDS = frozenset({"global", "consensus"})
TERMINAL_RECIPIENT_STATUSES = frozenset({"delivered", "skipped", "unavailable"})


def _clean_text(value: Any, *, minimum: int, maximum: int, error: str) -> str:
    text = str(value or "").strip()
    if len(text) < minimum or len(text) > maximum:
        raise ValueError(error)
    return text


def create_broadcast_draft(
    *,
    guild_id: int,
    author_id: int,
    author_display: str | None,
    kind: str,
    title: str,
    body: str,
    link_url: str | None = None,
) -> dict[str, Any]:
    clean_kind = str(kind or "").strip().lower()
    if clean_kind not in BROADCAST_KINDS:
        raise ValueError("broadcast_kind_invalid")
    clean_title = _clean_text(
        title,
        minimum=3,
        maximum=180,
        error="broadcast_title_invalid",
    )
    clean_body = _clean_text(
        body,
        minimum=5,
        maximum=3000,
        error="broadcast_body_invalid",
    )
    clean_link = str(link_url or "").strip()[:500] or None
    if clean_link and not clean_link.startswith(("https://", "http://")):
        raise ValueError("broadcast_link_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            INSERT INTO admin_broadcasts(
                guild_id, author_id, author_display, kind, title, body,
                link_url, status, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'draft', ?)
            """,
            (
                int(guild_id),
                int(author_id),
                str(author_display or "")[:200] or None,
                clean_kind,
                clean_title,
                clean_body,
                clean_link,
                now,
            ),
        )
        row = con.execute(
            "SELECT * FROM admin_broadcasts WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
        con.commit()
    if row is None:  # pragma: no cover - insert/select is one transaction
        raise RuntimeError("broadcast_create_failed")
    return dict(row)


def get_broadcast(broadcast_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM admin_broadcasts WHERE id = ?",
            (int(broadcast_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def activate_broadcast(
    broadcast_id: int,
    *,
    author_id: int,
    recipients: Iterable[tuple[int, str | None]],
    delivery_topic: str,
) -> tuple[dict[str, Any], bool]:
    unique: dict[int, str | None] = {}
    for user_id, display in recipients:
        clean_id = int(user_id)
        if clean_id > 0:
            unique[clean_id] = str(display or "")[:200] or None
    if not unique:
        raise ValueError("broadcast_recipients_empty")
    if len(unique) > 5000:
        raise ValueError("broadcast_recipients_too_many")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM admin_broadcasts WHERE id = ?",
            (int(broadcast_id),),
        ).fetchone()
        if row is None or int(row["author_id"]) != int(author_id):
            con.rollback()
            raise ValueError("broadcast_not_owned")
        broadcast = dict(row)
        if str(broadcast["status"]) != "draft":
            con.commit()
            return broadcast, False
        for user_id, display in sorted(unique.items()):
            con.execute(
                """
                INSERT OR IGNORE INTO admin_broadcast_recipients(
                    broadcast_id, user_id, user_display, status, updated_at
                ) VALUES(?, ?, ?, 'queued', ?)
                """,
                (int(broadcast_id), user_id, display, now),
            )
            outbox = delivery_outbox_enqueue_in_connection(
                con,
                topic=str(delivery_topic),
                dedupe_key=f"admin-broadcast:{int(broadcast_id)}:{user_id}",
                payload={
                    "payload_version": 1,
                    "broadcast_id": int(broadcast_id),
                    "guild_id": int(broadcast["guild_id"]),
                    "user_id": user_id,
                    "kind": str(broadcast["kind"]),
                    "title": str(broadcast["title"]),
                    "body": str(broadcast["body"]),
                    "link_url": broadcast.get("link_url"),
                    "author_display": broadcast.get("author_display"),
                },
                max_attempts=12,
                priority=20 if str(broadcast["kind"]) == "consensus" else 10,
                now=now,
            )
            con.execute(
                """
                UPDATE admin_broadcast_recipients
                SET outbox_id = ?, updated_at = ?
                WHERE broadcast_id = ? AND user_id = ?
                """,
                (int(outbox["id"]), now, int(broadcast_id), user_id),
            )
        con.execute(
            """
            UPDATE admin_broadcasts
            SET status = 'queued', recipient_count = ?, queued_at = ?
            WHERE id = ? AND status = 'draft'
            """,
            (len(unique), now, int(broadcast_id)),
        )
        updated = con.execute(
            "SELECT * FROM admin_broadcasts WHERE id = ?",
            (int(broadcast_id),),
        ).fetchone()
        con.commit()
    if updated is None:  # pragma: no cover
        raise RuntimeError("broadcast_activation_failed")
    return dict(updated), True


def get_broadcast_recipient(
    broadcast_id: int,
    user_id: int,
) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT * FROM admin_broadcast_recipients
            WHERE broadcast_id = ? AND user_id = ?
            """,
            (int(broadcast_id), int(user_id)),
        ).fetchone()
    return dict(row) if row is not None else None


def mark_broadcast_recipient(
    broadcast_id: int,
    user_id: int,
    *,
    status: str,
    reason: str | None = None,
    dm_message_id: int | None = None,
    available_at: str | None = None,
) -> dict[str, Any]:
    clean_status = str(status or "").strip().lower()
    if clean_status not in {
        "queued",
        "deferred",
        "delivered",
        "skipped",
        "unavailable",
    }:
        raise ValueError("broadcast_recipient_status_invalid")
    now = utc_now_iso()
    delivered_at = now if clean_status == "delivered" else None
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        changed = con.execute(
            """
            UPDATE admin_broadcast_recipients
            SET status = ?, reason = ?, dm_message_id = COALESCE(?, dm_message_id),
                available_at = ?, delivered_at = COALESCE(?, delivered_at),
                updated_at = ?
            WHERE broadcast_id = ? AND user_id = ?
            """,
            (
                clean_status,
                str(reason or "")[:300] or None,
                int(dm_message_id) if dm_message_id else None,
                str(available_at or "") or None,
                delivered_at,
                now,
                int(broadcast_id),
                int(user_id),
            ),
        )
        if changed.rowcount != 1:
            con.rollback()
            raise ValueError("broadcast_recipient_missing")
        open_count = int(
            con.execute(
                """
                SELECT COUNT(*) AS n FROM admin_broadcast_recipients
                WHERE broadcast_id = ? AND status NOT IN ('delivered', 'skipped', 'unavailable')
                """,
                (int(broadcast_id),),
            ).fetchone()["n"]
        )
        if open_count == 0:
            con.execute(
                """
                UPDATE admin_broadcasts
                SET status = 'completed', completed_at = COALESCE(completed_at, ?)
                WHERE id = ?
                """,
                (now, int(broadcast_id)),
            )
        row = con.execute(
            """
            SELECT * FROM admin_broadcast_recipients
            WHERE broadcast_id = ? AND user_id = ?
            """,
            (int(broadcast_id), int(user_id)),
        ).fetchone()
        con.commit()
    return dict(row)


def broadcast_report(
    *,
    broadcast_id: int | None = None,
    guild_id: int | None = None,
) -> dict[str, Any] | None:
    if broadcast_id is None and guild_id is None:
        raise ValueError("broadcast_report_scope_required")
    with _db_lock, connect() as con:
        if broadcast_id is not None:
            row = con.execute(
                "SELECT * FROM admin_broadcasts WHERE id = ?",
                (int(broadcast_id),),
            ).fetchone()
        else:
            row = con.execute(
                """
                SELECT * FROM admin_broadcasts
                WHERE guild_id = ? AND status != 'draft'
                ORDER BY id DESC LIMIT 1
                """,
                (int(guild_id or 0),),
            ).fetchone()
        if row is None:
            return None
        broadcast = dict(row)
        recipients = con.execute(
            """
            SELECT recipient.status, outbox.status AS outbox_status
            FROM admin_broadcast_recipients AS recipient
            LEFT JOIN delivery_outbox AS outbox ON outbox.id = recipient.outbox_id
            WHERE recipient.broadcast_id = ?
            """,
            (int(broadcast["id"]),),
        ).fetchall()
    counts = Counter(str(item["status"]) for item in recipients)
    counts["failed"] = sum(
        str(item["outbox_status"] or "") == "dead"
        and str(item["status"]) not in TERMINAL_RECIPIENT_STATUSES
        for item in recipients
    )
    broadcast["counts"] = dict(counts)
    return broadcast


__all__ = [
    "BROADCAST_KINDS",
    "TERMINAL_RECIPIENT_STATUSES",
    "activate_broadcast",
    "broadcast_report",
    "create_broadcast_draft",
    "get_broadcast",
    "get_broadcast_recipient",
    "mark_broadcast_recipient",
]
