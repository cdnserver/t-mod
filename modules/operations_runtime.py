"""Small runtime ports shared with the operations dashboard.

Feature modules may request a refresh without importing the dashboard's Discord
implementation. The operations adapter registers the concrete refresher during
application setup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


DashboardRefresher = Callable[[Any, Any], Awaitable[Any]]

_worker_wakeup: asyncio.Event | None = None
_dashboard_refresher: DashboardRefresher | None = None


def bind_worker_wakeup(event: asyncio.Event) -> asyncio.Event:
    global _worker_wakeup
    _worker_wakeup = event
    return event


def wake_operations_worker() -> None:
    if _worker_wakeup is not None:
        _worker_wakeup.set()


def register_dashboard_refresher(refresher: DashboardRefresher) -> None:
    global _dashboard_refresher
    _dashboard_refresher = refresher


async def refresh_operations_dashboard(bot: Any, guild: Any) -> Any:
    if _dashboard_refresher is None:
        return None
    return await _dashboard_refresher(bot, guild)
