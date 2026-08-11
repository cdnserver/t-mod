"""Durable member profiles and game characters."""

from __future__ import annotations

import sqlite3
from datetime import date

from persistence.core import (
    MemberProfile,
    ProfileCharacter,
    _db_lock,
    connect,
    connect_readonly,
    utc_now_iso,
)


PROFILE_STATUSES = frozenset({"active", "busy", "away", "vacation"})
PROFILE_VISIBILITIES = frozenset({"members", "private"})
PROFILE_THEMES = frozenset({"indigo", "emerald", "gold", "rose"})
PROFILE_MAX_CHARACTERS = 3
PROFILE_NICKNAME_MAX_LENGTH = 48
PROFILE_STATUS_NOTE_MAX_LENGTH = 120
PROFILE_BIOGRAPHY_MAX_LENGTH = 500
PROFILE_CONTRIBUTION_MAX_LENGTH = 500
PROFILE_RESPONSIBILITIES_MAX_LENGTH = 700
PROFILE_PREFERRED_NAME_MAX_LENGTH = 24


def normalize_profile_nickname(value: str) -> str:
    nickname = " ".join(str(value or "").strip().split())
    if not 2 <= len(nickname) <= PROFILE_NICKNAME_MAX_LENGTH:
        raise ValueError("profile_nickname_invalid")
    if any(ord(character) < 32 for character in nickname):
        raise ValueError("profile_nickname_invalid")
    return nickname


def normalize_profile_static(value: str) -> str:
    static_id = str(value or "").strip()
    if static_id.startswith("#"):
        static_id = static_id[1:].strip()
    if not static_id.isascii() or not static_id.isdigit() or not 1 <= len(static_id) <= 12:
        raise ValueError("profile_static_invalid")
    # A static is a numeric identity: #00123 and 123 must not bypass uniqueness.
    return str(int(static_id))


def normalize_profile_status(value: str) -> str:
    status = str(value or "").strip().lower()
    if status not in PROFILE_STATUSES:
        raise ValueError("profile_status_invalid")
    return status


def normalize_profile_status_note(value: str | None) -> str | None:
    note = " ".join(str(value or "").strip().split())
    if len(note) > PROFILE_STATUS_NOTE_MAX_LENGTH:
        raise ValueError("profile_status_note_too_long")
    return note or None


def normalize_profile_preferred_name(value: str) -> str:
    preferred_name = " ".join(str(value or "").strip().split())
    if (
        not 2 <= len(preferred_name) <= PROFILE_PREFERRED_NAME_MAX_LENGTH
        or "|" in preferred_name
        or any(ord(character) < 32 for character in preferred_name)
    ):
        raise ValueError("profile_preferred_name_invalid")
    return preferred_name


def _normalize_directory_text(
    value: str | None,
    *,
    maximum: int,
    error: str,
    required: bool,
) -> str | None:
    text = " ".join(str(value or "").strip().split())
    if (required and len(text) < 3) or len(text) > maximum:
        raise ValueError(error)
    return text or None


