"""Private `/account` presentation and controls for Voice Control diagnostics."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Awaitable, Callable

import discord

from persistence import voice_control_context as voice_storage
from modules.voice_control_service import VoiceDiagnosticResult, get_voice_control


PROFILE_VOICE_COLOR = 0x5865F2
PROFILE_VOICE_TIMEOUT_SECONDS = 900
RenderCallback = Callable[..., Awaitable[None]]
ErrorCallback = Callable[[discord.Interaction, Exception], Awaitable[None]]


def _clean(value: Any, *, fallback: str = "Не указано") -> str:
    text = " ".join(str(value or "").strip().split())
    return discord.utils.escape_markdown(text) if text else fallback


def _discord_time(value: datetime | str | None) -> str:
    if value is None:
        return "не зафиксировано"
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return "не зафиксировано"
    return f"<t:{int(parsed.timestamp())}:R>"


def profile_microphone_embed(
    member: discord.Member,
    voice_profile: Any | None,
    *,
    local_status: str,
    diagnostic: VoiceDiagnosticResult | None = None,
) -> discord.Embed:
    status_info = {
        "ready": ("🟢", "Готов"),
        "loading": ("🟡", "Загружается"),
        "cold": ("🟡", "Ожидает загрузки"),
        "degraded": ("🟠", "Работает через облачный резерв"),
        "not_installed": ("🟠", "Доступен облачный резерв"),
        "disabled": ("⚪", "Локальный путь отключён"),
    }
    status_emoji, status_label = status_info.get(
        local_status,
        ("⚪", "Состояние неизвестно"),
    )
    embed = discord.Embed(
        title="🎙️ Голос и микрофон",
        description=(
            f"Персональная диагностика **{_clean(member.display_name)}**. "
            "Аудиозапись и расшифровка не сохраняются — в базе остаются только "
            "технические показатели и безопасное усиление."
        ),
        color=PROFILE_VOICE_COLOR,
    )
    embed.add_field(
        name="Voice Control v2",
        value=(
            f"{status_emoji} Быстрый локальный движок: **{status_label}**\n"
            "OpenRouter автоматически используется для сложных названий и как резерв."
        ),
        inline=False,
    )
    current = diagnostic.profile if diagnostic is not None else voice_profile
    if current is not None and getattr(current, "calibrated_at", None):
        score = int(getattr(current, "quality_score", 0) or 0)
        icon = "🟢" if score >= 68 else "🟡" if score >= 48 else "🔴"
        embed.add_field(
            name="Текущая калибровка",
            value=(
                f"{icon} Качество: **{score}/100**\n"
                f"Усиление: **×{float(getattr(current, 'input_gain', 1.0) or 1.0):.2f}** · "
                f"сигнал/шум: **{float(getattr(current, 'snr_db', 0.0) or 0.0):.1f} dB**\n"
                f"Обновлено: {_discord_time(getattr(current, 'calibrated_at', None))}"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="Текущая калибровка",
            value="Не выполнена. T-Mod пока использует безопасные общие настройки.",
            inline=False,
        )
    if current is not None and int(getattr(current, "commands_total", 0) or 0) > 0:
        total = int(getattr(current, "commands_total", 0) or 0)
        failures = int(getattr(current, "failures_total", 0) or 0)
        average = getattr(current, "average_latency_ms", None)
        embed.add_field(
            name="Личная статистика",
            value=(
                f"Команд: **{total}** · повторов: **{failures}**\n"
                f"Среднее распознавание: **{average if average is not None else '—'} мс** · "
                f"последний путь: `{_clean(getattr(current, 'last_engine', None), fallback='—')}`"
            )[:1024],
            inline=False,
        )
    if diagnostic is not None:
        assessment = diagnostic.assessment
        embed.add_field(
            name=f"Результат · {assessment.quality_label}",
            value=(
                f"Оценка: **{assessment.quality_score}/100** · задержка: "
                f"**{diagnostic.latency_ms} мс**\n"
                f"Сигнал/шум: **{assessment.snr_db:.1f} dB** · клиппинг: "
                f"**{assessment.clipping_percent:.2f}%**\n{assessment.summary}"
            )[:1024],
            inline=False,
        )
        embed.add_field(
            name="Что услышал T-Mod",
            value=f"> {_clean(diagnostic.transcript, fallback='Речь не расшифрована')}"[:1024],
            inline=False,
        )
    embed.add_field(
        name="Как проверить",
        value=(
            "Зайдите в голосовой канал, нажмите **«Проверить»** и сразу после сигнала скажите:\n"
            "> **Банан, включи группу Кино**"
        ),
        inline=False,
    )
    embed.set_footer(text="Калибровка действует для всех будущих голосовых модулей T-Mod")
    return embed


class ProfileMicrophoneView(discord.ui.View):
    def __init__(
        self,
        requester_id: int,
        member: discord.Member,
        *,
        render: RenderCallback | None = None,
        back: RenderCallback | None = None,
        report_error: ErrorCallback | None = None,
    ) -> None:
        super().__init__(timeout=PROFILE_VOICE_TIMEOUT_SECONDS)
        self.requester_id = int(requester_id)
        self.member = member
        self.render = render
        self.render_back = back
        self.report_error = report_error

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Это меню профиля открыто для другого пользователя.",
            ephemeral=True,
        )
        return False

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        if self.report_error is not None:
            await self.report_error(interaction, error)

    async def _render(
        self,
        interaction: discord.Interaction,
        *,
        diagnostic: VoiceDiagnosticResult | None = None,
    ) -> None:
        if self.render is not None:
            await self.render(
                interaction,
                self.requester_id,
                self.member,
                diagnostic=diagnostic,
            )

    @discord.ui.button(label="Проверить", emoji="🎙️", style=discord.ButtonStyle.primary)
    async def diagnose(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        voice_control = get_voice_control(interaction.client)
        if voice_control is None:
            await interaction.followup.send(
                "Voice Control ещё загружается. Повторите через несколько секунд.",
                ephemeral=True,
            )
            await self._render(interaction)
            return
        try:
            result = await voice_control.run_diagnostic(
                self.member,
                int(interaction.channel_id or 0),
            )
        except Exception as exc:
            await interaction.followup.send(
                f"Проверка не завершена: {str(exc)[:1000]}",
                ephemeral=True,
            )
            await self._render(interaction)
            return
        await self._render(interaction, diagnostic=result)

    @discord.ui.button(
        label="Сбросить калибровку",
        emoji="♻️",
        style=discord.ButtonStyle.secondary,
    )
    async def reset(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        voice_control = get_voice_control(interaction.client)
        if voice_control is not None:
            await voice_control.reset_calibration(self.member.guild.id, self.member.id)
        else:
            await asyncio.to_thread(
                voice_storage.reset_microphone_calibration,
                self.member.guild.id,
                self.member.id,
            )
        await self._render(interaction)

    @discord.ui.button(
        label="Назад",
        emoji="↩️",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.render_back is not None:
            await self.render_back(interaction, self.requester_id, self.member)


__all__ = ["ProfileMicrophoneView", "profile_microphone_embed"]
