"""Application ports exposed by Craft without importing its Discord module."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


PlanRefresher = Callable[[Any, int], Awaitable[None]]
WorkerWakeup = Callable[[], None]
StatsPresenter = Callable[[dict[str, Any]], Any]

_plan_refresher: PlanRefresher | None = None
_worker_wakeup: WorkerWakeup | None = None
_stats_presenter: StatsPresenter | None = None


def register_craft_runtime(
    *,
    plan_refresher: PlanRefresher,
    worker_wakeup: WorkerWakeup,
    stats_presenter: StatsPresenter,
) -> None:
    global _plan_refresher, _worker_wakeup, _stats_presenter
    _plan_refresher = plan_refresher
    _worker_wakeup = worker_wakeup
    _stats_presenter = stats_presenter


async def refresh_craft_plan(bot: Any, plan_id: int) -> None:
    if _plan_refresher is not None:
        await _plan_refresher(bot, int(plan_id))


def wake_craft_worker() -> None:
    if _worker_wakeup is not None:
        _worker_wakeup()


def build_craft_stats_embed(stats: dict[str, Any]) -> Any | None:
    return _stats_presenter(stats) if _stats_presenter is not None else None
