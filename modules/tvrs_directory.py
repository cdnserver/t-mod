from __future__ import annotations

import asyncio
import re
import traceback
from datetime import datetime, timezone
from typing import Any, Iterable

import discord
from discord.ext import commands

from modules.tvrs_config import (
    TVRS_COCHAIR_IDS,
    TVRS_DIRECTORY_CHANNEL_ID,
    TVRS_PERMANENT_CHAIR_ID,
    TVRS_SENATOR_ROLE_ID,
)
from persistence import tvrs_repository as _tvrs_storage

_DIRECTORY_LOCKS: dict[int, asyncio.Lock] = {}
_DIRECTORY_TASKS: dict[int, asyncio.Task[None]] = {}

RESPONSIBLE_TECHNICAL_SUPPORT = "technical_support"
RESPONSIBLE_BILL_MODERATION = "bill_moderation"
RESPONSIBLE_OVR_COMMUNICATIONS = "ovr_communications"
RESPONSIBLE_BUREAU_SECRETARIAT = "bureau_secretariat"

CHAIR_FIRST = "chair_1"
CHAIR_SECOND = "chair_2"
CHAIR_THIRD = "chair_3"

_DIRECTORY_MARKER = "> 🤖 Живой реестр T-Mod"
_DIRECTORY_HEADER = "# Ответственные лица экосистемы T-Mod"
_MENTION_RE = re.compile(r"<@!?(\d{1,24})>")

_RESPONSIBLE_SECTIONS: tuple[tuple[str, str, str], ...] = (
    (RESPONSIBLE_TECHNICAL_SUPPORT, "🛠️ Техническая поддержка", "Ответственный"),
    (RESPONSIBLE_BILL_MODERATION, "📜 Модерация законопроектов", "Ответственный"),
    (RESPONSIBLE_OVR_COMMUNICATIONS, "🛰️ Коммуникации ОВР", "Ответственный"),
    (RESPONSIBLE_BUREAU_SECRETARIAT, "🗂️ Секретариат SGL Bureau", "Ответственный"),
)

_CHAIR_SECTIONS: tuple[tuple[str, str, str], ...] = (
    (CHAIR_FIRST, "I", "Первый постоянный председатель Сената"),
    (CHAIR_SECOND, "II", "Второй председатель Сената"),
    (CHAIR_THIRD, "III", "Третий председатель Сената"),
)

_SLOT_ALIASES = {
    "technical_support": RESPONSIBLE_TECHNICAL_SUPPORT,
    "tech": RESPONSIBLE_TECHNICAL_SUPPORT,
    "support": RESPONSIBLE_TECHNICAL_SUPPORT,
    "bill_moderation": RESPONSIBLE_BILL_MODERATION,
    "law": RESPONSIBLE_BILL_MODERATION,
    "laws": RESPONSIBLE_BILL_MODERATION,
    "ovr_communications": RESPONSIBLE_OVR_COMMUNICATIONS,
    "ovr": RESPONSIBLE_OVR_COMMUNICATIONS,
    "communications": RESPONSIBLE_OVR_COMMUNICATIONS,
    "bureau_secretariat": RESPONSIBLE_BUREAU_SECRETARIAT,
    "secretariat": RESPONSIBLE_BUREAU_SECRETARIAT,
    "chair_1": CHAIR_FIRST,
    "chair1": CHAIR_FIRST,
    "chair_2": CHAIR_SECOND,
    "chair2": CHAIR_SECOND,
    "chair_3": CHAIR_THIRD,
    "chair3": CHAIR_THIRD,
}


def _normalize_member_id(value: Any) -> int | None:
    try:
        member_id = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    return member_id if member_id > 0 else None


