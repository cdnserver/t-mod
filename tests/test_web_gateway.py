from __future__ import annotations

import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import web_gateway


class WebGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        backend = web.Application()

        async def echo(request: web.Request) -> web.Response:
            return web.Response(
                body=await request.read(),
                status=201,
                headers={"X-TMod-Upstream": "discord"},
            )

        backend.router.add_post("/api/echo", echo)
        self.backend = TestServer(backend)
        await self.backend.start_server()
        self.upstream_patch = patch.object(
            web_gateway, "UPSTREAM", str(self.backend.make_url("")).rstrip("/")
        )
        self.upstream_patch.start()
        self.gateway = TestClient(TestServer(await web_gateway.create_app()))
        await self.gateway.start_server()

    async def asyncTearDown(self) -> None:
        await self.gateway.close()
        await self.backend.close()
        self.upstream_patch.stop()

    async def test_gateway_has_independent_health_and_proxies_body_and_headers(self) -> None:
        health = await self.gateway.get("/gateway-health")
        self.assertEqual(health.status, 200)
        self.assertEqual((await health.json())["service"], "tmod-web")

        response = await self.gateway.post("/api/echo", data=b"postgres-ready")
        self.assertEqual(response.status, 201)
        self.assertEqual(response.headers["X-TMod-Upstream"], "discord")
        self.assertEqual(await response.read(), b"postgres-ready")

    async def test_gateway_returns_bounded_503_when_runtime_is_down(self) -> None:
        await self.backend.close()
        response = await self.gateway.get("/api/health")
        self.assertEqual(response.status, 503)
        payload = await response.json()
        self.assertEqual(payload["error"], "tmod_runtime_temporarily_unavailable")


if __name__ == "__main__":
    unittest.main()
