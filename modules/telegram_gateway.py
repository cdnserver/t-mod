"""Optional Telegram gateway for T-Mod account linking and Atlas AI.

The gateway deliberately uses the Bot API over the already-declared aiohttp
dependency.  No Telegram SDK or second persistence layer is needed.  It is
disabled until ``TELEGRAM_BOT_TOKEN`` is supplied in the private environment.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections import defaultdict
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from modules.atlas_ai import AtlasAIError, atlas_answer
from modules.global_log_runtime import emit_global_event
from persistence import atlas_billing_repository as billing_storage
from persistence import atlas_repository as atlas_storage
from persistence import global_ban_repository as ban_storage
from persistence import profile_repository as profile_storage
from persistence import telegram_repository as telegram_storage
from persistence import web_auth_repository as auth_storage
from persistence.access_projection_repository import get_web_access_projection


try:
    _GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0") or 0)
except (TypeError, ValueError):
    _GUILD_ID = 0

_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@").lower()
_POLLING_ENABLED = os.getenv("TELEGRAM_POLLING_ENABLED", "true").lower() in {
    "1", "true", "yes", "on"
}
_COMMAND_RE = re.compile(r"^\s*/(?P<name>[a-zA-Z0-9_]+)(?:@[a-zA-Z0-9_]+)?(?:\s+(?P<args>.*))?$", re.DOTALL)
_ATLAS_RE = re.compile(r"^\s*атлас\s*2?\s*[,;:—–-]\s*(?P<question>.+)$", re.IGNORECASE | re.DOTALL)
_MAX_TELEGRAM_TEXT = 3900


def _chunks(value: str, limit: int = _MAX_TELEGRAM_TEXT) -> list[str]:
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


def _telegram_display_name(user: dict[str, Any]) -> str:
    first = str(user.get("first_name") or "").strip()
    last = str(user.get("last_name") or "").strip()
    username = str(user.get("username") or "").strip()
    return " ".join(item for item in (first, last) if item) or username or "Telegram пользователь"


def _parse_command(text: str) -> tuple[str, str] | None:
    match = _COMMAND_RE.match(str(text or ""))
    if match is None:
        return None
    return str(match.group("name") or "").lower(), str(match.group("args") or "").strip()


async def _access_allowed(guild_id: int, discord_user_id: int) -> bool:
    if await asyncio.to_thread(ban_storage.is_globally_banned, int(guild_id), int(discord_user_id)):
        return False
    projection = await asyncio.to_thread(
        get_web_access_projection, int(guild_id), int(discord_user_id)
    )
    if projection is not None and bool(projection.administrator) and bool(projection.guild_member):
        return True
    grants = await asyncio.to_thread(
        auth_storage.web_section_grants, int(guild_id), int(discord_user_id)
    )
    return any(str(item.get("section")) == "atlas_ai" for item in grants)


class _TelegramApi:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.base_url = f"https://api.telegram.org/bot{_TOKEN}"

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        async with self.session.post(
            f"{self.base_url}/{method}",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=45, connect=10, sock_read=40),
        ) as response:
            body = await response.json(content_type=None)
            if response.status >= 400 or not bool(body.get("ok")):
                description = str(body.get("description") or f"HTTP {response.status}")[:300]
                raise RuntimeError(f"telegram_api_{method}:{description}")
            return body.get("result") or {}

    async def send(self, chat_id: int, text: str) -> None:
        for part in _chunks(text):
            await self.call(
                "sendMessage",
                {
                    "chat_id": int(chat_id),
                    "text": part,
                    "disable_web_page_preview": True,
                },
            )

    async def typing(self, chat_id: int) -> None:
        await self.call("sendChatAction", {"chat_id": int(chat_id), "action": "typing"})


async def _handle_atlas(
    api: _TelegramApi,
    *,
    chat_id: int,
    telegram_user: dict[str, Any],
    question: str,
    direct_mode: bool = False,
    locks: defaultdict[int, asyncio.Lock],
) -> None:
    if _GUILD_ID <= 0:
        await api.send(chat_id, "Atlas пока не настроен: администратору нужно указать DISCORD_GUILD_ID.")
        return
    telegram_user_id = int(telegram_user.get("id") or 0)
    link = await asyncio.to_thread(
        telegram_storage.get_link_by_telegram, _GUILD_ID, telegram_user_id
    )
    if link is None:
        await api.send(
            chat_id,
            "Сначала привяжите T-Mod аккаунт: в Discord выполните `/telegram-link`, "
            "затем отправьте сюда выданный одноразовый код командой `/link КОД`.",
        )
        return
    owner_id = int(link["discord_user_id"])
    if not await _access_allowed(_GUILD_ID, owner_id):
        await api.send(chat_id, "Для этого аккаунта пока нет доступа к Atlas AI.")
        return
    if not question.strip():
        await api.send(chat_id, "Напишите вопрос после `/atlas`, например: `/atlas что делать при задержании?`")
        return
    async with locks[telegram_user_id]:
        try:
            entitlement = await asyncio.to_thread(billing_storage.atlas_ai_entitlement, owner_id)
            if not bool(entitlement.get("allowed")):
                await api.send(chat_id, "Atlas Token закончились. Пополните баланс в личном кабинете Atlas.")
                return
            thread = await asyncio.to_thread(
                telegram_storage.get_atlas_thread,
                _GUILD_ID,
                owner_id,
                int(chat_id),
            )
            display_name = str(link.get("telegram_display_name") or "Telegram пользователь")
            if thread is None:
                dashboard = await asyncio.to_thread(
                    atlas_storage.atlas_dashboard, _GUILD_ID, owner_id, display_name
                )
                organization_id = int(dashboard["organization"]["id"])
                atlas_thread_id = await asyncio.to_thread(
                    atlas_storage.atlas_create_thread,
                    organization_id,
                    owner_id,
                    question[:100],
                    agent_id="atlas-tvr-a",
                )
                thread = await asyncio.to_thread(
                    telegram_storage.save_atlas_thread,
                    _GUILD_ID,
                    owner_id,
                    int(chat_id),
                    organization_id,
                    atlas_thread_id,
                )
            organization_id = int(thread["organization_id"])
            atlas_thread_id = int(thread["atlas_thread_id"])
            history = await asyncio.to_thread(
                atlas_storage.atlas_thread_messages,
                organization_id,
                owner_id,
                atlas_thread_id,
                limit=80,
            )
            memory = await asyncio.to_thread(
                atlas_storage.atlas_recent_chat_memory,
                organization_id,
                owner_id,
                exclude_thread_id=atlas_thread_id,
                agent_id="atlas-tvr-a",
            )
            dashboard = await asyncio.to_thread(
                atlas_storage.atlas_dashboard, _GUILD_ID, owner_id, display_name, organization_id
            )
            profile = dict(dashboard["membership"].get("profile") or {})
            await api.typing(chat_id)
            result = await atlas_answer(
                organization_id,
                question if not direct_mode else f"Атлас 2, {question}",
                history=list(history["messages"]),
                memory=memory,
                server_code=str(profile.get("server_code") or "phoenix-15"),
                faction_code=str(profile.get("faction_code") or "lspd"),
                model_id="atlas-tvr-a",
                user_profile=profile,
            )
            await asyncio.to_thread(
                atlas_storage.atlas_add_message,
                atlas_thread_id,
                "user",
                question,
                project_code=result["project_code"],
                server_code=result["server_code"],
                faction_code=result["faction_code"],
            )
            assistant_id = await asyncio.to_thread(
                atlas_storage.atlas_add_message,
                atlas_thread_id,
                "assistant",
                result["answer"],
                citations=result["citations"],
                model=result["model"],
                model_provider=result["model_provider"],
                model_release=result["model_release"],
                project_code=result["project_code"],
                server_code=result["server_code"],
                faction_code=result["faction_code"],
                latency_ms=result["latency_ms"],
            )
            try:
                await asyncio.to_thread(
                    billing_storage.atlas_record_ai_usage,
                    owner_id,
                    organization_id,
                    request_key=f"telegram:{chat_id}:{assistant_id}",
                    source="telegram",
                    model=result["model"],
                    model_provider=result["model_provider"],
                    usage=result.get("usage"),
                    message_id=assistant_id,
                )
            except Exception as exc:  # metering must not suppress an answer
                emit_global_event({
                    "event_type": "atlas.telegram.metering_error",
                    "summary": "Atlas Telegram metering failed",
                    "actor_user_id": owner_id,
                    "guild_id": _GUILD_ID,
                    "details": {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                })
            await api.send(chat_id, str(result.get("answer") or "Ответ не сформирован."))
            emit_global_event({
                "event_type": "atlas.telegram.answer",
                "summary": "Atlas ответил в Telegram",
                "actor_user_id": owner_id,
                "guild_id": _GUILD_ID,
                "content_text": question,
                "details": {
                    "telegram_user_id": telegram_user_id,
                    "telegram_chat_id": int(chat_id),
                    "model": result.get("model"),
                    "latency_ms": result.get("latency_ms"),
                },
            })
        except AtlasAIError as exc:
            await api.send(chat_id, "Atlas временно не смог ответить. Повторите запрос через несколько секунд.")
            emit_global_event({
                "event_type": "atlas.telegram.error",
                "summary": "Atlas Telegram request failed",
                "actor_user_id": owner_id,
                "guild_id": _GUILD_ID,
                "details": {"code": exc.code, "retryable": bool(exc.retryable)},
            })
        except Exception as exc:  # keep polling alive after a single bad update
            await api.send(chat_id, "Не удалось обработать запрос. Попробуйте ещё раз.")
            print(f"Telegram Atlas request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            emit_global_event({
                "event_type": "atlas.telegram.error",
                "summary": "Atlas Telegram handler failed",
                "actor_user_id": owner_id,
                "guild_id": _GUILD_ID,
                "details": {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
            })


async def _poll(api: _TelegramApi, *, stop: asyncio.Event) -> None:
    offset = 0
    locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
    while not stop.is_set():
        try:
            updates = await api.call(
                "getUpdates",
                {"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
            )
            for update in updates if isinstance(updates, list) else []:
                offset = max(offset, int(update.get("update_id") or 0) + 1)
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                user = message.get("from") or {}
                chat_id = int(chat.get("id") or 0)
                if chat_id <= 0 or str(chat.get("type") or "") != "private":
                    continue
                text = str(message.get("text") or "").strip()
                if not text:
                    continue
                parsed = _parse_command(text)
                if parsed is not None:
                    command, args = parsed
                    if command in {"start", "help"}:
                        await api.send(
                            chat_id,
                            "T-Mod · Atlas\n\n"
                            "Привязка: в Discord выполните `/telegram-link`, затем `/link КОД`.\n"
                            "Вопрос: `/atlas ваш вопрос`. Для текстового режима — `/atlas2 ваш вопрос`.",
                        )
                        continue
                    if command == "link":
                        result = await asyncio.to_thread(
                            telegram_storage.consume_link_challenge,
                            args,
                            guild_id=_GUILD_ID,
                            telegram_user_id=int(user.get("id") or 0),
                            telegram_chat_id=chat_id,
                            telegram_username=str(user.get("username") or ""),
                            telegram_display_name=_telegram_display_name(user),
                        )
                        messages = {
                            "invalid_code": "Код выглядит неверно. Возьмите новый код командой `/telegram-link` в Discord.",
                            "invalid_or_expired_code": "Код недействителен или уже использован. Запросите новый в Discord.",
                            "telegram_already_linked": "Этот Telegram-профиль уже привязан к другому T-Mod аккаунту.",
                        }
                        await api.send(
                            chat_id,
                            "Telegram привязан к T-Mod аккаунту. Теперь доступен `/atlas ваш вопрос`."
                            if result.get("ok")
                            else messages.get(str(result.get("error")), "Не удалось привязать аккаунт."),
                        )
                        continue
                    if command in {"atlas", "atlas2"}:
                        await _handle_atlas(
                            api,
                            chat_id=chat_id,
                            telegram_user=user,
                            question=args,
                            direct_mode=command == "atlas2",
                            locks=locks,
                        )
                        continue
                    await api.send(chat_id, "Неизвестная команда. Используйте `/help`.")
                    continue
                match = _ATLAS_RE.match(text)
                if match:
                    await _handle_atlas(
                        api,
                        chat_id=chat_id,
                        telegram_user=user,
                        question=str(match.group("question") or "").strip(),
                        direct_mode=text.casefold().startswith("атлас 2"),
                        locks=locks,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Telegram polling failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass


def setup_telegram_gateway(bot: commands.Bot) -> None:
    """Register Discord binding commands and prepare one optional poller."""

    started = False
    stop = asyncio.Event()

    @bot.tree.command(name="telegram-link", description="Привязать свой T-Mod аккаунт к Telegram")
    @app_commands.guild_only()
    async def telegram_link(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or int(interaction.guild_id) != _GUILD_ID:
            await interaction.response.send_message("Команда доступна в основном сервере Товарищества.", ephemeral=True)
            return
        challenge = await asyncio.to_thread(
            telegram_storage.create_link_challenge,
            int(interaction.guild_id),
            int(interaction.user.id),
        )
        bot_hint = f"@{_BOT_USERNAME}" if _BOT_USERNAME else "ваш Telegram-бот"
        await interaction.response.send_message(
            f"Одноразовый код для {bot_hint}:\n`{challenge['code']}`\n\n"
            "Откройте бота в Telegram и отправьте `/link "
            f"{challenge['code']}`. Код действует 10 минут и больше нигде не показывается.",
            ephemeral=True,
        )

    @bot.tree.command(name="telegram-unlink", description="Отвязать свой Telegram-профиль от T-Mod")
    @app_commands.guild_only()
    async def telegram_unlink(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or int(interaction.guild_id) != _GUILD_ID:
            await interaction.response.send_message("Команда доступна в основном сервере Товарищества.", ephemeral=True)
            return
        changed = await asyncio.to_thread(
            telegram_storage.unlink_by_discord,
            int(interaction.guild_id),
            int(interaction.user.id),
        )
        await interaction.response.send_message(
            "Telegram отвязан." if changed else "Для вашего аккаунта активной привязки нет.",
            ephemeral=True,
        )

    async def runner() -> None:
        nonlocal started
        if started or not _TOKEN or not _POLLING_ENABLED or _GUILD_ID <= 0:
            if not _TOKEN:
                print("Telegram gateway: disabled (TELEGRAM_BOT_TOKEN is empty)", flush=True)
            return
        started = True
        timeout = aiohttp.ClientTimeout(total=50, connect=10, sock_read=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            api = _TelegramApi(session)
            try:
                await api.call("deleteWebhook", {"drop_pending_updates": False})
            except Exception as exc:
                print(f"Telegram webhook cleanup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            print("Telegram gateway: polling enabled", flush=True)
            await _poll(api, stop=stop)

    # ``setup_telegram_gateway`` is called while the module catalog is built,
    # before ``Bot.run`` has installed its event loop.  Store the coroutine
    # factory and start it from ``setup_hook`` instead of touching ``bot.loop``
    # during import.
    setattr(bot, "_tmod_telegram_runner", runner)


async def start_telegram_gateway(bot: commands.Bot) -> None:
    runner = getattr(bot, "_tmod_telegram_runner", None)
    if runner is None:
        return
    task = getattr(bot, "_tmod_telegram_task", None)
    if task is None or task.done():
        setattr(bot, "_tmod_telegram_task", bot.loop.create_task(runner()))


__all__ = [
    "setup_telegram_gateway",
    "start_telegram_gateway",
    "_chunks",
    "_parse_command",
    "_access_allowed",
]
