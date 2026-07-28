"""Short-lived Discord-issued authentication for the consensus web console.

The browser never receives the bot token or a shared administrative secret.
An ephemeral Discord control surface creates a single-use entry ticket for the
requesting member.  The web server exchanges it for a signed, HttpOnly cookie
and resolves the member from Discord again on every authenticated request.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import discord
from aiohttp import web

from persistence import activity_repository as meta_storage


SESSION_COOKIE = "tmod_consensus_session"
_SECRET_META_KEY = "consensus_web:session_secret:v1"
_TICKET_LIFETIME_SECONDS = 10 * 60
_SESSION_LIFETIME_SECONDS = 12 * 60 * 60
_used_tickets: dict[str, int] = {}
_runtime_secret: bytes | None = None


@dataclass(frozen=True, slots=True)
class ConsensusWebPrincipal:
    user_id: int
    guild_id: int
    display_name: str
    csrf_token: str
    member: discord.Member

    @property
    def administrator(self) -> bool:
        return bool(self.member.guild_permissions.administrator)


class ConsensusWebAuthError(ValueError):
    pass


def _secret() -> bytes:
    global _runtime_secret
    if _runtime_secret is not None:
        return _runtime_secret
    try:
        stored = str(meta_storage.get_meta(_SECRET_META_KEY) or "").strip()
    except (OSError, RuntimeError, sqlite3.Error):
        stored = ""
    if len(stored) < 32:
        stored = secrets.token_urlsafe(48)
        try:
            meta_storage.set_meta_value(_SECRET_META_KEY, stored)
        except (OSError, RuntimeError, sqlite3.Error):
            # Tests and recovery tools can construct Discord views before the
            # persistent mount is available.  The in-memory key remains safe;
            # production persists it as soon as the normal database exists.
            pass
    _runtime_secret = stored.encode("utf-8")
    return _runtime_secret


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload: dict[str, Any], *, purpose: str) -> str:
    body = _b64encode(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signature = hmac.new(
        _secret(),
        f"{purpose}.{body}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return f"{body}.{_b64encode(signature)}"


def _verify(token: str, *, purpose: str) -> dict[str, Any]:
    try:
        body, supplied_signature = str(token).split(".", 1)
        expected = hmac.new(
            _secret(),
            f"{purpose}.{body}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        supplied = _b64decode(supplied_signature)
        if not hmac.compare_digest(supplied, expected):
            raise ConsensusWebAuthError("invalid_signature")
        payload = json.loads(_b64decode(body))
    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        if isinstance(exc, ConsensusWebAuthError):
            raise
        raise ConsensusWebAuthError("invalid_token") from exc
    if not isinstance(payload, dict):
        raise ConsensusWebAuthError("invalid_payload")
    if int(payload.get("exp") or 0) < int(time.time()):
        raise ConsensusWebAuthError("expired")
    return payload


def create_entry_ticket(*, guild_id: int, user_id: int) -> str:
    now = int(time.time())
    return _sign(
        {
            "gid": int(guild_id),
            "uid": int(user_id),
            "iat": now,
            "exp": now + _TICKET_LIFETIME_SECONDS,
            "nonce": secrets.token_urlsafe(12),
        },
        purpose="entry",
    )


def consensus_web_entry_url(
    base_url: str,
    *,
    guild_id: int,
    user_id: int,
    mode: str = "live",
) -> str:
    ticket = create_entry_ticket(guild_id=guild_id, user_id=user_id)
    query = urlencode({"ticket": ticket, "mode": str(mode)})
    return f"{str(base_url).rstrip('/')}/auth/ticket?{query}"


def consume_entry_ticket(
    token: str,
    *,
    expected_guild_id: int,
) -> tuple[int, int]:
    now = int(time.time())
    for nonce, expiry in list(_used_tickets.items()):
        if expiry < now:
            _used_tickets.pop(nonce, None)
    payload = _verify(token, purpose="entry")
    guild_id = int(payload.get("gid") or 0)
    user_id = int(payload.get("uid") or 0)
    nonce = str(payload.get("nonce") or "")
    if guild_id != int(expected_guild_id) or user_id <= 0 or not nonce:
        raise ConsensusWebAuthError("wrong_audience")
    if nonce in _used_tickets:
        raise ConsensusWebAuthError("ticket_used")
    _used_tickets[nonce] = int(payload["exp"])
    return guild_id, user_id


def create_session_token(*, guild_id: int, user_id: int) -> tuple[str, str]:
    now = int(time.time())
    csrf_token = secrets.token_urlsafe(24)
    return (
        _sign(
            {
                "gid": int(guild_id),
                "uid": int(user_id),
                "iat": now,
                "exp": now + _SESSION_LIFETIME_SECONDS,
                "csrf": csrf_token,
                "nonce": secrets.token_urlsafe(12),
            },
            purpose="session",
        ),
        csrf_token,
    )


def clear_session_cookie(
    response: web.StreamResponse,
    *,
    secure: bool,
) -> None:
    response.del_cookie(
        SESSION_COOKIE,
        path="/",
        secure=bool(secure),
        httponly=True,
        samesite="Lax",
    )


def set_session_cookie(
    response: web.StreamResponse,
    token: str,
    *,
    secure: bool,
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=_SESSION_LIFETIME_SECONDS,
        path="/",
        secure=bool(secure),
        httponly=True,
        samesite="Lax",
    )


async def resolve_principal(
    request: web.Request,
    bot: discord.Client,
    *,
    guild_id: int,
) -> ConsensusWebPrincipal | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    try:
        payload = _verify(token, purpose="session")
    except ConsensusWebAuthError:
        return None
    if int(payload.get("gid") or 0) != int(guild_id):
        return None
    user_id = int(payload.get("uid") or 0)
    guild = bot.get_guild(int(guild_id))
    if guild is None or user_id <= 0:
        return None
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.DiscordException:
            return None
    if not isinstance(member, discord.Member):
        return None
    csrf_token = str(payload.get("csrf") or "")
    if not csrf_token:
        return None
    return ConsensusWebPrincipal(
        user_id=user_id,
        guild_id=int(guild_id),
        display_name=str(member.display_name),
        csrf_token=csrf_token,
        member=member,
    )


def csrf_matches(request: web.Request, principal: ConsensusWebPrincipal) -> bool:
    supplied = request.headers.get("X-CSRF-Token", "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, principal.csrf_token)


__all__ = [
    "ConsensusWebAuthError",
    "ConsensusWebPrincipal",
    "SESSION_COOKIE",
    "clear_session_cookie",
    "consensus_web_entry_url",
    "consume_entry_ticket",
    "create_entry_ticket",
    "create_session_token",
    "csrf_matches",
    "resolve_principal",
    "set_session_cookie",
]
