from __future__ import annotations

import asyncio
import threading
import unittest

from modules.async_safety import consensus_projection_lock, run_blocking_cancellation_safe


class CancellationSafeBlockingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_waits_until_worker_finishes(self) -> None:
        started = threading.Event()
        release = threading.Event()
        completed = threading.Event()

        def blocking_write() -> int:
            started.set()
            release.wait(timeout=2)
            completed.set()
            return 7

        task = asyncio.create_task(run_blocking_cancellation_safe(blocking_write))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertFalse(completed.is_set())

        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(completed.is_set())

    async def test_result_and_exception_are_preserved(self) -> None:
        self.assertEqual(await run_blocking_cancellation_safe(lambda: 11), 11)

        def fail() -> None:
            raise RuntimeError("write_failed")

        with self.assertRaisesRegex(RuntimeError, "write_failed"):
            await run_blocking_cancellation_safe(fail)

    async def test_consensus_projection_writers_are_serialized_per_participant(self) -> None:
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        order: list[str] = []

        async def first() -> None:
            async with consensus_projection_lock("77:session", 2):
                order.append("first-enter")
                first_entered.set()
                await release_first.wait()
                order.append("first-leave")

        async def second() -> None:
            await first_entered.wait()
            async with consensus_projection_lock("77:session", 2):
                order.append("second-enter")

        first_task = asyncio.create_task(first())
        second_task = asyncio.create_task(second())
        await first_entered.wait()
        await asyncio.sleep(0)
        self.assertEqual(order, ["first-enter"])
        release_first.set()
        await asyncio.gather(first_task, second_task)

        self.assertEqual(order, ["first-enter", "first-leave", "second-enter"])


if __name__ == "__main__":
    unittest.main()
