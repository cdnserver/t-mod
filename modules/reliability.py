"""Production reliability contour shared by the bot and Nuclear Reactor."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from persistence.database_guard import (
    database_protection_snapshot,
    run_scheduled_database_protection,
)


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        selected = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        selected = int(default)
    return max(int(minimum), min(int(maximum), selected))


RELIABILITY_INTERVAL_SECONDS = _env_int(
    "TMOD_RELIABILITY_INTERVAL_SECONDS", 900, minimum=300, maximum=86400
)
RELIABILITY_DOMAIN_CACHE_SECONDS = _env_int(
    "TMOD_RELIABILITY_DOMAIN_CACHE_SECONDS", 60, minimum=15, maximum=600
)
STARTED_MONOTONIC = time.monotonic()
PUBLIC_SURFACES = (
    ("member", "Реактор", "PORTAL_WEB_PUBLIC_URL", "https://tvr.lat"),
    ("admin", "Ядерный Реактор", "REACTOR_WEB_PUBLIC_URL", "https://reactor.tvr.lat"),
    ("consensus", "Консенсус", "CONSENSUS_WEB_PUBLIC_URL", "https://consensus.tvr.lat"),
    ("atlas", "Atlas", "ATLAS_WEB_PUBLIC_URL", "https://atlas.tvr.lat"),
    ("zigmund", "Zigmund", "ZIGMUND_WEB_PUBLIC_URL", "https://zigmund.tvr.lat"),
)

_worker_task: asyncio.Task[Any] | None = None
_domain_lock: asyncio.Lock | None = None
_domain_cache: tuple[float, list[dict[str, Any]]] | None = None
_last_maintenance: dict[str, Any] | None = None
_last_maintenance_error: str | None = None


def _update_status_path() -> Path:
    return Path(
        os.getenv(
            "TMOD_UPDATE_STATUS_FILE",
            "/app/persistent/updates/status.json",
        )
    )


def read_update_status() -> dict[str, Any]:
    path = _update_status_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("release", os.getenv("TMOD_RELEASE", "unknown"))
    payload.setdefault("state", "unknown")
    payload["status_file"] = str(path)
    return payload


def _probe_https(url: str, *, timeout: float = 4.0) -> dict[str, Any]:
    parsed = urlsplit(url)
    host = str(parsed.hostname or "").strip()
    port = int(parsed.port or 443)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    started = time.monotonic()
    if parsed.scheme != "https" or not host:
        return {"status": "disabled", "url": url, "error": "invalid_https_url"}
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as secured:
                certificate = secured.getpeercert()
                secured.settimeout(timeout)
                request = (
                    f"HEAD {path} HTTP/1.1\r\nHost: {host}\r\n"
                    "User-Agent: T-Mod-Reliability/1\r\nConnection: close\r\n\r\n"
                )
                secured.sendall(request.encode("ascii", errors="ignore"))
                first_line = secured.recv(2048).split(b"\r\n", 1)[0].decode(
                    "latin-1", errors="replace"
                )
        parts = first_line.split(" ", 2)
        http_status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        not_after = certificate.get("notAfter")
        expires_at = None
        days_remaining = None
        if not_after:
            expiry_epoch = ssl.cert_time_to_seconds(str(not_after))
            expires_at = datetime.fromtimestamp(expiry_epoch, tz=timezone.utc).isoformat()
            days_remaining = max(
                0,
                int((expiry_epoch - datetime.now(timezone.utc).timestamp()) // 86400),
            )
        accepted = 200 <= http_status < 500
        probe_status = (
            "warning"
            if not accepted
            or (days_remaining is not None and days_remaining <= 14)
            else "ok"
        )
        return {
            "status": probe_status,
            "url": url,
            "host": host,
            "http_status": http_status,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "certificate_expires_at": expires_at,
            "certificate_days_remaining": days_remaining,
        }
    except Exception as exc:
        return {
            "status": "critical",
            "url": url,
            "host": host,
            "http_status": None,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "error": f"{type(exc).__name__}: {str(exc)[:400]}",
        }


async def public_surface_health(*, force: bool = False) -> list[dict[str, Any]]:
    global _domain_cache, _domain_lock
    now = time.monotonic()
    if (
        not force
        and _domain_cache is not None
        and now - _domain_cache[0] < RELIABILITY_DOMAIN_CACHE_SECONDS
    ):
        return [dict(item) for item in _domain_cache[1]]
    if not force and _domain_cache is None:
        # Dashboard requests must never wait on public DNS/TLS.  The worker
        # fills this cache immediately after on_ready and then on schedule.
        return [
            {
                "id": surface_id,
                "title": title,
                "url": os.getenv(env_name, default).strip(),
                "status": "warning",
                "error": "initial_probe_pending",
            }
            for surface_id, title, env_name, default in PUBLIC_SURFACES
        ]
    if _domain_lock is None:
        _domain_lock = asyncio.Lock()
    async with _domain_lock:
        now = time.monotonic()
        if (
            not force
            and _domain_cache is not None
            and now - _domain_cache[0] < RELIABILITY_DOMAIN_CACHE_SECONDS
        ):
            return [dict(item) for item in _domain_cache[1]]
        specifications = [
            (surface_id, title, os.getenv(env_name, default).strip())
            for surface_id, title, env_name, default in PUBLIC_SURFACES
        ]
        reports = await asyncio.gather(
            *(
                asyncio.to_thread(_probe_https, url)
                for _, _, url in specifications
            )
        )
        projected = [
            {"id": surface_id, "title": title, **report}
            for (surface_id, title, _), report in zip(specifications, reports)
        ]
        _domain_cache = (time.monotonic(), projected)
        return [dict(item) for item in projected]


async def reliability_snapshot(*, force: bool = False) -> dict[str, Any]:
    domains = await public_surface_health(force=force)
    database = await asyncio.to_thread(database_protection_snapshot)
    update = await asyncio.to_thread(read_update_status)
    statuses = [str(database.get("status") or "warning")]
    statuses.extend(str(item.get("status") or "warning") for item in domains)
    overall = (
        "critical"
        if "critical" in statuses
        else "warning"
        if any(item != "ok" for item in statuses)
        else "ok"
    )
    return {
        "overall": overall,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release": os.getenv("TMOD_RELEASE", "unknown"),
        "uptime_seconds": max(0, round(time.monotonic() - STARTED_MONOTONIC)),
        "database": database,
        "domains": domains,
        "update": update,
        "maintenance": _last_maintenance,
        "maintenance_error": _last_maintenance_error,
    }


async def reliability_worker(bot: Any) -> None:
    global _last_maintenance, _last_maintenance_error
    while not bot.is_closed():
        try:
            if os.getenv("TMOD_DB_PROTECTION_OWNER", "bot").strip().lower() == "bot":
                _last_maintenance = await asyncio.to_thread(
                    run_scheduled_database_protection
                )
            await public_surface_health(force=True)
            _last_maintenance_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _last_maintenance_error = f"{type(exc).__name__}: {str(exc)[:700]}"
        try:
            await asyncio.sleep(RELIABILITY_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


def setup_reliability(bot: Any) -> None:
    @bot.listen("on_ready")
    async def reliability_on_ready() -> None:
        global _worker_task
        if _worker_task is None or _worker_task.done():
            _worker_task = asyncio.create_task(
                reliability_worker(bot), name="tmod-reliability-worker"
            )


__all__ = [
    "PUBLIC_SURFACES",
    "RELIABILITY_INTERVAL_SECONDS",
    "public_surface_health",
    "read_update_status",
    "reliability_snapshot",
    "reliability_worker",
    "setup_reliability",
]
