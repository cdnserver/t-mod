from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from persistence.core import _db_lock, connect, utc_now_iso

OUTBOX_OPEN_STATUSES = frozenset({"pending", "processing", "retry"})
CONSENSUS_TOPIC_PREFIX = "tvrs.consensus."
CONSENSUS_CONTROL_DM_TOPIC = "tvrs.consensus.control-dm.v1"


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def delivery_outbox_enqueue_in_connection(
    con: sqlite3.Connection,
    *,
    topic: str,
    dedupe_key: str,
    payload: dict[str, Any],
    max_attempts: int = 8,
    priority: int = 0,
    supersede_key: str | None = None,
    available_at: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Insert once using the caller's transaction.

    Keeping this operation connection-aware is what lets a domain write and its
    delivery intent commit atomically.  A repeated key deliberately keeps the
    first payload: callers may safely retry a transaction after an uncertain
    response without rewriting an already delivered message.
    """

    clean_topic = str(topic).strip()
    clean_key = str(dedupe_key).strip()
    if not clean_topic or not clean_key:
        raise ValueError("outbox_topic_and_dedupe_key_required")
    created_at = str(now or utc_now_iso())
    ready_at = str(available_at or created_at)
    clean_supersede_key = str(supersede_key or "").strip()[:300] or None
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    existing = con.execute(
        "SELECT * FROM delivery_outbox WHERE topic = ? AND dedupe_key = ?",
        (clean_topic, clean_key),
    ).fetchone()
    if existing is not None:
        return dict(existing)
    if clean_supersede_key is not None:
        con.execute(
            """
            UPDATE delivery_outbox
            SET status = 'cancelled', lease_owner = NULL, lease_token = NULL,
                lease_until = NULL, last_error = 'superseded_by_newer_delivery',
                payload_json = '{"compacted":true,"reason":"superseded"}',
                updated_at = ?
            WHERE topic = ? AND supersede_key = ?
              AND status IN ('pending', 'retry')
            """,
            (created_at, clean_topic, clean_supersede_key),
        )
    con.execute(
        """
        INSERT INTO delivery_outbox(
            topic, dedupe_key, payload_json, status, attempts, max_attempts,
            priority, supersede_key, available_at, lease_owner, lease_token, lease_until, message_id,
            last_error, delivered_at, created_at, updated_at
        )
        VALUES(?, ?, ?, 'pending', 0, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
        ON CONFLICT(topic, dedupe_key) DO NOTHING
        """,
        (
            clean_topic,
            clean_key,
            payload_json,
            max(1, int(max_attempts)),
            max(-1000, min(1000, int(priority))),
            clean_supersede_key,
            ready_at,
            created_at,
            created_at,
        ),
    )
    row = con.execute(
        "SELECT * FROM delivery_outbox WHERE topic = ? AND dedupe_key = ?",
        (clean_topic, clean_key),
    ).fetchone()
    if row is None:  # pragma: no cover - protected by the insert/select transaction
        raise RuntimeError("outbox_enqueue_failed")
    return dict(row)


def delivery_outbox_enqueue(
    *,
    topic: str,
    dedupe_key: str,
    payload: dict[str, Any],
    max_attempts: int = 8,
    priority: int = 0,
    supersede_key: str | None = None,
    available_at: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = delivery_outbox_enqueue_in_connection(
            con,
            topic=topic,
            dedupe_key=dedupe_key,
            payload=payload,
            max_attempts=max_attempts,
            priority=priority,
            supersede_key=supersede_key,
            available_at=available_at,
            now=now,
        )
        con.commit()
        return row


def delivery_outbox_ensure_current(
    *,
    topic: str,
    dedupe_key: str,
    payload: dict[str, Any],
    max_attempts: int = 8,
    priority: int = 0,
    supersede_key: str,
    replace_live: bool = False,
    available_at: str | None = None,
    now: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Ensure one exact delivery generation exists for a semantic scope.

    Recovery callers must supply a fresh dedupe key for every repair attempt.
    An accidental reuse returns the existing row unchanged, including terminal
    ``dead``/``cancelled`` rows; tombstones are never silently resurrected.
    By default any live generation owns the scope.  ``replace_live`` is reserved
    for a newer durable state projection: it atomically supersedes queued older
    work while a currently leased predecessor is allowed to finish behind the
    handler's generation fence.
    """

    clean_topic = str(topic).strip()
    clean_key = str(dedupe_key).strip()
    clean_supersede_key = str(supersede_key or "").strip()[:300]
    if not clean_topic or not clean_key:
        raise ValueError("outbox_topic_and_dedupe_key_required")
    if not clean_supersede_key:
        raise ValueError("outbox_supersede_key_required")
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute(
            """
            SELECT * FROM delivery_outbox
            WHERE topic = ? AND supersede_key = ?
              AND status IN ('pending', 'processing', 'retry')
            ORDER BY id DESC LIMIT 1
            """,
            (clean_topic, clean_supersede_key),
        ).fetchone()
        if current is not None:
            if (
                str(current["dedupe_key"] or "") == clean_key
                or not bool(replace_live)
            ):
                con.commit()
                return dict(current), False
        existing_key = con.execute(
            "SELECT * FROM delivery_outbox WHERE topic = ? AND dedupe_key = ?",
            (clean_topic, clean_key),
        ).fetchone()
        if existing_key is not None:
            con.commit()
            return dict(existing_key), False
        row = delivery_outbox_enqueue_in_connection(
            con,
            topic=clean_topic,
            dedupe_key=clean_key,
            payload=payload,
            max_attempts=max_attempts,
            priority=priority,
            supersede_key=clean_supersede_key,
            available_at=available_at,
            now=now,
        )
        con.commit()
        return row, True


def delivery_outbox_claim(
    *,
    worker_id: str,
    limit: int = 25,
    lease_seconds: int = 90,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Atomically lease ready messages and fence stale workers with a token."""

    clean_worker = str(worker_id).strip()
    if not clean_worker:
        raise ValueError("outbox_worker_id_required")
    now_dt = datetime.fromisoformat(str(now or utc_now_iso()).replace("Z", "+00:00"))
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_iso = now_dt.astimezone(timezone.utc).isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, int(lease_seconds)))).astimezone(timezone.utc).isoformat()
    claimed: list[dict[str, Any]] = []
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        # A worker that died during its final permitted attempt must not cause an
        # infinite lease/reclaim loop.
        con.execute(
            """
            UPDATE delivery_outbox
            SET status = 'dead', lease_owner = NULL, lease_token = NULL,
                lease_until = NULL, last_error = COALESCE(last_error, 'delivery_lease_expired'),
                updated_at = ?
            WHERE status = 'processing' AND lease_until <= ? AND attempts >= max_attempts
            """,
            (now_iso, now_iso),
        )
        rows = con.execute(
            """
            WITH eligible AS (
                SELECT candidate.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY
                               candidate.topic,
                               candidate.supersede_key,
                               CASE WHEN candidate.supersede_key IS NULL THEN candidate.id ELSE 0 END
                           ORDER BY candidate.id DESC
                       ) AS scope_rank
                FROM delivery_outbox AS candidate
                WHERE candidate.attempts < candidate.max_attempts
                  AND (
                        (candidate.status IN ('pending', 'retry') AND candidate.available_at <= ?)
                     OR (candidate.status = 'processing' AND candidate.lease_until <= ?)
                  )
                  AND (
                        candidate.supersede_key IS NULL
                     OR NOT EXISTS (
                            SELECT 1
                            FROM delivery_outbox AS active
                            WHERE active.topic = candidate.topic
                              AND active.supersede_key = candidate.supersede_key
                              AND active.status = 'processing'
                              AND active.lease_until > ?
                              AND active.id != candidate.id
                        )
                  )
            )
            SELECT id FROM eligible
            WHERE scope_rank = 1
            ORDER BY priority DESC, available_at ASC, id ASC
            LIMIT ?
            """,
            (now_iso, now_iso, now_iso, max(1, min(int(limit), 100))),
        ).fetchall()
        for selected in rows:
            item_id = int(selected["id"])
            lease_token = uuid.uuid4().hex
            con.execute(
                """
                UPDATE delivery_outbox
                SET status = 'processing', attempts = attempts + 1,
                    lease_owner = ?, lease_token = ?, lease_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (clean_worker, lease_token, lease_until, now_iso, item_id),
            )
            row = con.execute("SELECT * FROM delivery_outbox WHERE id = ?", (item_id,)).fetchone()
            if row is not None:
                claimed.append(dict(row))
        con.commit()
    return claimed


def delivery_outbox_renew_lease(
    item_id: int,
    *,
    lease_token: str,
    lease_seconds: int = 90,
    now: str | None = None,
) -> bool:
    """Extend an active lease while fencing expired or replaced workers."""

    now_dt = datetime.fromisoformat(str(now or utc_now_iso()).replace("Z", "+00:00"))
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_iso = now_dt.astimezone(timezone.utc).isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, int(lease_seconds)))).astimezone(timezone.utc).isoformat()
    with _db_lock, connect() as con:
        cur = con.execute(
            """
            UPDATE delivery_outbox
            SET lease_until = ?, updated_at = ?
            WHERE id = ? AND status = 'processing' AND lease_token = ?
              AND lease_until > ?
            """,
            (lease_until, now_iso, int(item_id), str(lease_token), now_iso),
        )
        con.commit()
        return cur.rowcount == 1


