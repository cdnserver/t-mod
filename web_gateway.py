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


def _edge_spool_path() -> Path:
    return Path(os.getenv("TMOD_EDGE_SPOOL_FILE", "/app/persistent/data/global-log-edge-spool.jsonl"))


def _queue_edge(application: web.Application, event: dict[str, object]) -> None:
    event.setdefault("event_uuid", str(uuid4()))
    event.setdefault("occurred_at", datetime.now(timezone.utc).isoformat())
    application[EDGE_QUEUE].append(event)


def _store_edge_spool(queue: deque[dict[str, object]]) -> None:
    try:
        path = _edge_spool_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not queue:
            path.unlink(missing_ok=True)
            return
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            for event in queue:
                stream.write(json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        # Edge observability must never take the public gateway down.
        return


def _restore_edge_spool(queue: deque[dict[str, object]]) -> int:
    path = _edge_spool_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
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
    return restored


def _headers(source: web.BaseRequest | web.StreamResponse) -> dict[str, str]:
    headers = {
        key: value
        for key, value in source.headers.items()
        if key.lower() not in HOP_HEADERS and key.lower() != "host"
    }
    if isinstance(source, web.BaseRequest):
        headers["X-Forwarded-Host"] = source.host
        headers["X-Forwarded-Proto"] = source.headers.get(
            "X-Forwarded-Proto", source.scheme
        )
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
        {"status": "ok", "service": "tmod-web", "upstream": UPSTREAM}
    )


async def proxy(request: web.Request) -> web.StreamResponse:
    session = request.app[UPSTREAM_SESSION]
    target = f"{UPSTREAM}{request.rel_url}"
    started_at = time.perf_counter()
    request_id = request.headers.get("X-Request-ID", "").strip()[:120] or str(uuid4())
    upstream_headers = _headers(request)
    upstream_headers["X-Request-ID"] = request_id
    try:
        upstream = await session.request(
            request.method,
            target,
            headers=upstream_headers,
            data=request.content if request.can_read_body else None,
            allow_redirects=False,
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
            await asyncio.to_thread(_store_edge_spool, queue)
            await asyncio.sleep(1.5)


async def create_app() -> web.Application:
    upload_mib = max(
        16, min(514, int(os.getenv("TMOD_WEB_MAX_UPLOAD_MIB", "514") or 514))
    )
    app = web.Application(client_max_size=upload_mib * 1024 * 1024)
    app[EDGE_QUEUE] = deque()

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
    app.router.add_route("*", "/{tail:.*}", proxy)
    return app


def main() -> None:
    web.run_app(create_app(), host=HOST, port=PORT, access_log=None)


if __name__ == "__main__":
    main()
