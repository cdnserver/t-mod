"""Standalone database-backed API introduced beside the Discord web runtime.

Routes move here one at a time after parity verification.  The public gateway
keeps the established runtime as an automatic fallback during each rollout.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from aiohttp import web

from modules.consensus_web_auth import (
    ConsensusWebPrincipal,
    resolve_projected_principal,
    signed_session_identity,
)
from modules.admin_access_service import build_admin_access_payload
from modules.desktop_bootstrap_service import (
    DesktopBootstrapError,
    build_desktop_bootstrap_payload,
)
from modules.reactor_preparation_service import build_reactor_preparation_payload
from persistence import global_ban_repository as global_ban_storage
from persistence import reactor_repository as reactor_storage
from persistence.core import connect_readonly


PORT = int(os.getenv("TMOD_API_PORT", "8793") or 8793)
MODE = str(os.getenv("TMOD_API_MODE") or "shadow").strip() or "shadow"


def configured_guild_id() -> int:
    raw = str(
        os.getenv("CONSENSUS_WEB_GUILD_ID")
        or os.getenv("DISCORD_GUILD_ID")
        or "0"
    ).strip()
    return int(raw) if raw.isdigit() else 0


def _database_ready() -> bool:
    try:
        with connect_readonly() as connection:
            connection.execute(
                "SELECT guild_id FROM web_access_projection LIMIT 1"
            ).fetchone()
        return True
    except Exception:
        return False


def _ban_response(ban: dict[str, Any]) -> web.Response:
    return web.json_response(
        {
            "error": "globally_banned",
            "message": "Доступ к экосистеме T-Mod заблокирован.",
            "reason": str(ban.get("reason") or "Причина не указана."),
            "reference": f"GB-{int(ban.get('revision') or 1):03d}",
        },
        status=423,
        headers={"Cache-Control": "private, no-store"},
    )


async def _authenticate_projected_request(
    request: web.Request,
    *,
    guild_id: int,
    require_member: bool = False,
    include_desktop_installation: bool = False,
) -> tuple[ConsensusWebPrincipal | None, web.Response | None]:
    """Authorize a standalone API request without a live Discord dependency."""

    identity = signed_session_identity(request, expected_guild_id=guild_id)
    ban: dict[str, Any] | None = None
    if include_desktop_installation:
        desktop_token = str(request.headers.get("X-TMod-Install-Token") or "").strip()
        fingerprint = str(
            request.headers.get("X-TMod-Device-Fingerprint") or ""
        ).strip()
        if desktop_token:
            ban = await asyncio.to_thread(
                global_ban_storage.get_desktop_installation_ban,
                guild_id,
                desktop_token,
                fingerprint,
            )
    if identity is not None:
        ban = ban or await asyncio.to_thread(
            global_ban_storage.get_global_ban,
            guild_id,
            int(identity[1]),
        )
    if ban is not None:
        return None, _ban_response(ban)

    principal = await resolve_projected_principal(request, guild_id=guild_id)
    if principal is None:
        return None, web.json_response(
            {"error": "personal_login_required"},
            status=401,
            headers={"Cache-Control": "private, no-store"},
        )
    if require_member and not principal.guild_member:
        return None, web.json_response(
            {
                "error": "zero_account_reactor_forbidden",
                "message": "Нулевой аккаунт не имеет доступа к данным Товарищества.",
            },
            status=403,
            headers={"Cache-Control": "private, no-store"},
        )
    return principal, None


def _viewer_payload(principal: ConsensusWebPrincipal) -> dict[str, Any]:
    return {
        "id": int(principal.user_id),
        "name": str(principal.display_name),
        "administrator": bool(principal.administrator),
        "account_tier": str(principal.account_tier),
        "csrf_token": str(principal.csrf_token),
    }


async def create_app(*, guild_id: int | None = None) -> web.Application:
    selected_guild_id = int(guild_id or configured_guild_id())
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "service": "tmod-api",
                "mode": MODE,
            }
        )

    async def ready(_: web.Request) -> web.Response:
        database_ready = await asyncio.to_thread(_database_ready)
        configured = selected_guild_id > 0
        is_ready = database_ready and configured
        return web.json_response(
            {
                "status": "ready" if is_ready else "starting",
                "service": "tmod-api",
                "mode": MODE,
                "database": database_ready,
                "guild_configured": configured,
            },
            status=200 if is_ready else 503,
        )

    async def desktop_bootstrap(request: web.Request) -> web.Response:
        principal, auth_error = await _authenticate_projected_request(
            request,
            guild_id=selected_guild_id,
            include_desktop_installation=True,
        )
        if auth_error is not None:
            if auth_error.status == 401:
                return web.json_response(
                    {
                        "error": "desktop_login_required",
                        "login_url": "https://tvr.lat/login?next=/reactor",
                    },
                    status=401,
                    headers={"Cache-Control": "private, no-store"},
                )
            return auth_error
        if principal is None:  # pragma: no cover - guarded by auth_error
            return web.json_response(
                {
                    "error": "desktop_login_required",
                    "login_url": "https://tvr.lat/login?next=/reactor",
                },
                status=401,
                headers={"Cache-Control": "private, no-store"},
            )
        try:
            payload = await build_desktop_bootstrap_payload(
                headers=request.headers,
                principal=principal,
                guild_id=selected_guild_id,
            )
        except DesktopBootstrapError as exc:
            return web.json_response(
                {"error": exc.error, "message": exc.message},
                status=exc.status,
                headers={"Cache-Control": "private, no-store"},
            )
        return web.json_response(
            payload,
            headers={"Cache-Control": "private, no-store"},
        )

    async def reactor_notifications(request: web.Request) -> web.Response:
        principal, auth_error = await _authenticate_projected_request(
            request,
            guild_id=selected_guild_id,
            require_member=True,
        )
        if auth_error is not None:
            return auth_error
        if principal is None:  # pragma: no cover - guarded by auth_error
            raise web.HTTPUnauthorized()
        payload = await asyncio.to_thread(
            reactor_storage.reactor_list_notifications,
            selected_guild_id,
            int(principal.user_id),
            unread_only=request.query.get("unread") == "1",
            limit=100,
        )
        return web.json_response(
            {"viewer": _viewer_payload(principal), **payload},
            headers={"Cache-Control": "private, no-store"},
        )

    async def reactor_preparation(request: web.Request) -> web.Response:
        principal, auth_error = await _authenticate_projected_request(
            request,
            guild_id=selected_guild_id,
            require_member=True,
        )
        if auth_error is not None:
            return auth_error
        if principal is None:  # pragma: no cover - guarded by auth_error
            raise web.HTTPUnauthorized()
        payload = await asyncio.to_thread(
            build_reactor_preparation_payload,
            selected_guild_id,
            int(principal.user_id),
        )
        return web.json_response(
            payload,
            headers={"Cache-Control": "private, no-store"},
        )

    async def admin_access_self(request: web.Request) -> web.Response:
        principal, auth_error = await _authenticate_projected_request(
            request,
            guild_id=selected_guild_id,
        )
        if auth_error is not None:
            return auth_error
        if principal is None:  # pragma: no cover - guarded by auth_error
            raise web.HTTPUnauthorized()
        payload = await asyncio.to_thread(
            build_admin_access_payload,
            selected_guild_id,
            principal,
        )
        if not payload["sections"]:
            return web.json_response(
                {"error": "administrator_required"},
                status=403,
                headers={"Cache-Control": "private, no-store"},
            )
        return web.json_response(
            payload,
            headers={"Cache-Control": "private, no-store"},
        )

    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    app.router.add_get("/internal/desktop/v1/bootstrap", desktop_bootstrap)
    app.router.add_get("/internal/reactor/notifications", reactor_notifications)
    app.router.add_get("/internal/reactor/preparation", reactor_preparation)
    app.router.add_get("/internal/admin/access/self", admin_access_self)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT, access_log=None)
