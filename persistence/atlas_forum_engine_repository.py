"""Durable state for Atlas Forum Engine complaint monitoring."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


_THREAD_ID_RE = re.compile(r"\.(\d+)(?:/)?$")
_OPEN_DELIVERY_STATUSES = ("pending", "retry")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decoded(value: object, default: Any) -> Any:
    try:
        result = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return result


def _row(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("locked", "notifications_armed"):
        if key in result:
            result[key] = bool(result[key])
    for key in ("metadata_json", "last_stats_json"):
        if key in result:
            result[key.removesuffix("_json")] = _decoded(result.pop(key), {})
    return result


def forum_thread_id(url: str) -> str:
    path = urlsplit(str(url or "")).path.rstrip("/")
    match = _THREAD_ID_RE.search(path)
    if not match:
        raise ValueError("atlas_forum_complaint_thread_invalid")
    return match.group(1)


def ensure_default_monitor_feeds(guild_id: int) -> list[dict[str, Any]]:
    defaults = (
        (
            "phoenix-player-complaints-open",
            "open",
            "https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov.1253/",
            45,
        ),
        (
            "phoenix-player-complaints-accepted",
            "accepted",
            "https://forum.majestic-rp.ru/forums/rassmotrennyye-zhaloby.1254/",
            45,
        ),
        (
            "phoenix-player-complaints-rejected",
            "rejected",
            "https://forum.majestic-rp.ru/forums/otklonennyye-zhaloby.1255/",
            45,
        ),
    )
    now = utc_now_iso()
    with _db_lock, connect() as con:
        for feed_key, section_kind, root_url, interval_seconds in defaults:
            con.execute(
                """
                INSERT INTO atlas_forum_monitor_feeds(
                    guild_id, project_code, server_code, feed_key,
                    section_kind, root_url, interval_seconds, hot_pages,
                    full_pages, status, next_scan_at, created_at, updated_at
                ) VALUES(?, 'majestic-rp', 'phoenix-15', ?, ?, ?, ?, 1, 300,
                         'pending', ?, ?, ?)
                ON CONFLICT(guild_id, feed_key) DO UPDATE SET
                    root_url = excluded.root_url,
                    section_kind = excluded.section_kind,
                    interval_seconds = excluded.interval_seconds,
                    hot_pages = excluded.hot_pages,
                    updated_at = excluded.updated_at
                """,
                (
                    int(guild_id), feed_key, section_kind, root_url,
                    int(interval_seconds), now, now, now,
                ),
            )
        rows = con.execute(
            """
            SELECT * FROM atlas_forum_monitor_feeds
            WHERE guild_id = ? AND server_code = 'phoenix-15'
            ORDER BY CASE section_kind WHEN 'open' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END, id
            """,
            (int(guild_id),),
        ).fetchall()
        con.commit()
    return [_row(item) for item in rows if item is not None]


def due_monitor_feeds(guild_id: int, *, force: bool = False) -> list[dict[str, Any]]:
    now = utc_now_iso()
    stale = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT * FROM atlas_forum_monitor_feeds
            WHERE guild_id = ? AND status != 'disabled'
              AND (status != 'running' OR last_started_at IS NULL OR last_started_at <= ?)
              AND (? = 1 OR next_scan_at IS NULL OR next_scan_at <= ?)
            ORDER BY CASE section_kind WHEN 'open' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END, id
            """,
            (int(guild_id), stale, 1 if force else 0, now),
        ).fetchall()
    return [_row(item) for item in rows if item is not None]


