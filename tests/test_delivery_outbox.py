import asyncio
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import storage
from modules.delivery_outbox import (
    DeliveryDeferred,
    DeliveryReceipt,
    OutboxDispatcher,
    StorageOutboxRepository,
)
from modules.delivery_runtime import _report_dead_deliveries, delivery_worker


UTC = timezone.utc


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class TemporaryOutboxDatabase:
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.old_activity_file = storage.LEGACY_ACTIVITY_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "delivery-outbox-test.db"
        storage.LEGACY_ACTIVITY_FILE = storage.DATA_DIR / "activity.json"
        storage.init_db()
        self.started_at = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        storage.LEGACY_ACTIVITY_FILE = self.old_activity_file
        self.temp_dir.cleanup()

    def enqueue(
        self,
        key: str,
        *,
        topic: str = "test.delivery",
        payload: dict | None = None,
        max_attempts: int = 8,
        priority: int = 0,
        supersede_key: str | None = None,
    ) -> dict:
        return storage.delivery_outbox_enqueue(
            topic=topic,
            dedupe_key=key,
            payload=payload or {"key": key},
            max_attempts=max_attempts,
            priority=priority,
            supersede_key=supersede_key,
            now=self.started_at.isoformat(),
        )


class DeliveryOutboxStorageTests(TemporaryOutboxDatabase, unittest.TestCase):
    def test_connection_enqueue_obeys_caller_commit_and_rollback(self) -> None:
        with storage._db_lock, storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rolled_back = storage.delivery_outbox_enqueue_in_connection(
                connection,
                topic="test.atomic",
                dedupe_key="rolled-back",
                payload={"state": "must-not-survive"},
                now=self.started_at.isoformat(),
            )
            self.assertGreater(int(rolled_back["id"]), 0)
            connection.rollback()

        self.assertEqual(storage.delivery_outbox_counts(), {})

        with storage._db_lock, storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            committed = storage.delivery_outbox_enqueue_in_connection(
                connection,
                topic="test.atomic",
                dedupe_key="committed",
                payload={"state": "survives"},
                now=self.started_at.isoformat(),
            )
            connection.commit()

        persisted = storage.delivery_outbox_get(int(committed["id"]))
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["status"], "pending")  # type: ignore[index]
        self.assertEqual(json.loads(persisted["payload_json"]), {"state": "survives"})  # type: ignore[index]

    def test_delivered_payload_is_compacted_without_losing_dedupe_identity(self) -> None:
        row = self.enqueue(
            "privacy",
            payload={
                "payload_version": 1,
                "guild_id": 77,
                "session_key": "77:test",
                "destination": "participant_dm",
                "participants": [{"user_id": 2, "display_name": "Участник"}],
                "result": {"bill_id": 10, "votes": {"2": "yes"}},
            },
        )
        claimed = storage.delivery_outbox_claim(
            worker_id="privacy-worker",
            limit=1,
            lease_seconds=30,
            now=self.started_at.isoformat(),
        )[0]

        self.assertTrue(
            storage.delivery_outbox_mark_delivered(
                int(row["id"]),
                lease_token=str(claimed["lease_token"]),
                message_id=9001,
                now=self.started_at.isoformat(),
            )
        )

        delivered = storage.delivery_outbox_get(int(row["id"]))
        compacted = json.loads(str(delivered["payload_json"]))  # type: ignore[index]
        self.assertEqual(compacted["guild_id"], 77)
        self.assertEqual(compacted["bill_id"], 10)
        self.assertTrue(compacted["compacted"])
        self.assertNotIn("participants", compacted)
        self.assertNotIn("votes", json.dumps(compacted))

    def test_dedupe_keeps_first_payload_and_delivery_policy(self) -> None:
        first = storage.delivery_outbox_enqueue(
            topic="test.dedupe",
            dedupe_key="same-command",
            payload={"version": 1},
            max_attempts=2,
            available_at=(self.started_at + timedelta(seconds=30)).isoformat(),
            now=self.started_at.isoformat(),
        )
        second = storage.delivery_outbox_enqueue(
            topic="test.dedupe",
            dedupe_key="same-command",
            payload={"version": 2},
            max_attempts=99,
            available_at=(self.started_at + timedelta(seconds=60)).isoformat(),
            now=(self.started_at + timedelta(seconds=1)).isoformat(),
        )

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(json.loads(second["payload_json"]), {"version": 1})
        self.assertEqual(second["max_attempts"], 2)
        self.assertEqual(second["available_at"], (self.started_at + timedelta(seconds=30)).isoformat())
        self.assertEqual(storage.delivery_outbox_counts(), {"pending": 1})

    def test_concurrent_workers_claim_disjoint_batches(self) -> None:
        expected_ids = {int(self.enqueue(f"batch-{index}")["id"]) for index in range(10)}
        ready = threading.Barrier(2)

        def claim(worker_id: str) -> list[dict]:
            ready.wait(timeout=5)
            return storage.delivery_outbox_claim(
                worker_id=worker_id,
                limit=5,
                lease_seconds=30,
                now=self.started_at.isoformat(),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(claim, "worker-a")
            second_future = pool.submit(claim, "worker-b")
            first = first_future.result(timeout=10)
            second = second_future.result(timeout=10)

        first_ids = {int(row["id"]) for row in first}
        second_ids = {int(row["id"]) for row in second}
        self.assertEqual(len(first_ids), 5)
        self.assertEqual(len(second_ids), 5)
        self.assertTrue(first_ids.isdisjoint(second_ids))
        self.assertEqual(first_ids | second_ids, expected_ids)
        self.assertEqual({int(row["attempts"]) for row in first + second}, {1})
        self.assertEqual({str(row["lease_owner"]) for row in first + second}, {"worker-a", "worker-b"})

    def test_interactive_delivery_is_claimed_before_older_background_work(self) -> None:
        background = self.enqueue("background", priority=0)
        interactive = self.enqueue("interactive", priority=100)

        claimed = storage.delivery_outbox_claim(
            worker_id="priority-worker",
            limit=1,
            lease_seconds=30,
            now=self.started_at.isoformat(),
        )

        self.assertEqual(int(claimed[0]["id"]), int(interactive["id"]))
        self.assertNotEqual(int(claimed[0]["id"]), int(background["id"]))
        self.assertEqual(int(claimed[0]["priority"]), 100)

    def test_new_control_generation_cancels_older_queued_copy(self) -> None:
        first = self.enqueue(
            "control-1",
            topic="tvrs.consensus.control-dm.v1",
            priority=100,
            supersede_key="session:control:user-2",
        )
        second = self.enqueue(
            "control-2",
            topic="tvrs.consensus.control-dm.v1",
            priority=100,
            supersede_key="session:control:user-2",
        )

        self.assertEqual(storage.delivery_outbox_get(int(first["id"]))["status"], "cancelled")  # type: ignore[index]
        self.assertEqual(storage.delivery_outbox_get(int(second["id"]))["status"], "pending")  # type: ignore[index]
        claimed = storage.delivery_outbox_claim(
            worker_id="supersede-worker",
            limit=10,
            lease_seconds=30,
            now=self.started_at.isoformat(),
        )
        self.assertEqual([int(item["id"]) for item in claimed], [int(second["id"])])

    def test_claimed_old_generation_detects_newer_control_before_sending(self) -> None:
        scope = "session:control:user-2"
        first = self.enqueue(
            "control-processing",
            topic="tvrs.consensus.control-dm.v1",
            priority=100,
            supersede_key=scope,
        )
        storage.delivery_outbox_claim(
            worker_id="old-worker",
            limit=1,
            lease_seconds=30,
            now=self.started_at.isoformat(),
        )
        second = self.enqueue(
            "control-newer",
            topic="tvrs.consensus.control-dm.v1",
            priority=100,
            supersede_key=scope,
        )

        self.assertFalse(
            storage.delivery_outbox_is_current_supersession(
                int(first["id"]),
                topic="tvrs.consensus.control-dm.v1",
                supersede_key=scope,
            )
        )
        self.assertTrue(
            storage.delivery_outbox_is_current_supersession(
                int(second["id"]),
                topic="tvrs.consensus.control-dm.v1",
                supersede_key=scope,
            )
        )

    def test_cleanup_migration_cancels_legacy_control_backlog(self) -> None:
        migration_key = "migration:delivery-control-cleanup:2026-07-20-v2"
        with storage._db_lock, storage.connect() as connection:
            connection.execute("DELETE FROM meta WHERE key = ?", (migration_key,))
            connection.commit()
        legacy = [
            self.enqueue(
                f"legacy-control-{index}",
                topic="tvrs.consensus.control-dm.v1",
                priority=100,
            )
            for index in range(250)
        ]
        background = self.enqueue("unrelated-background")
        with storage._db_lock, storage.connect() as connection:
            connection.execute(
                "UPDATE delivery_outbox SET status = 'retry' WHERE id = ?",
                (int(legacy[0]["id"]),),
            )
            connection.execute(
                "UPDATE delivery_outbox SET status = 'processing', lease_owner = 'old', "
                "lease_token = 'old-token', lease_until = ? WHERE id = ?",
                (
                    (self.started_at + timedelta(minutes=5)).isoformat(),
                    int(legacy[1]["id"]),
                ),
            )
            connection.execute(
                "UPDATE delivery_outbox SET status = 'dead' WHERE id = ?",
                (int(legacy[2]["id"]),),
            )
            connection.commit()

        storage.init_db()

        for row in legacy:
            cancelled = storage.delivery_outbox_get(int(row["id"]))
            self.assertEqual(cancelled["status"], "cancelled")  # type: ignore[index]
            self.assertEqual(cancelled["last_error"], "superseded_by_control_delivery_v2")  # type: ignore[index]
        claimed = storage.delivery_outbox_claim(
            worker_id="legacy-worker",
            limit=10,
            lease_seconds=30,
            now=self.started_at.isoformat(),
        )
        self.assertEqual([int(item["id"]) for item in claimed], [int(background["id"])])
        self.assertEqual(storage.delivery_outbox_counts(), {"cancelled": 250, "processing": 1})

    def test_expired_lease_is_reclaimed_and_stale_token_is_fenced(self) -> None:
        item_id = int(self.enqueue("leased", max_attempts=3)["id"])
        first = storage.delivery_outbox_claim(
            worker_id="worker-a",
            limit=1,
            lease_seconds=5,
            now=self.started_at.isoformat(),
        )[0]

        before_expiry = storage.delivery_outbox_claim(
            worker_id="worker-b",
            limit=1,
            lease_seconds=5,
            now=(self.started_at + timedelta(seconds=4)).isoformat(),
        )
        self.assertEqual(before_expiry, [])

        reclaimed = storage.delivery_outbox_claim(
            worker_id="worker-b",
            limit=1,
            lease_seconds=5,
            now=(self.started_at + timedelta(seconds=6)).isoformat(),
        )[0]
        self.assertEqual(int(reclaimed["id"]), item_id)
        self.assertEqual(int(reclaimed["attempts"]), 2)
        self.assertNotEqual(first["lease_token"], reclaimed["lease_token"])

        self.assertFalse(
            storage.delivery_outbox_mark_delivered(
                item_id,
                lease_token=str(first["lease_token"]),
                message_id=100,
                now=(self.started_at + timedelta(seconds=7)).isoformat(),
            )
        )
        self.assertFalse(
            storage.delivery_outbox_mark_failed(
                item_id,
                lease_token=str(first["lease_token"]),
                error="stale worker",
                retry_at=(self.started_at + timedelta(seconds=8)).isoformat(),
                now=(self.started_at + timedelta(seconds=7)).isoformat(),
            )
        )
        self.assertTrue(
            storage.delivery_outbox_mark_delivered(
                item_id,
                lease_token=str(reclaimed["lease_token"]),
                message_id=200,
                now=(self.started_at + timedelta(seconds=7)).isoformat(),
            )
        )
        persisted = storage.delivery_outbox_get(item_id)
        self.assertEqual(persisted["status"], "delivered")  # type: ignore[index]
        self.assertEqual(persisted["message_id"], 200)  # type: ignore[index]

    def test_heartbeat_renews_live_lease_and_rejects_stale_token(self) -> None:
        item_id = int(self.enqueue("heartbeat", max_attempts=3)["id"])
        claimed = storage.delivery_outbox_claim(
            worker_id="worker-a",
            limit=1,
            lease_seconds=5,
            now=self.started_at.isoformat(),
        )[0]
        self.assertTrue(
            storage.delivery_outbox_renew_lease(
                item_id,
                lease_token=str(claimed["lease_token"]),
                lease_seconds=5,
                now=(self.started_at + timedelta(seconds=4)).isoformat(),
            )
        )
        self.assertEqual(
            storage.delivery_outbox_claim(
                worker_id="worker-b",
                limit=1,
                lease_seconds=5,
                now=(self.started_at + timedelta(seconds=6)).isoformat(),
            ),
            [],
        )
        self.assertFalse(
            storage.delivery_outbox_renew_lease(
                item_id,
                lease_token="stale-token",
                lease_seconds=5,
                now=(self.started_at + timedelta(seconds=7)).isoformat(),
            )
        )
        reclaimed = storage.delivery_outbox_claim(
            worker_id="worker-b",
            limit=1,
            lease_seconds=5,
            now=(self.started_at + timedelta(seconds=10)).isoformat(),
        )
        self.assertEqual([int(row["id"]) for row in reclaimed], [item_id])

    def test_dead_delivery_can_only_be_requeued_by_its_guild(self) -> None:
        row = self.enqueue("guild-owned", payload={"guild_id": 202}, max_attempts=1)
        claimed = storage.delivery_outbox_claim(
            worker_id="owner-test",
            limit=1,
            lease_seconds=30,
            now=self.started_at.isoformat(),
        )[0]
        storage.delivery_outbox_mark_failed(
            int(row["id"]),
            lease_token=str(claimed["lease_token"]),
            error="terminal",
            retry_at=(self.started_at + timedelta(seconds=1)).isoformat(),
            permanent=True,
            now=self.started_at.isoformat(),
        )

        self.assertFalse(
            storage.delivery_outbox_requeue_dead(
                int(row["id"]),
                guild_id=101,
                now=self.started_at.isoformat(),
            )
        )
        self.assertEqual(storage.delivery_outbox_get(int(row["id"]))["status"], "dead")  # type: ignore[index]
        self.assertTrue(
            storage.delivery_outbox_requeue_dead(
                int(row["id"]),
                guild_id=202,
                now=self.started_at.isoformat(),
            )
        )


class DeliveryOutboxDispatcherTests(TemporaryOutboxDatabase, unittest.IsolatedAsyncioTestCase):
    async def test_interactive_retry_never_backs_off_for_minutes(self) -> None:
        dispatcher = OutboxDispatcher(StorageOutboxRepository())

        self.assertEqual(dispatcher.retry_delay(1, priority=100), 2)
        self.assertEqual(dispatcher.retry_delay(4, priority=100), 15)
        self.assertEqual(dispatcher.retry_delay(10, priority=100), 15)
        self.assertGreater(dispatcher.retry_delay(10), 15)

    async def test_policy_deferral_preserves_attempt_and_delivers_when_ready(self) -> None:
        item_id = int(self.enqueue("quiet-hours", max_attempts=2)["id"])
        clock = FakeClock(self.started_at)
        dispatcher = OutboxDispatcher(
            StorageOutboxRepository(),
            worker_id="quiet-hours-worker",
            clock=clock,
        )
        calls = 0

        async def handler(_message) -> DeliveryReceipt:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DeliveryDeferred(self.started_at + timedelta(hours=2), "quiet_hours")
            return DeliveryReceipt(message_id=9001)

        dispatcher.register("test.delivery", handler)
        self.assertEqual(await dispatcher.run_once(), 1)
        deferred = storage.delivery_outbox_get(item_id)
        self.assertEqual(deferred["status"], "retry")  # type: ignore[index]
        self.assertEqual(deferred["attempts"], 0)  # type: ignore[index]
        self.assertEqual(await dispatcher.run_once(), 0)

        clock.advance(seconds=2 * 60 * 60)
        self.assertEqual(await dispatcher.run_once(), 1)
        delivered = storage.delivery_outbox_get(item_id)
        self.assertEqual(delivered["status"], "delivered")  # type: ignore[index]
        self.assertEqual(delivered["attempts"], 1)  # type: ignore[index]
        self.assertEqual(delivered["message_id"], 9001)  # type: ignore[index]

    async def test_slow_handler_heartbeat_prevents_parallel_external_effect(self) -> None:
        item_id = int(self.enqueue("slow-handler", max_attempts=3)["id"])
        first_clock = FakeClock(self.started_at)
        second_clock = FakeClock(self.started_at + timedelta(seconds=6))
        first = OutboxDispatcher(
            StorageOutboxRepository(),
            worker_id="slow-a",
            clock=first_clock,
            lease_seconds=5,
            heartbeat_seconds=0.01,
        )
        second = OutboxDispatcher(
            StorageOutboxRepository(),
            worker_id="slow-b",
            clock=second_clock,
            lease_seconds=5,
            heartbeat_seconds=0.01,
        )
        started = asyncio.Event()
        release = asyncio.Event()
        effects: list[str] = []

        async def slow_handler(message) -> DeliveryReceipt:
            effects.append(f"send:{message.id}")
            started.set()
            await release.wait()
            return DeliveryReceipt(message_id=7001)

        async def duplicate_handler(message) -> DeliveryReceipt:
            effects.append(f"duplicate:{message.id}")
            return DeliveryReceipt(message_id=7002)

        first.register("test.delivery", slow_handler)
        second.register("test.delivery", duplicate_handler)
        first_task = asyncio.create_task(first.run_once())
        await started.wait()
        first_clock.advance(seconds=4)
        await asyncio.sleep(0.03)
        self.assertEqual(await second.run_once(), 0)
        release.set()
        self.assertEqual(await first_task, 1)
        self.assertEqual(effects, [f"send:{item_id}"])
        self.assertEqual(storage.delivery_outbox_get(item_id)["status"], "delivered")  # type: ignore[index]

    async def test_failed_delivery_retries_on_fake_clock_then_dead_letters(self) -> None:
        item_id = int(self.enqueue("retry", max_attempts=2)["id"])
        clock = FakeClock(self.started_at)
        dispatcher = OutboxDispatcher(
            StorageOutboxRepository(),
            worker_id="retry-worker",
            clock=clock,
            lease_seconds=10,
            base_retry_seconds=2,
            max_retry_seconds=20,
        )
        calls: list[int] = []

        async def always_fails(message) -> None:
            calls.append(message.attempts)
            raise RuntimeError("destination unavailable")

        dispatcher.register("test.delivery", always_fails)

        self.assertEqual(await dispatcher.run_once(), 1)
        first_failure = storage.delivery_outbox_get(item_id)
        self.assertEqual(first_failure["status"], "retry")  # type: ignore[index]
        self.assertEqual(first_failure["attempts"], 1)  # type: ignore[index]
        self.assertIn("RuntimeError: destination unavailable", first_failure["last_error"])  # type: ignore[index]

        self.assertEqual(await dispatcher.run_once(), 0)
        clock.advance(seconds=2)
        self.assertEqual(await dispatcher.run_once(), 1)

        terminal = storage.delivery_outbox_get(item_id)
        self.assertEqual(terminal["status"], "dead")  # type: ignore[index]
        self.assertEqual(terminal["attempts"], 2)  # type: ignore[index]
        self.assertEqual(calls, [1, 2])
        clock.advance(seconds=60)
        self.assertEqual(await dispatcher.run_once(), 0)

        self.assertTrue(
            storage.delivery_outbox_requeue_dead(
                item_id,
                now=clock().isoformat(),
            )
        )
        requeued = storage.delivery_outbox_get(item_id)
        self.assertEqual(requeued["status"], "retry")  # type: ignore[index]
        self.assertEqual(requeued["attempts"], 0)  # type: ignore[index]

    async def test_handler_failure_and_missing_handler_do_not_block_other_topics(self) -> None:
        failed_id = int(self.enqueue("fails", topic="test.fails", max_attempts=3)["id"])
        missing_id = int(self.enqueue("missing", topic="test.missing", max_attempts=3)["id"])
        delivered_id = int(self.enqueue("works", topic="test.works", max_attempts=3)["id"])
        clock = FakeClock(self.started_at)
        dispatcher = OutboxDispatcher(
            StorageOutboxRepository(),
            worker_id="isolation-worker",
            clock=clock,
            lease_seconds=10,
            base_retry_seconds=3,
        )
        calls: list[str] = []

        async def fails(message) -> None:
            calls.append(message.topic)
            raise ValueError("bad destination")

        async def works(message) -> DeliveryReceipt:
            calls.append(message.topic)
            return DeliveryReceipt(message_id=4242)

        dispatcher.register("test.fails", fails)
        dispatcher.register("test.works", works)

        self.assertEqual(await dispatcher.run_once(limit=10), 3)
        self.assertEqual(calls, ["test.fails", "test.works"])

        failed = storage.delivery_outbox_get(failed_id)
        missing = storage.delivery_outbox_get(missing_id)
        delivered = storage.delivery_outbox_get(delivered_id)
        self.assertEqual(failed["status"], "retry")  # type: ignore[index]
        self.assertIn("ValueError: bad destination", failed["last_error"])  # type: ignore[index]
        self.assertEqual(missing["status"], "dead")  # type: ignore[index]
        self.assertIn("outbox_handler_not_registered", missing["last_error"])  # type: ignore[index]
        self.assertEqual(delivered["status"], "delivered")  # type: ignore[index]
        self.assertEqual(delivered["message_id"], 4242)  # type: ignore[index]

    async def test_worker_does_not_claim_while_gateway_is_disconnected(self) -> None:
        ready_event = asyncio.Event()

        class FakeBot:
            closed = False
            ready = False

            def is_closed(self) -> bool:
                return self.closed

            def is_ready(self) -> bool:
                return self.ready

            async def wait_until_ready(self) -> None:
                await ready_event.wait()

            def get_guild(self, guild_id: int):
                return None

        bot = FakeBot()

        async def run_once(*, limit: int) -> int:
            self.assertTrue(bot.ready)
            bot.closed = True
            return 1

        with (
            patch("modules.delivery_runtime._dispatcher.run_once", new=AsyncMock(side_effect=run_once)) as claimed,
            patch("modules.delivery_runtime._report_dead_deliveries", new=AsyncMock()),
        ):
            task = asyncio.create_task(delivery_worker(bot, idle_seconds=1))
            await asyncio.sleep(0)
            self.assertEqual(claimed.await_count, 0)
            bot.ready = True
            ready_event.set()
            await task
            self.assertEqual(claimed.await_count, 1)

    async def test_dead_reporter_failure_does_not_stop_worker(self) -> None:
        class FakeBot:
            closed = False

            def is_closed(self) -> bool:
                return self.closed

            def is_ready(self) -> bool:
                return True

            def get_guild(self, guild_id: int):
                return None

        bot = FakeBot()
        runs = 0

        async def run_once(*, limit: int) -> int:
            nonlocal runs
            runs += 1
            if runs == 2:
                bot.closed = True
            return 1

        with (
            patch("modules.delivery_runtime._dispatcher.run_once", new=AsyncMock(side_effect=run_once)),
            patch(
                "modules.delivery_runtime._report_dead_deliveries",
                new=AsyncMock(side_effect=[RuntimeError("tech log unavailable"), None]),
            ) as reporter,
            patch("modules.delivery_runtime.traceback.print_exc"),
        ):
            await delivery_worker(bot, idle_seconds=1)

        self.assertEqual(runs, 2)
        self.assertEqual(reporter.await_count, 2)

    async def test_dead_reporter_isolates_poison_payload_and_does_not_starve_later_rows(self) -> None:
        def make_dead(key: str, payload: dict) -> int:
            row = self.enqueue(key, payload=payload, max_attempts=1)
            claimed = storage.delivery_outbox_claim(
                worker_id=f"dead-{key}",
                limit=1,
                lease_seconds=30,
                now=self.started_at.isoformat(),
            )[0]
            storage.delivery_outbox_mark_failed(
                int(row["id"]),
                lease_token=str(claimed["lease_token"]),
                error="terminal",
                retry_at=self.started_at.isoformat(),
                permanent=True,
                now=self.started_at.isoformat(),
            )
            return int(row["id"])

        poison_id = make_dead("poison", {"guild_id": "not-a-number"})
        missing_ids = [make_dead(f"missing-{index}", {"guild_id": 404}) for index in range(24)]
        active_id = make_dead("active", {"guild_id": 77})
        guild = SimpleNamespace(id=77)
        bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 77 else None)

        with (
            patch("modules.delivery_runtime.log_technical_event", new=AsyncMock(return_value=True)) as reporter,
            patch("builtins.print"),
        ):
            await _report_dead_deliveries(bot)
            await _report_dead_deliveries(bot)

        for item_id in [poison_id, *missing_ids, active_id]:
            self.assertIsNotNone(storage.delivery_outbox_get(item_id)["dead_notified_at"])  # type: ignore[index]
        reporter.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
