"""Opt-in character discovery and private Blackbird conversations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso
from persistence.profile_repository import normalize_profile_static


def preferences(guild_id: int, user_id: int) -> dict[str, bool]:
    with connect_readonly() as con:
        row = con.execute(
            "SELECT discoverable FROM blackbird_communicate_preferences WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
    return {"discoverable": bool(row["discoverable"]) if row else False}


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
               JOIN blackbird_communicate_preferences p ON p.guild_id=pc.guild_id
                    AND p.user_id=pc.user_id AND p.discoverable=1
               WHERE pc.guild_id=? AND pc.static_id=? AND b.server_code=?
                 AND pc.is_public=1 AND pc.user_id!=? AND b.assignment_status!='revoked'
               LIMIT 1""",
            (guild_id, static, server, viewer_id),
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
    result = []
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
            "SELECT discoverable FROM blackbird_communicate_preferences WHERE guild_id=? AND user_id=?",
            (guild_id, partner_id),
        ).fetchone()
        if not target or not bool(target["discoverable"]):
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
