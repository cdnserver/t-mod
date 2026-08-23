"""Discord entry point for Atlas AI with durable per-thread context."""

from __future__ import annotations

import asyncio
import os
import re
import traceback
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from modules.atlas_ai import AtlasAIError, atlas_answer
from modules.technical_log import log_technical_event
from persistence import atlas_repository as atlas_storage
from persistence import global_ban_repository as global_ban_storage
from persistence import profile_repository as profile_storage
from persistence import web_auth_repository as auth_storage


try:
    ATLAS_CHANNEL_ID = int(os.getenv("ATLAS_DISCORD_CHANNEL_ID", "1494602485485277194") or 0)
except (TypeError, ValueError):
    ATLAS_CHANNEL_ID = 1494602485485277194
_ATLAS_CALL_RE = re.compile(r"^\s*атлас\s*[,;:—–-]\s*(.+)$", re.IGNORECASE | re.DOTALL)


def _chunks(value: str, limit: int = 3900) -> list[str]:
    text = str(value or "").strip()
    result: list[str] = []
    while text:
        if len(text) <= limit:
            result.append(text)
            break
        boundary = max(text.rfind("\n", 0, limit), text.rfind(". ", 0, limit))
        if boundary < limit // 2:
            boundary = limit
        result.append(text[:boundary].strip())
        text = text[boundary:].strip()
    return result or ["Ответ не сформирован."]


async def _atlas_access(guild_id: int, user: discord.abc.User) -> bool:
    if await asyncio.to_thread(
        global_ban_storage.is_globally_banned,
        int(guild_id),
        int(user.id),
    ):
        return False
    if isinstance(user, discord.Member) and user.guild_permissions.administrator:
        return True
    grants = await asyncio.to_thread(
        auth_storage.web_section_grants,
        int(guild_id),
        int(user.id),
    )
    return any(str(item.get("section")) == "atlas_ai" for item in grants)


async def _send_answer(channel: discord.abc.Messageable, result: dict) -> None:
    parts = _chunks(str(result.get("answer") or ""))
    citations = list(result.get("citations") or [])
    for index, part in enumerate(parts):
        embed = discord.Embed(
            title="Atlas" if index == 0 else "Atlas · продолжение",
            description=part,
            color=0x6BD8FF,
        )
        if index == 0 and citations:
            source_lines = []
            for item in citations[:8]:
                title = discord.utils.escape_markdown(str(item.get("title") or "Источник"))[:100]
                url = str(item.get("url") or "").strip()
                pinpoints = ", ".join(
                    discord.utils.escape_markdown(str(value))
                    for value in list(item.get("pinpoints") or [])[:4]
                )
                label = f"{title} · {pinpoints}" if pinpoints else title
                source_lines.append(
                    f"[{item.get('index', '•')}] [{label}]({url})"
                    if url
                    else f"[{item.get('index', '•')}] {label}"
                )
            embed.add_field(name="Источники", value="\n".join(source_lines)[:1024], inline=False)
        embed.set_footer(text=f"atlas-tvr-a · {int(result.get('latency_ms') or 0)} мс")
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


