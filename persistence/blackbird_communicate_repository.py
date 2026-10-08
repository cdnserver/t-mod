"""Character discovery and private Blackbird conversations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4

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
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


def _client_request(nonce: str, partner_id: int, body: str, filename: str = "", content: bytes = b"") -> tuple[str | None, str | None]:
    if not nonce:
        return None, None
    if len(nonce) != 32 or any(char not in "0123456789abcdef" for char in nonce):
        raise ValueError("communicate_nonce_invalid")
    payload = json.dumps([partner_id, body, filename, sha256(content).hexdigest() if content else ""], ensure_ascii=False, separators=(",", ":"))
    return nonce, sha256(payload.encode("utf-8")).hexdigest()


def _existing_request(con: Any, guild_id: int, user_id: int, nonce: str | None, payload_hash: str | None) -> dict[str, Any] | None:
    if not nonce:
        return None
    row = con.execute(
        """SELECT m.id,m.sender_id,m.recipient_id,m.body,m.created_at,m.client_payload_hash,
                  a.id AS attachment_id,a.filename,a.mime_type,a.byte_size
           FROM blackbird_communicate_messages m
           LEFT JOIN blackbird_communicate_attachments a ON a.message_id=m.id
           WHERE m.guild_id=? AND m.sender_id=? AND m.client_nonce=?""",
        (guild_id, user_id, nonce),
    ).fetchone()
    if row is None:
        return None
    if row["client_payload_hash"] != payload_hash:
        raise ValueError("communicate_request_conflict")
    result = _attachment_fields(row)
    result.pop("client_payload_hash", None)
    return result


def _attachment_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return "application/octet-stream"


def _attachment_fields(row: Any) -> dict[str, Any]:
    message = dict(row)
    if message.get("attachment_id"):
        message["attachment"] = {
            "id": message.pop("attachment_id"),
            "filename": message.pop("filename"),
            "mime_type": message.pop("mime_type"),
            "byte_size": message.pop("byte_size"),
        }
    else:
        for key in ("attachment_id", "filename", "mime_type", "byte_size"):
            message.pop(key, None)
        message["attachment"] = None
    return message


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
            """WITH latest AS (
                   SELECT CASE WHEN sender_id=? THEN recipient_id ELSE sender_id END AS partner_id,
                          MAX(id) AS last_id
                   FROM blackbird_communicate_messages
                   WHERE guild_id=? AND (sender_id=? OR recipient_id=?)
                   GROUP BY 1
               )
               SELECT m.id,m.sender_id,m.recipient_id,m.body,m.created_at,a.filename,latest.partner_id,
                      COALESCE((SELECT pc.nickname FROM profile_characters pc
                                WHERE pc.guild_id=m.guild_id AND pc.user_id=latest.partner_id AND pc.is_public=1
                                ORDER BY pc.position LIMIT 1), w.login_display, 'Участник Blackbird') AS partner_name
               FROM latest JOIN blackbird_communicate_messages m ON m.id=latest.last_id
               LEFT JOIN blackbird_communicate_attachments a ON a.message_id=m.id
               LEFT JOIN web_credentials w ON w.guild_id=m.guild_id AND w.user_id=latest.partner_id
               ORDER BY m.id DESC LIMIT 100""",
            (user_id, guild_id, user_id, user_id),
        ).fetchall()
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
        partner = int(row["partner_id"])
        last_message = str(row["body"] or "")
        if not last_message and row["filename"]:
            last_message = f"📎 {row['filename']}"
        partner_name = str(row["partner_name"])
        result.append({"partner_id": partner, "partner_name": partner_name, "last_message": last_message, "created_at": str(row["created_at"]), "from_me": int(row["sender_id"]) == user_id})
    return result


def conversation(guild_id: int, user_id: int, partner_id: int, *, limit: int = 80, before_id: int = 0, after_id: int = 0) -> list[dict[str, Any]]:
    if not 0 <= partner_id <= 9223372036854775807:
        raise ValueError("communicate_recipient_invalid")
    if not 0 <= before_id <= 9223372036854775807 or not 0 <= after_id <= 9223372036854775807 or (before_id and after_id):
        raise ValueError("communicate_cursor_invalid")
    if partner_id == SERVICE_PARTNER_ID:
        return [{
            "id": 0, "sender_id": SERVICE_PARTNER_ID, "recipient_id": user_id,
            "body": SERVICE_WELCOME, "created_at": "", "verified": True,
        }]
    if user_id == partner_id:
        raise ValueError("communicate_self_invalid")
    with connect_readonly() as con:
        rows = con.execute(
            f"""SELECT m.id,m.sender_id,m.recipient_id,m.body,m.created_at,
                      a.id AS attachment_id,a.filename,a.mime_type,a.byte_size
               FROM blackbird_communicate_messages m
               LEFT JOIN blackbird_communicate_attachments a ON a.message_id=m.id
               WHERE m.guild_id=?
                 AND ((m.sender_id=? AND m.recipient_id=?) OR (m.sender_id=? AND m.recipient_id=?))
                 AND (?=0 OR m.id<?)
                 AND (?=0 OR m.id>?)
               ORDER BY m.id {"ASC" if after_id else "DESC"} LIMIT ?""",
            (guild_id, user_id, partner_id, partner_id, user_id, before_id, before_id, after_id, after_id, max(1, min(limit, 100))),
        ).fetchall()
    return [_attachment_fields(row) for row in (rows if after_id else reversed(rows))]


def send_attachment_message(guild_id: int, user_id: int, partner_id: int, filename: str, data: bytes, caption: str = "", *, client_nonce: str = "") -> dict[str, Any]:
    if not 0 <= partner_id <= 9223372036854775807:
        raise ValueError("communicate_recipient_invalid")
    if user_id == partner_id or partner_id <= 0:
        raise ValueError("communicate_self_invalid")
    if not data or len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("communicate_attachment_size_invalid")
    safe_name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not safe_name or len(safe_name) > 120 or any(ord(char) < 32 for char in safe_name):
        raise ValueError("communicate_attachment_name_invalid")
    message = str(caption or "").strip()
    if len(message) > 1000 or any(ord(char) < 32 and char not in "\n\t" for char in message):
        raise ValueError("communicate_message_invalid")
    nonce, payload_hash = _client_request(client_nonce, partner_id, message, safe_name, data)
    attachment_id = uuid4().hex
    now = utc_now_iso()
    minute_ago = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    mime_type = _attachment_mime(data)
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = _existing_request(con, guild_id, user_id, nonce, payload_hash)
        if existing is not None:
            con.commit()
            return existing
        if not con.execute("SELECT 1 FROM web_credentials WHERE guild_id=? AND user_id=?", (guild_id, partner_id)).fetchone():
            raise ValueError("communicate_recipient_unavailable")
        sent = con.execute(
            "SELECT COUNT(*) AS n FROM blackbird_communicate_messages WHERE guild_id=? AND sender_id=? AND created_at>=?",
            (guild_id, user_id, minute_ago),
        ).fetchone()
        if int(sent["n"]) >= 12:
            raise ValueError("communicate_rate_limited")
        cursor = con.execute(
            """INSERT INTO blackbird_communicate_messages(guild_id,sender_id,recipient_id,body,created_at,client_nonce,client_payload_hash)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(guild_id,sender_id,client_nonce) DO NOTHING""",
            (guild_id, user_id, partner_id, message, now, nonce, payload_hash),
        )
        if cursor.rowcount == 0:
            existing = _existing_request(con, guild_id, user_id, nonce, payload_hash)
            if existing is None:
                raise ValueError("communicate_request_conflict")
            con.commit()
            return existing
        con.execute(
            """INSERT INTO blackbird_communicate_attachments
               (id,guild_id,sender_id,recipient_id,message_id,filename,mime_type,byte_size,content,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (attachment_id, guild_id, user_id, partner_id, int(cursor.lastrowid), safe_name, mime_type, len(data), data, now),
        )
        con.commit()
    return {
        "id": int(cursor.lastrowid), "sender_id": user_id, "recipient_id": partner_id,
        "body": message, "created_at": now,
        "attachment": {"id": attachment_id, "filename": safe_name, "mime_type": mime_type, "byte_size": len(data)},
    }


