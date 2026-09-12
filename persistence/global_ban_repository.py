"""Durable, audited identity blocks shared by every T-Mod surface."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


_DISCORD_STATES = frozenset({"pending", "banned", "unbanned", "failed"})
_DESKTOP_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_DESKTOP_FINGERPRINT_RE = re.compile(r"^[a-fA-F0-9]{64}$")


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
            WHERE user_id = ?
              AND (? = 0 OR active = 1)
            ORDER BY CASE WHEN guild_id = ? THEN 0 ELSE 1 END, updated_at DESC
            LIMIT 1
            """,
            (int(user_id), 1 if active_only else 0, int(guild_id)),
        ).fetchone()
    return _row(value)


def is_globally_banned(guild_id: int, user_id: int) -> bool:
    return get_global_ban(guild_id, user_id) is not None


def _desktop_token_hash(token: str) -> str | None:
    value = str(token or "").strip()
    if not _DESKTOP_TOKEN_RE.fullmatch(value):
        return None
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _desktop_device_hash(fingerprint: str) -> str | None:
    value = str(fingerprint or "").strip().lower()
    if not _DESKTOP_FINGERPRINT_RE.fullmatch(value):
        return None
    return hashlib.sha256(f"tmod-device-server-v1:{value}".encode("ascii")).hexdigest()


