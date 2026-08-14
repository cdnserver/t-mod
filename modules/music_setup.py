"""Composition root for the /music command and voice lifecycle listeners."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial

import discord
from discord.ext import commands

from modules.music_audio import voice_recv
from modules.music_public_panel import MusicPublicPanelService
from modules.music_public_views import PublicMusicPanelView
from modules.music_runtime import MusicManager
from modules.music_views import MusicPanelView, build_music_embed
from modules.music_voice_diagnostics import run_microphone_diagnostic


def _install_voice_recv_disconnect_guard() -> bool:
    """Close a race in voice-recv when an SSRC leaves after the reader stops."""

    if voice_recv is None:
        return False
    client_type = getattr(voice_recv, "VoiceRecvClient", None)
    original = getattr(client_type, "_remove_ssrc", None)
    if client_type is None or not callable(original):
        return False
    if bool(getattr(original, "__tmod_disconnect_guard__", False)):
        return True

    def remove_ssrc_safely(client, *, user_id: int) -> None:
        ssrc = client._id_to_ssrc.pop(user_id, None)
        if not ssrc:
            return
        reader = getattr(client, "_reader", None)
        timer = getattr(reader, "speaking_timer", None)
        if timer is not None:
            timer.drop_ssrc(ssrc)
        client._ssrc_to_id.pop(ssrc, None)

    remove_ssrc_safely.__tmod_disconnect_guard__ = True  # type: ignore[attr-defined]
    client_type._remove_ssrc = remove_ssrc_safely
    return True


def setup_music(
    bot: commands.Bot,
    remember_command_activity: Callable[[discord.Interaction, str, str], None],
) -> MusicManager:
    _install_voice_recv_disconnect_guard()
    # SenderReport RTCP packets are routine Discord control traffic. The
    # experimental receiver currently logs them at INFO as "unexpected".
    logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)
    # discord.py handles the new websocket sequence field before invoking the
    # extension hook; the extension only reports it as an unknown extra key.
    logging.getLogger("discord.ext.voice_recv.gateway").setLevel(logging.WARNING)
    # Small UDP gaps are expected and are tolerated by STT; the extension logs
    # jitter-buffer flushes as warnings even when the receiver stays healthy.
    logging.getLogger("discord.ext.voice_recv.opus").setLevel(logging.ERROR)
    manager = MusicManager(bot)
    bot.music_manager = manager
    bot.voice_control = manager.voice_control
    manager.voice_control.set_diagnostic_gateway(
        partial(run_microphone_diagnostic, manager)
    )
    panel_service = MusicPublicPanelService(bot, manager)
    manager.status_publisher = panel_service.publish
    manager.public_panel_service = panel_service
    persistent_view_registered = False

    @bot.listen("on_ready")
    async def music_public_panel_ready() -> None:
        nonlocal persistent_view_registered
        manager.voice_control.start()
        if not persistent_view_registered:
            bot.add_view(PublicMusicPanelView(manager))
            persistent_view_registered = True
        await panel_service.start()

    @bot.listen("on_raw_message_delete")
    async def music_public_panel_message_deleted(
        payload: discord.RawMessageDeleteEvent,
    ) -> None:
        if payload.guild_id is not None:
            panel_service.forget_message(payload.guild_id, payload.message_id)

    @bot.listen("on_guild_channel_delete")
    async def music_public_panel_channel_deleted(
        channel: discord.abc.GuildChannel,
    ) -> None:
        panel_service.forget_channel(channel.guild.id, channel.id)

    @bot.tree.command(
        name="music",
        description="Музыка YouTube и голосовое управление T-Mod",
    )
    async def music(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(
            interaction.user, discord.Member
        ):
            await interaction.response.send_message(
                "Команда работает только на сервере Discord.",
                ephemeral=True,
            )
            return
        remember_command_activity(interaction, "command_music", "/music")
        await interaction.response.send_message(
            embed=build_music_embed(manager, manager.get(interaction.guild.id)),
            view=MusicPanelView(manager, interaction.user.id, interaction.guild.id),
            ephemeral=True,
        )

    @bot.listen("on_voice_state_update")
    async def music_voice_lifecycle(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        await manager.handle_voice_state_update(member, before, after)

    return manager


__all__ = ["setup_music"]
