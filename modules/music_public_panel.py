"""One durable, shared and rate-limit-safe music panel per guild."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass

import discord
from discord.ext import commands

from modules.control_center import (
    edit_message_with_retry,
    ensure_control_center,
    ensure_panel_message,
    resolve_control_channel,
)
from modules.music_config import MUSIC_PANEL_REFRESH_SECONDS
from modules.music_public_views import (
    PublicMusicPanelView,
    build_public_music_embed,
)
from modules.music_runtime import MusicGuildSession, MusicManager
from persistence.activity_repository import get_meta, set_meta_value


logger = logging.getLogger(__name__)
_HEALTH_CHECK_SECONDS = 60.0


def _render_signature(embed: discord.Embed, view: discord.ui.View) -> str:
    payload = embed.to_dict()
    payload.pop("timestamp", None)
    return json.dumps(
        {"embed": payload, "components": view.to_components()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(slots=True)
class _PanelRecord:
    message: discord.Message
    signature: str
    checked_at: float


class MusicPublicPanelService:
    """Reconcile every second, but PATCH Discord only when visible state changes."""

    def __init__(self, bot: commands.Bot, manager: MusicManager) -> None:
        self.bot = bot
        self.manager = manager
        self._records: dict[int, _PanelRecord] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._initialized: set[int] = set()
        self._legacy_retired: set[int] = set()
        self._worker_task: asyncio.Task | None = None

    def view(self, guild_id: int) -> PublicMusicPanelView:
        return PublicMusicPanelView.for_guild(self.manager, guild_id)

    async def start(self) -> None:
        await self.reconcile_all()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._worker(), name="music-public-panel"
            )

    async def _worker(self) -> None:
        try:
            while not self.bot.is_closed():
                started = time.monotonic()
                await self.reconcile_all()
                delay = max(
                    0.05, MUSIC_PANEL_REFRESH_SECONDS - (time.monotonic() - started)
                )
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

    async def reconcile_all(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                if guild.id not in self._initialized:
                    await ensure_control_center(self.bot, guild)
                    self._initialized.add(guild.id)
                await self.refresh_guild(guild)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Music panel reconciliation failed for guild %s", guild.id
                )

    async def publish(
        self,
        session: MusicGuildSession,
        *,
        disconnected: bool = False,
    ) -> None:
        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            return
        try:
            await self.refresh_guild(guild, session=session, disconnected=disconnected)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Panel outages must never turn successful playback into a failure.
            logger.exception("Music panel publish failed for guild %s", guild.id)

    async def refresh_guild(
        self,
        guild: discord.Guild,
        *,
        session: MusicGuildSession | None = None,
        disconnected: bool = False,
    ) -> discord.Message | None:
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            current_session = (
                session if session is not None else self.manager.get(guild.id)
            )
            embed = build_public_music_embed(
                self.manager, current_session, disconnected=disconnected
            )
            view = self.view(guild.id)
            signature = _render_signature(embed, view)
            record = self._records.get(guild.id)
            now = time.monotonic()

            if record is not None and now - record.checked_at >= _HEALTH_CHECK_SECONDS:
                try:
                    record.message = await record.message.channel.fetch_message(
                        record.message.id
                    )
                    record.checked_at = now
                    record.signature = ""
                except discord.NotFound:
                    record = None
                    self._records.pop(guild.id, None)
                except discord.DiscordException:
                    record.checked_at = now

            if record is None:
                return await self._create_panel(guild, embed, view, signature, now)
            if record.signature == signature:
                return record.message
            try:
                edited = await edit_message_with_retry(
                    record.message,
                    content=None,
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.NotFound:
                self._records.pop(guild.id, None)
                return await self._create_panel(guild, embed, view, signature, now)
            record.message = edited
            record.signature = signature
            record.checked_at = now
            return edited

    async def _create_panel(
        self,
        guild: discord.Guild,
        embed: discord.Embed,
        view: PublicMusicPanelView,
        signature: str,
        now: float,
    ) -> discord.Message | None:
        channel = await resolve_control_channel(guild, "music")
        if channel is None:
            return None
        message = await ensure_panel_message(channel, "music", embed=embed, view=view)
        self._records[guild.id] = _PanelRecord(message, signature, now)
        await self._retire_legacy_card(guild, message)
        return message

    def forget_message(self, guild_id: int, message_id: int) -> None:
        record = self._records.get(int(guild_id))
        if record is not None and int(record.message.id) == int(message_id):
            self._records.pop(int(guild_id), None)

    def forget_channel(self, guild_id: int, channel_id: int) -> None:
        record = self._records.get(int(guild_id))
        if record is not None and int(record.message.channel.id) == int(channel_id):
            self._records.pop(int(guild_id), None)

    async def _retire_legacy_card(
        self, guild: discord.Guild, canonical: discord.Message
    ) -> None:
        if guild.id in self._legacy_retired:
            return
        self._legacy_retired.add(guild.id)
        prefix = f"music_status:{guild.id}"
        try:
            channel_raw, message_raw = await asyncio.gather(
                asyncio.to_thread(get_meta, f"{prefix}:channel_id"),
                asyncio.to_thread(get_meta, f"{prefix}:message_id"),
            )
            channel_id, message_id = int(channel_raw or 0), int(message_raw or 0)
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return
        if not channel_id or not message_id or message_id == int(canonical.id):
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(message_id)
            titles = {str(embed.title or "") for embed in message.embeds}
            bot_user_id = int(getattr(self.bot.user, "id", 0) or 0)
            if int(message.author.id) == bot_user_id and "🎵 T-Mod Music" in titles:
                await message.delete(
                    reason="Музыкальная панель перенесена в общий центр"
                )
                await asyncio.gather(
                    asyncio.to_thread(set_meta_value, f"{prefix}:channel_id", "0"),
                    asyncio.to_thread(set_meta_value, f"{prefix}:message_id", "0"),
                )
        except (discord.NotFound, discord.Forbidden):
            return
        except (discord.DiscordException, OSError, sqlite3.Error):
            return


__all__ = ["MusicPublicPanelService"]
