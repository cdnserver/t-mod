"""Dedicated maintenance process for backups and database health.

Docker's health probe is deliberately a cheap liveness check.  Database
readiness is cached by the maintenance loop and exposed separately so a slow
backup or a transient PostgreSQL reconnect cannot falsely kill the worker.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aiohttp import web

from modules.global_log_runtime import (
    emit_global_event,
    global_log_context,
    runtime_health as global_log_runtime_health,
    start_global_log_runtime,
    stop_global_log_runtime,
)
from persistence.database_guard import (
    run_scheduled_database_protection,
)


INTERVAL = max(60, int(os.getenv("TMOD_WORKER_INTERVAL_SECONDS", "300") or 300))
PORT = int(os.getenv("TMOD_WORKER_PORT", "8792") or 8792)
WORKER_STATE = web.AppKey("worker_state", dict)
WORKER_TASK = web.AppKey("worker_task", asyncio.Task)


def _audit(event: dict[str, Any]) -> bool:
    """Emit worker telemetry without ever affecting maintenance work."""

    try:
        return bool(emit_global_event(event))
    except Exception as exc:  # Observability must remain strictly fail-open.
        print(
            f"Worker global audit failed: {type(exc).__name__}: {str(exc)[:500]}",
            flush=True,
        )
        return False


def _audit_health() -> dict[str, Any]:
    try:
        return dict(global_log_runtime_health())
    except Exception as exc:
        return {
            "enabled": False,
            "running": False,
            "last_error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }


def _initial_state() -> dict[str, Any]:
    return {
        "status": "starting",
        "service": "tmod-worker",
        "database": None,
        "last_result": None,
        "last_error": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "global_log": _audit_health(),
    }


async def maintenance(application: web.Application) -> None:
    state = application[WORKER_STATE]
    active_cycle: str | None = None
    try:
        while True:
            active_cycle = str(uuid4())
            started = time.perf_counter()
            _audit({
                "source_service": "tmod-worker",
                "source_type": "background_job",
                "event_type": "database_protection_started",
                "request_id": active_cycle,
                "target_type": "maintenance_job",
                "target_id": "scheduled_database_protection",
                "summary": "Фоновая защита базы данных запущена",
                "details": {"interval_seconds": INTERVAL},
            })
            try:
                with global_log_context(request_id=active_cycle):
                    result = await asyncio.to_thread(
                        run_scheduled_database_protection
                    )
                protection = result.get("protection") if isinstance(result, dict) else None
                database_status = (
                    str(protection.get("status") or "unknown")
                    if isinstance(protection, dict)
                    else "unknown"
                )
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                state.update(
                    status="ok" if database_status == "ok" else "degraded",
                    database=protection,
                    last_result=result,
                    last_error=None,
                    checked_at=datetime.now(timezone.utc).isoformat(),
                    global_log=_audit_health(),
                )
                _audit({
                    "source_service": "tmod-worker",
                    "source_type": "background_job",
                    "event_type": "database_protection_completed",
                    "severity": "info" if database_status == "ok" else "warning",
                    "request_id": active_cycle,
                    "target_type": "maintenance_job",
                    "target_id": "scheduled_database_protection",
                    "summary": f"Фоновая защита базы данных завершена: {database_status}",
                    "duration_ms": elapsed_ms,
                    "details": {
                        "database_status": database_status,
                        "result": result,
                    },
                })
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                state.update(
                    status="degraded",
                    last_error=f"{type(exc).__name__}: {str(exc)[:700]}",
                    checked_at=datetime.now(timezone.utc).isoformat(),
                    global_log=_audit_health(),
                )
                _audit({
                    "source_service": "tmod-worker",
                    "source_type": "background_job",
                    "event_type": "database_protection_failed",
                    "severity": "error",
                    "request_id": active_cycle,
                    "target_type": "maintenance_job",
                    "target_id": "scheduled_database_protection",
                    "summary": "Фоновая защита базы данных завершилась ошибкой",
                    "duration_ms": elapsed_ms,
                    "details": {
                        "exception_type": type(exc).__name__,
                        "exception": str(exc)[:4000],
                    },
                })
            active_cycle = None
            await asyncio.sleep(INTERVAL)
    except asyncio.CancelledError:
        _audit({
            "source_service": "tmod-worker",
            "source_type": "lifecycle",
            "event_type": "worker_maintenance_cancelled",
            "severity": "info",
            "request_id": active_cycle,
            "target_type": "worker",
            "target_id": "database_maintenance",
            "summary": "Цикл фонового обслуживания остановлен",
        })
        raise


async def health(request: web.Request) -> web.Response:
    return web.json_response({
        **request.app[WORKER_STATE],
        "global_log": _audit_health(),
        "liveness": "ok",
    })


async def readiness(request: web.Request) -> web.Response:
    state = request.app[WORKER_STATE]
    ready = state.get("status") == "ok"
    return web.json_response(
        {**state, "global_log": _audit_health(), "ready": ready},
        status=200 if ready else 503,
    )


async def create_app() -> web.Application:
    app = web.Application()
    app[WORKER_STATE] = _initial_state()

    async def start(application: web.Application) -> None:
        try:
            audit_health = await start_global_log_runtime(asyncio.get_running_loop())
        except Exception as exc:
            audit_health = {
                "enabled": False,
                "running": False,
                "last_error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
            print(
                f"Worker global audit startup failed: {type(exc).__name__}: {str(exc)[:500]}",
                flush=True,
            )
        application[WORKER_STATE]["global_log"] = audit_health
        _audit({
            "source_service": "tmod-worker",
            "source_type": "lifecycle",
            "event_type": "worker_started",
            "target_type": "worker",
            "target_id": "database_maintenance",
            "summary": "Фоновый процесс T-Mod запущен",
            "details": {"interval_seconds": INTERVAL, "port": PORT},
        })
        application[WORKER_TASK] = asyncio.create_task(
            maintenance(application), name="tmod-database-maintenance"
        )

    async def stop(application: web.Application) -> None:
        _audit({
            "source_service": "tmod-worker",
            "source_type": "lifecycle",
            "event_type": "worker_stopping",
            "target_type": "worker",
            "target_id": "database_maintenance",
            "summary": "Фоновый процесс T-Mod завершает работу",
        })
        task = application[WORKER_TASK]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _audit({
            "source_service": "tmod-worker",
            "source_type": "lifecycle",
            "event_type": "worker_stopped",
            "target_type": "worker",
            "target_id": "database_maintenance",
            "summary": "Фоновый процесс T-Mod остановлен",
        })
        await stop_global_log_runtime()

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    app.router.add_get("/health", health)
    app.router.add_get("/ready", readiness)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT, access_log=None)
