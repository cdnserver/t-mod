"""Character discovery and private Blackbird conversations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso
from persistence.profile_repository import normalize_profile_static


# Reserved identity: no user can send as the service or reply to it. The
# welcome message is a projection, not a forged row in private message storage.
SERVICE_PARTNER_ID = 0
SERVICE_WELCOME = (
    "Добро пожаловать в Blackbird! Здесь можно общаться с участниками Товарищества "
    "и делиться страницами сервисов. Найдите человека по персонажу и серверу, "
    "или по точному логину T-Mod / Discord ID, чтобы начать личный диалог."
)


def preferences(guild_id: int, user_id: int) -> dict[str, bool]:
    with connect_readonly() as con:
        row = con.execute(
            "SELECT discoverable FROM blackbird_communicate_preferences WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
    return {"discoverable": bool(row["discoverable"]) if row else True}


def set_discoverable(guild_id: int, user_id: int, value: bool) -> dict[str, bool]:
    if not isinstance(value, bool):
        raise ValueError("communicate_discoverable_invalid")
    with _db_lock, connect() as con:
        con.execute(
            """INSERT INTO blackbird_communicate_preferences(guild_id,user_id,discoverable,updated_at)
               VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET
               discoverable=excluded.discoverable,updated_at=excluded.updated_at""",
            (guild_id, user_id, 1 if value else 0, utc_now_iso()),
        )
        con.commit()
    return {"discoverable": value}


def search_character(guild_id: int, viewer_id: int, server_code: str, static_id: str) -> dict[str, Any] | None:
    server = str(server_code or "").strip().lower()
    static = normalize_profile_static(static_id)
    if not server or len(server) > 50:
        raise ValueError("communicate_server_invalid")
    with connect_readonly() as con:
        row = con.execute(
            """SELECT pc.user_id, pc.id AS character_id, pc.nickname, pc.static_id, b.server_code
               FROM profile_characters pc
               JOIN atlas_character_bindings b ON b.guild_id=pc.guild_id
                    AND b.user_id=pc.user_id AND b.character_id=pc.id
               JOIN web_credentials w ON w.guild_id=pc.guild_id AND w.user_id=pc.user_id
               LEFT JOIN blackbird_communicate_preferences p ON p.guild_id=pc.guild_id
                    AND p.user_id=pc.user_id
               WHERE pc.guild_id=? AND pc.static_id=? AND b.server_code=?
                 AND pc.is_public=1 AND pc.user_id!=? AND b.assignment_status!='revoked'
                 AND COALESCE(p.discoverable, 1)=1
               LIMIT 1""",
            (guild_id, static, server, viewer_id),
        ).fetchone()
    return dict(row) if row else None


def search_account(guild_id: int, viewer_id: int, query: str) -> dict[str, Any] | None:
    """Find any registered account by exact login or Discord ID.

    The discovery switch controls character-based search only. It must not
    silently make a registered user unreachable by someone who knows their
    exact account identity.
    """
    key = str(query or "").strip().lower()
    if not key or len(key) > 32 or not all(c.isascii() and (c.isalnum() or c in "._-") for c in key):
        raise ValueError("communicate_account_invalid")
    with connect_readonly() as con:
        row = con.execute(
            """SELECT w.user_id, COALESCE(pc.nickname, w.login_display) AS nickname,
                      w.login_display AS login, COALESCE(pc.static_id, '') AS static_id,
                      COALESCE(b.server_code, '') AS server_code
               FROM web_credentials w
               LEFT JOIN profile_characters pc ON pc.guild_id=w.guild_id
                    AND pc.user_id=w.user_id AND pc.position=1 AND pc.is_public=1
               LEFT JOIN atlas_character_bindings b ON b.guild_id=pc.guild_id
                    AND b.user_id=pc.user_id AND b.character_id=pc.id
               WHERE w.guild_id=? AND (w.login_key=? OR CAST(w.user_id AS TEXT)=?)
                 AND w.user_id!=?
               LIMIT 1""",
            (guild_id, key, key, viewer_id),
        ).fetchone()
    return dict(row) if row else None


def list_conversations(guild_id: int, user_id: int) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """SELECT id,sender_id,recipient_id,body,created_at FROM blackbird_communicate_messages
               WHERE guild_id=? AND (sender_id=? OR recipient_id=?) ORDER BY id DESC LIMIT 200""",
            (guild_id, user_id, user_id),
        ).fetchall()
    seen: set[int] = set()
    result = [{
        "partner_id": SERVICE_PARTNER_ID,
        "partner_name": "Товарищество",
        "last_message": SERVICE_WELCOME,
        "created_at": "",
        "from_me": False,
        "verified": True,
        "system": True,
    }]
    for row in rows:
        partner = int(row["recipient_id"] if row["sender_id"] == user_id else row["sender_id"])
        if partner in seen:
            continue
        seen.add(partner)
        with connect_readonly() as con:
            character = con.execute(
                "SELECT nickname FROM profile_characters WHERE guild_id=? AND user_id=? ORDER BY position LIMIT 1",
                (guild_id, partner),
            ).fetchone()
        result.append({"partner_id": partner, "partner_name": str(character["nickname"]) if character else "Участник Blackbird", "last_message": str(row["body"]), "created_at": str(row["created_at"]), "from_me": int(row["sender_id"]) == user_id})
    return result


def conversation(guild_id: int, user_id: int, partner_id: int, *, limit: int = 80) -> list[dict[str, Any]]:
    if partner_id == SERVICE_PARTNER_ID:
        return [{
            "id": 0, "sender_id": SERVICE_PARTNER_ID, "recipient_id": user_id,
            "body": SERVICE_WELCOME, "created_at": "", "verified": True,
        }]
    if user_id == partner_id:
        raise ValueError("communicate_self_invalid")
    with connect_readonly() as con:
        rows = con.execute(
            """SELECT id,sender_id,recipient_id,body,created_at
               FROM blackbird_communicate_messages WHERE guild_id=?
                 AND ((sender_id=? AND recipient_id=?) OR (sender_id=? AND recipient_id=?))
               ORDER BY id DESC LIMIT ?""",
            (guild_id, user_id, partner_id, partner_id, user_id, max(1, min(limit, 100))),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def send_message(guild_id: int, user_id: int, partner_id: int, body: str) -> dict[str, Any]:
    message = str(body or "").strip()
    if user_id == partner_id or partner_id <= 0:
        raise ValueError("communicate_self_invalid")
    if not message or len(message) > 1000 or any(ord(c) < 32 and c not in "\n\t" for c in message):
        raise ValueError("communicate_message_invalid")
    now = utc_now_iso()
    minute_ago = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        target = con.execute(
            "SELECT 1 FROM web_credentials WHERE guild_id=? AND user_id=?",
            (guild_id, partner_id),
        ).fetchone()
        if not target:
            raise ValueError("communicate_recipient_unavailable")
        sent = con.execute(
            "SELECT COUNT(*) AS n FROM blackbird_communicate_messages WHERE guild_id=? AND sender_id=? AND created_at>=?",
            (guild_id, user_id, minute_ago),
        ).fetchone()
        if int(sent["n"]) >= 12:
            raise ValueError("communicate_rate_limited")
        cursor = con.execute(
            "INSERT INTO blackbird_communicate_messages(guild_id,sender_id,recipient_id,body,created_at) VALUES(?,?,?,?,?)",
            (guild_id, user_id, partner_id, message, now),
        )
        con.commit()
    return {"id": int(cursor.lastrowid), "sender_id": user_id, "recipient_id": partner_id, "body": message, "created_at": now}