def normalize_membership_since(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = date.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("profile_membership_since_invalid") from exc
    return parsed.isoformat()


def _profile_from_row(row: sqlite3.Row | None) -> MemberProfile | None:
    if row is None:
        return None
    return MemberProfile(
        guild_id=int(row["guild_id"]),
        user_id=int(row["user_id"]),
        status=str(row["status"] or "active"),
        status_note=row["status_note"],
        visibility=str(row["visibility"] or "members"),
        show_activity=bool(row["show_activity"]),
        show_availability=bool(row["show_availability"]),
        show_position=bool(row["show_position"]),
        show_characters=bool(row["show_characters"]),
        show_join_date=bool(row["show_join_date"]),
        show_directory=bool(row["show_directory"]),
        theme=str(row["theme"] or "indigo"),
        primary_character_id=(
            int(row["primary_character_id"])
            if row["primary_character_id"] is not None
            else None
        ),
        dm_notifications=bool(row["dm_notifications"]),
        dm_market=bool(row["dm_market"]),
        dm_craft=bool(row["dm_craft"]),
        dm_consensus=bool(row["dm_consensus"]),
        dm_finance=bool(row["dm_finance"]),
        dm_system=bool(row["dm_system"]),
        quiet_hours_enabled=bool(row["quiet_hours_enabled"]),
        quiet_start_minute=int(row["quiet_start_minute"] or 0),
        quiet_end_minute=int(row["quiet_end_minute"] or 0),
        biography=row["biography"],
        contribution=row["contribution"],
        responsibilities=row["responsibilities"],
        membership_since=row["membership_since"],
        preferred_name=row["preferred_name"],
        directory_completed_at=row["directory_completed_at"],
        directory_required=bool(row["directory_required"]),
        onboarding_prompted_at=row["onboarding_prompted_at"],
        onboarding_completed_at=row["onboarding_completed_at"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _character_from_row(row: sqlite3.Row | None) -> ProfileCharacter | None:
    if row is None:
        return None
    return ProfileCharacter(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        user_id=int(row["user_id"]),
        nickname=str(row["nickname"]),
        static_id=str(row["static_id"]),
        position=int(row["position"]),
        is_public=bool(row["is_public"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _ensure_profile(
    con: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    now: str,
) -> None:
    con.execute(
        """
        INSERT INTO member_profiles(guild_id, user_id, status, created_at, updated_at)
        VALUES(?, ?, 'active', ?, ?)
        ON CONFLICT(guild_id, user_id) DO NOTHING
        """,
        (int(guild_id), int(user_id), now, now),
    )


def get_member_profile(guild_id: int, user_id: int) -> MemberProfile | None:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM member_profiles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
    return _profile_from_row(row)


def get_profile_character(
    guild_id: int,
    user_id: int,
    character_id: int,
) -> ProfileCharacter | None:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT * FROM profile_characters
            WHERE id = ? AND guild_id = ? AND user_id = ?
            """,
            (int(character_id), int(guild_id), int(user_id)),
        ).fetchone()
    return _character_from_row(row)


def list_profile_characters(guild_id: int, user_id: int) -> list[ProfileCharacter]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM profile_characters
            WHERE guild_id = ? AND user_id = ?
            ORDER BY position, id
            """,
            (int(guild_id), int(user_id)),
        ).fetchall()
    return [character for row in rows if (character := _character_from_row(row)) is not None]


def get_profile_snapshot(
    guild_id: int,
    user_id: int,
) -> tuple[MemberProfile | None, list[ProfileCharacter]]:
    with connect_readonly() as con:
        profile_row = con.execute(
            "SELECT * FROM member_profiles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        character_rows = con.execute(
            """
            SELECT * FROM profile_characters
            WHERE guild_id = ? AND user_id = ?
            ORDER BY position, id
            """,
            (int(guild_id), int(user_id)),
        ).fetchall()
    return (
        _profile_from_row(profile_row),
        [
            character
            for row in character_rows
            if (character := _character_from_row(row)) is not None
        ],
    )


def profile_preferred_names(guild_id: int, user_ids: list[int] | tuple[int, ...]) -> dict[int, str]:
    clean_ids = sorted({int(user_id) for user_id in user_ids if int(user_id) > 0})
    if not clean_ids:
        return {}
    placeholders = ",".join("?" for _ in clean_ids)
    with connect_readonly() as con:
        rows = con.execute(
            f"""
            SELECT user_id, preferred_name
            FROM member_profiles
            WHERE guild_id = ? AND user_id IN ({placeholders})
              AND preferred_name IS NOT NULL AND TRIM(preferred_name) != ''
            """,
            (int(guild_id), *clean_ids),
        ).fetchall()
    return {int(row["user_id"]): str(row["preferred_name"]) for row in rows}


def set_member_profile_status(
    guild_id: int,
    user_id: int,
    status: str,
    *,
    note: str | None = None,
) -> MemberProfile:
    clean_status = normalize_profile_status(status)
    clean_note = normalize_profile_status_note(note)
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        _ensure_profile(con, guild_id, user_id, now=now)
        con.execute(
            """
            UPDATE member_profiles
            SET status = ?, status_note = ?, updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (clean_status, clean_note, now, int(guild_id), int(user_id)),
        )
        row = con.execute(
            "SELECT * FROM member_profiles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        con.commit()
    profile = _profile_from_row(row)
    if profile is None:  # pragma: no cover - protected by the transaction above
        raise RuntimeError("profile_write_failed")
    return profile


def update_member_directory(
    guild_id: int,
    user_id: int,
    *,
    biography: str,
    contribution: str,
    responsibilities: str,
    membership_since: str | None,
) -> MemberProfile:
    clean_biography = _normalize_directory_text(
        biography,
        maximum=PROFILE_BIOGRAPHY_MAX_LENGTH,
        error="profile_biography_invalid",
        required=True,
    )
    clean_contribution = _normalize_directory_text(
        contribution,
        maximum=PROFILE_CONTRIBUTION_MAX_LENGTH,
        error="profile_contribution_invalid",
        required=True,
    )
    clean_responsibilities = _normalize_directory_text(
        responsibilities,
        maximum=PROFILE_RESPONSIBILITIES_MAX_LENGTH,
        error="profile_responsibilities_invalid",
        required=True,
    )
    clean_since = normalize_membership_since(membership_since)
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        _ensure_profile(con, guild_id, user_id, now=now)
        con.execute(
            """
            UPDATE member_profiles
            SET biography = ?, contribution = ?, responsibilities = ?,
                membership_since = ?, directory_completed_at = ?,
                directory_required = CASE
                    WHEN preferred_name IS NOT NULL
                     AND TRIM(preferred_name) != ''
                     AND EXISTS(
                         SELECT 1 FROM profile_characters
                         WHERE profile_characters.guild_id = member_profiles.guild_id
                           AND profile_characters.user_id = member_profiles.user_id
                     )
                    THEN 0 ELSE directory_required
                END,
                updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (
                clean_biography,
                clean_contribution,
                clean_responsibilities,
                clean_since,
                now,
                now,
                int(guild_id),
                int(user_id),
            ),
        )
        row = con.execute(
            "SELECT * FROM member_profiles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        con.commit()
    profile = _profile_from_row(row)
    if profile is None:  # pragma: no cover
        raise RuntimeError("profile_write_failed")
    return profile


def complete_member_onboarding(
    guild_id: int,
    user_id: int,
    *,
    preferred_name: str,
    character_nickname: str | None,
    character_static: str | None,
    biography: str,
    contribution: str,
    responsibilities: str,
    membership_since: str | None,
) -> tuple[MemberProfile, list[ProfileCharacter]]:
    """Atomically finish admission and create the required first character.

    Existing characters are never overwritten: returning members only update
    their personal mandate while the established game identity stays intact.
    """

    clean_preferred_name = normalize_profile_preferred_name(preferred_name)
    clean_biography = _normalize_directory_text(
        biography,
        maximum=PROFILE_BIOGRAPHY_MAX_LENGTH,
        error="profile_biography_invalid",
        required=True,
    )
    clean_contribution = _normalize_directory_text(
        contribution,
        maximum=PROFILE_CONTRIBUTION_MAX_LENGTH,
        error="profile_contribution_invalid",
        required=True,
    )
    clean_responsibilities = _normalize_directory_text(
        responsibilities,
        maximum=PROFILE_RESPONSIBILITIES_MAX_LENGTH,
        error="profile_responsibilities_invalid",
        required=True,
    )
    clean_since = normalize_membership_since(membership_since)
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        _ensure_profile(con, guild_id, user_id, now=now)
        character_rows = con.execute(
            """
            SELECT * FROM profile_characters
            WHERE guild_id = ? AND user_id = ?
            ORDER BY position, id
            """,
            (int(guild_id), int(user_id)),
        ).fetchall()
        if not character_rows:
            clean_nickname = normalize_profile_nickname(character_nickname or "")
            if len(clean_nickname.split()) < 2:
                raise ValueError("profile_character_full_name_required")
            clean_static = normalize_profile_static(character_static or "")
            try:
                cursor = con.execute(
                    """
                    INSERT INTO profile_characters(
                        guild_id, user_id, nickname, static_id, position,
                        is_public, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, 1, 1, ?, ?)
                    """,
                    (
                        int(guild_id),
                        int(user_id),
                        clean_nickname,
                        clean_static,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                _raise_character_integrity_error(con, guild_id, clean_static)
            character_id = int(cursor.lastrowid)
            con.execute(
                """
                UPDATE member_profiles
                SET primary_character_id = COALESCE(primary_character_id, ?)
                WHERE guild_id = ? AND user_id = ?
                """,
                (character_id, int(guild_id), int(user_id)),
            )
        con.execute(
            """
            UPDATE member_profiles
            SET preferred_name = ?, biography = ?, contribution = ?,
                responsibilities = ?, membership_since = ?,
                directory_completed_at = COALESCE(directory_completed_at, ?),
                directory_required = 0,
                onboarding_completed_at = ?, updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (
                clean_preferred_name,
                clean_biography,
                clean_contribution,
                clean_responsibilities,
                clean_since,
                now,
                now,
                now,
                int(guild_id),
                int(user_id),
            ),
        )
        profile_row = con.execute(
            "SELECT * FROM member_profiles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        character_rows = con.execute(
            """
            SELECT * FROM profile_characters
            WHERE guild_id = ? AND user_id = ?
            ORDER BY position, id
            """,
            (int(guild_id), int(user_id)),
        ).fetchall()
        con.commit()
    profile = _profile_from_row(profile_row)
    if profile is None:  # pragma: no cover
        raise RuntimeError("profile_write_failed")
    return profile, [
        character
        for row in character_rows
        if (character := _character_from_row(row)) is not None
    ]


def require_member_directory(
    guild_id: int,
    user_id: int,
    *,
    prompted: bool = False,
) -> tuple[MemberProfile, bool]:
    """Require the directory card after a real admission event.

    Callers intentionally invoke this only when the Senator role is newly
    granted.  There is no startup sweep, so existing members are not made
    retroactively subject to the requirement.
    """

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT * FROM member_profiles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        _ensure_profile(con, guild_id, user_id, now=now)
        newly_required = existing is None or not bool(existing["directory_required"])
        if newly_required:
            con.execute(
                """
                UPDATE member_profiles
                SET directory_required = 1,
                    onboarding_prompted_at = ?,
                    updated_at = ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (
                    now if prompted else None,
                    now,
                    int(guild_id),
                    int(user_id),
                ),
            )
        elif prompted and existing["onboarding_prompted_at"] is None:
            con.execute(
                """
                UPDATE member_profiles
                SET onboarding_prompted_at = ?, updated_at = ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (now, now, int(guild_id), int(user_id)),
            )
        row = con.execute(
            "SELECT * FROM member_profiles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        con.commit()
    profile = _profile_from_row(row)
    if profile is None:  # pragma: no cover
        raise RuntimeError("profile_write_failed")
    return profile, newly_required


_PREFERENCE_UNSET = object()


def update_member_profile_preferences(
    guild_id: int,
    user_id: int,
    *,
    visibility: str | None = None,
    show_activity: bool | None = None,
    show_availability: bool | None = None,
    show_position: bool | None = None,
    show_characters: bool | None = None,
    show_join_date: bool | None = None,
    show_directory: bool | None = None,
    theme: str | None = None,
    primary_character_id: int | None | object = _PREFERENCE_UNSET,
    dm_notifications: bool | None = None,
    dm_market: bool | None = None,
    dm_craft: bool | None = None,
    dm_consensus: bool | None = None,
    dm_finance: bool | None = None,
    dm_system: bool | None = None,
    quiet_hours_enabled: bool | None = None,
    quiet_start_minute: int | None = None,
    quiet_end_minute: int | None = None,
) -> MemberProfile:
    assignments: list[str] = []
    values: list[object] = []
    if visibility is not None:
        clean_visibility = str(visibility).strip().lower()
        if clean_visibility not in PROFILE_VISIBILITIES:
            raise ValueError("profile_visibility_invalid")
        assignments.append("visibility = ?")
        values.append(clean_visibility)
    boolean_preferences = {
        "show_activity": show_activity,
        "show_availability": show_availability,
        "show_position": show_position,
        "show_characters": show_characters,
        "show_join_date": show_join_date,
        "show_directory": show_directory,
        "dm_notifications": dm_notifications,
        "dm_market": dm_market,
        "dm_craft": dm_craft,
        "dm_consensus": dm_consensus,
        "dm_finance": dm_finance,
        "dm_system": dm_system,
        "quiet_hours_enabled": quiet_hours_enabled,
    }
    for column, preference in boolean_preferences.items():
        if preference is None:
            continue
        if not isinstance(preference, bool):
            raise ValueError("profile_boolean_preference_invalid")
        assignments.append(f"{column} = ?")
        values.append(1 if preference else 0)
    if theme is not None:
        clean_theme = str(theme).strip().lower()
        if clean_theme not in PROFILE_THEMES:
            raise ValueError("profile_theme_invalid")
        assignments.append("theme = ?")
        values.append(clean_theme)
    for column, minute in {
        "quiet_start_minute": quiet_start_minute,
        "quiet_end_minute": quiet_end_minute,
    }.items():
        if minute is None:
            continue
        try:
            clean_minute = int(minute)
        except (TypeError, ValueError) as exc:
            raise ValueError("profile_quiet_hours_invalid") from exc
        if isinstance(minute, bool) or not 0 <= clean_minute < 1440:
            raise ValueError("profile_quiet_hours_invalid")
        assignments.append(f"{column} = ?")
        values.append(clean_minute)

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        _ensure_profile(con, guild_id, user_id, now=now)
        current = con.execute(
            "SELECT * FROM member_profiles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        if current is None:  # pragma: no cover
            raise RuntimeError("profile_write_failed")
        final_quiet_enabled = (
            quiet_hours_enabled
            if quiet_hours_enabled is not None
            else bool(current["quiet_hours_enabled"])
        )
        final_quiet_start = (
            int(quiet_start_minute)
            if quiet_start_minute is not None
            else int(current["quiet_start_minute"])
        )
        final_quiet_end = (
            int(quiet_end_minute)
            if quiet_end_minute is not None
            else int(current["quiet_end_minute"])
        )
        if final_quiet_enabled and final_quiet_start == final_quiet_end:
            raise ValueError("profile_quiet_hours_invalid")
        if primary_character_id is not _PREFERENCE_UNSET:
            clean_character_id = None
            if primary_character_id is not None:
                try:
                    clean_character_id = int(primary_character_id)
                except (TypeError, ValueError) as exc:
                    raise ValueError("profile_primary_character_invalid") from exc
                character = con.execute(
                    """
                    SELECT 1 FROM profile_characters
                    WHERE id = ? AND guild_id = ? AND user_id = ?
                    """,
                    (clean_character_id, int(guild_id), int(user_id)),
                ).fetchone()
                if character is None:
                    raise ValueError("profile_primary_character_invalid")
            assignments.append("primary_character_id = ?")
            values.append(clean_character_id)
        if assignments:
            assignments.append("updated_at = ?")
            values.append(now)
            con.execute(
                f"UPDATE member_profiles SET {', '.join(assignments)} "
                "WHERE guild_id = ? AND user_id = ?",
                (*values, int(guild_id), int(user_id)),
            )
        row = con.execute(
            "SELECT * FROM member_profiles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        con.commit()
    profile = _profile_from_row(row)
    if profile is None:  # pragma: no cover - protected by the transaction above
        raise RuntimeError("profile_write_failed")
    return profile


def _raise_character_integrity_error(
    con: sqlite3.Connection,
    guild_id: int,
    static_id: str,
) -> None:
    owner = con.execute(
        "SELECT 1 FROM profile_characters WHERE guild_id = ? AND static_id = ?",
        (int(guild_id), static_id),
    ).fetchone()
    if owner is not None:
        raise ValueError("profile_static_taken")
    raise ValueError("profile_character_conflict")


def add_profile_character(
    guild_id: int,
    user_id: int,
    nickname: str,
    static_id: str,
) -> ProfileCharacter:
    clean_nickname = normalize_profile_nickname(nickname)
    clean_static = normalize_profile_static(static_id)
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        _ensure_profile(con, guild_id, user_id, now=now)
        rows = con.execute(
            """
            SELECT position FROM profile_characters
            WHERE guild_id = ? AND user_id = ? ORDER BY position
            """,
            (int(guild_id), int(user_id)),
        ).fetchall()
        used_positions = {int(row["position"]) for row in rows}
        if len(used_positions) >= PROFILE_MAX_CHARACTERS:
            raise ValueError("profile_character_limit")
        position = next(
            value for value in range(1, PROFILE_MAX_CHARACTERS + 1) if value not in used_positions
        )
        try:
            cursor = con.execute(
                """
                INSERT INTO profile_characters(
                    guild_id, user_id, nickname, static_id, position, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(guild_id),
                    int(user_id),
                    clean_nickname,
                    clean_static,
                    position,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            _raise_character_integrity_error(con, guild_id, clean_static)
        row = con.execute(
            "SELECT * FROM profile_characters WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
        con.execute(
            """
            UPDATE member_profiles
            SET primary_character_id = COALESCE(primary_character_id, ?), updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (int(cursor.lastrowid), now, int(guild_id), int(user_id)),
        )
        con.commit()
    character = _character_from_row(row)
    if character is None:  # pragma: no cover - protected by the transaction above
        raise RuntimeError("profile_character_write_failed")
    return character


def update_profile_character(
    guild_id: int,
    user_id: int,
    character_id: int,
    nickname: str,
    static_id: str,
) -> ProfileCharacter:
    clean_nickname = normalize_profile_nickname(nickname)
    clean_static = normalize_profile_static(static_id)
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            """
            SELECT id FROM profile_characters
            WHERE id = ? AND guild_id = ? AND user_id = ?
            """,
            (int(character_id), int(guild_id), int(user_id)),
        ).fetchone()
        if existing is None:
            raise ValueError("profile_character_not_found")
        try:
            con.execute(
                """
                UPDATE profile_characters
                SET nickname = ?, static_id = ?, updated_at = ?
                WHERE id = ? AND guild_id = ? AND user_id = ?
                """,
                (
                    clean_nickname,
                    clean_static,
                    now,
                    int(character_id),
                    int(guild_id),
                    int(user_id),
                ),
            )
        except sqlite3.IntegrityError:
            _raise_character_integrity_error(con, guild_id, clean_static)
        row = con.execute(
            "SELECT * FROM profile_characters WHERE id = ?",
            (int(character_id),),
        ).fetchone()
        con.commit()
    character = _character_from_row(row)
    if character is None:  # pragma: no cover
        raise RuntimeError("profile_character_write_failed")
    return character


def set_profile_character_visibility(
    guild_id: int,
    user_id: int,
    character_id: int,
    *,
    is_public: bool,
) -> ProfileCharacter:
    if not isinstance(is_public, bool):
        raise ValueError("profile_character_visibility_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cursor = con.execute(
            """
            UPDATE profile_characters
            SET is_public = ?, updated_at = ?
            WHERE id = ? AND guild_id = ? AND user_id = ?
            """,
            (
                1 if is_public else 0,
                now,
                int(character_id),
                int(guild_id),
                int(user_id),
            ),
        )
        if cursor.rowcount <= 0:
            con.rollback()
            raise ValueError("profile_character_not_found")
        row = con.execute(
            "SELECT * FROM profile_characters WHERE id = ?",
            (int(character_id),),
        ).fetchone()
        con.commit()
    character = _character_from_row(row)
    if character is None:  # pragma: no cover
        raise RuntimeError("profile_character_write_failed")
    return character


def delete_profile_character(guild_id: int, user_id: int, character_id: int) -> bool:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cursor = con.execute(
            """
            DELETE FROM profile_characters
            WHERE id = ? AND guild_id = ? AND user_id = ?
            """,
            (int(character_id), int(guild_id), int(user_id)),
        )
        if cursor.rowcount <= 0:
            raise ValueError("profile_character_not_found")
        profile_row = con.execute(
            """
            SELECT primary_character_id FROM member_profiles
            WHERE guild_id = ? AND user_id = ?
            """,
            (int(guild_id), int(user_id)),
        ).fetchone()
        rows = con.execute(
            """
            SELECT id FROM profile_characters
            WHERE guild_id = ? AND user_id = ? ORDER BY position, id
            """,
            (int(guild_id), int(user_id)),
        ).fetchall()
        for position, row in enumerate(rows, start=1):
            con.execute(
                "UPDATE profile_characters SET position = ?, updated_at = ? WHERE id = ?",
                (position, now, int(row["id"])),
            )
        current_primary = profile_row["primary_character_id"] if profile_row is not None else None
        if current_primary is not None and int(current_primary) == int(character_id):
            next_primary = int(rows[0]["id"]) if rows else None
            con.execute(
                """
                UPDATE member_profiles SET primary_character_id = ?, updated_at = ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (next_primary, now, int(guild_id), int(user_id)),
            )
        else:
            con.execute(
                """
                UPDATE member_profiles SET updated_at = ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (now, int(guild_id), int(user_id)),
            )
        con.commit()
    return True


__all__ = [
    "PROFILE_STATUSES",
    "PROFILE_VISIBILITIES",
    "PROFILE_THEMES",
    "PROFILE_MAX_CHARACTERS",
    "PROFILE_NICKNAME_MAX_LENGTH",
    "PROFILE_STATUS_NOTE_MAX_LENGTH",
    "PROFILE_BIOGRAPHY_MAX_LENGTH",
    "PROFILE_CONTRIBUTION_MAX_LENGTH",
    "PROFILE_RESPONSIBILITIES_MAX_LENGTH",
    "PROFILE_PREFERRED_NAME_MAX_LENGTH",
    "normalize_profile_nickname",
    "normalize_profile_static",
    "normalize_profile_status",
    "normalize_profile_status_note",
    "normalize_profile_preferred_name",
    "normalize_membership_since",
    "get_member_profile",
    "get_profile_character",
    "list_profile_characters",
    "get_profile_snapshot",
    "profile_preferred_names",
    "set_member_profile_status",
    "update_member_directory",
    "complete_member_onboarding",
    "require_member_directory",
    "update_member_profile_preferences",
    "add_profile_character",
    "update_profile_character",
    "set_profile_character_visibility",
    "delete_profile_character",
]
