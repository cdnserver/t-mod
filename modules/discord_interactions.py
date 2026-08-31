"""Small safeguards for Discord interactions that can expire outside T-Mod."""

from __future__ import annotations

from typing import Any

import discord


def interaction_error_cause(error: BaseException) -> BaseException:
    current = error
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        original = getattr(current, "original", None)
        next_error = original if isinstance(original, BaseException) else current.__cause__
        if not isinstance(next_error, BaseException):
            break
        current = next_error
    return current


def is_expired_interaction_error(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, discord.NotFound) and int(getattr(current, "code", 0) or 0) == 10062:
            return True
        if "unknown interaction" in str(current).lower():
            return True
        original = getattr(current, "original", None)
        current = (
            original
            if isinstance(original, BaseException)
            else current.__cause__ or current.__context__
        )
    return False


async def safe_interaction_error_message(
    interaction: discord.Interaction,
    content: str,
    **kwargs: Any,
) -> bool:
    """Best-effort error response that never creates a second 10062 failure."""

    kwargs.setdefault("ephemeral", True)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, **kwargs)
        else:
            await interaction.response.send_message(content, **kwargs)
        return True
    except discord.DiscordException:
        return False


__all__ = [
    "interaction_error_cause",
    "is_expired_interaction_error",
    "safe_interaction_error_message",
]
