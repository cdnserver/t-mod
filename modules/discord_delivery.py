from __future__ import annotations

from datetime import datetime, timedelta, timezone

import discord

from modules.delivery_outbox import (
    DeliveryDeferred,
    DeliveryPermanentFailure,
)


class DeliveryDestinationUnavailable(DeliveryPermanentFailure):
    """A permanent Discord destination failure, distinct from a bad payload."""


def _discord_retry_after(exc: discord.HTTPException) -> float | None:
    if int(getattr(exc, "status", 0) or 0) != 429:
        return None
    raw = getattr(exc, "retry_after", None)
    if raw is None:
        headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
        raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    try:
        return max(1.0, float(raw or 5.0))
    except (TypeError, ValueError):
        return 5.0


def raise_classified_discord_error(
    exc: discord.DiscordException,
    *,
    missing_is_permanent: bool = False,
) -> None:
    """Map Discord failures according to the operation that produced them."""

    if isinstance(exc, discord.Forbidden):
        raise DeliveryDestinationUnavailable(str(exc)) from exc
    if isinstance(exc, discord.HTTPException):
        retry_after = _discord_retry_after(exc)
        if retry_after is not None:
            raise DeliveryDeferred(
                datetime.now(timezone.utc) + timedelta(seconds=retry_after),
                "discord_rate_limit",
            ) from exc
        status = int(getattr(exc, "status", 0) or 0)
        if isinstance(exc, discord.NotFound):
            if missing_is_permanent:
                raise DeliveryDestinationUnavailable(str(exc)) from exc
            raise exc
        if 400 <= status < 500:
            raise DeliveryPermanentFailure(str(exc)) from exc
    raise exc


async def resolve_delivery_member(guild: discord.Guild, user_id: int):
    member = guild.get_member(int(user_id))
    if member is not None:
        return member
    try:
        return await guild.fetch_member(int(user_id))
    except discord.DiscordException as exc:
        raise_classified_discord_error(exc, missing_is_permanent=True)


__all__ = [
    "DeliveryDestinationUnavailable",
    "raise_classified_discord_error",
    "resolve_delivery_member",
]
