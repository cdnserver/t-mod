"""Durable planning records for plenary consensus sessions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from persistence.core import _db_lock, connect, utc_now_iso


SCHEDULE_STATUSES = frozenset({"scheduled", "started", "completed", "cancelled"})


def _clean_text(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    error: str,
) -> str:
    text = str(value or "").strip()
    if len(text) < minimum or len(text) > maximum:
        raise ValueError(error)
    return text


def _utc_iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("consensus_schedule_time_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("consensus_schedule_time_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat()


def get_consensus_schedule(schedule_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM tvrs_consensus_schedules WHERE id = ?",
            (int(schedule_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def get_upcoming_consensus_schedule(guild_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT * FROM tvrs_consensus_schedules
            WHERE guild_id = ? AND status = 'scheduled'
            ORDER BY scheduled_for ASC, id ASC
            LIMIT 1
            """,
            (int(guild_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def get_consensus_schedule_for_session(
    guild_id: int,
    session_key: str,
) -> dict[str, Any] | None:
    """Return the plan bound to a live or recently completed session."""

    clean_key = str(session_key or "").strip()
    if int(guild_id) <= 0 or not clean_key:
        return None
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT * FROM tvrs_consensus_schedules
            WHERE guild_id = ? AND started_session_key = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(guild_id), clean_key),
        ).fetchone()
    return dict(row) if row is not None else None


def list_consensus_schedules(
    guild_id: int,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM tvrs_consensus_schedules
            WHERE guild_id = ?
            ORDER BY scheduled_for DESC, id DESC
            LIMIT ?
            """,
            (int(guild_id), max(1, min(int(limit), 100))),
        ).fetchall()
    return [dict(row) for row in rows]


def save_consensus_schedule(
    *,
    guild_id: int,
    plenary_number: int,
    title: str,
    description: str,
    invitation_text: str,
    scheduled_for: datetime | str,
    duration_minutes: int,
    voice_channel_id: int,
    actor_id: int,
    actor_display: str | None,
    schedule_id: int | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    clean_title = _clean_text(
        title,
        minimum=3,
        maximum=100,
        error="consensus_schedule_title_invalid",
    )
    clean_description = _clean_text(
        description,
        minimum=0,
        maximum=1000,
        error="consensus_schedule_description_invalid",
    )
    clean_invitation = _clean_text(
        invitation_text,
        minimum=0,
        maximum=1500,
        error="consensus_schedule_invitation_invalid",
    )
    clean_duration = int(duration_minutes)
    if clean_duration < 15 or clean_duration > 480:
        raise ValueError("consensus_schedule_duration_invalid")
    if int(guild_id) <= 0 or int(plenary_number) <= 0 or int(voice_channel_id) <= 0:
        raise ValueError("consensus_schedule_scope_invalid")
    scheduled_iso = _utc_iso(scheduled_for)
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if schedule_id is None:
            existing = con.execute(
                """
                SELECT id FROM tvrs_consensus_schedules
                WHERE guild_id = ? AND status = 'scheduled'
                """,
                (int(guild_id),),
            ).fetchone()
            if existing is not None:
                con.rollback()
                raise ValueError("consensus_schedule_already_exists")
            cursor = con.execute(
                """
                INSERT INTO tvrs_consensus_schedules(
                    guild_id, plenary_number, title, description,
                    invitation_text, scheduled_for, initial_scheduled_for,
                    time_shift_minutes, duration_minutes,
                    voice_channel_id, created_by_id, created_by_display,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(guild_id),
                    int(plenary_number),
                    clean_title,
                    clean_description,
                    clean_invitation,
                    scheduled_iso,
                    scheduled_iso,
                    clean_duration,
                    int(voice_channel_id),
                    int(actor_id),
                    str(actor_display or "")[:200] or None,
                    now,
                    now,
                ),
            )
            selected_id = int(cursor.lastrowid)
        else:
            if expected_revision is None:
                con.rollback()
                raise ValueError("consensus_schedule_revision_required")
            existing = con.execute(
                """
                SELECT scheduled_for, initial_scheduled_for, last_rescheduled_at
                FROM tvrs_consensus_schedules
                WHERE id = ? AND guild_id = ? AND status = 'scheduled'
                  AND revision = ?
                """,
                (
                    int(schedule_id),
                    int(guild_id),
                    int(expected_revision),
                ),
            ).fetchone()
            if existing is None:
                con.rollback()
                raise ValueError("consensus_schedule_conflict")
            initial_iso = str(
                existing["initial_scheduled_for"] or existing["scheduled_for"]
            )
            initial_time = datetime.fromisoformat(initial_iso.replace("Z", "+00:00"))
            selected_time = datetime.fromisoformat(scheduled_iso.replace("Z", "+00:00"))
            shift_minutes = int(round((selected_time - initial_time).total_seconds() / 60))
            was_rescheduled = str(existing["scheduled_for"]) != scheduled_iso
            last_rescheduled_at = (
                now if was_rescheduled else existing["last_rescheduled_at"]
            )
            changed = con.execute(
                """
                UPDATE tvrs_consensus_schedules
                SET plenary_number = ?, title = ?, description = ?,
                    invitation_text = ?, scheduled_for = ?, duration_minutes = ?,
                    voice_channel_id = ?, initial_scheduled_for = ?,
                    time_shift_minutes = ?, last_rescheduled_at = ?,
                    revision = revision + 1, updated_at = ?
                WHERE id = ? AND guild_id = ? AND status = 'scheduled'
                  AND revision = ?
                """,
                (
                    int(plenary_number),
                    clean_title,
                    clean_description,
                    clean_invitation,
                    scheduled_iso,
                    clean_duration,
                    int(voice_channel_id),
                    initial_iso,
                    shift_minutes,
                    last_rescheduled_at,
                    now,
                    int(schedule_id),
                    int(guild_id),
                    int(expected_revision),
                ),
            )
            if changed.rowcount != 1:
                con.rollback()
                raise ValueError("consensus_schedule_conflict")
            selected_id = int(schedule_id)
        row = con.execute(
            "SELECT * FROM tvrs_consensus_schedules WHERE id = ?",
            (selected_id,),
        ).fetchone()
        con.commit()
    if row is None:  # pragma: no cover - guarded by the transaction
        raise RuntimeError("consensus_schedule_save_failed")
    return dict(row)