def claim_monitor_feed(feed_id: int) -> dict[str, Any] | None:
    now = utc_now_iso()
    stale = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            UPDATE atlas_forum_monitor_feeds
            SET status = 'running', last_started_at = ?, updated_at = ?
            WHERE id = ? AND status != 'disabled'
              AND (status != 'running' OR last_started_at IS NULL OR last_started_at <= ?)
            """,
            (now, now, int(feed_id), stale),
        )
        row = con.execute(
            "SELECT * FROM atlas_forum_monitor_feeds WHERE id = ?",
            (int(feed_id),),
        ).fetchone()
        con.commit()
    return _row(row) if int(cursor.rowcount or 0) else None


def finish_monitor_feed(
    feed_id: int,
    *,
    stats: dict[str, Any],
    error: str | None = None,
    attention: bool = False,
    full_scan: bool = False,
    baseline_completed: bool = False,
) -> dict[str, Any]:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with _db_lock, connect() as con:
        feed = con.execute(
            "SELECT * FROM atlas_forum_monitor_feeds WHERE id = ?",
            (int(feed_id),),
        ).fetchone()
        if feed is None:
            raise ValueError("atlas_forum_monitor_feed_missing")
        interval = max(15, int(feed["interval_seconds"] or 45))
        if error:
            interval = min(interval, 45)
        next_scan = (now_dt + timedelta(seconds=interval)).isoformat()
        status = "attention" if attention else ("error" if error else "ok")
        con.execute(
            """
            UPDATE atlas_forum_monitor_feeds
            SET status = ?,
                baseline_completed_at = CASE
                    WHEN ? = 1 AND ? IS NULL THEN ? ELSE baseline_completed_at END,
                last_full_scan_at = CASE WHEN ? = 1 AND ? IS NULL THEN ? ELSE last_full_scan_at END,
                last_success_at = CASE WHEN ? IS NULL THEN ? ELSE last_success_at END,
                next_scan_at = ?, last_error = ?, last_stats_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                1 if (baseline_completed or full_scan) and not error else 0,
                feed["baseline_completed_at"],
                now,
                1 if full_scan else 0,
                error,
                now,
                error,
                now,
                next_scan,
                str(error or "")[:2000] or None,
                _json(stats),
                now,
                int(feed_id),
            ),
        )
        row = con.execute(
            "SELECT * FROM atlas_forum_monitor_feeds WHERE id = ?",
            (int(feed_id),),
        ).fetchone()
        con.commit()
    return _row(row) or {}


def list_monitored_characters(guild_id: int) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT character.id AS character_id, character.user_id,
                   character.nickname, character.static_id,
                   COALESCE(profile.dm_notifications, 1) AS dm_notifications
            FROM profile_characters AS character
            LEFT JOIN member_profiles AS profile
              ON profile.guild_id = character.guild_id
             AND profile.user_id = character.user_id
            WHERE character.guild_id = ?
              AND TRIM(character.static_id) != ''
            ORDER BY character.user_id, character.position, character.id
            """,
            (int(guild_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def complaint_by_thread(
    guild_id: int,
    project_code: str,
    server_code: str,
    thread_url: str,
) -> dict[str, Any] | None:
    thread_id = forum_thread_id(thread_url)
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT * FROM atlas_forum_complaints
            WHERE guild_id = ? AND project_code = ? AND server_code = ? AND thread_id = ?
            """,
            (int(guild_id), str(project_code), str(server_code), thread_id),
        ).fetchone()
    return _row(row)


def record_complaint_listing(
    *,
    guild_id: int,
    project_code: str,
    server_code: str,
    section_kind: str,
    thread_url: str,
    title: str,
    author: str | None,
    listing_fingerprint: str,
    locked: bool,
    metadata: dict[str, Any],
    notify: bool,
) -> dict[str, Any]:
    now = utc_now_iso()
    thread_id = forum_thread_id(thread_url)
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            """
            SELECT * FROM atlas_forum_complaints
            WHERE guild_id = ? AND project_code = ? AND server_code = ? AND thread_id = ?
            """,
            (int(guild_id), project_code, server_code, thread_id),
        ).fetchone()
        created = existing is None
        previous_section = str(existing["section_kind"]) if existing else None
        listing_changed = created or str(existing["listing_fingerprint"]) != listing_fingerprint
        section_changed = bool(existing is not None and previous_section != section_kind)
        if existing is None:
            cursor = con.execute(
                """
                INSERT INTO atlas_forum_complaints(
                    guild_id, project_code, server_code, thread_id, thread_url,
                    title, author, section_kind, listing_fingerprint, locked,
                    notifications_armed,
                    first_seen_at, last_seen_at, last_changed_at, metadata_json,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(guild_id), project_code, server_code, thread_id, thread_url,
                    str(title)[:240], str(author or "")[:120] or None, section_kind,
                    listing_fingerprint, 1 if locked else 0, 1 if notify else 0,
                    now, now, now,
                    _json(metadata), now, now,
                ),
            )
            complaint_id = int(cursor.lastrowid)
        else:
            complaint_id = int(existing["id"])
            con.execute(
                """
                UPDATE atlas_forum_complaints
                SET thread_url = ?, title = ?, author = COALESCE(?, author),
                    section_kind = ?, listing_fingerprint = ?, locked = ?,
                    last_seen_at = ?,
                    last_changed_at = CASE WHEN ? = 1 THEN ? ELSE last_changed_at END,
                    metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    thread_url, str(title)[:240], str(author or "")[:120] or None,
                    section_kind, listing_fingerprint, 1 if locked else 0, now,
                    1 if listing_changed or section_changed else 0, now,
                    _json({**_decoded(existing["metadata_json"], {}), **metadata}),
                    now, complaint_id,
                ),
            )
        event_id = None
        notifications_armed = bool(
            notify if existing is None else existing["notifications_armed"]
        )
        if section_changed:
            event_fingerprint = hashlib.sha256(
                f"status\0{previous_section}\0{section_kind}".encode("utf-8")
            ).hexdigest()
            con.execute(
                """
                INSERT OR IGNORE INTO atlas_forum_complaint_events(
                    complaint_id, event_kind, event_fingerprint,
                    previous_section_kind, section_kind, excerpt, created_at
                ) VALUES(?, 'status_changed', ?, ?, ?, ?, ?)
                """,
                (
                    complaint_id, event_fingerprint, previous_section, section_kind,
                    f"Статус жалобы изменён: {previous_section} → {section_kind}", now,
                ),
            )
            event = con.execute(
                """
                SELECT id FROM atlas_forum_complaint_events
                WHERE complaint_id = ? AND event_kind = 'status_changed' AND event_fingerprint = ?
                """,
                (complaint_id, event_fingerprint),
            ).fetchone()
            event_id = int(event["id"]) if event else None
            if event_id is not None and notify and notifications_armed:
                _enqueue_subject_deliveries(con, complaint_id, event_id, now)
        row = con.execute(
            "SELECT * FROM atlas_forum_complaints WHERE id = ?",
            (complaint_id,),
        ).fetchone()
        con.commit()
    return {
        "complaint": _row(row),
        "created": created,
        "listing_changed": listing_changed,
        "section_changed": section_changed,
        "event_id": event_id,
    }


