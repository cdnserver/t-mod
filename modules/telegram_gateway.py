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
_REACTOR_CONNECTION_URL = "https://tvr.lat/login?next=%2Freactor"
_MAIN_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🤖 Atlas", "callback_data": "menu:atlas"},
            {"text": "👤 Профиль", "callback_data": "menu:profile"},
        ],
        [
            {"text": "🗂 История", "callback_data": "menu:history"},
            {"text": "🆕 Новый диалог", "callback_data": "menu:newchat"},
        ],
        [
            {"text": "🧩 Персонажи", "callback_data": "menu:characters"},
            {"text": "💳 Atlas Token", "callback_data": "menu:usage"},
        ],
        [
            {"text": "🔔 Уведомления", "callback_data": "menu:notifications"},
            {"text": "⚙ Настройки", "callback_data": "menu:settings"},
        ],
        [
            {"text": "🔐 Код входа в T‑Mod", "callback_data": "menu:loginpin"},
            {"text": "⌂ Главное меню", "callback_data": "menu:home"},
        ],
    ],
}
_CHAT_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🗂 История", "callback_data": "menu:history"},
            {"text": "🆕 Новый диалог", "callback_data": "menu:newchat"},
        ],
        [
            {"text": "⌂ Меню", "callback_data": "menu:home"},
            {"text": "❌ Выйти из Atlas", "callback_data": "menu:stop"},
        ],
    ]
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
        self._background_tasks: set[asyncio.Task[Any]] = set()

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

    async def send_temporary(
        self,
        chat_id: int,
        text: str,
        *,
        ttl_seconds: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Send a sensitive one-time code and remove its message after expiry."""

        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "text": str(text)[:_MAX_TELEGRAM_TEXT],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = await self.call("sendMessage", payload)
        message_id = int(result.get("message_id") or 0)
        if message_id <= 0:
            return

        async def expire() -> None:
            try:
                await asyncio.sleep(max(60, min(600, int(ttl_seconds))))
                await self.call(
                    "deleteMessage",
                    {"chat_id": int(chat_id), "message_id": message_id},
                )
            except (asyncio.CancelledError, Exception):
                return

        task = asyncio.create_task(expire())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def cancel_background_tasks(self) -> None:
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

    async def typing(self, chat_id: int) -> None:
        await self.call("sendChatAction", {"chat_id": int(chat_id), "action": "typing"})

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        payload: dict[str, Any] = {"callback_query_id": str(callback_id)}
        if text:
            payload["text"] = str(text)[:180]
        await self.call("answerCallbackQuery", payload)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self.call(
            "deleteMessage",
            {"chat_id": int(chat_id), "message_id": int(message_id)},
        )

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "text": str(text)[:_MAX_TELEGRAM_TEXT],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self.call("editMessageText", payload)


class _TelegramPanelApi:
    """Edit the clicked dashboard message instead of stacking new menu posts."""

    _EDIT_FALLBACK_ERRORS = (
        "message is not modified",
        "message can't be edited",
        "message to edit not found",
        "message_id_invalid",
    )

    def __init__(self, api: _TelegramApi, chat_id: int, message_id: int) -> None:
        self._api = api
        self._chat_id = int(chat_id)
        self._message_id = int(message_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._api, name)

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        if int(chat_id) != self._chat_id or self._message_id <= 0:
            await self._api.send(chat_id, text, reply_markup=reply_markup)
            return
        parts = _chunks(text)
        try:
            await self._api.edit_message_text(
                self._chat_id,
                self._message_id,
                parts[0],
                reply_markup=reply_markup,
            )
        except RuntimeError as exc:
            message = str(exc).casefold()
            if "message is not modified" in message:
                return
            if not any(error in message for error in self._EDIT_FALLBACK_ERRORS):
                raise
            try:
                await self._api.delete_message(self._chat_id, self._message_id)
            except Exception:
                pass
            await self._api.send(self._chat_id, text, reply_markup=reply_markup)
            return
        for part in parts[1:]:
            await self._api.send(self._chat_id, part)


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
    notification_keyboard = {
        "inline_keyboard": [
            *[list(row) for row in _MAIN_KEYBOARD["inline_keyboard"]],
            [{"text": "✅ Отметить прочитанными", "callback_data": "menu:read"}],
        ]
    }
    await api.send(chat_id, text, reply_markup=notification_keyboard)


async def _handle_settings(api: _TelegramApi, *, chat_id: int, telegram_user: dict[str, Any]) -> None:
    link = await asyncio.to_thread(
        telegram_storage.get_link_by_telegram,
        _GUILD_ID,
        int(telegram_user.get("id") or 0),
    )
    status = "подключён" if link else "не подключён"
    settings_keyboard = {
        "inline_keyboard": [
            [{"text": "🔐 Получить одноразовый код входа", "callback_data": "menu:loginpin"}],
            [{"text": "🔗 Управлять подключением", "url": _REACTOR_CONNECTION_URL}],
            [{"text": "⌂ Главное меню", "callback_data": "menu:home"}],
        ]
    }
    if not link:
        settings_keyboard["inline_keyboard"].pop(0)
    await api.send(
        chat_id,
        "⚙ Настройки\n\n"
        f"Telegram ↔ T‑Mod: {status}\n"
        "Пароль T‑Mod не передаётся боту. Подключение можно проверить или изменить в личном Реакторе.\n\n"
        "Atlas отвечает в режиме диалога после нажатия кнопки «🤖 Atlas».",
        reply_markup=settings_keyboard,
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
            "Добро пожаловать в T‑Mod — личный центр уведомлений и Atlas.\n\n"
            "Чтобы открыть профиль и безопасный вход по одноразовому коду, сначала свяжите Telegram "
            "с аккаунтом T‑Mod в личном Реакторе. Пароль боту не нужен."
        )
        keyboard = {
            "inline_keyboard": [
                [{"text": "🔗 Подключить Telegram", "url": _REACTOR_CONNECTION_URL}],
                [{"text": "↻ Проверить подключение", "callback_data": "menu:home"}],
            ]
        }
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
        keyboard = _MAIN_KEYBOARD
    await api.send(chat_id, body, reply_markup=keyboard)


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


async def _list_atlas_threads(owner_id: int) -> list[dict[str, Any]]:
    spaces = await asyncio.to_thread(atlas_storage.atlas_user_spaces, _GUILD_ID, int(owner_id))
    current_ids = {
        int(space["id"]): str(space.get("display_name") or space.get("name") or "Пространство")
        for space in spaces
    }
    threads: list[dict[str, Any]] = []
    for organization_id, space_name in current_ids.items():
        rows = await asyncio.to_thread(
            atlas_storage.atlas_threads,
            organization_id,
            int(owner_id),
            limit=200,
        )
        threads.extend({**row, "_space_name": space_name} for row in rows)
    return sorted(
        threads,
        key=lambda row: (str(row.get("updated_at") or ""), int(row.get("id") or 0)),
        reverse=True,
    )[:40]


async def _handle_atlas_history(
    api: _TelegramApi,
    *,
    chat_id: int,
    telegram_user: dict[str, Any],
    offset: int = 0,
) -> None:
    link = await asyncio.to_thread(
        telegram_storage.get_link_by_telegram,
        _GUILD_ID,
        int(telegram_user.get("id") or 0),
    )
    if link is None:
        await api.send(chat_id, "Сначала подключите T‑Mod аккаунт через /tg-link в Discord.", reply_markup=_MAIN_KEYBOARD)
        return
    owner_id = int(link["discord_user_id"])
    if not await _access_allowed(_GUILD_ID, owner_id):
        await api.send(chat_id, "Для этого аккаунта Atlas сейчас недоступен.", reply_markup=_MAIN_KEYBOARD)
        return
    threads = await _list_atlas_threads(owner_id)
    if not threads:
        await api.send(
            chat_id,
            "🗂 История Atlas\n\nПока нет сохранённых диалогов. Нажмите «🤖 Atlas», чтобы начать.",
            reply_markup=_MAIN_KEYBOARD,
        )
        return

    page_size = 6
    page = max(0, min(int(offset), max(0, len(threads) - 1))) // page_size
    start = page * page_size
    visible = threads[start : start + page_size]
    active = await asyncio.to_thread(
        telegram_storage.get_atlas_thread,
        _GUILD_ID,
        owner_id,
        int(chat_id),
    )
    lines = [f"🗂 История Atlas · {start + 1}–{start + len(visible)} из {len(threads)}", ""]
    keyboard: list[list[dict[str, str]]] = []
    for item in visible:
        thread_id = int(item["id"])
        title = " ".join(str(item.get("title") or "Новый диалог").split())[:64]
        count = int(item.get("message_count") or 0)
        is_active = active is not None and int(active.get("atlas_thread_id") or 0) == thread_id
        stamp = str(item.get("updated_at") or "")[:16].replace("T", " ")
        preview = " ".join(str(item.get("preview") or "").split())[:100]
        lines.append(
            f"{'▶ ' if is_active else ''}{title}\n"
            f"{item.get('_space_name') or 'Atlas'} · {count} сообщ. · {stamp}"
            + (f"\n{preview}" if preview else "")
        )
        keyboard.append([{
            "text": f"{'▶ ' if is_active else '↪ '}{title[:52]}",
            "callback_data": f"atlas_thread:{thread_id}",
        }])
    nav: list[dict[str, str]] = []
    if page > 0:
        nav.append({"text": "‹ Новее", "callback_data": f"atlas_history:{(page - 1) * page_size}"})
    if start + page_size < len(threads):
        nav.append({"text": "Старее ›", "callback_data": f"atlas_history:{(page + 1) * page_size}"})
    if nav:
        keyboard.append(nav)
    keyboard.append([
        {"text": "🤖 Atlas", "callback_data": "menu:atlas"},
        {"text": "⌂ Меню", "callback_data": "menu:home"},
    ])
    await api.send(
        chat_id,
        "\n\n".join(lines),
        reply_markup={"inline_keyboard": keyboard},
    )


async def _resume_atlas_thread(
    api: _TelegramApi,
    *,
    chat_id: int,
    telegram_user: dict[str, Any],
    thread_id: int,
    active_modes: dict[int, str],
) -> None:
    link = await asyncio.to_thread(
        telegram_storage.get_link_by_telegram,
        _GUILD_ID,
        int(telegram_user.get("id") or 0),
    )
    if link is None:
        await api.send(chat_id, "Сначала подключите T‑Mod аккаунт через /tg-link в Discord.", reply_markup=_MAIN_KEYBOARD)
        return
    owner_id = int(link["discord_user_id"])
    if not await _access_allowed(_GUILD_ID, owner_id):
        await api.send(chat_id, "Для этого аккаунта Atlas сейчас недоступен.", reply_markup=_MAIN_KEYBOARD)
        return
    spaces = await asyncio.to_thread(atlas_storage.atlas_user_spaces, _GUILD_ID, owner_id)
    selected: dict[str, Any] | None = None
    for space in spaces:
        organization_id = int(space["id"])
        try:
            selected = await asyncio.to_thread(
                atlas_storage.atlas_thread_messages,
                organization_id,
                owner_id,
                int(thread_id),
                limit=3,
            )
            selected["organization_id"] = organization_id
            break
        except ValueError as exc:
            if str(exc) != "atlas_thread_not_found":
                raise
    if selected is None:
        await api.send(chat_id, "Этот диалог не найден или больше недоступен.", reply_markup=_MAIN_KEYBOARD)
        return

    thread = dict(selected["thread"])
    organization_id = int(selected["organization_id"])
    await asyncio.to_thread(
        telegram_storage.save_atlas_thread,
        _GUILD_ID,
        owner_id,
        int(chat_id),
        organization_id,
        int(thread_id),
    )
    active_modes[int(chat_id)] = "atlas"
    messages = list(selected.get("messages") or [])
    latest = " ".join(str(messages[-1].get("content_text") or "").split())[:300] if messages else ""
    await api.send(
        chat_id,
        f"↪ Продолжаем диалог «{str(thread.get('title') or 'Новый диалог')[:100]}».\n"
        "Контекст переписки восстановлен. Напишите следующее сообщение."
        + (f"\n\nПоследнее в чате: {latest}" if latest else ""),
        reply_markup=_CHAT_KEYBOARD,
    )


async def _handle_login_code(
    api: _TelegramApi,
    *,
    chat_id: int,
    telegram_user: dict[str, Any],
) -> None:
    """Send a short-lived T-Mod web sign-in code to the verified Telegram link."""

    telegram_user_id = int(telegram_user.get("id") or 0)
    link = await asyncio.to_thread(
        telegram_storage.get_link_by_telegram,
        _GUILD_ID,
        telegram_user_id,
    )
    if link is None:
        await api.send(
            chat_id,
            "Сначала подключите Telegram в личном Реакторе → Подключения. Без привязанного аккаунта код не выдаётся.",
            reply_markup=_MAIN_KEYBOARD,
        )
        return
    discord_user_id = int(link["discord_user_id"])
    credential = await asyncio.to_thread(
        auth_storage.get_web_credential,
        _GUILD_ID,
        discord_user_id,
    )
    if credential is None:
        await api.send(
            chat_id,
            "Для аккаунта ещё не настроен веб-вход. Сначала создайте логин в T‑Mod через /account.",
            reply_markup=_MAIN_KEYBOARD,
        )
        return
    if await asyncio.to_thread(
        ban_storage.is_globally_banned,
        _GUILD_ID,
        discord_user_id,
    ):
        await api.send(chat_id, "Вход в экосистему T‑Mod сейчас недоступен.", reply_markup=_MAIN_KEYBOARD)
        return
    try:
        challenge = await asyncio.to_thread(
            telegram_storage.create_telegram_login_code,
            _GUILD_ID,
            discord_user_id,
            telegram_user_id,
        )
    except ValueError as exc:
        if str(exc) == "telegram_login_code_rate_limited":
            await api.send(
                chat_id,
                "🔐 Код уже выпускался недавно. Подождите полминуты — так мы защищаем вход от частого перевыпуска.",
                reply_markup=_MAIN_KEYBOARD,
            )
            return
        if str(exc) != "telegram_account_not_linked":
            raise
        await api.send(chat_id, "Привязка изменилась. Подключите Telegram заново в личном Реакторе.", reply_markup=_MAIN_KEYBOARD)
        return
    await api.send_temporary(
        chat_id,
        "🔐 Одноразовый код входа в T‑Mod\n\n"
        f"Код: {challenge['code']}\n\n"
        "Введите его на странице входа в поле «Войти по Telegram». Код действует 5 минут, "
        "подходит только один раз и заменяет предыдущий код. Сообщение удалится автоматически. Никому не пересылайте код.",
        ttl_seconds=int(challenge.get("ttl_seconds") or 300),
        reply_markup={
            "inline_keyboard": [
                [{"text": "Открыть форму входа", "url": "https://tvr.lat/login?next=%2Freactor#telegram-login"}],
                [{"text": "📋 Скопировать код", "copy_text": {"text": str(challenge["code"])}}],
                [{"text": "↻ Выпустить новый код", "callback_data": "menu:loginpin"}],
                [{"text": "⌂ Главное меню", "callback_data": "menu:home"}],
            ]
        },
    )
    emit_global_event({
        "event_type": "auth.telegram_login_code.issued",
        "summary": "Выдан временный код входа T‑Mod в привязанный Telegram",
        "guild_id": _GUILD_ID,
        "actor_user_id": discord_user_id,
        "details": {"telegram_user_id": telegram_user_id, "ttl_seconds": int(challenge.get("ttl_seconds") or 300)},
    })


async def _handle_callback(
    api: _TelegramApi,
    callback: dict[str, Any],
    *,
    active_modes: dict[int, str],
) -> None:
    callback_id = str(callback.get("id") or "")
    if callback_id:
        try:
            await api.answer_callback(
                callback_id,
                "Готовлю одноразовый код…" if callback.get("data") == "menu:loginpin" else "",
            )
        except Exception:
            # Old inline keyboards may outlive Telegram's callback window.
            pass
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    telegram_user = callback.get("from") or {}
    chat_id = int(chat.get("id") or 0)
    if chat_id <= 0 or str(chat.get("type") or "") != "private":
        return
    message_id = int(message.get("message_id") or 0)
    panel_api = _TelegramPanelApi(api, chat_id, message_id)
    data = str(callback.get("data") or "")
    if data != "menu:atlas" and not data.startswith("atlas_thread:"):
        active_modes.pop(chat_id, None)
    if data == "menu:home":
        await _handle_menu(panel_api, chat_id=chat_id, telegram_user=telegram_user)
    elif data == "menu:atlas":
        active_modes[chat_id] = "atlas"
        await panel_api.send(chat_id, "🤖 Atlas готов. Напишите вопрос — контекст этой переписки сохранится.", reply_markup=_CHAT_KEYBOARD)
    elif data == "menu:profile":
        await _handle_profile(panel_api, chat_id=chat_id, telegram_user=telegram_user)
    elif data == "menu:history":
        await _handle_atlas_history(panel_api, chat_id=chat_id, telegram_user=telegram_user)
    elif data.startswith("atlas_history:"):
        try:
            offset = max(0, min(240, int(data.partition(":")[2])))
        except ValueError:
            offset = 0
        await _handle_atlas_history(panel_api, chat_id=chat_id, telegram_user=telegram_user, offset=offset)
    elif data.startswith("atlas_thread:"):
        try:
            thread_id = int(data.partition(":")[2])
        except ValueError:
            thread_id = 0
        if thread_id > 0:
            await _resume_atlas_thread(
                panel_api,
                chat_id=chat_id,
                telegram_user=telegram_user,
                thread_id=thread_id,
                active_modes=active_modes,
            )
    elif data == "menu:newchat":
        await _handle_new_chat(panel_api, chat_id=chat_id, telegram_user=telegram_user)
    elif data == "menu:characters":
        await _handle_characters(panel_api, chat_id=chat_id, telegram_user=telegram_user)
    elif data == "menu:usage":
        await _handle_usage(panel_api, chat_id=chat_id, telegram_user=telegram_user)
    elif data == "menu:notifications":
        await _handle_notifications(panel_api, chat_id=chat_id, telegram_user=telegram_user)
    elif data == "menu:read":
        await _handle_read_notifications(panel_api, chat_id=chat_id, telegram_user=telegram_user)
    elif data == "menu:settings":
        await _handle_settings(panel_api, chat_id=chat_id, telegram_user=telegram_user)
    elif data == "menu:loginpin":
        await _handle_login_code(api, chat_id=chat_id, telegram_user=telegram_user)
    elif data == "menu:stop":
        active_modes.pop(chat_id, None)
        await _handle_menu(api, chat_id=chat_id, telegram_user=telegram_user)


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
                reply_markup=_CHAT_KEYBOARD,
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
                reply_markup=_CHAT_KEYBOARD,
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
                reply_markup=_CHAT_KEYBOARD,
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
                {"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]},
            )
            for update in updates if isinstance(updates, list) else []:
                offset = max(offset, int(update.get("update_id") or 0) + 1)
                callback = update.get("callback_query")
                if isinstance(callback, dict):
                    await _handle_callback(api, callback, active_modes=active_modes)
                    continue
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
                                reply_markup=_CHAT_KEYBOARD,
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
                    if command in {"history", "chats"}:
                        await _handle_atlas_history(api, chat_id=chat_id, telegram_user=user)
                        continue
                    if command in {"loginpin", "login_code"}:
                        await _handle_login_code(api, chat_id=chat_id, telegram_user=user)
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
                        reply_markup=_CHAT_KEYBOARD,
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
                if normalized in {"🗂 история", "история atlas", "история"}:
                    await _handle_atlas_history(api, chat_id=chat_id, telegram_user=user)
                    continue
                if normalized in {"🔐 код входа в t‑mod", "код входа", "временный пин"}:
                    await _handle_login_code(api, chat_id=chat_id, telegram_user=user)
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
                    "Выберите раздел кнопками ниже или нажмите «🤖 Atlas», чтобы начать диалог.",
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
                            {"command": "account", "description": "Показать аккаунт T-Mod"},
                            {"command": "characters", "description": "Показать персонажей"},
                            {"command": "notifications", "description": "Показать уведомления"},
                            {"command": "history", "description": "Открыть историю Atlas"},
                            {"command": "read", "description": "Отметить уведомления прочитанными"},
                            {"command": "usage", "description": "Показать Atlas Token"},
                            {"command": "settings", "description": "Открыть настройки"},
                            {"command": "loginpin", "description": "Получить временный код входа T-Mod"},
                            {"command": "newchat", "description": "Начать новый диалог"},
                            {"command": "stop", "description": "Выйти из режима Atlas"},
                        ]
                    },
                )
            except Exception as exc:
                print(f"Telegram command menu setup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            print("Telegram gateway: polling enabled", flush=True)
            try:
                await _poll(api, stop=stop)
            finally:
                await api.cancel_background_tasks()

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
