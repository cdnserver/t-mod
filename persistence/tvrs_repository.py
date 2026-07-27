from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from persistence.core import TVRSBill, _db_lock, _tvrs_bill_from_row, connect, utc_now_iso
from persistence.activity_repository import _record_bot_action, get_meta, set_meta, set_meta_value
from persistence.outbox_repository import delivery_outbox_enqueue_in_connection


def _next_bill_number_in_connection(
    con: sqlite3.Connection,
    guild_id: int,
    *,
    default_next: int = 9,
) -> int:
    """Return a collision-safe number even when legacy metadata is stale."""

    meta_key = f"tvrs_next_bill_number:{int(guild_id)}"
    meta_row = con.execute(
        "SELECT value FROM meta WHERE key = ?",
        (meta_key,),
    ).fetchone()
    raw = str(meta_row["value"]) if meta_row else ""
    configured_next = int(raw) if raw.isdigit() else 1
    latest = con.execute(
        "SELECT MAX(bill_number) AS n FROM tvrs_bills WHERE guild_id = ?",
        (int(guild_id),),
    ).fetchone()
    after_existing = int(latest["n"] or 0) + 1 if latest else 1
    return max(1, int(default_next), configured_next, after_existing)


def tvrs_next_bill_number(guild_id: int, default_next: int = 9) -> int:
    with _db_lock, connect() as con:
        return _next_bill_number_in_connection(
            con,
            int(guild_id),
            default_next=int(default_next),
        )


def tvrs_set_next_bill_number(guild_id: int, next_number: int) -> None:
    set_meta_value(f"tvrs_next_bill_number:{guild_id}", str(max(1, int(next_number))))


def tvrs_set_last_accepted_bill_number(guild_id: int, last_number: int) -> int:
    next_number = max(1, int(last_number) + 1)
    tvrs_set_next_bill_number(guild_id, next_number)
    return next_number


def tvrs_create_bill(
    *,
    guild_id: int,
    channel_id: int | None,
    author_id: int,
    author_display: str | None,
    title: str,
    summary: str,
    materials: str | None,
    decision_category: str = "ordinary",
    implementation_plan: str | None = None,
    leadership_actions: str | None = None,
    editor_workspace_id: int | None = None,
) -> TVRSBill:
    now = utc_now_iso()
    meta_key = f"tvrs_next_bill_number:{guild_id}"
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        number = _next_bill_number_in_connection(con, int(guild_id))
        cur = con.execute(
            """
            INSERT INTO tvrs_bills(
                guild_id, bill_number, channel_id, message_id, author_id,
                author_display, title, summary, materials, decision_category,
                implementation_plan, leadership_actions, editor_workspace_id,
                status, created_at, updated_at
            )
            VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (
                guild_id,
                number,
                channel_id,
                author_id,
                author_display,
                title,
                summary,
                materials,
                "ordinary",
                implementation_plan,
                leadership_actions,
                editor_workspace_id,
                now,
                now,
            ),
        )
        set_meta(con, meta_key, str(number + 1))
        row = con.execute("SELECT * FROM tvrs_bills WHERE id = ?", (int(cur.lastrowid),)).fetchone()
        con.commit()
    bill = _tvrs_bill_from_row(row)
    if bill is None:
        raise RuntimeError("tvrs_bill_create_failed")
    return bill


def tvrs_create_bill_with_publication(
    *,
    guild_id: int,
    channel_id: int,
    author_id: int,
    author_display: str | None,
    title: str,
    summary: str,
    materials: str | None,
    delivery_topic: str,
    max_attempts: int = 12,
    decision_category: str = "ordinary",
    implementation_plan: str | None = None,
    leadership_actions: str | None = None,
    editor_workspace_id: int | None = None,
) -> tuple[TVRSBill, dict[str, Any], bool]:
    """Atomically create a bill and its durable public-card intent.

    An identical submission by the same author is reused for a short window.
    This covers an interaction retry after the database committed but Discord
    disconnected before the user received confirmation.
    """

    now = utc_now_iso()
    duplicate_after = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    clean_topic = str(delivery_topic).strip()
    if not clean_topic:
        raise ValueError("bill_publication_topic_required")
    meta_key = f"tvrs_next_bill_number:{int(guild_id)}"
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        active = con.execute(
            """
            SELECT 1 FROM tvrs_consensus_sessions
            WHERE guild_id = ? AND finished_at IS NULL LIMIT 1
            """,
            (int(guild_id),),
        ).fetchone()
        if active is not None:
            con.rollback()
            raise ValueError("bill_submission_locked_by_active_consensus")
        if editor_workspace_id is not None:
            # A workspace is a durable idempotency key.  This remains safe even
            # after an interaction retry or a restart much later than 30 minutes.
            row = con.execute(
                """
                SELECT * FROM tvrs_bills
                WHERE guild_id = ? AND author_id = ? AND editor_workspace_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (int(guild_id), int(author_id), int(editor_workspace_id)),
            ).fetchone()
        else:
            row = con.execute(
                """
                SELECT * FROM tvrs_bills
                WHERE guild_id = ? AND author_id = ? AND title = ? AND summary = ?
                  AND COALESCE(materials, '') = COALESCE(?, '')
                  AND decision_category = ?
                  AND editor_workspace_id IS NULL
                  AND status IN ('publishing', 'draft', 'queued', 'requeued')
                  AND created_at >= ?
                ORDER BY id DESC LIMIT 1
                """,
                (
                    int(guild_id),
                    int(author_id),
                    str(title),
                    str(summary),
                    materials,
                    "ordinary",
                    duplicate_after,
                ),
            ).fetchone()
        created = row is None
        if row is None:
            number = _next_bill_number_in_connection(con, int(guild_id))
            inserted = con.execute(
                """
                INSERT INTO tvrs_bills(
                    guild_id, bill_number, channel_id, message_id, author_id,
                    author_display, title, summary, materials, decision_category,
                    implementation_plan, leadership_actions, editor_workspace_id,
                    status, created_at, updated_at
                ) VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'publishing', ?, ?)
                """,
                (
                    int(guild_id),
                    number,
                    int(channel_id),
                    int(author_id),
                    author_display,
                    str(title),
                    str(summary),
                    materials,
                    "ordinary",
                    implementation_plan,
                    leadership_actions,
                    editor_workspace_id,
                    now,
                    now,
                ),
            )
            set_meta(con, meta_key, str(number + 1))
            row = con.execute(
                "SELECT * FROM tvrs_bills WHERE id = ?",
                (int(inserted.lastrowid),),
            ).fetchone()
        if row is None:  # pragma: no cover - insert/select is in one transaction
            con.rollback()
            raise RuntimeError("tvrs_bill_create_failed")
        bill = dict(row)
        delivery = delivery_outbox_enqueue_in_connection(
            con,
            topic=clean_topic,
            dedupe_key=f"tvrs:bill:{int(bill['id'])}:publication",
            payload={
                "payload_version": 1,
                "guild_id": int(guild_id),
                "channel_id": int(channel_id),
                "publication_kind": "initial",
                "bill": {
                    key: bill.get(key)
                    for key in (
                        "id",
                        "guild_id",
                        "bill_number",
                        "channel_id",
                        "message_id",
                        "author_id",
                        "author_display",
                        "title",
                        "summary",
                        "materials",
                        "decision_category",
                        "implementation_plan",
                        "leadership_actions",
                        "editor_workspace_id",
                        "status",
                    )
                },
            },
            max_attempts=max_attempts,
            now=now,
        )
        if str(delivery.get("status") or "") == "dead":
            con.execute(
                """
                UPDATE delivery_outbox
                SET status = 'retry', attempts = 0, available_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                    last_error = NULL, dead_notified_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'dead'
                """,
                (now, now, int(delivery["id"])),
            )
            refreshed = con.execute(
                "SELECT * FROM delivery_outbox WHERE id = ?",
                (int(delivery["id"]),),
            ).fetchone()
            if refreshed is not None:
                delivery = dict(refreshed)
        con.commit()
    result = _tvrs_bill_from_row(row)
    if result is None:  # pragma: no cover - row validated above
        raise RuntimeError("tvrs_bill_create_failed")
    return result, delivery, created


