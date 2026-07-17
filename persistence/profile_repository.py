"""Durable member profiles and game characters."""

from __future__ import annotations

import sqlite3

from persistence.core import (
    MemberProfile,
    ProfileCharacter,
    _db_lock,
    connect,
    utc_now_iso,
)


PROFILE_STATUSES = frozenset({"active", "busy", "away", "vacation"})
PROFILE_VISIBILITIES = frozenset({"members", "private"})
PROFILE_THEMES = frozenset({"indigo", "emerald", "gold", "rose"})
PROFILE_MAX_CHARACTERS = 3
PROFILE_NICKNAME_MAX_LENGTH = 48
PROFILE_STATUS_NOTE_MAX_LENGTH = 120


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
        theme=str(row["theme"] or "indigo"),
        primary_character_id=(
            int(row["primary_character_id"])
            if row["primary_character_id"] is not None
            else None
        ),
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
    with _db_lock, connect() as con:
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


_PREFERENCE_UNSET = object()


def update_member_profile_preferences(
    guild_id: int,
    user_id: int,
    *,
    visibility: str | None = None,
    show_activity: bool | None = None,
    theme: str | None = None,
    primary_character_id: int | None | object = _PREFERENCE_UNSET,
) -> MemberProfile:
    assignments: list[str] = []
    values: list[object] = []
    if visibility is not None:
        clean_visibility = str(visibility).strip().lower()
        if clean_visibility not in PROFILE_VISIBILITIES:
            raise ValueError("profile_visibility_invalid")
        assignments.append("visibility = ?")
        values.append(clean_visibility)
    if show_activity is not None:
        if not isinstance(show_activity, bool):
            raise ValueError("profile_show_activity_invalid")
        assignments.append("show_activity = ?")
        values.append(1 if show_activity else 0)
    if theme is not None:
        clean_theme = str(theme).strip().lower()
        if clean_theme not in PROFILE_THEMES:
            raise ValueError("profile_theme_invalid")
        assignments.append("theme = ?")
        values.append(clean_theme)

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        _ensure_profile(con, guild_id, user_id, now=now)
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
    "normalize_profile_nickname",
    "normalize_profile_static",
    "normalize_profile_status",
    "normalize_profile_status_note",
    "get_member_profile",
    "get_profile_character",
    "list_profile_characters",
    "get_profile_snapshot",
    "set_member_profile_status",
    "update_member_profile_preferences",
    "add_profile_character",
    "update_profile_character",
    "delete_profile_character",
]
