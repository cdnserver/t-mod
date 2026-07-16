import asyncio
import json
import os
import re
import traceback
import uuid
from datetime import datetime, timezone, timedelta
from typing import Callable, Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

import storage
from localization import safe_command_description, safe_command_name
from modules.consensus_core import (
    LiveConsensusSession,
    LiveParticipant,
    LiveResult,
    calculate_consensus as calculate_consensus_v2,
    clean_stage_name as clean_consensus_stage_name,
    session_from_snapshot,
    session_to_snapshot,
    transition_session,
)
from modules.control_center import log_technical_event
from modules.operations import ACTIVE_TASKS_CHANNEL_ID, wake_operations_worker


def env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def env_color(name: str, default: str = "0xD9D9D9") -> int:
    raw = os.getenv(name, default).strip()
    try:
        return int(raw, 16) if raw.lower().startswith("0x") else int(raw)
    except (TypeError, ValueError):
        return 0xD9D9D9


TVRS_MATERIALS_CHANNEL_ID = env_int("TVRS_MATERIALS_CHANNEL_ID", 1492583702641774642)
TVRS_BILLS_CHANNEL_ID = env_int("TVRS_BILLS_CHANNEL_ID", 1492471371085643937)
TVRS_CONSENSUS_VOICE_CHANNEL_ID = env_int("TVRS_CONSENSUS_VOICE_CHANNEL_ID", 1519419533667078145)
TVRS_SENATOR_ROLE_ID = env_int("TVRS_SENATOR_ROLE_ID", 1500563715622174881)
TVRS_PERMANENT_CHAIR_ID = env_int("TVRS_PERMANENT_CHAIR_ID", 811862068214890537)
TVRS_CHAIR_ROLE_ID = env_int("TVRS_CHAIR_ROLE_ID", 1488207163879985233)
TVRS_DEFAULT_NEXT_BILL_NUMBER = env_int("TVRS_DEFAULT_NEXT_BILL_NUMBER", 9)
TVRS_DEFAULT_NEXT_PLENARY_NUMBER = env_int("TVRS_DEFAULT_NEXT_PLENARY_NUMBER", 4)
TVRS_EMBED_COLOR = env_color("TVRS_EMBED_COLOR", "0xD9D9D9")
TVRS_STICKY_DEBOUNCE_SECONDS = max(1, env_int("TVRS_STICKY_DEBOUNCE_SECONDS", 2))
TVRS_DISCUSSION_CATEGORY_ID = env_int("TVRS_DISCUSSION_CATEGORY_ID", 1496802341377020067)
TVRS_TIMER_OPTIONS: list[tuple[str, int]] = [("30 сек", 30), ("1 мин", 60), ("3 мин", 180), ("5 мин", 300)]
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "Europe/Riga"))

TVRS_COMMAND_NAME = safe_command_name("tvrs.commands.tvrs_name", "tvrs")
TVRS_COMMAND_DESCRIPTION = safe_command_description("tvrs.commands.tvrs_description", "Открыть Универсалитет Товарищества")
TVRS_SETBILL_COMMAND_NAME = safe_command_name("tvrs.commands.setbill_name", "tvrs_setbill")
TVRS_SETBILL_COMMAND_DESCRIPTION = safe_command_description("tvrs.commands.setbill_description", "Изменить последний принятый законопроект")
TVRS_STICKY_COMMAND_NAME = safe_command_name("tvrs.commands.sticky_name", "tvrs_sticky")
TVRS_STICKY_COMMAND_DESCRIPTION = safe_command_description("tvrs.commands.sticky_description", "Обновить сообщение подачи законопроектов")
TVRS_ADMIN_COMMAND_NAME = safe_command_name("tvrs.commands.admin_name", "tvrs_admin")
TVRS_ADMIN_COMMAND_DESCRIPTION = safe_command_description("tvrs.commands.admin_description", "Администрирование законопроектов и консенсусов")

_sticky_locks: dict[int, asyncio.Lock] = {}
_sticky_tasks: dict[int, asyncio.Task] = {}
_active_sessions: dict[int, "LiveConsensusSession"] = {}
_session_locks: dict[int, asyncio.Lock] = {}
_restored_consensus_guilds: set[int] = set()


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(LOCAL_TZ)


def format_bill_number(number: int) -> str:
    return f"{int(number):03d}"


def format_dt(dt: datetime | None = None) -> str:
    return (dt or now_local()).strftime("%d.%m.%Y %H:%M")


def ru_ordinal(number: int) -> str:
    words = {
        1: "первый", 2: "второй", 3: "третий", 4: "четвертый", 5: "пятый",
        6: "шестой", 7: "седьмой", 8: "восьмой", 9: "девятый", 10: "десятый",
        11: "одиннадцатый", 12: "двенадцатый", 13: "тринадцатый", 14: "четырнадцатый", 15: "пятнадцатый",
        16: "шестнадцатый", 17: "семнадцатый", 18: "восемнадцатый", 19: "девятнадцатый", 20: "двадцатый",
    }
    return words.get(int(number), f"{number}-й")


