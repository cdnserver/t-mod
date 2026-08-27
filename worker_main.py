"""Dedicated maintenance process for backups and database health.

Docker's health probe is deliberately a cheap liveness check.  Database
readiness is cached by the maintenance loop and exposed separately so a slow
backup or a transient PostgreSQL reconnect cannot falsely kill the worker.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from persistence.database_guard import (
    run_scheduled_database_protection,
)


INTERVAL = max(60, int(os.getenv("TMOD_WORKER_INTERVAL_SECONDS", "300") or 300))
PORT = int(os.getenv("TMOD_WORKER_PORT", "8792") or 8792)
WORKER_STATE = web.AppKey("worker_state", dict)
WORKER_TASK = web.AppKey("worker_task", asyncio.Task)


def _initial_state() -> dict[str, Any]:
    return {
        "status": "starting",
        "service": "tmod-worker",
        "database": None,
        "last_result": None,
        "last_error": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


async def maintenance(application: web.Application) -> None:
    state = application[WORKER_STATE]
    while True:
        try:
            result = await asyncio.to_thread(
                run_scheduled_database_protection
            )
            protection = result.get("protection") if isinstance(result, dict) else None
            database_status = (
                str(protection.get("status") or "unknown")
                if isinstance(protection, dict)
                else "unknown"
            )
            state.update(
                status="ok" if database_status == "ok" else "degraded",
                database=protection,
                last_result=result,
                last_error=None,
                checked_at=datetime.now(timezone.utc).isoformat(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.update(
                status="degraded",
                last_error=f"{type(exc).__name__}: {str(exc)[:700]}",
                checked_at=datetime.now(timezone.utc).isoformat(),
            )
        await asyncio.sleep(INTERVAL)


async def health(request: web.Request) -> web.Response:
    return web.json_response({**request.app[WORKER_STATE], "liveness": "ok"})


async def readiness(request: web.Request) -> web.Response:
    state = request.app[WORKER_STATE]
    ready = state.get("status") == "ok"
    return web.json_response(
        {**state, "ready": ready},
        status=200 if ready else 503,
    )


async def create_app() -> web.Application:
    app = web.Application()
    app[WORKER_STATE] = _initial_state()

    async def start(application: web.Application) -> None:
        application[WORKER_TASK] = asyncio.create_task(
            maintenance(application), name="tmod-database-maintenance"
        )

    async def stop(application: web.Application) -> None:
        task = application[WORKER_TASK]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    app.router.add_get("/health", health)
    app.router.add_get("/ready", readiness)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT, access_log=None)
