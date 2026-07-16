import asyncio
import re
import traceback
import uuid
from datetime import datetime, timezone, timedelta
from typing import Callable, Any

import discord
from discord import app_commands
from discord.ext import commands

import storage
from modules.consensus_core import (
    ConsensusStateError,
    DEFAULT_CONSENSUS_RULES,
    LiveConsensusSession,
    LiveParticipant,
    LiveResult,
    calculate_consensus as calculate_consensus_v2,
)
from modules.consensus_runtime import (
    active_consensus_snapshot,
    active_sessions as _active_sessions,
    coordinator as _consensus,
    persist_session as persist_consensus_session,
    registry as _consensus_registry,
    repository as _consensus_repository,
    restored_guilds as _restored_consensus_guilds,
    session_lock as consensus_session_lock,
)
from modules.consensus_service import ConsensusActor
from modules.control_center_config import ACTIVE_TASKS_CHANNEL_ID
from modules.delivery_runtime import register_delivery_handler, wake_delivery_worker
from modules.delivery_outbox import DeliveryReceipt, OutboxMessage
from modules.hub_runtime import open_hub_section
from modules.operations_runtime import wake_operations_worker
from modules.public_panel_runtime import register_public_panel_provider
from modules.tvrs_config import (
    LOCAL_TZ,
    TVRS_ADMIN_COMMAND_DESCRIPTION,
    TVRS_ADMIN_COMMAND_NAME,
    TVRS_BILLS_CHANNEL_ID,
    TVRS_CHAIR_ROLE_ID,
    TVRS_COMMAND_DESCRIPTION,
    TVRS_COMMAND_NAME,
    TVRS_CONSENSUS_VOICE_CHANNEL_ID,
    TVRS_DEFAULT_NEXT_BILL_NUMBER,
    TVRS_DEFAULT_NEXT_PLENARY_NUMBER,
    TVRS_DISCUSSION_CATEGORY_ID,
    TVRS_EMBED_COLOR,
    TVRS_MATERIALS_CHANNEL_ID,
    TVRS_PERMANENT_CHAIR_ID,
    TVRS_SENATOR_ROLE_ID,
    TVRS_SETBILL_COMMAND_DESCRIPTION,
    TVRS_SETBILL_COMMAND_NAME,
    TVRS_STICKY_COMMAND_DESCRIPTION,
    TVRS_STICKY_COMMAND_NAME,
    TVRS_STICKY_DEBOUNCE_SECONDS,
    TVRS_TIMER_OPTIONS,
    env_color,
    env_int,
)
from modules.tvrs_embeds import (
    bill_attempt_text,
    build_bill_embed,
    build_discussion_embed,
    build_final_summary_embed,
    build_paused_embed,
    build_result_embed,
)
from modules.tvrs_formatting import (
    clean_stage_name,
    clip_text,
    format_bill_number,
    format_dt,
    format_timer,
    materials_text,
    now_local,
    progress_bar,
    remaining_timer_text,
    result_status_text,
    role_label,
    ru_ordinal,
    status_icon,
    vote_label,
    vote_progress,
    vote_split_lines,
)
from modules.tvrs_navigation_runtime import open_tvrs_hub, register_tvrs_hub_handler
from modules.tvrs_delivery import (
    TVRS_CONTROL_DM_TOPIC,
    TVRS_BILL_PUBLICATION_TOPIC,
    TVRS_RETRY_BILL_TOPIC,
    TVRS_RESULT_TOPIC,
    TVRS_SESSION_SUMMARY_TOPIC,
    build_control_dm_deliveries,
    build_retry_bill_delivery,
    build_result_deliveries,
    build_session_summary_deliveries,
    delivery_marker,
    find_delivery_marker,
    make_retry_bill_delivery_handler,
    make_result_delivery_handler,
    make_session_summary_delivery_handler,
)
from modules.technical_log import log_technical_event


_sticky_locks: dict[int, asyncio.Lock] = {}
_sticky_tasks: dict[int, asyncio.Task] = {}
_finalization_retry_tasks: dict[str, asyncio.Task] = {}
_consensus_recovery_tasks: dict[int, asyncio.Task] = {}


def consensus_bill_id(session: LiveConsensusSession) -> int:
    return int((session.current_bill or {}).get("id") or 0)


def consensus_result_bill_id(session: LiveConsensusSession) -> int:
    return int(session.results[-1].bill_id) if session.results else 0


def consensus_generation_matches(
    session: LiveConsensusSession,
    *,
    stage: str | None = None,
    bill_id: int | None = None,
    result_bill_id: int | None = None,
) -> bool:
    """Fence a Discord view to the lifecycle generation that created it."""

    if session.finished:
        return False
    if stage is not None and session.stage != str(stage):
        return False
    if bill_id is not None and consensus_bill_id(session) != int(bill_id):
        return False
    if result_bill_id is not None and consensus_result_bill_id(session) != int(result_bill_id):
        return False
    return True


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
    return clip_text("\n".join(lines), 1000)


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
        embed.add_field(name="Ближайшие к рассмотрению", value=clip_text("\n".join(blocks), 1000), inline=False)
    embed.set_footer(text="TVRS • очередь пленарного консенсуса")
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


