"""Runtime port for resolving operational channels without importing their UI."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


ChannelResolver = Callable[[Any, str], Awaitable[Any]]

_channel_resolver: ChannelResolver | None = None


def register_channel_resolver(resolver: ChannelResolver) -> None:
    global _channel_resolver
    _channel_resolver = resolver


async def resolve_registered_channel(guild: Any, key: str) -> Any | None:
    if _channel_resolver is None:
        return None
    return await _channel_resolver(guild, key)
