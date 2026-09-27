"""Durable password credentials for the self-hosted T-Mod web portal."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


_LOGIN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
_PIN_PATTERN = re.compile(r"^[0-9]{8}$")
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_MAX_FAILURES = 3
_DUMMY_HASH: str | None = None
WEB_GRANTABLE_SECTIONS = frozenset(
    {
        "overview",
        "modules",
        "audit",
        "treasury",
        "craft",
        "market",
        "bills",
        "sgl",
        "members",
        "communications",
        "media",
        "profile",
        "discord",
        "system",
        "atlas",
        "atlas_ai",
        "minecraft",
        "ovr",
    }
)


@dataclass(frozen=True, slots=True)
class WebCredential:
    guild_id: int
    user_id: int
    login: str
    session_version: int
    failed_attempts: int
    reset_required: bool
    last_login_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class WebAuthenticationResult:
    status: str
    credential: WebCredential | None = None
    user_id: int | None = None
    failed_attempts: int = 0
    notify_owner: bool = False


def normalize_web_login(value: str) -> str:
    login = str(value or "").strip().lower()
    if not _LOGIN_PATTERN.fullmatch(login):
        raise ValueError("web_login_invalid")
    return login


def normalize_web_pin(value: str) -> str:
    pin = str(value or "").strip()
    if not _PIN_PATTERN.fullmatch(pin):
        raise ValueError("web_pin_invalid")
    return pin


def _hash_pin(pin: str, *, salt: bytes | None = None) -> str:
    selected_salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        pin.encode("utf-8"),
        salt=selected_salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.urlsafe_b64encode(selected_salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _hash_pin("00000000", salt=b"T-Mod-web-dummy!")
    return _DUMMY_HASH


def _verify_pin(pin: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = str(encoded).split("$", 5)
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            str(pin).encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(
            base64.urlsafe_b64encode(digest).decode("ascii"),
            expected,
        )
    except (binascii.Error, TypeError, ValueError):
        return False


def _credential_from_row(row: sqlite3.Row | None) -> WebCredential | None:
    if row is None:
        return None
    return WebCredential(
        guild_id=int(row["guild_id"]),
        user_id=int(row["user_id"]),
        login=str(row["login_display"]),
        session_version=int(row["session_version"]),
        failed_attempts=int(row["failed_attempts"] or 0),
        reset_required=bool(row["reset_required"]),
        last_login_at=row["last_login_at"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def get_web_credential(guild_id: int, user_id: int) -> WebCredential | None:
    with connect_readonly() as con:
        row = con.execute(
            "SELECT * FROM web_credentials WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
    return _credential_from_row(row)


def configure_web_credential(
    guild_id: int,
    user_id: int,
    login: str,
    pin: str,
    *, kind: str = "pin",
) -> WebCredential:
    clean_login = normalize_web_login(login)
    if kind == "pin":
        clean_pin = normalize_web_pin(pin)
    elif kind == "password":
        clean_pin = str(pin)
        if not 12 <= len(clean_pin) <= 128 or clean_pin.isspace():
            raise ValueError("password_length")
    else:
        raise ValueError("credential_kind_invalid")
    pin_hash = _hash_pin(clean_pin)
    now = utc_now_iso()
    try:
        with _db_lock, connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """
                INSERT INTO web_credentials(
                    guild_id, user_id, login_key, login_display, pin_hash,
                    session_version, failed_attempts, locked_until,
                    reset_required, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, 1, 0, 0, 0, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    login_key = excluded.login_key,
                    login_display = excluded.login_display,
                    pin_hash = excluded.pin_hash,
                    session_version = web_credentials.session_version + 1,
                    failed_attempts = 0,
                    locked_until = 0,
                    reset_required = 0,
                    updated_at = excluded.updated_at
                """,
                (
                    int(guild_id),
                    int(user_id),
                    clean_login,
                    clean_login,
                    pin_hash,
                    now,
                    now,
                ),
            )
            row = con.execute(
                "SELECT * FROM web_credentials WHERE guild_id = ? AND user_id = ?",
                (int(guild_id), int(user_id)),
            ).fetchone()
            # Legacy /reset changes the primary credential, not the second
            # factor. Keep its PIN label accurate and revoke legacy cookies too.
            con.execute(
                "INSERT INTO account_security(guild_id,user_id,credential_kind) VALUES(?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET credential_kind=excluded.credential_kind, security_version=account_security.security_version+1, pending_method='', pending_secret='', pending_until=0",
                (int(guild_id), int(user_id), kind),
            )
            con.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("web_login_taken") from exc
    credential = _credential_from_row(row)
    if credential is None:  # pragma: no cover
        raise RuntimeError("web_credential_write_failed")
    return credential


def delete_web_credential(guild_id: int, user_id: int) -> bool:
    with _db_lock, connect() as con:
        cursor = con.execute(
            "DELETE FROM web_credentials WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        )
        con.commit()
    return cursor.rowcount > 0


def authenticate_web_credential(
    guild_id: int,
    login: str,
    pin: str,
    *,
    now_epoch: int | None = None,
) -> WebAuthenticationResult:
    try:
        clean_login = normalize_web_login(login)
        clean_pin = str(pin or "")
        if not 1 <= len(clean_pin) <= 128:
            raise ValueError("credential_invalid")
    except ValueError:
        _verify_pin("00000000", _dummy_hash())
        return WebAuthenticationResult("invalid")
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM web_credentials WHERE guild_id = ? AND login_key = ?",
            (int(guild_id), clean_login),
        ).fetchone()
    if row is None:
        _verify_pin(clean_pin, _dummy_hash())
        return WebAuthenticationResult("invalid")
    valid = _verify_pin(clean_pin, str(row["pin_hash"]))
    if bool(row["reset_required"]):
        return WebAuthenticationResult(
            "reset_required",
            user_id=int(row["user_id"]),
            failed_attempts=int(row["failed_attempts"] or _MAX_FAILURES),
        )
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute(
            "SELECT * FROM web_credentials WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(row["user_id"])),
        ).fetchone()
        if current is None or not hmac.compare_digest(
            str(current["pin_hash"]),
            str(row["pin_hash"]),
        ):
            con.rollback()
            return WebAuthenticationResult("invalid")
        if not valid:
            failures = int(current["failed_attempts"] or 0) + 1
            reset_required = failures >= _MAX_FAILURES
            con.execute(
                """
                UPDATE web_credentials
                SET failed_attempts = ?, locked_until = 0,
                    reset_required = ?,
                    session_version = session_version + ?,
                    updated_at = ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (
                    failures,
                    1 if reset_required else 0,
                    1 if reset_required else 0,
                    utc_now_iso(),
                    int(guild_id),
                    int(row["user_id"]),
                ),
            )
            con.commit()
            return WebAuthenticationResult(
                "reset_required" if reset_required else "invalid",
                user_id=int(row["user_id"]),
                failed_attempts=failures,
                notify_owner=True,
            )
        logged_in_at = utc_now_iso()
        con.execute(
            """
            UPDATE web_credentials
            SET failed_attempts = 0, locked_until = 0, reset_required = 0,
                last_login_at = ?, updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (
                logged_in_at,
                logged_in_at,
                int(guild_id),
                int(row["user_id"]),
            ),
        )
        current = con.execute(
            "SELECT * FROM web_credentials WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(row["user_id"])),
        ).fetchone()
        con.commit()
    credential = _credential_from_row(current)
    return WebAuthenticationResult(
        "ok",
        credential,
        user_id=(credential.user_id if credential is not None else None),
    )


def web_session_version_matches(
    guild_id: int,
    user_id: int,
    session_version: int,
) -> bool:
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT session_version FROM web_credentials
            WHERE guild_id = ? AND user_id = ?
            """,
            (int(guild_id), int(user_id)),
        ).fetchone()
    return row is not None and int(row["session_version"]) == int(session_version)


