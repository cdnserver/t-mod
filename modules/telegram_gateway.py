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
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

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
from persistence import reactor_repository as reactor_storage
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
_MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "🤖 Atlas"}, {"text": "👤 Профиль"}],
        [{"text": "🧩 Персонажи"}, {"text": "💳 Atlas Token"}],
        [{"text": "🔔 Уведомления"}, {"text": "⚙ Настройки"}],
        [{"text": "🆕 Новый диалог"}],
        [{"text": "⌂ Главное меню"}],
        [{"text": "❌ Выйти из Atlas"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "Выберите раздел или напишите вопрос…",
}


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


def _format_count(value: Any) -> str:
    try:
        return f"{int(value or 0):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def _greeting() -> str:
    try:
        hour = datetime.now(ZoneInfo("Europe/Moscow")).hour
    except Exception:
        hour = datetime.now().hour
    if 5 <= hour < 12:
        return "Доброе утро"
    if 12 <= hour < 18:
        return "Добрый день"
    if 18 <= hour < 23:
        return "Добрый вечер"
    return "Доброй ночи"


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

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        for part in _chunks(text):
            payload: dict[str, Any] = {
                "chat_id": int(chat_id),
                "text": part,
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            await self.call(
                "sendMessage",
                payload,
            )

    async def typing(self, chat_id: int) -> None:
        await self.call("sendChatAction", {"chat_id": int(chat_id), "action": "typing"})


async def _keep_typing(api: _TelegramApi, chat_id: int, stop: asyncio.Event) -> None:
    """Keep Telegram's short-lived typing indicator alive during Atlas work."""

    while not stop.is_set():
        try:
            await api.typing(chat_id)
        except Exception:
            # A transient Bot API failure must never cancel the actual answer.
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            continue


async def _handle_profile(api: _TelegramApi, *, chat_id: int, telegram_user: dict[str, Any]) -> None:
    link = await asyncio.to_thread(
        telegram_storage.get_link_by_telegram,
        _GUILD_ID,
        int(telegram_user.get("id") or 0),
    )
    if link is None:
        await api.send(
            chat_id,
            "Профиль ещё не подключён. Откройте личный Реактор → Подключения → Telegram · Atlas.",
            reply_markup=_MAIN_KEYBOARD,
        )
        return
    profile, characters = await asyncio.to_thread(
        profile_storage.get_profile_snapshot,
        _GUILD_ID,
        int(link["discord_user_id"]),
    )
    preferred = str(getattr(profile, "preferred_name", "") or "").strip()
    character_lines = [
        f"• {character.nickname} · статик {character.static_id}"
        for character in characters[:5]
    ] or ["• Персонажи ещё не добавлены"]
    access = await _access_allowed(_GUILD_ID, int(link["discord_user_id"]))
    await api.send(
        chat_id,
        "👤 Профиль T‑Mod\n\n"
        f"Как обращаться: {preferred or 'не указано'}\n"
        f"Discord ID: {link['discord_user_id']}\n"
        f"Atlas AI: {'доступен' if access else 'доступ ожидает выдачи'}\n\n"
        "Персонажи:\n" + "\n".join(character_lines),
        reply_markup=_MAIN_KEYBOARD,
    )


async def _handle_notifications(api: _TelegramApi, *, chat_id: int, telegram_user: dict[str, Any]) -> None:
    link = await asyncio.to_thread(
        telegram_storage.get_link_by_telegram,
        _GUILD_ID,
        int(telegram_user.get("id") or 0),
    )
    if link is None:
        await api.send(chat_id, "Сначала подключите T‑Mod аккаунт в личном Реакторе.", reply_markup=_MAIN_KEYBOARD)
        return
    inbox = await asyncio.to_thread(
        reactor_storage.reactor_list_notifications,
        _GUILD_ID,
        int(link["discord_user_id"]),
        limit=8,
    )
    items = list(inbox.get("items") or [])
    if not items:
        text = "🔔 Уведомления\n\nНовых событий нет."
    else:
        lines = [f"🔔 Уведомления · новых: {int(inbox.get('unread') or 0)}", ""]
        for item in items:
            marker = "●" if not item.get("read_at") else "○"
            lines.append(f"{marker} {item.get('title') or 'Событие'}\n  {item.get('body') or ''}")
        text = "\n".join(lines)
    await api.send(chat_id, text, reply_markup=_MAIN_KEYBOARD)


async def _handle_settings(api: _TelegramApi, *, chat_id: int, telegram_user: dict[str, Any]) -> None:
    link = await asyncio.to_thread(
        telegram_storage.get_link_by_telegram,
        _GUILD_ID,
        int(telegram_user.get("id") or 0),
    )
    status = "подключён" if link else "не подключён"
    await api.send(
        chat_id,
        "⚙ Настройки\n\n"
        f"Telegram ↔ T‑Mod: {status}\n"
        "Изменить подключение можно в личном Реакторе → Подключения.\n\n"
        "Atlas отвечает в режиме диалога после кнопки «🤖 Atlas».\n"
        "Для выхода используйте /stop.",
        reply_markup=_MAIN_KEYBOARD,
    )


async def _handle_menu(
    api: _TelegramApi,
    *,
    chat_id: int,
    telegram_user: dict[str, Any],
) -> None:
    """Render a small personal dashboard based on the linked T-Mod identity."""

    greeting_name = _telegram_display_name(telegram_user).split()[0]
    link = None
    if _GUILD_ID > 0:
        link = await asyncio.to_thread(
            telegram_storage.get_link_by_telegram,
            _GUILD_ID,
            int(telegram_user.get("id") or 0),
        )
    if link is None:
        body = (
            f"👋 {_greeting()}, {greeting_name}!\n\n"
            "Это ваш личный помощник T‑Mod. Здесь доступны профиль, персонажи и диалог с Atlas.\n\n"
            "Статус аккаунта: ещё не подключён. Чтобы связать T‑Mod и Telegram, отправьте "
            "`/tg-link` в личных сообщениях Discord-боту T‑Mod."
        )
    else:
        owner_id = int(link["discord_user_id"])
        profile, characters = await asyncio.to_thread(
            profile_storage.get_profile_snapshot,
            _GUILD_ID,
            owner_id,
        )
        preferred = str(getattr(profile, "preferred_name", "") or "").strip()
        if preferred:
            greeting_name = preferred
        access = await _access_allowed(_GUILD_ID, owner_id)
        character_count = len(characters)
        inbox = await asyncio.to_thread(
            reactor_storage.reactor_list_notifications,
            _GUILD_ID,
            owner_id,
            limit=1,
        )
        atlas_status = "доступ открыт" if access else "доступ пока не выдан"
        body = (
            f"👋 {_greeting()}, {greeting_name}!\n\n"
            "Ваш T‑Mod аккаунт подключён и готов к работе.\n"
            f"Atlas AI: {atlas_status}\n"
            f"Персонажей: {character_count} · непрочитанных уведомлений: {int(inbox.get('unread') or 0)}\n\n"
            "Куда направимся? Выберите раздел в меню."
        )
    await api.send(chat_id, body, reply_markup=_MAIN_KEYBOARD)


async def _handle_usage(api: _TelegramApi, *, chat_id: int, telegram_user: dict[str, Any]) -> None:
    """Show the user's Atlas plan and token balance without exposing billing data."""

    link = await asyncio.to_thread(
        telegram_storage.get_link_by_telegram,
        _GUILD_ID,
        int(telegram_user.get("id") or 0),
    )
    if link is None:
        await api.send(chat_id, "Сначала подключите T‑Mod аккаунт в личном Реакторе.", reply_markup=_MAIN_KEYBOARD)
        return
    summary = await asyncio.to_thread(
        billing_storage.atlas_billing_summary,
        int(link["discord_user_id"]),
    )
    plan = dict(summary.get("plan") or {})
    usage = dict(summary.get("period_usage") or {})
    await api.send(
        chat_id,
        "💳 Atlas Token\n\n"
        f"Тариф: {plan.get('name') or summary.get('account', {}).get('plan_code') or 'Free'}\n"
        f"Доступно: {_format_count(summary.get('balance_tokens'))} токенов\n"
        f"Из них месячных: {_format_count(summary.get('monthly_balance_tokens'))}\n"
        f"Куплено отдельно: {_format_count(summary.get('payg_balance_tokens'))}\n\n"
        f"Использовано в периоде: {_format_count(usage.get('atlas_tokens'))}\n"
        f"Запросов: {int(usage.get('requests') or 0)}",
        reply_markup=_MAIN_KEYBOARD,
    )


async def _handle_characters(api: _TelegramApi, *, chat_id: int, telegram_user: dict[str, Any]) -> None:
    """List all linked characters in a compact, readable view."""

    link = await asyncio.to_thread(
        telegram_storage.get_link_by_telegram,
        _GUILD_ID,
        int(telegram_user.get("id") or 0),
    )
    if link is None:
        await api.send(chat_id, "Сначала подключите T‑Mod аккаунт в личном Реакторе.", reply_markup=_MAIN_KEYBOARD)
        return
    _profile, characters = await asyncio.to_thread(
        profile_storage.get_profile_snapshot,
        _GUILD_ID,
        int(link["discord_user_id"]),
    )
    if not characters:
        text = "🧩 Персонажи\n\nПерсонажи ещё не добавлены. Добавьте их в личном Реакторе."
    else:
        lines = ["🧩 Персонажи T‑Mod", ""]
        for index, character in enumerate(characters, start=1):
            lines.append(f"{index}. {character.nickname}\n   Статик: {character.static_id}")
        text = "\n".join(lines)
    await api.send(chat_id, text, reply_markup=_MAIN_KEYBOARD)


async def _handle_read_notifications(api: _TelegramApi, *, chat_id: int, telegram_user: dict[str, Any]) -> None:
    link = await asyncio.to_thread(
        telegram_storage.get_link_by_telegram,
        _GUILD_ID,
        int(telegram_user.get("id") or 0),
    )
    if link is None:
        await api.send(chat_id, "Сначала подключите T‑Mod аккаунт в личном Реакторе.", reply_markup=_MAIN_KEYBOARD)
        return
    changed = await asyncio.to_thread(
        reactor_storage.reactor_mark_notifications_read,
        _GUILD_ID,
        int(link["discord_user_id"]),
    )
    await api.send(
        chat_id,
        f"✅ Уведомления отмечены прочитанными: {int(changed)}.",
        reply_markup=_MAIN_KEYBOARD,
    )


async def _handle_new_chat(api: _TelegramApi, *, chat_id: int, telegram_user: dict[str, Any]) -> None:
    """Start a new Atlas thread without deleting the previous conversation."""

    link = await asyncio.to_thread(
        telegram_storage.get_link_by_telegram,
        _GUILD_ID,
        int(telegram_user.get("id") or 0),
    )
    if link is None:
        await api.send(chat_id, "Сначала подключите T‑Mod аккаунт в личном Реакторе.", reply_markup=_MAIN_KEYBOARD)
        return
    await asyncio.to_thread(
        telegram_storage.reset_atlas_thread,
        _GUILD_ID,
        int(link["discord_user_id"]),
        int(chat_id),
    )
    await api.send(
        chat_id,
        "🆕 Новый диалог создан. Старый диалог сохранён в истории Atlas.\n"
        "Нажмите «🤖 Atlas» и отправьте первый вопрос.",
        reply_markup=_MAIN_KEYBOARD,
    )


def _discord_dm_text(message: discord.Message) -> str:
    """Convert a bot-authored Discord DM into a readable Telegram message."""

    sections = ["📩 T‑Mod · уведомление"]
    content = str(message.content or "").strip()
    if content:
        sections.extend(["", content])
    for embed in list(message.embeds or [])[:5]:
        title = str(embed.title or "").strip()
        description = str(embed.description or "").strip()
        if title or description:
            sections.extend(["", title, description])
        for field in list(embed.fields or [])[:12]:
            name = str(field.name or "").strip()
            value = str(field.value or "").strip()
            if name or value:
                sections.append(f"{name}: {value}" if name else value)
        if embed.url:
            sections.append(str(embed.url))
    components = list(getattr(message, "components", []) or [])
    for row in components[:5]:
        for button in list(getattr(row, "children", []) or [])[:5]:
            url = str(getattr(button, "url", "") or "").strip()
            label = str(getattr(button, "label", "") or "Открыть")
            if url:
                sections.append(f"{label}: {url}")
    attachments = list(message.attachments or [])
    if attachments:
        names = ", ".join(str(item.filename or "файл") for item in attachments[:8])
        sections.extend(["", f"В Discord также приложено: {names}"])
    rendered = "\n".join(part for part in sections if part).strip()
    return rendered[:20000] or "У вас новое личное сообщение от T‑Mod в Discord."


async def _mirror_bot_dm_to_telegram(bot: commands.Bot, message: discord.Message) -> None:
    """Mirror a T-Mod private Discord message to the linked Telegram chat."""

    if not _TOKEN or _GUILD_ID <= 0 or bot.user is None:
        return
    if message.guild is not None or int(message.author.id) != int(bot.user.id):
        return
    if not isinstance(message.channel, discord.DMChannel):
        return
    recipient = message.channel.recipient
    if recipient is None:
        # Gateway-created DMChannel objects can lack recipient metadata.
        # Resolve the already-cached channel created by the original send.
        cached_channel = bot.get_channel(int(message.channel.id))
        if isinstance(cached_channel, discord.DMChannel):
            recipient = cached_channel.recipient
    if recipient is None:
        recipient = next(
            (
                channel.recipient
                for channel in bot.private_channels
                if isinstance(channel, discord.DMChannel)
                and int(channel.id) == int(message.channel.id)
                and channel.recipient is not None
            ),
            None,
        )
    if recipient is None:
        return
    link = await asyncio.to_thread(
        telegram_storage.get_link_by_discord,
        _GUILD_ID,
        int(recipient.id),
    )
    if link is None:
        return
    api = getattr(bot, "_tmod_telegram_api", None)
    if api is None:
        return
    try:
        await api.send(int(link["telegram_chat_id"]), _discord_dm_text(message))
        emit_global_event({
            "event_type": "telegram.discord_dm_mirrored",
            "summary": "T-Mod Discord DM продублировано в Telegram",
            "guild_id": _GUILD_ID,
            "actor_user_id": int(recipient.id),
            "details": {
                "discord_message_id": int(message.id),
                "telegram_user_id": int(link["telegram_user_id"]),
            },
        })
    except Exception as exc:
        print(f"Telegram DM mirror failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        emit_global_event({
            "event_type": "telegram.discord_dm_mirror_failed",
            "summary": "Не удалось продублировать DM T-Mod в Telegram",
            "guild_id": _GUILD_ID,
            "actor_user_id": int(recipient.id),
            "details": {
                "discord_message_id": int(message.id),
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            },
        })


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
            typing_stop = asyncio.Event()
            typing_task = asyncio.create_task(_keep_typing(api, chat_id, typing_stop))
            try:
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
            finally:
                typing_stop.set()
                typing_task.cancel()
                await asyncio.gather(typing_task, return_exceptions=True)
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
            await api.send(
                chat_id,
                str(result.get("answer") or "Ответ не сформирован."),
                reply_markup=_MAIN_KEYBOARD,
            )
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
            await api.send(
                chat_id,
                "Atlas временно не смог ответить. Повторите запрос через несколько секунд.",
                reply_markup=_MAIN_KEYBOARD,
            )
            emit_global_event({
                "event_type": "atlas.telegram.error",
                "summary": "Atlas Telegram request failed",
                "actor_user_id": owner_id,
                "guild_id": _GUILD_ID,
                "details": {"code": exc.code, "retryable": bool(exc.retryable)},
            })
        except Exception as exc:  # keep polling alive after a single bad update
            await api.send(
                chat_id,
                "Не удалось обработать запрос. Попробуйте ещё раз.",
                reply_markup=_MAIN_KEYBOARD,
            )
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
    active_modes: dict[int, str] = {}
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
                    if command == "start" and args.lower().startswith("link_"):
                        result = await asyncio.to_thread(
                            telegram_storage.consume_link_challenge,
                            args[5:],
                            guild_id=_GUILD_ID,
                            telegram_user_id=int(user.get("id") or 0),
                            telegram_chat_id=chat_id,
                            telegram_username=str(user.get("username") or ""),
                            telegram_display_name=_telegram_display_name(user),
                        )
                        if result.get("ok"):
                            await _handle_menu(api, chat_id=chat_id, telegram_user=user)
                        else:
                            await api.send(
                                chat_id,
                                "Ссылка устарела или уже использована. Создайте новую в настройках Реактора.",
                                reply_markup=_MAIN_KEYBOARD,
                            )
                        continue
                    if command in {"start", "help"}:
                        await _handle_menu(api, chat_id=chat_id, telegram_user=user)
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
                            "invalid_code": "Код выглядит неверно. Возьмите новый код командой `/tg-link` в личных сообщениях T‑Mod.",
                            "invalid_or_expired_code": "Код недействителен или уже использован. Запросите новый командой `/tg-link` в личных сообщениях T‑Mod.",
                            "telegram_already_linked": "Этот Telegram-профиль уже привязан к другому T-Mod аккаунту.",
                        }
                        if result.get("ok"):
                            await _handle_menu(api, chat_id=chat_id, telegram_user=user)
                        else:
                            await api.send(
                                chat_id,
                                messages.get(str(result.get("error")), "Не удалось привязать аккаунт."),
                                reply_markup=_MAIN_KEYBOARD,
                            )
                        continue
                    if command in {"atlas", "atlas2"}:
                        if not args:
                            active_modes[chat_id] = "atlas2" if command == "atlas2" else "atlas"
                            await api.send(
                                chat_id,
                                "Режим Atlas 2 включён. Напишите вопрос одним или несколькими сообщениями.\n"
                                "Для выхода нажмите «Выйти из Atlas» или отправьте `/stop`."
                                if command == "atlas2"
                                else "Режим Atlas включён. Напишите вопрос одним или несколькими сообщениями.\n"
                                "Для выхода нажмите «Выйти из Atlas» или отправьте `/stop`.",
                                reply_markup=_MAIN_KEYBOARD,
                            )
                            continue
                        await _handle_atlas(
                            api,
                            chat_id=chat_id,
                            telegram_user=user,
                            question=args,
                            direct_mode=command == "atlas2",
                            locks=locks,
                        )
                        continue
                    if command in {"profile", "account"}:
                        await _handle_profile(api, chat_id=chat_id, telegram_user=user)
                        continue
                    if command in {"characters", "character", "chars"}:
                        await _handle_characters(api, chat_id=chat_id, telegram_user=user)
                        continue
                    if command in {"usage", "tokens", "balance"}:
                        await _handle_usage(api, chat_id=chat_id, telegram_user=user)
                        continue
                    if command in {"notifications", "notify"}:
                        await _handle_notifications(api, chat_id=chat_id, telegram_user=user)
                        continue
                    if command in {"read", "read_notifications"}:
                        await _handle_read_notifications(api, chat_id=chat_id, telegram_user=user)
                        continue
                    if command in {"newchat", "new_chat", "clear"}:
                        active_modes.pop(chat_id, None)
                        await _handle_new_chat(api, chat_id=chat_id, telegram_user=user)
                        continue
                    if command in {"settings", "connect"}:
                        await _handle_settings(api, chat_id=chat_id, telegram_user=user)
                        continue
                    if command in {"menu", "stop", "exit", "cancel"}:
                        active_modes.pop(chat_id, None)
                        await _handle_menu(api, chat_id=chat_id, telegram_user=user)
                        continue
                    await api.send(chat_id, "Неизвестная команда. Используйте `/help`.")
                    continue
                normalized = text.casefold()
                if normalized in {"🤖 atlas", "atlas", "атлас"}:
                    active_modes[chat_id] = "atlas"
                    await api.send(
                        chat_id,
                        "Режим Atlas включён. Напишите вопрос — я сохраню контекст этого диалога.",
                        reply_markup=_MAIN_KEYBOARD,
                    )
                    continue
                if normalized in {"👤 профиль", "профиль", "мой профиль"}:
                    await _handle_profile(api, chat_id=chat_id, telegram_user=user)
                    continue
                if normalized in {"🧩 персонажи", "персонажи", "мои персонажи"}:
                    await _handle_characters(api, chat_id=chat_id, telegram_user=user)
                    continue
                if normalized in {"💳 atlas token", "atlas token", "токены", "баланс"}:
                    await _handle_usage(api, chat_id=chat_id, telegram_user=user)
                    continue
                if normalized in {"🔔 уведомления", "уведомления"}:
                    await _handle_notifications(api, chat_id=chat_id, telegram_user=user)
                    continue
                if normalized in {"⚙ настройки", "настройки", "подключения"}:
                    await _handle_settings(api, chat_id=chat_id, telegram_user=user)
                    continue
                if normalized in {"⌂ главное меню", "главное меню", "меню", "назад"}:
                    active_modes.pop(chat_id, None)
                    await _handle_menu(api, chat_id=chat_id, telegram_user=user)
                    continue
                if normalized in {"❌ выйти из atlas", "выйти из atlas", "выйти"}:
                    active_modes.pop(chat_id, None)
                    await _handle_menu(api, chat_id=chat_id, telegram_user=user)
                    continue
                if normalized in {"🆕 новый диалог", "новый диалог", "новый чат"}:
                    active_modes.pop(chat_id, None)
                    await _handle_new_chat(api, chat_id=chat_id, telegram_user=user)
                    continue
                if chat_id in active_modes:
                    await _handle_atlas(
                        api,
                        chat_id=chat_id,
                        telegram_user=user,
                        question=text,
                        direct_mode=active_modes[chat_id] == "atlas2",
                        locks=locks,
                    )
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
                    continue
                    await api.send(
                        chat_id,
                        "Выберите раздел в меню ниже. Для свободного вопроса нажмите «🤖 Atlas».",
                        reply_markup=_MAIN_KEYBOARD,
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

    @bot.listen("on_message")
    async def mirror_member_dm_to_telegram(message: discord.Message) -> None:
        await _mirror_bot_dm_to_telegram(bot, message)

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

    @bot.tree.command(name="tg-link", description="Подключить Telegram к T-Mod в личных сообщениях")
    async def tg_link(interaction: discord.Interaction) -> None:
        """Create a Telegram link from a DM, including for non-senators."""

        if interaction.guild_id is not None:
            await interaction.response.send_message(
                "Откройте личные сообщения с T‑Mod и выполните `/tg-link` там.",
                ephemeral=True,
            )
            return
        if _GUILD_ID <= 0:
            await interaction.response.send_message(
                "Подключение временно недоступно: сервер T‑Mod ещё не настроен."
            )
            return
        challenge = await asyncio.to_thread(
            telegram_storage.create_link_challenge,
            _GUILD_ID,
            int(interaction.user.id),
        )
        code = str(challenge["code"])
        deep_link = f"https://t.me/{_BOT_USERNAME}?start=link_{code}" if _BOT_USERNAME else ""
        link_line = f"\n\nГотовая ссылка: {deep_link}" if deep_link else ""
        await interaction.response.send_message(
            "🔗 Подключение Telegram к T‑Mod\n\n"
            "Откройте ссылку ниже или найдите бота вручную, затем отправьте ему код.\n\n"
            f"Код: `{code}`"
            f"{link_line}\n\n"
            "Код одноразовый и действует 10 минут. Подключение не выдаёт"
            " дополнительных ролей или доступа к закрытым разделам."
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
            setattr(bot, "_tmod_telegram_api", api)
            try:
                await api.call("deleteWebhook", {"drop_pending_updates": False})
            except Exception as exc:
                print(f"Telegram webhook cleanup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            try:
                await api.call(
                    "setMyCommands",
                    {
                        "commands": [
                            {"command": "start", "description": "Открыть меню T-Mod"},
                            {"command": "atlas", "description": "Войти в режим общения с Atlas"},
                            {"command": "profile", "description": "Показать профиль T-Mod"},
                            {"command": "characters", "description": "Показать персонажей"},
                            {"command": "notifications", "description": "Показать уведомления"},
                            {"command": "read", "description": "Отметить уведомления прочитанными"},
                            {"command": "usage", "description": "Показать Atlas Token"},
                            {"command": "settings", "description": "Открыть настройки"},
                            {"command": "newchat", "description": "Начать новый диалог"},
                            {"command": "stop", "description": "Выйти из режима Atlas"},
                        ]
                    },
                )
            except Exception as exc:
                print(f"Telegram command menu setup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
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