def delivery_outbox_mark_delivered(
    item_id: int,
    *,
    lease_token: str,
    message_id: int | None = None,
    now: str | None = None,
) -> bool:
    completed_at = str(now or utc_now_iso())
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT payload_json FROM delivery_outbox
            WHERE id = ? AND status = 'processing' AND lease_token = ?
            """,
            (int(item_id), str(lease_token)),
        ).fetchone()
        if row is None:
            con.commit()
            return False
        try:
            original_payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            original_payload = {}
        compact_payload: dict[str, Any] = {"compacted": True}
        if isinstance(original_payload, dict):
            for key in ("payload_version", "guild_id", "session_key", "destination"):
                if original_payload.get(key) is not None:
                    compact_payload[key] = original_payload[key]
            result = original_payload.get("result")
            bill = original_payload.get("bill")
            bill_id = (
                dict(result).get("bill_id")
                if isinstance(result, dict)
                else dict(bill).get("id") if isinstance(bill, dict) else original_payload.get("bill_id")
            )
            if bill_id:
                compact_payload["bill_id"] = int(bill_id)
        cur = con.execute(
            """
            UPDATE delivery_outbox
            SET status = 'delivered', message_id = ?, delivered_at = ?,
                lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                last_error = NULL, payload_json = ?, updated_at = ?
            WHERE id = ? AND status = 'processing' AND lease_token = ?
            """,
            (
                int(message_id) if message_id else None,
                completed_at,
                json.dumps(compact_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                completed_at,
                int(item_id),
                str(lease_token),
            ),
        )
        con.commit()
        return cur.rowcount == 1


def delivery_outbox_mark_failed(
    item_id: int,
    *,
    lease_token: str,
    error: str,
    retry_at: str,
    permanent: bool = False,
    now: str | None = None,
) -> bool:
    updated_at = str(now or utc_now_iso())
    clean_error = " ".join(str(error or "delivery_failed").split())[:1000]
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT attempts, max_attempts FROM delivery_outbox
            WHERE id = ? AND status = 'processing' AND lease_token = ?
            """,
            (int(item_id), str(lease_token)),
        ).fetchone()
        if row is None:
            con.commit()
            return False
        is_dead = bool(permanent) or int(row["attempts"]) >= int(row["max_attempts"])
        con.execute(
            """
            UPDATE delivery_outbox
            SET status = ?, available_at = ?, lease_owner = NULL,
                lease_token = NULL, lease_until = NULL, last_error = ?, updated_at = ?
            WHERE id = ? AND status = 'processing' AND lease_token = ?
            """,
            (
                "dead" if is_dead else "retry",
                str(retry_at),
                clean_error,
                updated_at,
                int(item_id),
                str(lease_token),
            ),
        )
        con.commit()
        return True


