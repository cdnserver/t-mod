"""Private web console and ingestion endpoints for the global T-Mod log."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from aiohttp import web

from modules.consensus_web_auth import signed_session_identity
from modules.global_log_runtime import emit_global_event, hash_remote, redact_value, runtime_health
from persistence import global_log_repository as repository


_COOKIE = "tmod_global_log_session"
_login_attempts: dict[str, deque[float]] = defaultdict(deque)
_client_events: dict[str, deque[float]] = defaultdict(deque)


def _limited(bucket: dict[str, deque[float]], key: str, *, count: int, window: int) -> bool:
    now = time.monotonic()
    selected = bucket[key]
    while selected and now - selected[0] > window:
        selected.popleft()
    if len(selected) >= count:
        return True
    selected.append(now)
    if len(bucket) > 5000:
        for old_key in list(bucket)[:1000]:
            if not bucket[old_key] or now - bucket[old_key][-1] > window:
                bucket.pop(old_key, None)
    return False


def _remote(request: web.Request) -> str:
    return request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip() or str(request.remote or "unknown")


def _token(request: web.Request) -> str:
    return str(request.cookies.get(_COOKIE) or "").strip()


async def _principal(request: web.Request) -> dict[str, Any] | None:
    token = _token(request)
    if not token:
        return None
    return await asyncio.to_thread(repository.resolve_session, token)


async def _require_principal(request: web.Request) -> dict[str, Any]:
    principal = await _principal(request)
    if principal is None:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "global_log_auth_required"}),
            content_type="application/json",
        )
    return principal


def _filters(request: web.Request) -> dict[str, Any]:
    return {
        key: request.query.get(key)
        for key in (
            "q", "cursor", "source", "source_type", "event_type", "severity",
            "actor", "channel", "status", "from", "to", "limit",
        )
        if request.query.get(key) not in (None, "")
    }


def _origin_allowed(request: web.Request) -> bool:
    origin = str(request.headers.get("Origin") or "").strip()
    if not origin:
        return False
    parsed = urlsplit(origin)
    hostname = str(parsed.hostname or "").lower()
    return (
        parsed.scheme == "https" and (hostname == "tvr.lat" or hostname.endswith(".tvr.lat"))
    ) or hostname in {"127.0.0.1", "localhost"}


def register_global_log_web_routes(
    app: web.Application,
    *,
    guild_id: int,
    asset_dir: Path,
) -> None:
    async def page(_: web.Request) -> web.FileResponse:
        return web.FileResponse(asset_dir / "global-log.html")

    async def session(request: web.Request) -> web.Response:
        principal = await _principal(request)
        return web.json_response({
            "authenticated": principal is not None,
            "principal": principal,
            "runtime": runtime_health() if principal is not None else None,
        })

    async def login(request: web.Request) -> web.Response:
        remote = _remote(request)
        if _limited(_login_attempts, remote, count=10, window=300):
            return web.json_response({"error": "too_many_attempts"}, status=429)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        user_raw = str(body.get("user_id") or "").strip()
        code = str(body.get("code") or "").strip()
        user_id = int(user_raw) if user_raw.isdigit() else 0
        token = await asyncio.to_thread(
            repository.consume_login_code,
            user_id,
            code,
            ip_hash=hash_remote(remote),
            user_agent=request.headers.get("User-Agent"),
        )
        if token is None:
            emit_global_event({
                "source_service": "global-log",
                "source_type": "authentication",
                "event_type": "login_failed",
                "severity": "warning",
                "actor_user_id": user_id or None,
                "summary": "Отклонена попытка входа в глобальный журнал",
                "ip_hash": hash_remote(remote),
                "details": {"reason": "invalid_or_expired_code"},
            })
            await asyncio.sleep(0.35)
            return web.json_response({"error": "invalid_or_expired_code"}, status=401)
        response = web.json_response({"ok": True})
        response.set_cookie(
            _COOKIE,
            token,
            max_age=7200,
            secure=True,
            httponly=True,
            samesite="Strict",
            path="/",
        )
        emit_global_event({
            "source_service": "global-log",
            "source_type": "authentication",
            "event_type": "login_succeeded",
            "actor_user_id": user_id,
            "summary": "Оператор вошёл в глобальный журнал",
            "ip_hash": hash_remote(remote),
        })
        return response

    async def logout(request: web.Request) -> web.Response:
        principal = await _principal(request)
        await asyncio.to_thread(repository.revoke_session, _token(request))
        response = web.json_response({"ok": True})
        response.del_cookie(_COOKIE, path="/")
        emit_global_event({
            "source_service": "global-log", "source_type": "authentication",
            "event_type": "logout", "actor_user_id": (principal or {}).get("user_id"),
            "summary": "Оператор вышел из глобального журнала",
        })
        return response

    async def events(request: web.Request) -> web.Response:
        principal = await _require_principal(request)
        result = await asyncio.to_thread(repository.search_events, _filters(request))
        emit_global_event({
            "source_service": "global-log", "source_type": "operator",
            "event_type": "events_searched", "actor_user_id": principal["user_id"],
            "summary": "Выполнен поиск по глобальному журналу",
            "details": {"filters": _filters(request), "returned": len(result["events"])},
        })
        return web.json_response(result)

    async def event_facets(request: web.Request) -> web.Response:
        await _require_principal(request)
        return web.json_response(await asyncio.to_thread(repository.facets))

    async def integrity(request: web.Request) -> web.Response:
        principal = await _require_principal(request)
        result = await asyncio.to_thread(repository.verify_chain, limit=100_000)
        emit_global_event({
            "source_service": "global-log", "source_type": "operator",
            "event_type": "integrity_checked", "actor_user_id": principal["user_id"],
            "severity": "info" if result.get("ok") else "critical",
            "summary": "Проверена цепочка целостности глобального журнала",
            "details": result,
        })
        return web.json_response(result)

    async def export(request: web.Request) -> web.Response:
        principal = await _require_principal(request)
        filters = _filters(request)
        filters["limit"] = 200
        gathered: list[dict[str, Any]] = []
        for _ in range(25):
            page_result = await asyncio.to_thread(repository.search_events, filters)
            gathered.extend(page_result["events"])
            if not page_result.get("next_cursor"):
                break
            filters["cursor"] = page_result["next_cursor"]
        format_name = str(request.query.get("format") or "json").lower()
        emit_global_event({
            "source_service": "global-log", "source_type": "operator",
            "event_type": "events_exported", "actor_user_id": principal["user_id"],
            "summary": f"Экспортировано {len(gathered)} событий",
            "details": {"format": format_name, "count": len(gathered)},
        })
        if format_name == "csv":
            stream = io.StringIO()
            columns = [
                "id", "occurred_at", "source_service", "source_type", "event_type",
                "severity", "actor_user_id", "actor_display", "guild_id", "channel_id",
                "message_id", "status_code", "duration_ms", "summary", "content_text",
                "details", "event_hash",
            ]
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for item in gathered:
                row = dict(item)
                row["details"] = json.dumps(row.get("details") or {}, ensure_ascii=False, default=str)
                writer.writerow(row)
            return web.Response(
                body=("\ufeff" + stream.getvalue()).encode("utf-8"),
                content_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=tmod-global-log.csv"},
            )
        return web.Response(
            body=json.dumps({"events": gathered}, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
            content_type="application/json",
            headers={"Content-Disposition": "attachment; filename=tmod-global-log.json"},
        )

    async def client_event(request: web.Request) -> web.Response:
        if not _origin_allowed(request):
            return web.json_response({"error": "origin_required"}, status=403)
        remote = _remote(request)
        if _limited(_client_events, remote, count=240, window=60):
            return web.json_response({"error": "rate_limited"}, status=429)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid_payload"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid_payload"}, status=400)
        identity = signed_session_identity(request, expected_guild_id=int(guild_id))
        actor_user_id = int(identity[1]) if identity is not None else None
        event_name = str(body.get("event") or "client_event")[:120]
        emit_global_event({
            "source_service": str(body.get("service") or "web-client")[:120],
            "source_type": "client",
            "event_type": event_name,
            "actor_user_id": actor_user_id,
            "summary": str(body.get("summary") or event_name)[:1000],
            "target_type": str(body.get("target_type") or "ui")[:120],
            "target_id": str(body.get("target_id") or "")[:500] or None,
            "content_text": str(body.get("content") or "")[:20_000] or None,
            "details": redact_value(body.get("details") or {}),
            "ip_hash": hash_remote(remote),
            "user_agent": request.headers.get("User-Agent", "")[:1000],
        })
        return web.json_response({"ok": True}, status=202)

    app.router.add_get("/global-log", page)
    app.router.add_get("/global-log/", page)
    app.router.add_get("/api/global-log/session", session)
    app.router.add_post("/api/global-log/login", login)
    app.router.add_post("/api/global-log/logout", logout)
    app.router.add_get("/api/global-log/events", events)
    app.router.add_get("/api/global-log/facets", event_facets)
    app.router.add_get("/api/global-log/integrity", integrity)
    app.router.add_get("/api/global-log/export", export)
    app.router.add_post("/api/global-log/client", client_event)


__all__ = ["register_global_log_web_routes"]