def _default_chairs() -> dict[str, int | None]:
    permanent = _normalize_member_id(TVRS_PERMANENT_CHAIR_ID)
    candidates: list[int] = []
    for value in (TVRS_PERMANENT_CHAIR_ID, *TVRS_COCHAIR_IDS):
        member_id = _normalize_member_id(value)
        if member_id is not None and member_id not in candidates:
            candidates.append(member_id)
    first = permanent or (candidates[0] if candidates else None)
    remaining = [member_id for member_id in candidates if member_id != first]
    return {
        CHAIR_FIRST: first,
        CHAIR_SECOND: remaining[0] if len(remaining) >= 1 else None,
        CHAIR_THIRD: remaining[1] if len(remaining) >= 2 else None,
    }


def _state_defaults() -> dict[str, Any]:
    return {
        "responsibles": {slot: None for slot, _, _ in _RESPONSIBLE_SECTIONS},
        "chairs": _default_chairs(),
        "initialized": False,
        "imported_from_message_id": None,
        "updated_at": None,
        "updated_by": None,
        "revision": 0,
    }


def _normalize_state(raw: dict[str, Any] | None) -> dict[str, Any]:
    state = _state_defaults()
    if not isinstance(raw, dict):
        return state

    responsibles = raw.get("responsibles")
    if isinstance(responsibles, dict):
        for slot in state["responsibles"]:
            state["responsibles"][slot] = _normalize_member_id(responsibles.get(slot))

    chairs = raw.get("chairs")
    if isinstance(chairs, dict):
        for slot in state["chairs"]:
            if slot in chairs:
                state["chairs"][slot] = _normalize_member_id(chairs.get(slot))

    initialized = raw.get("initialized")
    state["initialized"] = (
        bool(initialized)
        if initialized is not None
        else any(key in raw for key in ("responsibles", "chairs", "revision"))
    )
    state["imported_from_message_id"] = _normalize_member_id(raw.get("imported_from_message_id"))
    state["updated_at"] = raw.get("updated_at")
    state["updated_by"] = _normalize_member_id(raw.get("updated_by"))
    try:
        state["revision"] = max(0, int(raw.get("revision") or 0))
    except (TypeError, ValueError, OverflowError):
        state["revision"] = 0
    return state


