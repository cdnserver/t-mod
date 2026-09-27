"""Durable, one-time Telegram ↔ T-Mod account links.

The Telegram bot is an additional client, not a second identity system.  A
link is therefore created by an authenticated Discord user and consumed with
one short-lived code.  Only a SHA-256 digest of that code is stored.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _code_hash(code: str) -> str:
    return hashlib.sha256(str(code or "").strip().upper().encode("ascii", "ignore")).hexdigest()


def _clean_code(code: str) -> str:
    return "".join(char for char in str(code or "").upper() if char in _CODE_ALPHABET)


def create_link_challenge(
    guild_id: int,
    discord_user_id: int,
    *,
    ttl_seconds: int = 600,
) -> dict[str, Any]:
    """Create a new challenge and invalidate previous challenges for a user."""

    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))
    now = datetime.now(timezone.utc)
    created_at = now.isoformat()
    expires_at = (now + timedelta(seconds=max(60, min(3600, int(ttl_seconds))))).isoformat()
    with _db_lock, connect() as con:
        con.execute(
            """
            DELETE FROM telegram_link_challenges
            WHERE guild_id = ? AND discord_user_id = ? AND consumed_at IS NULL
            """,
            (int(guild_id), int(discord_user_id)),
        )
        con.execute(
            """
            INSERT INTO telegram_link_challenges(
                code_hash, guild_id, discord_user_id, expires_at, created_at
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (_code_hash(code), int(guild_id), int(discord_user_id), expires_at, created_at),
        )
        con.commit()
    return {
        "code": code,
        "expires_at": expires_at,
        "ttl_seconds": max(60, min(3600, int(ttl_seconds))),
    }


def consume_link_challenge(
    code: str,
    *,
    guild_id: int,
    telegram_user_id: int,
    telegram_chat_id: int,
    telegram_username: str = "",
    telegram_display_name: str = "",
) -> dict[str, Any]:
    """Atomically consume a challenge and create the account binding."""

    clean_code = _clean_code(code)
    if len(clean_code) < 8:
        return {"ok": False, "error": "invalid_code"}
    now = utc_now_iso()
    with _db_lock, connect() as con:
        challenge = con.execute(
            """
            SELECT * FROM telegram_link_challenges
            WHERE code_hash = ? AND guild_id = ? AND consumed_at IS NULL
              AND expires_at > ?
            """,
            (_code_hash(clean_code), int(guild_id), now),
        ).fetchone()
        if challenge is None:
            con.rollback()
            return {"ok": False, "error": "invalid_or_expired_code"}
        owner_id = int(challenge["discord_user_id"])
        existing = con.execute(
            """
            SELECT * FROM telegram_account_links
            WHERE guild_id = ? AND telegram_user_id = ?
            """,
            (int(guild_id), int(telegram_user_id)),
        ).fetchone()
        if existing is not None and int(existing["discord_user_id"]) != owner_id:
            con.rollback()
            return {"ok": False, "error": "telegram_already_linked"}
        # A Discord account may be rebound only by issuing a fresh challenge;
        # replacing its previous Telegram identity is intentional here.
        con.execute(
            "DELETE FROM telegram_account_links WHERE guild_id = ? AND discord_user_id = ?",
            (int(guild_id), owner_id),
        )
        con.execute(
            """
            INSERT INTO telegram_account_links(
                guild_id, discord_user_id, telegram_user_id, telegram_chat_id,
                telegram_username, telegram_display_name, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(guild_id), owner_id, int(telegram_user_id), int(telegram_chat_id),
                str(telegram_username or "")[:120], str(telegram_display_name or "")[:200],
                now, now,
            ),
        )
        con.execute(
            "UPDATE telegram_link_challenges SET consumed_at = ? WHERE id = ?",
            (now, int(challenge["id"])),
        )
        con.commit()
    return {
        "ok": True,
        "guild_id": int(guild_id),
        "discord_user_id": owner_id,
        "telegram_user_id": int(telegram_user_id),
        "telegram_chat_id": int(telegram_chat_id),
    }


def get_link_by_telegram(guild_id: int, telegram_user_id: int) -> dict[str, Any] | None:
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT * FROM telegram_account_links
            WHERE guild_id = ? AND telegram_user_id = ?
            """,
            (int(guild_id), int(telegram_user_id)),
        ).fetchone()
    return dict(row) if row is not None else None


