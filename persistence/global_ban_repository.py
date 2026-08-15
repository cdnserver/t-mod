"""Durable, audited identity blocks shared by every T-Mod surface."""

from __future__ import annotations

from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


_DISCORD_STATES = frozenset({"pending", "banned", "unbanned", "failed"})


def _row(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    result = dict(value)
    result["active"] = bool(result.get("active"))
    result["user_id_text"] = str(result.get("user_id") or "")
    return result


def get_global_ban(
    guild_id: int,
    user_id: int,
    *,
    active_only: bool = True,
) -> dict[str, Any] | None:
    if int(user_id) <= 0:
        return None
    with connect_readonly() as con:
        value = con.execute(
            """
            SELECT * FROM global_bans
            WHERE guild_id = ? AND user_id = ?
              AND (? = 0 OR active = 1)
            """,
            (int(guild_id), int(user_id), 1 if active_only else 0),
        ).fetchone()
    return _row(value)


def is_globally_banned(guild_id: int, user_id: int) -> bool:
    return get_global_ban(guild_id, user_id) is not None


def list_global_bans(
    guild_id: int,
    *,
    include_revoked: bool = True,
    limit: int = 200,
) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT * FROM global_bans
            WHERE guild_id = ? AND (? = 1 OR active = 1)
            ORDER BY active DESC, updated_at DESC, user_id
            LIMIT ?
            """,
            (
                int(guild_id),
                1 if include_revoked else 0,
                max(1, min(int(limit), 500)),
            ),
        ).fetchall()
    return [_row(item) or {} for item in rows]


def issue_global_ban(
    guild_id: int,
    user_id: int,
    *,
    reason: str,
    actor_id: int,
    actor_display: str,
) -> dict[str, Any]:
    clean_reason = str(reason or "").strip()
    clean_actor = str(actor_display or "Администратор").strip()[:200]
    if int(user_id) <= 0 or not 5 <= len(clean_reason) <= 1000:
        raise ValueError("global_ban_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            INSERT INTO global_bans(
                guild_id, user_id, active, reason,
                issued_by_id, issued_by_display, issued_at,
                discord_state, revision, updated_at
            ) VALUES(?, ?, 1, ?, ?, ?, ?, 'pending', 1, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                active = 1,
                reason = excluded.reason,
                issued_by_id = excluded.issued_by_id,
                issued_by_display = excluded.issued_by_display,
                issued_at = excluded.issued_at,
                revoked_by_id = NULL,
                revoked_by_display = NULL,
                revoked_at = NULL,
                discord_state = 'pending',
                discord_error = NULL,
                revision = global_bans.revision + 1,
                updated_at = excluded.updated_at
            """,
            (
                int(guild_id),
                int(user_id),
                clean_reason,
                int(actor_id),
                clean_actor,
                now,
                now,
            ),
        )
        # Every previously issued SSO cookie becomes unusable immediately.
        con.execute(
            """
            UPDATE web_credentials
            SET session_version = session_version + 1, updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (now, int(guild_id), int(user_id)),
        )
        con.execute(
            """
            INSERT INTO global_ban_events(
                guild_id, user_id, action, actor_id, actor_display,
                reason, discord_state, created_at
            ) VALUES(?, ?, 'issued', ?, ?, ?, 'pending', ?)
            """,
            (
                int(guild_id),
                int(user_id),
                int(actor_id),
                clean_actor,
                clean_reason,
                now,
            ),
        )
        value = con.execute(
            "SELECT * FROM global_bans WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        con.commit()
    return _row(value) or {}


def revoke_global_ban(
    guild_id: int,
    user_id: int,
    *,
    reason: str,
    actor_id: int,
    actor_display: str,
) -> dict[str, Any]:
    clean_reason = str(reason or "").strip()
    clean_actor = str(actor_display or "Администратор").strip()[:200]
    if int(user_id) <= 0 or not 3 <= len(clean_reason) <= 1000:
        raise ValueError("global_unban_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        changed = con.execute(
            """
            UPDATE global_bans
            SET active = 0, revoked_by_id = ?, revoked_by_display = ?,
                revoked_at = ?, discord_state = 'unbanned', discord_error = NULL,
                revision = revision + 1, updated_at = ?
            WHERE guild_id = ? AND user_id = ? AND active = 1
            """,
            (
                int(actor_id),
                clean_actor,
                now,
                now,
                int(guild_id),
                int(user_id),
            ),
        )
        if changed.rowcount != 1:
            con.rollback()
            raise ValueError("global_ban_not_active")
        con.execute(
            """
            INSERT INTO global_ban_events(
                guild_id, user_id, action, actor_id, actor_display,
                reason, discord_state, created_at
            ) VALUES(?, ?, 'revoked', ?, ?, ?, 'unbanned', ?)
            """,
            (
                int(guild_id),
                int(user_id),
                int(actor_id),
                clean_actor,
                clean_reason,
                now,
            ),
        )
        value = con.execute(
            "SELECT * FROM global_bans WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        con.commit()
    return _row(value) or {}


def set_global_ban_discord_state(
    guild_id: int,
    user_id: int,
    *,
    state: str,
    error: str | None,
    actor_id: int,
    actor_display: str,
) -> dict[str, Any]:
    selected = str(state or "").strip().lower()
    if selected not in _DISCORD_STATES:
        raise ValueError("global_ban_discord_state_invalid")
    clean_error = str(error or "").strip()[:1000] or None
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        changed = con.execute(
            """
            UPDATE global_bans
            SET discord_state = ?, discord_error = ?, updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (selected, clean_error, now, int(guild_id), int(user_id)),
        )
        if changed.rowcount != 1:
            con.rollback()
            raise ValueError("global_ban_not_found")
        con.execute(
            """
            INSERT INTO global_ban_events(
                guild_id, user_id, action, actor_id, actor_display,
                discord_state, discord_error, created_at
            ) VALUES(?, ?, 'discord_sync', ?, ?, ?, ?, ?)
            """,
            (
                int(guild_id),
                int(user_id),
                int(actor_id),
                str(actor_display or "T-Mod")[:200],
                selected,
                clean_error,
                now,
            ),
        )
        value = con.execute(
            "SELECT * FROM global_bans WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        con.commit()
    return _row(value) or {}


__all__ = [
    "get_global_ban",
    "is_globally_banned",
    "issue_global_ban",
    "list_global_bans",
    "revoke_global_ban",
    "set_global_ban_discord_state",
]
