"""Feature registration port for the TVRS hub.

The hub owns navigation only. Finance, Craft, Audit and Market own their screens
and register handlers during application setup.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal


HubSurface = Literal["replace", "ephemeral"]
SectionHandler = Callable[[Any, int, HubSurface], Awaitable[None]]

_handlers: dict[str, SectionHandler] = {}


def register_hub_section(name: str, handler: SectionHandler) -> None:
    clean_name = str(name).strip().lower()
    if not clean_name:
        raise ValueError("hub_section_name_required")
    _handlers[clean_name] = handler


async def open_hub_section(
    name: str,
    interaction: Any,
    requester_id: int,
    *,
    surface: HubSurface,
) -> None:
    handler = _handlers.get(str(name).strip().lower())
    if handler is None:
        response = getattr(interaction, "response", None)
        if response is not None and not response.is_done():
            await response.send_message(
                "Этот раздел ещё запускается. Повторите действие через несколько секунд.",
                ephemeral=True,
            )
        return
    await handler(interaction, int(requester_id), surface)
