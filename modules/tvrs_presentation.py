from __future__ import annotations

import asyncio

import discord
from discord.ext import commands

from persistence import activity_repository as _activity_storage
from persistence import craft_repository as _craft_storage
from persistence import finance_repository as _finance_storage
from persistence import tvrs_repository as _tvrs_storage
from modules.consensus_core import (
    DEFAULT_CONSENSUS_RULES,
    LiveConsensusSession,
    LiveParticipant,
    calculate_consensus as calculate_consensus_v2,
)
from modules.consensus_runtime import (
    active_sessions as _active_sessions,
)
from modules.consensus_v3 import CONSENSUS_ENGINE_VERSION, consensus_progress_text
from modules.control_center_config import ACTIVE_TASKS_CHANNEL_ID
from modules.tvrs_config import (
    TVRS_CHAIR_ROLE_ID,
    TVRS_CONSENSUS_VOICE_CHANNEL_ID,
    TVRS_DEFAULT_NEXT_BILL_NUMBER,
    TVRS_DEFAULT_NEXT_PLENARY_NUMBER,
    TVRS_EMBED_COLOR,
    TVRS_MATERIALS_CHANNEL_ID,
    TVRS_PERMANENT_CHAIR_ID,
    TVRS_SENATOR_ROLE_ID,
)
from modules.tvrs_embeds import (
    build_discussion_embed,
    build_paused_embed,
)
from modules.tvrs_formatting import (
    clean_stage_name,
    clip_text,
    format_bill_number,
    materials_text,
    now_local,
    progress_bar,
    remaining_timer_text,
    role_label,
    ru_ordinal,
    status_icon,
    vote_label,
    vote_progress,
    vote_split_lines,
)

def TVRSUniversalityView(*args, **kwargs):
    from modules.tvrs_hub_views import TVRSUniversalityView as _implementation
    return _implementation(*args, **kwargs)

async def get_materials_channel(*args, **kwargs):
    from modules.tvrs_recovery import get_materials_channel as _implementation
    return await _implementation(*args, **kwargs)

def TVRSPermanentVoteView(*args, **kwargs):
    from modules.tvrs_consensus_views import TVRSPermanentVoteView as _implementation
    return _implementation(*args, **kwargs)

def TVRSVoteView(*args, **kwargs):
    from modules.tvrs_consensus_views import TVRSVoteView as _implementation
    return _implementation(*args, **kwargs)

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
    if member.guild_permissions.administrator:
        return "chair"
    if member.id == TVRS_PERMANENT_CHAIR_ID:
        return "chair"
    if TVRS_CHAIR_ROLE_ID and any(role.id == TVRS_CHAIR_ROLE_ID for role in member.roles):
        return "chair"
    if any(role.id == TVRS_SENATOR_ROLE_ID for role in member.roles):
        return "senator"
    return None


def queue_short_lines(guild_id: int, limit: int = 8, *, skip_bill_id: int | None = None) -> str:
    queue = _tvrs_storage.tvrs_queue_bills(guild_id, limit=limit + 3)
    lines: list[str] = []
    for index, bill in enumerate([b for b in queue if int(b.get("id") or 0) != int(skip_bill_id or -1)][:limit], 1):
        number = format_bill_number(int(bill.get("bill_number") or 0))
        title = clip_text(bill.get("title"), 70)
        attempt = int(bill.get("attempt") or 1)
        attempt_text = f" · попытка {attempt}/3" if attempt > 1 else ""
        lines.append(f"`{index:02d}` №`{number}` — **{title}**{attempt_text}")
    return "\n".join(lines) or "Очередь пуста."


def queue_lines(guild_id: int, limit: int = 20) -> str:
    queue = _tvrs_storage.tvrs_queue_bills(guild_id, limit=limit)
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
    queue = _tvrs_storage.tvrs_queue_bills(guild.id, limit=30)
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
    next_number = _tvrs_storage.tvrs_next_bill_number(guild.id, TVRS_DEFAULT_NEXT_BILL_NUMBER)
    embed = discord.Embed(
        title="Подача законопроектов Товарищества",
        description=f"Нажмите ниже, чтобы предложить законопроект.\n\nСледующий номер: **{format_bill_number(next_number)}**",
        color=TVRS_EMBED_COLOR,
    )
    embed.set_footer(text="Товарищество • пленарный консенсус")
    return embed