def delivery_outbox_defer(
    item_id: int,
    *,
    lease_token: str,
    available_at: str,
    reason: str,
    now: str | None = None,
) -> bool:
    """Return a leased delivery to the queue without consuming an attempt."""

    updated_at = str(now or utc_now_iso())
    with _db_lock, connect() as con:
        cur = con.execute(
            """
            UPDATE delivery_outbox
            SET status = 'retry', attempts = MAX(0, attempts - 1), available_at = ?,
                lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                last_error = ?, updated_at = ?
            WHERE id = ? AND status = 'processing' AND lease_token = ?
            """,
            (
                str(available_at),
                f"deferred:{str(reason or 'policy')[:200]}",
                updated_at,
                int(item_id),
                str(lease_token),
            ),
        )
        con.commit()
        return cur.rowcount == 1


def delivery_outbox_get(item_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM delivery_outbox WHERE id = ?", (int(item_id),)).fetchone()
    return dict(row) if row is not None else None


def delivery_outbox_is_current_supersession(
    item_id: int,
    *,
    topic: str,
    supersede_key: str | None,
) -> bool:
    clean_key = str(supersede_key or "").strip()
    if not clean_key:
        return True
    with _db_lock, connect() as con:
        newer = con.execute(
            """
            SELECT 1 FROM delivery_outbox
            WHERE topic = ? AND supersede_key = ? AND id > ?
              AND status IN ('pending', 'retry', 'processing', 'delivered')
            LIMIT 1
            """,
            (str(topic), clean_key, int(item_id)),
        ).fetchone()
    return newer is None


def delivery_outbox_latest_supersession(
    *,
    topic: str,
    supersede_key: str,
) -> dict[str, Any] | None:
    """Return the newest durable state for a semantic delivery scope."""

    clean_topic = str(topic).strip()
    clean_key = str(supersede_key).strip()
    if not clean_topic or not clean_key:
        return None
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT * FROM delivery_outbox
            WHERE topic = ? AND supersede_key = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (clean_topic, clean_key),
        ).fetchone()
    return dict(row) if row is not None else None


