import asyncio
import unittest

from modules.web_snapshot_cache import AsyncSnapshotCache


class AsyncSnapshotCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_fresh_value(self) -> None:
        cache = AsyncSnapshotCache[str, int](ttl_seconds=30)
        calls = 0

        async def load() -> int:
            nonlocal calls
            calls += 1
            return calls

        first, first_state = await cache.get("overview", load)
        second, second_state = await cache.get("overview", load)
        self.assertEqual((first, first_state), (1, "refreshed"))
        self.assertEqual((second, second_state), (1, "fresh"))
        self.assertEqual(calls, 1)

    async def test_stale_value_returns_without_waiting_for_refresh(self) -> None:
        cache = AsyncSnapshotCache[str, int](ttl_seconds=0.1)
        release = asyncio.Event()
        calls = 0

        async def load() -> int:
            nonlocal calls
            calls += 1
            if calls > 1:
                await release.wait()
            return calls

        await cache.get("overview", load)
        await asyncio.sleep(0.11)
        stale, state = await cache.get("overview", load)
        self.assertEqual((stale, state), (1, "stale"))
        release.set()
        await asyncio.sleep(0)
        refreshed, refreshed_state = await cache.get("overview", load)
        self.assertEqual((refreshed, refreshed_state), (2, "fresh"))

    async def test_concurrent_cold_requests_share_loader(self) -> None:
        cache = AsyncSnapshotCache[str, int](ttl_seconds=30)
        calls = 0

        async def load() -> int:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return 7

        results = await asyncio.gather(
            cache.get("health", load),
            cache.get("health", load),
        )
        self.assertEqual([item[0] for item in results], [7, 7])
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
