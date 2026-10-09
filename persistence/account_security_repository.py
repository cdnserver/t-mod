"""Account factors, encrypted TOTP seeds and bounded single-use challenges.

TMOD_ACCOUNT_SECURITY_KEY is a persistent Fernet key kept outside the database.
Never log request bodies, enrollment seeds, OTPs, passwords or recovery codes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from pathlib import Path
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso, postgres_enabled
from persistence import web_auth_repository as credentials


def _key() -> str:
    key = os.environ.get("TMOD_ACCOUNT_SECURITY_KEY", "").strip()
    if not key and os.environ.get("TMOD_ACCOUNT_SECURITY_KEY_FILE"):
        try:
            key = Path(os.environ["TMOD_ACCOUNT_SECURITY_KEY_FILE"]).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            raise ValueError("security_key_unavailable") from None
    return key


def cipher() -> Fernet:
    try:
        return Fernet(_key().encode("ascii"))
    except (KeyError, ValueError, UnicodeError) as exc:
        raise ValueError("security_key_unavailable") from exc


def configured() -> bool:
    try:
        cipher()
        return True
    except ValueError:
        return False


def state(guild: int, user: int) -> dict:
    with connect_readonly() as con:
        row = con.execute("SELECT * FROM account_security WHERE guild_id = ? AND user_id = ?", (guild, user)).fetchone()
    return dict(row) if row else dict(credential_kind="pin", mfa_method="", security_version=1)


def session_allowed(guild: int, user: int, payload: dict, *, require_mfa: bool = True) -> bool:
    selected = state(guild, user)
    if selected["credential_kind"] == "deleted":
        return False
    version = selected["security_version"]
    if version > 1 and payload.get("av") != version:
        return False
    return not require_mfa or not selected["mfa_method"] or payload.get("mfv") == version


def totp(seed: str, step: int) -> str:
    digest = hmac.new(base64.b32decode(seed), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff
    return f"{number % 1000000:06d}"


def _hash(value: str) -> str:
    # HMAC prevents offline brute force of six-digit message codes from a DB leak.
    key = base64.urlsafe_b64decode(_key())
    if len(key) != 32:
        raise ValueError("security_key_unavailable")
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def _ensure(con, guild: int, user: int) -> None:
    con.execute("INSERT INTO account_security(guild_id, user_id) VALUES(?, ?) ON CONFLICT(guild_id,user_id) DO NOTHING", (guild, user))


def _locked(sql: str) -> str:
    # BEGIN IMMEDIATE serializes SQLite writers across processes; PostgreSQL
    # needs row locks as the Python mutex protects only one process.
    return sql + (" FOR UPDATE" if postgres_enabled() else "")


def require_credential(guild: int, user: int, password: str) -> None:
    current = credentials.get_web_credential(guild, user)
    if not current or credentials.authenticate_web_credential(guild, current.login, password).status != "ok":
        raise ValueError("current_credential_invalid")


def change_credential(guild: int, user: int, current: str, value: str, kind: str, expected_version: int | None = None) -> None:
    if kind == "pin":
        value = credentials.normalize_web_pin(value)
    elif kind == "password":
        if not 12 <= len(value) <= 128 or value.isspace():
            raise ValueError("password_length")
    else:
        raise ValueError("credential_kind_invalid")
    require_credential(guild, user, current)
    encoded = credentials._hash_pin(value)
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(_locked("SELECT pin_hash FROM web_credentials WHERE guild_id=? AND user_id=?"), (guild, user)).fetchone()
        if not row or not credentials._verify_pin(current, row["pin_hash"]):
            raise ValueError("current_credential_invalid")
        _ensure(con, guild, user)
        selected = con.execute(_locked("SELECT security_version FROM account_security WHERE guild_id=? AND user_id=?"), (guild, user)).fetchone()
        if expected_version is not None and selected["security_version"] != expected_version:
            raise ValueError("verification_invalid")
        con.execute("UPDATE web_credentials SET pin_hash=?, session_version=session_version+1, failed_attempts=0, reset_required=0, updated_at=? WHERE guild_id=? AND user_id=?", (encoded, utc_now_iso(), guild, user))
        con.execute("UPDATE account_security SET credential_kind=?,security_version=security_version+1 WHERE guild_id=? AND user_id=?", (kind, guild, user))
        con.execute("DELETE FROM account_security_challenges WHERE guild_id=? AND user_id=?", (guild, user))
        con.commit()


def begin_enrollment(guild: int, user: int, method: str, login: str) -> dict:
    cipher()
    if method not in {"totp", "discord", "telegram"}:
        raise ValueError("factor_invalid")
    selected = state(guild, user)
    if selected["mfa_method"]:
        raise ValueError("factor_already_enabled")
    seed = base64.b32encode(secrets.token_bytes(20)).decode() if method == "totp" else ""
    encrypted = cipher().encrypt(seed.encode()).decode() if seed else ""
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        _ensure(con, guild, user)
        cursor = con.execute("UPDATE account_security SET pending_method=?,pending_secret=?,pending_until=?,security_version=security_version+1 WHERE guild_id=? AND user_id=? AND mfa_method=''", (method, encrypted, int(time.time()) + 600, guild, user))
        if cursor.rowcount != 1:
            raise ValueError("factor_already_enabled")
        version = con.execute("SELECT security_version FROM account_security WHERE guild_id=? AND user_id=?", (guild, user)).fetchone()["security_version"]
        con.commit()
    return dict(method=method, secret=seed, security_version=version, uri=f"otpauth://totp/{quote('Blackbird:' + login)}?secret={seed}&issuer=Blackbird&algorithm=SHA1&digits=6&period=30" if seed else "")


def challenge(guild: int, user: int, purpose: str, method: str) -> tuple[str, str]:
    now = int(time.time())
    nonce, code = secrets.token_urlsafe(32), f"{secrets.randbelow(1000000):06d}"
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        selected = con.execute(_locked("SELECT security_version FROM account_security WHERE guild_id=? AND user_id=?"), (guild, user)).fetchone()
        con.execute("DELETE FROM account_security_challenges WHERE expires_at < ?", (now - 600,))
        recent = con.execute("SELECT COUNT(*) AS n FROM account_security_challenges WHERE guild_id=? AND user_id=? AND expires_at>?", (guild, user, now + 240)).fetchone()["n"]
        if recent >= 3:
            raise ValueError("challenge_rate_limited")
        con.execute("INSERT INTO account_security_challenges(id,guild_id,user_id,purpose,method,code_hash,security_version,expires_at) VALUES(?,?,?,?,?,?,?,?)", (nonce, guild, user, purpose, method, _hash(nonce + ':' + code), selected["security_version"] if selected else 1, now + 300))
        con.commit()
    return nonce, code


def verify(guild: int, user: int, code: str, nonce: str, purpose: str) -> bool:
    now = int(time.time())
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        selected = con.execute(_locked("SELECT * FROM account_security WHERE guild_id=? AND user_id=?"), (guild, user)).fetchone()
        item = con.execute("SELECT * FROM account_security_challenges WHERE id=? AND guild_id=? AND user_id=? AND purpose=?", (nonce, guild, user, purpose)).fetchone()
        if not selected or not item or item["consumed"] or item["expires_at"] < now or item["attempts"] >= 5 or item["security_version"] != selected["security_version"]:
            return False
        method = selected["pending_method"] if purpose == "enroll" else selected["mfa_method"]
        valid = False
        if item["method"] != method or (purpose == "enroll" and selected["pending_until"] < now):
            return False
        if method == "totp":
            encrypted = selected["pending_secret"] if purpose == "enroll" else selected["mfa_secret"]
            try:
                seed = cipher().decrypt(encrypted.encode()).decode()
            except InvalidToken:
                # A missing/rotated encryption key must not produce an HTTP
                # 500 or silently downgrade the second factor.
                raise ValueError("security_key_unavailable") from None
            for step in [now // 30 - 1, now // 30, now // 30 + 1]:
                if step > selected["last_step"] and hmac.compare_digest(totp(seed, step), code):
                    con.execute("UPDATE account_security SET last_step=? WHERE guild_id=? AND user_id=?", (step, guild, user))
                    valid = True
                    break
        else:
            valid = hmac.compare_digest(_hash(nonce + ':' + code), item["code_hash"])
        if not valid and purpose != "enroll":
            recovery = json.loads(selected["recovery_hashes"])
            hashed = _hash(code.strip().upper())
            if hashed in recovery:
                recovery.remove(hashed)
                con.execute("UPDATE account_security SET recovery_hashes=? WHERE guild_id=? AND user_id=?", (json.dumps(recovery), guild, user))
                valid = True
        con.execute("UPDATE account_security_challenges SET attempts=attempts+1, consumed=? WHERE id=?", (int(valid), nonce))
        con.commit()
        return valid


def enable(guild: int, user: int, nonce: str) -> list[str]:
    recovery = [secrets.token_hex(8).upper() for _ in range(10)]
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(_locked("SELECT user_id FROM web_credentials WHERE guild_id=? AND user_id=?"), (guild, user)).fetchone()
        proof = con.execute("SELECT * FROM account_security_challenges WHERE id=? AND guild_id=? AND user_id=? AND purpose='enroll' AND consumed=1", (nonce, guild, user)).fetchone()
        selected = con.execute(_locked("SELECT * FROM account_security WHERE guild_id=? AND user_id=?"), (guild, user)).fetchone()
        if not proof or not selected or proof["security_version"] != selected["security_version"] or proof["method"] != selected["pending_method"] or proof["expires_at"] < int(time.time()):
            raise ValueError("verification_invalid")
        con.execute("UPDATE account_security SET mfa_method=pending_method,mfa_secret=pending_secret,pending_method='',pending_secret='',pending_until=0,recovery_hashes=?,security_version=security_version+1 WHERE guild_id=? AND user_id=? AND pending_method<>''", (json.dumps([_hash(item) for item in recovery]), guild, user))
        con.execute("UPDATE web_credentials SET session_version=session_version+1 WHERE guild_id=? AND user_id=?", (guild, user))
        con.commit()
    return recovery


def disable(guild: int, user: int, expected_version: int | None = None) -> None:
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(_locked("SELECT user_id FROM web_credentials WHERE guild_id=? AND user_id=?"), (guild, user)).fetchone()
        selected = con.execute(_locked("SELECT security_version FROM account_security WHERE guild_id=? AND user_id=?"), (guild, user)).fetchone()
        if expected_version is not None and (not selected or selected["security_version"] != expected_version):
            raise ValueError("verification_invalid")
        con.execute("UPDATE account_security SET mfa_method='',mfa_secret='',recovery_hashes='[]',last_step=-1,security_version=security_version+1 WHERE guild_id=? AND user_id=?", (guild, user))
        con.execute("UPDATE web_credentials SET session_version=session_version+1 WHERE guild_id=? AND user_id=?", (guild, user))
        con.execute("DELETE FROM account_security_challenges WHERE guild_id=? AND user_id=?", (guild, user))
        con.commit()
