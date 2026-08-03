from __future__ import annotations

import discord

from modules.async_safety import run_blocking_cancellation_safe
from modules.consensus_core import (
    ConsensusStateError,
    LiveConsensusSession,
)
from modules.consensus_runtime import (
    active_sessions as _active_sessions,
    coordinator as _consensus,
    registry as _consensus_registry,
    session_lock as consensus_session_lock,
)
from modules.consensus_service import ConsensusActor
from modules.delivery_runtime import wake_delivery_worker
from modules.operations_runtime import wake_operations_worker
from modules.tvrs_config import (
    TVRS_PERMANENT_CHAIR_ID,
    TVRS_TIMER_OPTIONS,
)
from modules.tvrs_embeds import (
    build_result_embed,
)
from modules.tvrs_formatting import (
    format_timer,
)
from modules.tvrs_delivery import (
    build_control_dm_deliveries,
)

from modules.tvrs_presentation import (
    build_dm_vote_embed,
    build_live_vote_embed,
    consensus_bill_id,
    consensus_generation_matches,
)
from modules.tvrs_hub_views import TVRSBaseView
from modules.tvrs_result_views import TVRSAfterResultView, TVRSVetoConfirmView
from modules.tvrs_registration_gate import (
    TVRSStartCurrentRosterConfirmView,
    build_incomplete_roster_warning,
)

async def ensure_sticky_message(*args, **kwargs):
    from modules.tvrs_recovery import ensure_sticky_message as _implementation
    return await _implementation(*args, **kwargs)

async def end_discussion(*args, **kwargs):
    from modules.tvrs_discussion import end_discussion as _implementation
    return await _implementation(*args, **kwargs)

async def pause_session(*args, **kwargs):
    from modules.tvrs_discussion import pause_session as _implementation
    return await _implementation(*args, **kwargs)

async def request_discussion(*args, **kwargs):
    from modules.tvrs_discussion import request_discussion as _implementation
    return await _implementation(*args, **kwargs)

async def resume_session(*args, **kwargs):
    from modules.tvrs_discussion import resume_session as _implementation
    return await _implementation(*args, **kwargs)

def session_voice_quorum_ready(*args, **kwargs):
    from modules.tvrs_discussion import session_voice_quorum_ready as _implementation
    return _implementation(*args, **kwargs)

async def set_vote_timer(*args, **kwargs):
    from modules.tvrs_discussion import set_vote_timer as _implementation
    return await _implementation(*args, **kwargs)

async def start_discussion_channel(*args, **kwargs):
    from modules.tvrs_discussion import start_discussion_channel as _implementation
    return await _implementation(*args, **kwargs)

async def begin_next_bill_vote(*args, **kwargs):
    from modules.tvrs_control import begin_next_bill_vote as _implementation
    return await _implementation(*args, **kwargs)

async def retry_pending_finalization_once(*args, **kwargs):
    from modules.tvrs_control import retry_pending_finalization_once as _implementation
    return await _implementation(*args, **kwargs)

async def update_host_registration_message(*args, **kwargs):
    from modules.tvrs_control import update_host_registration_message as _implementation
    return await _implementation(*args, **kwargs)

async def update_host_vote_message(*args, **kwargs):
    from modules.tvrs_control import update_host_vote_message as _implementation
    return await _implementation(*args, **kwargs)

async def apply_veto(*args, **kwargs):
    from modules.tvrs_decision import apply_veto as _implementation
    return await _implementation(*args, **kwargs)

async def finalize_current_vote(*args, **kwargs):
    from modules.tvrs_decision import finalize_current_vote as _implementation
    return await _implementation(*args, **kwargs)

