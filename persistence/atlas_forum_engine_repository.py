"""Durable state for Atlas Forum Engine complaint monitoring."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


_THREAD_ID_RE = re.compile(r"/threads/(?:[^/]*\.)?(\d+)(?:/)?$", re.IGNORECASE)
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
            30,
        ),
        (
            "phoenix-player-complaints-accepted",
            "accepted",
            "https://forum.majestic-rp.ru/forums/rassmotrennyye-zhaloby.1254/",
            30,
        ),
        (
            "phoenix-player-complaints-rejected",
            "rejected",
            "https://forum.majestic-rp.ru/forums/otklonennyye-zhaloby.1255/",
            30,
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
                    full_pages, backfill_cursor_url, status, next_scan_at,
                    created_at, updated_at
                ) VALUES(?, 'majestic-rp', 'phoenix-15', ?, ?, ?, ?, 1, 300,
                         ?, 'pending', ?, ?, ?)
                ON CONFLICT(guild_id, feed_key) DO UPDATE SET
                    root_url = excluded.root_url,
                    section_kind = excluded.section_kind,
                    interval_seconds = excluded.interval_seconds,
                    hot_pages = excluded.hot_pages,
                    backfill_cursor_url = COALESCE(
                        atlas_forum_monitor_feeds.backfill_cursor_url,
                        excluded.backfill_cursor_url
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    int(guild_id), feed_key, section_kind, root_url,
                    int(interval_seconds), root_url, now, now, now,
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


def recover_interrupted_monitor_feeds(guild_id: int) -> int:
    """Release leases left behind by the previous web runtime.

    Only one Forum Engine is hosted by the Discord/web application process.
    Consequently a ``running`` lease still present during that process' next
    startup is necessarily interrupted work, not a live competing scan.
    """

    now = utc_now_iso()
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            UPDATE atlas_forum_monitor_feeds
            SET status = 'pending', next_scan_at = ?,
                last_error = NULL, updated_at = ?
            WHERE guild_id = ? AND status = 'running'
            """,
            (now, now, int(guild_id)),
        )
        con.commit()
    return max(0, int(cursor.rowcount or 0))


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
            interval = min(interval, 30)
        next_scan = (now_dt + timedelta(seconds=interval)).isoformat()
        status = "attention" if attention else ("error" if error else "ok")
        failure_count = int(feed["failure_count"] or 0) + 1 if error else 0
        # Do not put untyped placeholders inside ``CASE ... IS NULL`` here.
        # SQLite accepts that construct, but PostgreSQL cannot infer the type
        # of the timestamp placeholder when the compared value is NULL and
        # rejects every completed pass with IndeterminateDatatype.  Resolve
        # the conditional values in Python, then assign them directly so the
        # target columns provide PostgreSQL with an unambiguous type.
        baseline_completed_at = feed["baseline_completed_at"]
        if (baseline_completed or full_scan) and not error and baseline_completed_at is None:
            baseline_completed_at = now
        last_full_scan_at = feed["last_full_scan_at"]
        if full_scan and not error:
            last_full_scan_at = now
        last_success_at = feed["last_success_at"]
        if error is None:
            last_success_at = now
        con.execute(
            """
            UPDATE atlas_forum_monitor_feeds
            SET status = ?,
                baseline_completed_at = ?,
                last_full_scan_at = ?,
                last_success_at = ?,
                next_scan_at = ?, last_error = ?, failure_count = ?,
                last_stats_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                baseline_completed_at,
                last_full_scan_at,
                last_success_at,
                next_scan,
                str(error or "")[:2000] or None,
                failure_count,
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


def advance_monitor_backfill(
    feed_id: int,
    *,
    next_url: str | None,
    pages_scanned: int,
    topics_seen: int,
) -> dict[str, Any]:
    """Persist one bounded archive window so a restart loses no progress."""

    now = utc_now_iso()
    with _db_lock, connect() as con:
        feed = con.execute(
            "SELECT * FROM atlas_forum_monitor_feeds WHERE id = ?",
            (int(feed_id),),
        ).fetchone()
        if feed is None:
            raise ValueError("atlas_forum_monitor_feed_missing")
        completed_at = feed["backfill_completed_at"]
        if not next_url and completed_at is None:
            completed_at = now
        con.execute(
            """
            UPDATE atlas_forum_monitor_feeds
            SET backfill_cursor_url = ?,
                backfill_pages_scanned = backfill_pages_scanned + ?,
                backfill_topics_seen = backfill_topics_seen + ?,
                backfill_completed_at = ?, last_backfill_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                str(next_url or "")[:1000] or None,
                max(0, int(pages_scanned)),
                max(0, int(topics_seen)),
                completed_at,
                now,
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
    posts: Iterable[dict[str, Any]] = (),
    participant_statics: dict[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    now = utc_now_iso()
    characters = list(matched_characters)
    snapshot_posts = [dict(item) for item in posts if isinstance(item, dict)]
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
        matched_keys = {
            (int(character["user_id"]), str(character["static_id"]))
            for character in characters
        }
        existing_subjects = con.execute(
            "SELECT id, user_id, static_id FROM atlas_forum_complaint_subjects WHERE complaint_id = ?",
            (int(complaint_id),),
        ).fetchall()
        for subject in existing_subjects:
            if (int(subject["user_id"]), str(subject["static_id"])) not in matched_keys:
                con.execute(
                    "DELETE FROM atlas_forum_complaint_subjects WHERE id = ?",
                    (int(subject["id"]),),
                )
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
                hydration_attempts = 0, hydration_next_attempt_at = NULL,
                hydration_last_error = NULL,
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
        for position, post in enumerate(snapshot_posts, start=1):
            try:
                post_index = max(1, int(post.get("index") or position))
            except (TypeError, ValueError):
                post_index = position
            content = str(post.get("content") or "").strip()
            if not content:
                continue
            post_fingerprint = hashlib.sha256(
                "\0".join(
                    (
                        str(post_index),
                        str(post.get("author") or ""),
                        str(post.get("posted_at") or ""),
                        content,
                    )
                ).encode("utf-8")
            ).hexdigest()
            con.execute(
                """
                INSERT INTO atlas_forum_complaint_posts(
                    complaint_id, post_index, author, author_role, posted_at,
                    content, is_staff, content_fingerprint, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(complaint_id, post_index) DO UPDATE SET
                    author = excluded.author,
                    author_role = excluded.author_role,
                    posted_at = excluded.posted_at,
                    content = excluded.content,
                    is_staff = excluded.is_staff,
                    content_fingerprint = excluded.content_fingerprint,
                    updated_at = excluded.updated_at
                """,
                (
                    int(complaint_id),
                    post_index,
                    str(post.get("author") or "")[:120] or None,
                    str(post.get("author_role") or "")[:180] or None,
                    str(post.get("posted_at") or "")[:100] or None,
                    content,
                    1 if post.get("is_staff") else 0,
                    post_fingerprint,
                    now,
                    now,
                ),
            )
        if participant_statics is not None:
            previous_participants = {
                (str(row["participant_role"]), str(row["static_id"])):
                    str(row["first_seen_at"] or now)
                for row in con.execute(
                    """
                    SELECT participant_role, static_id, first_seen_at
                    FROM atlas_forum_complaint_participants
                    WHERE complaint_id = ?
                    """,
                    (int(complaint_id),),
                ).fetchall()
            }
            con.execute(
                "DELETE FROM atlas_forum_complaint_participants WHERE complaint_id = ?",
                (int(complaint_id),),
            )
            for participant_role in ("reporter", "target"):
                values = participant_statics.get(participant_role, ())
                statics = {
                    re.sub(r"\D", "", str(value or ""))
                    for value in values
                }
                for static_id in sorted(item for item in statics if item):
                    con.execute(
                        """
                        INSERT INTO atlas_forum_complaint_participants(
                            complaint_id, participant_role, static_id,
                            first_seen_at, last_seen_at, created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(complaint_id, participant_role, static_id) DO UPDATE SET
                            last_seen_at = excluded.last_seen_at,
                            updated_at = excluded.updated_at
                        """,
                        (
                            int(complaint_id), participant_role, static_id,
                            previous_participants.get(
                                (participant_role, static_id), now
                            ),
                            now, now, now,
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
    now = utc_now_iso()
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT thread_url FROM atlas_forum_complaints
            WHERE guild_id = ? AND content_fingerprint IS NULL
              AND (hydration_next_attempt_at IS NULL OR hydration_next_attempt_at <= ?)
            ORDER BY CASE section_kind WHEN 'open' THEN 0 ELSE 1 END,
                     first_seen_at DESC, id DESC
            LIMIT ?
            """,
            (int(guild_id), now, max(1, min(int(limit), 200))),
        ).fetchall()
    return [str(row["thread_url"]) for row in rows]


def mark_complaint_hydration_failed(
    guild_id: int,
    thread_urls: Iterable[str],
    *,
    error: str = "forum_thread_unavailable",
) -> int:
    """Back off unreadable archive topics without blocking the whole feed."""

    urls = list(dict.fromkeys(str(item or "").strip() for item in thread_urls if item))
    if not urls:
        return 0
    now_dt = datetime.now(timezone.utc)
    changed = 0
    with _db_lock, connect() as con:
        for url in urls[:500]:
            try:
                thread_id = forum_thread_id(url)
            except ValueError:
                continue
            row = con.execute(
                """
                SELECT id, hydration_attempts FROM atlas_forum_complaints
                WHERE guild_id = ? AND thread_id = ?
                """,
                (int(guild_id), thread_id),
            ).fetchone()
            if row is None:
                continue
            attempts = int(row["hydration_attempts"] or 0) + 1
            delay = min(86_400, 300 * (2 ** min(attempts - 1, 8)))
            next_attempt = (now_dt + timedelta(seconds=delay)).isoformat()
            cursor = con.execute(
                """
                UPDATE atlas_forum_complaints
                SET hydration_attempts = ?, hydration_next_attempt_at = ?,
                    hydration_last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    attempts,
                    next_attempt,
                    str(error or "forum_thread_unavailable")[:1000],
                    now_dt.isoformat(),
                    int(row["id"]),
                ),
            )
            changed += max(0, int(cursor.rowcount or 0))
        con.commit()
    return changed


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
        matched_keys = {
            (int(character["user_id"]), str(character["static_id"]))
            for character in characters
        }
        existing_subjects = con.execute(
            "SELECT id, user_id, static_id FROM atlas_forum_complaint_subjects WHERE complaint_id = ?",
            (int(complaint_id),),
        ).fetchall()
        for subject in existing_subjects:
            if (int(subject["user_id"]), str(subject["static_id"])) not in matched_keys:
                con.execute(
                    "DELETE FROM atlas_forum_complaint_subjects WHERE id = ?",
                    (int(subject["id"]),),
                )
                changed += 1
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
                   SUM(CASE WHEN content_fingerprint IS NULL THEN 1 ELSE 0 END) AS pending_hydration,
                   SUM(CASE WHEN content_fingerprint IS NOT NULL THEN 1 ELSE 0 END) AS hydrated
            FROM atlas_forum_complaints WHERE guild_id = ?
            """,
            (int(guild_id),),
        ).fetchone()
        post_total = con.execute(
            """
            SELECT COUNT(*) AS post_count
            FROM atlas_forum_complaint_posts AS post
            JOIN atlas_forum_complaints AS complaint ON complaint.id = post.complaint_id
            WHERE complaint.guild_id = ?
            """,
            (int(guild_id),),
        ).fetchone()
    return {
        "feeds": [_row(item) for item in feeds if item is not None],
        "complaints": int(totals["complaints"] or 0) if totals else 0,
        "open": int(totals["open_count"] or 0) if totals else 0,
        "pending_hydration": int(totals["pending_hydration"] or 0) if totals else 0,
        "hydrated": int(totals["hydrated"] or 0) if totals else 0,
        "posts": int(post_total["post_count"] or 0) if post_total else 0,
    }


def forum_static_profile(
    guild_id: int,
    static_id: str,
    *,
    limit: int = 30,
) -> dict[str, Any]:
    """Return Forum Eye-style filed/received history for one public static."""

    normalized = re.sub(r"\D", "", str(static_id or ""))
    if not normalized or len(normalized) > 12:
        raise ValueError("atlas_forum_static_invalid")
    with connect_readonly() as con:
        counts = con.execute(
            """
            SELECT participant.participant_role, COUNT(DISTINCT participant.complaint_id) AS amount
            FROM atlas_forum_complaint_participants AS participant
            JOIN atlas_forum_complaints AS complaint ON complaint.id = participant.complaint_id
            WHERE complaint.guild_id = ? AND participant.static_id = ?
            GROUP BY participant.participant_role
            """,
            (int(guild_id), normalized),
        ).fetchall()
        rows = con.execute(
            """
            SELECT complaint.id, complaint.thread_url, complaint.title,
                   complaint.author, complaint.section_kind, complaint.post_count,
                   complaint.last_changed_at, complaint.latest_post_at,
                   complaint.latest_post_author, participant.participant_role
            FROM atlas_forum_complaint_participants AS participant
            JOIN atlas_forum_complaints AS complaint ON complaint.id = participant.complaint_id
            WHERE complaint.guild_id = ? AND participant.static_id = ?
            ORDER BY complaint.last_changed_at DESC, complaint.id DESC
            LIMIT ?
            """,
            (int(guild_id), normalized, max(1, min(int(limit), 100))),
        ).fetchall()
    totals = {str(row["participant_role"]): int(row["amount"] or 0) for row in counts}
    return {
        "static_id": normalized,
        "filed": int(totals.get("reporter", 0)),
        "received": int(totals.get("target", 0)),
        "items": [_row(row) for row in rows if row is not None],
    }


__all__ = [
    "advance_monitor_backfill",
    "claim_monitor_feed",
    "complaint_identities",
    "complaint_by_thread",
    "complaints_needing_hydration",
    "due_monitor_feeds",
    "ensure_default_monitor_feeds",
    "finish_monitor_feed",
    "forum_monitor_status",
    "forum_static_profile",
    "forum_thread_id",
    "list_monitored_characters",
    "mark_complaint_delivery",
    "mark_complaint_hydration_failed",
    "pending_complaint_deliveries",
    "record_complaint_listing",
    "record_complaint_snapshot",
    "reconcile_complaint_subjects",
    "suppress_complaint_delivery",
    "user_forum_complaints",
]