def delivery_outbox_next_due_delay(*, now: str | None = None) -> float | None:
    """Return seconds until the next claim or expired-lease cleanup is due."""

    now_dt = _parse_utc(str(now or utc_now_iso()))
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT due_at
            FROM (
                SELECT available_at AS due_at
                FROM delivery_outbox
                WHERE status IN ('pending', 'retry') AND attempts < max_attempts
                UNION ALL
                SELECT COALESCE(lease_until, available_at, updated_at) AS due_at
                FROM delivery_outbox
                WHERE status = 'processing'
            )
            WHERE due_at IS NOT NULL
            ORDER BY julianday(due_at) ASC, due_at ASC
            LIMIT 1
            """
        ).fetchone()
    if row is None or row["due_at"] is None:
        return None
    try:
        due_at = _parse_utc(str(row["due_at"]))
    except (TypeError, ValueError):
        # A malformed legacy timestamp should trigger a diagnostic claim cycle
        # instead of putting the whole delivery worker to sleep indefinitely.
        return 0.0
    return max(0.0, (due_at - now_dt).total_seconds())


def delivery_outbox_counts() -> dict[str, int]:
    with _db_lock, connect() as con:
        rows = con.execute(
            "SELECT status, COUNT(*) AS n FROM delivery_outbox GROUP BY status"
        ).fetchall()
    return {str(row["status"]): int(row["n"]) for row in rows}


def delivery_outbox_unreported_dead(limit: int = 25) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM delivery_outbox
            WHERE status = 'dead' AND dead_notified_at IS NULL
            ORDER BY updated_at ASC, id ASC LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [dict(row) for row in rows]


def delivery_outbox_mark_dead_notified(item_id: int, *, now: str | None = None) -> bool:
    notified_at = str(now or utc_now_iso())
    with _db_lock, connect() as con:
        cur = con.execute(
            """
            UPDATE delivery_outbox SET dead_notified_at = ?, updated_at = ?
            WHERE id = ? AND status = 'dead' AND dead_notified_at IS NULL
            """,
            (notified_at, notified_at, int(item_id)),
        )
        con.commit()
        return cur.rowcount == 1


def delivery_outbox_requeue_dead(
    item_id: int,
    *,
    guild_id: int | None = None,
    now: str | None = None,
) -> bool:
    ready_at = str(now or utc_now_iso())
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT payload_json FROM delivery_outbox WHERE id = ? AND status = 'dead'",
            (int(item_id),),
        ).fetchone()
        if row is None:
            con.rollback()
            return False
        if guild_id is not None:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
                owner_guild_id = int(payload.get("guild_id") or 0) if isinstance(payload, dict) else 0
            except (TypeError, ValueError, json.JSONDecodeError):
                owner_guild_id = 0
            if owner_guild_id != int(guild_id):
                con.rollback()
                return False
        cur = con.execute(
            """
            UPDATE delivery_outbox
            SET status = 'retry', attempts = 0, available_at = ?, lease_owner = NULL,
                lease_token = NULL, lease_until = NULL, last_error = NULL,
                dead_notified_at = NULL, updated_at = ?
            WHERE id = ? AND status = 'dead'
            """,
            (ready_at, ready_at, int(item_id)),
        )
        con.commit()
        return cur.rowcount == 1


def _consensus_payload_matches(
    raw_payload: object,
    *,
    session_key: str,
    guild_id: int,
) -> bool:
    try:
        payload = json.loads(str(raw_payload or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    try:
        owner_guild_id = int(payload.get("guild_id") or 0)
    except (TypeError, ValueError):
        return False
    return owner_guild_id == int(guild_id) and str(payload.get("session_key") or "") == str(
        session_key
    )


def delivery_outbox_consensus_status(
    session_key: str,
    *,
    guild_id: int,
) -> dict[str, Any]:
    """Summarize delivery state owned by one consensus session.

    Ownership is verified from the durable payload rather than inferred from a
    dedupe key.  This prevents one guild's recovery panel from exposing or
    reviving another guild's jobs.
    """

    clean_session_key = str(session_key).strip()
    if not clean_session_key:
        raise ValueError("consensus_session_key_required")
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT id, topic, status, supersede_key, payload_json, last_error
            FROM delivery_outbox
            WHERE topic LIKE ?
            ORDER BY id ASC
            """,
            (f"{CONSENSUS_TOPIC_PREFIX}%",),
        ).fetchall()
    owned = [
        dict(row)
        for row in rows
        if _consensus_payload_matches(
            row["payload_json"],
            session_key=clean_session_key,
            guild_id=int(guild_id),
        )
    ]
    counts: dict[str, int] = {}
    for row in owned:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    dead = [row for row in owned if str(row.get("status") or "") == "dead"]
    return {
        "session_key": clean_session_key,
        "guild_id": int(guild_id),
        "total": len(owned),
        "counts": counts,
        "dead_ids": [int(row["id"]) for row in dead],
        "dead_errors": [str(row.get("last_error") or "delivery_failed") for row in dead],
    }