async def finish_session(*args, **kwargs):
    from modules.tvrs_decision import finish_session as _implementation
    return await _implementation(*args, **kwargs)

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
        unconfirmed = [
            participant
            for participant in session.participants.values()
            if not participant.confirmed
        ]
        if unconfirmed:
            await interaction.response.send_message(
                build_incomplete_roster_warning(session),
                view=TVRSStartCurrentRosterConfirmView(
                    session.session_key,
                    session.leader_id,
                    confirmed_user_ids={
                        participant.user_id
                        for participant in session.confirmed_participants()
                    },
                ),
                ephemeral=True,
            )
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
        cancelled_session: LiveConsensusSession | None = None
        async with consensus_session_lock(interaction.guild.id):
            current = self.session(interaction.guild.id)
            if current is None or current.stage != "registration":
                await interaction.followup.send("Сессия уже завершена.", ephemeral=True)
                return
            await run_blocking_cancellation_safe(
                _consensus.finish_atomically,
                current,
                actor=ConsensusActor(
                    interaction.user.id,
                    getattr(interaction.user, "display_name", str(interaction.user)),
                ),
                cancelled=True,
            )
            cancelled_session = current
        assert cancelled_session is not None
        # Remove only after leaving the guild lock: popping a lock while it is
        # held would allow the next session to create a second lock generation.
        _consensus_registry.remove(
            interaction.guild.id,
            session_key=cancelled_session.session_key,
        )
        wake_operations_worker()
        from modules.tvrs_consensus_portal import ensure_public_consensus_card

        await ensure_public_consensus_card(
            interaction.client,
            interaction.guild,
            cancelled_session,
            terminal=True,
        )
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
            await run_blocking_cancellation_safe(
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
        await confirm_consensus_participant(
            interaction,
            self.session_key,
            self.user_id,
        )


async def confirm_consensus_participant(
    interaction: discord.Interaction,
    session_key: str,
    user_id: int,
    *,
    refreshed_portal: bool = False,
) -> LiveConsensusSession | None:
    """Confirm from either a DM or the V3 in-server personal portal."""

    if interaction.user.id != int(user_id):
        await interaction.response.send_message("Это подтверждение не для вас.", ephemeral=True)
        return None
    session = next(
        (item for item in _active_sessions.values() if item.session_key == session_key),
        None,
    )
    if session is None:
        await interaction.response.send_message("Сессия консенсуса уже закрыта.", ephemeral=True)
        return None
    participant = session.participants.get(int(user_id))
    if participant is None:
        await interaction.response.send_message("Вы не указаны как участник консенсуса.", ephemeral=True)
        return None
    if session.stage != "registration":
        await interaction.response.send_message(
            "Регистрация на это заседание уже завершена.",
            ephemeral=True,
        )
        return None
    await interaction.response.defer()
    async with consensus_session_lock(session.guild_id):
        if session.stage != "registration":
            await interaction.followup.send("Регистрация уже завершена.", ephemeral=True)
            return None
        await run_blocking_cancellation_safe(
            _consensus.confirm_participant,
            session,
            int(user_id),
            actor=ConsensusActor(
                interaction.user.id,
                getattr(interaction.user, "display_name", str(interaction.user)),
            ),
        )
    guild = interaction.client.get_guild(session.guild_id)
    if guild:
        await update_host_registration_message(interaction.client, guild, session)
    if refreshed_portal:
        from modules.tvrs_consensus_portal import (
            TVRSParticipantPortalView,
            build_participant_portal_embed,
        )

        await interaction.edit_original_response(
            content=None,
            embed=build_participant_portal_embed(session, int(user_id)),
            view=TVRSParticipantPortalView(session.session_key, int(user_id)),
        )
    else:
        await interaction.edit_original_response(
            content="Участие подтверждено.",
            embed=None,
            view=None,
        )
    return session


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
                (
                    "Воздержаться",
                    discord.ButtonStyle.secondary,
                    "tvrs_vote_abstain",
                    self._abstain_callback,
                ),
            ]
            if not session.discussion_initiator_id:
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

    async def _abstain_callback(self, interaction: discord.Interaction) -> None:
        await self._cast(interaction, "abstain")

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
        if p is None or not p.confirmed:
            await interaction.response.send_message(
                "Дискуссию может инициировать только зарегистрированный участник.",
                ephemeral=True,
            )
            return
        if session.discussion_initiator_id:
            await interaction.response.send_message("Дискуссия по этому законопроекту уже инициирована.", ephemeral=True)
            return
        guild = interaction.client.get_guild(session.guild_id)
        if guild is None:
            await interaction.response.send_message("Сервер не найден.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await request_discussion(
                interaction.client,
                guild,
                session,
                p,
                expected_bill_id=self.bill_id,
            )
        except ConsensusStateError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
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

    async def _cast(
        self,
        interaction: discord.Interaction,
        vote: str,
        *,
        replacement_view: discord.ui.View | None = None,
    ) -> None:
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
            should_finalize = await run_blocking_cancellation_safe(
                _consensus.cast_vote,
                session,
                self.user_id,
                vote,
                actor=ConsensusActor(
                    interaction.user.id,
                    getattr(interaction.user, "display_name", str(interaction.user)),
                ),
            )
        guild = (
            getattr(interaction, "guild", None)
            or interaction.client.get_guild(session.guild_id)
        )
        # Finalization is the durable business action.  It must run before any
        # best-effort Discord projection: an unavailable/stale DM or host panel
        # must never leave a fully voted bill stuck in the voting stage.
        if should_finalize and guild:
            await finalize_current_vote(
                interaction.client,
                guild,
                session,
                forced=False,
                expected_bill_id=self.bill_id,
            )
            return
        try:
            await interaction.message.edit(
                embed=build_dm_vote_embed(session, session.participants[self.user_id]),
                view=(
                    replacement_view
                    or TVRSVoteView(
                        session.session_key,
                        self.user_id,
                        bill_id=self.bill_id,
                    )
                ),
            )  # type: ignore[union-attr]
        except discord.DiscordException:
            pass
        if guild:
            try:
                await update_host_vote_message(interaction.client, guild, session)
            except discord.DiscordException:
                pass


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
            try:
                await start_discussion_channel(
                    interaction.client,
                    guild,
                    session,
                    label,
                    expected_bill_id=self.bill_id,
                )
            except ConsensusStateError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
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
        if session and session.stage == "presentation":
            self._add_button(
                "Поставить на воут",
                discord.ButtonStyle.success,
                self.open_vote,
                row=0,
            )
            self._add_button(
                "Пауза",
                discord.ButtonStyle.secondary,
                self.pause,
                row=0,
            )
            self._add_button(
                "Завершить консенсус",
                discord.ButtonStyle.danger,
                self.finish_session_btn,
                row=0,
            )
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
        self._add_button(
            "Воздержаться",
            discord.ButtonStyle.secondary,
            self.host_abstain,
            row=0,
        )
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
            should_finalize = await run_blocking_cancellation_safe(
                _consensus.cast_vote,
                session,
                session.leader_id,
                "yes",
                actor=ConsensusActor(
                    interaction.user.id,
                    getattr(interaction.user, "display_name", str(interaction.user)),
                ),
            )
        if should_finalize:
            await finalize_current_vote(interaction.client, interaction.guild, session, forced=False, expected_bill_id=self.bill_id)
            return
        try:
            await interaction.edit_original_response(embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.DiscordException:
            pass

    async def open_vote(self, interaction: discord.Interaction) -> None:
        from modules.tvrs_vote_opening import open_vote_from_interaction
        await open_vote_from_interaction(interaction, self.session_key, self.bill_id)

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
            should_finalize = await run_blocking_cancellation_safe(
                _consensus.cast_vote,
                session,
                session.leader_id,
                "no",
                actor=ConsensusActor(
                    interaction.user.id,
                    getattr(interaction.user, "display_name", str(interaction.user)),
                ),
            )
        if should_finalize:
            await finalize_current_vote(interaction.client, interaction.guild, session, forced=False, expected_bill_id=self.bill_id)
            return
        try:
            await interaction.edit_original_response(embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.DiscordException:
            pass

    async def host_abstain(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session is None:
            await interaction.response.send_message("Сессия не найдена.", ephemeral=True)
            return
        if not consensus_generation_matches(
            session,
            stage="voting",
            bill_id=self.bill_id,
        ):
            await interaction.response.send_message(
                "Сейчас голосование недоступно.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        async with consensus_session_lock(session.guild_id):
            if not consensus_generation_matches(
                session,
                stage="voting",
                bill_id=self.bill_id,
            ):
                await interaction.followup.send(
                    "Эта панель относится к уже завершённому проекту.",
                    ephemeral=True,
                )
                return
            should_finalize = await run_blocking_cancellation_safe(
                _consensus.cast_vote,
                session,
                session.leader_id,
                "abstain",
                actor=ConsensusActor(
                    interaction.user.id,
                    getattr(interaction.user, "display_name", str(interaction.user)),
                ),
            )
        if should_finalize:
            await finalize_current_vote(
                interaction.client,
                interaction.guild,
                session,
                forced=False,
                expected_bill_id=self.bill_id,
            )
            return
        try:
            await interaction.edit_original_response(
                embed=build_live_vote_embed(session),
                view=TVRSHostVoteView(session.session_key),
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                ),
            )
        except discord.DiscordException:
            pass

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
            try:
                await start_discussion_channel(
                    interaction.client,
                    interaction.guild,
                    session,
                    label,
                    expected_bill_id=self.bill_id,
                )
            except ConsensusStateError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
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


__all__ = ['TVRSRegistrationView', 'TVRSStartCurrentRosterConfirmView', 'TVRSConfirmView', 'confirm_consensus_participant', 'TVRSVoteView', 'TVRSPermanentVoteView', 'TVRSRestoredVoteView', 'TVRSDiscussionTypeView', 'TVRSHostVoteView', 'TVRSVetoConfirmView', 'TVRSAfterResultView']