def read_attachment(guild_id: int, user_id: int, attachment_id: str) -> tuple[dict[str, Any], bytes] | None:
    if len(attachment_id) != 32 or any(char not in "0123456789abcdef" for char in attachment_id):
        raise ValueError("communicate_attachment_invalid")
    with connect_readonly() as con:
        row = con.execute(
            """SELECT filename,mime_type,byte_size,content FROM blackbird_communicate_attachments
               WHERE id=? AND guild_id=? AND (sender_id=? OR recipient_id=?)""",
            (attachment_id, guild_id, user_id, user_id),
        ).fetchone()
    if not row:
        return None
    data = bytes(row["content"])
    if len(data) != int(row["byte_size"]):
        raise ValueError("communicate_attachment_corrupted")
    return {key: row[key] for key in ("filename", "mime_type", "byte_size")}, data


def send_message(guild_id: int, user_id: int, partner_id: int, body: str, *, client_nonce: str = "") -> dict[str, Any]:
    if not 0 <= partner_id <= 9223372036854775807:
        raise ValueError("communicate_recipient_invalid")
    message = str(body or "").strip()
    if user_id == partner_id or partner_id <= 0:
        raise ValueError("communicate_self_invalid")
    if not message or len(message) > 1000 or any(ord(c) < 32 and c not in "\n\t" for c in message):
        raise ValueError("communicate_message_invalid")
    nonce, payload_hash = _client_request(client_nonce, partner_id, message)
    now = utc_now_iso()
    minute_ago = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = _existing_request(con, guild_id, user_id, nonce, payload_hash)
        if existing is not None:
            con.commit()
            return existing
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
            """INSERT INTO blackbird_communicate_messages(guild_id,sender_id,recipient_id,body,created_at,client_nonce,client_payload_hash)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(guild_id,sender_id,client_nonce) DO NOTHING""",
            (guild_id, user_id, partner_id, message, now, nonce, payload_hash),
        )
        if cursor.rowcount == 0:
            existing = _existing_request(con, guild_id, user_id, nonce, payload_hash)
            if existing is None:
                raise ValueError("communicate_request_conflict")
            con.commit()
            return existing
        con.commit()
    return {"id": int(cursor.lastrowid), "sender_id": user_id, "recipient_id": partner_id, "body": message, "created_at": now}