def delivery_outbox_requeue_dead_for_consensus(
    session_key: str,
    *,
    guild_id: int,
    allowed_topics: set[str] | frozenset[str] | None = None,
    now: str | None = None,
    limit: int = 100,
) -> list[int]:
    """Revive current dead semantic deliveries for an active consensus.

    Control DMs are intentionally excluded: their recovery creates a fresh
    generation protected by a supersession fence.  For every other superseded
    scope only the newest generation may be revived, so a dead stale panel or
    notice can never overwrite a newer delivered state.
    """

    clean_session_key = str(session_key).strip()
    if not clean_session_key:
        raise ValueError("consensus_session_key_required")
    topics = (
        {str(topic).strip() for topic in allowed_topics if str(topic).strip()}
        if allowed_topics is not None
        else None
    )
    ready_at = str(now or utc_now_iso())
    selected_ids: list[int] = []
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute(
            """
            SELECT id, topic, supersede_key, payload_json
            FROM delivery_outbox
            WHERE status = 'dead' AND topic LIKE ?
            ORDER BY id DESC
            """,
            (f"{CONSENSUS_TOPIC_PREFIX}%",),
        ).fetchall()
        for row in rows:
            topic = str(row["topic"] or "")
            if topic == CONSENSUS_CONTROL_DM_TOPIC or (topics is not None and topic not in topics):
                continue
            if not _consensus_payload_matches(
                row["payload_json"],
                session_key=clean_session_key,
                guild_id=int(guild_id),
            ):
                continue
            supersede_key = str(row["supersede_key"] or "").strip()
            if supersede_key:
                newest = con.execute(
                    """
                    SELECT id FROM delivery_outbox
                    WHERE topic = ? AND supersede_key = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (topic, supersede_key),
                ).fetchone()
                if newest is None or int(newest["id"]) != int(row["id"]):
                    continue
            selected_ids.append(int(row["id"]))
            if len(selected_ids) >= max(1, min(int(limit), 500)):
                break
        if selected_ids:
            placeholders = ",".join("?" for _ in selected_ids)
            con.execute(
                f"""
                UPDATE delivery_outbox
                SET status = 'retry', attempts = 0, available_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                    last_error = NULL, dead_notified_at = NULL, updated_at = ?
                WHERE status = 'dead' AND id IN ({placeholders})
                """,
                (ready_at, ready_at, *selected_ids),
            )
        con.commit()
    return list(reversed(selected_ids))

__all__ = ['OUTBOX_OPEN_STATUSES', 'CONSENSUS_TOPIC_PREFIX', 'CONSENSUS_CONTROL_DM_TOPIC', 'delivery_outbox_enqueue_in_connection', 'delivery_outbox_enqueue', 'delivery_outbox_ensure_current', 'delivery_outbox_claim', 'delivery_outbox_renew_lease', 'delivery_outbox_mark_delivered', 'delivery_outbox_mark_failed', 'delivery_outbox_defer', 'delivery_outbox_get', 'delivery_outbox_is_current_supersession', 'delivery_outbox_latest_supersession', 'delivery_outbox_next_due_delay', 'delivery_outbox_counts', 'delivery_outbox_unreported_dead', 'delivery_outbox_mark_dead_notified', 'delivery_outbox_requeue_dead', 'delivery_outbox_consensus_status', 'delivery_outbox_requeue_dead_for_consensus']
