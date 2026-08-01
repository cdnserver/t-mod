"""Small stale-while-revalidate cache for read-only web projections."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, Hashable, TypeVar


K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


@dataclass(slots=True)
class _CacheEntry(Generic[V]):
    value: V | None = None
    written_at: float = 0.0
    task: asyncio.Task[V] | None = None


class AsyncSnapshotCache(Generic[K, V]):
    """Return fresh data, or stale data immediately while refreshing it."""

    def __init__(self, *, ttl_seconds: float, max_stale_seconds: float = 120.0):
        self.ttl_seconds = max(0.1, float(ttl_seconds))
        self.max_stale_seconds = max(self.ttl_seconds, float(max_stale_seconds))
        self._entries: dict[K, _CacheEntry[V]] = {}

    def _start(self, key: K, loader: Callable[[], Awaitable[V]]) -> asyncio.Task[V]:
        entry = self._entries.setdefault(key, _CacheEntry())
        if entry.task is not None and not entry.task.done():
            return entry.task

        async def run() -> V:
            try:
                value = await loader()
                entry.value = value
                entry.written_at = time.monotonic()
                return value
            except Exception:
                if entry.value is not None:
                    return entry.value
                raise
            finally:
                entry.task = None

        entry.task = asyncio.create_task(run())
        return entry.task

    async def get(
        self,
        key: K,
        loader: Callable[[], Awaitable[V]],
        *,
        force: bool = False,
    ) -> tuple[V, str]:
        entry = self._entries.setdefault(key, _CacheEntry())
        age = (
            time.monotonic() - entry.written_at
            if entry.value is not None
            else float("inf")
        )
        if entry.value is not None and not force and age <= self.ttl_seconds:
            return entry.value, "fresh"

        task = self._start(key, loader)
        if entry.value is not None and not force and age <= self.max_stale_seconds:
            return entry.value, "stale"
        return await task, "refreshed"

    def invalidate(self, key: K | None = None) -> None:
        if key is None:
            for entry in self._entries.values():
                entry.written_at = 0.0
            return
        entry = self._entries.get(key)
        if entry is not None:
            entry.written_at = 0.0


__all__ = ["AsyncSnapshotCache"]
