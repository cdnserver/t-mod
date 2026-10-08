"""Browser-owned, expiring Discord pairing and insert-only account creation."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time

from persistence.core import _db_lock, connect, connect_readonly, postgres_enabled, utc_now_iso
from persistence import profile_repository as profiles
from persistence import web_auth_repository as credentials

LIFETIME = 15 * 60
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else int(now)


def _eligible(con, guild_id: int, user_id: int) -> None:
    if con.execute("SELECT 1 FROM global_bans WHERE user_id=? AND active=1 LIMIT 1", (user_id,)).fetchone():
        raise ValueError("registration_blocked")
    if con.execute("SELECT 1 FROM web_credentials WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone():
        raise ValueError("registration_account_exists")


def start_registration(guild_id: int, *, previous_token: str = "", now: int | None = None) -> dict:
    epoch = _now(now)
    browser_token = secrets.token_urlsafe(32)
    code = "".join(secrets.choice(ALPHABET) for _ in range(10))
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM account_registrations WHERE expires_at<?", (epoch - 86400,))
        if previous_token:
            con.execute("DELETE FROM account_registrations WHERE browser_hash=? AND status!='completed'", (digest(previous_token),))
        if int(con.execute("SELECT COUNT(*) AS n FROM account_registrations WHERE expires_at>?", (epoch,)).fetchone()["n"]) >= 10000:
            raise ValueError("registration_capacity")
        con.execute("INSERT INTO account_registrations(browser_hash,code_hash,guild_id,expires_at,created_at) VALUES(?,?,?,?,?)",
                    (digest(browser_token), digest(code), int(guild_id), epoch + LIFETIME, utc_now_iso()))
        con.commit()
    return {"browser_token": browser_token, "pairing_code": code, "expires_at": epoch + LIFETIME}


def registration_status(guild_id: int, browser_token: str, *, now: int | None = None) -> dict:
    with connect_readonly() as con:
        row = con.execute("SELECT * FROM account_registrations WHERE browser_hash=? AND guild_id=?",
                          (digest(browser_token), int(guild_id))).fetchone()
    if row is None or int(row["expires_at"]) <= _now(now):
        raise ValueError("registration_expired")
    return {"status": str(row["status"]), "expires_at": int(row["expires_at"]),
            "discord_name": str(row["display_name"]), "user_id": str(row["user_id"] or ""),
            "login": str(row["login_display"])}


def claim_registration(guild_id: int, user_id: int, code: str, display_name: str, *, now: int | None = None) -> None:
    clean = str(code).replace("-", "").replace(" ", "").upper()
    if len(clean) != 10 or any(letter not in ALPHABET for letter in clean):
        raise ValueError("registration_code_invalid")
    epoch = _now(now)
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        _eligible(con, int(guild_id), int(user_id))
        cursor = con.execute("""UPDATE account_registrations SET user_id=?,display_name=?,status='claimed'
            WHERE code_hash=? AND guild_id=? AND status='waiting' AND expires_at>?""",
            (int(user_id), str(display_name)[:80], digest(clean), int(guild_id), epoch))
        if cursor.rowcount != 1:
            raise ValueError("registration_code_invalid")
        con.commit()


def complete_registration(guild_id: int, browser_token: str, *, login: str, secret: str,
                          kind: str, preferred_name: str, characters: list, now: int | None = None) -> dict:
    clean_login = credentials.normalize_web_login(login)
    if kind == "pin":
        secret = credentials.normalize_web_pin(secret)
    elif kind != "password" or not isinstance(secret, str) or not 12 <= len(secret) <= 128 or secret.isspace():
        raise ValueError("password_length")
    name = profiles.normalize_profile_preferred_name(preferred_name)
    if not isinstance(characters, list) or len(characters) > 3:
        raise ValueError("registration_characters_invalid")
    cleaned = []
    for item in characters:
        if not isinstance(item, dict) or any(not isinstance(item.get(key), str) for key in ("nickname", "static_id")):
            raise ValueError("registration_characters_invalid")
        nickname = profiles.normalize_profile_nickname(item.get("nickname", ""))
        if len(nickname.split()) < 2:
            raise ValueError("profile_character_full_name_required")
        cleaned.append((nickname, profiles.normalize_profile_static(item.get("static_id", ""))))
    if len({static for _, static in cleaned}) != len(cleaned):
        raise ValueError("profile_static_taken")
    pin_hash = credentials._hash_pin(secret)
    stamp = utc_now_iso()
    try:
        with _db_lock, connect() as con:
            con.execute("BEGIN IMMEDIATE")
            locking = " FOR UPDATE" if postgres_enabled() else ""
            row = con.execute("SELECT * FROM account_registrations WHERE browser_hash=? AND guild_id=?" + locking,
                              (digest(browser_token), int(guild_id))).fetchone()
            if row is None or int(row["expires_at"]) <= _now(now):
                raise ValueError("registration_expired")
            if row["status"] == "completed":
                return {"ok": True, "login": str(row["login_display"])}
            if row["status"] != "claimed" or not row["user_id"]:
                raise ValueError("registration_discord_required")
            user_id = int(row["user_id"])
            _eligible(con, int(guild_id), user_id)
            existing = con.execute("SELECT position FROM profile_characters WHERE guild_id=? AND user_id=?",
                                   (int(guild_id), user_id)).fetchall()
            positions = {int(item["position"]) for item in existing}
            if len(positions) + len(cleaned) > 3:
                raise ValueError("profile_character_limit")
            if con.execute("SELECT 1 FROM web_credentials WHERE guild_id=? AND login_key=?", (int(guild_id), clean_login)).fetchone():
                raise ValueError("web_login_taken")
            for _, static in cleaned:
                if con.execute("SELECT 1 FROM profile_characters WHERE guild_id=? AND static_id=?", (int(guild_id), static)).fetchone():
                    raise ValueError("profile_static_taken")
            # No upsert: a concurrent /account creation must never be overwritten.
            con.execute("""INSERT INTO web_credentials(guild_id,user_id,login_key,login_display,pin_hash,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""", (int(guild_id), user_id, clean_login, clean_login, pin_hash, stamp, stamp))
            con.execute("""INSERT INTO account_security(guild_id,user_id,credential_kind) VALUES(?,?,?)
                ON CONFLICT(guild_id,user_id) DO UPDATE SET credential_kind=excluded.credential_kind""", (int(guild_id), user_id, kind))
            profiles._ensure_profile(con, int(guild_id), user_id, now=stamp)
            con.execute("UPDATE member_profiles SET preferred_name=?,updated_at=? WHERE guild_id=? AND user_id=?",
                        (name, stamp, int(guild_id), user_id))
            for nickname, static in cleaned:
                position = next(value for value in range(1, 4) if value not in positions)
                positions.add(position)
                cursor = con.execute("""INSERT INTO profile_characters(guild_id,user_id,nickname,static_id,position,is_public,created_at,updated_at)
                    VALUES(?,?,?,?,?,0,?,?)""", (int(guild_id), user_id, nickname, static, position, stamp, stamp))
                con.execute("UPDATE member_profiles SET primary_character_id=COALESCE(primary_character_id,?) WHERE guild_id=? AND user_id=?",
                            (int(cursor.lastrowid), int(guild_id), user_id))
            con.execute("UPDATE account_registrations SET status='completed',login_display=?,completed_at=? WHERE browser_hash=?",
                        (clean_login, stamp, digest(browser_token)))
            con.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("registration_conflict") from exc
    return {"ok": True, "login": clean_login}
