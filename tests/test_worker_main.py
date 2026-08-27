from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

import worker_main


class WorkerMainTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, result: object) -> TestClient:
        patcher = patch.object(worker_main, "run_scheduled_database_protection", result)
        patcher.start()
        self.addCleanup(patcher.stop)
        client = TestClient(TestServer(await worker_main.create_app()))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def _wait_for_status(self, client: TestClient, expected: str) -> dict:
        for _ in range(40):
            response = await client.get("/health")
            payload = await response.json()
            if payload["status"] == expected:
                return payload
            await asyncio.sleep(0.01)
        self.fail(f"worker status did not become {expected}")

    async def test_liveness_is_fast_and_readiness_uses_cached_protection(self) -> None:
        client = await self._client(
            lambda: {"protection": {"status": "ok", "backend": "postgresql"}}
        )
        payload = await self._wait_for_status(client, "ok")
        self.assertEqual(payload["liveness"], "ok")
        self.assertEqual(payload["database"]["backend"], "postgresql")

        ready = await client.get("/ready")
        self.assertEqual(ready.status, 200)
        self.assertTrue((await ready.json())["ready"])

    async def test_maintenance_failure_degrades_readiness_not_liveness(self) -> None:
        def fail() -> dict:
            raise RuntimeError("postgres_temporarily_unavailable")

        client = await self._client(fail)
        payload = await self._wait_for_status(client, "degraded")
        self.assertEqual(payload["liveness"], "ok")
        self.assertIn("postgres_temporarily_unavailable", payload["last_error"])

        ready = await client.get("/ready")
        self.assertEqual(ready.status, 503)
        self.assertFalse((await ready.json())["ready"])


if __name__ == "__main__":
    unittest.main()