def _enqueue_subject_deliveries(con: Any, complaint_id: int, event_id: int, now: str) -> int:
    cursor = con.execute(
        """
        INSERT OR IGNORE INTO atlas_forum_complaint_deliveries(
            event_id, complaint_id, guild_id, user_id, static_id,
            status, next_attempt_at, created_at, updated_at
        )
        SELECT ?, subject.complaint_id, subject.guild_id, subject.user_id,
               subject.static_id, 'pending', ?, ?, ?
        FROM atlas_forum_complaint_subjects AS subject
        WHERE subject.complaint_id = ?
        """,
        (int(event_id), now, now, now, int(complaint_id)),
    )
    return int(cursor.rowcount or 0)


def record_complaint_snapshot(
    complaint_id: int,
    *,
    content_fingerprint: str,
    first_post_excerpt: str,
    latest_post_excerpt: str,
    latest_post_author: str | None,
    latest_post_role: str | None,
    latest_post_at: str | None,
    latest_post_is_staff: bool,
    post_count: int,
    matched_characters: Iterable[dict[str, Any]],
    notify: bool,
) -> dict[str, Any]:
    now = utc_now_iso()
    characters = list(matched_characters)
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT * FROM atlas_forum_complaints WHERE id = ?",
            (int(complaint_id),),
        ).fetchone()
        if existing is None:
            raise ValueError("atlas_forum_complaint_missing")
        previous_fingerprint = str(existing["content_fingerprint"] or "")
        previous_posts = int(existing["post_count"] or 0)
        notifications_armed = bool(existing["notifications_armed"])
        content_changed = bool(previous_fingerprint and previous_fingerprint != content_fingerprint)
        first_hydration = not previous_fingerprint
        for character in characters:
            con.execute(
                """
                INSERT INTO atlas_forum_complaint_subjects(
                    complaint_id, guild_id, user_id, character_id, static_id,
                    nickname, first_matched_at, last_matched_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(complaint_id, user_id, static_id) DO UPDATE SET
                    character_id = excluded.character_id,
                    nickname = excluded.nickname,
                    last_matched_at = excluded.last_matched_at,
                    updated_at = excluded.updated_at
                """,
                (
                    int(complaint_id), int(existing["guild_id"]), int(character["user_id"]),
                    int(character["character_id"]) if character.get("character_id") else None,
                    str(character["static_id"]), str(character.get("nickname") or "")[:180] or None,
                    now, now, now, now,
                ),
            )
        con.execute(
            """
            UPDATE atlas_forum_complaints
            SET content_fingerprint = ?, first_post_excerpt = ?,
                latest_post_excerpt = ?, latest_post_author = ?, latest_post_role = ?,
                latest_post_at = ?, post_count = ?,
                notifications_armed = 1,
                last_changed_at = CASE WHEN ? = 1 THEN ? ELSE last_changed_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                content_fingerprint, str(first_post_excerpt)[:16000],
                str(latest_post_excerpt)[:6000], str(latest_post_author or "")[:120] or None,
                str(latest_post_role or "")[:180] or None, str(latest_post_at or "")[:100] or None,
                max(0, int(post_count)), 1 if content_changed else 0, now, now,
                int(complaint_id),
            ),
        )
        event_kind = None
        if first_hydration:
            event_kind = "discovered"
        elif content_changed:
            event_kind = (
                "staff_reply" if latest_post_is_staff and int(post_count) > previous_posts
                else "reply" if int(post_count) > previous_posts
                else "updated"
            )
        event_id = None
        if event_kind:
            event_fingerprint = hashlib.sha256(
                f"{event_kind}\0{content_fingerprint}".encode("utf-8")
            ).hexdigest()
            con.execute(
                """
                INSERT OR IGNORE INTO atlas_forum_complaint_events(
                    complaint_id, event_kind, event_fingerprint, section_kind,
                    actor, actor_role, excerpt, occurred_at, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(complaint_id), event_kind, event_fingerprint,
                    str(existing["section_kind"]), str(latest_post_author or "")[:120] or None,
                    str(latest_post_role or "")[:180] or None,
                    str(latest_post_excerpt)[:4000] or None,
                    str(latest_post_at or "")[:100] or None, now,
                ),
            )
            event = con.execute(
                """
                SELECT id FROM atlas_forum_complaint_events
                WHERE complaint_id = ? AND event_kind = ? AND event_fingerprint = ?
                """,
                (int(complaint_id), event_kind, event_fingerprint),
            ).fetchone()
            event_id = int(event["id"]) if event else None
            if event_id is not None and notify and notifications_armed:
                _enqueue_subject_deliveries(con, int(complaint_id), event_id, now)
        con.commit()
    return {
        "event_kind": event_kind,
        "event_id": event_id,
        "content_changed": content_changed,
        "first_hydration": first_hydration,
        "matched": len(characters),
    }


