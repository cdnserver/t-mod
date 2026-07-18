"""Discord presentation and personal controls for T-Mod Music."""

from __future__ import annotations

from typing import Any, Callable, Coroutine

import discord

from modules.music_domain import clip_track_title, format_duration
from modules.music_runtime import MusicGuildSession, MusicManager, MusicRuntimeError


MUSIC_COLOR = 0x7C5CFC


def _safe_title(value: str, limit: int = 90) -> str:
    return discord.utils.escape_markdown(clip_track_title(value, limit))


def build_music_embed(
    manager: MusicManager,
    session: MusicGuildSession | None,
    *,
    disconnected: bool = False,
) -> discord.Embed:
    if session is None:
        embed = discord.Embed(
            title="🎵 T-Mod Music",
            description=(
                "Зайдите в голосовой канал и нажмите **«Подключить»**. Затем добавляйте музыку "
                "по русскому названию или ссылке YouTube."
            ),
            color=MUSIC_COLOR,
        )
        embed.add_field(
            name="Голосовое управление",
            value=(
                "Доступно после подключения: включается отдельно для каждого пользователя. "
                "Скажите **«банан»**, **«сборщик риса»** или **«т мод»**, либо нажмите "
                "**«Голосовая команда»**, дождитесь сигнала и произнесите команду."
                if manager.voice_receive_available
                else "Сейчас недоступно; кнопки и команда `/music` продолжают работать."
            ),
            inline=False,
        )
        embed.set_footer(text="T-Mod • /music • персональная панель")
        return embed

    voice_client = manager._voice_client(session.guild_id)
    if disconnected or voice_client is None or not voice_client.is_connected():
        state = "⚫ отключён"
    elif voice_client.is_paused():
        state = "⏸️ пауза"
    elif voice_client.is_playing():
        state = "🟢 воспроизведение"
    else:
        state = "🟡 подключён и ожидает"
    embed = discord.Embed(
        title="🎵 T-Mod Music",
        description=(
            f"**Состояние:** {state}\n"
            f"**Голосовой канал:** <#{session.voice_channel_id}>\n"
            f"**Громкость:** `{round(session.volume * 100)}%`"
        ),
        color=MUSIC_COLOR,
    )
    if session.current is not None:
        track = session.current
        value = (
            f"[{_safe_title(track.title, 180)}]({track.webpage_url})\n"
            f"Длительность: `{format_duration(track.duration_seconds)}`"
        )
        if track.uploader:
            value += f" · {_safe_title(track.uploader, 80)}"
        if track.requested_by_id:
            value += f"\nДобавил: <@{track.requested_by_id}>"
        embed.add_field(name="Сейчас играет", value=value[:1024], inline=False)
        if track.thumbnail_url:
            embed.set_thumbnail(url=track.thumbnail_url)
    else:
        embed.add_field(name="Сейчас играет", value="Пока ничего.", inline=False)

    if session.queue:
        lines = [
            f"`{index:02d}` [{_safe_title(track.title, 72)}]({track.webpage_url}) "
            f"· `{format_duration(track.duration_seconds)}`"
            for index, track in enumerate(list(session.queue)[:8], start=1)
        ]
        if len(session.queue) > 8:
            lines.append(f"…и ещё **{len(session.queue) - 8}**")
        queue_text = "\n".join(lines)
    else:
        queue_text = "Очередь пуста."
    embed.add_field(
        name=f"Очередь · {len(session.queue)}", value=queue_text[:1024], inline=False
    )
    voice_text = (
        f"✅ включено для **{len(session.voice_users)}** участн.\n"
        "Обрабатывается только речь пользователей, включивших функцию для себя."
        if session.voice_users
        else (
            "Готово к включению. Аудио не сохраняется."
            if manager.voice_receive_available
            else "Недоступно в текущей сборке; обычное управление работает."
        )
    )
    embed.add_field(name="🎙️ Голосовой вызов", value=voice_text, inline=False)
    if session.last_error:
        embed.add_field(
            name="⚠️ Последняя ошибка", value=session.last_error[:1024], inline=False
        )
    elif session.last_notice:
        embed.add_field(
            name="Последнее действие", value=session.last_notice[:1024], inline=False
        )
    embed.set_footer(
        text="T-Mod Music • откройте /music для личных кнопок • голосовые фрагменты не хранятся"
    )
    return embed


