from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, AsyncIterator, TypeVar


T = TypeVar("T")


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


_projection_locks: dict[tuple[int, str, int], _LockEntry] = {}


@asynccontextmanager
async def consensus_projection_lock(
    session_key: str,
    user_id: int,
) -> AsyncIterator[None]:
    """Serialize every Discord write to one participant's consensus panel.

    Control-panel and result deliveries use different outbox topics and can be
    leased by different worker slots.  They nevertheless edit the same Discord
    message, so their ownership boundary must be shared.  The running-loop id
    keeps isolated test loops independent, while the small reference count
    removes entries after the last holder or waiter leaves.
    """

    loop_key = id(asyncio.get_running_loop())
    key = (loop_key, str(session_key), int(user_id))
    entry = _projection_locks.get(key)
    if entry is None:
        entry = _LockEntry(asyncio.Lock())
        _projection_locks[key] = entry
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0 and _projection_locks.get(key) is entry:
            _projection_locks.pop(key, None)


async def run_blocking_cancellation_safe(
    function: Callable[..., T],
    /,
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run a blocking state change without releasing its async lock early.

    ``asyncio.to_thread`` cannot stop the worker thread when its awaiting task is
    cancelled.  Awaiting it directly from a locked critical section can
    therefore release the lock while the database write is still mutating the
    live session.  This helper delays cancellation until the worker has either
    committed or failed, preserving the lock's serialization guarantee.
    """

    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True

    error = task.exception()
    if error is not None:
        raise error
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


__all__ = ["consensus_projection_lock", "run_blocking_cancellation_safe"]
