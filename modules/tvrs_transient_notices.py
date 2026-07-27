"""Durable cleanup for short-lived consensus DM notifications."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from persistence import outbox_repository as outbox_storage
from modules.delivery_outbox import (
    DeliveryDeferred,
    DeliveryPermanentFailure,
    DeliveryReceipt,
    OutboxMessage,
)
from modules.delivery_runtime import wake_delivery_worker
from modules.discord_delivery import resolve_delivery_member
from modules.tvrs_delivery import TVRS_NOTICE_DELETE_TOPIC


async def enqueue_notice_deletion(
    source: OutboxMessage,
    *,
    guild_id: int,
    user_id: int,
    channel_id: int,
    message_id: int,
    marker: str,
) -> None:
    """Persist deletion before acknowledging the transient notice delivery."""

    available_at = datetime.now(timezone.utc) + timedelta(seconds=3)
    await asyncio.to_thread(
        outbox_storage.delivery_outbox_enqueue,
        topic=TVRS_NOTICE_DELETE_TOPIC,
        dedupe_key=f"{source.dedupe_key}:delete",
        payload={
            "payload_version": 1,
            "guild_id": int(guild_id),
            "user_id": int(user_id),
            "channel_id": int(channel_id),
            "message_id": int(message_id),
            "marker": str(marker),
        },
        max_attempts=24,
        priority=140,
        available_at=available_at.isoformat(),
    )
    wake_delivery_worker()


async def deliver_consensus_notice_deletion(
    message: OutboxMessage,
    bot: commands.Bot | discord.Client,
) -> DeliveryReceipt:
    """Delete a transient notice; outbox retries survive restarts and outages."""

    payload = message.payload
    if int(payload.get("payload_version") or 0) != 1:
        raise DeliveryPermanentFailure(
            "tvrs_notice_delete_payload_version_unsupported"
        )
    guild_id = int(payload.get("guild_id") or 0)
    user_id = int(payload.get("user_id") or 0)
    channel_id = int(payload.get("channel_id") or 0)
    message_id = int(payload.get("message_id") or 0)
    marker = str(payload.get("marker") or "")
    if (
        guild_id <= 0
        or user_id <= 0
        or channel_id <= 0
        or message_id <= 0
        or not marker
    ):
        raise DeliveryPermanentFailure("tvrs_notice_delete_payload_invalid")
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise RuntimeError(f"tvrs_delivery_guild_unavailable:{guild_id}")
    member = await resolve_delivery_member(guild, user_id)
    dm_channel = member.dm_channel or await member.create_dm()
    if int(getattr(dm_channel, "id", 0) or 0) != channel_id:
        raise DeliveryDeferred(
            datetime.now(timezone.utc) + timedelta(seconds=5),
            "notice_dm_channel_pending",
        )
    try:
        transient = await dm_channel.fetch_message(message_id)
    except discord.NotFound:
        return DeliveryReceipt(message_id=message_id)
    footer_markers = {
        str(getattr(getattr(embed, "footer", None), "text", "") or "")
        for embed in getattr(transient, "embeds", ())
    }
    if marker not in footer_markers:
        # Never delete a message whose identity cannot be proven.
        return DeliveryReceipt(message_id=message_id)
    await transient.delete()
    return DeliveryReceipt(message_id=message_id)


__all__ = [
    "deliver_consensus_notice_deletion",
    "enqueue_notice_deletion",
]
