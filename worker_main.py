"""Dedicated maintenance process for backups and database health."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from persistence.database_guard import (
    database_protection_snapshot,
    run_scheduled_database_protection,
)


INTERVAL = max(60, int(os.getenv("TMOD_WORKER_INTERVAL_SECONDS", "300") or 300))
PORT = int(os.getenv("TMOD_WORKER_PORT", "8792") or 8792)


async def maintenance(application: web.Application) -> None:
    while True:
        try:
            application["last_result"] = await asyncio.to_thread(
                run_scheduled_database_protection
            )
            application["last_error"] = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            application["last_error"] = f"{type(exc).__name__}: {str(exc)[:700]}"
        await asyncio.sleep(INTERVAL)


async def health(request: web.Request) -> web.Response:
    snapshot = await asyncio.to_thread(database_protection_snapshot)
    error = request.app.get("last_error")
    return web.json_response(
        {
            "status": "ok" if snapshot.get("status") != "critical" and not error else "degraded",
            "service": "tmod-worker",
            "database": snapshot,
            "last_error": error,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        },
        status=200 if snapshot.get("status") != "critical" else 503,
    )


async def create_app() -> web.Application:
    app = web.Application()

    async def start(application: web.Application) -> None:
        application["task"] = asyncio.create_task(
            maintenance(application), name="tmod-database-maintenance"
        )

    async def stop(application: web.Application) -> None:
        task: asyncio.Task[Any] = application["task"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    app.router.add_get("/health", health)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT, access_log=None)
