"""Stable public edge for T-Mod web surfaces.

The Discord runtime still owns Discord-dependent handlers, but it is no longer
the public upstream.  This gateway remains healthy during Discord reconnects,
provides bounded failures, and is the seam for moving read-only routes into a
dedicated web service without another public deployment change.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Final
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web


UPSTREAM: Final = os.getenv(
    "TMOD_INTERNAL_WEB_UPSTREAM", "http://tmod-discord-bot:8788"
).rstrip("/")
API_UPSTREAM: Final = os.getenv(
    "TMOD_INTERNAL_API_UPSTREAM", "http://tmod-api:8793"
).rstrip("/")
DESKTOP_BOOTSTRAP_PATH: Final = "/api/desktop/v1/bootstrap"
DESKTOP_BOOTSTRAP_API_PATH: Final = "/internal/desktop/v1/bootstrap"
REACTOR_NOTIFICATIONS_PATH: Final = "/api/reactor/notifications"
REACTOR_NOTIFICATIONS_API_PATH: Final = "/internal/reactor/notifications"
REACTOR_PREPARATION_PATH: Final = "/api/reactor/preparation"
REACTOR_PREPARATION_API_PATH: Final = "/internal/reactor/preparation"
API_ROUTE_MAP: Final = {
    ("GET", DESKTOP_BOOTSTRAP_PATH): DESKTOP_BOOTSTRAP_API_PATH,
    ("GET", REACTOR_NOTIFICATIONS_PATH): REACTOR_NOTIFICATIONS_API_PATH,
    ("GET", REACTOR_PREPARATION_PATH): REACTOR_PREPARATION_API_PATH,
}
HOST: Final = os.getenv("TMOD_WEB_GATEWAY_HOST", "0.0.0.0")
PORT: Final = int(os.getenv("TMOD_WEB_GATEWAY_PORT", "8787") or 8787)
HOP_HEADERS: Final = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
UPSTREAM_SESSION = web.AppKey("upstream_session", ClientSession)
EDGE_QUEUE = web.AppKey("edge_queue", deque)
EDGE_TASK = web.AppKey("edge_task", asyncio.Task)
EDGE_DROPPED = web.AppKey("edge_dropped", int)
_FORWARDED_HOST_HEADER = "X-TMod-Forwarded-Host"
_FORWARDED_PROTO_HEADER = "X-TMod-Forwarded-Proto"
_HOST_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?::[0-9]{1,5})?$"
)


def _safe_forwarded_host(value: object) -> str | None:
    raw = str(value or "").strip().lower().rstrip(".")
    if not raw or len(raw) > 255 or any(
        char in raw for char in "/?#@\\, \t\r\n"
    ):
        return None
    if not _HOST_RE.fullmatch(raw):
        return None
    host, separator, port = raw.partition(":")
    if separator and int(port) > 65535:
        return None
    return raw


def _edge_spool_path() -> Path:
    return Path(os.getenv("TMOD_EDGE_SPOOL_FILE", "/app/persistent/data/global-log-edge-spool.jsonl"))


def _edge_queue_maxsize() -> int:
    try:
        value = int(os.getenv("TMOD_EDGE_QUEUE_MAX", "5000") or 0)
    except (TypeError, ValueError):
        value = 5000
    return max(100, min(value, 50_000))


def _edge_spool_max_bytes() -> int:
    try:
        value = int(os.getenv("TMOD_EDGE_SPOOL_MAX_BYTES", str(64 * 1024 * 1024)) or 0)
    except (TypeError, ValueError):
        value = 64 * 1024 * 1024
    return max(1 * 1024 * 1024, min(value, 2 * 1024 * 1024 * 1024))


def _trim_edge_queue(application: web.Application) -> None:
    queue = application[EDGE_QUEUE]
    maximum = _edge_queue_maxsize()
    while len(queue) > maximum:
        queue.popleft()
        application[EDGE_DROPPED] += 1


def _queue_edge(application: web.Application, event: dict[str, object]) -> None:
    event.setdefault("event_uuid", str(uuid4()))
    event.setdefault("occurred_at", datetime.now(timezone.utc).isoformat())
    application[EDGE_QUEUE].append(event)
    _trim_edge_queue(application)


def _store_edge_spool(queue: deque[dict[str, object]]) -> None:
    try:
        path = _edge_spool_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not queue:
            path.unlink(missing_ok=True)
            return
        lines = [
            (json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":")) + "\n").encode("utf-8")
            for event in queue
        ]
        total = sum(len(line) for line in lines)
        maximum = _edge_spool_max_bytes()
        while lines and total > maximum:
            total -= len(lines.pop(0))
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            stream.writelines(lines)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        # Edge observability must never take the public gateway down.
        return


def _restore_edge_spool(queue: deque[dict[str, object]]) -> int:
    path = _edge_spool_path()
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            if size > _edge_spool_max_bytes():
                stream.seek(-_edge_spool_max_bytes(), os.SEEK_END)
                # The first partial JSON line is deliberately discarded.
                stream.readline()
            else:
                stream.seek(0)
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    restored = 0
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            queue.append(item)
            restored += 1
    while len(queue) > _edge_queue_maxsize():
        queue.popleft()
    return restored


def _headers(source: web.BaseRequest | web.StreamResponse) -> dict[str, str]:
    headers = {
        key: value
        for key, value in source.headers.items()
        if key.lower() not in HOP_HEADERS
        and key.lower() not in {"host", "x-tmod-forwarded-host", "x-tmod-forwarded-proto"}
    }
    if isinstance(source, web.BaseRequest):
        # Caddy supplies the browser-facing host in X-Forwarded-Host.  Keep
        # that value in a gateway-owned header instead of replacing it with
        # the internal upstream host (the old behaviour broke canonical
        # redirects and cross-subdomain cookies).  Invalid/multiple values
        # are ignored and fall back to the request host.
        forwarded_host = _safe_forwarded_host(
            source.headers.get("X-Forwarded-Host")
        ) or _safe_forwarded_host(source.host)
        if forwarded_host:
            headers[_FORWARDED_HOST_HEADER] = forwarded_host
            headers["X-Forwarded-Host"] = forwarded_host
        forwarded_proto = str(
            source.headers.get("X-Forwarded-Proto") or source.scheme
        ).strip().lower()
        headers[_FORWARDED_PROTO_HEADER] = (
            forwarded_proto if forwarded_proto in {"http", "https"} else source.scheme
        )
        headers["X-Forwarded-Proto"] = headers[_FORWARDED_PROTO_HEADER]
        headers["X-Forwarded-For"] = ", ".join(
            item
            for item in (
                source.headers.get("X-Forwarded-For", ""),
                source.remote or "",
            )
            if item
        )
    return headers


async def gateway_health(_: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "service": "tmod-web",
            "upstream": UPSTREAM,
            "api_upstream": API_UPSTREAM,
            "api_routes": sorted(path for _, path in API_ROUTE_MAP),
            "api_route_mode": "api-with-legacy-fallback",
        }
    )


async def gateway_ready(request: web.Request) -> web.Response:
    """Report readiness only when the application upstream answers health."""

    session = request.app.get(UPSTREAM_SESSION)
    if session is None:
        return web.json_response(
            {"status": "starting", "service": "tmod-web"},
            status=503,
            headers={"Retry-After": "2"},
        )
    try:
        async with session.get(
            f"{UPSTREAM}/api/health",
            headers={"X-TMod-Internal-Health": "tmod-web/v1"},
            allow_redirects=False,
            timeout=ClientTimeout(total=4, connect=1, sock_connect=1, sock_read=2),
        ) as upstream:
            if upstream.status >= 500:
                return web.json_response(
                    {
                        "status": "degraded",
                        "service": "tmod-web",
                        "upstream_status": upstream.status,
                    },
                    status=503,
                    headers={"Retry-After": "3"},
                )
    except (ClientError, asyncio.TimeoutError):
        return web.json_response(
            {"status": "degraded", "service": "tmod-web"},
            status=503,
            headers={"Retry-After": "3"},
        )
    return web.json_response({"status": "ready", "service": "tmod-web"})


def _route_targets(request: web.Request) -> tuple[str, str | None, str]:
    """Return the primary target, optional fallback, and route label.

    Only an idempotent GET with a proven parity contract is moved to the
    standalone API.  Mutating routes remain on the established runtime until
    they receive their own migration and rollback proof.
    """

    legacy_target = f"{UPSTREAM}{request.rel_url}"
    internal_path = API_ROUTE_MAP.get((request.method, request.path))
    if internal_path is not None:
        query = f"?{request.query_string}" if request.query_string else ""
        return (
            f"{API_UPSTREAM}{internal_path}{query}",
            legacy_target,
            "tmod-api",
        )
    return legacy_target, None, "legacy"


async def _discard_response(response: object) -> None:
    try:
        await asyncio.wait_for(response.read(), timeout=2)  # type: ignore[attr-defined]
    except (ClientError, asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        response.release()  # type: ignore[attr-defined]


async def _request_with_fallback(
    request: web.Request,
    *,
    session: ClientSession,
    target: str,
    fallback_target: str | None,
    route_label: str,
    upstream_headers: dict[str, str],
    request_id: str,
    started_at: float,
) -> tuple[object, str]:
    async def open_target(url: str) -> object:
        return await session.request(
            request.method,
            url,
            headers=upstream_headers,
            data=request.content if request.can_read_body else None,
            allow_redirects=False,
        )

    try:
        upstream = await open_target(target)
    except (ClientError, asyncio.TimeoutError):
        if fallback_target is None:
            raise
        _queue_edge(request.app, {
            "event_type": "gateway_route_fallback",
            "severity": "warning",
            "summary": f"Gateway switched {request.path} to the legacy fallback",
            "status_code": 0,
            "request_id": request_id,
            "duration_ms": (time.perf_counter() - started_at) * 1000,
            "target_id": request.path,
            "details": {
                "route": route_label,
                "reason": "transport_error",
            },
        })
        return await open_target(fallback_target), "legacy-fallback"

    if fallback_target is not None and int(upstream.status) >= 500:  # type: ignore[attr-defined]
        primary_status = int(upstream.status)  # type: ignore[attr-defined]
        await _discard_response(upstream)
        _queue_edge(request.app, {
            "event_type": "gateway_route_fallback",
            "severity": "warning",
            "summary": f"Gateway switched {request.path} to the legacy fallback",
            "status_code": primary_status,
            "request_id": request_id,
            "duration_ms": (time.perf_counter() - started_at) * 1000,
            "target_id": request.path,
            "details": {
                "route": route_label,
                "reason": "upstream_5xx",
                "upstream_status": primary_status,
            },
        })
        return await open_target(fallback_target), "legacy-fallback"
    return upstream, route_label


async def proxy(request: web.Request) -> web.StreamResponse:
    session = request.app[UPSTREAM_SESSION]
    target, fallback_target, route_label = _route_targets(request)
    started_at = time.perf_counter()
    request_id = request.headers.get("X-Request-ID", "").strip()[:120] or str(uuid4())
    upstream_headers = _headers(request)
    upstream_headers["X-Request-ID"] = request_id
    try:
        upstream, selected_backend = await _request_with_fallback(
            request,
            session=session,
            target=target,
            fallback_target=fallback_target,
            route_label=route_label,
            upstream_headers=upstream_headers,
            request_id=request_id,
            started_at=started_at,
        )
    except (ClientError, asyncio.TimeoutError) as exc:
        _queue_edge(request.app, {
            "event_type": "gateway_upstream_error",
            "severity": "error",
            "summary": f"Gateway {request.method} {request.path} → 503",
            "status_code": 503,
            "request_id": request_id,
            "duration_ms": (time.perf_counter() - started_at) * 1000,
            "target_id": request.path,
            "details": {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "exception": type(exc).__name__,
                "forwarded_for": request.headers.get("X-Forwarded-For"),
            },
        })
        if request.path.startswith("/api/"):
            return web.json_response(
                {
                    "status": "degraded",
                    "error": "tmod_runtime_temporarily_unavailable",
                    "detail": type(exc).__name__,
                },
                status=503,
                headers={"Retry-After": "3"},
            )
        return web.Response(
            text=(
                "<!doctype html><html lang='ru'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width'>"
                "<title>T-Mod · восстановление связи</title>"
                "<style>body{margin:0;display:grid;place-items:center;min-height:100vh;"
                "background:#05070c;color:#e8eefb;font:16px system-ui;text-align:center}"
                "main{max-width:540px;padding:48px}b{font-size:28px}p{color:#8996ad;line-height:1.6}"
                "</style><main><b>T-Mod восстанавливает связь</b>"
                "<p>Серверная часть обновляется. Страница автоматически вернётся через несколько секунд.</p>"
                "<script>setTimeout(()=>location.reload(),3000)</script></main></html>"
            ),
            content_type="text/html",
            status=503,
            headers={"Retry-After": "3"},
        )

    response_headers = _headers(upstream)
    response_headers["X-Request-ID"] = request_id
    if fallback_target is not None:
        response_headers["X-TMod-Backend"] = selected_backend
    response = web.StreamResponse(
        status=upstream.status,
        reason=upstream.reason,
        headers=response_headers,
    )
    await response.prepare(request)
    try:
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await response.write(chunk)
            await response.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            # A navigation, tab close, or cancelled download can disconnect the
            # browser while the upstream is still streaming. This is a normal
            # client lifecycle event, not a gateway failure.
            return response
    finally:
        upstream.release()
        _queue_edge(request.app, {
            "event_type": "gateway_request",
            "severity": "error" if upstream.status >= 500 else "warning" if upstream.status >= 400 else "info",
            "summary": f"Gateway {request.method} {request.path} → {upstream.status}",
            "status_code": int(upstream.status),
            "request_id": request_id,
            "duration_ms": (time.perf_counter() - started_at) * 1000,
            "target_id": request.path,
            "details": {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "forwarded_for": request.headers.get("X-Forwarded-For"),
                "backend": selected_backend,
            },
        })
    return response


async def edge_reporter(application: web.Application) -> None:
    queue = application[EDGE_QUEUE]
    session = application[UPSTREAM_SESSION]
    last_checkpoint = 0.0
    while True:
        await asyncio.sleep(0.25)
        if not queue:
            continue
        batch = []
        while queue and len(batch) < 200:
            batch.append(queue.popleft())
        try:
            response = await session.post(
                f"{UPSTREAM}/api/global-log/internal",
                json={"service": "tmod-web", "events": batch},
                headers={"X-TMod-Internal-Event": "tmod-web/v1"},
                timeout=ClientTimeout(total=3, connect=1, sock_connect=1, sock_read=2),
            )
            try:
                if response.status != 202:
                    raise RuntimeError(f"edge_report_http_{response.status}")
                await response.read()
            finally:
                response.release()
            if time.monotonic() - last_checkpoint >= 5:
                await asyncio.to_thread(_store_edge_spool, queue)
                last_checkpoint = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            for event in reversed(batch):
                queue.appendleft(event)
            _trim_edge_queue(application)
            await asyncio.to_thread(_store_edge_spool, queue)
            await asyncio.sleep(1.5)


async def create_app() -> web.Application:
    upload_mib = max(
        16, min(514, int(os.getenv("TMOD_WEB_MAX_UPLOAD_MIB", "514") or 514))
    )
    app = web.Application(client_max_size=upload_mib * 1024 * 1024)
    app[EDGE_QUEUE] = deque()
    app[EDGE_DROPPED] = 0

    async def start(application: web.Application) -> None:
        application[UPSTREAM_SESSION] = ClientSession(
            connector=TCPConnector(limit=200, ttl_dns_cache=30),
            timeout=ClientTimeout(total=None, connect=4, sock_connect=4, sock_read=300),
            auto_decompress=False,
        )
        await asyncio.to_thread(_restore_edge_spool, application[EDGE_QUEUE])
        application[EDGE_TASK] = asyncio.create_task(
            edge_reporter(application), name="tmod-edge-event-reporter"
        )

    async def stop(application: web.Application) -> None:
        task = application[EDGE_TASK]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(_store_edge_spool, application[EDGE_QUEUE])
        await application[UPSTREAM_SESSION].close()

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    app.router.add_get("/gateway-health", gateway_health)
    app.router.add_get("/gateway-ready", gateway_ready)
    app.router.add_route("*", "/{tail:.*}", proxy)
    return app


def main() -> None:
    web.run_app(create_app(), host=HOST, port=PORT, access_log=None)


if __name__ == "__main__":
    main()
