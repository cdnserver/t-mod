"""Persistent shared controls and presentation for T-Mod Music."""

from __future__ import annotations

from typing import Any, Callable, Coroutine

import discord

from modules.control_center_config import PANEL_MARKERS
from modules.music_domain import format_duration
from modules.music_progress import playback_elapsed_seconds, progress_bar
from modules.music_runtime import MusicGuildSession, MusicManager, MusicRuntimeError
from modules.music_views import (
    MUSIC_COLOR,
    MusicSearchModal,
    build_music_embed,
    build_queue_embed,
)


def build_public_music_embed(
    manager: MusicManager,
    session: MusicGuildSession | None,
    *,
    disconnected: bool = False,
) -> discord.Embed:
    """Render the shared card without a per-user state or volatile timestamp."""

    embed = build_music_embed(manager, session, disconnected=disconnected)
    if session is None:
        embed.description = (
            "Зайдите в голосовой канал и нажмите **«Подключить»**. Все участники видят "
            "одно состояние, а результат каждого нажатия T-Mod показывает только нажавшему."
        )
    embed.color = MUSIC_COLOR
    if (
        session is not None
        and session.current is not None
        and session.track_started_at is not None
    ):
        elapsed = playback_elapsed_seconds(session)
        duration = session.current.duration_seconds
        if duration is not None:
            elapsed = min(elapsed, duration)
        timeline = progress_bar(elapsed, duration)
        timing = (
            f"`{format_duration(elapsed)}` {timeline} `{format_duration(duration)}`"
        )
        for index, field in enumerate(embed.fields):
            if field.name == "Сейчас играет":
                value = f"{str(field.value)[:850]}\n{timing}"
                embed.set_field_at(index, name=field.name, value=value, inline=False)
                break
    embed.set_footer(
        text=(
            "T-Mod Music • общая панель • проверка каждую секунду • "
            f"{PANEL_MARKERS['music']}"
        )
    )
    return embed


ButtonAction = Callable[[discord.Member], Coroutine[Any, Any, Any]]


