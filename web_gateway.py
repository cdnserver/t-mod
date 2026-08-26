"""Stable public edge for T-Mod web surfaces.

The Discord runtime still owns Discord-dependent handlers, but it is no longer
the public upstream.  This gateway remains healthy during Discord reconnects,
provides bounded failures, and is the seam for moving read-only routes into a
dedicated web service without another public deployment change.
"""

from __future__ import annotations

import asyncio
import os
from typing import Final

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
    try:
        upstream = await session.request(
            request.method,
            target,
            headers=_headers(request),
            data=request.content if request.can_read_body else None,
            allow_redirects=False,
        )
    except (ClientError, asyncio.TimeoutError) as exc:
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

    response = web.StreamResponse(
        status=upstream.status,
        reason=upstream.reason,
        headers=_headers(upstream),
    )
    await response.prepare(request)
    try:
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await response.write(chunk)
    finally:
        upstream.release()
    await response.write_eof()
    return response


async def create_app() -> web.Application:
    upload_mib = max(
        16, min(514, int(os.getenv("TMOD_WEB_MAX_UPLOAD_MIB", "514") or 514))
    )
    app = web.Application(client_max_size=upload_mib * 1024 * 1024)

    async def start(application: web.Application) -> None:
        application[UPSTREAM_SESSION] = ClientSession(
            connector=TCPConnector(limit=200, ttl_dns_cache=30),
            timeout=ClientTimeout(total=None, connect=4, sock_connect=4, sock_read=300),
            auto_decompress=False,
        )

    async def stop(application: web.Application) -> None:
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