def bind_desktop_installation(
    guild_id: int,
    user_id: int,
    token: str,
    *,
    platform: str = "",
    app_version: str = "",
    device_fingerprint: str = "",
) -> dict[str, Any] | None:
    """Bind an authenticated account to a pseudonymous Desktop install.

    The bearer credential itself is intentionally never persisted.  A single
    install can contain more than one explicitly authenticated T-Mod account;
    this makes a known banned installation visible without guessing links from
    shared IP addresses.
    """

    token_hash = _desktop_token_hash(token)
    if token_hash is None or int(user_id) <= 0:
        return None
    now = utc_now_iso()
    clean_platform = str(platform or "").strip()[:80]
    clean_version = str(app_version or "").strip()[:40]
    installation_ref = f"TD-{token_hash[:12].upper()}"
    device_hash = _desktop_device_hash(device_fingerprint)
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            INSERT INTO desktop_installations(
                guild_id, token_hash, installation_ref, device_hash, platform, app_version,
                first_seen_at, last_seen_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, token_hash) DO UPDATE SET
                device_hash = COALESCE(excluded.device_hash, desktop_installations.device_hash),
                platform = excluded.platform,
                app_version = excluded.app_version,
                last_seen_at = excluded.last_seen_at
            """,
            (
                int(guild_id),
                token_hash,
                installation_ref,
                device_hash,
                clean_platform,
                clean_version,
                now,
                now,
            ),
        )
        con.execute(
            """
            INSERT INTO desktop_installation_accounts(
                guild_id, token_hash, user_id, first_seen_at, last_seen_at
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, token_hash, user_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at
            """,
            (int(guild_id), token_hash, int(user_id), now, now),
        )
        if device_hash is not None:
            con.execute(
                """
                INSERT INTO desktop_device_accounts(
                    guild_id, device_hash, user_id, first_seen_at, last_seen_at
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, device_hash, user_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
                """,
                (int(guild_id), device_hash, int(user_id), now, now),
            )
        linked = con.execute(
            """
            SELECT COUNT(*) AS account_count
            FROM desktop_installation_accounts
            WHERE guild_id = ? AND token_hash = ?
            """,
            (int(guild_id), token_hash),
        ).fetchone()
        con.commit()
    return {
        "installation_ref": installation_ref,
        "account_count": int(linked["account_count"] if linked else 1),
        "trusted": True,
        "hardware_bound": device_hash is not None,
    }


def get_desktop_installation_ban(
    guild_id: int,
    token: str,
    device_fingerprint: str = "",
) -> dict[str, Any] | None:
    """Resolve an active decision already linked to this Desktop install."""

    token_hash = _desktop_token_hash(token)
    device_hash = _desktop_device_hash(device_fingerprint)
    value = None
    with connect_readonly() as con:
        if token_hash is not None:
            value = con.execute(
                """
                SELECT gb.*, di.installation_ref
                FROM desktop_installations di
                JOIN desktop_installation_accounts dia
                  ON dia.guild_id = di.guild_id AND dia.token_hash = di.token_hash
                JOIN global_bans gb
                  ON gb.user_id = dia.user_id
                WHERE di.guild_id = ? AND di.token_hash = ? AND gb.active = 1
                ORDER BY gb.updated_at DESC
                LIMIT 1
                """,
                (int(guild_id), token_hash),
            ).fetchone()
        if value is None and device_hash is not None:
            value = con.execute(
                """
                SELECT gb.*, NULL AS installation_ref
                FROM desktop_device_accounts dda
                JOIN global_bans gb ON gb.user_id = dda.user_id
                WHERE dda.guild_id = ? AND dda.device_hash = ? AND gb.active = 1
                ORDER BY gb.updated_at DESC
                LIMIT 1
                """,
                (int(guild_id), device_hash),
            ).fetchone()
    return _row(value)


def desktop_installation_summaries(
    guild_id: int,
) -> dict[str, dict[str, Any]]:
    """Return compact, non-sensitive device counts for the admin registry."""

    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT dia.user_id, COUNT(*) AS installation_count,
                   MAX(dia.last_seen_at) AS installation_last_seen_at
            FROM desktop_installation_accounts dia
            WHERE dia.guild_id = ?
            GROUP BY dia.user_id
            """,
            (int(guild_id),),
        ).fetchall()
    return {
        str(row["user_id"]): {
            "installation_count": int(row["installation_count"] or 0),
            "installation_last_seen_at": row["installation_last_seen_at"],
        }
        for row in rows
    }


def linked_desktop_accounts(guild_id: int, user_id: int) -> list[int]:
    """Accounts explicitly authenticated on any install used by ``user_id``."""

    if int(user_id) <= 0:
        return []
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT DISTINCT peer.user_id
            FROM desktop_installation_accounts source
            JOIN desktop_installation_accounts peer
              ON peer.guild_id = source.guild_id
             AND peer.token_hash = source.token_hash
            WHERE source.guild_id = ? AND source.user_id = ?
              AND peer.user_id <> source.user_id
            ORDER BY peer.user_id
            """,
            (int(guild_id), int(user_id)),
        ).fetchall()
        device_rows = con.execute(
            """
            SELECT DISTINCT peer.user_id
            FROM desktop_device_accounts source
            JOIN desktop_device_accounts peer
              ON peer.guild_id = source.guild_id
             AND peer.device_hash = source.device_hash
            WHERE source.guild_id = ? AND source.user_id = ?
              AND peer.user_id <> source.user_id
            ORDER BY peer.user_id
            """,
            (int(guild_id), int(user_id)),
        ).fetchall()
    return sorted(
        {int(row["user_id"]) for row in rows}
        | {int(row["user_id"]) for row in device_rows}
    )


def linked_global_ban_subjects(guild_id: int, source_user_id: int) -> list[int]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT user_id FROM global_bans
            WHERE guild_id = ? AND source_user_id = ? AND active = 1
            ORDER BY user_id
            """,
            (int(guild_id), int(source_user_id)),
        ).fetchall()
    return [int(row["user_id"]) for row in rows]


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
    source_user_id: int | None = None,
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
                guild_id, user_id, active, reason, source_user_id,
                issued_by_id, issued_by_display, issued_at,
                discord_state, revision, updated_at
            ) VALUES(?, ?, 1, ?, ?, ?, ?, ?, 'pending', 1, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                active = 1,
                reason = excluded.reason,
                source_user_id = excluded.source_user_id,
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
                int(source_user_id) if source_user_id else None,
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
    "bind_desktop_installation",
    "desktop_installation_summaries",
    "get_desktop_installation_ban",
    "get_global_ban",
    "is_globally_banned",
    "issue_global_ban",
    "linked_desktop_accounts",
    "linked_global_ban_subjects",
    "list_global_bans",
    "revoke_global_ban",
    "set_global_ban_discord_state",
]