async def _open_tvrs_hub_impl(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
        return
    await interaction.response.defer()
    embed = await asyncio.to_thread(build_universality_embed, interaction.guild, interaction.user.id)
    await interaction.edit_original_response(
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
    return clip_text("\n".join(lines), 1000, "Нет участников.")


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
    parity = "нечётное количество" if session.rules.senators_must_be_odd else "любое количество"
    embed.set_footer(
        text=(
            f"Правила v{session.rules.version} • минимум {session.rules.minimum_chairs} председателя "
            f"и {parity} сенаторов"
        )
    )
    return embed


def calculate_consensus(session: LiveConsensusSession) -> dict:
    return calculate_consensus_v2(session)


def vote_lines(session: LiveConsensusSession) -> str:
    lines = []
    for p in sorted(session.confirmed_participants(), key=lambda x: (x.kind != "chair", x.display_name.lower())):
        lines.append(f"{p.mention} — `{role_label(p)}` — **{vote_label(session.votes.get(p.user_id))}**")
    return clip_text("\n".join(lines), 1000, "Голосов пока нет.")


def build_live_vote_embed(session: LiveConsensusSession) -> discord.Embed:
    if session.stage == "paused":
        return build_paused_embed(session)
    if session.stage in {"discussion_type", "discussion"}:
        return build_discussion_embed(session)
    if session.stage == "finalizing":
        bill = session.current_bill or {}
        embed = discord.Embed(
            title="⏳ Результат фиксируется",
            description=f"**{clip_text(bill.get('title'), 220)}**",
            color=TVRS_EMBED_COLOR,
            timestamp=now_local(),
        )
        embed.add_field(
            name="Что происходит",
            value="Голоса заблокированы. Бот сохраняет итог и обновляет карточки участников.",
            inline=False,
        )
        embed.set_footer(text="Повторное нажатие не создаст второй результат")
        return embed
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
    embed.add_field(
        name="🌐 Общий консенсус",
        value=(
            f"`{calc['overall_percent']}%` {progress_bar(float(calc['overall_percent']), 10)}\n"
            f"порог принятия: `{calc['acceptance_percent']}%`"
        ),
        inline=True,
    )
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
    if session.stage in {"discussion_type", "discussion"}:
        embed = build_discussion_embed(session)
        embed.add_field(name="Ваш статус", value="На время дискуссии кнопки голосования скрыты.", inline=False)
        return embed
    if session.stage == "finalizing":
        embed = build_live_vote_embed(session)
        embed.add_field(name="Ваш статус", value="Голос принят. Ожидайте итог.", inline=False)
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
    if len(chairs) < DEFAULT_CONSENSUS_RULES.minimum_chairs:
        return items, (
            "Минимальный кворум не набран: нужно минимум "
            f"{DEFAULT_CONSENSUS_RULES.minimum_chairs} председателя, сейчас `{len(chairs)}`."
        )
    if len(senators) < DEFAULT_CONSENSUS_RULES.minimum_senators or (
        DEFAULT_CONSENSUS_RULES.senators_must_be_odd and len(senators) % 2 == 0
    ):
        return items, f"Минимальный кворум не набран: нужно нечетное количество сенаторов, сейчас `{len(senators)}`."
    return items, None


class TVRSBaseView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: Any) -> None:
        expected = isinstance(error, ConsensusStateError)
        if not expected:
            traceback.print_exception(type(error), error, error.__traceback__)
        message = str(error) if expected else "Внутренняя ошибка действия. Попробуйте ещё раз или обновите панель /tvrs."
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
            else:
                await interaction.followup.send(message, ephemeral=True)
        except discord.DiscordException:
            pass
        if not expected and interaction.guild is not None:
            await log_technical_event(
                interaction.client,
                interaction.guild,
                title="Ошибка интерфейса TVRS",
                details=f"Элемент: `{getattr(item, 'custom_id', None) or getattr(item, 'label', 'неизвестно')}`\nОшибка: `{type(error).__name__}: {str(error)[:700]}`",
                dedupe_key=f"tvrs-view:{type(error).__name__}",
                cooldown_seconds=60,
            )


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
        await open_hub_section("finance", interaction, self.requester_id, surface="replace")

    @discord.ui.button(label="Крафты", emoji="🏭", style=discord.ButtonStyle.success, row=0)
    async def craft(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_hub_section("craft", interaction, self.requester_id, surface="replace")

    @discord.ui.button(label="Аудит", emoji="🧾", style=discord.ButtonStyle.secondary, row=0)
    async def audit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_hub_section("audit", interaction, self.requester_id, surface="replace")

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
        await open_hub_section("market", interaction, self.requester_id, surface="replace")

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
        await open_hub_section("finance", interaction, interaction.user.id, surface="ephemeral")

    @discord.ui.button(
        label="Крафты",
        emoji="🏭",
        style=discord.ButtonStyle.success,
        row=0,
        custom_id="tmod_public_tvrs_craft",
    )
    async def craft(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_hub_section("craft", interaction, interaction.user.id, surface="ephemeral")

    @discord.ui.button(
        label="Аудит",
        emoji="🧾",
        style=discord.ButtonStyle.secondary,
        row=0,
        custom_id="tmod_public_tvrs_audit",
    )
    async def audit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_hub_section("audit", interaction, interaction.user.id, surface="ephemeral")

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
        await open_hub_section("market", interaction, interaction.user.id, surface="ephemeral")

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
            async with consensus_session_lock(interaction.guild.id):
                active = _active_sessions.get(interaction.guild.id)
                if active and not active.finished:
                    await interaction.followup.send(
                        "Сейчас идёт пленарный консенсус. Подача новых законопроектов будет снова доступна после завершения.",
                        ephemeral=True,
                    )
                    return
                bill, _, created = await asyncio.to_thread(
                    storage.tvrs_create_bill_with_publication,
                    guild_id=interaction.guild.id,
                    channel_id=interaction.channel_id,
                    author_id=interaction.user.id,
                    author_display=interaction.user.display_name,
                    title=str(self.heading.value).strip(),
                    summary=str(self.summary.value).strip(),
                    materials=str(self.materials.value).strip() or None,
                    delivery_topic=TVRS_BILL_PUBLICATION_TOPIC,
                )
            wake_delivery_worker()
            await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)
            state = "создан и поставлен в надёжную очередь публикации" if created else "уже был создан; публикация продолжится автоматически"
            await interaction.followup.send(
                f"Законопроект **{format_bill_number(bill.bill_number)}** {state}.",
                ephemeral=True,
            )
        except ValueError as exc:
            if str(exc) == "bill_submission_locked_by_active_consensus":
                await interaction.followup.send(
                    "Сейчас идёт пленарный консенсус. Подача новых законопроектов будет снова доступна после завершения.",
                    ephemeral=True,
                )
                return
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            await interaction.followup.send(f"Не удалось создать законопроект: {str(exc)[:800]}", ephemeral=True)
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
        await interaction.response.defer()
        previous_host = None
        async with consensus_session_lock(interaction.guild.id):
            current = _consensus_registry.find(session.session_key)
            if current is not session or session.finished:
                embed = None
                view = None
            else:
                previous_host = session.host_message_obj
                session.host_message_obj = interaction.message
                await asyncio.to_thread(
                    _consensus.bind_host_message,
                    session,
                    getattr(interaction.message, "id", None),
                    "host_panel_reopened",
                )
                if session.stage == "registration":
                    embed = build_registration_embed(session)
                    view = TVRSRegistrationView(session.session_key)
                elif session.stage == "after_result" and session.results:
                    embed = build_result_embed(session.results[-1], session)
                    view = TVRSAfterResultView(session.session_key)
                else:
                    embed = build_live_vote_embed(session)
                    view = TVRSHostVoteView(session.session_key)
        if embed is None:
            await interaction.edit_original_response(
                content="Заседание уже завершено или было заменено новым.",
                embed=None,
                view=None,
            )
            return
        if previous_host is not None and previous_host is not interaction.message and hasattr(previous_host, "edit"):
            try:
                await previous_host.edit(view=None)
            except Exception:
                pass
        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

    @discord.ui.button(label="Начать консенсус", style=discord.ButtonStyle.secondary)
    async def start_consensus(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
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
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with consensus_session_lock(interaction.guild.id):
            if interaction.guild.id in _active_sessions and not _active_sessions[interaction.guild.id].finished:
                await interaction.followup.send("На сервере уже идет пленарный консенсус.", ephemeral=True)
                return
            plenary = await asyncio.to_thread(
                storage.tvrs_get_next_plenary_number,
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
            _consensus_registry.add(session)
            try:
                deliveries = build_control_dm_deliveries(session, phase="registration")
                await asyncio.to_thread(
                    _consensus.save_with_deliveries,
                    session,
                    "session_created",
                    actor=ConsensusActor(interaction.user.id, interaction.user.display_name),
                    details={"participant_count": len(session.participants)},
                    deliveries=deliveries,
                )
            except Exception:
                _consensus_registry.remove(interaction.guild.id, session_key=session.session_key)
                raise
        wake_operations_worker()
        await delete_sticky_message(interaction.client, interaction.guild)
        msg = await interaction.followup.send(embed=build_registration_embed(session), view=TVRSRegistrationView(session.session_key), ephemeral=True, wait=True, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        host_bound = False
        async with consensus_session_lock(interaction.guild.id):
            current = _consensus_registry.find(session.session_key)
            if current is session and not session.finished and session.stage == "registration":
                session.host_message_obj = msg
                await asyncio.to_thread(
                    _consensus.bind_host_message,
                    session,
                    getattr(msg, "id", None),
                    "host_panel_bound",
                )
                host_bound = True
        if not host_bound:
            try:
                await msg.edit(view=None)
            except discord.DiscordException:
                pass
            return
        wake_delivery_worker()
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
        if session.stage != "registration":
            await interaction.response.send_message("Эта панель регистрации уже устарела.", ephemeral=True)
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
        await begin_next_bill_vote(
            interaction.client,
            interaction.guild,
            session,
            interaction.channel,
            expected_stage="registration",
        )

    @discord.ui.button(label="Отменить", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session is None:
            await interaction.response.send_message("Сессия консенсуса не найдена.", ephemeral=True)
            return
        await interaction.response.defer()
        async with consensus_session_lock(interaction.guild.id):
            current = self.session(interaction.guild.id)
            if current is None or current.stage != "registration":
                await interaction.followup.send("Сессия уже завершена.", ephemeral=True)
                return
            await asyncio.to_thread(
                _consensus.finish_atomically,
                current,
                actor=ConsensusActor(
                    interaction.user.id,
                    getattr(interaction.user, "display_name", str(interaction.user)),
                ),
                cancelled=True,
            )
            _consensus_registry.remove(interaction.guild.id, session_key=current.session_key)
        wake_operations_worker()
        await interaction.edit_original_response(content="Консенсус отменен.", embed=None, view=None)
        await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)

    @discord.ui.button(label="Повторить приглашения", style=discord.ButtonStyle.secondary)
    async def resend_invitations(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session is None:
            await interaction.response.send_message("Сессия консенсуса не найдена.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with consensus_session_lock(session.guild_id):
            if session.stage != "registration":
                await interaction.followup.send("Регистрация уже завершена.", ephemeral=True)
                return
            deliveries = build_control_dm_deliveries(
                session,
                phase="registration",
                generation=f"manual-{session.revision + 1}",
            )
            await asyncio.to_thread(
                _consensus.save_with_deliveries,
                session,
                "registration_invitations_retried",
                actor=ConsensusActor(
                    interaction.user.id,
                    getattr(interaction.user, "display_name", str(interaction.user)),
                ),
                deliveries=deliveries,
            )
        wake_delivery_worker()
        await update_host_registration_message(interaction.client, interaction.guild, session)
        await interaction.followup.send("Приглашения поставлены в надёжную очередь доставки.", ephemeral=True)


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
        await interaction.response.defer()
        async with consensus_session_lock(session.guild_id):
            if session.stage != "registration":
                await interaction.followup.send("Регистрация уже завершена.", ephemeral=True)
                return
            await asyncio.to_thread(
                _consensus.confirm_participant,
                session,
                self.user_id,
                actor=ConsensusActor(
                    interaction.user.id,
                    getattr(interaction.user, "display_name", str(interaction.user)),
                ),
            )
        await interaction.edit_original_response(content="Участие подтверждено.", embed=None, view=None)
        guild = interaction.client.get_guild(session.guild_id)
        if guild:
            await update_host_registration_message(interaction.client, guild, session)


class TVRSVoteView(TVRSBaseView):
    def __init__(
        self,
        session_key: str,
        user_id: int,
        host_panel: bool = False,
        *,
        bill_id: int | None = None,
        _include_legacy_custom_ids: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.session_key = session_key
        self.user_id = user_id
        self.host_panel = host_panel
        session = self.session()
        self.bill_id = int(bill_id if bill_id is not None else (consensus_bill_id(session) if session else 0))
        participant = session.participants.get(user_id) if session else None
        if session and participant and session.stage == "voting":
            controls = [
                ("За", discord.ButtonStyle.success, "tvrs_vote_yes", self._yes_callback),
                ("Против", discord.ButtonStyle.danger, "tvrs_vote_no", self._no_callback),
            ]
            if participant.kind == "senator" and not session.discussion_initiator_id:
                controls.append(
                    ("Дискуссия", discord.ButtonStyle.secondary, "tvrs_discussion", self._discussion_callback)
                )
            if participant.permanent:
                controls.append(("Вето!", discord.ButtonStyle.danger, "tvrs_veto_dm", self._veto_callback))

            generations = [(False, 0)]
            if _include_legacy_custom_ids:
                # Recovery-only compatibility: a pre-deploy DM can still carry
                # the old IDs, while every newly rendered control is fenced by
                # bill_id. Both generations live in one registration View so
                # discord.py tracks either on the existing message safely.
                generations.append((True, 1))
            for legacy, row in generations:
                suffix = (
                    f"{session_key}:{user_id}"
                    if legacy
                    else f"{session_key}:{self.bill_id}:{user_id}"
                )
                for label, style, prefix, callback in controls:
                    button = discord.ui.Button(
                        label=label,
                        style=style,
                        custom_id=f"{prefix}:{suffix}",
                        row=row,
                    )
                    # A legacy ID has no bill generation and therefore can
                    # never be allowed to mutate the restored current bill.
                    # Its only job is to replace the old controls with v2.
                    button.callback = self._legacy_callback if legacy else callback  # type: ignore[assignment]
                    self.add_item(button)

    def session(self) -> LiveConsensusSession | None:
        return next((s for s in _active_sessions.values() if s.session_key == self.session_key), None)

    async def _yes_callback(self, interaction: discord.Interaction) -> None:
        await self._cast(interaction, "yes")

    async def _no_callback(self, interaction: discord.Interaction) -> None:
        await self._cast(interaction, "no")

    async def _legacy_callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Эта кнопка не для вас.", ephemeral=True)
            return
        session = self.session()
        participant = session.participants.get(self.user_id) if session else None
        if (
            session is None
            or session.stage != "voting"
            or session.current_bill is None
            or participant is None
            or not participant.confirmed
        ):
            await interaction.response.send_message(
                "Эта панель устарела. Откройте актуальное сообщение голосования.",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            content="Панель обновлена. Теперь выберите позицию ещё раз.",
            embed=build_dm_vote_embed(session, participant),
            view=TVRSVoteView(
                session.session_key,
                self.user_id,
                bill_id=consensus_bill_id(session),
            ),
        )

    async def _discussion_callback(self, interaction: discord.Interaction) -> None:
        session = self.session()
        if session is None or not consensus_generation_matches(
            session,
            stage="voting",
            bill_id=self.bill_id,
        ):
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
        await request_discussion(
            interaction.client,
            guild,
            session,
            p,
            expected_bill_id=self.bill_id,
        )
        try:
            await interaction.followup.send("Дискуссия инициирована. Выберите тип дискуссии ниже.", view=TVRSDiscussionTypeView(session.session_key, self.user_id, bill_id=self.bill_id), ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send("Дискуссия инициирована. Выберите тип дискуссии ниже.", view=TVRSDiscussionTypeView(session.session_key, self.user_id, bill_id=self.bill_id))

    async def _veto_callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Эта кнопка не для вас.", ephemeral=True)
            return
        if interaction.user.id != TVRS_PERMANENT_CHAIR_ID:
            await interaction.response.send_message("Право вето доступно только постоянному председателю.", ephemeral=True)
            return
        session = self.session()
        if session is None or not consensus_generation_matches(session, stage="voting", bill_id=self.bill_id):
            await interaction.response.send_message("Эта кнопка относится к уже завершённому проекту.", ephemeral=True)
            return
        await interaction.response.send_message("Подтвердите применение права вето.", ephemeral=True, view=TVRSVetoConfirmView(self.session_key, interaction.user.id, bill_id=self.bill_id))

    async def _cast(self, interaction: discord.Interaction, vote: str) -> None:
        session = self.session()
        if session is None or not consensus_generation_matches(
            session,
            stage="voting",
            bill_id=self.bill_id,
        ):
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
        await interaction.response.defer()
        async with consensus_session_lock(session.guild_id):
            if not consensus_generation_matches(session, stage="voting", bill_id=self.bill_id):
                await interaction.followup.send("Эта кнопка относится к уже завершённому проекту.", ephemeral=True)
                return
            should_finalize = await asyncio.to_thread(
                _consensus.cast_vote,
                session,
                self.user_id,
                vote,
                actor=ConsensusActor(
                    interaction.user.id,
                    getattr(interaction.user, "display_name", str(interaction.user)),
                ),
            )
        try:
            await interaction.message.edit(embed=build_dm_vote_embed(session, session.participants[self.user_id]), view=TVRSVoteView(session.session_key, self.user_id, bill_id=self.bill_id))  # type: ignore[union-attr]
        except discord.DiscordException:
            pass
        guild = interaction.client.get_guild(session.guild_id)
        if guild:
            await update_host_vote_message(interaction.client, guild, session)
        if should_finalize and guild:
            await finalize_current_vote(
                interaction.client,
                guild,
                session,
                forced=False,
                expected_bill_id=self.bill_id,
            )


class TVRSPermanentVoteView(TVRSVoteView):
    pass


class TVRSRestoredVoteView(TVRSVoteView):
    """Persistent registration adapter for both pre-v2 and v2 vote DMs."""

    def __init__(self, session_key: str, user_id: int, *, bill_id: int) -> None:
        super().__init__(
            session_key,
            user_id,
            bill_id=bill_id,
            _include_legacy_custom_ids=True,
        )


class TVRSDiscussionTypeView(TVRSBaseView):
    def __init__(self, session_key: str, user_id: int, *, bill_id: int) -> None:
        super().__init__(timeout=300)
        self.session_key = session_key
        self.user_id = user_id
        self.bill_id = int(bill_id)
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
            if not consensus_generation_matches(session, stage="discussion_type", bill_id=self.bill_id):
                await interaction.response.send_message("Этот выбор относится к уже завершённому проекту.", ephemeral=True)
                return
            guild = interaction.client.get_guild(session.guild_id)
            if guild is None:
                await interaction.response.send_message("Сервер не найден.", ephemeral=True)
                return
            await interaction.response.defer()
            await start_discussion_channel(
                interaction.client,
                guild,
                session,
                label,
                expected_bill_id=self.bill_id,
            )
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
        self.expected_stage = str(session.stage) if session else ""
        self.bill_id = consensus_bill_id(session) if session else 0
        if session and session.stage == "finalizing":
            self._add_button("Повторить фиксацию", discord.ButtonStyle.primary, self.retry_finalization, row=0)
            return
        if session and session.stage == "paused":
            self._add_button("Продолжить", discord.ButtonStyle.success, self.resume, row=0)
            self._add_button("Завершить консенсус", discord.ButtonStyle.danger, self.finish_session_btn, row=0)
            return
        if session and session.stage in {"discussion_type", "discussion"}:
            control_row = 0
            if session.stage == "discussion_type":
                for label in ["Правовая", "Фактическая", "Процедурная", "Иная"]:
                    self._add_button(
                        label,
                        discord.ButtonStyle.secondary,
                        self._discussion_type_callback(label),
                        row=0,
                    )
                control_row = 1
            self._add_button("Завершить дискуссию", discord.ButtonStyle.success, self.end_discussion, row=control_row)
            self._add_button("Пауза", discord.ButtonStyle.secondary, self.pause, row=control_row)
            self._add_button("Завершить консенсус", discord.ButtonStyle.danger, self.finish_session_btn, row=control_row)
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
        if not consensus_generation_matches(
            session,
            stage=self.expected_stage,
            bill_id=self.bill_id,
        ):
            await interaction.response.send_message("Эта панель относится к уже завершённому этапу.", ephemeral=True)
            return False
        return True

    async def host_yes(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session is None:
            await interaction.response.send_message("Сессия не найдена.", ephemeral=True)
            return
        if not consensus_generation_matches(session, stage="voting", bill_id=self.bill_id):
            await interaction.response.send_message("Сейчас голосование недоступно.", ephemeral=True)
            return
        await interaction.response.defer()
        async with consensus_session_lock(session.guild_id):
            if not consensus_generation_matches(session, stage="voting", bill_id=self.bill_id):
                await interaction.followup.send("Эта панель относится к уже завершённому проекту.", ephemeral=True)
                return
            should_finalize = await asyncio.to_thread(
                _consensus.cast_vote,
                session,
                session.leader_id,
                "yes",
                actor=ConsensusActor(
                    interaction.user.id,
                    getattr(interaction.user, "display_name", str(interaction.user)),
                ),
            )
        await interaction.edit_original_response(embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        if should_finalize:
            await finalize_current_vote(interaction.client, interaction.guild, session, forced=False, expected_bill_id=self.bill_id)

    async def host_no(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session is None:
            await interaction.response.send_message("Сессия не найдена.", ephemeral=True)
            return
        if not consensus_generation_matches(session, stage="voting", bill_id=self.bill_id):
            await interaction.response.send_message("Сейчас голосование недоступно.", ephemeral=True)
            return
        await interaction.response.defer()
        async with consensus_session_lock(session.guild_id):
            if not consensus_generation_matches(session, stage="voting", bill_id=self.bill_id):
                await interaction.followup.send("Эта панель относится к уже завершённому проекту.", ephemeral=True)
                return
            should_finalize = await asyncio.to_thread(
                _consensus.cast_vote,
                session,
                session.leader_id,
                "no",
                actor=ConsensusActor(
                    interaction.user.id,
                    getattr(interaction.user, "display_name", str(interaction.user)),
                ),
            )
        await interaction.edit_original_response(embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        if should_finalize:
            await finalize_current_vote(interaction.client, interaction.guild, session, forced=False, expected_bill_id=self.bill_id)

    async def finish(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            await finalize_current_vote(
                interaction.client,
                interaction.guild,
                session,
                forced=True,
                expected_bill_id=self.bill_id,
            )

    async def retry_finalization(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session is None or not consensus_generation_matches(
            session,
            stage="finalizing",
            bill_id=self.bill_id,
        ):
            await interaction.response.send_message("Фиксация уже завершена.", ephemeral=True)
            return
        await interaction.response.defer()
        await retry_pending_finalization_once(interaction.client, interaction.guild, session)
        if session.stage == "after_result" and session.results:
            await interaction.edit_original_response(
                embed=build_result_embed(session.results[-1], session),
                view=TVRSAfterResultView(session.session_key),
            )

    def _timer_callback(self, seconds: int):
        async def callback(interaction: discord.Interaction) -> None:
            assert interaction.guild is not None
            session = self.session(interaction.guild.id)
            if session is None or not consensus_generation_matches(session, stage="voting", bill_id=self.bill_id):
                await interaction.response.send_message("Таймер можно поставить только во время голосования.", ephemeral=True)
                return
            await interaction.response.defer()
            await set_vote_timer(
                interaction.client,
                interaction.guild,
                session,
                seconds,
                expected_bill_id=self.bill_id,
            )
            await interaction.followup.send(f"Таймер установлен: **{format_timer(seconds)}**.", ephemeral=True)
        return callback

    def _discussion_type_callback(self, label: str):
        async def callback(interaction: discord.Interaction) -> None:
            assert interaction.guild is not None
            session = self.session(interaction.guild.id)
            if session is None or not consensus_generation_matches(
                session,
                stage="discussion_type",
                bill_id=self.bill_id,
            ):
                await interaction.response.send_message("Тип дискуссии уже выбран.", ephemeral=True)
                return
            await interaction.response.defer()
            await start_discussion_channel(
                interaction.client,
                interaction.guild,
                session,
                label,
                expected_bill_id=self.bill_id,
            )
            await interaction.followup.send(f"Дискуссия типа **{label}** начата.", ephemeral=True)

        return callback

    async def pause(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            await pause_session(
                interaction.client,
                interaction.guild,
                session,
                "Консенсус приостановлен ведущим.",
                automatic=False,
                expected_stage=self.expected_stage,
                expected_bill_id=self.bill_id,
            )

    async def resume(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            await resume_session(
                interaction.client,
                interaction.guild,
                session,
                expected_stage=self.expected_stage,
                expected_bill_id=self.bill_id,
            )

    async def finish_session_btn(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            await finish_session(
                interaction.client,
                interaction.guild,
                session,
                interaction.channel,
                expected_stage=self.expected_stage,
                expected_bill_id=self.bill_id,
            )

    async def end_discussion(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            await end_discussion(
                interaction.client,
                interaction.guild,
                session,
                expected_stage=self.expected_stage,
                expected_bill_id=self.bill_id,
            )

    async def veto(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session is None:
            await interaction.response.send_message("Сессия не найдена.", ephemeral=True)
            return
        if interaction.user.id != TVRS_PERMANENT_CHAIR_ID:
            await interaction.response.send_message("Право вето доступно только постоянному председателю.", ephemeral=True)
            return
        if not consensus_generation_matches(session, stage="voting", bill_id=self.bill_id):
            await interaction.response.send_message("Эта панель относится к уже завершённому проекту.", ephemeral=True)
            return
        await interaction.response.send_message("Подтвердите применение права вето.", ephemeral=True, view=TVRSVetoConfirmView(session.session_key, interaction.user.id, bill_id=self.bill_id))


class TVRSVetoConfirmView(TVRSBaseView):
    def __init__(self, session_key: str, user_id: int, *, bill_id: int) -> None:
        super().__init__(timeout=120)
        self.session_key = session_key
        self.user_id = user_id
        self.bill_id = int(bill_id)

    @discord.ui.button(label="Подтвердить вето", style=discord.ButtonStyle.danger)
    async def confirm_veto(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это подтверждение не для вас.", ephemeral=True)
            return
        session = next((s for s in _active_sessions.values() if s.session_key == self.session_key), None)
        if session is None or not consensus_generation_matches(session, stage="voting", bill_id=self.bill_id):
            await interaction.response.send_message("Сессия не найдена.", ephemeral=True)
            return
        guild = interaction.client.get_guild(session.guild_id)
        if guild is None:
            await interaction.response.send_message("Сервер не найден.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await apply_veto(
                interaction.client,
                guild,
                session,
                interaction.user,
                expected_bill_id=self.bill_id,
            )
        except ConsensusStateError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send("Право вето применено.", ephemeral=True)


class TVRSAfterResultView(TVRSBaseView):
    def __init__(self, session_key: str) -> None:
        super().__init__(timeout=None)
        self.session_key = session_key
        session = next((s for s in _active_sessions.values() if s.session_key == self.session_key), None)
        self.result_bill_id = consensus_result_bill_id(session) if session else 0

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
        if not consensus_generation_matches(
            session,
            stage="after_result",
            result_bill_id=self.result_bill_id,
        ):
            await interaction.response.send_message("Эта панель относится к уже завершённому этапу.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Следующий проект", style=discord.ButtonStyle.success)
    async def next_bill(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            await begin_next_bill_vote(
                interaction.client,
                interaction.guild,
                session,
                interaction.channel,
                expected_stage="after_result",
                expected_result_bill_id=self.result_bill_id,
            )

    @discord.ui.button(label="Завершить консенсус", style=discord.ButtonStyle.secondary)
    async def finish_all(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            await finish_session(
                interaction.client,
                interaction.guild,
                session,
                interaction.channel,
                expected_stage="after_result",
                expected_result_bill_id=self.result_bill_id,
            )


async def cancel_vote_timer(session: LiveConsensusSession) -> None:
    task = session.timer_task
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
    session.timer_task = None
    session.timer_deadline = None
    session.timer_seconds = None


async def set_vote_timer(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    seconds: int,
    *,
    expected_bill_id: int | None = None,
) -> None:
    async with consensus_session_lock(session.guild_id):
        if not consensus_generation_matches(
            session,
            stage="voting",
            bill_id=expected_bill_id,
        ) or session.current_bill is None:
            raise ConsensusStateError("Таймер можно установить только во время голосования.")
        previous_task = session.timer_task
        with _consensus.mutation(session):
            session.timer_seconds = int(seconds)
            session.timer_deadline = datetime.now(timezone.utc) + timedelta(seconds=int(seconds))
            await asyncio.to_thread(
                _consensus.save,
                session,
                "timer_set",
                actor=ConsensusActor(session.leader_id, session.leader_display),
                details={"seconds": int(seconds)},
            )
        if previous_task and previous_task is not asyncio.current_task() and not previous_task.done():
            previous_task.cancel()
        session.timer_task = None
        schedule_vote_timer_task(
            bot,
            guild,
            session,
            int(seconds),
            bill_id=consensus_bill_id(session),
        )

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


def schedule_vote_timer_task(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    seconds: int,
    *,
    bill_id: int | None = None,
) -> None:
    seconds = max(0, int(seconds))
    expected_bill_id = int(bill_id if bill_id is not None else consensus_bill_id(session))

    async def runner() -> None:
        try:
            await asyncio.sleep(seconds)
            current = _active_sessions.get(guild.id)
            if (
                current is session
                and consensus_generation_matches(
                    session,
                    stage="voting",
                    bill_id=expected_bill_id,
                )
            ):
                await finalize_current_vote(
                    bot,
                    guild,
                    session,
                    forced=True,
                    expected_bill_id=expected_bill_id,
                )
        except asyncio.CancelledError:
            return
        except Exception:
            traceback.print_exc()

    session.timer_task = asyncio.create_task(runner())


async def update_all_vote_dms(guild: discord.Guild, session: LiveConsensusSession, content: str | None = None) -> None:
    for p in session.confirmed_participants():
        if session.stage not in {"voting", "paused", "discussion_type", "discussion"}:
            return
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
    async with consensus_session_lock(session.guild_id):
        if session.stage in {"voting", "paused", "discussion_type", "discussion"}:
            await asyncio.to_thread(_consensus.save, session, "vote_messages_refreshed")


async def request_discussion(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    initiator: LiveParticipant,
    *,
    expected_bill_id: int | None = None,
) -> None:
    async with consensus_session_lock(session.guild_id):
        if not consensus_generation_matches(
            session,
            stage="voting",
            bill_id=expected_bill_id,
        ) or session.current_bill is None:
            return
        if session.discussion_initiator_id:
            return
        await cancel_vote_timer(session)
        await asyncio.to_thread(_consensus.request_discussion, session, initiator)
    wake_operations_worker()
    await update_all_vote_dms(guild, session, content=f"<@{initiator.user_id}> инициировал дискуссию. Голосование временно приостановлено.")
    await update_host_vote_message(bot, guild, session)


async def start_discussion_channel(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    discussion_type: str,
    *,
    expected_bill_id: int | None = None,
) -> None:
    if session.current_bill is None or not consensus_generation_matches(
        session,
        stage="discussion_type",
        bill_id=expected_bill_id,
    ):
        return
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
    allowed: set[int] = {p.user_id for p in session.confirmed_participants() if p.kind == "senator"}
    non_leader_chair = next((p for p in session.confirmed_participants() if p.kind == "chair" and p.user_id != session.leader_id), None)
    if non_leader_chair:
        allowed.add(non_leader_chair.user_id)
    async with consensus_session_lock(session.guild_id):
        if session.current_bill is None or not consensus_generation_matches(
            session,
            stage="discussion_type",
            bill_id=expected_bill_id,
        ):
            if created is not None:
                try:
                    await created.delete(reason="TVRS discussion state changed before activation")
                except discord.DiscordException:
                    pass
            return
        await asyncio.to_thread(
            _consensus.begin_discussion,
            session,
            discussion_type,
            channel_id=created.id if created else None,
            allowed_user_ids=allowed,
        )
    wake_operations_worker()
    if created:
        try:
            await created.send(embed=build_discussion_embed(session), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.DiscordException:
            pass
    for p in session.confirmed_participants():
        if session.stage != "discussion":
            break
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
    async with consensus_session_lock(session.guild_id):
        if session.stage == "discussion":
            await asyncio.to_thread(_consensus.save, session, "discussion_invitations_sent")
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


async def end_discussion(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    *,
    expected_stage: str | None = None,
    expected_bill_id: int | None = None,
) -> None:
    if expected_stage is not None and not consensus_generation_matches(
        session,
        stage=expected_stage,
        bill_id=expected_bill_id,
    ):
        return
    if session.stage not in {"discussion_type", "discussion"}:
        return
    if session.discussion_channel_id:
        channel = guild.get_channel(session.discussion_channel_id)
        if hasattr(channel, "send"):
            try:
                await channel.send("Дискуссия завершена ведущим. Голосование возвращено в активный режим.")  # type: ignore[attr-defined]
            except discord.DiscordException:
                pass
    async with consensus_session_lock(session.guild_id):
        if expected_stage is not None and not consensus_generation_matches(
            session,
            stage=expected_stage,
            bill_id=expected_bill_id,
        ):
            return
        if session.stage not in {"discussion_type", "discussion"}:
            return
        await asyncio.to_thread(
            _consensus.end_discussion,
            session,
            actor=ConsensusActor(session.leader_id, session.leader_display),
        )
    wake_operations_worker()
    await update_all_vote_dms(guild, session, content="Дискуссия завершена. Голосование снова открыто.")
    await update_host_vote_message(bot, guild, session)


async def pause_session(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    reason: str,
    automatic: bool = False,
    *,
    expected_stage: str | None = None,
    expected_bill_id: int | None = None,
) -> None:
    async with consensus_session_lock(session.guild_id):
        if expected_stage is not None and not consensus_generation_matches(
            session,
            stage=expected_stage,
            bill_id=expected_bill_id,
        ):
            return
        if session.stage == "paused":
            return
        await cancel_vote_timer(session)
        await asyncio.to_thread(
            _consensus.pause,
            session,
            reason,
            automatic=automatic,
            actor=None if automatic else ConsensusActor(session.leader_id, session.leader_display),
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
    rules = session.rules
    ok = (
        len(chairs) >= rules.minimum_chairs
        and len(senators) >= rules.minimum_senators
        and (not rules.senators_must_be_odd or len(senators) % 2 == 1)
    )
    if ok:
        return True, f"Кворум сохранен: председатели `{len(chairs)}`, сенаторы `{len(senators)}`."
    return False, f"Кворум утрачен: председатели `{len(chairs)}`, сенаторы `{len(senators)}`. Консенсус приостановлен."


async def check_realtime_quorum(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession) -> None:
    if session.finished or session.stage in {"registration", "finalizing"}:
        return
    ok, reason = session_voice_quorum_ready(guild, session)
    if not ok and session.stage != "paused":
        await pause_session(bot, guild, session, reason, automatic=True)


async def resume_session(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    *,
    expected_stage: str | None = None,
    expected_bill_id: int | None = None,
) -> None:
    quorum_failed = False
    async with consensus_session_lock(session.guild_id):
        if expected_stage is not None and not consensus_generation_matches(
            session,
            stage=expected_stage,
            bill_id=expected_bill_id,
        ):
            return
        if session.stage != "paused":
            return
        ok, _ = session_voice_quorum_ready(guild, session)
        if not ok:
            quorum_failed = True
            target = ""
        else:
            target = await asyncio.to_thread(
                _consensus.resume,
                session,
                actor=ConsensusActor(session.leader_id, session.leader_display),
            )
    if quorum_failed:
        await update_host_vote_message(bot, guild, session)
        return
    if target in {"discussion_type", "discussion"}:
        content = "Кворум восстановлен. Дискуссия продолжается."
    elif target == "after_result":
        content = "Кворум восстановлен. Можно переходить к следующему проекту."
    else:
        content = "Кворум восстановлен. Голосование продолжается."
    wake_operations_worker()
    if target == "after_result" and session.results:
        embed = build_result_embed(session.results[-1], session)
        for participant in session.confirmed_participants():
            await edit_vote_dm_to_result(guild, session, participant, embed, content=content)
        await edit_session_host_message(
            session,
            embed=embed,
            view=TVRSAfterResultView(session.session_key),
        )
        return
    await update_all_vote_dms(guild, session, content=content)
    await update_host_vote_message(bot, guild, session)


def _control_delivery_matches(
    session: LiveConsensusSession,
    *,
    phase: str,
    bill_id: int,
    user_id: int,
) -> bool:
    participant = session.participants.get(int(user_id))
    if participant is None or session.finished:
        return False
    if phase == "registration":
        return session.stage == "registration" and not participant.confirmed
    return (
        phase == "voting"
        and participant.confirmed
        and session.stage in {"voting", "paused", "discussion_type", "discussion"}
        and int((session.current_bill or {}).get("id") or 0) == int(bill_id)
    )


async def deliver_consensus_control_dm(message: OutboxMessage, bot: commands.Bot | discord.Client) -> DeliveryReceipt:
    """Deliver a recoverable control DM and durably store its Discord receipt."""

    payload = message.payload
    if int(payload.get("payload_version") or 0) != 1:
        raise ValueError("tvrs_control_payload_version_unsupported")
    guild_id = int(payload.get("guild_id") or 0)
    session_key = str(payload.get("session_key") or "")
    phase = str(payload.get("phase") or "")
    bill_id = int(payload.get("bill_id") or 0)
    user_id = int(payload.get("user_id") or 0)
    if guild_id <= 0 or user_id <= 0 or phase not in {"registration", "voting"}:
        raise ValueError("tvrs_control_payload_invalid")

    session = _consensus_registry.find(session_key)
    if session is None:
        snapshots = await asyncio.to_thread(_consensus_repository.active_snapshots, guild_id)
        if any(str(item.get("session_key") or "") == session_key for item in snapshots):
            raise RuntimeError("tvrs_control_session_not_restored")
        return DeliveryReceipt()
    if not _control_delivery_matches(
        session,
        phase=phase,
        bill_id=bill_id,
        user_id=user_id,
    ):
        return DeliveryReceipt()

    guild = bot.get_guild(guild_id)
    if guild is None:
        raise RuntimeError(f"tvrs_delivery_guild_unavailable:{guild_id}")
    member = guild.get_member(user_id)
    if member is None:
        member = await guild.fetch_member(user_id)
    participant = session.participants[user_id]
    if phase == "registration":
        queue = await asyncio.to_thread(queue_lines, guild_id, 10)
        embed = discord.Embed(
            title="Пленарный консенсус Товарищества",
            description=f"Ведущий: <@{session.leader_id}>\nРоль: **{role_label(participant)}**",
            color=TVRS_EMBED_COLOR,
            timestamp=now_local(),
        )
        embed.add_field(name="Очередь законопроектов", value=queue, inline=False)
        view: discord.ui.View = TVRSConfirmView(session_key, user_id)
        content: str | None = "Подтвердите участие в консенсусе."
        existing_message_id = int(participant.dm_message_id or 0)
    else:
        embed = await asyncio.to_thread(build_dm_vote_embed, session, participant)
        view = TVRSPermanentVoteView(session_key, user_id) if participant.permanent else TVRSVoteView(session_key, user_id)
        content = None
        existing_message_id = int(participant.vote_message_id or participant.dm_message_id or 0)

    marker = delivery_marker(message.dedupe_key)
    embed.set_footer(text=marker)
    dm_channel = member.dm_channel or await member.create_dm()
    sent = None
    if existing_message_id:
        try:
            sent = await dm_channel.fetch_message(existing_message_id)
        except discord.NotFound:
            sent = None
    if sent is None:
        sent = await find_delivery_marker(dm_channel, marker)
    if sent is None:
        sent = await member.send(content=content, embed=embed, view=view)
    else:
        await sent.edit(content=content, embed=embed, view=view)
    message_id = int(sent.id)

    async with consensus_session_lock(guild_id):
        current = _consensus_registry.find(session_key)
        if current is None or not _control_delivery_matches(
            current,
            phase=phase,
            bill_id=bill_id,
            user_id=user_id,
        ):
            try:
                await sent.edit(view=None)
            except discord.DiscordException:
                pass
            return DeliveryReceipt(message_id=message_id)

        def persist_receipt() -> None:
            with _consensus.mutation(current):
                target = current.participants[user_id]
                if phase == "registration":
                    target.dm_message_id = message_id
                else:
                    target.vote_message_id = message_id
                target.dm_failed = False
                _consensus.save(
                    current,
                    f"{phase}_control_delivered",
                    details={"user_id": user_id, "message_id": message_id, "bill_id": bill_id or None},
                )

        await asyncio.to_thread(persist_receipt)
    return DeliveryReceipt(message_id=message_id)


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


async def begin_next_bill_vote(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    channel,
    *,
    expected_stage: str | None = None,
    expected_result_bill_id: int | None = None,
) -> None:
    if session.finished or session.stage not in {"registration", "after_result"}:
        return
    bills: list[dict[str, Any]] = []
    paused_reason: str | None = None
    empty_queue_generation: tuple[str, int, int] | None = None
    async with consensus_session_lock(session.guild_id):
        if session.finished or session.stage not in {"registration", "after_result"}:
            return
        if expected_stage is not None and not consensus_generation_matches(
            session,
            stage=expected_stage,
            result_bill_id=expected_result_bill_id,
        ):
            return
        voice_ok, voice_reason = session_voice_quorum_ready(guild, session)
        if not voice_ok:
            if session.stage == "after_result":
                await cancel_vote_timer(session)
                await asyncio.to_thread(
                    _consensus.pause,
                    session,
                    voice_reason,
                    automatic=True,
                    actor=None,
                )
                paused_reason = voice_reason
        else:
            await cancel_vote_timer(session)
            bills = await asyncio.to_thread(storage.tvrs_queue_bills, guild.id, 1)
            if bills:
                bill = bills[0]
                deliveries = build_control_dm_deliveries(
                    session,
                    phase="voting",
                    bill_id=int(bill["id"]),
                )
                await asyncio.to_thread(
                    _consensus.begin_bill_atomically,
                    session,
                    bill,
                    actor=ConsensusActor(session.leader_id, session.leader_display),
                    deliveries=deliveries,
                )
            else:
                empty_queue_generation = (
                    str(session.stage),
                    consensus_bill_id(session),
                    consensus_result_bill_id(session),
                )
    if paused_reason is not None:
        wake_operations_worker()
        await update_all_vote_dms(guild, session, content=paused_reason)
        await update_host_vote_message(bot, guild, session)
        return
    if not voice_ok:
        return
    if not bills:
        if empty_queue_generation is None:
            return
        empty_stage, empty_bill_id, empty_result_bill_id = empty_queue_generation
        await finish_session(
            bot,
            guild,
            session,
            channel,
            expected_stage=empty_stage,
            expected_bill_id=empty_bill_id,
            expected_result_bill_id=empty_result_bill_id,
        )
        return
    started_bill_id = int(bills[0].get("id") or 0)
    wake_delivery_worker()
    wake_operations_worker()

    if not await edit_session_host_message(session, embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key)):
        if session.host_message_id:
            try:
                msg = await channel.fetch_message(session.host_message_id)
                await msg.edit(embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
                return
            except discord.DiscordException:
                pass
        msg = await channel.send(embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        host_bound = False
        async with consensus_session_lock(session.guild_id):
            if (
                _consensus_registry.find(session.session_key) is session
                and not session.finished
                and session.stage in {"voting", "paused", "discussion_type", "discussion"}
                and int((session.current_bill or {}).get("id") or 0) == started_bill_id
            ):
                session.host_message_obj = msg
                await asyncio.to_thread(_consensus.bind_host_message, session, msg.id, "host_panel_bound")
                host_bound = True
        if not host_bound:
            try:
                await msg.edit(view=None)
            except discord.DiscordException:
                pass


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


async def retry_pending_finalization_once(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
) -> bool:
    if session.stage != "finalizing":
        return True
    pending = session.pending_action or {}
    if pending.get("kind") == "veto":
        await apply_veto_for_actor(
            bot,
            guild,
            session,
            ConsensusActor(
                int(pending.get("actor_id") or TVRS_PERMANENT_CHAIR_ID),
                str(pending.get("actor_display") or "Постоянный председатель"),
            ),
        )
    else:
        await finalize_current_vote(bot, guild, session, forced=bool(pending.get("forced")))
    return session.stage != "finalizing"


def schedule_finalization_retry(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
) -> asyncio.Task:
    existing = _finalization_retry_tasks.get(session.session_key)
    if existing is not None and not existing.done():
        return existing

    async def runner() -> None:
        delay = 5
        attempt = 0
        try:
            while session.stage == "finalizing" and not session.finished:
                await asyncio.sleep(delay)
                attempt += 1
                try:
                    if await retry_pending_finalization_once(bot, guild, session):
                        return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    traceback.print_exc()
                    await log_technical_event(
                        bot,
                        guild,
                        title="Фиксация консенсуса будет повторена",
                        details=(
                            f"Сессия: `{session.session_key[:120]}`\n"
                            f"Попытка: `{attempt}`\n"
                            f"Ошибка: `{type(exc).__name__}: {str(exc)[:700]}`"
                        ),
                        dedupe_key=f"consensus-finalization-retry:{session.session_key}",
                        cooldown_seconds=300,
                    )
                delay = min(300, delay * 2)
        finally:
            if _finalization_retry_tasks.get(session.session_key) is asyncio.current_task():
                _finalization_retry_tasks.pop(session.session_key, None)

    task = asyncio.create_task(runner(), name=f"consensus-finalization:{session.session_key}")
    _finalization_retry_tasks[session.session_key] = task
    return task


def clear_finalization_retry(session_key: str) -> None:
    task = _finalization_retry_tasks.pop(str(session_key), None)
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()


async def finalize_current_vote(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    forced: bool,
    *,
    expected_bill_id: int | None = None,
) -> None:
    actor = ConsensusActor(session.leader_id, session.leader_display) if forced else None
    async with consensus_session_lock(session.guild_id):
        if expected_bill_id is not None and consensus_bill_id(session) != int(expected_bill_id):
            return
        if session.stage == "voting":
            if session.current_bill is None:
                return
            await asyncio.to_thread(
                _consensus.claim_finalization,
                session,
                kind="vote",
                actor=actor,
                forced=forced,
            )
            await cancel_vote_timer(session)
        elif session.stage != "finalizing" or (session.pending_action or {}).get("kind") != "vote":
            return
        if session.current_bill is None:
            return
        bill = dict(session.current_bill)
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
        deliveries = build_result_deliveries(
            session,
            result,
            participant_content="Голосование по текущему законопроекту завершено.",
        )
        try:
            await asyncio.to_thread(
                _consensus.complete_result_atomically,
                session,
                result,
                bill_status=status,
                result_summary=f"{result_status_text(status)} • общий консенсус {result.overall_percent}%",
                event_type="vote_finalized_manually" if forced else "vote_finalized",
                actor=actor,
                deliveries=deliveries,
            )
        except Exception:
            schedule_finalization_retry(bot, guild, session)
            raise
    clear_finalization_retry(session.session_key)
    embed = build_result_embed(result, session)
    wake_delivery_worker()
    wake_operations_worker()
    async with consensus_session_lock(session.guild_id):
        current = _consensus_registry.find(session.session_key)
        if (
            current is session
            and session.stage == "after_result"
            and session.results
            and int(session.results[-1].bill_id) == int(result.bill_id)
        ):
            await edit_session_host_message(
                session,
                embed=embed,
                view=TVRSAfterResultView(session.session_key),
            )


async def apply_veto(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    user: discord.User | discord.Member,
    *,
    expected_bill_id: int | None = None,
) -> None:
    await apply_veto_for_actor(
        bot,
        guild,
        session,
        ConsensusActor(user.id, getattr(user, "display_name", str(user))),
        expected_bill_id=expected_bill_id,
    )


async def apply_veto_for_actor(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    actor: ConsensusActor,
    *,
    expected_bill_id: int | None = None,
) -> None:
    if actor.user_id != TVRS_PERMANENT_CHAIR_ID:
        raise ConsensusStateError("Право вето доступно только постоянному председателю.")
    async with consensus_session_lock(session.guild_id):
        if expected_bill_id is not None and consensus_bill_id(session) != int(expected_bill_id):
            raise ConsensusStateError("Это подтверждение вето относится к уже завершённому проекту.")
        if session.stage == "voting":
            if session.current_bill is None:
                return
            await asyncio.to_thread(
                _consensus.claim_finalization,
                session,
                kind="veto",
                actor=actor,
                veto_authorized=actor.user_id == TVRS_PERMANENT_CHAIR_ID,
            )
            await cancel_vote_timer(session)
        elif session.stage != "finalizing" or (session.pending_action or {}).get("kind") != "veto":
            return
        if session.current_bill is None:
            return
        bill = dict(session.current_bill)
        try:
            retry = await asyncio.to_thread(
                storage.tvrs_create_retry_bill,
                int(bill["id"]),
                int(actor.user_id or 0),
                actor.display_name,
            )
        except Exception:
            schedule_finalization_retry(bot, guild, session)
            raise
        result = LiveResult(
            bill_id=int(bill["id"]),
            bill_number=int(bill["bill_number"]),
            title=str(bill.get("title") or ""),
            status="vetoed",
            internal_percent=0.0,
            overall_percent=0.0,
            internal_active=False,
            votes=dict(session.votes),
            veto_by_id=actor.user_id,
            retry_bill_number=(int(retry["bill_number"]) if retry else None),
        )
        deliveries = build_result_deliveries(
            session,
            result,
            participant_content="Постоянный председатель применил право вето. Голосование отменено.",
        )
        if retry is not None:
            deliveries.append(build_retry_bill_delivery(session, result, retry))
        try:
            await asyncio.to_thread(
                _consensus.complete_result_atomically,
                session,
                result,
                bill_status="vetoed",
                result_summary="Применено право вето",
                event_type="veto_applied",
                actor=actor,
                details={
                    "veto_by_id": actor.user_id,
                    "retry_bill_number": result.retry_bill_number,
                },
                deliveries=deliveries,
            )
        except Exception:
            schedule_finalization_retry(bot, guild, session)
            raise
    clear_finalization_retry(session.session_key)
    embed = build_result_embed(result, session)
    wake_delivery_worker()
    wake_operations_worker()
    async with consensus_session_lock(session.guild_id):
        current = _consensus_registry.find(session.session_key)
        if (
            current is session
            and session.stage == "after_result"
            and session.results
            and int(session.results[-1].bill_id) == int(result.bill_id)
        ):
            await edit_session_host_message(
                session,
                embed=embed,
                view=TVRSAfterResultView(session.session_key),
            )


async def finish_session(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    channel,
    *,
    expected_stage: str | None = None,
    expected_bill_id: int | None = None,
    expected_result_bill_id: int | None = None,
) -> None:
    async with consensus_session_lock(session.guild_id):
        if expected_stage is not None and not consensus_generation_matches(
            session,
            stage=expected_stage,
            bill_id=expected_bill_id,
            result_bill_id=expected_result_bill_id,
        ):
            return
        if session.finished:
            return
        deliveries = build_session_summary_deliveries(session)
        await asyncio.to_thread(
            _consensus.finish_atomically,
            session,
            actor=ConsensusActor(session.leader_id, session.leader_display),
            deliveries=deliveries,
        )
    wake_delivery_worker()
    await cancel_vote_timer(session)
    embed = build_final_summary_embed(session)
    async with consensus_session_lock(session.guild_id):
        if _consensus_registry.find(session.session_key) is session and session.finished:
            await edit_session_host_message(session, embed=embed, view=None)
    await ensure_sticky_message(bot, guild, force_repost=True)
    _consensus_registry.remove(guild.id, session_key=session.session_key)
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
    register_public_panel_provider(build_public_universality_embed, TVRSPublicPanelView)
    register_tvrs_hub_handler(_open_tvrs_hub_impl)
    bot.add_view(TVRSStickyView())
    bot.add_view(TVRSPublicPanelView())


def register_restored_view(
    bot: commands.Bot,
    view: discord.ui.View,
    *,
    message_id: int,
) -> bool:
    try:
        bot.add_view(view, message_id=int(message_id))
        return True
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"consensus_view_restore_failed:{int(message_id)}") from exc


async def reconcile_restored_consensus_session(
    bot: commands.Bot,
    guild: discord.Guild,
    session: LiveConsensusSession,
) -> None:
    """Reattach controls and finish any operation interrupted by a restart."""
    if session.stage == "registration":
        for participant in session.participants.values():
            if participant.confirmed or not participant.dm_message_id:
                continue
            register_restored_view(
                bot,
                TVRSConfirmView(session.session_key, participant.user_id),
                message_id=participant.dm_message_id,
            )
    elif session.stage == "voting":
        for participant in session.confirmed_participants():
            if not participant.vote_message_id:
                continue
            register_restored_view(
                bot,
                TVRSRestoredVoteView(
                    session.session_key,
                    participant.user_id,
                    bill_id=consensus_bill_id(session),
                ),
                message_id=participant.vote_message_id,
            )

    if session.stage == "finalizing":
        pending = session.pending_action or {}
        if pending.get("kind") == "veto":
            await apply_veto_for_actor(
                bot,
                guild,
                session,
                ConsensusActor(
                    int(pending.get("actor_id") or TVRS_PERMANENT_CHAIR_ID),
                    str(pending.get("actor_display") or "Постоянный председатель"),
                ),
            )
        else:
            await finalize_current_vote(bot, guild, session, forced=bool(pending.get("forced")))

    if session.stage == "after_result" and session.results:
        result = session.results[-1]
        # Backfill only idempotent edits for sessions finalized before the
        # outbox migration.  Public sends are intentionally not reconstructed:
        # without an old receipt they could duplicate an already published
        # result.  New finalizations already contain all jobs atomically.
        recovery_deliveries = build_result_deliveries(
            session,
            result,
            participant_content="Бот восстановился после перезапуска. Итог голосования сохранён.",
        )
        for delivery in recovery_deliveries:
            payload = dict(delivery.get("payload") or {})
            if payload.get("destination") != "participant_dm" or not payload.get("destination_message_id"):
                continue
            await asyncio.to_thread(
                storage.delivery_outbox_enqueue,
                topic=str(delivery["topic"]),
                dedupe_key=str(delivery["dedupe_key"]),
                payload=payload,
                max_attempts=int(delivery.get("max_attempts") or 8),
            )
        wake_delivery_worker()
        if result.retry_bill_number:
            retry = await asyncio.to_thread(
                storage.tvrs_get_bill_by_number,
                guild.id,
                result.retry_bill_number,
            )
            if retry is not None and not retry.get("message_id"):
                delivery = build_retry_bill_delivery(session, result, retry)
                await asyncio.to_thread(
                    storage.delivery_outbox_enqueue,
                    topic=str(delivery["topic"]),
                    dedupe_key=str(delivery["dedupe_key"]),
                    payload=dict(delivery["payload"]),
                    max_attempts=int(delivery.get("max_attempts") or 8),
                )
                wake_delivery_worker()

    if session.stage == "voting" and session.timer_deadline is not None:
        remaining = int((session.timer_deadline - datetime.now(timezone.utc)).total_seconds())
        if remaining <= 0:
            await finalize_current_vote(bot, guild, session, forced=True)
        else:
            schedule_vote_timer_task(bot, guild, session, remaining)


def schedule_consensus_recovery_retry(bot: commands.Bot, guild_id: int) -> asyncio.Task:
    """Retry transient restore failures without waiting for another on_ready."""

    key = int(guild_id)
    existing = _consensus_recovery_tasks.get(key)
    if existing is not None and not existing.done():
        return existing

    async def runner() -> None:
        delay = 5
        try:
            while not bot.is_closed():
                if key != 0 and key in _restored_consensus_guilds:
                    return
                await asyncio.sleep(delay)
                try:
                    await restore_tvrs_consensus_sessions(
                        bot,
                        guild_id=None if key == 0 else key,
                        _propagate_read_error=True,
                    )
                    if key == 0:
                        return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    traceback.print_exc()
                delay = min(300, delay * 2)
        finally:
            if _consensus_recovery_tasks.get(key) is asyncio.current_task():
                _consensus_recovery_tasks.pop(key, None)

    task = asyncio.create_task(runner(), name=f"consensus-recovery:{key}")
    _consensus_recovery_tasks[key] = task
    return task


async def restore_tvrs_consensus_sessions(
    bot: commands.Bot,
    guild_id: int | None = None,
    *,
    _propagate_read_error: bool = False,
) -> int:
    """Restore active sessions and their DM controls after a container restart."""
    try:
        snapshots = await asyncio.to_thread(_consensus_repository.active_snapshots, guild_id)
    except Exception:
        if _propagate_read_error:
            raise
        traceback.print_exc()
        schedule_consensus_recovery_retry(bot, int(guild_id or 0))
        return 0
    restored_count = 0
    for snapshot in snapshots:
        guild_id = int(snapshot.get("guild_id") or 0)
        if guild_id <= 0 or guild_id in _restored_consensus_guilds:
            continue
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue
        session = _consensus_registry.get(guild_id)
        if session is None:
            try:
                restored = _consensus_registry.restore([snapshot])
                session = restored[0] if restored else None
            except (ConsensusStateError, KeyError, TypeError, ValueError) as exc:
                traceback.print_exc()
                await asyncio.to_thread(
                    _consensus_repository.quarantine,
                    str(snapshot.get("session_key") or ""),
                    f"{type(exc).__name__}: {exc}",
                )
                await log_technical_event(
                    bot,
                    guild,
                    title="Сессия консенсуса изолирована",
                    details=(
                        f"Сессия: `{str(snapshot.get('session_key') or 'неизвестно')[:120]}`\n"
                        f"Причина: `{type(exc).__name__}: {str(exc)[:700]}`\n"
                        "Текущий законопроект возвращён в очередь. Можно начать новое заседание."
                    ),
                    dedupe_key=f"consensus-quarantine:{guild_id}",
                    cooldown_seconds=300,
                )
                continue
        if session is None:
            continue
        try:
            await reconcile_restored_consensus_session(bot, guild, session)
        except Exception as exc:
            # A temporary Discord or storage outage must not mark recovery as
            # complete. A later on_ready pass can safely retry the same state.
            traceback.print_exc()
            await log_technical_event(
                bot,
                guild,
                title="Восстановление консенсуса будет повторено",
                details=(
                    f"Сессия: `{session.session_key[:120]}`\n"
                    f"Ошибка: `{type(exc).__name__}: {str(exc)[:700]}`"
                ),
                dedupe_key=f"consensus-recovery:{guild_id}",
                cooldown_seconds=300,
            )
            schedule_consensus_recovery_retry(bot, guild_id)
            continue
        _restored_consensus_guilds.add(guild_id)
        retry_task = _consensus_recovery_tasks.pop(guild_id, None)
        if retry_task is not None and retry_task is not asyncio.current_task() and not retry_task.done():
            retry_task.cancel()
        restored_count += 1

    if restored_count:
        wake_operations_worker()
    return restored_count


async def tvrs_ensure_sticky_all(bot: commands.Bot) -> None:
    await restore_tvrs_consensus_sessions(bot)
    for guild in bot.guilds:
        if _consensus_registry.get(guild.id) is not None:
            continue
        try:
            await ensure_sticky_message(bot, guild, force_repost=False)
        except Exception:
            traceback.print_exc()


def setup_tvrs(bot: commands.Bot, remember_command_activity: Callable[[discord.Interaction, str, str], None]) -> None:
    register_public_panel_provider(build_public_universality_embed, TVRSPublicPanelView)
    register_tvrs_hub_handler(_open_tvrs_hub_impl)
    register_delivery_handler(TVRS_RESULT_TOPIC, make_result_delivery_handler(bot))
    register_delivery_handler(TVRS_BILL_PUBLICATION_TOPIC, make_retry_bill_delivery_handler(bot))
    register_delivery_handler(TVRS_RETRY_BILL_TOPIC, make_retry_bill_delivery_handler(bot))
    register_delivery_handler(
        TVRS_CONTROL_DM_TOPIC,
        lambda message: deliver_consensus_control_dm(message, bot),
    )
    register_delivery_handler(TVRS_SESSION_SUMMARY_TOPIC, make_session_summary_delivery_handler(bot))

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
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        remember_command_activity(interaction, "command_tvrs", "/tvrs")
        embed = await asyncio.to_thread(build_universality_embed, interaction.guild, interaction.user.id)
        await interaction.followup.send(
            embed=embed,
            view=TVRSUniversalityView(interaction.user.id),
            ephemeral=True,
        )

    @bot.tree.command(name=TVRS_SETBILL_COMMAND_NAME, description=TVRS_SETBILL_COMMAND_DESCRIPTION)
    @app_commands.describe(last_accepted="Последний принятый номер. Например: 8, чтобы следующий был 009")
    async def tvrs_setbill(interaction: discord.Interaction, last_accepted: app_commands.Range[int, 0, 9999]) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        if not is_chair(interaction.user):
            await interaction.response.send_message("Команда доступна только председателю.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        remember_command_activity(interaction, "command_tvrs_setbill", "/tvrs_setbill")
        next_number = await asyncio.to_thread(
            storage.tvrs_set_last_accepted_bill_number,
            interaction.guild.id,
            int(last_accepted),
        )
        await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)
        await interaction.followup.send(f"Последний принятый законопроект: `{format_bill_number(int(last_accepted))}`. Следующий номер: `{format_bill_number(next_number)}`.", ephemeral=True)



    @bot.tree.command(name=TVRS_ADMIN_COMMAND_NAME, description=TVRS_ADMIN_COMMAND_DESCRIPTION)
    @app_commands.describe(target="bill/result/plenary/delivery", action="view/delete/edit/retry", identifier="Номер проекта, результата, консенсуса или доставки", confirm="Для удаления напишите DELETE", field="Поле для edit", value="Новое значение")
    async def tvrs_admin(interaction: discord.Interaction, target: str, action: str, identifier: str, confirm: str | None = None, field: str | None = None, value: str | None = None) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        if not is_chair(interaction.user):
            await interaction.response.send_message("Команда доступна только председателю.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        remember_command_activity(interaction, "command_tvrs_admin", "/tvrs_admin")
        target = target.strip().lower()
        action = action.strip().lower()
        if target in {"bill", "закон", "project", "проект"}:
            try:
                num = int(re.sub(r"\D", "", identifier) or "0")
            except ValueError:
                num = 0
            if action == "view":
                bill = await asyncio.to_thread(storage.tvrs_get_bill_by_number, interaction.guild.id, num)
                if not bill:
                    await interaction.followup.send(f"Законопроект `{identifier}` не найден.", ephemeral=True)
                    return
                await interaction.followup.send(f"`{format_bill_number(bill['bill_number'])}` • **{bill.get('title','')}**\nСтатус: `{bill.get('status')}`\nID: `{bill.get('id')}`", ephemeral=True)
                return
            if action == "delete":
                if confirm != "DELETE":
                    await interaction.followup.send("Для удаления укажите `confirm: DELETE`.", ephemeral=True)
                    return
                try:
                    bill = await asyncio.to_thread(
                        storage.tvrs_delete_bill_by_number,
                        interaction.guild.id,
                        num,
                        interaction.user.id,
                        getattr(interaction.user, "display_name", str(interaction.user)),
                    )
                except ValueError as exc:
                    if str(exc) == "bill_locked_by_active_consensus":
                        await interaction.followup.send(
                            "Этот законопроект участвует в активном консенсусе и защищён от удаления.",
                            ephemeral=True,
                        )
                    elif str(exc) == "tvrs_delivery_pending":
                        await interaction.followup.send(
                            "Для этого законопроекта ещё выполняется публикация или уведомление. Повторите удаление после доставки.",
                            ephemeral=True,
                        )
                    else:
                        await interaction.followup.send(f"Удаление отклонено: `{str(exc)[:200]}`", ephemeral=True)
                    return
                await interaction.followup.send((f"Законопроект `{format_bill_number(num)}` удален." if bill else "Законопроект не найден."), ephemeral=True)
                await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)
                return
            if action == "edit":
                if not field or value is None:
                    await interaction.followup.send("Для edit нужны `field` и `value`. Поля: title, summary, materials, status, result_summary.", ephemeral=True)
                    return
                try:
                    bill = await asyncio.to_thread(
                        storage.tvrs_update_bill_field,
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
                try:
                    row = await asyncio.to_thread(
                        storage.tvrs_delete_live_result,
                        rid,
                        interaction.guild.id,
                        interaction.user.id,
                        getattr(interaction.user, "display_name", str(interaction.user)),
                    )
                except ValueError as exc:
                    await interaction.followup.send(
                        (
                            "Для этого итога ещё выполняется доставка. Повторите удаление после неё."
                            if str(exc) == "tvrs_delivery_pending"
                            else "Итог относится к активному консенсусу и пока защищён от удаления."
                        ),
                        ephemeral=True,
                    )
                    return
                await interaction.followup.send((f"Итог голосования ID `{rid}` удален." if row else "Итог не найден."), ephemeral=True)
                return
        if target in {"plenary", "consensus", "консенсус"}:
            pn = int(re.sub(r"\D", "", identifier) or "0")
            if action == "delete":
                if confirm != "DELETE":
                    await interaction.followup.send("Для удаления укажите `confirm: DELETE`.", ephemeral=True)
                    return
                try:
                    count = await asyncio.to_thread(
                        storage.tvrs_delete_plenary_results,
                        interaction.guild.id,
                        pn,
                        interaction.user.id,
                        getattr(interaction.user, "display_name", str(interaction.user)),
                    )
                except ValueError as exc:
                    await interaction.followup.send(
                        (
                            "Финальная сводка этого консенсуса ещё доставляется. Повторите удаление позже."
                            if str(exc) == "tvrs_delivery_pending"
                            else "Итоги активного консенсуса пока защищены от удаления."
                        ),
                        ephemeral=True,
                    )
                    return
                await interaction.followup.send(f"Удалено итогов консенсуса №`{pn}`: `{count}`.", ephemeral=True)
                return
        if target in {"delivery", "outbox", "доставка"}:
            delivery_id = int(re.sub(r"\D", "", identifier) or "0")
            if action in {"retry", "repeat", "повторить"}:
                requeued = await asyncio.to_thread(
                    storage.delivery_outbox_requeue_dead,
                    delivery_id,
                    guild_id=interaction.guild.id,
                )
                if requeued:
                    wake_delivery_worker()
                await interaction.followup.send(
                    (
                        f"Доставка ID `{delivery_id}` возвращена в очередь."
                        if requeued
                        else "Остановленная доставка с таким ID не найдена."
                    ),
                    ephemeral=True,
                )
                return
        await interaction.followup.send("Неизвестная команда администратора. Используйте target: bill/result/plenary/delivery и action: view/delete/edit/retry.", ephemeral=True)

    @bot.tree.command(name=TVRS_STICKY_COMMAND_NAME, description=TVRS_STICKY_COMMAND_DESCRIPTION)
    async def tvrs_sticky(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        if not is_chair(interaction.user):
            await interaction.response.send_message("Команда доступна только председателю.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        remember_command_activity(interaction, "command_tvrs_sticky", "/tvrs_sticky")
        await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)
        await interaction.followup.send(f"Сообщение подачи законопроектов обновлено в <#{TVRS_MATERIALS_CHANNEL_ID}>.", ephemeral=True)