def _load_state(guild_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = _tvrs_storage.tvrs_get_directory_state(guild_id)
    return _normalize_state(raw), raw if isinstance(raw, dict) else {}


def _clean_slot(slot: str) -> str:
    raw = str(slot or "").strip().lower()
    return _SLOT_ALIASES.get(raw, raw)


def _slot_kind(slot: str) -> str | None:
    clean = _clean_slot(slot)
    if clean in {item[0] for item in _RESPONSIBLE_SECTIONS}:
        return "responsible"
    if clean in {item[0] for item in _CHAIR_SECTIONS}:
        return "chair"
    return None


def _lock_for(guild_id: int) -> asyncio.Lock:
    return _DIRECTORY_LOCKS.setdefault(int(guild_id), asyncio.Lock())


def _member_label(member_id: int | None) -> str:
    return f"<@{int(member_id)}>" if member_id is not None else "_не назначен_"


def _senator_members(guild: discord.Guild) -> list[discord.Member]:
    if TVRS_SENATOR_ROLE_ID <= 0:
        return []
    members = [
        member
        for member in guild.members
        if not getattr(member, "bot", False)
        and any(role.id == TVRS_SENATOR_ROLE_ID for role in getattr(member, "roles", ()))
    ]
    return sorted(members, key=lambda item: (item.display_name.casefold(), int(item.id)))


def _render_senator_lines(members: list[discord.Member]) -> list[str]:
    if not members:
        return ["> Пока нет участников с ролью сенатора."]
    lines = [f"> {member.mention}" for member in members[:24]]
    if len(members) > len(lines):
        lines.append(f"> … и ещё {len(members) - len(lines)} сенаторов")
    return lines


def _render_content(guild: discord.Guild, state: dict[str, Any]) -> str:
    senators = _senator_members(guild)
    lines: list[str] = [
        f"{_DIRECTORY_MARKER} · сенаторов: **{len(senators)}**",
        "> Состав Сената обновляется автоматически по роли.",
        "",
        _DIRECTORY_HEADER,
        "",
        "### 🛠️ Техническая поддержка",
        "",
        f"**Ответственный:** {_member_label(state['responsibles'].get(RESPONSIBLE_TECHNICAL_SUPPORT))}",
        "",
        "---",
        "",
        "# Ответственные лица сообщества Товарищества",
        "",
    ]
    for slot, heading, label in _RESPONSIBLE_SECTIONS[1:]:
        lines.extend(
            [
                f"### {heading}",
                "",
                f"**{label}:** {_member_label(state['responsibles'].get(slot))}",
                "",
            ]
        )
    lines.extend(
        [
            "---",
            "",
            "# 🏛️ Сенат сообщества Товарищества",
            "",
            "### Совет председателей",
            "",
        ]
    )
    for slot, ordinal, label in _CHAIR_SECTIONS:
        lines.append(f"**{ordinal}.** {_member_label(state['chairs'].get(slot))} — **{label}**")
    lines.extend(["", "### Состав Сената", "", *_render_senator_lines(senators), ""])
    if TVRS_SENATOR_ROLE_ID > 0:
        lines.append(
            f"> Сенаторы подтягиваются по роли <@&{TVRS_SENATOR_ROLE_ID}>. "
            "Для назначения ответственных используйте /tvrs_directory."
        )
    else:
        lines.append("> Для назначения ответственных используйте /tvrs_directory.")
    content = "\n".join(lines).strip()
    return content if len(content) <= 2000 else content[:1999].rstrip() + "…"


def directory_state_for(guild_id: int) -> dict[str, Any]:
    state, _ = _load_state(guild_id)
    return state


def directory_summary_text(guild: discord.Guild) -> str:
    state = directory_state_for(guild.id)
    lines = [f"**Живой реестр · ревизия {state['revision']}**"]
    for slot, heading, _ in _RESPONSIBLE_SECTIONS:
        lines.append(f"• {heading}: {_member_label(state['responsibles'].get(slot))}")
    for slot, _, label in _CHAIR_SECTIONS:
        lines.append(f"• {label}: {_member_label(state['chairs'].get(slot))}")
    source_id = state.get("imported_from_message_id")
    if source_id:
        lines.append(f"• Исходное сообщение: {source_id}")
    return "\n".join(lines)


def _normalize_member_ids(values: Iterable[Any]) -> list[int | None]:
    return [_normalize_member_id(value) for value in values]


def directory_is_relevant_member(
    guild_id: int,
    member_id: int,
    roles: Iterable[int] | None = None,
) -> bool:
    if roles is not None:
        try:
            if TVRS_SENATOR_ROLE_ID in {int(role_id) for role_id in roles}:
                return True
        except (TypeError, ValueError, OverflowError):
            pass
    state = directory_state_for(guild_id)
    tracked = {
        item
        for item in (
            *_normalize_member_ids(state["responsibles"].values()),
            *_normalize_member_ids(state["chairs"].values()),
        )
        if item is not None
    }
    return int(member_id) in tracked


async def get_directory_channel(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild | None = None,
) -> discord.TextChannel | None:
    if TVRS_DIRECTORY_CHANNEL_ID <= 0:
        return None
    channel = guild.get_channel(TVRS_DIRECTORY_CHANNEL_ID) if guild is not None else None
    if channel is None and hasattr(bot, "get_channel"):
        channel = bot.get_channel(TVRS_DIRECTORY_CHANNEL_ID)  # type: ignore[attr-defined]
    if channel is None and hasattr(bot, "fetch_channel"):
        try:
            fetched = await bot.fetch_channel(TVRS_DIRECTORY_CHANNEL_ID)  # type: ignore[attr-defined]
            channel = fetched if isinstance(fetched, discord.TextChannel) else None
        except discord.DiscordException:
            channel = None
    if not isinstance(channel, discord.TextChannel):
        return None
    channel_guild = getattr(channel, "guild", None)
    if guild is not None:
        channel_guild_id = _normalize_member_id(getattr(channel_guild, "id", None))
        if channel_guild_id != int(guild.id):
            return None
    return channel


async def _ensure_member_cache(guild: discord.Guild) -> None:
    if getattr(guild, "chunked", True):
        return
    try:
        await guild.chunk(cache=True)
    except discord.DiscordException:
        pass


def _first_mention(value: str) -> int | None:
    match = _MENTION_RE.search(value)
    return _normalize_member_id(match.group(1)) if match else None


def _section_body(content: str, title: str) -> str:
    folded = content.casefold()
    start = folded.find(title.casefold())
    if start < 0:
        return ""
    value = content[start + len(title) :]
    boundary = re.search(r"\n#{1,3}\s", value)
    return value[: boundary.start()] if boundary else value


def _assignments_from_manual_content(content: str) -> dict[str, int]:
    assignments: dict[str, int] = {}
    for slot, heading, _ in _RESPONSIBLE_SECTIONS:
        member_id = _first_mention(_section_body(content, heading))
        if member_id is not None:
            assignments[slot] = member_id
    for slot, _, label in _CHAIR_SECTIONS:
        for line in content.splitlines():
            if label.casefold() in line.casefold():
                member_id = _first_mention(line)
                if member_id is not None:
                    assignments[slot] = member_id
                break
    return assignments


async def _find_manual_directory_message(
    channel: discord.TextChannel,
) -> tuple[Any | None, dict[str, int]]:
    if not hasattr(channel, "history"):
        return None, {}
    try:
        async for candidate in channel.history(limit=80):
            author = getattr(candidate, "author", None)
            if getattr(author, "bot", False):
                continue
            content = str(getattr(candidate, "content", "") or "")
            if _DIRECTORY_HEADER not in content:
                continue
            assignments = _assignments_from_manual_content(content)
            if assignments:
                return candidate, assignments
    except discord.DiscordException:
        pass
    return None, {}


async def _initialize_from_manual_message(
    channel: discord.TextChannel,
    guild_id: int,
    state: dict[str, Any],
    *,
    force: bool,
) -> tuple[dict[str, Any], int | None]:
    if state["initialized"] and not force:
        return state, None
    source, assignments = await _find_manual_directory_message(channel)
    if not assignments:
        if not state["initialized"] and not force:
            state["initialized"] = True
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(_tvrs_storage.tvrs_set_directory_state, guild_id, state)
        return state, None
    for slot, member_id in assignments.items():
        bucket = state["responsibles"] if _slot_kind(slot) == "responsible" else state["chairs"]
        bucket[slot] = member_id
    state["initialized"] = True
    state["imported_from_message_id"] = _normalize_member_id(getattr(source, "id", None))
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["revision"] = int(state.get("revision") or 0) + 1
    await asyncio.to_thread(_tvrs_storage.tvrs_set_directory_state, guild_id, state)
    return state, state["imported_from_message_id"]


def _is_owned_by_bot(message: Any, bot: commands.Bot | discord.Client) -> bool:
    bot_user = getattr(bot, "user", None)
    bot_user_id = _normalize_member_id(getattr(bot_user, "id", None))
    if bot_user_id is None:
        return False
    author = getattr(message, "author", None)
    author_id = _normalize_member_id(getattr(author, "id", None))
    return author is None or author_id == bot_user_id


async def _find_known_directory_message(
    channel: discord.TextChannel,
    bot_user_id: int,
) -> Any | None:
    if not hasattr(channel, "history"):
        return None
    try:
        async for candidate in channel.history(limit=80):
            if getattr(candidate, "author", None) and int(candidate.author.id) == int(bot_user_id):
                content = str(getattr(candidate, "content", "") or "")
                if content.startswith(_DIRECTORY_MARKER) or content.startswith("> Автосинхронизация активна"):
                    return candidate
    except discord.DiscordException:
        return None
    return None


async def ensure_directory_message(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
) -> discord.Message | None:
    channel = await get_directory_channel(bot, guild)
    if channel is None:
        return None
    lock = _lock_for(guild.id)
    async with lock:
        await _ensure_member_cache(guild)
        state, _ = await asyncio.to_thread(_load_state, guild.id)
        state, _ = await _initialize_from_manual_message(channel, guild.id, state, force=False)
        content = _render_content(guild, state)
        message_id = await asyncio.to_thread(
            _tvrs_storage.tvrs_get_directory_message_id,
            guild.id,
        )
        message = None
        if message_id and hasattr(channel, "fetch_message"):
            try:
                candidate = await channel.fetch_message(message_id)
                message = candidate if _is_owned_by_bot(candidate, bot) else None
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            bot_user = getattr(bot, "user", None)
            bot_user_id = _normalize_member_id(getattr(bot_user, "id", None))
            if bot_user_id is not None and hasattr(channel, "history"):
                message = await _find_known_directory_message(channel, bot_user_id)
        try:
            if message is None:
                message = await channel.send(
                    content=content,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            elif getattr(message, "content", None) != content:
                await message.edit(
                    content=content,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except (discord.DiscordException, OSError):
            return None
        await asyncio.to_thread(
            _tvrs_storage.tvrs_set_directory_message_id,
            guild.id,
            int(message.id),
        )
        return message


async def adopt_directory_from_channel(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
) -> int | None:
    channel = await get_directory_channel(bot, guild)
    if channel is None:
        return None
    async with _lock_for(guild.id):
        state, _ = await asyncio.to_thread(_load_state, guild.id)
        _, source_id = await _initialize_from_manual_message(
            channel,
            guild.id,
            state,
            force=True,
        )
    return source_id


def schedule_directory_refresh(bot: commands.Bot, guild: discord.Guild) -> None:
    key = int(guild.id)
    task = _DIRECTORY_TASKS.get(key)
    if task and not task.done():
        task.cancel()

    async def runner() -> None:
        try:
            await asyncio.sleep(1.5)
            await ensure_directory_message(bot, guild)
        except asyncio.CancelledError:
            return
        except Exception:
            traceback.print_exc()

    _DIRECTORY_TASKS[key] = asyncio.create_task(runner())


async def ensure_directory_all(bot: commands.Bot | discord.Client) -> None:
    for guild in getattr(bot, "guilds", []):
        try:
            await ensure_directory_message(bot, guild)
        except Exception:
            traceback.print_exc()


async def tvrs_ensure_directory_all(bot: commands.Bot | discord.Client) -> None:
    await ensure_directory_all(bot)


def update_directory_state(
    guild_id: int,
    *,
    slot: str,
    member_id: int | None,
    actor_id: int | None = None,
) -> dict[str, Any]:
    clean_slot = _clean_slot(slot)
    kind = _slot_kind(clean_slot)
    if kind is None:
        raise ValueError(f"unknown_directory_slot:{slot}")
    state = directory_state_for(guild_id)
    bucket = state["responsibles"] if kind == "responsible" else state["chairs"]
    bucket[clean_slot] = _normalize_member_id(member_id)
    state["initialized"] = True
    state["updated_by"] = _normalize_member_id(actor_id)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["revision"] = int(state.get("revision") or 0) + 1
    return _tvrs_storage.tvrs_set_directory_state(guild_id, state)


def clear_directory_slot(
    guild_id: int,
    *,
    slot: str,
    actor_id: int | None = None,
) -> dict[str, Any]:
    return update_directory_state(guild_id, slot=slot, member_id=None, actor_id=actor_id)


def reset_directory_state(
    guild_id: int,
    *,
    actor_id: int | None = None,
) -> dict[str, Any]:
    state = _state_defaults()
    state["initialized"] = True
    state["updated_by"] = _normalize_member_id(actor_id)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["revision"] = 1
    return _tvrs_storage.tvrs_set_directory_state(guild_id, state)


__all__ = [
    "adopt_directory_from_channel",
    "clear_directory_slot",
    "directory_is_relevant_member",
    "directory_state_for",
    "directory_summary_text",
    "ensure_directory_all",
    "ensure_directory_message",
    "get_directory_channel",
    "reset_directory_state",
    "schedule_directory_refresh",
    "tvrs_ensure_directory_all",
    "update_directory_state",
]
