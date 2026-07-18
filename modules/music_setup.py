"""Composition root for the /music command and voice lifecycle listeners."""

from __future__ import annotations

from collections.abc import Callable

import discord
from discord.ext import commands

from modules.music_runtime import MusicManager
from modules.music_views import MusicPanelView, build_music_embed


def setup_music(
    bot: commands.Bot,
    remember_command_activity: Callable[[discord.Interaction, str, str], None],
) -> MusicManager:
    manager = MusicManager(bot)

    @bot.tree.command(
        name="music",
        description="Музыка YouTube и голосовое управление T-Mod",
    )
    async def music(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
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