def get_link_by_discord(guild_id: int, discord_user_id: int) -> dict[str, Any] | None:
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT * FROM telegram_account_links
            WHERE guild_id = ? AND discord_user_id = ?
            """,
            (int(guild_id), int(discord_user_id)),
        ).fetchone()
    return dict(row) if row is not None else None


def unlink_by_discord(guild_id: int, discord_user_id: int) -> bool:
    with _db_lock, connect() as con:
        cursor = con.execute(
            "DELETE FROM telegram_account_links WHERE guild_id = ? AND discord_user_id = ?",
            (int(guild_id), int(discord_user_id)),
        )
        con.commit()
        return cursor.rowcount > 0


def create_telegram_login_code(
    guild_id: int,
    discord_user_id: int,
    telegram_user_id: int,
    *,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Issue a one-time web login code to an already linked Telegram identity."""

    code = f"{secrets.randbelow(100_000_000):08d}"
    now = datetime.now(timezone.utc)
    created_at = now.isoformat()
    ttl = max(60, min(600, int(ttl_seconds)))
    expires_at = (now + timedelta(seconds=ttl)).isoformat()
    code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
    with _db_lock, connect() as con:
        link = con.execute(
            """
            SELECT telegram_user_id FROM telegram_account_links
            WHERE guild_id = ? AND discord_user_id = ?
            """,
            (int(guild_id), int(discord_user_id)),
        ).fetchone()
        if link is None or int(link["telegram_user_id"]) != int(telegram_user_id):
            con.rollback()
            raise ValueError("telegram_account_not_linked")
        latest = con.execute(
            """
            SELECT created_at FROM telegram_login_codes
            WHERE guild_id = ? AND discord_user_id = ?
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (int(guild_id), int(discord_user_id)),
        ).fetchone()
        if latest is not None:
            try:
                issued_at = datetime.fromisoformat(str(latest["created_at"]))
                if issued_at.tzinfo is None:
                    issued_at = issued_at.replace(tzinfo=timezone.utc)
                seconds_since_issue = (now - issued_at.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                seconds_since_issue = 30.0
            if seconds_since_issue < 30:
                con.rollback()
                raise ValueError("telegram_login_code_rate_limited")
        con.execute(
            "DELETE FROM telegram_login_codes WHERE consumed_at IS NOT NULL OR expires_at <= ?",
            (created_at,),
        )
        con.execute(
            """
            DELETE FROM telegram_login_codes
            WHERE guild_id = ? AND discord_user_id = ? AND consumed_at IS NULL
            """,
            (int(guild_id), int(discord_user_id)),
        )
        con.execute(
            """
            INSERT INTO telegram_login_codes(
                code_hash, guild_id, discord_user_id, telegram_user_id,
                expires_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                code_hash, int(guild_id), int(discord_user_id),
                int(telegram_user_id), expires_at, created_at,
            ),
        )
        con.commit()
    return {"code": code, "expires_at": expires_at, "ttl_seconds": ttl}


