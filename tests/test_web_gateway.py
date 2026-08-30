from __future__ import annotations

import asyncio
import unittest
from collections import deque
from unittest.mock import AsyncMock, patch

from aiohttp import ClientError, web
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

    async def test_gateway_returns_bounded_503_on_upstream_timeout(self) -> None:
        session = self.gateway.app[web_gateway.UPSTREAM_SESSION]
        timed_out = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch.object(session, "request", new=timed_out):
            response = await self.gateway.get("/api/slow-upstream")

        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers["Retry-After"], "3")
        payload = await response.json()
        self.assertEqual(payload["error"], "tmod_runtime_temporarily_unavailable")
        self.assertEqual(payload["detail"], "TimeoutError")
        timed_out.assert_awaited_once()

    async def test_edge_reporter_requeues_batch_after_delivery_error(self) -> None:
        queued = deque(
            [
                {"event_type": "first", "status_code": 200},
                {"event_type": "second", "status_code": 503},
            ],
            maxlen=100,
        )
        delivery_attempted = asyncio.Event()

        class FailingSession:
            async def post(self, *_args: object, **_kwargs: object) -> None:
                delivery_attempted.set()
                raise ClientError("global log unavailable")

        application = {
            web_gateway.EDGE_QUEUE: queued,
            web_gateway.UPSTREAM_SESSION: FailingSession(),
        }
        task = asyncio.create_task(web_gateway.edge_reporter(application))
        try:
            await asyncio.wait_for(delivery_attempted.wait(), timeout=1)
            await asyncio.sleep(0)
            self.assertEqual(
                list(queued),
                [
                    {"event_type": "first", "status_code": 200},
                    {"event_type": "second", "status_code": 503},
                ],
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_edge_reporter_failure_does_not_break_successful_response(self) -> None:
        queue = self.gateway.app[web_gateway.EDGE_QUEUE]
        queue.clear()
        session = self.gateway.app[web_gateway.UPSTREAM_SESSION]
        report_failed = AsyncMock(side_effect=ClientError("global log unavailable"))

        with patch.object(session, "post", new=report_failed):
            response = await self.gateway.post("/api/echo", data=b"still-online")
            self.assertEqual(response.status, 201)
            self.assertEqual(await response.read(), b"still-online")

            for _ in range(20):
                if report_failed.await_count and queue:
                    break
                await asyncio.sleep(0.05)

        self.assertGreaterEqual(report_failed.await_count, 1)
        self.assertTrue(
            any(
                event.get("event_type") == "gateway_request"
                and event.get("status_code") == 201
                and event.get("target_id") == "/api/echo"
                for event in queue
            )
        )


if __name__ == "__main__":
    unittest.main()
