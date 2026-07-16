"""Navigation port back to the TVRS hub without importing its Discord views."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


HubHandler = Callable[[Any], Awaitable[None]]

_hub_handler: HubHandler | None = None


def register_tvrs_hub_handler(handler: HubHandler) -> None:
    global _hub_handler
    _hub_handler = handler


async def open_tvrs_hub(interaction: Any) -> None:
    if _hub_handler is None:
        response = getattr(interaction, "response", None)
        if response is not None and not response.is_done():
            await response.send_message(
                "Панель TVRS ещё запускается. Повторите действие через несколько секунд.",
                ephemeral=True,
            )
        return
    await _hub_handler(interaction)
