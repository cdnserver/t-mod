"""Discord portal for Consensus V3 preparation, participation and observation."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid

import discord

from persistence import activity_repository as _activity_storage
from persistence import tvrs_repository as storage
from persistence import consensus_schedule_repository as schedule_storage
from modules.consensus_core import (
    ConsensusStateError,
    DEFAULT_CONSENSUS_RULES,
    LiveConsensusSession,
    clean_stage_name,
)
from modules.consensus_runtime import (
    active_sessions as _active_sessions,
    coordinator as _consensus,
    registry as _consensus_registry,
    session_lock as consensus_session_lock,
)
from modules.consensus_service import ConsensusActor
from modules.async_safety import run_blocking_cancellation_safe
from modules.consensus_v3 import (
    CONSENSUS_ENGINE_VERSION,
    ConsensusPreflight,
    consensus_progress_text,
    evaluate_consensus_preflight,
)
from modules.delivery_runtime import wake_delivery_worker
from modules.operations_runtime import wake_operations_worker
from modules.tvrs_config import (
    TVRS_BILLS_CHANNEL_ID,
    TVRS_CONSENSUS_VOICE_CHANNEL_ID,
    TVRS_DEFAULT_NEXT_PLENARY_NUMBER,
    TVRS_EMBED_COLOR,
)
from modules.tvrs_delivery import (
    build_control_dm_deliveries,
)
from modules.tvrs_embeds import build_final_summary_embed, build_result_embed
from modules.tvrs_formatting import clip_text, format_bill_number, role_label, ru_ordinal
from modules.tvrs_hub_views import TVRSBaseView
from modules.tvrs_navigation_runtime import open_tvrs_hub
from modules.tvrs_presentation import (
    build_dm_vote_embed,
    build_live_vote_embed,
    build_registration_embed,
    delete_sticky_message,
    is_chair,
    queue_short_lines,
    voice_participants,
)

_public_status_locks: dict[int, asyncio.Lock] = {}


def _preflight(
    guild: discord.Guild,
    leader_id: int,
) -> tuple[list, ConsensusPreflight]:
    participants, voice_error = voice_participants(guild)
    queue_count = len(storage.tvrs_queue_bills(guild.id, limit=1000))
    report = evaluate_consensus_preflight(
        participants,
        queue_count=queue_count,
        leader_id=leader_id,
        rules=DEFAULT_CONSENSUS_RULES,
        voice_error=voice_error,
    )
    return participants, report


def build_preparation_embed(
    guild: discord.Guild,
    leader_id: int,
) -> tuple[discord.Embed, ConsensusPreflight]:
    participants, report = _preflight(guild, leader_id)
    plenary_number = storage.tvrs_get_next_plenary_number(
        guild.id,
        TVRS_DEFAULT_NEXT_PLENARY_NUMBER,
    )
    embed = discord.Embed(
        title=f"🧭 Подготовка заседания • {ru_ordinal(plenary_number)}",
        description=(
            "Проверьте повестку и состав. Рабочая сессия и приглашения будут созданы "
            "только после нажатия **«Подготовить заседание»**."
        ),
        color=TVRS_EMBED_COLOR,
    )
    embed.add_field(
        name="Повестка",
        value=(
            f"Законопроектов: **{report.queue_count}**\n"
            f"{queue_short_lines(guild.id, limit=6)}"
        )[:1024],
        inline=False,
    )
    embed.add_field(
        name="Состав в голосовом канале",
        value=(
            f"Председатели: **{report.chair_count}** · "
            f"сенаторы: **{report.senator_count}**\n"
            f"Ведущий в войсе: {'✅' if report.leader_present else '❌'} · "
            f"кворум: {'✅' if report.quorum_ready else '❌'}"
        ),
        inline=False,
    )
    if participants:
        embed.add_field(
            name="Будущие участники",
            value="\n".join(
                f"• {item.mention} — **{role_label(item)}**"
                for item in participants
            )[:1024],
            inline=False,
        )
    embed.add_field(
        name="Готовность",
        value=(
            "✅ **Можно открывать регистрацию.**"
            if report.can_open_registration
            else "\n".join(f"• {item}" for item in report.blockers)[:1024]
        ),
        inline=False,
    )
    embed.set_footer(
        text=(
            f"Consensus V{CONSENSUS_ENGINE_VERSION} • войс <#{TVRS_CONSENSUS_VOICE_CHANNEL_ID}> "
            f"• правила v{DEFAULT_CONSENSUS_RULES.version}"
        )
    )
    return embed, report


async def open_consensus_registration(
    bot,
    guild: discord.Guild,
    leader: discord.Member,
    *,
    schedule_id: int | None = None,
) -> LiveConsensusSession:
    """Open registration through the canonical application workflow.

    Discord buttons and the web leader console deliberately share this
    function, so preflight, durable delivery, registry and public projection
    cannot drift into two different implementations.
    """

    participants, report = _preflight(guild, leader.id)
    if not report.can_open_registration:
        reason = "; ".join(report.blockers) or "условия открытия не выполнены"
        raise ConsensusStateError(f"Регистрация не открыта: {reason}")
    async with consensus_session_lock(guild.id):
        active = _active_sessions.get(guild.id)
        if active is not None and not active.finished:
            raise ConsensusStateError(
                f"Заседание уже открыто ведущим {active.leader_display}."
            )
        participants, report = _preflight(guild, leader.id)
        if not report.can_open_registration:
            reason = "; ".join(report.blockers) or "условия изменились"
            raise ConsensusStateError(f"Регистрация не открыта: {reason}")
        plenary_number = await asyncio.to_thread(
            storage.tvrs_get_next_plenary_number,
            guild.id,
            TVRS_DEFAULT_NEXT_PLENARY_NUMBER,
        )
        session = LiveConsensusSession(
            session_key=f"{guild.id}:v3:{uuid.uuid4().hex[:12]}",
            guild_id=guild.id,
            channel_id=TVRS_BILLS_CHANNEL_ID,
            leader_id=leader.id,
            leader_display=leader.display_name,
            plenary_number=plenary_number,
            participants={item.user_id: item for item in participants},
            engine_version=CONSENSUS_ENGINE_VERSION,
        )
        session.participants[leader.id].confirmed = True
        _consensus_registry.add(session)
        try:
            deliveries = build_control_dm_deliveries(
                session,
                phase="registration",
            )
            await run_blocking_cancellation_safe(
                _consensus.save_with_deliveries,
                session,
                "v3_registration_opened",
                actor=ConsensusActor(leader.id, leader.display_name),
                details={
                    "engine_version": CONSENSUS_ENGINE_VERSION,
                    "participant_count": len(session.participants),
                    "queue_count": report.queue_count,
                },
                deliveries=deliveries,
            )
        except Exception:
            _consensus_registry.remove(
                guild.id,
                session_key=session.session_key,
            )
            raise

    wake_delivery_worker()
    if schedule_id is not None:
        try:
            planned = await asyncio.to_thread(
                schedule_storage.get_consensus_schedule,
                int(schedule_id),
            )
            if (
                planned is not None
                and int(planned.get("guild_id") or 0) == int(guild.id)
                and str(planned.get("status") or "") == "scheduled"
            ):
                await asyncio.to_thread(
                    schedule_storage.start_consensus_schedule,
                    int(guild.id),
                    session_key=str(session.session_key),
                    schedule_id=int(schedule_id),
                )
        except (OSError, ValueError):
            # The live session is already durable and must not be rolled back
            # because a non-critical planning projection failed.
            pass
    await delete_sticky_message(bot, guild)
    await ensure_public_consensus_card(bot, guild, session)
    wake_operations_worker()
    return session


def build_observer_embed(session: LiveConsensusSession) -> discord.Embed:
    quorum_label = (
        "⏸️ заседание приостановлено"
        if session.stage == "paused"
        else ("✅ набран" if session.quorum_ready() else "❌ не набран")
    )
    embed = discord.Embed(
        title=f"⚖️ Заседание • {ru_ordinal(session.plenary_number)}",
        description=consensus_progress_text(session.stage),
        color=TVRS_EMBED_COLOR,
    )
    embed.add_field(name="Этап", value=f"**{clean_stage_name(session.stage)}**", inline=True)
    embed.add_field(name="Ведущий", value=f"<@{session.leader_id}>", inline=True)
    embed.add_field(
        name="Участники",
        value=(
            f"Подтвердились: **{len(session.confirmed_participants())}/{len(session.participants)}**\n"
            f"Кворум: {quorum_label}"
        ),
        inline=True,
    )
    if session.stage == "paused" and session.paused_reason:
        embed.add_field(
            name="Причина паузы",
            value=clip_text(session.paused_reason, 900),
            inline=False,
        )
    bill = session.current_bill or {}
    if bill:
        embed.add_field(
            name=f"Текущий проект №{format_bill_number(int(bill.get('bill_number') or 0))}",
            value=(
                f"**{clip_text(bill.get('title'), 220)}**\n"
                + (
                    "Воут: **ожидает команды ведущего**"
                    if session.stage == "presentation"
                    else f"Проголосовали: **{len(session.votes)}/{len(session.confirmed_participants())}**"
                )
            )[:1024],
            inline=False,
        )
    if session.results:
        result = session.results[-1]
        status = {
            "accepted": "✅ принят",
            "rejected": "❌ отклонён",
            "vetoed": "🛑 применено вето",
        }.get(result.status, result.status)
        embed.add_field(
            name=f"Последний результат · №{format_bill_number(result.bill_number)}",
            value=f"**{status}** · общий консенсус **{result.overall_percent}%**",
            inline=False,
        )
    embed.set_footer(
        text=(
            f"Consensus V{session.engine_version} • режим наблюдения • "
            "содержание голосов раскрывается только после результата"
        )
    )
    return embed


def build_participant_portal_embed(
    session: LiveConsensusSession,
    user_id: int,
) -> discord.Embed:
    participant = session.participants[int(user_id)]
    if session.stage == "registration":
        embed = discord.Embed(
            title=f"👤 Личный пульт • {ru_ordinal(session.plenary_number)}",
            description=consensus_progress_text(session.stage),
            color=TVRS_EMBED_COLOR,
        )
        embed.add_field(name="Ваша роль", value=f"**{role_label(participant)}**", inline=True)
        embed.add_field(
            name="Регистрация",
            value="✅ участие подтверждено" if participant.confirmed else "⏳ требуется подтверждение",
            inline=True,
        )
        embed.add_field(
            name="Повестка",
            value=queue_short_lines(session.guild_id, limit=6),
            inline=False,
        )
        embed.set_footer(text="Этот пульт работает на сервере даже при закрытых ЛС")
        return embed
    if session.stage == "after_result" and session.results:
        embed = build_result_embed(session.results[-1], session)
        embed.description = (
            f"{embed.description or ''}\n\n{consensus_progress_text(session.stage)}"
        ).strip()
        embed.set_footer(text="Личный пульт • ожидайте следующий проект или итоговый протокол")
        return embed
    embed = build_dm_vote_embed(session, participant)
    embed.description = (
        f"{embed.description or ''}\n\n{consensus_progress_text(session.stage)}"
    ).strip()
    embed.set_footer(
        text=(
            "Личный пульт V3 • действия полностью равнозначны кнопкам в ЛС"
            if session.stage == "voting"
            else "Личный пульт V3 • обновите карточку после смены этапа"
        )
    )
    return embed


def _public_status_meta_key(guild_id: int) -> str:
    return f"tvrs_consensus_public_status_message_id:{int(guild_id)}"


def _public_status_marker(session_key: str) -> str:
    return f"tmod-consensus-status:{str(session_key)[:72]}"


def _public_projection_is_current(session: LiveConsensusSession) -> bool:
    """Fence delayed writes from a session already replaced in memory."""

    current = _active_sessions.get(int(session.guild_id))
    return current is None or current.session_key == session.session_key


async def ensure_public_consensus_card(
    bot,
    guild: discord.Guild,
    session: LiveConsensusSession,
    *,
    terminal: bool = False,
) -> discord.Message | None:
    """Create or update the one canonical status card for the guild.

    The stored message id is preferred.  A footer marker recovers the same
    message if the process stopped after Discord accepted a send but before the
    receipt reached SQLite.  While the session is active, its single persistent
    entry button opens a fresh private control surface for the requester.
    """

    lock = _public_status_locks.setdefault(int(guild.id), asyncio.Lock())
    async with lock:
        if not _public_projection_is_current(session):
            return None
        return await _ensure_public_consensus_card_unlocked(
            bot,
            guild,
            session,
            terminal=terminal,
        )


async def _ensure_public_consensus_card_unlocked(
    bot,
    guild: discord.Guild,
    session: LiveConsensusSession,
    *,
    terminal: bool,
) -> discord.Message | None:
    guild_get_channel = getattr(guild, "get_channel", None)
    channel = (
        guild_get_channel(session.channel_id)
        if callable(guild_get_channel)
        else None
    )
    if channel is None and hasattr(bot, "get_channel"):
        channel = bot.get_channel(session.channel_id)
    if channel is None or not hasattr(channel, "send"):
        return None
    meta_key = _public_status_meta_key(guild.id)
    try:
        raw_message_id = await asyncio.to_thread(_activity_storage.get_meta, meta_key)
    except (OSError, sqlite3.Error):
        return None
    message_id = int(raw_message_id) if raw_message_id and str(raw_message_id).isdigit() else 0
    message = None
    if message_id and hasattr(channel, "fetch_message"):
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

    marker = _public_status_marker(session.session_key)
    if message is None and hasattr(channel, "history"):
        try:
            async for candidate in channel.history(limit=50):
                if any(
                    marker in str(getattr(getattr(embed, "footer", None), "text", "") or "")
                    for embed in getattr(candidate, "embeds", ())
                ):
                    message = candidate
                    break
        except discord.DiscordException:
            # A history transport failure must not create a possible duplicate.
            return None

    # Fetch/history can yield while another task opens the next session.  Check
    # the generation again immediately before the Discord write so a delayed
    # terminal projection cannot remove the new session's entry button.
    if not _public_projection_is_current(session):
        return None
    if terminal and session.stage == "cancelled":
        embed = build_observer_embed(session)
        embed.title = f"⚪ Заседание отменено • {ru_ordinal(session.plenary_number)}"
        embed.description = "Регистрация закрыта ведущим. Решения на этом заседании не принимались."
    else:
        embed = build_final_summary_embed(session) if terminal else build_observer_embed(session)
    view = None if terminal else TVRSConsensusEntryView()
    footer_prefix = str(getattr(embed.footer, "text", "") or "").strip()
    embed.set_footer(text=f"{footer_prefix} • {marker}" if footer_prefix else marker)
    try:
        if message is None:
            message = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await message.edit(
                content=None,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await asyncio.to_thread(
            _activity_storage.set_meta_value,
            meta_key,
            str(message.id),
        )
    except (discord.DiscordException, OSError, sqlite3.Error):
        return None
    return message


class TVRSConsensusEntryView(TVRSBaseView):
    """Stable public entry point that always resolves the current live session."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label="Открыть личный пульт",
            emoji="⚖️",
            style=discord.ButtonStyle.primary,
            custom_id="tvrs_consensus_entry:v1",
        )
        button.callback = self.open_personal_panel
        self.add_item(button)

    async def open_personal_panel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                content="Личный пульт консенсуса работает только на сервере Discord.",
                ephemeral=True,
            )
            return

        session = _active_sessions.get(int(interaction.guild.id))
        if session is None or session.finished:
            await interaction.response.send_message(
                content=(
                    "Сейчас активного заседания нет. Карточка будет обновлена автоматически."
                ),
                ephemeral=True,
            )
            return

        user_id = int(interaction.user.id)
        if user_id == session.leader_id:
            from modules.tvrs_consensus_views import (
                TVRSAfterResultView,
                TVRSHostVoteView,
                TVRSRegistrationView,
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
            from modules.consensus_web import consensus_web_entry_url

            web_url = consensus_web_entry_url(
                guild_id=session.guild_id,
                user_id=user_id,
            )
            host_url = consensus_web_entry_url(
                guild_id=session.guild_id,
                user_id=user_id,
                destination="/host",
            )
            await interaction.response.send_message(
                content=(
                    f"🖥️ [Открыть персональный веб-пульт]({web_url})\n"
                    f"◉ [Открыть живой суфлёр ведущего]({host_url})"
                ),
                embed=embed,
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        participant = session.participants.get(user_id)
        if participant is not None and (
            session.stage == "registration" or participant.confirmed
        ):
            await interaction.response.send_message(
                embed=build_participant_portal_embed(session, user_id),
                view=TVRSParticipantPortalView(session.session_key, user_id),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if participant is not None:
            explanation = (
                "Вы не подтвердили участие во время регистрации, поэтому для этого "
                "заседания доступен режим наблюдения."
            )
        else:
            explanation = (
                "Вы не входите в состав этого заседания. Открыт безопасный режим наблюдения."
            )
        await interaction.response.send_message(
            content=explanation,
            embed=build_observer_embed(session),
            view=TVRSObserverView(user_id, session.session_key),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _RequesterPortalView(TVRSBaseView):
    def __init__(self, requester_id: int, *, timeout: float = 900) -> None:
        super().__init__(timeout=timeout)
        self.requester_id = int(requester_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Этот личный пульт открыт для другого участника.",
                ephemeral=True,
            )
            return False
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Пульт работает только на сервере Discord.",
                ephemeral=True,
            )
            return False
        return True


class TVRSPreparationView(_RequesterPortalView):
    def __init__(
        self,
        requester_id: int,
        guild_id: int,
        report: ConsensusPreflight,
        schedule_id: int | None = None,
    ) -> None:
        super().__init__(requester_id, timeout=600)
        self.guild_id = int(guild_id)
        self.schedule_id = int(schedule_id) if schedule_id else None
        open_button = discord.ui.Button(
            label="Подготовить заседание",
            emoji="✅",
            style=discord.ButtonStyle.success,
            disabled=not report.can_open_registration,
            row=0,
        )
        open_button.callback = self.open_registration
        self.add_item(open_button)
        refresh = discord.ui.Button(
            label="Проверить снова",
            emoji="🔄",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        refresh.callback = self.refresh
        self.add_item(refresh)
        back = discord.ui.Button(
            label="Назад",
            emoji="⬅️",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        back.callback = self.back
        self.add_item(back)
        from modules.consensus_web import consensus_web_entry_url

        self.add_item(
            discord.ui.Button(
                label="Веб-пульт",
                emoji="🖥️",
                style=discord.ButtonStyle.link,
                url=consensus_web_entry_url(
                    guild_id=self.guild_id,
                    user_id=self.requester_id,
                ),
                row=1,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Суфлёр ведущего",
                emoji="🎙️",
                style=discord.ButtonStyle.link,
                url=consensus_web_entry_url(
                    guild_id=self.guild_id,
                    user_id=self.requester_id,
                    destination="/host",
                ),
                row=1,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        assert isinstance(interaction.user, discord.Member)
        if not is_chair(interaction.user):
            await interaction.response.send_message(
                "Подготовить заседание может только председатель.",
                ephemeral=True,
            )
            return False
        return True

    async def refresh(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        embed, report = build_preparation_embed(interaction.guild, interaction.user.id)
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=TVRSPreparationView(
                interaction.user.id,
                interaction.guild.id,
                report,
                schedule_id=self.schedule_id,
            ),
        )

    async def back(self, interaction: discord.Interaction) -> None:
        await open_tvrs_hub(interaction)

    async def open_registration(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        await interaction.response.defer()
        try:
            session = await open_consensus_registration(
                interaction.client,
                interaction.guild,
                interaction.user,
                schedule_id=self.schedule_id,
            )
        except ConsensusStateError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        from modules.tvrs_consensus_views import TVRSRegistrationView

        await interaction.edit_original_response(
            content=None,
            embed=build_registration_embed(session),
            view=TVRSRegistrationView(session.session_key),
        )
        await ensure_public_consensus_card(
            interaction.client,
            interaction.guild,
            session,
        )
        session.host_message_obj = interaction.message
        async with consensus_session_lock(interaction.guild.id):
            if _consensus_registry.find(session.session_key) is session:
                await run_blocking_cancellation_safe(
                    _consensus.bind_host_message,
                    session,
                    getattr(interaction.message, "id", None),
                    "v3_host_panel_bound",
                )


class TVRSParticipantPortalView(_RequesterPortalView):
    def __init__(self, session_key: str, user_id: int) -> None:
        super().__init__(user_id, timeout=900)
        self.session_key = str(session_key)
        self.user_id = int(user_id)
        session = self.session()
        participant = session.participants.get(self.user_id) if session else None
        if session and participant and session.stage == "registration" and not participant.confirmed:
            self._add_action("Подтвердить участие", "✅", discord.ButtonStyle.success, self.confirm, row=0)
        elif session and participant and session.stage == "voting":
            self._add_action("За", "✅", discord.ButtonStyle.success, self.vote_yes, row=0)
            self._add_action("Против", "❌", discord.ButtonStyle.danger, self.vote_no, row=0)
            self._add_action(
                "Воздержаться",
                "➖",
                discord.ButtonStyle.secondary,
                self.vote_abstain,
                row=0,
            )
            if participant.kind == "senator" and not session.discussion_initiator_id:
                self._add_action("Дискуссия", "💬", discord.ButtonStyle.secondary, self.discussion, row=0)
            if participant.permanent:
                self._add_action("Вето", "🛑", discord.ButtonStyle.danger, self.veto, row=0)
        self._add_action("Обновить", "🔄", discord.ButtonStyle.secondary, self.refresh, row=1)
        self._add_action("Обзор", "👁️", discord.ButtonStyle.secondary, self.observe, row=1)
        self._add_action("Назад", "⬅️", discord.ButtonStyle.secondary, self.back, row=1)
        if session is not None:
            from modules.consensus_web import consensus_web_entry_url

            self.add_item(
                discord.ui.Button(
                    label="Веб-бюллетень",
                    emoji="🖥️",
                    style=discord.ButtonStyle.link,
                    url=consensus_web_entry_url(
                        guild_id=session.guild_id,
                        user_id=self.user_id,
                    ),
                    row=2,
                )
            )

    def _add_action(self, label, emoji, style, callback, *, row: int) -> None:
        button = discord.ui.Button(label=label, emoji=emoji, style=style, row=row)
        button.callback = callback
        self.add_item(button)

    def session(self) -> LiveConsensusSession | None:
        return next(
            (
                item
                for item in _active_sessions.values()
                if item.session_key == self.session_key and not item.finished
            ),
            None,
        )

    async def _require_session(self, interaction: discord.Interaction) -> LiveConsensusSession | None:
        session = self.session()
        if session is None or self.user_id not in session.participants:
            await interaction.response.send_message(
                "Ваше активное заседание не найдено.",
                ephemeral=True,
            )
            return None
        return session

    async def confirm(self, interaction: discord.Interaction) -> None:
        from modules.tvrs_consensus_views import confirm_consensus_participant

        await confirm_consensus_participant(
            interaction,
            self.session_key,
            self.user_id,
            refreshed_portal=True,
        )

    async def _vote(self, interaction: discord.Interaction, value: str) -> None:
        from modules.tvrs_consensus_views import TVRSVoteView

        session = await self._require_session(interaction)
        if session is None:
            return
        bill_id = int((session.current_bill or {}).get("id") or 0)
        replacement = TVRSParticipantPortalView(session.session_key, self.user_id)
        await TVRSVoteView(
            session.session_key,
            self.user_id,
            bill_id=bill_id,
        )._cast(interaction, value, replacement_view=replacement)

    async def vote_yes(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, "yes")

    async def vote_no(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, "no")

    async def vote_abstain(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, "abstain")

    async def discussion(self, interaction: discord.Interaction) -> None:
        from modules.tvrs_consensus_views import TVRSVoteView

        session = await self._require_session(interaction)
        if session is None:
            return
        bill_id = int((session.current_bill or {}).get("id") or 0)
        await TVRSVoteView(
            session.session_key,
            self.user_id,
            bill_id=bill_id,
        )._discussion_callback(interaction)

    async def veto(self, interaction: discord.Interaction) -> None:
        from modules.tvrs_consensus_views import TVRSVoteView

        session = await self._require_session(interaction)
        if session is None:
            return
        bill_id = int((session.current_bill or {}).get("id") or 0)
        await TVRSVoteView(
            session.session_key,
            self.user_id,
            bill_id=bill_id,
        )._veto_callback(interaction)

    async def refresh(self, interaction: discord.Interaction) -> None:
        session = await self._require_session(interaction)
        if session is None:
            return
        await interaction.response.edit_message(
            content=None,
            embed=build_participant_portal_embed(session, self.user_id),
            view=TVRSParticipantPortalView(session.session_key, self.user_id),
        )

    async def observe(self, interaction: discord.Interaction) -> None:
        session = await self._require_session(interaction)
        if session is None:
            return
        await interaction.response.edit_message(
            content=None,
            embed=build_observer_embed(session),
            view=TVRSObserverView(self.user_id, session.session_key),
        )

    async def back(self, interaction: discord.Interaction) -> None:
        await open_tvrs_hub(interaction)


class TVRSObserverView(_RequesterPortalView):
    def __init__(self, requester_id: int, session_key: str) -> None:
        super().__init__(requester_id, timeout=900)
        self.session_key = str(session_key)
        self._add("Обновить", "🔄", self.refresh)
        self._add("Назад", "⬅️", self.back)
        session = self.session()
        if session is not None:
            from modules.consensus_web import consensus_web_entry_url

            self.add_item(
                discord.ui.Button(
                    label="Открыть веб-экран",
                    emoji="🖥️",
                    style=discord.ButtonStyle.link,
                    url=consensus_web_entry_url(
                        guild_id=session.guild_id,
                        user_id=self.requester_id,
                    ),
                )
            )

    def _add(self, label: str, emoji: str, callback) -> None:
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
        )
        button.callback = callback
        self.add_item(button)

    def session(self) -> LiveConsensusSession | None:
        return next(
            (
                item
                for item in _active_sessions.values()
                if item.session_key == self.session_key and not item.finished
            ),
            None,
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        session = self.session()
        if session is None:
            await interaction.response.edit_message(
                content="Заседание завершено. Итог опубликован в штатном канале.",
                embed=None,
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=None,
            embed=build_observer_embed(session),
            view=TVRSObserverView(self.requester_id, session.session_key),
        )

    async def back(self, interaction: discord.Interaction) -> None:
        await open_tvrs_hub(interaction)


async def open_preparation_portal(
    interaction: discord.Interaction,
    *,
    schedule_id: int | None = None,
) -> None:
    assert interaction.guild is not None
    embed, report = build_preparation_embed(interaction.guild, interaction.user.id)
    await interaction.response.edit_message(
        content=None,
        embed=embed,
        view=TVRSPreparationView(
            interaction.user.id,
            interaction.guild.id,
            report,
            schedule_id=schedule_id,
        ),
    )


async def open_participant_portal(
    interaction: discord.Interaction,
    session: LiveConsensusSession,
) -> None:
    if interaction.user.id not in session.participants:
        await interaction.response.send_message(
            "Вы не входите в состав этого заседания.",
            ephemeral=True,
        )
        return
    await interaction.response.edit_message(
        content=None,
        embed=build_participant_portal_embed(session, interaction.user.id),
        view=TVRSParticipantPortalView(session.session_key, interaction.user.id),
    )


async def open_observer_portal(
    interaction: discord.Interaction,
    session: LiveConsensusSession,
) -> None:
    await interaction.response.edit_message(
        content=None,
        embed=build_observer_embed(session),
        view=TVRSObserverView(interaction.user.id, session.session_key),
    )


__all__ = [
    "TVRSConsensusEntryView",
    "TVRSObserverView",
    "TVRSParticipantPortalView",
    "TVRSPreparationView",
    "build_observer_embed",
    "build_participant_portal_embed",
    "build_preparation_embed",
    "ensure_public_consensus_card",
    "open_observer_portal",
    "open_participant_portal",
    "open_preparation_portal",
]
