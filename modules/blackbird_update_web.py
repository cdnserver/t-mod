"""Account-authenticated, locally staged Blackbird update feed.

The GitHub release repository is private. Installers are staged on the T-Mod
server and served only after normal account-session validation; no GitHub token
or permanent download credential is distributed to desktop clients.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web


_INSTALLER = re.compile(r"BLACKBIRD-Private-Setup-[A-Za-z0-9.-]+\.exe(?:\.blockmap)?\Z")


def update_file_allowed(name: str) -> bool:
    return name == "latest.yml" or bool(_INSTALLER.fullmatch(name))


def register_blackbird_update_routes(
    app: web.Application,
    *,
    authenticate: Callable[[web.Request], Awaitable[tuple[object | None, bool]]],
) -> None:
    async def download(request: web.Request) -> web.StreamResponse:
        principal, _ = await authenticate(request)
        # Service bearer tokens do not stand in for a user's account session.
        if principal is None:
            raise web.HTTPUnauthorized(text="account_session_required")
        name = request.match_info["name"]
        if not update_file_allowed(name):
            raise web.HTTPNotFound()
        directory = Path(os.getenv("TMOD_BLACKBIRD_UPDATE_DIR") or "/app/persistent/blackbird-updates").resolve()
        target = (directory / name).resolve()
        if target.parent != directory or not target.is_file():
            raise web.HTTPNotFound()
        response = web.FileResponse(target, headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        })
        if name == "latest.yml":
            response.content_type = "application/x-yaml"
        return response

    app.router.add_get("/api/desktop/v1/updates/blackbird/{name}", download)
