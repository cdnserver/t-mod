"""Persistent canonical Discord status card for T-Mod Music."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import discord

from persistence.activity_repository import get_meta, set_meta_value


def _status_meta_keys(guild_id: int) -> tuple[str, str]:
    prefix = f"music_status:{int(guild_id)}"
    return f"{prefix}:channel_id", f"{prefix}:message_id"


async def publish_music_status(
    manager: Any,
    session: Any,
    *,
    disconnected: bool = False,
) -> None:
    """Edit one durable card and avoid duplicates during transient API errors."""

    from modules.music_views import build_music_embed

    async with session.status_lock:
        channel = manager.bot.get_channel(session.text_channel_id)
        if channel is None or not hasattr(channel, "send"):
            return

        if session.status_message_obj is None and session.status_message_id is None:
            channel_key, message_key = _status_meta_keys(session.guild_id)
            try:
                stored_channel_raw, stored_message_raw = await asyncio.gather(
                    asyncio.to_thread(get_meta, channel_key),
                    asyncio.to_thread(get_meta, message_key),
                )
                stored_channel_id = int(stored_channel_raw or 0)
                stored_message_id = int(stored_message_raw or 0)
            except (OSError, sqlite3.Error, TypeError, ValueError):
                stored_channel_id = 0
                stored_message_id = 0
            if stored_channel_id > 0 and stored_message_id > 0:
                stored_channel = manager.bot.get_channel(stored_channel_id)
                if stored_channel is not None and hasattr(stored_channel, "send"):
                    channel = stored_channel
                    session.text_channel_id = stored_channel_id
                    session.status_message_id = stored_message_id

        embed = build_music_embed(manager, session, disconnected=disconnected)
        message = session.status_message_obj
        if (
            message is None
            and session.status_message_id
            and hasattr(channel, "fetch_message")
        ):
            try:
                message = await channel.fetch_message(session.status_message_id)
            except discord.NotFound:
                message = None
                session.status_message_id = None
            except discord.DiscordException:
                # A transient Discord/API failure must not create a duplicate card.
                return
        try:
            if message is not None:
                await message.edit(
                    content=None,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                session.status_message_obj = message
                return
        except discord.NotFound:
            message = None
        except discord.DiscordException:
            return

        try:
            message = await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException:
            return
        session.status_message_id = int(message.id)
        session.status_message_obj = message
        channel_key, message_key = _status_meta_keys(session.guild_id)
        try:
            await asyncio.gather(
                asyncio.to_thread(
                    set_meta_value,
                    channel_key,
                    str(session.text_channel_id),
                ),
                asyncio.to_thread(
                    set_meta_value,
                    message_key,
                    str(session.status_message_id),
                ),
            )
        except (OSError, sqlite3.Error):
            # The card remains useful even when the optional receipt cannot be saved.
            return


__all__ = ["publish_music_status"]