class PublicMusicPanelView(discord.ui.View):
    """Persistent controls whose responses are always private to the actor."""

    def __init__(self, manager: MusicManager, guild_id: int | None = None) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        self.guild_id = int(guild_id or 0)
        self._add_controls()

    def _add_button(
        self,
        *,
        label: str,
        emoji: str,
        style: discord.ButtonStyle,
        custom_id: str,
        row: int,
        callback: Callable[[discord.Interaction], Coroutine[Any, Any, None]],
        disabled: bool = False,
    ) -> None:
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=custom_id,
            row=row,
            disabled=disabled,
        )
        button.callback = callback
        self.add_item(button)

    def _add_controls(self) -> None:
        # The displayed instance reflects global playback state. A generic
        # instance registered on startup still routes every stable custom_id.
        guild_id = self.guild_id
        session = self.manager.get(guild_id) if guild_id else None
        voice_client = self.manager._voice_client(guild_id) if guild_id else None
        connected = bool(session and voice_client and voice_client.is_connected())
        paused = bool(voice_client and voice_client.is_paused())
        has_track = bool(session and session.current)

        self._add_button(
            label="Подключить",
            emoji="🔊",
            style=discord.ButtonStyle.success,
            custom_id="tmod_music_public_connect",
            row=0,
            callback=self.connect,
            disabled=connected,
        )
        self._add_button(
            label="Отключить",
            emoji="👋",
            style=discord.ButtonStyle.danger,
            custom_id="tmod_music_public_disconnect",
            row=0,
            callback=self.disconnect,
            disabled=not connected,
        )
        self._add_button(
            label="Голос для меня",
            emoji="🎙️",
            style=discord.ButtonStyle.secondary,
            custom_id="tmod_music_public_voice_toggle",
            row=0,
            callback=self.voice_toggle,
            disabled=not connected,
        )
        self._add_button(
            label="Включить",
            emoji="🔎",
            style=discord.ButtonStyle.primary,
            custom_id="tmod_music_public_play",
            row=1,
            callback=self.play,
            disabled=not connected,
        )
        self._add_button(
            label="Продолжить" if paused else "Пауза",
            emoji="▶️" if paused else "⏸️",
            style=discord.ButtonStyle.secondary,
            custom_id="tmod_music_public_pause",
            row=1,
            callback=self.pause_or_resume,
            disabled=not connected or not has_track,
        )
        self._add_button(
            label="Следующая",
            emoji="⏭️",
            style=discord.ButtonStyle.secondary,
            custom_id="tmod_music_public_skip",
            row=1,
            callback=self.skip,
            disabled=not connected or not has_track,
        )
        self._add_button(
            label="Стоп",
            emoji="⏹️",
            style=discord.ButtonStyle.danger,
            custom_id="tmod_music_public_stop",
            row=1,
            callback=self.stop,
            disabled=not connected,
        )
        self._add_button(
            label="Тише",
            emoji="🔉",
            style=discord.ButtonStyle.secondary,
            custom_id="tmod_music_public_volume_down",
            row=2,
            callback=self.volume_down,
            disabled=not connected,
        )
        self._add_button(
            label="Громче",
            emoji="🔊",
            style=discord.ButtonStyle.secondary,
            custom_id="tmod_music_public_volume_up",
            row=2,
            callback=self.volume_up,
            disabled=not connected,
        )
        self._add_button(
            label="Очередь",
            emoji="📋",
            style=discord.ButtonStyle.secondary,
            custom_id="tmod_music_public_queue",
            row=2,
            callback=self.queue,
            disabled=not connected,
        )
        self._add_button(
            label="Обновить",
            emoji="🔄",
            style=discord.ButtonStyle.secondary,
            custom_id="tmod_music_public_refresh",
            row=2,
            callback=self.refresh,
        )
        self._add_button(
            label="Голосовая команда",
            emoji="🎤",
            style=discord.ButtonStyle.primary,
            custom_id="tmod_music_public_voice_command",
            row=3,
            callback=self.voice_command,
            disabled=not connected,
        )

    @classmethod
    def for_guild(cls, manager: MusicManager, guild_id: int) -> PublicMusicPanelView:
        return cls(manager, guild_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(
            interaction.user, discord.Member
        ):
            await interaction.response.send_message(
                "Музыкальный центр работает только на сервере.", ephemeral=True
            )
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, content: str) -> None:
        await interaction.edit_original_response(content=content)

    async def _run(
        self,
        interaction: discord.Interaction,
        action: ButtonAction,
        success: str,
    ) -> None:
        assert isinstance(interaction.user, discord.Member)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await action(interaction.user)
            content = success
        except Exception as exc:
            content = f"Не удалось выполнить действие: {str(exc)[:1200]}"
        await self._finish(interaction, content)

    async def connect(self, interaction: discord.Interaction) -> None:
        assert isinstance(interaction.user, discord.Member)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.manager.connect(
                interaction.user, int(interaction.channel_id or 0)
            )
            content = "T-Mod подключён к вашему голосовому каналу."
        except Exception as exc:
            content = f"Не удалось подключиться: {str(exc)[:1200]}"
        await self._finish(interaction, content)

    async def disconnect(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.manager.disconnect, "T-Mod отключён.")

    async def voice_toggle(self, interaction: discord.Interaction) -> None:
        async def action(member: discord.Member) -> None:
            await self.manager.toggle_voice_control(member)

        await self._run(
            interaction, action, "Настройка голосового управления изменена."
        )

    async def voice_command(self, interaction: discord.Interaction) -> None:
        await self._run(
            interaction,
            self.manager.arm_voice_command,
            "Слушаю одну команду. Говорите после сигнала — обращение не требуется.",
        )

    async def play(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.send_modal(
            MusicSearchModal(
                self.manager,
                interaction.user.id,
                interaction.guild.id,
                compact_response=True,
            )
        )

    async def pause_or_resume(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        voice_client = self.manager._voice_client(interaction.guild.id)
        action = (
            self.manager.resume
            if voice_client and voice_client.is_paused()
            else self.manager.pause
        )
        await self._run(interaction, action, "Состояние воспроизведения изменено.")

    async def skip(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.manager.skip, "Текущая композиция пропущена.")

    async def stop(self, interaction: discord.Interaction) -> None:
        await self._run(
            interaction, self.manager.stop, "Музыка остановлена, очередь очищена."
        )

    async def volume_down(self, interaction: discord.Interaction) -> None:
        await self._change_volume(interaction, -10)

    async def volume_up(self, interaction: discord.Interaction) -> None:
        await self._change_volume(interaction, 10)

    async def _change_volume(
        self, interaction: discord.Interaction, delta: int
    ) -> None:
        async def action(member: discord.Member) -> None:
            session = self.manager.get(member.guild.id)
            if session is None:
                raise MusicRuntimeError("T-Mod сейчас не подключён к музыке.")
            await self.manager.set_volume(member, round(session.volume * 100) + delta)

        await self._run(interaction, action, "Громкость изменена.")

    async def queue(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.manager.get(interaction.guild.id)
        if session is None:
            await interaction.response.send_message(
                "Очередь пока не создана.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=build_queue_embed(session), ephemeral=True
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        service = getattr(self.manager, "public_panel_service", None)
        try:
            if service is not None:
                await service.refresh_guild(interaction.guild)
            content = "Общая музыкальная панель обновлена."
        except Exception as exc:
            content = f"Не удалось обновить общую панель: {str(exc)[:1200]}"
        await self._finish(interaction, content)


__all__ = ["PublicMusicPanelView", "build_public_music_embed"]