def build_queue_embed(session: MusicGuildSession) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎼 Очередь T-Mod Music · {len(session.queue)}",
        color=MUSIC_COLOR,
    )
    if not session.queue:
        embed.description = "Очередь пуста."
        return embed
    blocks = []
    for index, track in enumerate(list(session.queue)[:20], start=1):
        blocks.append(
            f"`{index:02d}` [{_safe_title(track.title, 90)}]({track.webpage_url})\n"
            f"      `{format_duration(track.duration_seconds)}`"
            + (
                f" · добавил <@{track.requested_by_id}>"
                if track.requested_by_id
                else ""
            )
        )
    if len(session.queue) > 20:
        blocks.append(f"…и ещё **{len(session.queue) - 20}**")
    embed.description = "\n".join(blocks)[:4000]
    embed.set_footer(text="Очередь воспроизводится сверху вниз")
    return embed


ButtonCallback = Callable[[discord.Interaction], Coroutine[Any, Any, None]]


class MusicPanelView(discord.ui.View):
    def __init__(self, manager: MusicManager, requester_id: int, guild_id: int) -> None:
        super().__init__(timeout=900)
        self.manager = manager
        self.requester_id = int(requester_id)
        self.guild_id = int(guild_id)
        session = manager.get(guild_id)
        voice_client = manager._voice_client(guild_id)
        connected = bool(voice_client and voice_client.is_connected() and session)
        paused = bool(voice_client and voice_client.is_paused())

        self._add("Подключить", "🔊", discord.ButtonStyle.success, self.connect, row=0)
        self._add(
            "Отключить",
            "👋",
            discord.ButtonStyle.danger,
            self.disconnect,
            row=0,
            disabled=not connected,
        )
        voice_enabled = bool(
            session
            and requester_id in session.voice_users
            and requester_id not in session.one_shot_voice_users
        )
        self._add(
            "Голос: выключить" if voice_enabled else "Голос: включить",
            "🎙️",
            discord.ButtonStyle.primary
            if voice_enabled
            else discord.ButtonStyle.secondary,
            self.voice_toggle,
            row=0,
            disabled=not connected,
        )
        self._add(
            "Включить",
            "🔎",
            discord.ButtonStyle.primary,
            self.play,
            row=1,
            disabled=not connected,
        )
        self._add(
            "Продолжить" if paused else "Пауза",
            "▶️" if paused else "⏸️",
            discord.ButtonStyle.secondary,
            self.pause_or_resume,
            row=1,
            disabled=not connected or not session or session.current is None,
        )
        self._add(
            "Следующая",
            "⏭️",
            discord.ButtonStyle.secondary,
            self.skip,
            row=1,
            disabled=not connected or not session or session.current is None,
        )
        self._add(
            "Стоп",
            "⏹️",
            discord.ButtonStyle.danger,
            self.stop,
            row=1,
            disabled=not connected or not session,
        )
        self._add(
            "Тише",
            "🔉",
            discord.ButtonStyle.secondary,
            self.volume_down,
            row=2,
            disabled=not connected,
        )
        self._add(
            "Громче",
            "🔊",
            discord.ButtonStyle.secondary,
            self.volume_up,
            row=2,
            disabled=not connected,
        )
        self._add(
            "Очередь",
            "📋",
            discord.ButtonStyle.secondary,
            self.queue,
            row=2,
            disabled=not connected,
        )
        self._add("Обновить", "🔄", discord.ButtonStyle.secondary, self.refresh, row=2)
        self._add(
            "Голосовая команда",
            "🎤",
            discord.ButtonStyle.primary,
            self.voice_command,
            row=3,
            disabled=not connected,
        )

    def _add(
        self,
        label: str,
        emoji: str,
        style: discord.ButtonStyle,
        callback: ButtonCallback,
        *,
        row: int,
        disabled: bool = False,
    ) -> None:
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=style,
            row=row,
            disabled=disabled,
        )
        button.callback = callback
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Эта музыкальная панель открыта для другого пользователя.",
                ephemeral=True,
            )
            return False
        if interaction.guild is None or not isinstance(
            interaction.user, discord.Member
        ):
            await interaction.response.send_message(
                "Музыкальная панель работает только на сервере.",
                ephemeral=True,
            )
            return False
        return True

    async def _edit(self, interaction: discord.Interaction) -> None:
        session = self.manager.get(self.guild_id)
        await interaction.edit_original_response(
            content=None,
            embed=build_music_embed(self.manager, session),
            view=MusicPanelView(self.manager, self.requester_id, self.guild_id),
        )

    async def _run(
        self,
        interaction: discord.Interaction,
        action: Callable[[discord.Member], Coroutine[Any, Any, Any]],
    ) -> None:
        assert isinstance(interaction.user, discord.Member)
        await interaction.response.defer()
        try:
            await action(interaction.user)
        except Exception as exc:
            session = self.manager.get(self.guild_id)
            if session is not None:
                session.last_error = str(exc)[:500]
            else:
                await interaction.followup.send(str(exc)[:1500], ephemeral=True)
        await self._edit(interaction)

    async def connect(self, interaction: discord.Interaction) -> None:
        assert isinstance(interaction.user, discord.Member)
        await interaction.response.defer()
        try:
            await self.manager.connect(interaction.user, interaction.channel_id)
        except Exception as exc:
            session = self.manager.get(self.guild_id)
            if session is not None:
                session.last_error = str(exc)[:500]
            else:
                await interaction.followup.send(str(exc)[:1500], ephemeral=True)
        await self._edit(interaction)

    async def disconnect(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.manager.disconnect)

    async def voice_toggle(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.manager.toggle_voice_control)

    async def voice_command(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.manager.arm_voice_command)

    async def play(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            MusicSearchModal(self.manager, self.requester_id, self.guild_id)
        )

    async def pause_or_resume(self, interaction: discord.Interaction) -> None:
        voice_client = self.manager._voice_client(self.guild_id)
        action = (
            self.manager.resume
            if voice_client and voice_client.is_paused()
            else self.manager.pause
        )
        await self._run(interaction, action)

    async def skip(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.manager.skip)

    async def stop(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.manager.stop)

    async def volume_down(self, interaction: discord.Interaction) -> None:
        async def action(member: discord.Member) -> None:
            session = self.manager.get(member.guild.id)
            if session is None:
                raise MusicRuntimeError("T-Mod сейчас не подключён к музыке.")
            await self.manager.set_volume(member, round(session.volume * 100) - 10)

        await self._run(interaction, action)

    async def volume_up(self, interaction: discord.Interaction) -> None:
        async def action(member: discord.Member) -> None:
            session = self.manager.get(member.guild.id)
            if session is None:
                raise MusicRuntimeError("T-Mod сейчас не подключён к музыке.")
            await self.manager.set_volume(member, round(session.volume * 100) + 10)

        await self._run(interaction, action)

    async def queue(self, interaction: discord.Interaction) -> None:
        session = self.manager.get(self.guild_id)
        if session is None:
            await interaction.response.send_message(
                "Очередь пока не создана.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=build_queue_embed(session), ephemeral=True
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._edit(interaction)


class MusicSearchModal(discord.ui.Modal):
    def __init__(self, manager: MusicManager, requester_id: int, guild_id: int) -> None:
        super().__init__(title="Включить музыку", timeout=600)
        self.manager = manager
        self.requester_id = int(requester_id)
        self.guild_id = int(guild_id)
        self.query = discord.ui.TextInput(
            label="Название или ссылка YouTube",
            placeholder="Например: Кино — Группа крови",
            min_length=2,
            max_length=240,
            required=True,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild is None
            or not isinstance(interaction.user, discord.Member)
            or interaction.user.id != self.requester_id
        ):
            await interaction.response.send_message(
                "Эта форма открыта не для вас.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            track = await self.manager.enqueue(interaction.user, str(self.query.value))
            content = f"Добавлено: **{_safe_title(track.title, 180)}**"
        except Exception as exc:
            content = f"Не удалось добавить композицию: {str(exc)[:1200]}"
        await interaction.edit_original_response(
            content=content,
            embed=build_music_embed(self.manager, self.manager.get(self.guild_id)),
            view=MusicPanelView(self.manager, self.requester_id, self.guild_id),
        )


__all__ = [
    "MUSIC_COLOR",
    "MusicPanelView",
    "MusicSearchModal",
    "build_music_embed",
    "build_queue_embed",
]
