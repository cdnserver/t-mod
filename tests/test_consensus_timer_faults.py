from __future__ import annotations

import asyncio
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules.consensus_core import LiveConsensusSession, LiveParticipant
from modules.consensus_runtime import session_lock
from modules.consensus_service import ConsensusActor, ConsensusCoordinator
from modules.tvrs_discussion import pause_session, request_discussion, set_vote_timer


class _FailingRepository:
    def save(self, *args, **kwargs):
        raise RuntimeError("database unavailable")


def _session() -> LiveConsensusSession:
    participants = {
        1: LiveParticipant(
            user_id=1,
            display_name="Ведущий",
            mention="<@1>",
            kind="chair",
            confirmed=True,
        ),
        2: LiveParticipant(
            user_id=2,
            display_name="Сенатор",
            mention="<@2>",
            kind="senator",
            confirmed=True,
        ),
    }
    session = LiveConsensusSession(
        session_key="77:timer-faults",
        guild_id=77,
        channel_id=100,
        leader_id=1,
        leader_display="Ведущий",
        plenary_number=4,
        participants=participants,
    )
    session.stage = "voting"
    session.current_bill = {"id": 10, "bill_number": 9, "title": "Таймер"}
    session.timer_deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    session.timer_seconds = 300
    return session


class ConsensusTimerPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = ConsensusCoordinator(_FailingRepository())  # type: ignore[arg-type]

    def assert_timer_and_vote_were_restored(
        self,
        session: LiveConsensusSession,
        deadline: datetime,
        runtime_timer: object,
    ) -> None:
        self.assertEqual(session.stage, "voting")
        self.assertEqual(session.timer_deadline, deadline)
        self.assertEqual(session.timer_seconds, 300)
        self.assertIs(session.timer_task, runtime_timer)

    def test_failed_pause_restores_stage_deadline_and_runtime_task(self) -> None:
        session = _session()
        runtime_timer = object()
        session.timer_task = runtime_timer  # type: ignore[assignment]
        deadline = session.timer_deadline
        assert deadline is not None

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            self.coordinator.pause(
                session,
                "Кворум утрачен",
                automatic=True,
                actor=None,
            )

        self.assert_timer_and_vote_were_restored(session, deadline, runtime_timer)
        self.assertIsNone(session.previous_stage)
        self.assertIsNone(session.paused_reason)

    def test_failed_discussion_request_restores_timer_and_initiator(self) -> None:
        session = _session()
        runtime_timer = object()
        session.timer_task = runtime_timer  # type: ignore[assignment]
        deadline = session.timer_deadline
        assert deadline is not None

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            self.coordinator.request_discussion(session, session.participants[2])

        self.assert_timer_and_vote_were_restored(session, deadline, runtime_timer)
        self.assertIsNone(session.previous_stage)
        self.assertIsNone(session.discussion_initiator_id)

    def test_failed_finalization_claim_restores_active_timer(self) -> None:
        session = _session()
        runtime_timer = object()
        session.timer_task = runtime_timer  # type: ignore[assignment]
        deadline = session.timer_deadline
        assert deadline is not None

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            self.coordinator.claim_finalization(
                session,
                kind="vote",
                actor=ConsensusActor(1, "Ведущий"),
            )

        self.assert_timer_and_vote_were_restored(session, deadline, runtime_timer)
        self.assertIsNone(session.pending_action)

    def test_failed_timer_change_restores_previous_deadline(self) -> None:
        session = _session()
        runtime_timer = object()
        session.timer_task = runtime_timer  # type: ignore[assignment]
        deadline = session.timer_deadline
        assert deadline is not None

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            self.coordinator.set_timer(
                session,
                seconds=30,
                deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
                actor=ConsensusActor(1, "Ведущий"),
            )

        self.assert_timer_and_vote_were_restored(session, deadline, runtime_timer)


class ConsensusTimerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session = _session()
        self.timer_gate = asyncio.Event()
        self.timer_task = asyncio.create_task(self.timer_gate.wait())
        self.session.timer_task = self.timer_task
        self.guild = SimpleNamespace(id=77)
        self.bot = SimpleNamespace()

    async def asyncTearDown(self) -> None:
        tasks = {self.timer_task}
        if self.session.timer_task is not None:
            tasks.add(self.session.timer_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_pause_database_failure_keeps_runtime_timer_alive(self) -> None:
        deadline = self.session.timer_deadline
        with (
            patch(
                "modules.tvrs_discussion._consensus.pause",
                side_effect=RuntimeError("database unavailable"),
            ),
            patch("modules.tvrs_discussion.update_all_vote_dms", new=AsyncMock()) as project,
            patch("modules.tvrs_discussion.update_host_vote_message", new=AsyncMock()) as host,
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                await pause_session(
                    self.bot,  # type: ignore[arg-type]
                    self.guild,  # type: ignore[arg-type]
                    self.session,
                    "Ручная пауза",
                )

        self.assertIs(self.session.timer_task, self.timer_task)
        self.assertFalse(self.timer_task.done())
        self.assertEqual(self.session.timer_deadline, deadline)
        self.assertEqual(self.session.timer_seconds, 300)
        project.assert_not_awaited()
        host.assert_not_awaited()

    async def test_discussion_database_failure_keeps_runtime_timer_alive(self) -> None:
        deadline = self.session.timer_deadline
        with (
            patch(
                "modules.tvrs_discussion._consensus.request_discussion",
                side_effect=RuntimeError("database unavailable"),
            ),
            patch("modules.tvrs_discussion.update_all_vote_dms", new=AsyncMock()) as project,
            patch("modules.tvrs_discussion.update_host_vote_message", new=AsyncMock()) as host,
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                await request_discussion(
                    self.bot,  # type: ignore[arg-type]
                    self.guild,  # type: ignore[arg-type]
                    self.session,
                    self.session.participants[2],
                    expected_bill_id=10,
                )

        self.assertIs(self.session.timer_task, self.timer_task)
        self.assertFalse(self.timer_task.done())
        self.assertEqual(self.session.timer_deadline, deadline)
        self.assertEqual(self.session.timer_seconds, 300)
        project.assert_not_awaited()
        host.assert_not_awaited()

    async def test_successful_pause_persists_before_runtime_timer_is_cancelled(self) -> None:
        def persist_pause(current, *args, **kwargs) -> None:
            self.assertFalse(self.timer_task.done())
            current.stage = "paused"
            current.previous_stage = "voting"
            current.timer_deadline = None
            current.timer_seconds = None

        with (
            patch("modules.tvrs_discussion._consensus.pause", side_effect=persist_pause),
            patch("modules.tvrs_discussion.update_all_vote_dms", new=AsyncMock()),
            patch("modules.tvrs_discussion.update_host_vote_message", new=AsyncMock()),
            patch("modules.tvrs_discussion.wake_operations_worker"),
        ):
            await pause_session(
                self.bot,  # type: ignore[arg-type]
                self.guild,  # type: ignore[arg-type]
                self.session,
                "Ручная пауза",
            )

        await asyncio.sleep(0)
        self.assertEqual(self.session.stage, "paused")
        self.assertIsNone(self.session.timer_task)
        self.assertTrue(self.timer_task.cancelled())
        self.assertIsNone(self.session.timer_deadline)
        self.assertIsNone(self.session.timer_seconds)

    async def test_timer_change_failure_keeps_previous_runtime_timer(self) -> None:
        deadline = self.session.timer_deadline
        with (
            patch(
                "modules.tvrs_discussion._consensus.set_timer",
                side_effect=RuntimeError("database unavailable"),
            ),
            patch("modules.tvrs_discussion.update_all_vote_dms", new=AsyncMock()) as project,
            patch("modules.tvrs_discussion.update_host_vote_message", new=AsyncMock()) as host,
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                await set_vote_timer(
                    self.bot,  # type: ignore[arg-type]
                    self.guild,  # type: ignore[arg-type]
                    self.session,
                    30,
                    expected_bill_id=10,
                )

        self.assertIs(self.session.timer_task, self.timer_task)
        self.assertFalse(self.timer_task.done())
        self.assertEqual(self.session.timer_deadline, deadline)
        self.assertEqual(self.session.timer_seconds, 300)
        project.assert_not_awaited()
        host.assert_not_awaited()

    async def test_replace_timer_sets_new_deadline_instead_of_extending_old_one(self) -> None:
        captured: dict[str, object] = {}

        def persist_timer(current, *, seconds, deadline, actor, added_seconds) -> None:
            captured.update(
                seconds=seconds,
                deadline=deadline,
                actor=actor,
                added_seconds=added_seconds,
            )
            current.timer_seconds = seconds
            current.timer_deadline = deadline
            current.timer_added_seconds = added_seconds

        with (
            patch(
                "modules.tvrs_discussion._consensus.set_timer",
                side_effect=persist_timer,
            ),
            patch("modules.tvrs_discussion.update_all_vote_dms", new=AsyncMock()),
            patch("modules.tvrs_discussion.update_host_vote_message", new=AsyncMock()),
        ):
            await set_vote_timer(
                self.bot,  # type: ignore[arg-type]
                self.guild,  # type: ignore[arg-type]
                self.session,
                45,
                expected_bill_id=10,
                replace=True,
            )

        self.assertEqual(captured["seconds"], 45)
        self.assertEqual(captured["added_seconds"], 0)
        self.assertEqual(self.session.timer_seconds, 45)
        self.assertLess(
            (self.session.timer_deadline - datetime.now(timezone.utc)).total_seconds(),
            46,
        )
        await asyncio.sleep(0)
        self.assertTrue(self.timer_task.cancelled())

    async def test_cancelled_timer_change_still_installs_committed_runtime_task(self) -> None:
        write_started = threading.Event()
        allow_write = threading.Event()

        def slow_persist_timer(current, *, seconds, deadline, actor) -> None:
            write_started.set()
            if not allow_write.wait(timeout=2):
                raise RuntimeError("test write gate timed out")
            current.timer_seconds = seconds
            current.timer_deadline = deadline

        with (
            patch(
                "modules.tvrs_discussion._consensus.set_timer",
                side_effect=slow_persist_timer,
            ),
            patch("modules.tvrs_discussion.update_all_vote_dms", new=AsyncMock()),
            patch("modules.tvrs_discussion.update_host_vote_message", new=AsyncMock()),
        ):
            transition = asyncio.create_task(
                set_vote_timer(
                    self.bot,  # type: ignore[arg-type]
                    self.guild,  # type: ignore[arg-type]
                    self.session,
                    300,
                    expected_bill_id=10,
                )
            )
            while not write_started.is_set():
                await asyncio.sleep(0)
            transition.cancel()
            allow_write.set()
            with self.assertRaises(asyncio.CancelledError):
                await transition

        await asyncio.sleep(0)
        self.assertTrue(self.timer_task.cancelled())
        self.assertIsNotNone(self.session.timer_task)
        self.assertIsNot(self.session.timer_task, self.timer_task)
        self.assertFalse(self.session.timer_task.done())  # type: ignore[union-attr]
        self.assertEqual(self.session.timer_seconds, 600)
        self.assertIsNotNone(self.session.timer_deadline)

    async def test_cancellation_waits_for_pause_write_and_keeps_lock_serialized(self) -> None:
        self.session.guild_id = 9077
        self.session.session_key = "9077:timer-cancel"
        self.guild.id = 9077
        write_started = threading.Event()
        allow_write = threading.Event()

        def slow_persist_pause(current, *args, **kwargs) -> None:
            write_started.set()
            if not allow_write.wait(timeout=2):
                raise RuntimeError("test write gate timed out")
            current.stage = "paused"
            current.previous_stage = "voting"
            current.timer_deadline = None
            current.timer_seconds = None

        with (
            patch("modules.tvrs_discussion._consensus.pause", side_effect=slow_persist_pause),
            patch("modules.tvrs_discussion.update_all_vote_dms", new=AsyncMock()),
            patch("modules.tvrs_discussion.update_host_vote_message", new=AsyncMock()),
        ):
            transition = asyncio.create_task(
                pause_session(
                    self.bot,  # type: ignore[arg-type]
                    self.guild,  # type: ignore[arg-type]
                    self.session,
                    "Ручная пауза",
                )
            )
            while not write_started.is_set():
                await asyncio.sleep(0)

            transition.cancel()
            lock = session_lock(self.session.guild_id)
            competing_writer = asyncio.create_task(lock.acquire())
            await asyncio.sleep(0.01)
            self.assertFalse(competing_writer.done())

            allow_write.set()
            with self.assertRaises(asyncio.CancelledError):
                await transition
            await asyncio.wait_for(competing_writer, timeout=1)
            lock.release()

        await asyncio.sleep(0)
        self.assertEqual(self.session.stage, "paused")
        self.assertIsNone(self.session.timer_task)
        self.assertTrue(self.timer_task.cancelled())


if __name__ == "__main__":
    unittest.main()
