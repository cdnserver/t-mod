"""Durable, idempotent requests for exercising personal-data rights."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


PRIVACY_REQUEST_TYPES = frozenset(
    {"withdraw", "erase", "access", "rectify", "export", "restrict", "object", "question"}
)
_RECEIPT_RE = re.compile(r"^[A-Za-z0-9_-]{16,100}$")
_EMAIL_RE = re.compile(r"^[^\s@]{1,120}@[^\s@]{1,190}\.[^\s@]{2,63}$")


def _single_line(value: Any, *, maximum: int) -> str:
    return " ".join(str(value or "").strip().split())[:maximum]


def _request_code() -> str:
    stamp = datetime.now(timezone.utc).strftime("%y%m%d")
    return f"DSR-{stamp}-{secrets.token_hex(4).upper()}"


def _row_payload(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def create_privacy_request(
    *,
    receipt_key: str,
    request_type: str,
    requester_email: str,
    account_login: str | None,
    discord_id: int | str | None,
    account_user_id: int | None,
    scope: str,
    details: str | None,
    remote_hash: str | None,
    user_agent: str | None,
) -> tuple[dict[str, Any], bool]:
    """Create one request or return the existing browser retry receipt."""

    receipt = str(receipt_key or "").strip()
    if not _RECEIPT_RE.fullmatch(receipt):
        raise ValueError("privacy_receipt_invalid")
    kind = str(request_type or "").strip().lower()
    if kind not in PRIVACY_REQUEST_TYPES:
        raise ValueError("privacy_request_type_invalid")
    email = _single_line(requester_email, maximum=320).casefold()
    if not _EMAIL_RE.fullmatch(email):
        raise ValueError("privacy_email_invalid")
    login = _single_line(account_login, maximum=64) or None
    selected_discord_id: int | None = None
    if str(discord_id or "").strip():
        raw_discord_id = str(discord_id).strip()
        if not raw_discord_id.isascii() or not raw_discord_id.isdigit():
            raise ValueError("privacy_discord_id_invalid")
        selected_discord_id = int(raw_discord_id)
        if selected_discord_id <= 0 or len(raw_discord_id) > 20:
            raise ValueError("privacy_discord_id_invalid")
    selected_account_id = int(account_user_id or 0) or None
    if selected_discord_id is None and selected_account_id is not None:
        selected_discord_id = selected_account_id
    if selected_account_id is None and selected_discord_id is None and not login:
        raise ValueError("privacy_identity_required")
    clean_scope = _single_line(scope, maximum=1000)
    if len(clean_scope) < 3:
        raise ValueError("privacy_scope_invalid")
    clean_details = str(details or "").strip()[:5000] or None
    now = utc_now_iso()

    with _db_lock, connect() as con:
        existing = con.execute(
            "SELECT * FROM privacy_requests WHERE receipt_key = ?",
            (receipt,),
        ).fetchone()
        if existing is not None:
            return _row_payload(existing), False
        for _ in range(5):
            code = _request_code()
            try:
                cursor = con.execute(
                    """
                    INSERT INTO privacy_requests(
                        request_code, receipt_key, request_type,
                        requester_email, account_login, discord_id,
                        account_user_id, scope, details, status, remote_hash,
                        user_agent, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', ?, ?, ?, ?)
                    """,
                    (
                        code,
                        receipt,
                        kind,
                        email,
                        login,
                        selected_discord_id,
                        selected_account_id,
                        clean_scope,
                        clean_details,
                        str(remote_hash or "")[:128] or None,
                        str(user_agent or "")[:1000] or None,
                        now,
                        now,
                    ),
                )
                request_id = int(cursor.lastrowid)
                con.commit()
                row = con.execute(
                    "SELECT * FROM privacy_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
                return _row_payload(row), True
            except Exception as exc:
                # Receipt races are legitimate (two tabs/retry after a lost
                # response).  Return the first committed result.  A rare code
                # collision is retried; every other database error is real.
                existing = con.execute(
                    "SELECT * FROM privacy_requests WHERE receipt_key = ?",
                    (receipt,),
                ).fetchone()
                if existing is not None:
                    con.commit()
                    return _row_payload(existing), False
                collision = con.execute(
                    "SELECT 1 FROM privacy_requests WHERE request_code = ?",
                    (code,),
                ).fetchone()
                if collision is None:
                    raise exc
        raise RuntimeError("privacy_request_code_exhausted")


def get_privacy_request(request_code: str) -> dict[str, Any] | None:
    code = _single_line(request_code, maximum=40).upper()
    with connect_readonly() as con:
        row = con.execute(
            "SELECT * FROM privacy_requests WHERE request_code = ?",
            (code,),
        ).fetchone()
    return _row_payload(row) if row is not None else None


def list_unnotified_privacy_requests(limit: int = 20) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT * FROM privacy_requests
            WHERE notification_sent_at IS NULL
            ORDER BY id ASC
            LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [_row_payload(row) for row in rows]


def record_privacy_notification(
    request_id: int,
    *,
    delivered: bool,
    error: str | None = None,
) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE privacy_requests
            SET notification_attempts = notification_attempts + 1,
                notification_sent_at = CASE WHEN ? = 1 THEN ? ELSE notification_sent_at END,
                notification_error = CASE WHEN ? = 1 THEN NULL ELSE ? END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                1 if delivered else 0,
                now,
                1 if delivered else 0,
                str(error or "delivery_unavailable")[:1000],
                now,
                int(request_id),
            ),
        )
        con.commit()


__all__ = [
    "PRIVACY_REQUEST_TYPES",
    "create_privacy_request",
    "get_privacy_request",
    "list_unnotified_privacy_requests",
    "record_privacy_notification",
]