def tvrs_set_bill_message(bill_id: int, message_id: int, channel_id: int | None = None) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        if channel_id is None:
            con.execute("UPDATE tvrs_bills SET message_id = ?, updated_at = ? WHERE id = ?", (message_id, now, bill_id))
        else:
            con.execute("UPDATE tvrs_bills SET message_id = ?, channel_id = ?, updated_at = ? WHERE id = ?", (message_id, channel_id, now, bill_id))
        con.commit()


def tvrs_complete_bill_publication(
    bill_id: int,
    message_id: int,
    channel_id: int,
) -> dict[str, Any] | None:
    """Bind the public card and release an initial bill into the queue."""

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            UPDATE tvrs_bills
            SET message_id = ?, channel_id = ?,
                status = CASE WHEN status = 'publishing' THEN 'draft' ELSE status END,
                updated_at = ?
            WHERE id = ?
            """,
            (int(message_id), int(channel_id), now, int(bill_id)),
        )
        row = con.execute("SELECT * FROM tvrs_bills WHERE id = ?", (int(bill_id),)).fetchone()
        con.commit()
    return dict(row) if row is not None else None


def tvrs_get_bill_by_id(bill_id: int) -> TVRSBill | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM tvrs_bills WHERE id = ?", (bill_id,)).fetchone()
    return _tvrs_bill_from_row(row)


def tvrs_get_bill_by_message(guild_id: int, message_id: int) -> TVRSBill | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM tvrs_bills WHERE guild_id = ? AND message_id = ?", (guild_id, message_id)).fetchone()
    return _tvrs_bill_from_row(row)


def tvrs_cast_vote(*, guild_id: int, bill_id: int, voter_id: int, voter_display: str | None, vote: str) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        before = con.execute(
            "SELECT * FROM tvrs_votes WHERE guild_id = ? AND bill_id = ? AND voter_id = ?",
            (guild_id, bill_id, voter_id),
        ).fetchone()
        con.execute(
            """
            INSERT INTO tvrs_votes(guild_id, bill_id, voter_id, voter_display, vote, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bill_id, voter_id) DO UPDATE SET
                voter_display = excluded.voter_display, vote = excluded.vote, updated_at = excluded.updated_at
            """,
            (guild_id, bill_id, voter_id, voter_display, vote, now, now),
        )
        after = con.execute(
            "SELECT * FROM tvrs_votes WHERE guild_id = ? AND bill_id = ? AND voter_id = ?",
            (guild_id, bill_id, voter_id),
        ).fetchone()
        if after is not None:
            _record_bot_action(
                con,
                guild_id=guild_id,
                actor_id=voter_id,
                actor_display=voter_display,
                module="tvrs",
                action_kind="generic_row_restore",
                target_type="tvrs_vote",
                target_id=int(after["id"]),
                summary=f"Голос по законопроекту #{bill_id}: {vote}",
                payload={"table": "tvrs_votes", "row_id": int(after["id"]), "before": None if before is None else dict(before)},
                now=now,
            )
        con.commit()


def tvrs_votes_for_bill(guild_id: int, bill_id: int) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute("SELECT * FROM tvrs_votes WHERE guild_id = ? AND bill_id = ? ORDER BY updated_at ASC", (guild_id, bill_id)).fetchall()
    return [dict(row) for row in rows]


def tvrs_vote_counts(guild_id: int, bill_id: int) -> dict[str, int]:
    counts = {"approve": 0, "abstain": 0, "reject": 0}
    for row in tvrs_votes_for_bill(guild_id, bill_id):
        vote = str(row.get("vote") or "")
        if vote in counts:
            counts[vote] += 1
    counts["total"] = sum(counts.values())
    return counts


def tvrs_recent_bills(guild_id: int, limit: int = 10) -> list[TVRSBill]:
    with _db_lock, connect() as con:
        rows = con.execute("SELECT * FROM tvrs_bills WHERE guild_id = ? ORDER BY bill_number DESC LIMIT ?", (guild_id, limit)).fetchall()
    return [b for row in rows if (b := _tvrs_bill_from_row(row)) is not None]


# ---------------- TVRS live plenary consensus ----------------


def tvrs_consensus_save_session(
    snapshot: dict[str, Any],
    *,
    event_type: str,
    actor_id: int | None = None,
    actor_display: str | None = None,
    stage_from: str | None = None,
    details: dict[str, Any] | None = None,
    deliveries: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    session_key = str(snapshot.get("session_key") or "").strip()
    if not session_key:
        raise ValueError("consensus_session_key_required")
    guild_id = int(snapshot.get("guild_id") or 0)
    if guild_id <= 0:
        raise ValueError("consensus_guild_id_required")
    stage = str(snapshot.get("stage") or "").strip()
    if not stage:
        raise ValueError("consensus_stage_required")
    current_bill = snapshot.get("current_bill") if isinstance(snapshot.get("current_bill"), dict) else {}
    current_bill_id = int(current_bill.get("id") or 0) or None
    now = utc_now_iso()
    created_at = str(snapshot.get("created_at") or now)
    finished = bool(snapshot.get("finished")) or stage in {"finished", "cancelled"}
    expected_revision = int(snapshot.get("revision") or 0)
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT revision, stage, guild_id, created_at, finished_at FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        if existing is None:
            if expected_revision != 0:
                con.rollback()
                raise RuntimeError("consensus_session_revision_conflict")
            revision = 1
        else:
            if existing["finished_at"] is not None:
                con.rollback()
                raise RuntimeError("consensus_session_already_finished")
            if int(existing["guild_id"] or 0) != guild_id:
                con.rollback()
                raise RuntimeError("consensus_session_guild_conflict")
            if int(existing["revision"] or 0) != expected_revision:
                con.rollback()
                raise RuntimeError("consensus_session_revision_conflict")
            revision = expected_revision + 1
        stored_snapshot = dict(snapshot)
        stored_snapshot["revision"] = revision
        payload = json.dumps(stored_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        previous_stage = stage_from if stage_from is not None else (str(existing["stage"]) if existing else None)
        if existing is None:
            con.execute(
                """
                INSERT INTO tvrs_consensus_sessions(
                    session_key, guild_id, engine_version, plenary_number, stage, leader_id,
                    current_bill_id, snapshot_json, revision, created_at, updated_at, finished_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_key,
                    guild_id,
                    int(snapshot.get("engine_version") or 2),
                    int(snapshot.get("plenary_number") or 0),
                    stage,
                    int(snapshot.get("leader_id") or 0),
                    current_bill_id,
                    payload,
                    revision,
                    created_at,
                    now,
                    now if finished else None,
                ),
            )
        else:
            updated = con.execute(
                """
                UPDATE tvrs_consensus_sessions
                SET engine_version = ?, plenary_number = ?, stage = ?, leader_id = ?, current_bill_id = ?,
                    snapshot_json = ?, revision = ?, updated_at = ?, finished_at = ?
                WHERE session_key = ? AND revision = ? AND finished_at IS NULL
                """,
                (
                    int(snapshot.get("engine_version") or 2),
                    int(snapshot.get("plenary_number") or 0),
                    stage,
                    int(snapshot.get("leader_id") or 0),
                    current_bill_id,
                    payload,
                    revision,
                    now,
                    now if finished else None,
                    session_key,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                con.rollback()
                raise RuntimeError("consensus_session_revision_conflict")
        con.execute(
            """
            INSERT INTO tvrs_consensus_events(
                session_key, guild_id, event_type, actor_id, actor_display,
                stage_from, stage_to, details_json, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_key,
                guild_id,
                str(event_type)[:80],
                actor_id,
                actor_display,
                previous_stage,
                stage,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        for delivery in deliveries:
            delivery_outbox_enqueue_in_connection(
                con,
                topic=str(delivery.get("topic") or ""),
                dedupe_key=str(delivery.get("dedupe_key") or ""),
                payload=dict(delivery.get("payload") or {}),
                max_attempts=int(delivery.get("max_attempts") or 8),
                priority=int(delivery.get("priority") or 0),
                supersede_key=delivery.get("supersede_key"),
                available_at=delivery.get("available_at"),
                now=now,
            )
        row = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        con.commit()
    return dict(row) if row else {}


def tvrs_consensus_commit_begin_bill(
    snapshot: dict[str, Any],
    *,
    expected_revision: int,
    bill_id: int,
    event_type: str,
    actor_id: int | None,
    actor_display: str | None,
    details: dict[str, Any] | None = None,
    deliveries: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Atomically activate a bill, persist the voting stage and enqueue DMs."""

    session_key = str(snapshot.get("session_key") or "").strip()
    guild_id = int(snapshot.get("guild_id") or 0)
    expected = int(expected_revision)
    selected_bill_id = int(bill_id)
    current_bill = snapshot.get("current_bill") if isinstance(snapshot.get("current_bill"), dict) else {}
    if not session_key or guild_id <= 0 or expected < 1 or selected_bill_id <= 0:
        raise ValueError("consensus_begin_bill_identity_required")
    if str(snapshot.get("stage") or "") != "voting" or int(current_bill.get("id") or 0) != selected_bill_id:
        raise ValueError("consensus_begin_bill_snapshot_invalid")

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        persisted = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        if persisted is None:
            con.rollback()
            raise RuntimeError("consensus_session_not_persisted")
        if (
            str(persisted["stage"] or "") == "voting"
            and int(persisted["current_bill_id"] or 0) == selected_bill_id
            and int(persisted["revision"] or 0) == expected + 1
        ):
            outbox_ids: list[int] = []
            for delivery in deliveries:
                outbox_row = con.execute(
                    "SELECT id FROM delivery_outbox WHERE topic = ? AND dedupe_key = ?",
                    (str(delivery.get("topic") or ""), str(delivery.get("dedupe_key") or "")),
                ).fetchone()
                if outbox_row is None:
                    con.rollback()
                    raise RuntimeError("consensus_begin_bill_delivery_missing")
                outbox_ids.append(int(outbox_row["id"]))
            con.commit()
            return {"session": dict(persisted), "outbox_ids": outbox_ids, "idempotent": True}
        if (
            int(persisted["revision"] or 0) != expected
            or str(persisted["stage"] or "") not in {"registration", "after_result"}
            or persisted["current_bill_id"] is not None
            or persisted["finished_at"] is not None
        ):
            con.rollback()
            raise RuntimeError("consensus_begin_bill_revision_conflict")
        bill_update = con.execute(
            """
            UPDATE tvrs_bills SET status = 'voting', updated_at = ?
            WHERE id = ? AND guild_id = ? AND status IN ('draft', 'queued', 'requeued')
            """,
            (now, selected_bill_id, guild_id),
        )
        if bill_update.rowcount != 1:
            con.rollback()
            raise RuntimeError("consensus_begin_bill_unavailable")

        new_revision = expected + 1
        stored_snapshot = dict(snapshot)
        stored_snapshot["revision"] = new_revision
        snapshot_json = json.dumps(stored_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        session_update = con.execute(
            """
            UPDATE tvrs_consensus_sessions
            SET stage = 'voting', current_bill_id = ?, snapshot_json = ?, revision = ?, updated_at = ?
            WHERE session_key = ? AND revision = ? AND finished_at IS NULL
              AND current_bill_id IS NULL
            """,
            (selected_bill_id, snapshot_json, new_revision, now, session_key, expected),
        )
        if session_update.rowcount != 1:
            con.rollback()
            raise RuntimeError("consensus_begin_bill_revision_conflict")
        con.execute(
            """
            INSERT INTO tvrs_consensus_events(
                session_key, guild_id, event_type, actor_id, actor_display,
                stage_from, stage_to, details_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'voting', ?, ?)
            """,
            (
                session_key,
                guild_id,
                str(event_type)[:80],
                actor_id,
                actor_display,
                str(persisted["stage"] or ""),
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        outbox_ids: list[int] = []
        for delivery in deliveries:
            outbox_row = delivery_outbox_enqueue_in_connection(
                con,
                topic=str(delivery.get("topic") or ""),
                dedupe_key=str(delivery.get("dedupe_key") or ""),
                payload=dict(delivery.get("payload") or {}),
                max_attempts=int(delivery.get("max_attempts") or 8),
                priority=int(delivery.get("priority") or 0),
                supersede_key=delivery.get("supersede_key"),
                available_at=delivery.get("available_at"),
                now=now,
            )
            outbox_ids.append(int(outbox_row["id"]))
        final_session = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        con.commit()
    return {
        "session": dict(final_session) if final_session is not None else {},
        "outbox_ids": outbox_ids,
        "idempotent": False,
    }


def tvrs_consensus_commit_finalization(
    snapshot: dict[str, Any],
    *,
    expected_revision: int,
    result: dict[str, Any],
    bill_status: str,
    result_summary: str,
    event_type: str,
    actor_id: int | None = None,
    actor_display: str | None = None,
    details: dict[str, Any] | None = None,
    deliveries: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Commit a consensus decision and every delivery intent atomically.

    This is the production write path for finalization.  The legacy granular
    functions remain available for administration and compatibility, but the
    live workflow must not create a result, advance the session, and enqueue
    notifications in separate transactions.
    """

    session_key = str(snapshot.get("session_key") or "").strip()
    guild_id = int(snapshot.get("guild_id") or 0)
    bill_id = int(result.get("bill_id") or 0)
    if not session_key or guild_id <= 0 or bill_id <= 0:
        raise ValueError("consensus_finalization_identity_required")
    if str(snapshot.get("stage") or "") != "after_result" or snapshot.get("current_bill") is not None:
        raise ValueError("consensus_finalization_snapshot_invalid")
    expected = int(expected_revision)
    if expected < 1:
        raise ValueError("consensus_finalization_revision_required")

    try:
        normalized_votes_json = json.dumps(
            json.loads(str(result.get("votes_json") or "{}")),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("consensus_result_votes_invalid") from exc
    try:
        normalized_blocks_json = json.dumps(
            json.loads(str(result.get("block_votes_json") or "{}")),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("consensus_result_blocks_invalid") from exc

    def result_conflicts(row: sqlite3.Row, expected_values: dict[str, Any]) -> bool:
        for key, value in expected_values.items():
            if key in {"votes_json", "block_votes_json"}:
                try:
                    persisted_votes = json.loads(str(row[key] or "{}"))
                    expected_votes = json.loads(str(value or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    return True
                if persisted_votes != expected_votes:
                    return True
            elif row[key] != value:
                return True
        return False

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        persisted = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        if persisted is None:
            con.rollback()
            raise RuntimeError("consensus_session_not_persisted")

        persisted_stage = str(persisted["stage"] or "")
        persisted_revision = int(persisted["revision"] or 0)
        existing_result = con.execute(
            "SELECT * FROM tvrs_live_results WHERE session_key = ? AND bill_id = ?",
            (session_key, bill_id),
        ).fetchone()
        if persisted_stage == "after_result" and existing_result is not None:
            # The whole transaction either committed or did not.  Returning the
            # previous receipt makes recovery and caller retries idempotent.
            expected_existing = {
                "guild_id": guild_id,
                "bill_number": int(result.get("bill_number") or 0),
                "bill_title": str(result.get("bill_title") or result.get("title") or ""),
                "status": str(result.get("status") or bill_status),
                "internal_percent": float(result.get("internal_percent") or 0.0),
                "overall_percent": float(result.get("overall_percent") or 0.0),
                "internal_active": 1 if bool(result.get("internal_active")) else 0,
                "votes_json": normalized_votes_json,
                "source_channel_id": (
                    int(result["source_channel_id"])
                    if result.get("source_channel_id")
                    else None
                ),
                "source_message_id": (
                    int(result["source_message_id"])
                    if result.get("source_message_id")
                    else None
                ),
                "decision_category": str(
                    result.get("decision_category") or "ordinary"
                ),
                "required_percent": float(
                    result.get("required_percent") or 50.0
                ),
                "opposed_percent": float(
                    result.get("opposed_percent") or 0.0
                ),
                "block_votes_json": normalized_blocks_json,
                "veto_by_id": int(result["veto_by_id"]) if result.get("veto_by_id") else None,
                "veto_by_display": result.get("veto_by_display"),
                "resolution_method": (
                    "veto"
                    if str(result.get("status") or bill_status) == "vetoed"
                    else ("oral" if str(result.get("resolution_method") or "") == "oral" else "vote")
                ),
                "resolution_note": result.get("resolution_note"),
                "resolved_by_id": int(result["resolved_by_id"]) if result.get("resolved_by_id") else None,
                "resolved_by_display": result.get("resolved_by_display"),
            }
            if result_conflicts(existing_result, expected_existing):
                con.rollback()
                raise RuntimeError("consensus_result_conflict")
            outbox_ids: list[int] = []
            for delivery in deliveries:
                outbox_row = con.execute(
                    "SELECT id FROM delivery_outbox WHERE topic = ? AND dedupe_key = ?",
                    (str(delivery.get("topic") or ""), str(delivery.get("dedupe_key") or "")),
                ).fetchone()
                if outbox_row is not None:
                    outbox_ids.append(int(outbox_row["id"]))
            con.commit()
            return {
                "session": dict(persisted),
                "result_id": int(existing_result["id"]),
                "outbox_ids": outbox_ids,
                "idempotent": True,
            }
        if (
            persisted_stage != "finalizing"
            or persisted_revision != expected
            or int(persisted["current_bill_id"] or 0) != bill_id
        ):
            con.rollback()
            raise RuntimeError("consensus_finalization_revision_conflict")
        try:
            persisted_snapshot = json.loads(str(persisted["snapshot_json"] or "{}"))
            pending_kind = str(dict(persisted_snapshot.get("pending_action") or {}).get("kind") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            pending_kind = ""
        result_kind = str(result.get("resolution_method") or "").strip().lower()
        if str(result.get("status") or bill_status) == "vetoed":
            # Backward compatibility: pre-metadata callers represented veto
            # only through the result status.
            result_kind = "veto"
        elif result_kind not in {"vote", "oral"}:
            result_kind = "vote"
        if pending_kind != result_kind:
            con.rollback()
            raise RuntimeError("consensus_finalization_kind_conflict")

        result_values = (
            guild_id,
            session_key,
            int(snapshot.get("plenary_number") or 0),
            bill_id,
            int(result.get("bill_number") or 0),
            str(result.get("bill_title") or result.get("title") or ""),
            str(result.get("status") or bill_status),
            float(result.get("internal_percent") or 0.0),
            float(result.get("overall_percent") or 0.0),
            1 if bool(result.get("internal_active")) else 0,
            normalized_votes_json,
            (
                int(result["source_channel_id"])
                if result.get("source_channel_id")
                else None
            ),
            (
                int(result["source_message_id"])
                if result.get("source_message_id")
                else None
            ),
            str(result.get("decision_category") or "ordinary"),
            float(result.get("required_percent") or 50.0),
            float(result.get("opposed_percent") or 0.0),
            normalized_blocks_json,
            int(result["veto_by_id"]) if result.get("veto_by_id") else None,
            result.get("veto_by_display"),
            result_kind,
            result.get("resolution_note"),
            int(result["resolved_by_id"]) if result.get("resolved_by_id") else None,
            result.get("resolved_by_display"),
            now,
        )
        if existing_result is None:
            cur = con.execute(
                """
                INSERT INTO tvrs_live_results(
                    guild_id, session_key, plenary_number, bill_id, bill_number,
                    bill_title, status, internal_percent, overall_percent,
                    internal_active, votes_json, source_channel_id,
                    source_message_id, decision_category, required_percent,
                    opposed_percent, block_votes_json, veto_by_id, veto_by_display,
                    resolution_method, resolution_note, resolved_by_id,
                    resolved_by_display, created_at
                ) VALUES(
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?
                )
                """,
                result_values,
            )
            result_id = int(cur.lastrowid)
        else:
            comparable = {
                "guild_id": guild_id,
                "bill_number": int(result.get("bill_number") or 0),
                "bill_title": str(result.get("bill_title") or result.get("title") or ""),
                "status": str(result.get("status") or bill_status),
                "internal_percent": float(result.get("internal_percent") or 0.0),
                "overall_percent": float(result.get("overall_percent") or 0.0),
                "internal_active": 1 if bool(result.get("internal_active")) else 0,
                "votes_json": normalized_votes_json,
                "source_channel_id": (
                    int(result["source_channel_id"])
                    if result.get("source_channel_id")
                    else None
                ),
                "source_message_id": (
                    int(result["source_message_id"])
                    if result.get("source_message_id")
                    else None
                ),
                "decision_category": str(
                    result.get("decision_category") or "ordinary"
                ),
                "required_percent": float(
                    result.get("required_percent") or 50.0
                ),
                "opposed_percent": float(
                    result.get("opposed_percent") or 0.0
                ),
                "block_votes_json": normalized_blocks_json,
                "veto_by_id": int(result["veto_by_id"]) if result.get("veto_by_id") else None,
                "veto_by_display": result.get("veto_by_display"),
                "resolution_method": result_kind,
                "resolution_note": result.get("resolution_note"),
                "resolved_by_id": int(result["resolved_by_id"]) if result.get("resolved_by_id") else None,
                "resolved_by_display": result.get("resolved_by_display"),
            }
            if result_conflicts(existing_result, comparable):
                con.rollback()
                raise RuntimeError("consensus_result_conflict")
            result_id = int(existing_result["id"])

        bill_update = con.execute(
            """
            UPDATE tvrs_bills
            SET status = ?, result_summary = ?,
                vetoed_by_id = COALESCE(?, vetoed_by_id),
                vetoed_by_display = COALESCE(?, vetoed_by_display), updated_at = ?
            WHERE id = ? AND guild_id = ?
            """,
            (
                str(bill_status),
                str(result_summary),
                int(result["veto_by_id"]) if result.get("veto_by_id") else None,
                result.get("veto_by_display"),
                now,
                bill_id,
                guild_id,
            ),
        )
        if bill_update.rowcount != 1:
            con.rollback()
            raise RuntimeError("consensus_bill_not_found")

        retry_bill_number = int(result.get("retry_bill_number") or 0)
        if str(result.get("status") or bill_status) == "vetoed" and retry_bill_number:
            retry_update = con.execute(
                """
                UPDATE tvrs_bills
                SET status = 'requeued', updated_at = ?
                WHERE guild_id = ? AND bill_number = ?
                  AND status IN ('pending_veto', 'requeued')
                """,
                (now, guild_id, retry_bill_number),
            )
            if retry_update.rowcount != 1:
                con.rollback()
                raise RuntimeError("consensus_retry_bill_not_found")

        new_revision = expected + 1
        stored_snapshot = dict(snapshot)
        stored_snapshot["revision"] = new_revision
        snapshot_json = json.dumps(stored_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        session_update = con.execute(
            """
            UPDATE tvrs_consensus_sessions
            SET stage = 'after_result', current_bill_id = NULL, snapshot_json = ?,
                revision = ?, updated_at = ?, finished_at = NULL
            WHERE session_key = ? AND revision = ? AND stage = 'finalizing'
              AND current_bill_id = ?
            """,
            (snapshot_json, new_revision, now, session_key, expected, bill_id),
        )
        if session_update.rowcount != 1:
            con.rollback()
            raise RuntimeError("consensus_finalization_revision_conflict")
        con.execute(
            """
            INSERT INTO tvrs_consensus_events(
                session_key, guild_id, event_type, actor_id, actor_display,
                stage_from, stage_to, details_json, created_at
            ) VALUES(?, ?, ?, ?, ?, 'finalizing', 'after_result', ?, ?)
            """,
            (
                session_key,
                guild_id,
                str(event_type)[:80],
                actor_id,
                actor_display,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        outbox_ids: list[int] = []
        for delivery in deliveries:
            row = delivery_outbox_enqueue_in_connection(
                con,
                topic=str(delivery.get("topic") or ""),
                dedupe_key=str(delivery.get("dedupe_key") or ""),
                payload=dict(delivery.get("payload") or {}),
                max_attempts=int(delivery.get("max_attempts") or 8),
                priority=int(delivery.get("priority") or 0),
                supersede_key=delivery.get("supersede_key"),
                available_at=delivery.get("available_at"),
                now=now,
            )
            outbox_ids.append(int(row["id"]))
        final_session = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        con.commit()
    return {
        "session": dict(final_session) if final_session is not None else {},
        "result_id": result_id,
        "outbox_ids": outbox_ids,
        "idempotent": False,
    }


def tvrs_consensus_commit_finish(
    snapshot: dict[str, Any],
    *,
    expected_revision: int,
    current_bill_id: int | None,
    advance_plenary: bool,
    event_type: str,
    actor_id: int | None,
    actor_display: str | None,
    details: dict[str, Any] | None = None,
    deliveries: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Atomically close a session and release any in-flight bill."""

    session_key = str(snapshot.get("session_key") or "").strip()
    guild_id = int(snapshot.get("guild_id") or 0)
    target_stage = str(snapshot.get("stage") or "")
    expected = int(expected_revision)
    if not session_key or guild_id <= 0 or expected < 1:
        raise ValueError("consensus_finish_identity_required")
    if target_stage not in {"finished", "cancelled"} or not bool(snapshot.get("finished")):
        raise ValueError("consensus_finish_snapshot_invalid")
    if snapshot.get("current_bill") is not None:
        raise ValueError("consensus_finish_bill_not_cleared")

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        persisted = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        if persisted is None:
            con.rollback()
            raise RuntimeError("consensus_session_not_persisted")
        if str(persisted["stage"] or "") == target_stage and persisted["finished_at"]:
            con.commit()
            return {"session": dict(persisted), "idempotent": True}
        if int(persisted["revision"] or 0) != expected or persisted["finished_at"]:
            con.rollback()
            raise RuntimeError("consensus_finish_revision_conflict")

        persisted_bill_id = int(persisted["current_bill_id"] or 0)
        requested_bill_id = int(current_bill_id or 0)
        if persisted_bill_id != requested_bill_id:
            con.rollback()
            raise RuntimeError("consensus_finish_bill_conflict")
        if requested_bill_id:
            bill_update = con.execute(
                """
                UPDATE tvrs_bills
                SET status = 'requeued', result_summary = ?, updated_at = ?
                WHERE id = ? AND guild_id = ? AND status IN ('voting', 'requeued')
                """,
                (
                    "Рассмотрение отложено из-за завершения консенсуса.",
                    now,
                    requested_bill_id,
                    guild_id,
                ),
            )
            if bill_update.rowcount != 1:
                con.rollback()
                raise RuntimeError("consensus_finish_bill_not_requeued")

        if advance_plenary:
            meta_key = f"tvrs_next_plenary_number:{guild_id}"
            next_number = max(1, int(snapshot.get("plenary_number") or 0) + 1)
            set_meta(con, meta_key, str(next_number))

        new_revision = expected + 1
        stored_snapshot = dict(snapshot)
        stored_snapshot["revision"] = new_revision
        snapshot_json = json.dumps(stored_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        update = con.execute(
            """
            UPDATE tvrs_consensus_sessions
            SET stage = ?, current_bill_id = NULL, snapshot_json = ?, revision = ?,
                updated_at = ?, finished_at = ?
            WHERE session_key = ? AND revision = ? AND finished_at IS NULL
              AND COALESCE(current_bill_id, 0) = ?
            """,
            (
                target_stage,
                snapshot_json,
                new_revision,
                now,
                now,
                session_key,
                expected,
                requested_bill_id,
            ),
        )
        if update.rowcount != 1:
            con.rollback()
            raise RuntimeError("consensus_finish_revision_conflict")
        con.execute(
            """
            INSERT INTO tvrs_consensus_events(
                session_key, guild_id, event_type, actor_id, actor_display,
                stage_from, stage_to, details_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_key,
                guild_id,
                str(event_type)[:80],
                actor_id,
                actor_display,
                str(persisted["stage"] or ""),
                target_stage,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        outbox_ids: list[int] = []
        for delivery in deliveries:
            outbox_row = delivery_outbox_enqueue_in_connection(
                con,
                topic=str(delivery.get("topic") or ""),
                dedupe_key=str(delivery.get("dedupe_key") or ""),
                payload=dict(delivery.get("payload") or {}),
                max_attempts=int(delivery.get("max_attempts") or 8),
                priority=int(delivery.get("priority") or 0),
                supersede_key=delivery.get("supersede_key"),
                available_at=delivery.get("available_at"),
                now=now,
            )
            outbox_ids.append(int(outbox_row["id"]))
        final_session = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        con.commit()
    return {
        "session": dict(final_session) if final_session is not None else {},
        "outbox_ids": outbox_ids,
        "idempotent": False,
    }


def tvrs_consensus_active_sessions(guild_id: int | None = None) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        if guild_id is None:
            rows = con.execute(
                """
                SELECT * FROM tvrs_consensus_sessions
                WHERE finished_at IS NULL
                ORDER BY updated_at ASC
                """
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT * FROM tvrs_consensus_sessions
                WHERE guild_id = ? AND finished_at IS NULL
                ORDER BY updated_at ASC
                """,
                (int(guild_id),),
            ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            snapshot = json.loads(str(row["snapshot_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            # Keep enough identity for the restore layer to quarantine the row.
            # Silently skipping it would leave an invisible active session in
            # the database indefinitely.
            snapshot = {
                "snapshot_version": 0,
                "session_key": str(row["session_key"]),
                "guild_id": int(row["guild_id"]),
                "stage": str(row["stage"]),
                "corrupt_snapshot_json": True,
            }
        if isinstance(snapshot, dict):
            snapshot.setdefault("engine_version", int(row["engine_version"] or 2))
            snapshot["revision"] = int(row["revision"] or 0)
            snapshot["persisted_updated_at"] = str(row["updated_at"] or "")
            result.append(snapshot)
    return result


def tvrs_consensus_events(session_key: str, limit: int = 200) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM tvrs_consensus_events
            WHERE session_key = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (str(session_key), max(1, min(int(limit), 1000))),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in reversed(rows):
        item = dict(row)
        try:
            item["details"] = json.loads(str(item.get("details_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            item["details"] = {}
        result.append(item)
    return result


def tvrs_consensus_quarantine_session(session_key: str, reason: str) -> bool:
    """Close an unreadable session so it cannot block the guild forever."""
    now = utc_now_iso()
    clean_reason = str(reason or "unreadable snapshot")[:1000]
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ? AND finished_at IS NULL",
            (str(session_key),),
        ).fetchone()
        if row is None:
            con.rollback()
            return False
        current_bill_id = int(row["current_bill_id"] or 0)
        if current_bill_id:
            current_bill = con.execute(
                "SELECT original_bill_id FROM tvrs_bills WHERE id = ?",
                (current_bill_id,),
            ).fetchone()
            root_bill_id = (
                int(current_bill["original_bill_id"] or current_bill_id)
                if current_bill is not None
                else current_bill_id
            )
            con.execute(
                """
                DELETE FROM tvrs_bills
                WHERE guild_id = ? AND status = 'pending_veto' AND original_bill_id = ?
                """,
                (int(row["guild_id"]), root_bill_id),
            )
            con.execute(
                """
                UPDATE tvrs_bills
                SET status = 'requeued', result_summary = ?, updated_at = ?
                WHERE id = ? AND status = 'voting'
                """,
                ("Сессия восстановлению не подлежит; проект возвращён в очередь.", now, current_bill_id),
            )
        con.execute(
            """
            UPDATE tvrs_consensus_sessions
            SET stage = 'cancelled', updated_at = ?, finished_at = ?
            WHERE session_key = ?
            """,
            (now, now, str(session_key)),
        )
        con.execute(
            """
            INSERT INTO tvrs_consensus_events(
                session_key, guild_id, event_type, actor_id, actor_display,
                stage_from, stage_to, details_json, created_at
            )
            VALUES(?, ?, 'session_quarantined', NULL, NULL, ?, 'cancelled', ?, ?)
            """,
            (
                str(session_key),
                int(row["guild_id"]),
                str(row["stage"]),
                json.dumps({"reason": clean_reason}, ensure_ascii=False),
                now,
            ),
        )
        con.commit()
    return True


def tvrs_bill_row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def tvrs_queue_bills(guild_id: int, limit: int = 100) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM tvrs_bills
            WHERE guild_id = ? AND status IN ('draft', 'queued', 'requeued')
            ORDER BY bill_number ASC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def tvrs_get_bill_dict_by_id(bill_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM tvrs_bills WHERE id = ?", (bill_id,)).fetchone()
    return dict(row) if row else None


def tvrs_mark_bill_status(bill_id: int, status: str, result_summary: str | None = None, veto_by_id: int | None = None, veto_by_display: str | None = None) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE tvrs_bills
            SET status = ?, result_summary = ?, vetoed_by_id = COALESCE(?, vetoed_by_id), vetoed_by_display = COALESCE(?, vetoed_by_display), updated_at = ?
            WHERE id = ?
            """,
            (status, result_summary, veto_by_id, veto_by_display, now, bill_id),
        )
        con.commit()


def tvrs_create_retry_bill(original_bill_id: int, author_id: int, author_display: str | None) -> dict[str, Any] | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        original = con.execute("SELECT * FROM tvrs_bills WHERE id = ?", (original_bill_id,)).fetchone()
        if original is None:
            con.commit()
            return None
        original_dict = dict(original)
        current_attempt = int(original_dict.get("attempt") or 1)
        if current_attempt >= 3:
            con.commit()
            return None
        guild_id = int(original_dict["guild_id"])
        root_bill_id = int(original_dict.get("original_bill_id") or original_bill_id)
        next_attempt = current_attempt + 1
        existing_retry = con.execute(
            """
            SELECT * FROM tvrs_bills
            WHERE guild_id = ? AND original_bill_id = ? AND attempt = ?
            ORDER BY id ASC LIMIT 1
            """,
            (guild_id, root_bill_id, next_attempt),
        ).fetchone()
        if existing_retry is not None:
            con.commit()
            return dict(existing_retry)
        meta_key = f"tvrs_next_bill_number:{guild_id}"
        number = _next_bill_number_in_connection(con, guild_id)
        title = str(original_dict.get("title") or "")
        summary = str(original_dict.get("summary") or "")
        retry_note = f"\n\nПовторная попытка консенсуса: {next_attempt}/3. Законопроект возвращён на рассмотрение после применения права вето."
        cur = con.execute(
            """
            INSERT INTO tvrs_bills(
                guild_id, bill_number, channel_id, message_id, author_id,
                author_display, title, summary, materials, decision_category,
                implementation_plan, leadership_actions, editor_workspace_id,
                status, created_at, updated_at, original_bill_id, attempt
            )
            VALUES(
                ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'pending_veto', ?, ?, ?, ?
            )
            """,
            (
                guild_id,
                number,
                original_dict.get("channel_id"),
                author_id,
                author_display,
                title,
                summary + retry_note,
                original_dict.get("materials"),
                "ordinary",
                original_dict.get("implementation_plan"),
                original_dict.get("leadership_actions"),
                original_dict.get("editor_workspace_id"),
                now,
                now,
                root_bill_id,
                next_attempt,
            ),
        )
        set_meta(con, meta_key, str(number + 1))
        row = con.execute("SELECT * FROM tvrs_bills WHERE id = ?", (int(cur.lastrowid),)).fetchone()
        con.commit()
    return dict(row) if row else None


def tvrs_get_next_plenary_number(guild_id: int, default_value: int = 4) -> int:
    raw = get_meta(f"tvrs_next_plenary_number:{guild_id}")
    if raw and str(raw).isdigit():
        return max(1, int(raw))
    return default_value


def tvrs_increment_plenary_number(guild_id: int, current_number: int) -> None:
    set_meta_value(f"tvrs_next_plenary_number:{guild_id}", str(max(1, int(current_number) + 1)))


def tvrs_save_live_result(
    *,
    guild_id: int,
    session_key: str,
    plenary_number: int,
    bill_id: int,
    bill_number: int,
    bill_title: str,
    status: str,
    internal_percent: float,
    overall_percent: float,
    internal_active: bool,
    votes_json: str,
    source_channel_id: int | None = None,
    source_message_id: int | None = None,
    decision_category: str = "ordinary",
    required_percent: float = 50.0,
    opposed_percent: float = 0.0,
    block_votes_json: str | None = None,
    veto_by_id: int | None = None,
    veto_by_display: str | None = None,
    resolution_method: str = "vote",
    resolution_note: str | None = None,
    resolved_by_id: int | None = None,
    resolved_by_display: str | None = None,
) -> int:
    now = utc_now_iso()
    if str(status) == "vetoed" and str(resolution_method) == "vote":
        resolution_method = "veto"
    with _db_lock, connect() as con:
        cur = con.execute(
            """
            INSERT INTO tvrs_live_results(
                guild_id, session_key, plenary_number, bill_id, bill_number,
                bill_title, status, internal_percent, overall_percent,
                internal_active, votes_json, source_channel_id,
                source_message_id, decision_category, required_percent,
                opposed_percent, block_votes_json, veto_by_id, veto_by_display,
                resolution_method, resolution_note, resolved_by_id,
                resolved_by_display, created_at
            )
            VALUES(
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(session_key, bill_id) DO UPDATE SET
                plenary_number = excluded.plenary_number,
                bill_number = excluded.bill_number,
                bill_title = excluded.bill_title,
                status = excluded.status,
                internal_percent = excluded.internal_percent,
                overall_percent = excluded.overall_percent,
                internal_active = excluded.internal_active,
                votes_json = excluded.votes_json,
                source_channel_id = excluded.source_channel_id,
                source_message_id = excluded.source_message_id,
                decision_category = excluded.decision_category,
                required_percent = excluded.required_percent,
                opposed_percent = excluded.opposed_percent,
                block_votes_json = excluded.block_votes_json,
                veto_by_id = excluded.veto_by_id,
                veto_by_display = excluded.veto_by_display,
                resolution_method = excluded.resolution_method,
                resolution_note = excluded.resolution_note,
                resolved_by_id = excluded.resolved_by_id,
                resolved_by_display = excluded.resolved_by_display
            """,
            (
                guild_id,
                session_key,
                plenary_number,
                bill_id,
                bill_number,
                bill_title,
                status,
                internal_percent,
                overall_percent,
                1 if internal_active else 0,
                votes_json,
                source_channel_id,
                source_message_id,
                str(decision_category or "ordinary"),
                float(required_percent),
                float(opposed_percent),
                block_votes_json,
                veto_by_id,
                veto_by_display,
                resolution_method,
                resolution_note,
                resolved_by_id,
                resolved_by_display,
                now,
            ),
        )
        row = con.execute(
            "SELECT id FROM tvrs_live_results WHERE session_key = ? AND bill_id = ?",
            (str(session_key), int(bill_id)),
        ).fetchone()
        con.commit()
        return int(row["id"]) if row else int(cur.lastrowid)


def tvrs_live_result_for_bill(session_key: str, bill_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM tvrs_live_results WHERE session_key = ? AND bill_id = ?",
            (str(session_key), int(bill_id)),
        ).fetchone()
    return dict(row) if row else None


# ---------------- TVRS admin helpers ----------------

def _tvrs_open_delivery_payloads(
    con: sqlite3.Connection,
    guild_id: int,
) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT payload_json FROM delivery_outbox
        WHERE topic LIKE 'tvrs.%'
          AND status IN ('pending', 'processing', 'retry')
        """
    ).fetchall()
    payloads: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and int(payload.get("guild_id") or 0) == int(guild_id):
            payloads.append(payload)
    return payloads


def _tvrs_assert_no_open_delivery(
    con: sqlite3.Connection,
    *,
    guild_id: int,
    bill_id: int | None = None,
    session_key: str | None = None,
    plenary_number: int | None = None,
) -> None:
    selected_bill_id = int(bill_id or 0)
    for payload in _tvrs_open_delivery_payloads(con, guild_id):
        payload_bill_ids = {
            int(payload.get("bill_id") or 0),
            int(dict(payload.get("bill") or {}).get("id") or 0),
            int(dict(payload.get("result") or {}).get("bill_id") or 0),
        }
        payload_bill_ids.update(
            int(dict(item).get("bill_id") or 0)
            for item in payload.get("results") or []
            if isinstance(item, dict)
        )
        if selected_bill_id > 0 and selected_bill_id in payload_bill_ids:
            raise ValueError("tvrs_delivery_pending")
        if session_key is not None and str(payload.get("session_key") or "") == str(session_key):
            raise ValueError("tvrs_delivery_pending")
        if (
            plenary_number is not None
            and int(payload.get("plenary_number") or 0) == int(plenary_number)
        ):
            raise ValueError("tvrs_delivery_pending")


def _tvrs_bill_referenced_by_active_consensus(
    con: sqlite3.Connection,
    bill_id: int,
    guild_id: int | None = None,
) -> bool:
    guild_filter = ""
    if guild_id is not None:
        guild_filter = "AND session.guild_id = ?"
    row = con.execute(
        f"""
        SELECT 1
        FROM tvrs_consensus_sessions AS session
        WHERE session.finished_at IS NULL
          {guild_filter}
          AND (
              session.current_bill_id = ?
              OR EXISTS(
                  SELECT 1
                  FROM tvrs_live_results AS result
                  WHERE result.session_key = session.session_key
                    AND result.bill_id = ?
              )
          )
        LIMIT 1
        """,
        # The optional guild predicate appears before the bill predicates.
        ([int(guild_id)] if guild_id is not None else []) + [int(bill_id), int(bill_id)],
    ).fetchone()
    return row is not None


def _tvrs_assert_bill_admin_mutable(con: sqlite3.Connection, bill: sqlite3.Row) -> None:
    bill_id = int(bill["id"])
    status = str(bill["status"] or "")
    active = _tvrs_bill_referenced_by_active_consensus(
        con,
        bill_id,
        int(bill["guild_id"]),
    )
    if status in {"voting", "pending_veto"} or active:
        raise ValueError("bill_locked_by_active_consensus")
    _tvrs_assert_no_open_delivery(
        con,
        guild_id=int(bill["guild_id"]),
        bill_id=bill_id,
    )


def _tvrs_assert_result_admin_mutable(con: sqlite3.Connection, result: sqlite3.Row) -> None:
    active = con.execute(
        """
        SELECT 1 FROM tvrs_consensus_sessions
        WHERE session_key = ? AND finished_at IS NULL
        LIMIT 1
        """,
        (str(result["session_key"]),),
    ).fetchone()
    if active is not None:
        raise ValueError("result_locked_by_active_consensus")
    _tvrs_assert_no_open_delivery(
        con,
        guild_id=int(result["guild_id"]),
        bill_id=int(result["bill_id"]),
        session_key=str(result["session_key"]),
    )

def tvrs_get_bill_by_number(guild_id: int, bill_number: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM tvrs_bills WHERE guild_id = ? AND bill_number = ?", (guild_id, bill_number)).fetchone()
    return dict(row) if row else None


def tvrs_delete_bill_by_number(
    guild_id: int,
    bill_number: int,
    actor_id: int | None = None,
    actor_display: str | None = None,
) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM tvrs_bills WHERE guild_id = ? AND bill_number = ?", (guild_id, bill_number)).fetchone()
        if row is None:
            con.commit()
            return None
        _tvrs_assert_bill_admin_mutable(con, row)
        bill = dict(row)
        bill_id = int(bill["id"])
        votes = con.execute(
            "SELECT * FROM tvrs_votes WHERE guild_id = ? AND bill_id = ?",
            (guild_id, bill_id),
        ).fetchall()
        results = con.execute(
            "SELECT * FROM tvrs_live_results WHERE guild_id = ? AND bill_id = ?",
            (guild_id, bill_id),
        ).fetchall()
        snapshots = [{"table": "tvrs_bills", "row_id": bill_id, "before": bill}]
        snapshots.extend(
            {"table": "tvrs_votes", "row_id": int(item["id"]), "before": dict(item)} for item in votes
        )
        snapshots.extend(
            {"table": "tvrs_live_results", "row_id": int(item["id"]), "before": dict(item)} for item in results
        )
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="tvrs",
            action_kind="generic_rows_restore",
            target_type="tvrs_bill",
            target_id=bill_id,
            summary=f"Удалён законопроект №{bill_number}",
            payload={"rows": snapshots},
        )
        con.execute("DELETE FROM tvrs_votes WHERE guild_id = ? AND bill_id = ?", (guild_id, bill_id))
        con.execute("DELETE FROM tvrs_live_results WHERE guild_id = ? AND bill_id = ?", (guild_id, bill_id))
        con.execute("DELETE FROM tvrs_bills WHERE id = ?", (bill_id,))
        con.commit()
    return bill


def tvrs_delete_live_result(
    result_id: int,
    guild_id: int | None = None,
    actor_id: int | None = None,
    actor_display: str | None = None,
) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if guild_id is None:
            row = con.execute("SELECT * FROM tvrs_live_results WHERE id = ?", (result_id,)).fetchone()
        else:
            row = con.execute("SELECT * FROM tvrs_live_results WHERE id = ? AND guild_id = ?", (result_id, guild_id)).fetchone()
        if row is None:
            con.commit()
            return None
        _tvrs_assert_result_admin_mutable(con, row)
        result = dict(row)
        _record_bot_action(
            con,
            guild_id=int(row["guild_id"]),
            actor_id=actor_id,
            actor_display=actor_display,
            module="tvrs",
            action_kind="generic_row_restore",
            target_type="tvrs_live_result",
            target_id=result_id,
            summary=f"Удалён итог голосования #{result_id}",
            payload={"table": "tvrs_live_results", "row_id": result_id, "before": result},
        )
        con.execute("DELETE FROM tvrs_live_results WHERE id = ?", (result_id,))
        con.commit()
    return result


def tvrs_delete_plenary_results(
    guild_id: int,
    plenary_number: int,
    actor_id: int | None = None,
    actor_display: str | None = None,
) -> int:
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute(
            "SELECT * FROM tvrs_live_results WHERE guild_id = ? AND plenary_number = ? ORDER BY id",
            (guild_id, plenary_number),
        ).fetchall()
        if any(
            con.execute(
                """
                SELECT 1 FROM tvrs_consensus_sessions
                WHERE session_key = ? AND finished_at IS NULL LIMIT 1
                """,
                (str(row["session_key"]),),
            ).fetchone()
            is not None
            for row in rows
        ):
            con.rollback()
            raise ValueError("plenary_locked_by_active_consensus")
        _tvrs_assert_no_open_delivery(
            con,
            guild_id=guild_id,
            plenary_number=plenary_number,
        )
        if rows:
            _record_bot_action(
                con,
                guild_id=guild_id,
                actor_id=actor_id,
                actor_display=actor_display,
                module="tvrs",
                action_kind="generic_rows_restore",
                target_type="tvrs_plenary",
                target_id=plenary_number,
                summary=f"Удалены итоги пленарного консенсуса №{plenary_number}",
                payload={
                    "rows": [
                        {"table": "tvrs_live_results", "row_id": int(row["id"]), "before": dict(row)}
                        for row in rows
                    ]
                },
            )
        cur = con.execute("DELETE FROM tvrs_live_results WHERE guild_id = ? AND plenary_number = ?", (guild_id, plenary_number))
        count = int(cur.rowcount or 0)
        con.commit()
    return count


def tvrs_update_bill_field(
    guild_id: int,
    bill_number: int,
    field: str,
    value: str,
    actor_id: int | None = None,
    actor_display: str | None = None,
) -> dict[str, Any] | None:
    allowed = {"title", "summary", "materials", "status", "result_summary"}
    if field not in allowed:
        raise ValueError(f"field_not_allowed:{field}")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM tvrs_bills WHERE guild_id = ? AND bill_number = ?", (guild_id, bill_number)).fetchone()
        if row is None:
            con.commit()
            return None
        _tvrs_assert_bill_admin_mutable(con, row)
        if field == "status" and str(value).strip() not in {
            "draft",
            "queued",
            "requeued",
            "accepted",
            "rejected",
            "vetoed",
        }:
            con.rollback()
            raise ValueError("bill_status_not_allowed")
        con.execute(f"UPDATE tvrs_bills SET {field} = ?, updated_at = ? WHERE guild_id = ? AND bill_number = ?", (value, now, guild_id, bill_number))
        updated = con.execute("SELECT * FROM tvrs_bills WHERE guild_id = ? AND bill_number = ?", (guild_id, bill_number)).fetchone()
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="tvrs",
            action_kind="generic_row_restore",
            target_type="tvrs_bill",
            target_id=int(row["id"]),
            summary=f"Изменено поле {field} законопроекта №{bill_number}",
            payload={"table": "tvrs_bills", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        con.commit()
    return dict(updated) if updated else None

def tvrs_recent_live_results(guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute("SELECT * FROM tvrs_live_results WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (guild_id, limit)).fetchall()
    return [dict(row) for row in rows]


__all__ = ['tvrs_next_bill_number', 'tvrs_set_next_bill_number', 'tvrs_set_last_accepted_bill_number', 'tvrs_create_bill', 'tvrs_create_bill_with_publication', 'tvrs_set_bill_message', 'tvrs_complete_bill_publication', 'tvrs_get_bill_by_id', 'tvrs_get_bill_by_message', 'tvrs_cast_vote', 'tvrs_votes_for_bill', 'tvrs_vote_counts', 'tvrs_recent_bills', 'tvrs_consensus_save_session', 'tvrs_consensus_commit_begin_bill', 'tvrs_consensus_commit_finalization', 'tvrs_consensus_commit_finish', 'tvrs_consensus_active_sessions', 'tvrs_consensus_events', 'tvrs_consensus_quarantine_session', 'tvrs_bill_row_to_dict', 'tvrs_queue_bills', 'tvrs_get_bill_dict_by_id', 'tvrs_mark_bill_status', 'tvrs_create_retry_bill', 'tvrs_get_next_plenary_number', 'tvrs_increment_plenary_number', 'tvrs_save_live_result', 'tvrs_live_result_for_bill', '_tvrs_open_delivery_payloads', '_tvrs_assert_no_open_delivery', '_tvrs_bill_referenced_by_active_consensus', '_tvrs_assert_bill_admin_mutable', '_tvrs_assert_result_admin_mutable', 'tvrs_get_bill_by_number', 'tvrs_delete_bill_by_number', 'tvrs_delete_live_result', 'tvrs_delete_plenary_results', 'tvrs_update_bill_field', 'tvrs_recent_live_results']