def complaints_needing_hydration(guild_id: int, *, limit: int = 60) -> list[str]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT thread_url FROM atlas_forum_complaints
            WHERE guild_id = ? AND content_fingerprint IS NULL
            ORDER BY CASE section_kind WHEN 'open' THEN 0 ELSE 1 END,
                     first_seen_at DESC, id DESC
            LIMIT ?
            """,
            (int(guild_id), max(1, min(int(limit), 200))),
        ).fetchall()
    return [str(row["thread_url"]) for row in rows]


def pending_complaint_deliveries(*, limit: int = 50) -> list[dict[str, Any]]:
    now = utc_now_iso()
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT delivery.*, event.event_kind, event.actor, event.actor_role,
                   event.excerpt, event.occurred_at,
                   complaint.title, complaint.thread_url, complaint.section_kind,
                   complaint.server_code, subject.nickname,
                   COALESCE(profile.dm_notifications, 1) AS dm_notifications
            FROM atlas_forum_complaint_deliveries AS delivery
            JOIN atlas_forum_complaint_events AS event ON event.id = delivery.event_id
            JOIN atlas_forum_complaints AS complaint ON complaint.id = delivery.complaint_id
            LEFT JOIN atlas_forum_complaint_subjects AS subject
              ON subject.complaint_id = delivery.complaint_id
             AND subject.user_id = delivery.user_id
             AND subject.static_id = delivery.static_id
            LEFT JOIN member_profiles AS profile
              ON profile.guild_id = delivery.guild_id
             AND profile.user_id = delivery.user_id
            WHERE delivery.status IN ('pending', 'retry')
              AND delivery.next_attempt_at <= ?
            ORDER BY delivery.next_attempt_at, delivery.id
            LIMIT ?
            """,
            (now, max(1, min(int(limit), 200))),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_complaint_delivery(
    delivery_id: int,
    *,
    sent: bool,
    dm_message_id: int | None = None,
    error: str | None = None,
) -> None:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT attempts FROM atlas_forum_complaint_deliveries WHERE id = ?",
            (int(delivery_id),),
        ).fetchone()
        if row is None:
            return
        attempts = int(row["attempts"] or 0) + 1
        if sent:
            status = "sent"
            next_attempt = now
        elif attempts >= 8:
            status = "dead"
            next_attempt = now
        else:
            status = "retry"
            delay = min(21_600, 60 * (2 ** min(attempts - 1, 8)))
            next_attempt = (now_dt + timedelta(seconds=delay)).isoformat()
        con.execute(
            """
            UPDATE atlas_forum_complaint_deliveries
            SET status = ?, attempts = ?, next_attempt_at = ?, dm_message_id = ?,
                last_error = ?, delivered_at = CASE WHEN ? = 1 THEN ? ELSE delivered_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status, attempts, next_attempt,
                int(dm_message_id) if dm_message_id else None,
                None if sent else str(error or "delivery_failed")[:1000],
                1 if sent else 0, now, now, int(delivery_id),
            ),
        )
        con.commit()


def suppress_complaint_delivery(delivery_id: int, *, reason: str) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE atlas_forum_complaint_deliveries
            SET status = 'suppressed', last_error = ?, delivered_at = ?, updated_at = ?
            WHERE id = ? AND status IN ('pending', 'retry')
            """,
            (str(reason or "notifications_disabled")[:1000], now, now, int(delivery_id)),
        )
        con.commit()


