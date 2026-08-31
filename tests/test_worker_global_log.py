from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

import worker_main


class WorkerGlobalLogTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for_status(self, client: TestClient, expected: str) -> dict:
        for _ in range(50):
            response = await client.get("/health")
            payload = await response.json()
            if payload["status"] == expected:
                return payload
            await asyncio.sleep(0.01)
        self.fail(f"worker status did not become {expected}")

    async def test_successful_cycle_emits_correlated_lifecycle_events(self) -> None:
        events: list[dict] = []
        with (
            patch.object(
                worker_main,
                "start_global_log_runtime",
                new=AsyncMock(return_value={"enabled": True, "running": True}),
            ),
            patch.object(
                worker_main,
                "global_log_runtime_health",
                return_value={"enabled": True, "running": True},
            ),
            patch.object(worker_main, "emit_global_event", side_effect=lambda event: events.append(event) or True),
            patch.object(
                worker_main,
                "run_scheduled_database_protection",
                return_value={"protection": {"status": "ok", "backend": "postgresql"}},
            ),
        ):
            client = TestClient(TestServer(await worker_main.create_app()))
            await client.start_server()
            try:
                payload = await self._wait_for_status(client, "ok")
                self.assertTrue(payload["global_log"]["running"])
                kinds = [event["event_type"] for event in events]
                self.assertIn("worker_started", kinds)
                self.assertIn("database_protection_started", kinds)
                self.assertIn("database_protection_completed", kinds)
                started = next(event for event in events if event["event_type"] == "database_protection_started")
                completed = next(event for event in events if event["event_type"] == "database_protection_completed")
                self.assertEqual(started["request_id"], completed["request_id"])
                self.assertEqual(completed["details"]["database_status"], "ok")
            finally:
                await client.close()
        self.assertIn("worker_stopped", [event["event_type"] for event in events])

    async def test_audit_startup_failure_does_not_block_worker(self) -> None:
        events: list[dict] = []
        with (
            patch.object(
                worker_main,
                "start_global_log_runtime",
                new=AsyncMock(side_effect=RuntimeError("audit_database_unavailable")),
            ),
            patch.object(
                worker_main,
                "global_log_runtime_health",
                side_effect=RuntimeError("audit_health_unavailable"),
            ),
            patch.object(worker_main, "emit_global_event", side_effect=lambda event: events.append(event) or True),
            patch.object(
                worker_main,
                "run_scheduled_database_protection",
                return_value={"protection": {"status": "ok", "backend": "postgresql"}},
            ),
        ):
            client = TestClient(TestServer(await worker_main.create_app()))
            await client.start_server()
            try:
                payload = await self._wait_for_status(client, "ok")
                self.assertEqual(payload["liveness"], "ok")
                self.assertFalse(payload["global_log"]["running"])
                self.assertIn("audit_health_unavailable", payload["global_log"]["last_error"])
            finally:
                await client.close()

    async def test_failed_cycle_emits_error_without_losing_liveness(self) -> None:
        events: list[dict] = []

        def fail() -> dict:
            raise RuntimeError("backup_failed")

        with (
            patch.object(
                worker_main,
                "start_global_log_runtime",
                new=AsyncMock(return_value={"enabled": True, "running": True}),
            ),
            patch.object(
                worker_main,
                "global_log_runtime_health",
                return_value={"enabled": True, "running": True},
            ),
            patch.object(worker_main, "emit_global_event", side_effect=lambda event: events.append(event) or True),
            patch.object(worker_main, "run_scheduled_database_protection", side_effect=fail),
        ):
            client = TestClient(TestServer(await worker_main.create_app()))
            await client.start_server()
            try:
                payload = await self._wait_for_status(client, "degraded")
                self.assertEqual(payload["liveness"], "ok")
                failure = next(event for event in events if event["event_type"] == "database_protection_failed")
                self.assertEqual(failure["severity"], "error")
                self.assertEqual(failure["details"]["exception_type"], "RuntimeError")
                self.assertIn("backup_failed", failure["details"]["exception"])
            finally:
                await client.close()


if __name__ == "__main__":
    unittest.main()