def consume_telegram_login_code(
    code: str,
    *,
    guild_id: int,
) -> dict[str, Any]:
    """Consume a valid login code exactly once, while confirming its link remains active."""

    clean_code = str(code or "").strip()
    if len(clean_code) != 8 or not clean_code.isascii() or not clean_code.isdigit():
        return {"ok": False, "error": "invalid_or_expired_code"}
    now = datetime.now(timezone.utc).isoformat()
    code_hash = hashlib.sha256(clean_code.encode("ascii")).hexdigest()
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT id, discord_user_id, telegram_user_id
            FROM telegram_login_codes
            WHERE code_hash = ? AND guild_id = ? AND consumed_at IS NULL
              AND expires_at > ?
            """,
            (code_hash, int(guild_id), now),
        ).fetchone()
        if row is None:
            con.rollback()
            return {"ok": False, "error": "invalid_or_expired_code"}
        link = con.execute(
            """
            SELECT 1 FROM telegram_account_links
            WHERE guild_id = ? AND discord_user_id = ? AND telegram_user_id = ?
            """,
            (int(guild_id), int(row["discord_user_id"]), int(row["telegram_user_id"])),
        ).fetchone()
        if link is None:
            con.execute(
                "UPDATE telegram_login_codes SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now, int(row["id"])),
            )
            con.commit()
            return {"ok": False, "error": "invalid_or_expired_code"}
        consumed = con.execute(
            """
            UPDATE telegram_login_codes SET consumed_at = ?
            WHERE id = ? AND consumed_at IS NULL AND expires_at > ?
            """,
            (now, int(row["id"]), now),
        )
        if consumed.rowcount != 1:
            con.rollback()
            return {"ok": False, "error": "invalid_or_expired_code"}
        con.commit()
    return {
        "ok": True,
        "guild_id": int(guild_id),
        "discord_user_id": int(row["discord_user_id"]),
        "telegram_user_id": int(row["telegram_user_id"]),
    }


def get_atlas_thread(
    guild_id: int,
    discord_user_id: int,
    telegram_chat_id: int,
) -> dict[str, Any] | None:
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT * FROM telegram_atlas_threads
            WHERE guild_id = ? AND discord_user_id = ? AND telegram_chat_id = ?
            """,
            (int(guild_id), int(discord_user_id), int(telegram_chat_id)),
        ).fetchone()
    return dict(row) if row is not None else None


def save_atlas_thread(
    guild_id: int,
    discord_user_id: int,
    telegram_chat_id: int,
    organization_id: int,
    atlas_thread_id: int,
) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            INSERT INTO telegram_atlas_threads(
                guild_id, discord_user_id, telegram_chat_id,
                organization_id, atlas_thread_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, discord_user_id, telegram_chat_id) DO UPDATE SET
                organization_id = excluded.organization_id,
                atlas_thread_id = excluded.atlas_thread_id,
                updated_at = excluded.updated_at
            """,
            (
                int(guild_id), int(discord_user_id), int(telegram_chat_id),
                int(organization_id), int(atlas_thread_id), now, now,
            ),
        )
        con.commit()
    return {
        "guild_id": int(guild_id),
        "discord_user_id": int(discord_user_id),
        "telegram_chat_id": int(telegram_chat_id),
        "organization_id": int(organization_id),
        "atlas_thread_id": int(atlas_thread_id),
    }


def reset_atlas_thread(
    guild_id: int,
    discord_user_id: int,
    telegram_chat_id: int,
) -> bool:
    """Forget the active Telegram conversation while keeping its history.

    The Atlas thread itself is intentionally retained for audit and continuity
    in the web/Discord clients.  Removing only the Telegram pointer makes the
    next message start a clean conversation without deleting user data.
    """

    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            DELETE FROM telegram_atlas_threads
            WHERE guild_id = ? AND discord_user_id = ? AND telegram_chat_id = ?
            """,
            (int(guild_id), int(discord_user_id), int(telegram_chat_id)),
        )
        con.commit()
        return cursor.rowcount > 0


__all__ = [
    "create_link_challenge",
    "consume_link_challenge",
    "get_link_by_telegram",
    "get_link_by_discord",
    "unlink_by_discord",
    "create_telegram_login_code",
    "consume_telegram_login_code",
    "get_atlas_thread",
    "save_atlas_thread",
    "reset_atlas_thread",
]
