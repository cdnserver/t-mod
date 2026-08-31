"""Discord collection and one-time access command for the global log."""

from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from modules.global_log_runtime import emit_global_event, redact_value
from persistence import global_log_repository as repository


def _message_event(message: discord.Message, event_type: str, *, before: str | None = None) -> None:
    channel = message.channel
    guild = message.guild
    attachments = [
        {
            "id": str(item.id), "filename": item.filename, "size": item.size,
            "content_type": item.content_type, "url": item.url,
        }
        for item in list(message.attachments)[:20]
    ]
    emit_global_event({
        "source_service": "discord",
        "source_type": "message",
        "event_type": event_type,
        "actor_user_id": int(message.author.id),
        "actor_display": getattr(message.author, "display_name", str(message.author)),
        "guild_id": int(guild.id) if guild is not None else None,
        "channel_id": int(channel.id),
        "message_id": int(message.id),
        "summary": f"Discord: {event_type} · {getattr(message.author, 'display_name', message.author)}",
        "content_text": message.content,
        "target_type": "discord_message",
        "target_id": str(message.id),
        "details": {
            "before": before,
            "content": message.content,
            "attachments": attachments,
            "embeds": [item.to_dict() for item in list(message.embeds)[:10]],
            "stickers": [str(item.id) for item in list(message.stickers)[:10]],
            "reference_message_id": getattr(message.reference, "message_id", None),
            "channel_name": getattr(channel, "name", None),
            "guild_name": getattr(guild, "name", None),
            "author_bot": bool(message.author.bot),
        },
    })


def setup_global_log_discord(bot: commands.Bot) -> None:
    @bot.tree.command(
        name="global-log",
        description="Получить одноразовый код защищённого глобального журнала",
    )
    async def global_log_code(interaction: discord.Interaction) -> None:
        user_id = int(interaction.user.id)
        if not repository.user_is_allowed(user_id):
            emit_global_event({
                "source_service": "discord", "source_type": "authentication",
                "event_type": "global_log_code_denied", "severity": "warning",
                "actor_user_id": user_id,
                "summary": "Отклонён запрос кода глобального журнала",
                "guild_id": interaction.guild_id, "channel_id": interaction.channel_id,
            })
            await interaction.response.send_message(
                "Доступ к этому контуру не назначен.", ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        code = await asyncio.to_thread(
            repository.issue_login_code,
            user_id,
            requested_from=f"discord:{interaction.guild_id or 'dm'}",
        )
        text = (
            "**T-Mod · Глобальный журнал**\n\n"
            f"Одноразовый код: `{code}`\n"
            "Срок действия — 5 минут. Код погашается после первого входа.\n\n"
            "Откройте https://log.global.tvr.lat и укажите свой Discord ID. "
            "Никому не пересылайте этот код."
        )
        dm_delivered = True
        try:
            await interaction.user.send(text)
        except discord.HTTPException:
            dm_delivered = False
        if dm_delivered:
            await interaction.followup.send(
                "Одноразовый код отправлен вам в личные сообщения. Он действует 5 минут.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                text + "\n\nЛичные сообщения закрыты, поэтому код показан здесь.",
                ephemeral=True,
            )
        emit_global_event({
            "source_service": "discord", "source_type": "authentication",
            "event_type": "global_log_code_issued", "actor_user_id": user_id,
            "summary": "Выдан одноразовый код глобального журнала",
            "guild_id": interaction.guild_id, "channel_id": interaction.channel_id,
            "details": {"dm_delivered": dm_delivered, "expires_seconds": 300},
        })

    async def interaction_listener(interaction: discord.Interaction) -> None:
        data = interaction.data if isinstance(interaction.data, dict) else {}
        command = getattr(interaction.command, "qualified_name", None)
        custom_id = str(data.get("custom_id") or "")[:500] or None
        emit_global_event({
            "source_service": "discord",
            "source_type": "interaction",
            "event_type": f"interaction_{str(interaction.type).split('.')[-1]}",
            "actor_user_id": int(interaction.user.id),
            "actor_display": getattr(interaction.user, "display_name", str(interaction.user)),
            "guild_id": interaction.guild_id,
            "channel_id": interaction.channel_id,
            "message_id": getattr(interaction.message, "id", None),
            "summary": f"Discord interaction: {command or custom_id or interaction.type}",
            "target_type": "command" if command else "component",
            "target_id": command or custom_id,
            "details": {
                "interaction_id": str(interaction.id),
                "type": str(interaction.type),
                "command": command,
                "data": redact_value(data),
            },
        })

    async def message_listener(message: discord.Message) -> None:
        _message_event(message, "message_created")

    async def edit_listener(before: discord.Message, after: discord.Message) -> None:
        if before.content == after.content and before.embeds == after.embeds and before.attachments == after.attachments:
            return
        _message_event(after, "message_edited", before=before.content)

    async def delete_listener(message: discord.Message) -> None:
        _message_event(message, "message_deleted")

    async def raw_delete_listener(payload: discord.RawMessageDeleteEvent) -> None:
        if payload.cached_message is not None:
            return
        emit_global_event({
            "source_service": "discord", "source_type": "message",
            "event_type": "message_deleted_uncached", "guild_id": payload.guild_id,
            "channel_id": payload.channel_id, "message_id": payload.message_id,
            "summary": "Discord: удалено некэшированное сообщение",
            "target_type": "discord_message", "target_id": str(payload.message_id),
        })

    async def error_listener(event_method: str, *args: Any, **kwargs: Any) -> None:
        emit_global_event({
            "source_service": "discord", "source_type": "system",
            "event_type": "discord_event_error", "severity": "error",
            "summary": f"Ошибка обработчика Discord: {event_method}",
            "details": {"event_method": event_method, "args_count": len(args), "kwargs": list(kwargs)},
        })

    bot.add_listener(interaction_listener, "on_interaction")
    bot.add_listener(message_listener, "on_message")
    bot.add_listener(edit_listener, "on_message_edit")
    bot.add_listener(delete_listener, "on_message_delete")
    bot.add_listener(raw_delete_listener, "on_raw_message_delete")
    # discord.py calls Bot.on_error rather than dispatching an event to extra
    # listeners.  The global command tree and HTTP middleware cover the common
    # failures; the callback stays available for explicit dispatches.
    bot.add_listener(error_listener, "on_global_log_error")


__all__ = ["setup_global_log_discord"]
