"""Short-lived Discord-issued authentication for the consensus web console.

The browser never receives the bot token or a shared administrative secret.
An ephemeral Discord control surface creates a single-use entry ticket for the
requesting member.  The web server exchanges it for a signed, HttpOnly cookie
and resolves the member from Discord again on every authenticated request.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import discord
from aiohttp import web

from persistence import activity_repository as meta_storage
from persistence import web_auth_repository as credential_storage
from persistence import profile_repository as profile_storage


SESSION_COOKIE = "tmod_account_session"
LEGACY_SESSION_COOKIE = "tmod_consensus_session"
_SECRET_META_KEY = "consensus_web:session_secret:v1"
_TICKET_LIFETIME_SECONDS = 10 * 60
_SESSION_LIFETIME_SECONDS = 12 * 60 * 60
PERSISTENT_SESSION_LIFETIME_SECONDS = 30 * 24 * 60 * 60
_used_tickets: dict[str, int] = {}
_runtime_secret: bytes | None = None
_COOKIE_DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


@dataclass(frozen=True, slots=True)
class TModAccountIdentity:
    """Minimal Discord-shaped identity for accounts outside the home guild."""

    id: int
    display_name: str
    roles: tuple[Any, ...] = ()

    @property
    def guild_permissions(self) -> discord.Permissions:
        return discord.Permissions.none()

    @property
    def mention(self) -> str:
        return f"<@{int(self.id)}>"


@dataclass(frozen=True, slots=True)
class ConsensusWebPrincipal:
    user_id: int
    guild_id: int
    display_name: str
    csrf_token: str
    member: discord.Member | TModAccountIdentity

    @property
    def administrator(self) -> bool:
        return bool(self.member.guild_permissions.administrator)

    @property
    def guild_member(self) -> bool:
        return not isinstance(self.member, TModAccountIdentity)

    @property
    def account_tier(self) -> str:
        if self.administrator:
            return "administrator"
        return "member" if self.guild_member else "zero"


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
    destination: str = "/",
) -> str:
    ticket = create_entry_ticket(guild_id=guild_id, user_id=user_id)
    selected_destination = (
        str(destination)
        if str(destination)
        in {"/", "/admin", "/reactor", "/atlas", "/games", "/host", "/ovr", "/tasks", "/admission"}
        else "/"
    )
    query = urlencode(
        {
            "ticket": ticket,
            "mode": str(mode),
            "next": selected_destination,
        }
    )
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


def create_session_token(
    *,
    guild_id: int,
    user_id: int,
    lifetime_seconds: int = _SESSION_LIFETIME_SECONDS,
    session_version: int | None = None,
) -> tuple[str, str]:
    now = int(time.time())
    csrf_token = secrets.token_urlsafe(24)
    payload: dict[str, Any] = {
        "gid": int(guild_id),
        "uid": int(user_id),
        "iat": now,
        "exp": now + max(60, int(lifetime_seconds)),
        "csrf": csrf_token,
        "nonce": secrets.token_urlsafe(12),
    }
    if session_version is not None:
        payload["sv"] = int(session_version)
    return (
        _sign(payload, purpose="session"),
        csrf_token,
    )


def signed_session_identity(
    request: web.Request,
    *,
    expected_guild_id: int,
) -> tuple[int, int] | None:
    """Read the signed identity even when its credential was invalidated.

    Global-ban enforcement must still recognize an already issued cookie after
    its session version has been revoked.  This helper validates the signature,
    expiry and audience, but deliberately does not grant portal permissions.
    """

    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    try:
        payload = _verify(token, purpose="session")
    except ConsensusWebAuthError:
        return None
    guild_id = int(payload.get("gid") or 0)
    user_id = int(payload.get("uid") or 0)
    if guild_id != int(expected_guild_id) or user_id <= 0:
        return None
    return guild_id, user_id


def account_cookie_domain(request_host: str | None) -> str | None:
    """Return a shared parent domain only for an actual member of that domain."""

    configured = os.getenv("TMOD_ACCOUNT_COOKIE_DOMAIN", ".tvr.lat").strip().lower()
    if configured in {"", "none", "host-only"}:
        return None
    base = configured.lstrip(".").rstrip(".")
    if not _COOKIE_DOMAIN_RE.fullmatch(base):
        return None
    raw_host = str(request_host or "").strip().lower().rstrip(".")
    if raw_host.startswith("["):
        host = raw_host[1:].split("]", 1)[0]
    else:
        host = raw_host.split(":", 1)[0]
    if host != base and not host.endswith(f".{base}"):
        return None
    return f".{base}"


def clear_session_cookie(
    response: web.StreamResponse,
    *,
    secure: bool,
    request_host: str | None = None,
) -> None:
    domain = account_cookie_domain(request_host)
    response.del_cookie(
        SESSION_COOKIE,
        path="/",
        domain=domain,
        secure=bool(secure),
        httponly=True,
        samesite="Lax",
    )
    # Remove the previous host-only consensus cookie during the SSO migration.
    response.del_cookie(
        LEGACY_SESSION_COOKIE,
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
    max_age: int = _SESSION_LIFETIME_SECONDS,
    request_host: str | None = None,
) -> None:
    domain = account_cookie_domain(request_host)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max(60, int(max_age)),
        path="/",
        domain=domain,
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
    # Do not authenticate with the former host-only cookie.  Falling back to
    # it after a shared logout could silently restore a session on another
    # subdomain.  It is deleted opportunistically by the logout endpoint.
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
    session_version = payload.get("sv")
    if session_version is not None and not await asyncio.to_thread(
        credential_storage.web_session_version_matches,
        int(guild_id),
        user_id,
        int(session_version),
    ):
        return None
    if session_version is not None and not await asyncio.to_thread(
        profile_storage.list_profile_characters,
        int(guild_id),
        user_id,
    ):
        return None
    if user_id <= 0:
        return None
    guild = bot.get_guild(int(guild_id))
    member = guild.get_member(user_id) if guild is not None else None
    if member is None and guild is not None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.DiscordException:
            member = None
    if not isinstance(member, discord.Member):
        # A persistent credential is also the proof for a zero-level T-Mod
        # account. Ticket sessions never contain `sv`, so a former/foreign
        # Discord user cannot turn a one-time link into an external account.
        if session_version is None:
            return None
        credential = await asyncio.to_thread(
            credential_storage.get_web_credential,
            int(guild_id),
            user_id,
        )
        if credential is None:
            return None
        member = TModAccountIdentity(
            id=user_id,
            display_name=str(credential.login),
        )
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
    "TModAccountIdentity",
    "PERSISTENT_SESSION_LIFETIME_SECONDS",
    "LEGACY_SESSION_COOKIE",
    "SESSION_COOKIE",
    "account_cookie_domain",
    "clear_session_cookie",
    "consensus_web_entry_url",
    "consume_entry_ticket",
    "create_entry_ticket",
    "create_session_token",
    "csrf_matches",
    "resolve_principal",
    "set_session_cookie",
    "signed_session_identity",
]