def setup_atlas_discord(bot: commands.Bot) -> None:
    locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @bot.tree.command(name="atlas-access", description="Выдать или отозвать доступ к Atlas AI")
    @app_commands.guild_only()
    @app_commands.describe(user="Пользователь Discord", enabled="Включить или отозвать доступ")
    async def atlas_access_command(
        interaction: discord.Interaction,
        user: discord.User,
        enabled: bool = True,
    ) -> None:
        actor = interaction.user
        if not isinstance(actor, discord.Member) or not actor.guild_permissions.administrator:
            await interaction.response.send_message("Действие доступно администратору.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        changed = await asyncio.to_thread(
            auth_storage.web_set_section_grant,
            int(interaction.guild_id or 0),
            int(user.id),
            "atlas_ai",
            enabled=bool(enabled),
            granted_by_id=int(actor.id),
        )
        dm_sent = False
        if enabled and changed:
            embed = discord.Embed(
                title="Вам открыт Atlas AI",
                description=(
                    "Теперь можно обращаться к Atlas на сайте и в выделенном канале Discord. "
                    "Для веб-входа создайте T-Mod Account командой `/account` в ЛС с ботом."
                ),
                url="https://atlas.tvr.lat/",
                color=0x57F2C8,
            )
            try:
                await user.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                dm_sent = True
            except discord.DiscordException:
                pass
        guild = interaction.guild
        if guild is not None:
            await log_technical_event(
                bot,
                guild,
                title="Atlas · доступ изменён",
                details=(
                    f"Пользователь: <@{user.id}> (`{user.id}`)\n"
                    f"Действие: **{'выдан' if enabled else 'отозван'}**\n"
                    f"Администратор: <@{actor.id}>\nЛС: **{'доставлено' if dm_sent else 'не требовалось / недоступно'}**"
                ),
                level="info",
                dedupe_key=f"atlas-access:{user.id}:{int(enabled)}",
                cooldown_seconds=2,
                component="atlas",
            )
        await interaction.edit_original_response(
            content=(
                "Доступ к Atlas AI выдан."
                if enabled and changed
                else "Доступ к Atlas AI отозван."
                if not enabled and changed
                else "Состояние доступа уже было таким."
            )
        )

    async def answer_in_thread(
        message: discord.Message,
        mapping: dict,
        question: str,
        channel: discord.TextChannel | discord.Thread | None = None,
    ) -> None:
        conversation_channel = channel or message.channel
        async with locks[int(conversation_channel.id)]:
            history = await asyncio.to_thread(
                atlas_storage.atlas_thread_messages,
                int(mapping["organization_id"]),
                int(mapping["owner_user_id"]),
                int(mapping["atlas_thread_id"]),
                limit=80,
            )
            memory = await asyncio.to_thread(
                atlas_storage.atlas_recent_chat_memory,
                int(mapping["organization_id"]),
                int(mapping["owner_user_id"]),
                exclude_thread_id=int(mapping["atlas_thread_id"]),
                agent_id="atlas-tvr-a",
            )
            dashboard = await asyncio.to_thread(
                atlas_storage.atlas_dashboard,
                int(message.guild.id),
                int(mapping["owner_user_id"]),
                str(getattr(message.author, "display_name", message.author.name)),
            )
            profile = dict(dashboard["membership"].get("profile") or {})
            async with conversation_channel.typing():
                result = await atlas_answer(
                    int(mapping["organization_id"]),
                    question,
                    history=list(history["messages"]),
                    memory=memory,
                    server_code=str(profile.get("server_code") or "phoenix-15"),
                    faction_code=str(profile.get("faction_code") or "lspd"),
                    model_id="atlas-tvr-a",
                    user_profile=profile,
                )
            await asyncio.to_thread(
                atlas_storage.atlas_add_message,
                int(mapping["atlas_thread_id"]),
                "user",
                question,
            )
            await asyncio.to_thread(
                atlas_storage.atlas_add_message,
                int(mapping["atlas_thread_id"]),
                "assistant",
                result["answer"],
                citations=result["citations"],
                model=result["model"],
                latency_ms=result["latency_ms"],
            )
            await _send_answer(conversation_channel, result)
            await asyncio.to_thread(
                atlas_storage.atlas_record_event,
                int(mapping["organization_id"]),
                int(mapping["owner_user_id"]),
                "ai_answer_created",
                "Atlas ответил в Discord",
                target_type="discord_thread",
                target_id=int(conversation_channel.id),
                details={"source": "discord", "model": result["model"]},
            )

    @bot.listen("on_message")
    async def atlas_message_listener(message: discord.Message) -> None:
        if message.author.bot or message.guild is None or ATLAS_CHANNEL_ID <= 0:
            return
        question = ""
        mapping: dict | None = None
        conversation_channel = message.channel
        if isinstance(message.channel, discord.Thread):
            if int(message.channel.parent_id or 0) != ATLAS_CHANNEL_ID:
                return
            mapping = await asyncio.to_thread(
                atlas_storage.atlas_discord_thread,
                int(message.channel.id),
            )
            if mapping is None or int(mapping["owner_user_id"]) != int(message.author.id):
                return
            if not await _atlas_access(message.guild.id, message.author):
                return
            question = str(message.content or "").strip()
        elif int(message.channel.id) == ATLAS_CHANNEL_ID:
            matched = _ATLAS_CALL_RE.match(str(message.content or ""))
            if matched is None:
                return
            question = matched.group(1).strip()
            if not await _atlas_access(message.guild.id, message.author):
                await message.reply(
                    "Доступ к Atlas AI пока не выдан. Обратитесь к администратору.",
                    delete_after=12,
                    mention_author=False,
                )
                return
            characters = await asyncio.to_thread(
                profile_storage.list_profile_characters,
                int(message.guild.id),
                int(message.author.id),
            )
            if not characters:
                await message.reply(
                    "Сначала создайте T-Mod Account и добавьте персонажа командой `/account` в ЛС.",
                    delete_after=16,
                    mention_author=False,
                )
                return
            thread = await message.create_thread(
                name=f"Atlas · {question[:72]}",
                auto_archive_duration=1440,
            )
            conversation_channel = thread
            dashboard = await asyncio.to_thread(
                atlas_storage.atlas_dashboard,
                int(message.guild.id),
                int(message.author.id),
                str(message.author.display_name),
            )
            organization_id = int(dashboard["organization"]["id"])
            atlas_thread_id = await asyncio.to_thread(
                atlas_storage.atlas_create_thread,
                organization_id,
                int(message.author.id),
                question[:100],
            )
            mapping = await asyncio.to_thread(
                atlas_storage.atlas_bind_discord_thread,
                discord_thread_id=int(thread.id),
                guild_id=int(message.guild.id),
                parent_channel_id=ATLAS_CHANNEL_ID,
                organization_id=organization_id,
                atlas_thread_id=atlas_thread_id,
                owner_user_id=int(message.author.id),
            )
        else:
            return
        if not question or mapping is None:
            return
        try:
            await answer_in_thread(
                message,
                mapping,
                question[:8000],
                channel=conversation_channel,
            )
        except AtlasAIError as exc:
            await conversation_channel.send(
                f"Atlas временно не смог ответить: **{str(exc)}**",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await log_technical_event(
                bot,
                message.guild,
                title="Atlas · ответ временно недоступен",
                details=(
                    f"Канал: <#{conversation_channel.id}>\nПользователь: <@{message.author.id}>\n"
                    f"Код: `{exc.code}`\nОшибка: `{str(exc)[:900]}`"
                ),
                level="warning" if exc.retryable else "error",
                dedupe_key=f"atlas-discord-ai:{exc.code}",
                cooldown_seconds=120,
                exception=exc,
                component="atlas",
            )
        except Exception as exc:  # noqa: BLE001 - gateway listener must survive
            traceback.print_exc()
            await log_technical_event(
                bot,
                message.guild,
                title="Atlas · ошибка Discord-диалога",
                details=(
                    f"Канал: <#{conversation_channel.id}>\nПользователь: <@{message.author.id}>\n"
                    f"Ошибка: `{type(exc).__name__}: {str(exc)[:900]}`"
                ),
                dedupe_key=f"atlas-discord:{type(exc).__name__}",
                cooldown_seconds=60,
                exception=exc,
                component="atlas",
            )
            try:
                await conversation_channel.send("Atlas столкнулся с ошибкой. Событие уже передано в технический журнал.")
            except discord.DiscordException:
                pass


__all__ = ["ATLAS_CHANNEL_ID", "setup_atlas_discord"]