def complaint_identities(guild_id: int, *, limit: int = 10_000) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT id, title, first_post_excerpt
            FROM atlas_forum_complaints
            WHERE guild_id = ? AND content_fingerprint IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(guild_id), max(1, min(int(limit), 25_000))),
        ).fetchall()
    return [dict(row) for row in rows]


def reconcile_complaint_subjects(
    complaint_id: int,
    matched_characters: Iterable[dict[str, Any]],
) -> int:
    now = utc_now_iso()
    characters = list(matched_characters)
    with _db_lock, connect() as con:
        complaint = con.execute(
            "SELECT guild_id FROM atlas_forum_complaints WHERE id = ?",
            (int(complaint_id),),
        ).fetchone()
        if complaint is None:
            return 0
        changed = 0
        for character in characters:
            cursor = con.execute(
                """
                INSERT INTO atlas_forum_complaint_subjects(
                    complaint_id, guild_id, user_id, character_id, static_id,
                    nickname, first_matched_at, last_matched_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(complaint_id, user_id, static_id) DO UPDATE SET
                    character_id = excluded.character_id,
                    nickname = excluded.nickname,
                    last_matched_at = excluded.last_matched_at,
                    updated_at = excluded.updated_at
                """,
                (
                    int(complaint_id), int(complaint["guild_id"]), int(character["user_id"]),
                    int(character["character_id"]) if character.get("character_id") else None,
                    str(character["static_id"]), str(character.get("nickname") or "")[:180] or None,
                    now, now, now, now,
                ),
            )
            changed += max(0, int(cursor.rowcount or 0))
        con.commit()
    return int(changed)


def user_forum_complaints(
    guild_id: int,
    user_id: int,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT complaint.*, subject.static_id, subject.nickname,
                   (SELECT COUNT(*) FROM atlas_forum_complaint_events AS event
                    WHERE event.complaint_id = complaint.id) AS event_count
            FROM atlas_forum_complaint_subjects AS subject
            JOIN atlas_forum_complaints AS complaint ON complaint.id = subject.complaint_id
            WHERE subject.guild_id = ? AND subject.user_id = ?
            ORDER BY complaint.last_changed_at DESC, complaint.id DESC
            LIMIT ?
            """,
            (int(guild_id), int(user_id), max(1, min(int(limit), 200))),
        ).fetchall()
    return [_row(item) for item in rows if item is not None]


def forum_monitor_status(guild_id: int) -> dict[str, Any]:
    with connect_readonly() as con:
        feeds = con.execute(
            "SELECT * FROM atlas_forum_monitor_feeds WHERE guild_id = ? ORDER BY id",
            (int(guild_id),),
        ).fetchall()
        totals = con.execute(
            """
            SELECT COUNT(*) AS complaints,
                   SUM(CASE WHEN section_kind = 'open' THEN 1 ELSE 0 END) AS open_count,
                   SUM(CASE WHEN content_fingerprint IS NULL THEN 1 ELSE 0 END) AS pending_hydration
            FROM atlas_forum_complaints WHERE guild_id = ?
            """,
            (int(guild_id),),
        ).fetchone()
    return {
        "feeds": [_row(item) for item in feeds if item is not None],
        "complaints": int(totals["complaints"] or 0) if totals else 0,
        "open": int(totals["open_count"] or 0) if totals else 0,
        "pending_hydration": int(totals["pending_hydration"] or 0) if totals else 0,
    }


__all__ = [
    "claim_monitor_feed",
    "complaint_identities",
    "complaint_by_thread",
    "complaints_needing_hydration",
    "due_monitor_feeds",
    "ensure_default_monitor_feeds",
    "finish_monitor_feed",
    "forum_monitor_status",
    "forum_thread_id",
    "list_monitored_characters",
    "mark_complaint_delivery",
    "pending_complaint_deliveries",
    "record_complaint_listing",
    "record_complaint_snapshot",
    "reconcile_complaint_subjects",
    "suppress_complaint_delivery",
    "user_forum_complaints",
]