def bind_consensus_schedule_event(
    schedule_id: int,
    discord_event_id: int,
) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        changed = con.execute(
            """
            UPDATE tvrs_consensus_schedules
            SET discord_event_id = ?, updated_at = ?
            WHERE id = ? AND status = 'scheduled'
            """,
            (int(discord_event_id), now, int(schedule_id)),
        )
        if changed.rowcount != 1:
            raise ValueError("consensus_schedule_missing")
        row = con.execute(
            "SELECT * FROM tvrs_consensus_schedules WHERE id = ?",
            (int(schedule_id),),
        ).fetchone()
        con.commit()
    return dict(row)


def bind_consensus_schedule_invitation(
    schedule_id: int,
    broadcast_id: int,
) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        changed = con.execute(
            """
            UPDATE tvrs_consensus_schedules
            SET invitation_broadcast_id = ?, updated_at = ?
            WHERE id = ? AND status = 'scheduled'
            """,
            (int(broadcast_id), now, int(schedule_id)),
        )
        if changed.rowcount != 1:
            raise ValueError("consensus_schedule_missing")
        row = con.execute(
            "SELECT * FROM tvrs_consensus_schedules WHERE id = ?",
            (int(schedule_id),),
        ).fetchone()
        con.commit()
    return dict(row)


def cancel_consensus_schedule(
    schedule_id: int,
    *,
    guild_id: int,
    expected_revision: int,
) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        changed = con.execute(
            """
            UPDATE tvrs_consensus_schedules
            SET status = 'cancelled', revision = revision + 1,
                cancelled_at = ?, updated_at = ?
            WHERE id = ? AND guild_id = ? AND status = 'scheduled'
              AND revision = ?
            """,
            (now, now, int(schedule_id), int(guild_id), int(expected_revision)),
        )
        if changed.rowcount != 1:
            raise ValueError("consensus_schedule_conflict")
        row = con.execute(
            "SELECT * FROM tvrs_consensus_schedules WHERE id = ?",
            (int(schedule_id),),
        ).fetchone()
        con.commit()
    return dict(row)