def build_main_panel_embed(guild: discord.Guild) -> discord.Embed:
    queue_count = len(_tvrs_storage.tvrs_queue_bills(guild.id, limit=100))
    plenary = _tvrs_storage.tvrs_get_next_plenary_number(guild.id, TVRS_DEFAULT_NEXT_PLENARY_NUMBER)
    active = _active_sessions.get(guild.id)
    active_text = "🟢 активного заседания нет"
    if active and not active.finished:
        active_text = (
            f"🟡 идёт {ru_ordinal(active.plenary_number)} заседание · "
            f"этап **{clean_stage_name(active.stage)}** · ведущий <@{active.leader_id}>"
        )
    embed = discord.Embed(
        title="⚖️ Центр пленарного консенсуса",
        description=(
            f"**Следующее заседание:** {ru_ordinal(plenary)}\n"
            f"**Законопроектов в очереди:** {queue_count}\n"
            f"**Состояние:** {active_text}"
        ),
        color=TVRS_EMBED_COLOR,
        timestamp=now_local(),
    )
    if active and not active.finished:
        embed.add_field(
            name="Маршрут заседания",
            value=consensus_progress_text(active.stage),
            inline=False,
        )
    else:
        embed.add_field(
            name="Следующее действие",
            value=(
                "Председатель проверяет повестку и войс через **«Подготовить заседание»**. "
                "Рабочая сессия создаётся только после успешной проверки."
            ),
            inline=False,
        )
    embed.add_field(name="📚 Очередь", value=queue_short_lines(guild.id, limit=8), inline=False)
    embed.add_field(name="🎙️ Голосовой канал", value=f"<#{TVRS_CONSENSUS_VOICE_CHANNEL_ID}>", inline=True)
    embed.add_field(name="⚖️ Правило принятия", value="председатель `49%` + активный сенат `2%` = `51%`", inline=True)
    engine_version = active.engine_version if active and not active.finished else CONSENSUS_ENGINE_VERSION
    embed.set_footer(text=f"TVRS • Consensus V{engine_version} • один контекстный вход")
    return embed


def _hub_money(value: int | None) -> str:
    if value is None:
        return "неизвестно"
    return f"{int(value):,} $".replace(",", " ")


def build_universality_embed(guild: discord.Guild, requester_id: int | None) -> discord.Embed:
    """Build the read-only overview for the common /tvrs entry point."""
    finance = _finance_storage.finance_get_latest_state(guild.id)
    active_plans = _craft_storage.craft_active_plans(guild.id, 100)
    recipes = _craft_storage.craft_list_recipes(guild.id, active_only=True, limit=200)
    actions = (
        _activity_storage.bot_list_actions(guild.id, actor_id=requester_id, status="active", limit=100)
        if requester_id is not None
        else []
    )
    queue_count = len(_tvrs_storage.tvrs_queue_bills(guild.id, limit=100))
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
            "**Консенсус** — подготовка, личное голосование и наблюдение за заседанием.\n"
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
            "Это приватный пульт ведущего. Участники подтверждают участие в ЛС или через "
            "`/tvrs` → **«Консенсус»**. Ведущий зарегистрирован автоматически."
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
            f"Consensus V{session.engine_version} • правила v{session.rules.version} • "
            f"минимум {session.rules.minimum_chairs} председателя "
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
    old_id_raw = _activity_storage.get_meta(meta_key)
    old_id = int(old_id_raw) if old_id_raw and old_id_raw.isdigit() else None
    if old_id:
        try:
            old_msg = await channel.fetch_message(old_id)
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    _activity_storage.set_meta_value(meta_key, "")


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
            p.vote_bill_id = int((session.current_bill or {}).get("id") or 0) or None
            return
        except discord.DiscordException:
            pass
    try:
        dm = await member.send(embed=embed, view=view)
        p.vote_message_id = dm.id
        p.vote_bill_id = int((session.current_bill or {}).get("id") or 0) or None
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

__all__ = ['consensus_bill_id', 'consensus_result_bill_id', 'consensus_generation_matches', 'is_chair', 'is_senator', 'participant_kind', 'queue_short_lines', 'queue_lines', 'build_queue_embed', 'build_sticky_embed', 'build_main_panel_embed', '_hub_money', 'build_universality_embed', 'build_public_universality_embed', 'build_universality_help_embed', '_open_tvrs_hub_impl', 'participant_lines', 'build_registration_embed', 'calculate_consensus', 'vote_lines', 'build_live_vote_embed', 'build_dm_vote_embed', 'delete_sticky_message', 'edit_session_host_message', 'edit_or_send_vote_dm', 'edit_vote_dm_to_result', 'voice_participants']
