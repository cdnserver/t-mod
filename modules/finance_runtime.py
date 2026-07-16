"""Runtime ports exposed by Finance to other features."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


PanelOpener = Callable[[Any], Awaitable[None]]
WorkerWakeup = Callable[[], None]

_panel_opener: PanelOpener | None = None
_worker_wakeup: WorkerWakeup | None = None


def register_finance_runtime(*, panel_opener: PanelOpener, worker_wakeup: WorkerWakeup) -> None:
    global _panel_opener, _worker_wakeup
    _panel_opener = panel_opener
    _worker_wakeup = worker_wakeup


async def open_finance_panel(interaction: Any) -> None:
    if _panel_opener is None:
        response = getattr(interaction, "response", None)
        if response is not None and not response.is_done():
            await response.send_message(
                "Казначейство ещё запускается. Повторите действие через несколько секунд.",
                ephemeral=True,
            )
        return
    await _panel_opener(interaction)


def wake_notification_worker() -> None:
    if _worker_wakeup is not None:
        _worker_wakeup()