def is_chair(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if member.id == TVRS_PERMANENT_CHAIR_ID:
        return True
    return bool(TVRS_CHAIR_ROLE_ID and any(role.id == TVRS_CHAIR_ROLE_ID for role in member.roles))


def is_senator(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if member.id == TVRS_PERMANENT_CHAIR_ID:
        return True
    return any(role.id == TVRS_SENATOR_ROLE_ID for role in member.roles)


def participant_kind(member: discord.Member) -> str | None:
    if member.id == TVRS_PERMANENT_CHAIR_ID:
        return "chair"
    if TVRS_CHAIR_ROLE_ID and any(role.id == TVRS_CHAIR_ROLE_ID for role in member.roles):
        return "chair"
    if any(role.id == TVRS_SENATOR_ROLE_ID for role in member.roles):
        return "senator"
    return None


def role_label(p: LiveParticipant) -> str:
    if p.permanent:
        return "ППС"
    return "Председатель" if p.kind == "chair" else "Сенатор"


def status_icon(confirmed: bool) -> str:
    return "✅" if confirmed else "❌"


def materials_text(materials: str | None) -> str:
    if not materials or not materials.strip():
        return "Материалы не приложены."
    parts = [item.strip() for item in materials.split(",") if item.strip()]
    if not parts:
        return "Материалы не приложены."
    return "\n".join(f"• {item}" for item in parts)[:1000]


def clip_text(value: Any, limit: int = 1000, empty: str = "—") -> str:
    text = str(value or "").strip()
    if not text:
        return empty
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def progress_bar(percent: float, size: int = 12) -> str:
    percent = max(0.0, min(100.0, float(percent or 0.0)))
    filled = round(size * percent / 100.0)
    return "█" * filled + "░" * (size - filled)


def vote_progress(session: LiveConsensusSession) -> tuple[int, int, float]:
    total = len(session.confirmed_participants())
    voted = len([p for p in session.confirmed_participants() if p.user_id in session.votes])
    percent = (voted / total * 100.0) if total else 0.0
    return voted, total, round(percent, 1)


def clean_stage_name(stage: str) -> str:
    return clean_consensus_stage_name(stage)


def active_consensus_snapshot(guild_id: int) -> dict[str, Any] | None:
    session = _active_sessions.get(guild_id)
    if session is None or session.finished:
        return None
    bill = session.current_bill or {}
    return {
        "guild_id": session.guild_id,
        "session_key": session.session_key,
        "plenary_number": session.plenary_number,
        "stage": session.stage,
        "stage_label": clean_stage_name(session.stage),
        "leader_id": session.leader_id,
        "leader_display": session.leader_display,
        "confirmed_count": len(session.confirmed_participants()),
        "participant_count": len(session.participants),
        "timer_deadline": session.timer_deadline.isoformat() if session.timer_deadline else None,
        "discussion_channel_id": session.discussion_channel_id,
        "current_bill": (
            {
                "id": int(bill.get("id") or 0),
                "bill_number": int(bill.get("bill_number") or 0),
                "title": str(bill.get("title") or ""),
            }
            if bill
            else None
        ),
    }


def consensus_session_lock(guild_id: int) -> asyncio.Lock:
    return _session_locks.setdefault(int(guild_id), asyncio.Lock())


def persist_consensus_session(
    session: LiveConsensusSession,
    event_type: str,
    *,
    actor_id: int | None = None,
    actor_display: str | None = None,
    stage_from: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    storage.tvrs_consensus_save_session(
        session_to_snapshot(session),
        event_type=event_type,
        actor_id=actor_id,
        actor_display=actor_display,
        stage_from=stage_from,
        details=details,
    )


def change_consensus_stage(
    session: LiveConsensusSession,
    target_stage: str,
    event_type: str,
    *,
    actor_id: int | None = None,
    actor_display: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    previous, _ = transition_session(session, target_stage)
    persist_consensus_session(
        session,
        event_type,
        actor_id=actor_id,
        actor_display=actor_display,
        stage_from=previous,
        details=details,
    )


def vote_split_lines(session: LiveConsensusSession) -> tuple[str, str, str]:
    yes: list[str] = []
    no: list[str] = []
    wait: list[str] = []
    for p in sorted(session.confirmed_participants(), key=lambda x: (x.kind != "chair", x.display_name.lower())):
        line = f"{p.mention} · `{role_label(p)}`"
        vote = session.votes.get(p.user_id)
        if vote == "yes":
            yes.append(line)
        elif vote == "no":
            no.append(line)
        else:
            wait.append(line)
    return ("\n".join(yes)[:1000] or "—", "\n".join(no)[:1000] or "—", "\n".join(wait)[:1000] or "—")


def queue_short_lines(guild_id: int, limit: int = 8, *, skip_bill_id: int | None = None) -> str:
    queue = storage.tvrs_queue_bills(guild_id, limit=limit + 3)
    lines: list[str] = []
    for index, bill in enumerate([b for b in queue if int(b.get("id") or 0) != int(skip_bill_id or -1)][:limit], 1):
        number = format_bill_number(int(bill.get("bill_number") or 0))
        title = clip_text(bill.get("title"), 70)
        attempt = int(bill.get("attempt") or 1)
        attempt_text = f" · попытка {attempt}/3" if attempt > 1 else ""
        lines.append(f"`{index:02d}` №`{number}` — **{title}**{attempt_text}")
    return "\n".join(lines) or "Очередь пуста."

def format_timer(seconds: int | None) -> str:
    if not seconds:
        return "не установлен"
    if seconds < 60:
        return f"{seconds} сек."
    minutes = seconds // 60
    rest = seconds % 60
    return f"{minutes} мин. {rest} сек." if rest else f"{minutes} мин."


def remaining_timer_text(session: LiveConsensusSession) -> str:
    if not session.timer_deadline:
        return "Таймер не установлен."
    left = int((session.timer_deadline - datetime.now(timezone.utc)).total_seconds())
    if left <= 0:
        return "Время истекло."
    return f"Осталось примерно {format_timer(left)}."


def queue_lines(guild_id: int, limit: int = 20) -> str:
    queue = storage.tvrs_queue_bills(guild_id, limit=limit)
    if not queue:
        return "Очередь законопроектов пуста."
    lines: list[str] = []
    for index, bill in enumerate(queue, 1):
        number = format_bill_number(int(bill.get("bill_number") or 0))
        title = str(bill.get("title") or "Без заголовка")
        status = str(bill.get("status") or "draft")
        attempt = int(bill.get("attempt") or 1)
        suffix = f" • попытка {attempt}/3" if attempt > 1 else ""
        lines.append(f"`{index:02d}.` `{number}` • **{title[:90]}** • `{status}`{suffix}")
    return "\n".join(lines)[:3900]


def build_queue_embed(guild: discord.Guild) -> discord.Embed:
    queue = storage.tvrs_queue_bills(guild.id, limit=30)
    embed = discord.Embed(
        title="📚 Очередь законопроектов Товарищества",
        description=("Очередь пуста." if not queue else "Проекты идут на рассмотрение по возрастанию номера."),
        color=TVRS_EMBED_COLOR,
        timestamp=now_local(),
    )
    if queue:
        blocks: list[str] = []
        for index, bill in enumerate(queue[:20], 1):
            number = format_bill_number(int(bill.get("bill_number") or 0))
            attempt = int(bill.get("attempt") or 1)
            status = str(bill.get("status") or "draft")
            attempt_text = f" • попытка `{attempt}/3`" if attempt > 1 else ""
            blocks.append(f"`{index:02d}`  №`{number}`  **{clip_text(bill.get('title'), 88)}**\n      Автор: {clip_text(bill.get('author_display'), 40)} • статус `{status}`{attempt_text}")
        embed.add_field(name="Ближайшие к рассмотрению", value="\n".join(blocks)[:3900], inline=False)
    embed.set_footer(text="TVRS • очередь пленарного консенсуса")
    return embed


def build_paused_embed(session: LiveConsensusSession) -> discord.Embed:
    embed = discord.Embed(
        title=f"Консенсус приостановлен — {ru_ordinal(session.plenary_number)}",
        description=session.paused_reason or "Консенсус временно приостановлен.",
        color=0xD6B46A,
        timestamp=now_local(),
    )
    embed.add_field(name="Ведущий", value=f"<@{session.leader_id}>", inline=True)
    embed.add_field(name="Предыдущий этап", value=f"`{session.previous_stage or 'unknown'}`", inline=True)
    if session.current_bill:
        embed.add_field(name="Текущий законопроект", value=f"`{format_bill_number(int(session.current_bill.get('bill_number') or 0))}` • {session.current_bill.get('title','')[:200]}", inline=False)
    embed.add_field(name="Действия", value="Можно продолжить после восстановления кворума или завершить консенсус. При завершении текущий законопроект возвращается в очередь.", inline=False)
    return embed


def build_discussion_embed(session: LiveConsensusSession) -> discord.Embed:
    bill = session.current_bill or {}
    embed = discord.Embed(
        title=f"Дискуссия по законопроекту {format_bill_number(int(bill.get('bill_number', 0) or 0))}",
        description=str(bill.get("title") or ""),
        color=0x8FAADC,
        timestamp=now_local(),
    )
    embed.add_field(name="Тип дискуссии", value=session.discussion_type or "выбирается", inline=True)
    embed.add_field(name="Инициатор", value=(f"<@{session.discussion_initiator_id}>" if session.discussion_initiator_id else "не указан"), inline=True)
    if session.discussion_channel_id:
        embed.add_field(name="Канал дискуссии", value=f"<#{session.discussion_channel_id}>", inline=False)
    embed.add_field(name="Порядок", value="На время дискуссии голосование скрыто. Участники отвечают в ЛС на сообщение бота, а бот переносит материалы в канал дискуссии.", inline=False)
    return embed


def bill_attempt_text(bill: dict) -> str:
    attempt = int(bill.get("attempt") or 1)
    return f"Попытка консенсуса: {attempt}/3" if attempt > 1 else "Первичное рассмотрение"


def build_bill_embed(guild: discord.Guild, bill: storage.TVRSBill | dict) -> discord.Embed:
    if isinstance(bill, dict):
        number = format_bill_number(int(bill["bill_number"]))
        title = str(bill.get("title") or "")
        summary = str(bill.get("summary") or "")
        materials = bill.get("materials")
        author = bill.get("author_display") or str(bill.get("author_id"))
        author_id = int(bill.get("author_id") or 0)
        attempt = bill_attempt_text(bill)
    else:
        number = format_bill_number(bill.bill_number)
        title = bill.title
        summary = bill.summary
        materials = bill.materials
        author = bill.author_display or str(bill.author_id)
        author_id = bill.author_id
        attempt = "Первичное рассмотрение"
    embed = discord.Embed(
        title=f"В процессе консенсуса {number}",
        description=f"{summary}\n\nПредседатель передает директиву по проведению консенсуса Товариществу.",
        color=TVRS_EMBED_COLOR,
        timestamp=now_local(),
    )
    embed.add_field(name="Законопроект", value=title[:1024], inline=False)
    embed.add_field(name="Материалы", value=materials_text(materials), inline=False)
    embed.add_field(name="Рассмотрение", value=attempt, inline=True)
    embed.set_footer(text=f"Автор: {author} ({author_id}) • {format_dt()}")
    return embed


def build_sticky_embed(guild: discord.Guild) -> discord.Embed:
    next_number = storage.tvrs_next_bill_number(guild.id, TVRS_DEFAULT_NEXT_BILL_NUMBER)
    embed = discord.Embed(
        title="Подача законопроектов Товарищества",
        description=f"Нажмите ниже, чтобы предложить законопроект.\n\nСледующий номер: **{format_bill_number(next_number)}**",
        color=TVRS_EMBED_COLOR,
    )
    embed.set_footer(text="Товарищество • пленарный консенсус")
    return embed


def build_main_panel_embed(guild: discord.Guild) -> discord.Embed:
    queue_count = len(storage.tvrs_queue_bills(guild.id, limit=100))
    plenary = storage.tvrs_get_next_plenary_number(guild.id, TVRS_DEFAULT_NEXT_PLENARY_NUMBER)
    active = _active_sessions.get(guild.id)
    active_text = "🟢 свободно"
    if active and not active.finished:
        active_text = f"🟡 идет {ru_ordinal(active.plenary_number)} консенсус • этап **{clean_stage_name(active.stage)}** • ведущий <@{active.leader_id}>"
    embed = discord.Embed(
        title="🏛️ Центральное управление Товариществом",
        description=(
            f"**Следующий консенсус:** {ru_ordinal(plenary)}\n"
            f"**Законопроектов в очереди:** {queue_count}\n"
            f"**Состояние:** {active_text}"
        ),
        color=TVRS_EMBED_COLOR,
        timestamp=now_local(),
    )
    embed.add_field(name="📚 Очередь", value=queue_short_lines(guild.id, limit=8), inline=False)
    embed.add_field(name="🎙️ Голосовой канал", value=f"<#{TVRS_CONSENSUS_VOICE_CHANNEL_ID}>", inline=True)
    embed.add_field(name="⚖️ Правило принятия", value="председатель `49%` + активный сенат `2%` = `51%`", inline=True)
    embed.set_footer(text="TVRS • пленарный консенсус")
    return embed


def _hub_money(value: int | None) -> str:
    if value is None:
        return "неизвестно"
    return f"{int(value):,} $".replace(",", " ")


def build_universality_embed(guild: discord.Guild, requester_id: int | None) -> discord.Embed:
    """Build the read-only overview for the common /tvrs entry point."""
    finance = storage.finance_get_latest_state(guild.id)
    active_plans = storage.craft_active_plans(guild.id, 100)
    recipes = storage.craft_list_recipes(guild.id, active_only=True, limit=200)
    actions = (
        storage.bot_list_actions(guild.id, actor_id=requester_id, status="active", limit=100)
        if requester_id is not None
        else []
    )
    queue_count = len(storage.tvrs_queue_bills(guild.id, limit=100))
    active = _active_sessions.get(guild.id)
    consensus = "🟢 свободно"
    if active and not active.finished:
        consensus = f"🟡 {ru_ordinal(active.plenary_number)} консенсус · {clean_stage_name(active.stage)}"
    embed = discord.Embed(
        title="🏛️ Универсалитет Товарищества",
        description=(
            "Единый приватный центр управления. Выберите раздел ниже — бот откроет рабочую "
            "панель, а публичные карточки и журналы отправит в их штатные каналы."
        ),
        color=TVRS_EMBED_COLOR,
        timestamp=now_local(),
    )
    embed.add_field(
        name="💼 Казна",
        value=(
            f"Расчётный остаток: **{_hub_money(finance.get('estimated_balance'))}**\n"
            f"После сверки операций: **{int(finance.get('movements_after_report') or 0)}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="🏭 Крафты",
        value=f"Активных планов: **{len(active_plans)}**\nРецептов: **{len(recipes)}**",
        inline=True,
    )
    embed.add_field(
        name="🧾 Контроль",
        value=(
            f"Ваших активных действий: **{len(actions)}**\nДоступны аудит, статистика и отмена"
            if requester_id is not None
            else "Личный аудит, статистика и безопасная отмена действий"
        ),
        inline=True,
    )
    embed.add_field(
        name="⚖️ Управление",
        value=f"Законопроектов в очереди: **{queue_count}**\nКонсенсус: {consensus}",
        inline=False,
    )
    embed.add_field(
        name="📍 Операционный центр",
        value=(
            f"Активные задачи и процессы → <#{ACTIVE_TASKS_CHANNEL_ID}>\n"
            "Финансовая история и технические события не смешиваются с рабочими задачами.\n"
            f"Законопроекты → <#{TVRS_MATERIALS_CHANNEL_ID}>"
        ),
        inline=False,
    )
    embed.set_footer(text="Панель видна только вам • старые команды остаются быстрыми путями")
    return embed


def build_public_universality_embed(guild: discord.Guild) -> discord.Embed:
    """Build the shared entry panel; button responses remain ephemeral."""
    embed = build_universality_embed(guild, None)
    embed.title = "🏛️ Панель управления Товариществом"
    embed.description = (
        "Это общая точка входа T-Mod. Меню видят все участники, но нажатие любой "
        "рабочей кнопки открывает отдельную личную панель и не изменяет это сообщение."
    )
    embed.set_footer(text="Общее меню • взаимодействия личные • tmod-public-control-panel")
    return embed


def build_universality_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🧭 Как пользоваться Универсалитетом",
        description="Откройте `/tvrs` и переходите между разделами кнопками.",
        color=TVRS_EMBED_COLOR,
    )
    embed.add_field(
        name="Основные разделы",
        value=(
            "**Казна** — посмотреть остаток, снять, положить или провести межотчёт.\n"
            "**Крафты** — планы, рецепты и производство.\n"
            "**Рынок** — русский поиск предметов и подробная статистика цен RU15.\n"
            "**Аудит** — действия, поиск по кодам, статистика и отмена.\n"
            "**Консенсус** — пленарная панель председателя.\n"
            "**Законопроекты** — очередь и переход к подаче проекта.\n"
            "**Ссылки** — доступ к Бюро и заявка в Товарищество."
        ),
        inline=False,
    )
    embed.add_field(
        name="Безопасность",
        value=(
            "Меню можно открыть в любом серверном канале, но бот сохраняет штатные каналы "
            "публикации. Ролевые ограничения председателей и администраторов продолжают действовать."
        ),
        inline=False,
    )
    embed.set_footer(text="Все ответы навигации приватны")
    return embed


async def open_tvrs_hub(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
        return
    embed = await asyncio.to_thread(build_universality_embed, interaction.guild, interaction.user.id)
    await interaction.response.edit_message(
        content=None,
        embed=embed,
        view=TVRSUniversalityView(interaction.user.id),
    )


def participant_lines(session: LiveConsensusSession) -> str:
    chairs = [p for p in session.participants.values() if p.kind == "chair"]
    senators = [p for p in session.participants.values() if p.kind == "senator"]
    lines: list[str] = []
    if chairs:
        lines.append("**Председатели**")
        for p in sorted(chairs, key=lambda x: (not x.permanent, x.display_name.lower())):
            suffix = " • Вето" if p.permanent else ""
            dm = " • ЛС закрыты" if p.dm_failed else ""
            lines.append(f"{status_icon(p.confirmed)} {p.mention} — `{role_label(p)}`{suffix}{dm}")
    if senators:
        lines.append("\n**Сенаторы**")
        for p in sorted(senators, key=lambda x: x.display_name.lower()):
            dm = " • ЛС закрыты" if p.dm_failed else ""
            lines.append(f"{status_icon(p.confirmed)} {p.mention} — `{role_label(p)}`{dm}")
    return "\n".join(lines)[:3800] or "Нет участников."


def build_registration_embed(session: LiveConsensusSession) -> discord.Embed:
    chairs = len(session.confirmed_chairs())
    senators = len(session.confirmed_senators())
    total_confirmed = len(session.confirmed_participants())
    total_invited = len(session.participants)
    percent = (total_confirmed / total_invited * 100.0) if total_invited else 0.0
    embed = discord.Embed(
        title=f"🟦 Регистрация консенсуса • {ru_ordinal(session.plenary_number)}",
        description=(
            "Это приватная панель ведущего. Участники подтверждают участие в личных сообщениях.\n"
            "Ведущий уже зарегистрирован автоматически."
        ),
        color=TVRS_EMBED_COLOR,
        timestamp=now_local(),
    )
    embed.add_field(name="👤 Ведущий", value=f"<@{session.leader_id}>", inline=True)
    embed.add_field(name="📌 Кворум", value=("✅ **набран**" if session.quorum_ready() else "❌ **ожидается**"), inline=True)
    embed.add_field(name="🧾 Подтверждения", value=f"`{total_confirmed}/{total_invited}` {progress_bar(percent, 10)} `{round(percent, 1)}%`", inline=False)
    embed.add_field(name="⚖️ Состав", value=f"Председатели: `{chairs}` • Сенаторы: `{senators}`", inline=True)
    embed.add_field(name="🎙️ Войс", value=f"<#{TVRS_CONSENSUS_VOICE_CHANNEL_ID}>", inline=True)
    embed.add_field(name="👥 Участники", value=participant_lines(session), inline=False)
    embed.add_field(name="📚 Очередь к рассмотрению", value=queue_short_lines(session.guild_id, limit=8), inline=False)
    embed.set_footer(text="Минимум для старта: 2 председателя и нечетное количество сенаторов.")
    return embed


def vote_label(vote: str | None) -> str:
    if vote == "yes":
        return "✅ За"
    if vote == "no":
        return "❌ Против"
    return "⏳ ожидается"


def calculate_consensus(session: LiveConsensusSession) -> dict:
    return calculate_consensus_v2(session)


def vote_lines(session: LiveConsensusSession) -> str:
    lines = []
    for p in sorted(session.confirmed_participants(), key=lambda x: (x.kind != "chair", x.display_name.lower())):
        lines.append(f"{p.mention} — `{role_label(p)}` — **{vote_label(session.votes.get(p.user_id))}**")
    return "\n".join(lines)[:3800] or "Голосов пока нет."


def build_live_vote_embed(session: LiveConsensusSession) -> discord.Embed:
    if session.stage == "paused":
        return build_paused_embed(session)
    if session.stage in {"discussion_pending", "discussion_type", "discussion"}:
        return build_discussion_embed(session)
    bill = session.current_bill or {}
    calc = calculate_consensus(session)
    voted, total, vote_pct = vote_progress(session)
    bill_number = format_bill_number(int(bill.get("bill_number", 0) or 0))
    embed = discord.Embed(
        title=f"🗳️ Live-голосование • законопроект №{bill_number}",
        description=f"**{clip_text(bill.get('title'), 220)}**",
        color=TVRS_EMBED_COLOR,
        timestamp=now_local(),
    )
    embed.add_field(name="📄 Суть законопроекта", value=clip_text(bill.get("summary"), 950), inline=False)
    embed.add_field(name="📎 Материалы", value=materials_text(bill.get("materials")), inline=False)
    embed.add_field(name="⏱️ Таймер", value=remaining_timer_text(session), inline=True)
    embed.add_field(name="📊 Прогресс голосования", value=f"`{voted}/{total}` {progress_bar(vote_pct, 10)} `{vote_pct}%`", inline=True)
    embed.add_field(name="⚖️ Прогноз", value=("✅ **проект проходит**" if calc["accepted"] else "❌ **проект пока не проходит**"), inline=True)
    embed.add_field(name="🏛️ Внутренний консенсус сенаторов", value=f"`{calc['internal_percent']}%` {progress_bar(float(calc['internal_percent']), 10)}\n{'✅ активирован' if calc['internal_active'] else '❌ не активирован'}", inline=True)
    embed.add_field(name="🌐 Общий консенсус", value=f"`{calc['overall_percent']}%` {progress_bar(float(calc['overall_percent']), 10)}\nпорог принятия: `51%`", inline=True)
    yes, no, wait = vote_split_lines(session)
    embed.add_field(name="✅ За", value=yes, inline=True)
    embed.add_field(name="❌ Против", value=no, inline=True)
    embed.add_field(name="⏳ Ожидаются", value=wait, inline=True)
    embed.add_field(name="📚 Далее в очереди", value=queue_short_lines(session.guild_id, limit=5, skip_bill_id=int(bill.get("id") or 0)), inline=False)
    embed.set_footer(text="Панель ведущего • кнопки ниже управляют только текущим голосованием")
    return embed


def build_dm_vote_embed(session: LiveConsensusSession, participant: LiveParticipant) -> discord.Embed:
    if session.stage == "paused":
        embed = build_paused_embed(session)
        embed.add_field(name="Ваш статус", value="Голосование временно недоступно.", inline=False)
        return embed
    if session.stage in {"discussion_pending", "discussion_type", "discussion"}:
        embed = build_discussion_embed(session)
        embed.add_field(name="Ваш статус", value="На время дискуссии кнопки голосования скрыты.", inline=False)
        return embed
    bill = session.current_bill or {}
    calc = calculate_consensus(session)
    voted, total, vote_pct = vote_progress(session)
    bill_number = format_bill_number(int(bill.get("bill_number", 0) or 0))
    embed = discord.Embed(
        title=f"🗳️ Пленарный консенсус • №{bill_number}",
        description=f"**{clip_text(bill.get('title'), 220)}**",
        color=TVRS_EMBED_COLOR,
        timestamp=now_local(),
    )
    embed.add_field(name="📄 Суть", value=clip_text(bill.get("summary"), 950), inline=False)
    embed.add_field(name="📎 Материалы", value=materials_text(bill.get("materials")), inline=False)
    embed.add_field(name="Ваш голос", value=vote_label(session.votes.get(participant.user_id)), inline=True)
    embed.add_field(name="Таймер", value=remaining_timer_text(session), inline=True)
    embed.add_field(name="Прогресс", value=f"`{voted}/{total}` {progress_bar(vote_pct, 8)}", inline=True)
    embed.add_field(name="Общий консенсус сейчас", value=f"`{calc['overall_percent']}%` {progress_bar(float(calc['overall_percent']), 8)}", inline=False)
    embed.add_field(name="Очередь после текущего", value=queue_short_lines(session.guild_id, limit=4, skip_bill_id=int(bill.get("id") or 0)), inline=False)
    if participant.permanent:
        embed.set_footer(text="Вы можете проголосовать, инициировать вето или дождаться итогов.")
    elif participant.kind == "senator":
        embed.set_footer(text="Выберите позицию. При необходимости можно инициировать дискуссию.")
    else:
        embed.set_footer(text="Выберите позицию по текущему законопроекту.")
    return embed


def result_status_text(status: str) -> str:
    return {
        "accepted": "принят",
        "rejected": "не принят",
        "vetoed": "вето",
    }.get(status, status)


def build_result_embed(result: LiveResult, session: LiveConsensusSession) -> discord.Embed:
    color = 0x7FD17F if result.status == "accepted" else (0xD6B46A if result.status == "vetoed" else 0xD67F7F)
    icon = "✅" if result.status == "accepted" else ("🟨" if result.status == "vetoed" else "❌")
    embed = discord.Embed(
        title=f"{icon} Итог голосования • №{format_bill_number(result.bill_number)}",
        description=f"**{clip_text(result.title, 240)}**\n\nРешение: **{result_status_text(result.status)}**",
        color=color,
        timestamp=now_local(),
    )
    embed.add_field(name="🏛️ Внутренний консенсус", value=f"`{result.internal_percent}%` {progress_bar(result.internal_percent, 10)}\n{'✅ активирован' if result.internal_active else '❌ не активирован'}", inline=True)
    embed.add_field(name="🌐 Общий консенсус", value=f"`{result.overall_percent}%` {progress_bar(result.overall_percent, 10)}\nпорог принятия `51%`", inline=True)
    if result.veto_by_id:
        value = f"<@{result.veto_by_id}>"
        if result.retry_bill_number:
            value += f"\nПовторное рассмотрение: №`{format_bill_number(result.retry_bill_number)}`"
        embed.add_field(name="🛑 Право вето", value=value, inline=False)
    lines = []
    for p in sorted(session.confirmed_participants(), key=lambda x: (x.kind != "chair", x.display_name.lower())):
        lines.append(f"{p.mention} · `{role_label(p)}` · **{vote_label(result.votes.get(p.user_id))}**")
    embed.add_field(name="🧾 Зафиксированные голоса", value="\n".join(lines)[:3800] or "Нет голосов.", inline=False)
    embed.set_footer(text="TVRS • результат зафиксирован")
    return embed


def build_final_summary_embed(session: LiveConsensusSession) -> discord.Embed:
    accepted = len([r for r in session.results if r.status == "accepted"])
    rejected = len([r for r in session.results if r.status == "rejected"])
    vetoed = len([r for r in session.results if r.status == "vetoed"])
    embed = discord.Embed(
        title=f"🏛️ Проведен {ru_ordinal(session.plenary_number)} пленарный консенсус Товарищества",
        description=f"Рассмотрено проектов: **{len(session.results)}** • принято: **{accepted}** • не принято: **{rejected}** • вето: **{vetoed}**",
        color=TVRS_EMBED_COLOR,
        timestamp=now_local(),
    )
    chairs = [p for p in session.confirmed_participants() if p.kind == "chair"]
    senators = [p for p in session.confirmed_participants() if p.kind == "senator"]
    embed.add_field(name="👥 Участники", value=f"Председатели: `{len(chairs)}` • Сенаторы: `{len(senators)}`", inline=True)
    embed.add_field(name="👤 Ведущий", value=f"<@{session.leader_id}>", inline=True)
    participants = [f"{p.mention} — `{role_label(p)}`" for p in sorted(session.confirmed_participants(), key=lambda x: (x.kind != 'chair', x.display_name.lower()))]
    embed.add_field(name="Состав", value="\n".join(participants)[:2000] or "Нет участников.", inline=False)
    if session.results:
        lines = []
        for r in session.results:
            mark = "✅" if r.status == "accepted" else ("🟨" if r.status == "vetoed" else "❌")
            lines.append(f"{mark} №`{format_bill_number(r.bill_number)}` • **{clip_text(r.title, 80)}** — {result_status_text(r.status)} • общий `{r.overall_percent}%`")
        embed.add_field(name="Рассмотренные законопроекты", value="\n".join(lines)[:3900], inline=False)
    else:
        embed.add_field(name="Рассмотренные законопроекты", value="Законопроекты не рассматривались.", inline=False)
    embed.set_footer(text="TVRS • официальный итог пленарного консенсуса")
    return embed




async def delete_sticky_message(bot: commands.Bot | discord.Client, guild: discord.Guild) -> None:
    channel = await get_materials_channel(bot, guild)
    if channel is None:
        return
    meta_key = f"tvrs_sticky_message_id:{guild.id}:{channel.id}"
    old_id_raw = storage.get_meta(meta_key)
    old_id = int(old_id_raw) if old_id_raw and old_id_raw.isdigit() else None
    if old_id:
        try:
            old_msg = await channel.fetch_message(old_id)
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    storage.set_meta_value(meta_key, "")


async def edit_session_host_message(session: LiveConsensusSession, *, embed: discord.Embed, view: discord.ui.View | None = None) -> bool:
    msg = session.host_message_obj
    if msg is not None and hasattr(msg, "edit"):
        try:
            await msg.edit(embed=embed, view=view)
            return True
        except Exception:
            pass
    return False


async def edit_or_send_vote_dm(guild: discord.Guild, session: LiveConsensusSession, p: LiveParticipant) -> None:
    if p.user_id == session.leader_id:
        return
    member = guild.get_member(p.user_id)
    if member is None:
        try:
            member = await guild.fetch_member(p.user_id)
        except discord.DiscordException:
            p.dm_failed = True
            return
    view: discord.ui.View = TVRSPermanentVoteView(session.session_key, p.user_id) if p.permanent else TVRSVoteView(session.session_key, p.user_id)
    embed = build_dm_vote_embed(session, p)
    if p.vote_message_id:
        try:
            dm_channel = member.dm_channel or await member.create_dm()
            msg = await dm_channel.fetch_message(p.vote_message_id)
            await msg.edit(content=None, embed=embed, view=view)
            return
        except discord.DiscordException:
            pass
    try:
        dm = await member.send(embed=embed, view=view)
        p.vote_message_id = dm.id
    except discord.DiscordException:
        p.dm_failed = True


async def edit_vote_dm_to_result(guild: discord.Guild, session: LiveConsensusSession, p: LiveParticipant, embed: discord.Embed, content: str | None = None) -> None:
    if p.user_id == session.leader_id:
        return
    member = guild.get_member(p.user_id)
    if member is None:
        try:
            member = await guild.fetch_member(p.user_id)
        except discord.DiscordException:
            return
    if p.vote_message_id:
        try:
            dm_channel = member.dm_channel or await member.create_dm()
            msg = await dm_channel.fetch_message(p.vote_message_id)
            await msg.edit(content=content, embed=embed, view=None)
            return
        except discord.DiscordException:
            pass
    try:
        dm = await member.send(content=content, embed=embed)
        p.vote_message_id = dm.id
    except discord.DiscordException:
        pass

def voice_participants(guild: discord.Guild) -> tuple[list[LiveParticipant], str | None]:
    channel = guild.get_channel(TVRS_CONSENSUS_VOICE_CHANNEL_ID)
    if not isinstance(channel, discord.VoiceChannel):
        return [], f"Не найден голосовой канал <#{TVRS_CONSENSUS_VOICE_CHANNEL_ID}>."
    participants: dict[int, LiveParticipant] = {}
    for member in channel.members:
        if member.bot:
            continue
        kind = participant_kind(member)
        if kind is None:
            continue
        participants[member.id] = LiveParticipant(
            user_id=member.id,
            display_name=member.display_name,
            mention=member.mention,
            kind=kind,  # type: ignore[arg-type]
            permanent=(member.id == TVRS_PERMANENT_CHAIR_ID),
        )
    items = list(participants.values())
    chairs = [p for p in items if p.kind == "chair"]
    senators = [p for p in items if p.kind == "senator"]
    if len(chairs) < 2:
        return items, f"Минимальный кворум не набран: нужно минимум 2 председателя, сейчас `{len(chairs)}`."
    if len(senators) < 1 or len(senators) % 2 == 0:
        return items, f"Минимальный кворум не набран: нужно нечетное количество сенаторов, сейчас `{len(senators)}`."
    return items, None


class TVRSBaseView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: Any) -> None:
        traceback.print_exception(type(error), error, error.__traceback__)
        if interaction.guild is not None:
            await log_technical_event(
                interaction.client,
                interaction.guild,
                title="Ошибка интерфейса TVRS",
                details=f"Элемент: `{getattr(item, 'custom_id', None) or getattr(item, 'label', 'неизвестно')}`\nОшибка: `{type(error).__name__}: {str(error)[:700]}`",
                dedupe_key=f"tvrs-view:{type(error).__name__}",
                cooldown_seconds=60,
            )
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("Внутренняя ошибка действия. Попробуйте ещё раз или обновите панель /tvrs.", ephemeral=True)
            else:
                await interaction.followup.send("Внутренняя ошибка действия. Попробуйте ещё раз или обновите панель /tvrs.", ephemeral=True)
        except discord.DiscordException:
            pass


class TVRSRequesterView(TVRSBaseView):
    def __init__(self, requester_id: int, *, timeout: float | None = 900) -> None:
        super().__init__(timeout=timeout)
        self.requester_id = int(requester_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Это приватное меню открыто не для вас.", ephemeral=True)
            return False
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return False
        return True


class TVRSUniversalityView(TVRSRequesterView):
    def __init__(self, requester_id: int) -> None:
        super().__init__(requester_id, timeout=900)

    @discord.ui.button(label="Казна", emoji="💼", style=discord.ButtonStyle.primary, row=0)
    async def finance(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from modules.finance import FinancePanelView, finance_panel_embed

        assert interaction.guild is not None
        await interaction.response.defer()
        state = await asyncio.to_thread(storage.finance_get_latest_state, interaction.guild.id)
        await interaction.edit_original_response(
            content=None,
            embed=finance_panel_embed(state),
            view=FinancePanelView(
                allow_any_channel=True,
                requester_id=self.requester_id,
                back_to_tvrs=True,
            ),
        )

    @discord.ui.button(label="Крафты", emoji="🏭", style=discord.ButtonStyle.success, row=0)
    async def craft(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from modules.craft import CraftMenuView, craft_menu_embed

        assert interaction.guild is not None
        await interaction.response.defer()
        embed = await asyncio.to_thread(craft_menu_embed, interaction.guild.id)
        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=CraftMenuView(
                allow_any_channel=True,
                requester_id=self.requester_id,
                back_to_tvrs=True,
            ),
        )

    @discord.ui.button(label="Аудит", emoji="🧾", style=discord.ButtonStyle.secondary, row=0)
    async def audit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from modules.finance import AuditCenterView, audit_center_embed

        assert interaction.guild is not None
        embed = await asyncio.to_thread(audit_center_embed, interaction.guild.id, interaction.user.id)
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=AuditCenterView(self.requester_id, back_to_tvrs=True),
        )

    @discord.ui.button(label="Консенсус", emoji="⚖️", style=discord.ButtonStyle.secondary, row=1)
    async def consensus(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        if not is_chair(interaction.user):
            await interaction.response.send_message(
                "Пленарной панелью могут управлять председатели. Остальные разделы `/tvrs` доступны вам без ограничений.",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            content=None,
            embed=build_main_panel_embed(interaction.guild),
            view=TVRSMainPanelView(self.requester_id, back_to_hub=True),
        )

    @discord.ui.button(label="Законопроекты", emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def bills(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.edit_message(
            content=None,
            embed=build_queue_embed(interaction.guild),
            view=TVRSQueueHubView(self.requester_id, interaction.guild.id),
        )

    @discord.ui.button(label="Справка", emoji="🧭", style=discord.ButtonStyle.secondary, row=1)
    async def help(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content=None,
            embed=build_universality_help_embed(),
            view=TVRSHelpView(self.requester_id),
        )

    @discord.ui.button(label="Ссылки", emoji="🔗", style=discord.ButtonStyle.secondary, row=2)
    async def links(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(
            title="🔗 Ссылки Товарищества",
            description="Выберите нужное направление. Ссылки открываются обычными кнопками Discord.",
            color=TVRS_EMBED_COLOR,
        )
        embed.set_footer(text="Меню видно только вам")
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=TVRSLinksView(self.requester_id),
        )

    @discord.ui.button(label="Рынок", emoji="📈", style=discord.ButtonStyle.primary, row=2)
    async def market(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from modules.market import MarketHomeView, market_catalog, market_home_embed

        await interaction.response.defer()
        status = await market_catalog.ensure_ready()
        await interaction.edit_original_response(
            content=None,
            embed=market_home_embed(status),
            view=MarketHomeView(self.requester_id, back_to_tvrs=True),
        )

    @discord.ui.button(label="Обновить", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.defer()
        embed = await asyncio.to_thread(build_universality_embed, interaction.guild, interaction.user.id)
        await interaction.edit_original_response(content=None, embed=embed, view=self)


class TVRSPublicPanelView(TVRSBaseView):
    """Persistent shared menu whose working surfaces are always private."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Панель работает только на сервере Discord.", ephemeral=True)
            return False
        return True

    @discord.ui.button(
        label="Казна",
        emoji="💼",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="tmod_public_tvrs_finance",
    )
    async def finance(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from modules.finance import FinancePanelView, finance_panel_embed

        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = await asyncio.to_thread(storage.finance_get_latest_state, interaction.guild.id)
        await interaction.followup.send(
            embed=finance_panel_embed(state),
            view=FinancePanelView(
                allow_any_channel=True,
                requester_id=interaction.user.id,
                back_to_tvrs=True,
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Крафты",
        emoji="🏭",
        style=discord.ButtonStyle.success,
        row=0,
        custom_id="tmod_public_tvrs_craft",
    )
    async def craft(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from modules.craft import CraftMenuView, craft_menu_embed

        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await asyncio.to_thread(craft_menu_embed, interaction.guild.id)
        await interaction.followup.send(
            embed=embed,
            view=CraftMenuView(
                allow_any_channel=True,
                requester_id=interaction.user.id,
                back_to_tvrs=True,
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Аудит",
        emoji="🧾",
        style=discord.ButtonStyle.secondary,
        row=0,
        custom_id="tmod_public_tvrs_audit",
    )
    async def audit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from modules.finance import AuditCenterView, audit_center_embed

        assert interaction.guild is not None
        embed = await asyncio.to_thread(audit_center_embed, interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(
            embed=embed,
            view=AuditCenterView(interaction.user.id, back_to_tvrs=True),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Консенсус",
        emoji="⚖️",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="tmod_public_tvrs_consensus",
    )
    async def consensus(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        if not is_chair(interaction.user):
            await interaction.response.send_message(
                "Пленарной панелью могут управлять председатели. Остальные разделы доступны вам без ограничений.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=build_main_panel_embed(interaction.guild),
            view=TVRSMainPanelView(interaction.user.id, back_to_hub=True),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Законопроекты",
        emoji="📜",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="tmod_public_tvrs_bills",
    )
    async def bills(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.send_message(
            embed=build_queue_embed(interaction.guild),
            view=TVRSQueueHubView(interaction.user.id, interaction.guild.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Справка",
        emoji="🧭",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="tmod_public_tvrs_help",
    )
    async def help(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=build_universality_help_embed(),
            view=TVRSHelpView(interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Ссылки",
        emoji="🔗",
        style=discord.ButtonStyle.secondary,
        row=2,
        custom_id="tmod_public_tvrs_links",
    )
    async def links(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(
            title="🔗 Ссылки Товарищества",
            description="Выберите нужное направление. Ссылки открываются обычными кнопками Discord.",
            color=TVRS_EMBED_COLOR,
        )
        embed.set_footer(text="Меню видно только вам")
        await interaction.response.send_message(
            embed=embed,
            view=TVRSLinksView(interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Рынок",
        emoji="📈",
        style=discord.ButtonStyle.primary,
        row=2,
        custom_id="tmod_public_tvrs_market",
    )
    async def market(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from modules.market import send_market_panel

        await send_market_panel(interaction, back_to_tvrs=True)

    @discord.ui.button(
        label="Обновить",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        row=2,
        custom_id="tmod_public_tvrs_refresh",
    )
    async def personal(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await asyncio.to_thread(build_universality_embed, interaction.guild, interaction.user.id)
        await interaction.followup.send(
            embed=embed,
            view=TVRSUniversalityView(interaction.user.id),
            ephemeral=True,
        )


class TVRSQueueHubView(TVRSRequesterView):
    def __init__(self, requester_id: int, guild_id: int) -> None:
        super().__init__(requester_id, timeout=900)
        self.add_item(
            discord.ui.Button(
                label="Перейти к подаче",
                emoji="📝",
                style=discord.ButtonStyle.link,
                url=f"https://discord.com/channels/{guild_id}/{TVRS_MATERIALS_CHANNEL_ID}",
            )
        )

    @discord.ui.button(label="Назад", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_tvrs_hub(interaction)


class TVRSHelpView(TVRSRequesterView):
    @discord.ui.button(label="Назад", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_tvrs_hub(interaction)


class TVRSLinksView(TVRSRequesterView):
    def __init__(self, requester_id: int) -> None:
        from modules.links import DISCORD_BUREAU_LINK, DISCORD_TVRS_LINK

        super().__init__(requester_id, timeout=900)
        self.add_item(
            discord.ui.Button(
                label="Доступ к Бюро",
                emoji="⚖️",
                style=discord.ButtonStyle.link,
                url=DISCORD_BUREAU_LINK,
                row=0,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Заявка в Товарищество",
                emoji="🏛️",
                style=discord.ButtonStyle.link,
                url=DISCORD_TVRS_LINK,
                row=0,
            )
        )

    @discord.ui.button(label="Назад", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_tvrs_hub(interaction)


class TVRSStickyView(TVRSBaseView):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Создать", style=discord.ButtonStyle.secondary, custom_id="tvrs_bill_create")
    async def create_bill(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        if interaction.channel_id != TVRS_MATERIALS_CHANNEL_ID:
            await interaction.response.send_message(f"Эта система работает только в канале <#{TVRS_MATERIALS_CHANNEL_ID}>.", ephemeral=True)
            return
        if not is_senator(interaction.user):
            await interaction.response.send_message("Законопроект может предложить только сенатор или председатель Товарищества.", ephemeral=True)
            return
        active = _active_sessions.get(interaction.guild.id)
        if active and not active.finished:
            await interaction.response.send_message("Сейчас идет пленарный консенсус. Подача новых законопроектов будет снова доступна после завершения.", ephemeral=True)
            return
        next_number = storage.tvrs_next_bill_number(interaction.guild.id, TVRS_DEFAULT_NEXT_BILL_NUMBER)
        await interaction.response.send_modal(TVRSBillModal(next_number))


class TVRSBillModal(discord.ui.Modal):
    def __init__(self, next_number: int) -> None:
        super().__init__(title=f"Драфт законопроекта {format_bill_number(next_number)}"[:45], timeout=900)
        self.heading = discord.ui.TextInput(
            label="Заголовок законопроекта",
            placeholder="О принятии Имя Фамилия в состав Товарищества",
            min_length=5,
            max_length=180,
            required=True,
        )
        self.summary = discord.ui.TextInput(
            label="Суть законопроекта",
            placeholder="Предлагается рассмотреть вступление кандидата и условия его участия в Товариществе.",
            style=discord.TextStyle.paragraph,
            min_length=10,
            max_length=3000,
            required=True,
        )
        self.materials = discord.ui.TextInput(
            label="Материалы",
            placeholder="Ссылки через запятую: https://example.com/doc, https://example.com/proof",
            style=discord.TextStyle.paragraph,
            min_length=0,
            max_length=1000,
            required=False,
        )
        self.add_item(self.heading)
        self.add_item(self.summary)
        self.add_item(self.materials)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or interaction.channel is None:
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        if interaction.channel_id != TVRS_MATERIALS_CHANNEL_ID:
            await interaction.response.send_message(f"Эта система работает только в канале <#{TVRS_MATERIALS_CHANNEL_ID}>.", ephemeral=True)
            return
        if not is_senator(interaction.user):
            await interaction.response.send_message("Законопроект может предложить только сенатор или председатель Товарищества.", ephemeral=True)
            return
        active = _active_sessions.get(interaction.guild.id)
        if active and not active.finished:
            await interaction.response.send_message("Сейчас идет пленарный консенсус. Подача новых законопроектов будет снова доступна после завершения.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            bill = storage.tvrs_create_bill(
                guild_id=interaction.guild.id,
                channel_id=interaction.channel_id,
                author_id=interaction.user.id,
                author_display=interaction.user.display_name,
                title=str(self.heading.value).strip(),
                summary=str(self.summary.value).strip(),
                materials=str(self.materials.value).strip() or None,
            )
            message = await interaction.channel.send(embed=build_bill_embed(interaction.guild, bill), allowed_mentions=discord.AllowedMentions.none())
            storage.tvrs_set_bill_message(bill.id, message.id, interaction.channel_id)
            await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)
            await interaction.followup.send(f"Законопроект **{format_bill_number(bill.bill_number)}** создан и отправлен в канал материалов.", ephemeral=True)
        except Exception as exc:
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            await interaction.followup.send(f"Не удалось создать законопроект: {str(exc)[:800]}", ephemeral=True)


class TVRSMainPanelView(TVRSBaseView):
    def __init__(self, requester_id: int, *, back_to_hub: bool = False) -> None:
        super().__init__(timeout=600)
        self.requester_id = requester_id
        self.back_to_hub = back_to_hub
        if back_to_hub:
            back = discord.ui.Button(label="Назад", emoji="⬅️", style=discord.ButtonStyle.secondary)

            async def back_callback(interaction: discord.Interaction) -> None:
                await open_tvrs_hub(interaction)

            back.callback = back_callback
            self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Это меню открыто не для вас.", ephemeral=True)
            return False
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return False
        if not is_chair(interaction.user):
            await interaction.response.send_message("Панель доступна только председателю.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Открыть заседание", emoji="⚖️", style=discord.ButtonStyle.primary)
    async def open_active_consensus(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        session = _active_sessions.get(interaction.guild.id)
        if session is None or session.finished:
            await interaction.response.send_message("Активного заседания сейчас нет.", ephemeral=True)
            return
        if interaction.user.id != session.leader_id:
            await interaction.response.send_message(
                f"Активным заседанием управляет ведущий <@{session.leader_id}>.",
                ephemeral=True,
            )
            return
        session.host_message_obj = interaction.message
        session.host_message_id = getattr(interaction.message, "id", None)
        persist_consensus_session(
            session,
            "host_panel_reopened",
            actor_id=interaction.user.id,
            actor_display=getattr(interaction.user, "display_name", str(interaction.user)),
        )
        if session.stage == "registration":
            embed = build_registration_embed(session)
            view: discord.ui.View = TVRSRegistrationView(session.session_key)
        elif session.stage == "after_result" and session.results:
            embed = build_result_embed(session.results[-1], session)
            view = TVRSAfterResultView(session.session_key)
        else:
            embed = build_live_vote_embed(session)
            view = TVRSHostVoteView(session.session_key)
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

    @discord.ui.button(label="Начать консенсус", style=discord.ButtonStyle.secondary)
    async def start_consensus(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        async with consensus_session_lock(interaction.guild.id):
            if interaction.guild.id in _active_sessions and not _active_sessions[interaction.guild.id].finished:
                await interaction.response.send_message("На сервере уже идет пленарный консенсус.", ephemeral=True)
                return
            participants, error = voice_participants(interaction.guild)
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            if interaction.user.id not in {participant.user_id for participant in participants}:
                await interaction.response.send_message(
                    f"Ведущий должен находиться в голосовом канале <#{TVRS_CONSENSUS_VOICE_CHANNEL_ID}>.",
                    ephemeral=True,
                )
                return
            plenary = storage.tvrs_get_next_plenary_number(
                interaction.guild.id,
                TVRS_DEFAULT_NEXT_PLENARY_NUMBER,
            )
            session = LiveConsensusSession(
                session_key=f"{interaction.guild.id}:{uuid.uuid4().hex[:12]}",
                guild_id=interaction.guild.id,
                channel_id=TVRS_BILLS_CHANNEL_ID,
                leader_id=interaction.user.id,
                leader_display=interaction.user.display_name,
                plenary_number=plenary,
                participants={participant.user_id: participant for participant in participants},
            )
            if interaction.user.id in session.participants:
                session.participants[interaction.user.id].confirmed = True
            _active_sessions[interaction.guild.id] = session
            persist_consensus_session(
                session,
                "session_created",
                actor_id=interaction.user.id,
                actor_display=interaction.user.display_name,
                details={"participant_count": len(session.participants)},
            )
        wake_operations_worker()
        await interaction.response.defer(ephemeral=True, thinking=True)
        await delete_sticky_message(interaction.client, interaction.guild)
        msg = await interaction.followup.send(embed=build_registration_embed(session), view=TVRSRegistrationView(session.session_key), ephemeral=True, wait=True, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        session.host_message_obj = msg
        session.host_message_id = getattr(msg, "id", None)
        await send_confirmation_messages(interaction.client, interaction.guild, session)
        persist_consensus_session(session, "registration_invitations_sent")
        await edit_session_host_message(session, embed=build_registration_embed(session), view=TVRSRegistrationView(session.session_key))

    @discord.ui.button(label="Очередь", style=discord.ButtonStyle.secondary)
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.send_message(embed=build_queue_embed(interaction.guild), ephemeral=True)

    @discord.ui.button(label="Обновить", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.edit_message(embed=build_main_panel_embed(interaction.guild), view=self)


class TVRSRegistrationView(TVRSBaseView):
    def __init__(self, session_key: str) -> None:
        super().__init__(timeout=None)
        self.session_key = session_key

    def session(self, guild_id: int) -> LiveConsensusSession | None:
        s = _active_sessions.get(guild_id)
        return s if s and s.session_key == self.session_key else None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return False
        session = self.session(interaction.guild.id)
        if session is None:
            await interaction.response.send_message("Сессия консенсуса не найдена.", ephemeral=True)
            return False
        if interaction.user.id != session.leader_id:
            await interaction.response.send_message("Управлять этим консенсусом может только тот, кто его инициировал.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Начать голосование", style=discord.ButtonStyle.success)
    async def start_vote(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session is None:
            await interaction.response.send_message("Сессия консенсуса не найдена.", ephemeral=True)
            return
        if not session.quorum_ready():
            await interaction.response.send_message("Подтвержденный кворум еще не набран.", ephemeral=True)
            return
        voice_ok, voice_reason = session_voice_quorum_ready(interaction.guild, session)
        if not voice_ok:
            await interaction.response.send_message(voice_reason, ephemeral=True)
            return
        await interaction.response.defer()
        await begin_next_bill_vote(interaction.client, interaction.guild, session, interaction.channel)

    @discord.ui.button(label="Отменить", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            change_consensus_stage(
                session,
                "cancelled",
                "session_cancelled",
                actor_id=interaction.user.id,
                actor_display=getattr(interaction.user, "display_name", str(interaction.user)),
            )
            _active_sessions.pop(interaction.guild.id, None)
            wake_operations_worker()
        await interaction.response.edit_message(content="Консенсус отменен.", embed=None, view=None)
        await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)


class TVRSConfirmView(TVRSBaseView):
    def __init__(self, session_key: str, user_id: int) -> None:
        super().__init__(timeout=None)
        self.session_key = session_key
        self.user_id = user_id
        button = discord.ui.Button(
            label="Подтвердить участие",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"tvrs_confirm:{session_key}:{user_id}",
        )
        button.callback = self.confirm  # type: ignore[assignment]
        self.add_item(button)

    async def confirm(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это подтверждение не для вас.", ephemeral=True)
            return
        session = next((s for s in _active_sessions.values() if s.session_key == self.session_key), None)
        if session is None:
            await interaction.response.send_message("Сессия консенсуса уже закрыта.", ephemeral=True)
            return
        p = session.participants.get(self.user_id)
        if p is None:
            await interaction.response.send_message("Вы не указаны как участник консенсуса.", ephemeral=True)
            return
        if session.stage != "registration":
            await interaction.response.send_message("Регистрация на это заседание уже завершена.", ephemeral=True)
            return
        p.confirmed = True
        p.dm_failed = False
        persist_consensus_session(
            session,
            "participant_confirmed",
            actor_id=interaction.user.id,
            actor_display=getattr(interaction.user, "display_name", str(interaction.user)),
        )
        await interaction.response.edit_message(content="Участие подтверждено.", embed=None, view=None)
        guild = interaction.client.get_guild(session.guild_id)
        if guild:
            await update_host_registration_message(interaction.client, guild, session)


class TVRSVoteView(TVRSBaseView):
    def __init__(self, session_key: str, user_id: int, host_panel: bool = False) -> None:
        super().__init__(timeout=None)
        self.session_key = session_key
        self.user_id = user_id
        self.host_panel = host_panel
        session = self.session()
        participant = session.participants.get(user_id) if session else None
        if session and participant and session.stage == "voting":
            yes = discord.ui.Button(label="За", style=discord.ButtonStyle.success, custom_id=f"tvrs_vote_yes:{session_key}:{user_id}")
            no = discord.ui.Button(label="Против", style=discord.ButtonStyle.danger, custom_id=f"tvrs_vote_no:{session_key}:{user_id}")
            yes.callback = self._yes_callback  # type: ignore[assignment]
            no.callback = self._no_callback  # type: ignore[assignment]
            self.add_item(yes)
            self.add_item(no)
            if participant.kind == "senator" and not session.discussion_initiator_id:
                discussion = discord.ui.Button(label="Дискуссия", style=discord.ButtonStyle.secondary, custom_id=f"tvrs_discussion:{session_key}:{user_id}")
                discussion.callback = self._discussion_callback  # type: ignore[assignment]
                self.add_item(discussion)
            if participant.permanent:
                veto = discord.ui.Button(label="Вето!", style=discord.ButtonStyle.danger, custom_id=f"tvrs_veto_dm:{session_key}:{user_id}")
                veto.callback = self._veto_callback  # type: ignore[assignment]
                self.add_item(veto)

    def session(self) -> LiveConsensusSession | None:
        return next((s for s in _active_sessions.values() if s.session_key == self.session_key), None)

    async def _yes_callback(self, interaction: discord.Interaction) -> None:
        await self._cast(interaction, "yes")

    async def _no_callback(self, interaction: discord.Interaction) -> None:
        await self._cast(interaction, "no")

    async def _discussion_callback(self, interaction: discord.Interaction) -> None:
        session = self.session()
        if session is None or session.current_bill is None:
            await interaction.response.send_message("Активное голосование не найдено.", ephemeral=True)
            return
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Эта кнопка не для вас.", ephemeral=True)
            return
        p = session.participants.get(self.user_id)
        if p is None or p.kind != "senator":
            await interaction.response.send_message("Дискуссию может инициировать только сенатор.", ephemeral=True)
            return
        if session.discussion_initiator_id:
            await interaction.response.send_message("Дискуссия по этому законопроекту уже инициирована.", ephemeral=True)
            return
        guild = interaction.client.get_guild(session.guild_id)
        if guild is None:
            await interaction.response.send_message("Сервер не найден.", ephemeral=True)
            return
        await interaction.response.defer()
        await request_discussion(interaction.client, guild, session, p)
        try:
            await interaction.followup.send("Дискуссия инициирована. Выберите тип дискуссии ниже.", view=TVRSDiscussionTypeView(session.session_key, self.user_id), ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send("Дискуссия инициирована. Выберите тип дискуссии ниже.", view=TVRSDiscussionTypeView(session.session_key, self.user_id))

    async def _veto_callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Эта кнопка не для вас.", ephemeral=True)
            return
        if interaction.user.id != TVRS_PERMANENT_CHAIR_ID:
            await interaction.response.send_message("Право вето доступно только постоянному председателю.", ephemeral=True)
            return
        await interaction.response.send_message("Подтвердите применение права вето.", ephemeral=True, view=TVRSVetoConfirmView(self.session_key, interaction.user.id))

    async def _cast(self, interaction: discord.Interaction, vote: str) -> None:
        session = self.session()
        if session is None or session.current_bill is None:
            await interaction.response.send_message("Активное голосование не найдено.", ephemeral=True)
            return
        if session.stage != "voting":
            await interaction.response.send_message("Сейчас голосование недоступно: идет пауза или дискуссия.", ephemeral=True)
            return
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Эта кнопка не для вас.", ephemeral=True)
            return
        if self.user_id not in {p.user_id for p in session.confirmed_participants()}:
            await interaction.response.send_message("Вы не зарегистрированы в этом голосовании.", ephemeral=True)
            return
        async with consensus_session_lock(session.guild_id):
            if session.stage != "voting" or session.current_bill is None:
                await interaction.response.send_message("Голосование уже завершено.", ephemeral=True)
                return
            session.votes[self.user_id] = vote
            persist_consensus_session(
                session,
                "vote_cast",
                actor_id=interaction.user.id,
                actor_display=getattr(interaction.user, "display_name", str(interaction.user)),
                details={"vote": vote, "bill_id": int(session.current_bill.get("id") or 0)},
            )
            should_finalize = session.all_voted()
        await interaction.response.defer()
        try:
            await interaction.message.edit(embed=build_dm_vote_embed(session, session.participants[self.user_id]), view=TVRSVoteView(session.session_key, self.user_id))  # type: ignore[union-attr]
        except discord.DiscordException:
            pass
        guild = interaction.client.get_guild(session.guild_id)
        if guild:
            await update_host_vote_message(interaction.client, guild, session)
        if should_finalize and guild:
            await finalize_current_vote(interaction.client, guild, session, forced=False)


class TVRSPermanentVoteView(TVRSVoteView):
    pass


class TVRSDiscussionTypeView(TVRSBaseView):
    def __init__(self, session_key: str, user_id: int) -> None:
        super().__init__(timeout=300)
        self.session_key = session_key
        self.user_id = user_id
        for label in ["Правовая", "Фактическая", "Процедурная", "Иная"]:
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=f"tvrs_disc_type:{session_key}:{user_id}:{label}")
            button.callback = self._make_callback(label)  # type: ignore[assignment]
            self.add_item(button)

    def _make_callback(self, label: str):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("Это меню не для вас.", ephemeral=True)
                return
            session = next((s for s in _active_sessions.values() if s.session_key == self.session_key), None)
            if session is None:
                await interaction.response.send_message("Сессия консенсуса не найдена.", ephemeral=True)
                return
            guild = interaction.client.get_guild(session.guild_id)
            if guild is None:
                await interaction.response.send_message("Сервер не найден.", ephemeral=True)
                return
            await interaction.response.defer()
            await start_discussion_channel(interaction.client, guild, session, label)
            try:
                await interaction.followup.send(f"Дискуссия типа **{label}** начата.", ephemeral=True)
            except discord.HTTPException:
                await interaction.followup.send(f"Дискуссия типа **{label}** начата.")
        return callback


class TVRSHostVoteView(TVRSBaseView):
    def __init__(self, session_key: str) -> None:
        super().__init__(timeout=None)
        self.session_key = session_key
        session = next((s for s in _active_sessions.values() if s.session_key == self.session_key), None)
        if session and session.stage == "paused":
            self._add_button("Продолжить", discord.ButtonStyle.success, self.resume, row=0)
            self._add_button("Завершить консенсус", discord.ButtonStyle.danger, self.finish_session_btn, row=0)
            return
        if session and session.stage in {"discussion_pending", "discussion_type", "discussion"}:
            self._add_button("Завершить дискуссию", discord.ButtonStyle.success, self.end_discussion, row=0)
            self._add_button("Пауза", discord.ButtonStyle.secondary, self.pause, row=0)
            self._add_button("Завершить консенсус", discord.ButtonStyle.danger, self.finish_session_btn, row=0)
            return
        self._add_button("За", discord.ButtonStyle.success, self.host_yes, row=0)
        self._add_button("Против", discord.ButtonStyle.danger, self.host_no, row=0)
        self._add_button("Завершить голосование", discord.ButtonStyle.secondary, self.finish, row=0)
        for label, seconds in TVRS_TIMER_OPTIONS:
            self._add_button(f"Таймер {label}", discord.ButtonStyle.secondary, self._timer_callback(seconds), row=1)
        self._add_button("Пауза", discord.ButtonStyle.secondary, self.pause, row=2)
        if session and session.leader_id == TVRS_PERMANENT_CHAIR_ID:
            self._add_button("Вето!", discord.ButtonStyle.danger, self.veto, row=2)

    def _add_button(self, label: str, style: discord.ButtonStyle, callback, row: int = 0) -> None:
        button = discord.ui.Button(label=label, style=style, row=row)
        button.callback = callback  # type: ignore[assignment]
        self.add_item(button)

    def session(self, guild_id: int) -> LiveConsensusSession | None:
        s = _active_sessions.get(guild_id)
        return s if s and s.session_key == self.session_key else None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return False
        session = self.session(interaction.guild.id)
        if session is None:
            await interaction.response.send_message("Сессия не найдена.", ephemeral=True)
            return False
        if interaction.user.id != session.leader_id:
            await interaction.response.send_message("Управлять голосованием может только ведущий консенсуса.", ephemeral=True)
            return False
        return True

    async def host_yes(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            if session.stage != "voting":
                await interaction.response.send_message("Сейчас голосование недоступно.", ephemeral=True)
                return
            async with consensus_session_lock(session.guild_id):
                if session.stage != "voting" or session.current_bill is None:
                    await interaction.response.send_message("Голосование уже завершено.", ephemeral=True)
                    return
                session.votes[session.leader_id] = "yes"
                persist_consensus_session(
                    session,
                    "vote_cast",
                    actor_id=interaction.user.id,
                    actor_display=getattr(interaction.user, "display_name", str(interaction.user)),
                    details={"vote": "yes", "bill_id": int(session.current_bill.get("id") or 0)},
                )
                should_finalize = session.all_voted()
            await interaction.response.edit_message(embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            if should_finalize:
                await finalize_current_vote(interaction.client, interaction.guild, session, forced=False)

    async def host_no(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            if session.stage != "voting":
                await interaction.response.send_message("Сейчас голосование недоступно.", ephemeral=True)
                return
            async with consensus_session_lock(session.guild_id):
                if session.stage != "voting" or session.current_bill is None:
                    await interaction.response.send_message("Голосование уже завершено.", ephemeral=True)
                    return
                session.votes[session.leader_id] = "no"
                persist_consensus_session(
                    session,
                    "vote_cast",
                    actor_id=interaction.user.id,
                    actor_display=getattr(interaction.user, "display_name", str(interaction.user)),
                    details={"vote": "no", "bill_id": int(session.current_bill.get("id") or 0)},
                )
                should_finalize = session.all_voted()
            await interaction.response.edit_message(embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            if should_finalize:
                await finalize_current_vote(interaction.client, interaction.guild, session, forced=False)

    async def finish(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            await finalize_current_vote(interaction.client, interaction.guild, session, forced=True)

    def _timer_callback(self, seconds: int):
        async def callback(interaction: discord.Interaction) -> None:
            assert interaction.guild is not None
            session = self.session(interaction.guild.id)
            if session is None or session.stage != "voting":
                await interaction.response.send_message("Таймер можно поставить только во время голосования.", ephemeral=True)
                return
            await interaction.response.defer()
            await set_vote_timer(interaction.client, interaction.guild, session, seconds)
            await interaction.followup.send(f"Таймер установлен: **{format_timer(seconds)}**.", ephemeral=True)
        return callback

    async def pause(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            await pause_session(interaction.client, interaction.guild, session, "Консенсус приостановлен ведущим.", automatic=False)

    async def resume(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            await resume_session(interaction.client, interaction.guild, session)

    async def finish_session_btn(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            await finish_session(interaction.client, interaction.guild, session, interaction.channel)

    async def end_discussion(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            await end_discussion(interaction.client, interaction.guild, session)

    async def veto(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session is None:
            await interaction.response.send_message("Сессия не найдена.", ephemeral=True)
            return
        if interaction.user.id != TVRS_PERMANENT_CHAIR_ID:
            await interaction.response.send_message("Право вето доступно только постоянному председателю.", ephemeral=True)
            return
        await interaction.response.send_message("Подтвердите применение права вето.", ephemeral=True, view=TVRSVetoConfirmView(session.session_key, interaction.user.id))


class TVRSVetoConfirmView(TVRSBaseView):
    def __init__(self, session_key: str, user_id: int) -> None:
        super().__init__(timeout=120)
        self.session_key = session_key
        self.user_id = user_id

    @discord.ui.button(label="Подтвердить вето", style=discord.ButtonStyle.danger)
    async def confirm_veto(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это подтверждение не для вас.", ephemeral=True)
            return
        session = next((s for s in _active_sessions.values() if s.session_key == self.session_key), None)
        if session is None:
            await interaction.response.send_message("Сессия не найдена.", ephemeral=True)
            return
        guild = interaction.client.get_guild(session.guild_id)
        if guild is None:
            await interaction.response.send_message("Сервер не найден.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await apply_veto(interaction.client, guild, session, interaction.user)
        await interaction.followup.send("Право вето применено.", ephemeral=True)


class TVRSAfterResultView(TVRSBaseView):
    def __init__(self, session_key: str) -> None:
        super().__init__(timeout=None)
        self.session_key = session_key

    def session(self, guild_id: int) -> LiveConsensusSession | None:
        s = _active_sessions.get(guild_id)
        return s if s and s.session_key == self.session_key else None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return False
        session = self.session(interaction.guild.id)
        if session is None:
            await interaction.response.send_message("Сессия не найдена.", ephemeral=True)
            return False
        if interaction.user.id != session.leader_id:
            await interaction.response.send_message("Управлять этим консенсусом может только ведущий.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Следующий проект", style=discord.ButtonStyle.success)
    async def next_bill(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            await begin_next_bill_vote(interaction.client, interaction.guild, session, interaction.channel)

    @discord.ui.button(label="Завершить консенсус", style=discord.ButtonStyle.secondary)
    async def finish_all(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            await finish_session(interaction.client, interaction.guild, session, interaction.channel)


async def cancel_vote_timer(session: LiveConsensusSession) -> None:
    task = session.timer_task
    if task and not task.done():
        task.cancel()
    session.timer_task = None
    session.timer_deadline = None
    session.timer_seconds = None


async def set_vote_timer(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession, seconds: int) -> None:
    await cancel_vote_timer(session)
    session.timer_seconds = int(seconds)
    session.timer_deadline = datetime.now(timezone.utc) + timedelta(seconds=int(seconds))
    persist_consensus_session(
        session,
        "timer_set",
        actor_id=session.leader_id,
        actor_display=session.leader_display,
        details={"seconds": int(seconds)},
    )

    async def runner() -> None:
        try:
            await asyncio.sleep(int(seconds))
            current = _active_sessions.get(guild.id)
            if current is session and not session.finished and session.current_bill is not None and session.stage == "voting":
                await finalize_current_vote(bot, guild, session, forced=True)
        except asyncio.CancelledError:
            return
        except Exception:
            traceback.print_exc()

    session.timer_task = asyncio.create_task(runner())
    content = f"Установлен таймер голосования: {format_timer(seconds)}."
    for p in session.confirmed_participants():
        if p.user_id == session.leader_id:
            continue
        await edit_or_send_vote_dm(guild, session, p)
        member = guild.get_member(p.user_id)
        if member and p.vote_message_id:
            try:
                dm_channel = member.dm_channel or await member.create_dm()
                msg = await dm_channel.fetch_message(p.vote_message_id)
                await msg.edit(content=content, embed=build_dm_vote_embed(session, p), view=TVRSVoteView(session.session_key, p.user_id))
            except discord.DiscordException:
                pass
    await update_host_vote_message(bot, guild, session)


async def update_all_vote_dms(guild: discord.Guild, session: LiveConsensusSession, content: str | None = None) -> None:
    for p in session.confirmed_participants():
        if p.user_id == session.leader_id:
            continue
        member = guild.get_member(p.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(p.user_id)
            except discord.DiscordException:
                continue
        embed = build_dm_vote_embed(session, p)
        view = TVRSVoteView(session.session_key, p.user_id)
        if p.vote_message_id:
            try:
                dm_channel = member.dm_channel or await member.create_dm()
                msg = await dm_channel.fetch_message(p.vote_message_id)
                await msg.edit(content=content, embed=embed, view=view)
                continue
            except discord.DiscordException:
                pass
        try:
            dm = await member.send(content=content, embed=embed, view=view)
            p.vote_message_id = dm.id
        except discord.DiscordException:
            p.dm_failed = True


async def request_discussion(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession, initiator: LiveParticipant) -> None:
    if session.stage != "voting" or session.current_bill is None:
        return
    if session.discussion_initiator_id:
        return
    await cancel_vote_timer(session)
    session.previous_stage = session.stage
    session.discussion_initiator_id = initiator.user_id
    change_consensus_stage(
        session,
        "discussion_type",
        "discussion_requested",
        actor_id=initiator.user_id,
        actor_display=initiator.display_name,
    )
    wake_operations_worker()
    await update_all_vote_dms(guild, session, content=f"<@{initiator.user_id}> инициировал дискуссию. Голосование временно приостановлено.")
    await update_host_vote_message(bot, guild, session)


async def start_discussion_channel(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession, discussion_type: str) -> None:
    if session.current_bill is None:
        return
    session.discussion_type = discussion_type
    change_consensus_stage(
        session,
        "discussion",
        "discussion_started",
        actor_id=session.discussion_initiator_id,
        details={"discussion_type": discussion_type},
    )
    wake_operations_worker()
    category = guild.get_channel(TVRS_DISCUSSION_CATEGORY_ID)
    if category is None:
        try:
            fetched = await bot.fetch_channel(TVRS_DISCUSSION_CATEGORY_ID)  # type: ignore[attr-defined]
            category = fetched if isinstance(fetched, discord.CategoryChannel) else None
        except discord.DiscordException:
            category = None
    overwrites: dict[Any, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True) if guild.me else discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    senator_role = guild.get_role(TVRS_SENATOR_ROLE_ID)
    chair_role = guild.get_role(TVRS_CHAIR_ROLE_ID)
    if senator_role:
        overwrites[senator_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
    if chair_role:
        overwrites[chair_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
    number = format_bill_number(int(session.current_bill.get("bill_number") or 0))
    safe_title = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ_-]+", "-", str(session.current_bill.get("title") or "project")).strip("-")[:35]
    channel_name = f"дискуссия-{number}-{safe_title}"[:90]
    created = None
    try:
        created = await guild.create_text_channel(channel_name, category=category if isinstance(category, discord.CategoryChannel) else None, overwrites=overwrites, reason="TVRS live consensus discussion")
    except discord.DiscordException:
        created = None
    if created:
        session.discussion_channel_id = created.id
        try:
            await created.send(embed=build_discussion_embed(session), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.DiscordException:
            pass
    allowed: set[int] = {p.user_id for p in session.confirmed_participants() if p.kind == "senator"}
    non_leader_chair = next((p for p in session.confirmed_participants() if p.kind == "chair" and p.user_id != session.leader_id), None)
    if non_leader_chair:
        allowed.add(non_leader_chair.user_id)
    session.discussion_allowed_user_ids = allowed
    for p in session.confirmed_participants():
        if p.user_id not in allowed:
            continue
        member = guild.get_member(p.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(p.user_id)
            except discord.DiscordException:
                continue
        try:
            msg = await member.send(
                content="Дискуссия начата. Ответьте именно на это сообщение, чтобы бот перенес вашу позицию и материалы в канал дискуссии.",
                embed=build_discussion_embed(session),
            )
            p.discussion_message_id = msg.id
        except discord.DiscordException:
            p.dm_failed = True
    persist_consensus_session(
        session,
        "discussion_channel_ready",
        details={"discussion_channel_id": session.discussion_channel_id},
    )
    await update_all_vote_dms(guild, session, content="Дискуссия начата. Голосование временно скрыто.")
    await update_host_vote_message(bot, guild, session)


async def forward_discussion_message(bot: commands.Bot, message: discord.Message) -> bool:
    if message.guild is not None or message.author.bot:
        return False
    for session in list(_active_sessions.values()):
        if session.finished or session.stage != "discussion" or not session.discussion_channel_id:
            continue
        if message.author.id not in session.discussion_allowed_user_ids:
            continue
        p = session.participants.get(message.author.id)
        if not p:
            continue
        ref_id = getattr(getattr(message, "reference", None), "message_id", None)
        if p.discussion_message_id and ref_id and int(ref_id) != int(p.discussion_message_id):
            continue
        if p.discussion_message_id and not ref_id:
            # Accept free DM while discussion is active; mobile clients often forget reply references.
            pass
        channel = bot.get_channel(session.discussion_channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(session.discussion_channel_id)
            except discord.DiscordException:
                channel = None
        if not hasattr(channel, "send"):
            return False
        embed = discord.Embed(
            title=f"Материал дискуссии от {p.display_name}",
            description=(message.content or "Без текста")[:3900],
            color=0x8FAADC,
            timestamp=now_local(),
        )
        embed.add_field(name="Роль", value=role_label(p), inline=True)
        if session.current_bill:
            embed.add_field(name="Законопроект", value=format_bill_number(int(session.current_bill.get("bill_number") or 0)), inline=True)
        files = []
        for att in message.attachments[:5]:
            try:
                files.append(await att.to_file())
            except discord.DiscordException:
                pass
        try:
            await channel.send(embed=embed, files=files, allowed_mentions=discord.AllowedMentions.none())  # type: ignore[attr-defined]
            try:
                await message.add_reaction("✅")
            except discord.DiscordException:
                pass
            return True
        except discord.DiscordException:
            return False
    return False


async def end_discussion(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession) -> None:
    if session.stage not in {"discussion_pending", "discussion_type", "discussion"}:
        return
    if session.discussion_channel_id:
        channel = guild.get_channel(session.discussion_channel_id)
        if hasattr(channel, "send"):
            try:
                await channel.send("Дискуссия завершена ведущим. Голосование возвращено в активный режим.")  # type: ignore[attr-defined]
            except discord.DiscordException:
                pass
    session.discussion_type = None
    session.discussion_initiator_id = None
    session.discussion_allowed_user_ids.clear()
    for p in session.participants.values():
        p.discussion_message_id = None
    change_consensus_stage(
        session,
        "voting",
        "discussion_finished",
        actor_id=session.leader_id,
        actor_display=session.leader_display,
    )
    wake_operations_worker()
    await update_all_vote_dms(guild, session, content="Дискуссия завершена. Голосование снова открыто.")
    await update_host_vote_message(bot, guild, session)


async def pause_session(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession, reason: str, automatic: bool = False) -> None:
    if session.stage == "paused":
        return
    await cancel_vote_timer(session)
    session.previous_stage = session.stage
    session.paused_reason = reason
    session.pause_is_automatic = automatic
    change_consensus_stage(
        session,
        "paused",
        "session_paused_automatically" if automatic else "session_paused",
        actor_id=None if automatic else session.leader_id,
        actor_display=None if automatic else session.leader_display,
        details={"reason": reason},
    )
    wake_operations_worker()
    await update_all_vote_dms(guild, session, content=reason)
    await update_host_vote_message(bot, guild, session)


def session_voice_quorum_ready(guild: discord.Guild, session: LiveConsensusSession) -> tuple[bool, str]:
    channel = guild.get_channel(TVRS_CONSENSUS_VOICE_CHANNEL_ID)
    if not isinstance(channel, discord.VoiceChannel):
        return False, "Голосовой канал консенсуса не найден."
    voice_ids = {m.id for m in channel.members if not m.bot}
    confirmed = [p for p in session.confirmed_participants() if p.user_id in voice_ids]
    chairs = [p for p in confirmed if p.kind == "chair"]
    senators = [p for p in confirmed if p.kind == "senator"]
    ok = len(chairs) >= 2 and len(senators) >= 1 and len(senators) % 2 == 1
    if ok:
        return True, f"Кворум сохранен: председатели `{len(chairs)}`, сенаторы `{len(senators)}`."
    return False, f"Кворум утрачен: председатели `{len(chairs)}`, сенаторы `{len(senators)}`. Консенсус приостановлен."


async def check_realtime_quorum(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession) -> None:
    if session.finished or session.stage in {"registration", "after_result"}:
        return
    ok, reason = session_voice_quorum_ready(guild, session)
    if not ok and session.stage != "paused":
        await pause_session(bot, guild, session, reason, automatic=True)


async def resume_session(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession) -> None:
    ok, reason = session_voice_quorum_ready(guild, session)
    if not ok:
        await update_host_vote_message(bot, guild, session)
        return
    previous = session.previous_stage or "voting"
    target_stage = previous if previous != "paused" else "voting"
    session.paused_reason = None
    session.pause_is_automatic = False
    if target_stage in {"discussion_type", "discussion"}:
        content = "Кворум восстановлен. Дискуссия продолжается."
    else:
        target_stage = "voting" if session.current_bill else "after_result"
        content = "Кворум восстановлен. Голосование продолжается."
    change_consensus_stage(
        session,
        target_stage,
        "session_resumed",
        actor_id=session.leader_id,
        actor_display=session.leader_display,
    )
    wake_operations_worker()
    await update_all_vote_dms(guild, session, content=content)
    await update_host_vote_message(bot, guild, session)


async def requeue_current_bill_if_any(session: LiveConsensusSession) -> None:
    if session.current_bill:
        try:
            storage.tvrs_mark_bill_status(int(session.current_bill["id"]), "requeued", "Рассмотрение отложено из-за завершения/приостановки консенсуса.")
        except Exception:
            traceback.print_exc()
        session.current_bill = None
        session.votes = {}

async def send_confirmation_messages(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession) -> None:
    for p in session.participants.values():
        if p.user_id == session.leader_id:
            p.confirmed = True
            continue
        member = guild.get_member(p.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(p.user_id)
            except discord.DiscordException:
                p.dm_failed = True
                continue
        try:
            embed = discord.Embed(
                title="Пленарный консенсус Товарищества",
                description=f"Ведущий: <@{session.leader_id}>\nРоль: **{role_label(p)}**",
                color=TVRS_EMBED_COLOR,
                timestamp=now_local(),
            )
            embed.add_field(name="Очередь законопроектов", value=queue_lines(guild.id, limit=10), inline=False)
            dm = await member.send(
                content="Подтвердите участие в консенсусе.",
                embed=embed,
                view=TVRSConfirmView(session.session_key, p.user_id),
            )
            p.dm_message_id = dm.id
        except discord.DiscordException:
            p.dm_failed = True


async def update_host_registration_message(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession) -> None:
    if await edit_session_host_message(session, embed=build_registration_embed(session), view=TVRSRegistrationView(session.session_key)):
        return
    if not session.host_message_id:
        return
    channel = guild.get_channel(session.channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return
    try:
        msg = await channel.fetch_message(session.host_message_id)  # type: ignore[attr-defined]
        await msg.edit(embed=build_registration_embed(session), view=TVRSRegistrationView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    except discord.DiscordException:
        pass


async def update_host_vote_message(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession) -> None:
    if await edit_session_host_message(session, embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key)):
        return
    if not session.host_message_id:
        return
    channel = guild.get_channel(session.channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return
    try:
        msg = await channel.fetch_message(session.host_message_id)  # type: ignore[attr-defined]
        await msg.edit(embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    except discord.DiscordException:
        pass


async def begin_next_bill_vote(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession, channel) -> None:
    if session.finished or session.stage not in {"registration", "after_result"}:
        return
    await cancel_vote_timer(session)
    session.discussion_channel_id = None
    session.discussion_initiator_id = None
    session.discussion_type = None
    session.discussion_allowed_user_ids.clear()
    bills = storage.tvrs_queue_bills(guild.id, limit=1)
    if not bills:
        await finish_session(bot, guild, session, channel)
        return
    bill = bills[0]
    session.current_bill = bill
    session.votes = {}
    storage.tvrs_mark_bill_status(int(bill["id"]), "voting")
    change_consensus_stage(
        session,
        "voting",
        "bill_voting_started",
        actor_id=session.leader_id,
        actor_display=session.leader_display,
        details={
            "bill_id": int(bill["id"]),
            "bill_number": int(bill["bill_number"]),
        },
    )
    wake_operations_worker()

    for p in session.confirmed_participants():
        await edit_or_send_vote_dm(guild, session, p)

    if not await edit_session_host_message(session, embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key)):
        if session.host_message_id:
            try:
                msg = await channel.fetch_message(session.host_message_id)
                await msg.edit(embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
                return
            except discord.DiscordException:
                pass
        msg = await channel.send(embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        session.host_message_id = msg.id
        session.host_message_obj = msg
        persist_consensus_session(session, "host_panel_bound")


async def notify_participants(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession, embed: discord.Embed, content: str | None = None) -> None:
    for p in session.confirmed_participants():
        if p.user_id == session.leader_id:
            continue
        member = guild.get_member(p.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(p.user_id)
            except discord.DiscordException:
                continue
        try:
            await member.send(content=content, embed=embed)
        except discord.DiscordException:
            pass


async def finalize_current_vote(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession, forced: bool) -> None:
    if session.stage != "voting" or session.current_bill is None:
        return
    await cancel_vote_timer(session)
    bill = session.current_bill
    calc = calculate_consensus(session)
    status = "accepted" if calc["accepted"] else "rejected"
    result = LiveResult(
        bill_id=int(bill["id"]),
        bill_number=int(bill["bill_number"]),
        title=str(bill.get("title") or ""),
        status=status,
        internal_percent=float(calc["internal_percent"]),
        overall_percent=float(calc["overall_percent"]),
        internal_active=bool(calc["internal_active"]),
        votes=dict(session.votes),
    )
    session.results.append(result)
    votes_json = json.dumps({str(k): v for k, v in session.votes.items()}, ensure_ascii=False)
    storage.tvrs_save_live_result(
        guild_id=guild.id,
        session_key=session.session_key,
        plenary_number=session.plenary_number,
        bill_id=result.bill_id,
        bill_number=result.bill_number,
        bill_title=result.title,
        status=status,
        internal_percent=result.internal_percent,
        overall_percent=result.overall_percent,
        internal_active=result.internal_active,
        votes_json=votes_json,
    )
    storage.tvrs_mark_bill_status(result.bill_id, status, f"{result_status_text(status)} • общий консенсус {result.overall_percent}%")
    session.current_bill = None
    session.votes = {}
    change_consensus_stage(
        session,
        "after_result",
        "vote_finalized_manually" if forced else "vote_finalized",
        actor_id=session.leader_id if forced else None,
        actor_display=session.leader_display if forced else None,
        details={
            "bill_id": result.bill_id,
            "bill_number": result.bill_number,
            "status": status,
            "overall_percent": result.overall_percent,
        },
    )
    embed = build_result_embed(result, session)
    for p in session.confirmed_participants():
        await edit_vote_dm_to_result(guild, session, p, embed, content="Голосование по текущему законопроекту завершено.")
    wake_operations_worker()
    channel = guild.get_channel(session.channel_id)
    if isinstance(channel, discord.abc.Messageable):
        # Public result for observers.
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.DiscordException:
            pass
        if not await edit_session_host_message(session, embed=embed, view=TVRSAfterResultView(session.session_key)):
            if session.host_message_id:
                try:
                    msg = await channel.fetch_message(session.host_message_id)  # type: ignore[attr-defined]
                    await msg.edit(embed=embed, view=TVRSAfterResultView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
                    return
                except discord.DiscordException:
                    pass
            msg = await channel.send(embed=embed, view=TVRSAfterResultView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            session.host_message_id = msg.id


async def apply_veto(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession, user: discord.User | discord.Member) -> None:
    if session.stage != "voting" or session.current_bill is None:
        return
    await cancel_vote_timer(session)
    bill = session.current_bill
    retry = storage.tvrs_create_retry_bill(int(bill["id"]), user.id, getattr(user, "display_name", str(user)))
    if retry is not None:
        materials_channel = await get_materials_channel(bot, guild)
        if materials_channel is not None:
            try:
                msg = await materials_channel.send(embed=build_bill_embed(guild, retry), allowed_mentions=discord.AllowedMentions.none())
                storage.tvrs_set_bill_message(int(retry["id"]), msg.id, materials_channel.id)
            except discord.DiscordException:
                pass
    storage.tvrs_mark_bill_status(int(bill["id"]), "vetoed", "Применено право вето", user.id, getattr(user, "display_name", str(user)))
    result = LiveResult(
        bill_id=int(bill["id"]),
        bill_number=int(bill["bill_number"]),
        title=str(bill.get("title") or ""),
        status="vetoed",
        internal_percent=0.0,
        overall_percent=0.0,
        internal_active=False,
        votes=dict(session.votes),
        veto_by_id=user.id,
        retry_bill_number=(int(retry["bill_number"]) if retry else None),
    )
    session.results.append(result)
    storage.tvrs_save_live_result(
        guild_id=guild.id,
        session_key=session.session_key,
        plenary_number=session.plenary_number,
        bill_id=result.bill_id,
        bill_number=result.bill_number,
        bill_title=result.title,
        status="vetoed",
        internal_percent=0.0,
        overall_percent=0.0,
        internal_active=False,
        votes_json=json.dumps({str(k): v for k, v in session.votes.items()}, ensure_ascii=False),
        veto_by_id=user.id,
        veto_by_display=getattr(user, "display_name", str(user)),
    )
    session.current_bill = None
    session.votes = {}
    change_consensus_stage(
        session,
        "after_result",
        "veto_applied",
        actor_id=user.id,
        actor_display=getattr(user, "display_name", str(user)),
        details={
            "bill_id": result.bill_id,
            "bill_number": result.bill_number,
            "retry_bill_number": result.retry_bill_number,
        },
    )
    embed = build_result_embed(result, session)
    for p in session.confirmed_participants():
        await edit_vote_dm_to_result(guild, session, p, embed, content="Постоянный председатель применил право вето. Голосование отменено.")
    wake_operations_worker()
    channel = guild.get_channel(session.channel_id)
    if isinstance(channel, discord.abc.Messageable):
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.DiscordException:
            pass
        if not await edit_session_host_message(session, embed=embed, view=TVRSAfterResultView(session.session_key)):
            if session.host_message_id:
                try:
                    msg = await channel.fetch_message(session.host_message_id)  # type: ignore[attr-defined]
                    await msg.edit(embed=embed, view=TVRSAfterResultView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
                    return
                except discord.DiscordException:
                    pass
            msg = await channel.send(embed=embed, view=TVRSAfterResultView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            session.host_message_id = msg.id


async def finish_session(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession, channel) -> None:
    if session.finished:
        return
    await cancel_vote_timer(session)
    await requeue_current_bill_if_any(session)
    change_consensus_stage(
        session,
        "finished",
        "session_finished",
        actor_id=session.leader_id,
        actor_display=session.leader_display,
        details={"result_count": len(session.results)},
    )
    embed = build_final_summary_embed(session)
    try:
        if not await edit_session_host_message(session, embed=embed, view=None):
            if session.host_message_id:
                msg = await channel.fetch_message(session.host_message_id)
                await msg.edit(embed=embed, view=None, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            else:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    except discord.DiscordException:
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.DiscordException:
            pass
    output = guild.get_channel(TVRS_BILLS_CHANNEL_ID)
    if output is None:
        try:
            output = await bot.fetch_channel(TVRS_BILLS_CHANNEL_ID)  # type: ignore[attr-defined]
        except discord.DiscordException:
            output = None
    if hasattr(output, "send"):
        try:
            await output.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))  # type: ignore[attr-defined]
        except discord.DiscordException:
            pass
    await notify_participants(bot, guild, session, embed, content="Пленарный консенсус завершен.")
    storage.tvrs_increment_plenary_number(guild.id, session.plenary_number)
    await ensure_sticky_message(bot, guild, force_repost=True)
    _active_sessions.pop(guild.id, None)
    wake_operations_worker()


async def get_materials_channel(bot: commands.Bot | discord.Client, guild: discord.Guild | None = None) -> discord.TextChannel | None:
    channel = None
    if guild is not None:
        channel = guild.get_channel(TVRS_MATERIALS_CHANNEL_ID)
    if channel is None:
        channel = bot.get_channel(TVRS_MATERIALS_CHANNEL_ID)  # type: ignore[attr-defined]
    if channel is None:
        try:
            fetched = await bot.fetch_channel(TVRS_MATERIALS_CHANNEL_ID)  # type: ignore[attr-defined]
            channel = fetched if isinstance(fetched, discord.TextChannel) else None
        except discord.DiscordException:
            channel = None
    return channel if isinstance(channel, discord.TextChannel) else None


async def ensure_sticky_message(bot: commands.Bot | discord.Client, guild: discord.Guild, force_repost: bool = False) -> None:
    channel = await get_materials_channel(bot, guild)
    if channel is None:
        return
    lock = _sticky_locks.setdefault(channel.id, asyncio.Lock())
    async with lock:
        meta_key = f"tvrs_sticky_message_id:{guild.id}:{channel.id}"
        old_id_raw = storage.get_meta(meta_key)
        old_id = int(old_id_raw) if old_id_raw and old_id_raw.isdigit() else None
        if old_id:
            try:
                old_msg = await channel.fetch_message(old_id)
                if force_repost:
                    await old_msg.delete()
                else:
                    await old_msg.edit(embed=build_sticky_embed(guild), view=TVRSStickyView(), allowed_mentions=discord.AllowedMentions.none())
                    return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        msg = await channel.send(embed=build_sticky_embed(guild), view=TVRSStickyView(), allowed_mentions=discord.AllowedMentions.none())
        storage.set_meta_value(meta_key, str(msg.id))


def schedule_sticky_refresh(bot: commands.Bot, guild: discord.Guild) -> None:
    key = guild.id
    task = _sticky_tasks.get(key)
    if task and not task.done():
        task.cancel()

    async def runner() -> None:
        try:
            await asyncio.sleep(TVRS_STICKY_DEBOUNCE_SECONDS)
            await ensure_sticky_message(bot, guild, force_repost=True)
        except asyncio.CancelledError:
            return
        except Exception:
            traceback.print_exc()

    _sticky_tasks[key] = asyncio.create_task(runner())


def register_tvrs_persistent_views(bot: commands.Bot) -> None:
    bot.add_view(TVRSStickyView())
    bot.add_view(TVRSPublicPanelView())


async def tvrs_ensure_sticky_all(bot: commands.Bot) -> None:
    for guild in bot.guilds:
        try:
            await ensure_sticky_message(bot, guild, force_repost=False)
        except Exception:
            traceback.print_exc()


def setup_tvrs(bot: commands.Bot, remember_command_activity: Callable[[discord.Interaction, str, str], None]) -> None:
    @bot.listen("on_message")
    async def tvrs_on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            await forward_discussion_message(bot, message)
            return
        if message.channel.id != TVRS_MATERIALS_CHANNEL_ID:
            return
        active = _active_sessions.get(message.guild.id)
        if active and not active.finished:
            return
        schedule_sticky_refresh(bot, message.guild)

    @bot.listen("on_voice_state_update")
    async def tvrs_voice_quorum_listener(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if member.bot or member.guild is None:
            return
        if (getattr(before.channel, "id", None) != TVRS_CONSENSUS_VOICE_CHANNEL_ID and getattr(after.channel, "id", None) != TVRS_CONSENSUS_VOICE_CHANNEL_ID):
            return
        session = _active_sessions.get(member.guild.id)
        if session and not session.finished:
            await check_realtime_quorum(bot, member.guild, session)

    @bot.tree.command(name=TVRS_COMMAND_NAME, description=TVRS_COMMAND_DESCRIPTION)
    async def tvrs(interaction: discord.Interaction) -> None:
        remember_command_activity(interaction, "command_tvrs", "/tvrs")
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await asyncio.to_thread(build_universality_embed, interaction.guild, interaction.user.id)
        await interaction.followup.send(
            embed=embed,
            view=TVRSUniversalityView(interaction.user.id),
            ephemeral=True,
        )

    @bot.tree.command(name=TVRS_SETBILL_COMMAND_NAME, description=TVRS_SETBILL_COMMAND_DESCRIPTION)
    @app_commands.describe(last_accepted="Последний принятый номер. Например: 8, чтобы следующий был 009")
    async def tvrs_setbill(interaction: discord.Interaction, last_accepted: app_commands.Range[int, 0, 9999]) -> None:
        remember_command_activity(interaction, "command_tvrs_setbill", "/tvrs_setbill")
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        if not is_chair(interaction.user):
            await interaction.response.send_message("Команда доступна только председателю.", ephemeral=True)
            return
        next_number = storage.tvrs_set_last_accepted_bill_number(interaction.guild.id, int(last_accepted))
        await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)
        await interaction.response.send_message(f"Последний принятый законопроект: `{format_bill_number(int(last_accepted))}`. Следующий номер: `{format_bill_number(next_number)}`.", ephemeral=True)



    @bot.tree.command(name=TVRS_ADMIN_COMMAND_NAME, description=TVRS_ADMIN_COMMAND_DESCRIPTION)
    @app_commands.describe(target="bill/result/plenary", action="view/delete/edit", identifier="Номер проекта, ID результата или номер консенсуса", confirm="Для удаления напишите DELETE", field="Поле для edit", value="Новое значение")
    async def tvrs_admin(interaction: discord.Interaction, target: str, action: str, identifier: str, confirm: str | None = None, field: str | None = None, value: str | None = None) -> None:
        remember_command_activity(interaction, "command_tvrs_admin", "/tvrs_admin")
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        if not is_chair(interaction.user):
            await interaction.response.send_message("Команда доступна только председателю.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        target = target.strip().lower()
        action = action.strip().lower()
        if target in {"bill", "закон", "project", "проект"}:
            try:
                num = int(re.sub(r"\D", "", identifier) or "0")
            except ValueError:
                num = 0
            if action == "view":
                bill = storage.tvrs_get_bill_by_number(interaction.guild.id, num)
                if not bill:
                    await interaction.followup.send(f"Законопроект `{identifier}` не найден.", ephemeral=True)
                    return
                await interaction.followup.send(f"`{format_bill_number(bill['bill_number'])}` • **{bill.get('title','')}**\nСтатус: `{bill.get('status')}`\nID: `{bill.get('id')}`", ephemeral=True)
                return
            if action == "delete":
                if confirm != "DELETE":
                    await interaction.followup.send("Для удаления укажите `confirm: DELETE`.", ephemeral=True)
                    return
                bill = storage.tvrs_delete_bill_by_number(
                    interaction.guild.id,
                    num,
                    interaction.user.id,
                    getattr(interaction.user, "display_name", str(interaction.user)),
                )
                await interaction.followup.send((f"Законопроект `{format_bill_number(num)}` удален." if bill else "Законопроект не найден."), ephemeral=True)
                await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)
                return
            if action == "edit":
                if not field or value is None:
                    await interaction.followup.send("Для edit нужны `field` и `value`. Поля: title, summary, materials, status, result_summary.", ephemeral=True)
                    return
                try:
                    bill = storage.tvrs_update_bill_field(
                        interaction.guild.id,
                        num,
                        field.strip(),
                        value,
                        interaction.user.id,
                        getattr(interaction.user, "display_name", str(interaction.user)),
                    )
                except Exception as exc:
                    await interaction.followup.send(f"Ошибка изменения: `{str(exc)[:300]}`", ephemeral=True)
                    return
                await interaction.followup.send((f"Законопроект `{format_bill_number(num)}` обновлен." if bill else "Законопроект не найден."), ephemeral=True)
                return
        if target in {"result", "итог"}:
            rid = int(re.sub(r"\D", "", identifier) or "0")
            if action == "delete":
                if confirm != "DELETE":
                    await interaction.followup.send("Для удаления укажите `confirm: DELETE`.", ephemeral=True)
                    return
                row = storage.tvrs_delete_live_result(
                    rid,
                    interaction.guild.id,
                    interaction.user.id,
                    getattr(interaction.user, "display_name", str(interaction.user)),
                )
                await interaction.followup.send((f"Итог голосования ID `{rid}` удален." if row else "Итог не найден."), ephemeral=True)
                return
        if target in {"plenary", "consensus", "консенсус"}:
            pn = int(re.sub(r"\D", "", identifier) or "0")
            if action == "delete":
                if confirm != "DELETE":
                    await interaction.followup.send("Для удаления укажите `confirm: DELETE`.", ephemeral=True)
                    return
                count = storage.tvrs_delete_plenary_results(
                    interaction.guild.id,
                    pn,
                    interaction.user.id,
                    getattr(interaction.user, "display_name", str(interaction.user)),
                )
                await interaction.followup.send(f"Удалено итогов консенсуса №`{pn}`: `{count}`.", ephemeral=True)
                return
        await interaction.followup.send("Неизвестная команда администратора. Используйте target: bill/result/plenary и action: view/delete/edit.", ephemeral=True)

    @bot.tree.command(name=TVRS_STICKY_COMMAND_NAME, description=TVRS_STICKY_COMMAND_DESCRIPTION)
    async def tvrs_sticky(interaction: discord.Interaction) -> None:
        remember_command_activity(interaction, "command_tvrs_sticky", "/tvrs_sticky")
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        if not is_chair(interaction.user):
            await interaction.response.send_message("Команда доступна только председателю.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)
        await interaction.followup.send(f"Сообщение подачи законопроектов обновлено в <#{TVRS_MATERIALS_CHANNEL_ID}>.", ephemeral=True)
