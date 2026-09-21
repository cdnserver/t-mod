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
    resolve_projected_principal,
    signed_session_identity,
)
from modules.desktop_bootstrap_service import (
    DesktopBootstrapError,
    build_desktop_bootstrap_payload,
)
from persistence import global_ban_repository as global_ban_storage
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
        desktop_token = str(request.headers.get("X-TMod-Install-Token") or "").strip()
        fingerprint = str(request.headers.get("X-TMod-Device-Fingerprint") or "").strip()
        identity = signed_session_identity(
            request,
            expected_guild_id=selected_guild_id,
        )
        ban: dict[str, Any] | None = None
        if desktop_token:
            ban = await asyncio.to_thread(
                global_ban_storage.get_desktop_installation_ban,
                selected_guild_id,
                desktop_token,
                fingerprint,
            )
        if identity is not None:
            ban = ban or await asyncio.to_thread(
                global_ban_storage.get_global_ban,
                selected_guild_id,
                int(identity[1]),
            )
        if ban is not None:
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
        principal = await resolve_projected_principal(
            request,
            guild_id=selected_guild_id,
        )
        if principal is None:
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

    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    app.router.add_get("/internal/desktop/v1/bootstrap", desktop_bootstrap)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT, access_log=None)
