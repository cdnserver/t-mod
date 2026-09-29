"""Operator-only Blackbird overlay messages, delivered through durable notifications."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from modules.technical_log import log_technical_event
from persistence import reactor_repository, web_auth_repository


_DEFAULT_OPERATOR_ID = 902235631952998410
_SURFACES = {"overlay", "fullscreen", "toast"}


def overlay_operator_ids() -> set[int]:
    raw = os.getenv("BLACKBIRD_OVERLAY_OPERATOR_IDS", str(_DEFAULT_OPERATOR_ID))
    return {int(item) for item in re.split(r"[\s,;]+", raw) if item.isdigit() and int(item) > 0}


def resolve_overlay_recipient(guild_id: int, target: str) -> int | None:
    """Resolve a Discord ID or an exact T-Mod login; never do fuzzy account lookup."""
    value = str(target or "").strip()
    if value.isdigit():
        user_id = int(value)
        return user_id if web_auth_repository.get_web_credential(guild_id, user_id) else None
    try:
        return web_auth_repository.web_user_id_for_login(guild_id, value)
    except ValueError:
        return None


def setup_overlay_remote_discord(bot: commands.Bot) -> None:
    @bot.tree.command(name="orl", description="Отправить сообщение в Blackbird выбранному аккаунту")
    @app_commands.guild_only()
    @app_commands.describe(
        target="Discord ID или точный логин T-Mod",
        type="Где показать сообщение",
        msg="Текст сообщения (до 500 символов)",
    )
    @app_commands.choices(type=[
        app_commands.Choice(name="Оверлей в игре", value="overlay"),
        app_commands.Choice(name="На весь экран", value="fullscreen"),
        app_commands.Choice(name="Уведомление", value="toast"),
    ])
    async def orl(
        interaction: discord.Interaction,
        target: str,
        type: app_commands.Choice[str],
        msg: app_commands.Range[str, 1, 500],
    ) -> None:
        guild_id = int(interaction.guild_id or 0)
        if not guild_id or int(interaction.user.id) not in overlay_operator_ids():
            await interaction.response.send_message("Команда доступна только операторам Blackbird.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        recipient = await asyncio.to_thread(resolve_overlay_recipient, guild_id, target)
        if recipient is None:
            await interaction.followup.send("Аккаунт с таким Discord ID или точным логином не найден.", ephemeral=True)
            return
        surface = str(type.value)
        if surface not in _SURFACES:
            await interaction.followup.send("Неизвестный тип показа.", ephemeral=True)
            return
        message = str(msg).strip()
        if not message:
            await interaction.followup.send("Сообщение пустое.", ephemeral=True)
            return
        notification_id = await asyncio.to_thread(
            reactor_repository.reactor_put_notification,
            guild_id=guild_id,
            user_id=recipient,
            severity="info",
            kind=f"orl:{surface}",
            title="Сообщение Blackbird",
            body=message,
            dedupe_key=f"orl:{interaction.id}",
            source_key=f"operator:{interaction.user.id}",
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        await interaction.followup.send(
            f"Сообщение поставлено в очередь Blackbird (№{notification_id}). "
            "Оно появится, когда клиент получателя будет запущен и подключён.",
            ephemeral=True,
        )
        if interaction.guild is not None:
            await log_technical_event(
                bot,
                interaction.guild,
                title="Отправлено сообщение в Blackbird",
                details=f"Оператор: {interaction.user.id}; получатель: {recipient}; показ: {surface}; событие: {notification_id}",
                level="info",
                dedupe_key=f"orl:{interaction.id}",
                component="blackbird-overlay",
            )


__all__ = ["overlay_operator_ids", "resolve_overlay_recipient", "setup_overlay_remote_discord"]