def start_consensus_schedule(
    guild_id: int,
    *,
    session_key: str,
    schedule_id: int | None = None,
) -> dict[str, Any] | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        changed = con.execute(
            """
            UPDATE tvrs_consensus_schedules
            SET status = 'started', started_session_key = ?, started_at = ?,
                revision = revision + 1, updated_at = ?
            WHERE guild_id = ? AND status = 'scheduled'
              AND (? IS NULL OR id = ?)
            """,
            (
                str(session_key),
                now,
                now,
                int(guild_id),
                int(schedule_id) if schedule_id is not None else None,
                int(schedule_id) if schedule_id is not None else None,
            ),
        )
        if schedule_id is not None and changed.rowcount != 1:
            con.rollback()
            raise ValueError("consensus_schedule_not_active")
        row = con.execute(
            """
            SELECT * FROM tvrs_consensus_schedules
            WHERE guild_id = ? AND started_session_key = ?
            ORDER BY id DESC LIMIT 1
            """,
            (int(guild_id), str(session_key)),
        ).fetchone()
        con.commit()
    return dict(row) if row is not None else None


def update_started_consensus_schedule(
    *,
    guild_id: int,
    session_key: str,
    title: str,
    duration_minutes: int,
    expected_revision: int,
) -> dict[str, Any]:
    """Update the public passport of a schedule already bound to a session.

    The live consensus state remains authoritative for procedure.  This method
    only changes display metadata and uses optimistic locking so two host
    consoles cannot silently overwrite each other.
    """

    clean_key = str(session_key or "").strip()
    clean_title = _clean_text(
        title,
        minimum=3,
        maximum=100,
        error="consensus_schedule_title_invalid",
    )
    clean_duration = int(duration_minutes)
    if clean_duration < 15 or clean_duration > 480:
        raise ValueError("consensus_schedule_duration_invalid")
    if int(guild_id) <= 0 or not clean_key:
        raise ValueError("consensus_schedule_scope_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        changed = con.execute(
            """
            UPDATE tvrs_consensus_schedules
            SET title = ?, duration_minutes = ?, revision = revision + 1,
                updated_at = ?
            WHERE guild_id = ? AND started_session_key = ?
              AND status = 'started' AND revision = ?
            """,
            (
                clean_title,
                clean_duration,
                now,
                int(guild_id),
                clean_key,
                int(expected_revision),
            ),
        )
        if changed.rowcount != 1:
            con.rollback()
            raise ValueError("consensus_schedule_conflict")
        row = con.execute(
            """
            SELECT * FROM tvrs_consensus_schedules
            WHERE guild_id = ? AND started_session_key = ?
            ORDER BY id DESC LIMIT 1
            """,
            (int(guild_id), clean_key),
        ).fetchone()
        con.commit()
    if row is None:  # pragma: no cover - protected by the update predicate
        raise RuntimeError("consensus_schedule_update_failed")
    return dict(row)


def complete_consensus_schedule(
    guild_id: int,
    *,
    session_key: str,
) -> dict[str, Any] | None:
    """Project a terminal live session back onto its planning record."""

    clean_key = str(session_key or "").strip()
    if int(guild_id) <= 0 or not clean_key:
        return None
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE tvrs_consensus_schedules
            SET status = 'completed', revision = revision + 1, updated_at = ?
            WHERE guild_id = ? AND started_session_key = ? AND status = 'started'
            """,
            (now, int(guild_id), clean_key),
        )
        row = con.execute(
            """
            SELECT * FROM tvrs_consensus_schedules
            WHERE guild_id = ? AND started_session_key = ?
            ORDER BY id DESC LIMIT 1
            """,
            (int(guild_id), clean_key),
        ).fetchone()
        con.commit()
    return dict(row) if row is not None else None


__all__ = [
    "SCHEDULE_STATUSES",
    "bind_consensus_schedule_event",
    "bind_consensus_schedule_invitation",
    "cancel_consensus_schedule",
    "complete_consensus_schedule",
    "get_consensus_schedule",
    "get_consensus_schedule_for_session",
    "get_upcoming_consensus_schedule",
    "list_consensus_schedules",
    "save_consensus_schedule",
    "start_consensus_schedule",
    "update_started_consensus_schedule",
]
