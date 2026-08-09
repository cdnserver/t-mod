"""Standalone SGL bureau surface backed by the canonical T-Mod case store."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Awaitable, Callable

import discord
from aiohttp import web

from modules.consensus_web_auth import ConsensusWebPrincipal
from persistence import sgl_repository as sgl_storage
from persistence import web_auth_repository as web_auth_storage
from persistence import web_portal_repository as portal_storage


AuthenticatedRequest = Callable[
    [web.Request], Awaitable[tuple[ConsensusWebPrincipal | None, bool]]
]


def register_sgl_web_routes(
    app: web.Application,
    bot: discord.Client,
    *,
    guild_id: int,
    asset_dir: Path,
    authenticate: AuthenticatedRequest,
) -> None:
    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(asset_dir / "index.html")

    async def asset(request: web.Request) -> web.FileResponse:
        name = str(request.match_info.get("name") or "")
        if name not in {"app.js", "style.css", "favicon.svg"}:
            raise web.HTTPNotFound()
        response = web.FileResponse(asset_dir / name)
        response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=86400"
        return response

    async def principal(request: web.Request) -> tuple[ConsensusWebPrincipal, bool]:
        selected, legacy = await authenticate(request)
        if legacy or selected is None:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "sgl_login_required", "message": "Войдите в T-Mod SGL."}, ensure_ascii=False),
                content_type="application/json",
            )
        if selected.administrator:
            return selected, True
        try:
            grants = await asyncio.to_thread(
                web_auth_storage.web_section_grants, int(guild_id), int(selected.user_id)
            )
        except (OSError, sqlite3.Error):
            grants = []
        if not any(str(item.get("section")) == "sgl" for item in grants):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "sgl_access_required", "message": "Доступ к SGL выдаёт администратор T-Mod."}, ensure_ascii=False),
                content_type="application/json",
            )
        return selected, False

    async def bootstrap(request: web.Request) -> web.Response:
        selected, administrator = await principal(request)
        query = str(request.query.get("q") or "").strip() or None
        status = str(request.query.get("status") or "").strip() or None
        cases, archives = await asyncio.gather(
            asyncio.to_thread(
                portal_storage.portal_sgl_cases,
                int(guild_id), status=status, query=query, limit=100, offset=0,
            ),
            asyncio.to_thread(
                portal_storage.portal_sgl_archives,
                int(guild_id), query=query, limit=100, offset=0,
            ),
        )
        items = list(cases.get("items") or [])
        totals: dict[str, int] = {}
        for item in items:
            key = str(item.get("status") or "unknown")
            totals[key] = totals.get(key, 0) + 1
            channel_id = int(item.get("channel_id") or 0)
            item["discord_url"] = (
                f"https://discord.com/channels/{int(guild_id)}/{channel_id}" if channel_id else None
            )
        return web.json_response(
            {
                "viewer": {
                    "id": int(selected.user_id),
                    "name": str(selected.display_name),
                    "administrator": administrator,
                    "csrf_token": str(selected.csrf_token),
                },
                "cases": cases,
                "archives": archives,
                "counts": {
                    "all": int(cases.get("total") or 0),
                    "open": sum(value for key, value in totals.items() if key not in {"closed", "archived"}),
                    "closed": totals.get("closed", 0),
                    "archives": int(archives.get("total") or 0),
                },
                "status_counts": totals,
            }
        )

    async def case_detail(request: web.Request) -> web.Response:
        await principal(request)
        try:
            case_number = int(request.match_info.get("case_number") or 0)
        except (TypeError, ValueError):
            case_number = 0
        case = await asyncio.to_thread(
            sgl_storage.get_sgl_case_by_number, int(guild_id), case_number
        )
        if case is None:
            raise web.HTTPNotFound(
                text='{"error":"sgl_case_not_found","message":"Кейс не найден."}',
                content_type="application/json",
            )
        events, receipts = await asyncio.gather(
            asyncio.to_thread(sgl_storage.get_sgl_case_events, int(case.id), 50),
            asyncio.to_thread(sgl_storage.list_sgl_receipts_for_case, int(case.id), 25),
        )
        payload = asdict(case)
        payload["discord_url"] = (
            f"https://discord.com/channels/{int(guild_id)}/{int(case.channel_id)}"
            if case.channel_id else None
        )
        return web.json_response(
            {"case": payload, "events": events, "receipts": [asdict(item) for item in receipts]}
        )

    app.router.add_get("/sgl", index)
    app.router.add_get("/sgl/", index)
    app.router.add_get("/sgl/assets/{name}", asset)
    app.router.add_get("/api/sgl/bootstrap", bootstrap)
    app.router.add_get("/api/sgl/cases/{case_number}", case_detail)


__all__ = ["register_sgl_web_routes"]