def invalidate_web_sessions(guild_id: int, user_id: int) -> bool:
    """Revoke every persistent T-Mod web session for an account.

    Sessions are signed and intentionally stateless, so the account's version
    is the durable revocation point.  Ticket-only sessions do not carry a
    session version and remain governed by their short expiry; a durable ticket
    registry is deliberately deferred until the schema for it is introduced.
    """

    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            UPDATE web_credentials
            SET session_version = session_version + 1,
                updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (utc_now_iso(), int(guild_id), int(user_id)),
        )
        con.commit()
    return cursor.rowcount > 0


def consume_web_entry_ticket_nonce(
    guild_id: int,
    nonce: str,
    *,
    expires_at: int,
    now_epoch: int,
) -> bool:
    """Persist atomically that a signed Discord entry link has been used.

    A process-local set makes a ticket reusable after a web restart.  The
    primary key below provides the same one-time guarantee across every
    container and safely resolves concurrent clicks on the same link.
    """

    clean_nonce = str(nonce or "").strip()
    if not clean_nonce or int(expires_at) <= int(now_epoch):
        return False
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "DELETE FROM web_entry_ticket_uses WHERE expires_at <= ?",
            (int(now_epoch),),
        )
        try:
            cursor = con.execute(
                """
                INSERT INTO web_entry_ticket_uses(guild_id, nonce, expires_at, used_at)
                VALUES(?, ?, ?, ?)
                """,
                (
                    int(guild_id),
                    clean_nonce,
                    int(expires_at),
                    utc_now_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            con.rollback()
            return False
        con.commit()
    return cursor.rowcount == 1


def web_section_grants(guild_id: int, user_id: int | None = None) -> list[dict]:
    with connect_readonly() as con:
        if user_id is None:
            rows = con.execute(
                "SELECT * FROM web_section_grants WHERE guild_id = ? ORDER BY user_id, section",
                (int(guild_id),),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM web_section_grants WHERE guild_id = ? AND user_id = ? ORDER BY section",
                (int(guild_id), int(user_id)),
            ).fetchall()
    return [dict(row) for row in rows]


def web_set_section_grant(
    guild_id: int,
    user_id: int,
    section: str,
    *,
    enabled: bool,
    granted_by_id: int,
) -> bool:
    selected = str(section or "").strip().lower()
    if selected not in WEB_GRANTABLE_SECTIONS or int(user_id) <= 0:
        raise ValueError("web_section_grant_invalid")
    with _db_lock, connect() as con:
        if enabled:
            cursor = con.execute(
                """
                INSERT OR IGNORE INTO web_section_grants(
                    guild_id, user_id, section, granted_by_id, created_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    int(guild_id),
                    int(user_id),
                    selected,
                    int(granted_by_id),
                    utc_now_iso(),
                ),
            )
        else:
            cursor = con.execute(
                """
                DELETE FROM web_section_grants
                WHERE guild_id = ? AND user_id = ? AND section = ?
                """,
                (int(guild_id), int(user_id), selected),
            )
        con.commit()
    return cursor.rowcount > 0


__all__ = [
    "WebAuthenticationResult",
    "WebCredential",
    "authenticate_web_credential",
    "configure_web_credential",
    "delete_web_credential",
    "consume_web_entry_ticket_nonce",
    "get_web_credential",
    "invalidate_web_sessions",
    "normalize_web_login",
    "normalize_web_pin",
    "web_session_version_matches",
    "WEB_GRANTABLE_SECTIONS",
    "web_section_grants",
    "web_set_section_grant",
]
