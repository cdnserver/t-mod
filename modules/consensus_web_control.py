"""Application adapter for leader actions from the consensus web console."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from modules.async_safety import run_blocking_cancellation_safe
from modules.consensus_core import ConsensusStateError, LiveConsensusSession
from modules.consensus_finalization_recovery import retry_pending_finalization_once
from modules.consensus_runtime import (
    active_sessions,
    coordinator,
    session_lock,
)
from modules.consensus_service import ConsensusActor
from modules.consensus_simulator import (
    get_consensus_simulation,
    refresh_consensus_simulation_projection,
)
from modules.consensus_web_auth import ConsensusWebPrincipal
from modules.delivery_runtime import wake_delivery_worker
from modules.operations_runtime import wake_operations_worker
from modules.tvrs_config import TVRS_PERMANENT_CHAIR_ID
from modules.tvrs_control import (
    begin_next_bill_vote,
    update_host_registration_message,
    update_host_vote_message,
)
from modules.tvrs_decision import (
    apply_veto_for_actor,
    finalize_current_vote,
    finish_session,
)
from modules.tvrs_delivery import build_control_dm_deliveries
from modules.tvrs_discussion import (
    end_discussion,
    pause_session,
    publish_discussion_message,
    request_discussion,
    resume_session,
    session_voice_quorum_ready,
    set_vote_timer,
    start_discussion_channel,
)
from modules.tvrs_presentation import (
    consensus_bill_id,
    consensus_result_bill_id,
    is_chair,
    voice_participants,
)
from modules.tvrs_vote_opening import open_current_bill_vote
from persistence import consensus_schedule_repository as schedule_storage


class ConsensusWebCommandError(ConsensusStateError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.status = int(status)
        self.details = dict(details or {})


_command_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def _web_ballot_access_open(session: LiveConsensusSession) -> bool:
    schedule = schedule_storage.get_consensus_schedule_for_session(
        int(session.guild_id),
        str(session.session_key),
    )
    if schedule is None:
        candidate = schedule_storage.get_upcoming_consensus_schedule(
            int(session.guild_id)
        )
        if (
            candidate is not None
            and int(candidate.get("plenary_number") or 0)
            == int(session.plenary_number)
        ):
            schedule = candidate
    if schedule is None:
        return True
    try:
        scheduled_for = datetime.fromisoformat(
            str(schedule.get("scheduled_for") or "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return True
    if scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)
    try:
        unlock_seconds = max(
            0,
            min(3600, int(os.getenv("CONSENSUS_BALLOT_UNLOCK_SECONDS", "90") or 90)),
        )
    except (TypeError, ValueError):
        unlock_seconds = 90
    return datetime.now(timezone.utc) >= scheduled_for.astimezone(timezone.utc) - timedelta(
        seconds=unlock_seconds
    )


def _is_live_leader(
    session: LiveConsensusSession | None,
    principal: ConsensusWebPrincipal,
) -> bool:
    return bool(
        session
        and not session.finished
        and int(session.leader_id) == int(principal.user_id)
    )


def _participant_capabilities(
    session: LiveConsensusSession,
    principal: ConsensusWebPrincipal,
    *,
    allow_discussion_post: bool = True,
) -> list[str]:
    participant = session.participants.get(int(principal.user_id))
    if participant is None or not participant.confirmed:
        return []
    stage = str(session.stage)
    if (
        stage == "voting"
        and session.current_bill is not None
        and _web_ballot_access_open(session)
    ):
        capabilities = ["participant_vote"]
        if participant.kind == "senator" and not session.discussion_initiator_id:
            capabilities.append("request_discussion")
        return capabilities
    if (
        allow_discussion_post
        and stage == "discussion"
        and int(principal.user_id) in session.discussion_allowed_user_ids
    ):
        return ["post_discussion_message"]
    return []


def consensus_web_capabilities(
    *,
    mode: str,
    session: LiveConsensusSession | None,
    principal: ConsensusWebPrincipal | None,
) -> list[str]:
    if principal is None:
        return []
    stage = str(getattr(session, "stage", "") or "")
    if mode == "simulation":
        simulation = get_consensus_simulation(principal.guild_id)
        if (
            simulation is None
            or simulation.session is not session
        ):
            return []
        if simulation.leader_id == principal.user_id:
            participant = session.participants.get(int(principal.user_id))
            capabilities = _stage_capabilities(
                stage,
                permanent=bool(participant and participant.permanent),
                simulation=True,
            )
            if not simulation.has_fake_roster:
                capabilities = [
                    item
                    for item in capabilities
                    if item
                    not in {"confirm_next", "confirm_all", "fake_vote", "fake_scenario"}
                ]
            if simulation.has_real_roster and stage == "registration":
                capabilities.append("resend_invitations")
            return capabilities
        return _participant_capabilities(
            session,
            principal,
            allow_discussion_post=False,
        )
    if session is None:
        return ["open_registration"] if is_chair(principal.member) else []
    if not _is_live_leader(session, principal):
        return _participant_capabilities(session, principal)
    capabilities = _stage_capabilities(
        stage,
        permanent=principal.user_id == TVRS_PERMANENT_CHAIR_ID,
        simulation=False,
    )
    # The command itself resolves the bound schedule under optimistic locking.
    # Keeping capability calculation free of database I/O makes live polling
    # cheap; the UI enables this control only when a schedule payload exists.
    capabilities.append("update_session_settings")
    return capabilities


def _stage_capabilities(
    stage: str,
    *,
    permanent: bool,
    simulation: bool,
) -> list[str]:
    capabilities: list[str] = []
    if stage == "registration":
        capabilities.extend(
            ["start_vote", "cancel_session"]
            if simulation
            else ["start_vote", "resend_invitations", "cancel_session"]
        )
        if simulation:
            capabilities.extend(["confirm_next", "confirm_all"])
    elif stage == "presentation":
        capabilities.extend(["open_vote", "pause", "finish_session"])
    elif stage == "voting":
        capabilities.extend(
            [
                "leader_vote",
                "set_timer",
                "finalize_vote",
                "pause",
                "finish_session",
            ]
        )
        if simulation:
            capabilities.extend(
                [
                    "fake_vote",
                    "fake_scenario",
                    "request_discussion",
                ]
            )
        if permanent:
            capabilities.append("veto")
    elif stage == "finalizing":
        capabilities.append("retry_finalization")
    elif stage == "discussion_type":
        capabilities.extend(
            ["choose_discussion", "pause", "finish_session"]
        )
        if simulation:
            capabilities.extend(["oral_result"])
    elif stage == "discussion":
        capabilities.extend(["end_discussion", "pause", "finish_session"])
        if simulation:
            capabilities.extend(["oral_result"])
    elif stage == "paused":
        capabilities.extend(["resume", "finish_session"])
        if simulation:
            capabilities.append("oral_result")
    elif stage == "after_result":
        capabilities.extend(["next_bill", "finish_session"])
    return capabilities


def _require_confirmation(payload: dict[str, Any], action: str) -> None:
    if payload.get("confirm") is not True:
        raise ConsensusWebCommandError(
            "confirmation_required",
            f"Действие «{action}» требует явного подтверждения.",
            status=409,
        )


def _validate_generation(
    session: LiveConsensusSession,
    *,
    session_key: str,
    revision: int,
    bill_id: int | None,
) -> None:
    if str(session.session_key) != str(session_key):
        raise ConsensusWebCommandError(
            "stale_session",
            "В браузере открыта другая сессия. Панель уже обновляется.",
            status=409,
        )
    if int(session.revision) != int(revision):
        raise ConsensusWebCommandError(
            "stale_revision",
            "Состояние уже изменилось в Discord или другой вкладке.",
            status=409,
            details={"current_revision": int(session.revision)},
        )
    if bill_id is not None and int(bill_id) != consensus_bill_id(session):
        raise ConsensusWebCommandError(
            "stale_bill",
            "Команда относится к уже сменившемуся законопроекту.",
            status=409,
        )


def _validate_ballot_generation(
    session: LiveConsensusSession,
    *,
    session_key: str,
    bill_id: int | None,
) -> None:
    """Protect a ballot without conflicting with votes from other members."""

    if str(session.session_key) != str(session_key):
        raise ConsensusWebCommandError(
            "stale_session",
            "Открыт бюллетень другого заседания. Панель уже обновляется.",
            status=409,
        )
    if bill_id is None or int(bill_id) != consensus_bill_id(session):
        raise ConsensusWebCommandError(
            "stale_bill",
            "Этот бюллетень относится к уже сменившемуся законопроекту.",
            status=409,
        )


async def execute_consensus_web_command(
    bot: discord.Client,
    guild: discord.Guild,
    principal: ConsensusWebPrincipal,
    *,
    mode: str,
    action: str,
    session_key: str,
    revision: int,
    bill_id: int | None,
    payload: dict[str, Any],
) -> str:
    clean_mode = "simulation" if mode == "simulation" else "live"
    clean_action = str(action).strip().lower()
    async with _command_locks[int(guild.id)]:
        if clean_mode == "simulation":
            return await _execute_simulation(
                principal,
                action=clean_action,
                session_key=session_key,
                revision=revision,
                bill_id=bill_id,
                payload=payload,
            )
        return await _execute_live(
            bot,
            guild,
            principal,
            action=clean_action,
            session_key=session_key,
            revision=revision,
            bill_id=bill_id,
            payload=payload,
        )


async def _execute_live(
    bot: discord.Client,
    guild: discord.Guild,
    principal: ConsensusWebPrincipal,
    *,
    action: str,
    session_key: str,
    revision: int,
    bill_id: int | None,
    payload: dict[str, Any],
) -> str:
    session = active_sessions.get(int(guild.id))
    if action == "open_registration":
        if session is not None and not session.finished:
            raise ConsensusWebCommandError(
                "session_active",
                "Рабочее заседание уже открыто.",
                status=409,
            )
        if not is_chair(principal.member):
            raise ConsensusWebCommandError(
                "forbidden",
                "Открыть регистрацию может только председатель.",
                status=403,
            )
        from modules.tvrs_consensus_portal import open_consensus_registration

        schedule = await asyncio.to_thread(
            schedule_storage.get_upcoming_consensus_schedule,
            int(guild.id),
        )
        await open_consensus_registration(
            bot,
            guild,
            principal.member,
            schedule_id=(int(schedule["id"]) if schedule is not None else None),
        )
        return "Заседание подготовлено; приглашения поставлены в надёжную очередь."

    if session is None or session.finished:
        raise ConsensusWebCommandError(
            "session_missing",
            "Активный консенсус не найден.",
            status=409,
        )
    if action == "participant_vote":
        _validate_ballot_generation(
            session,
            session_key=session_key,
            bill_id=bill_id,
        )
        if "participant_vote" not in consensus_web_capabilities(
            mode="live",
            session=session,
            principal=principal,
        ):
            raise ConsensusWebCommandError(
                "vote_forbidden",
                "Вы не входите в подтверждённый состав этого голосования.",
                status=403,
            )
        vote = str(payload.get("vote") or "").strip().lower()
        if vote not in {"yes", "no", "abstain"}:
            raise ConsensusWebCommandError(
                "invalid_vote",
                "Выберите «за», «против» или «воздержаться».",
            )
        expected_bill_id = consensus_bill_id(session)
        actor = ConsensusActor(principal.user_id, principal.display_name)
        async with session_lock(session.guild_id):
            if (
                session.stage != "voting"
                or str(session.session_key) != str(session_key)
                or consensus_bill_id(session) != expected_bill_id
            ):
                raise ConsensusWebCommandError(
                    "stale_ballot",
                    "Голосование уже изменилось. Бюллетень обновляется.",
                    status=409,
                )
            participant = session.participants.get(int(principal.user_id))
            if participant is None or not participant.confirmed:
                raise ConsensusWebCommandError(
                    "vote_forbidden",
                    "Вы не входите в подтверждённый состав этого голосования.",
                    status=403,
                )
            should_finalize = await run_blocking_cancellation_safe(
                coordinator.cast_vote,
                session,
                int(principal.user_id),
                vote,
                actor=actor,
            )
        if should_finalize:
            await finalize_current_vote(
                bot,
                guild,
                session,
                forced=False,
                expected_bill_id=expected_bill_id,
            )
        else:
            try:
                await update_host_vote_message(bot, guild, session)
            except discord.DiscordException:
                # The vote is already durable. A Discord projection outage must
                # not turn a successful browser ballot into an HTTP failure.
                pass
        return "Ваш голос принят и синхронизирован с Discord."
    if action == "request_discussion":
        _validate_ballot_generation(
            session,
            session_key=session_key,
            bill_id=bill_id,
        )
        if action not in consensus_web_capabilities(
            mode="live",
            session=session,
            principal=principal,
        ):
            raise ConsensusWebCommandError(
                "discussion_forbidden",
                "Запрос дискуссии сейчас недоступен.",
                status=403,
            )
        participant = session.participants[int(principal.user_id)]
        await request_discussion(
            bot,
            guild,
            session,
            participant,
            expected_bill_id=consensus_bill_id(session),
        )
        return "Запрос дискуссии принят. Ведущий выбирает её формат."
    if action == "post_discussion_message":
        _validate_ballot_generation(
            session,
            session_key=session_key,
            bill_id=bill_id,
        )
        if action not in consensus_web_capabilities(
            mode="live",
            session=session,
            principal=principal,
        ):
            raise ConsensusWebCommandError(
                "discussion_forbidden",
                "Вы не включены в текущую дискуссию.",
                status=403,
            )
        content = str(payload.get("content") or "").strip()
        if not content or len(content) > 1800:
            raise ConsensusWebCommandError(
                "discussion_message_invalid",
                "Напишите сообщение длиной от 1 до 1800 символов.",
            )
        participant = session.participants[int(principal.user_id)]
        avatar = getattr(getattr(principal.member, "display_avatar", None), "url", None)
        await publish_discussion_message(
            bot,
            session,
            participant,
            content,
            avatar_url=str(avatar or "") or None,
            expected_bill_id=consensus_bill_id(session),
        )
        return "Ваше выступление опубликовано в канале дискуссии."
    if not _is_live_leader(session, principal):
        raise ConsensusWebCommandError(
            "forbidden",
            "Управлять заседанием может только его текущий ведущий.",
            status=403,
        )
    _validate_generation(
        session,
        session_key=session_key,
        revision=revision,
        bill_id=bill_id,
    )
    capabilities = consensus_web_capabilities(
        mode="live",
        session=session,
        principal=principal,
    )
    if action not in capabilities:
        raise ConsensusWebCommandError(
            "action_unavailable",
            "Это действие недоступно на текущем этапе.",
            status=409,
        )
    actor = ConsensusActor(principal.user_id, principal.display_name)
    expected_stage = str(session.stage)
    expected_bill_id = consensus_bill_id(session)
    expected_result_bill_id = consensus_result_bill_id(session)
    channel = guild.get_channel(int(session.channel_id))

    if action == "start_vote":
        if not session.quorum_ready():
            raise ConsensusWebCommandError(
                "quorum_missing",
                "Подтверждённый кворум ещё не набран.",
                status=409,
            )
        voice_ok, reason = session_voice_quorum_ready(guild, session)
        if not voice_ok:
            raise ConsensusWebCommandError(
                "voice_quorum_missing",
                reason,
                status=409,
            )
        unconfirmed = [
            participant.display_name
            for participant in session.participants.values()
            if not participant.confirmed
        ]
        if unconfirmed and payload.get("confirm_current_roster") is not True:
            raise ConsensusWebCommandError(
                "roster_confirmation_required",
                "Не все приглашённые подтвердились. Подтвердите начало текущим составом.",
                status=409,
                details={"unconfirmed": unconfirmed},
            )
        await begin_next_bill_vote(
            bot,
            guild,
            session,
            channel,
            expected_stage="registration",
            expected_revision=revision,
        )
        return "Первый законопроект представлен. Воут пока закрыт."

    if action == "open_vote":
        await open_current_bill_vote(
            bot,
            guild,
            session,
            expected_bill_id=expected_bill_id,
            expected_revision=revision,
            actor=actor,
        )
        return "Воут открыт; кнопки голосования отправлены участникам."

    if action == "resend_invitations":
        voice_roster, voice_error = voice_participants(guild)
        if voice_error and not voice_roster:
            raise ConsensusWebCommandError(
                "voice_roster_unavailable",
                voice_error,
                status=409,
            )
        added_names: list[str] = []
        async with session_lock(session.guild_id):
            if (
                session.stage != "registration"
                or int(session.revision) != int(revision)
            ):
                raise ConsensusWebCommandError(
                    "stale_revision",
                    "Регистрация уже изменилась.",
                    status=409,
                )
            for participant in voice_roster:
                if int(participant.user_id) not in session.participants:
                    session.participants[int(participant.user_id)] = participant
                    added_names.append(str(participant.display_name))
            deliveries = build_control_dm_deliveries(
                session,
                phase="registration",
                generation=f"web-manual-{session.revision + 1}",
            )
            await run_blocking_cancellation_safe(
                coordinator.save_with_deliveries,
                session,
                "registration_invitations_retried",
                actor=actor,
                deliveries=deliveries,
            )
        wake_delivery_worker()
        await update_host_registration_message(bot, guild, session)
        if added_names:
            return (
                "Состав обновлён, новые участники приглашены: "
                + ", ".join(added_names[:12])
                + ("…" if len(added_names) > 12 else "")
                + (f" Предупреждение: {voice_error}" if voice_error else "")
            )
        return "Приглашения повторно поставлены в очередь текущему составу."

    if action == "cancel_session":
        _require_confirmation(payload, "отменить заседание")
        await finish_session(
            bot,
            guild,
            session,
            channel,
            expected_stage=expected_stage,
            expected_bill_id=expected_bill_id,
            expected_result_bill_id=expected_result_bill_id,
            actor=actor,
            cancelled=True,
            reason="Заседание отменено ведущим через веб-пульт.",
            expected_revision=revision,
        )
        return "Заседание отменено."

    if action == "leader_vote":
        vote = str(payload.get("vote") or "").lower()
        if vote not in {"yes", "no", "abstain"}:
            raise ConsensusWebCommandError(
                "invalid_vote",
                "Выберите «за», «против» или «воздержаться».",
            )
        async with session_lock(session.guild_id):
            if (
                session.stage != "voting"
                or int(session.revision) != int(revision)
                or consensus_bill_id(session) != expected_bill_id
            ):
                raise ConsensusWebCommandError(
                    "stale_revision",
                    "Голосование уже изменилось.",
                    status=409,
                )
            should_finalize = await run_blocking_cancellation_safe(
                coordinator.cast_vote,
                session,
                session.leader_id,
                vote,
                actor=actor,
            )
        if should_finalize:
            await finalize_current_vote(
                bot,
                guild,
                session,
                forced=False,
                expected_bill_id=expected_bill_id,
            )
        else:
            await update_host_vote_message(bot, guild, session)
        return "Голос ведущего принят."

    if action == "set_timer":
        seconds = int(payload.get("seconds") or 0)
        if seconds < 15 or seconds > 7200:
            raise ConsensusWebCommandError(
                "invalid_timer",
                "Таймер должен быть от 15 секунд до 2 часов.",
            )
        timer_mode = str(payload.get("mode") or "extend").strip().lower()
        if timer_mode not in {"extend", "replace"}:
            raise ConsensusWebCommandError(
                "invalid_timer_mode",
                "Выберите продление текущего таймера или установку нового.",
            )
        replace = timer_mode == "replace"
        was_extension = bool(
            session.timer_deadline
            and session.timer_deadline > datetime.now(timezone.utc)
            and not replace
        )
        await set_vote_timer(
            bot,
            guild,
            session,
            seconds,
            expected_bill_id=expected_bill_id,
            expected_revision=revision,
            replace=replace,
        )
        return (
            f"К текущему таймеру добавлено {seconds} секунд."
            if was_extension
            else f"Таймер установлен на {seconds} секунд."
        )

    if action == "update_session_settings":
        schedule = await asyncio.to_thread(
            schedule_storage.get_consensus_schedule_for_session,
            int(guild.id),
            str(session.session_key),
        )
        if schedule is None:
            raise ConsensusWebCommandError(
                "schedule_missing",
                "У этого заседания нет связанного паспорта планирования.",
                status=409,
            )
        try:
            updated = await asyncio.to_thread(
                schedule_storage.update_started_consensus_schedule,
                guild_id=int(guild.id),
                session_key=str(session.session_key),
                title=str(payload.get("title") or ""),
                duration_minutes=int(payload.get("duration_minutes") or 0),
                expected_revision=int(payload.get("schedule_revision") or 0),
            )
        except ValueError as exc:
            code = str(exc).split(":", 1)[0]
            messages = {
                "consensus_schedule_title_invalid": "Название должно содержать от 3 до 100 символов.",
                "consensus_schedule_duration_invalid": "Плановая длительность должна быть от 15 до 480 минут.",
                "consensus_schedule_conflict": "Паспорт уже изменён в другой вкладке. Пульт обновляется.",
            }
            raise ConsensusWebCommandError(
                code,
                messages.get(code, "Не удалось изменить паспорт заседания."),
                status=409 if code == "consensus_schedule_conflict" else 400,
            ) from exc
        return (
            f"Паспорт обновлён: «{updated['title']}», "
            f"плановая длительность — {int(updated['duration_minutes'])} мин."
        )

    if action == "finalize_vote":
        _require_confirmation(payload, "досрочно завершить голосование")
        await finalize_current_vote(
            bot,
            guild,
            session,
            forced=True,
            expected_bill_id=expected_bill_id,
            expected_revision=revision,
        )
        return "Результат голосования зафиксирован."

    if action == "retry_finalization":
        await retry_pending_finalization_once(bot, guild, session)
        return "Повторная фиксация выполнена."

    if action == "choose_discussion":
        discussion_type = str(payload.get("discussion_type") or "").strip()
        if discussion_type not in {
            "Правовая",
            "Фактическая",
            "Процедурная",
            "Иная",
        }:
            raise ConsensusWebCommandError(
                "invalid_discussion_type",
                "Выберите допустимый тип дискуссии.",
            )
        await start_discussion_channel(
            bot,
            guild,
            session,
            discussion_type,
            expected_bill_id=expected_bill_id,
            expected_revision=revision,
        )
        return f"Открыта дискуссия: {discussion_type.lower()}."

    if action == "end_discussion":
        await end_discussion(
            bot,
            guild,
            session,
            actor=actor,
            expected_stage=expected_stage,
            expected_bill_id=expected_bill_id,
            expected_revision=revision,
        )
        return "Дискуссия завершена; голосование продолжено."

    if action == "pause":
        reason = " ".join(
            str(payload.get("reason") or "Консенсус приостановлен ведущим через веб-пульт.").split()
        )[:300]
        await pause_session(
            bot,
            guild,
            session,
            reason,
            automatic=False,
            expected_stage=expected_stage,
            expected_bill_id=expected_bill_id,
            expected_revision=revision,
        )
        return "Заседание поставлено на паузу."

    if action == "resume":
        await resume_session(
            bot,
            guild,
            session,
            expected_stage=expected_stage,
            expected_bill_id=expected_bill_id,
            expected_revision=revision,
        )
        if session.stage == "paused":
            raise ConsensusWebCommandError(
                "voice_quorum_missing",
                "Продолжить нельзя: голосовой кворум ещё не восстановлен.",
                status=409,
            )
        return "Заседание продолжено."

    if action == "veto":
        _require_confirmation(payload, "применить вето")
        await apply_veto_for_actor(
            bot,
            guild,
            session,
            actor,
            expected_bill_id=expected_bill_id,
            expected_revision=revision,
        )
        return "Право вето применено."

    if action == "next_bill":
        await begin_next_bill_vote(
            bot,
            guild,
            session,
            channel,
            expected_stage="after_result",
            expected_result_bill_id=expected_result_bill_id,
            expected_revision=revision,
        )
        return "Следующий законопроект представлен; воут пока закрыт."

    if action == "finish_session":
        _require_confirmation(payload, "завершить заседание")
        await finish_session(
            bot,
            guild,
            session,
            channel,
            expected_stage=expected_stage,
            expected_bill_id=expected_bill_id,
            expected_result_bill_id=expected_result_bill_id,
            actor=actor,
            expected_revision=revision,
        )
        return "Заседание завершено; итоговый протокол поставлен в очередь."

    raise ConsensusWebCommandError("unknown_action", "Неизвестная команда.")


async def _execute_simulation(
    principal: ConsensusWebPrincipal,
    *,
    action: str,
    session_key: str,
    revision: int,
    bill_id: int | None,
    payload: dict[str, Any],
) -> str:
    simulation = get_consensus_simulation(principal.guild_id)
    if simulation is None:
        raise ConsensusWebCommandError(
            "simulation_missing",
            "Симуляция не запущена.",
            status=409,
        )
    session = simulation.session
    _validate_generation(
        session,
        session_key=session_key,
        revision=revision,
        bill_id=bill_id,
    )
    if action not in consensus_web_capabilities(
        mode="simulation",
        session=session,
        principal=principal,
    ):
        raise ConsensusWebCommandError(
            "action_unavailable",
            "Это действие недоступно на текущем этапе симуляции.",
            status=409,
        )
    if action == "confirm_next":
        simulation.confirm_next()
    elif action == "confirm_all":
        simulation.confirm_all()
    elif action == "resend_invitations":
        simulation.resend_invitations()
    elif action == "start_vote":
        simulation.begin_voting()
    elif action == "open_vote":
        simulation.open_voting()
    elif action == "leader_vote":
        if simulation.leader_id != principal.user_id:
            raise ConsensusWebCommandError(
                "forbidden",
                "Голос ведущего доступен только ведущему симуляции.",
                status=403,
            )
        simulation.cast_leader_vote(str(payload.get("vote") or ""))
    elif action == "participant_vote":
        simulation.cast_participant_vote(
            principal.user_id,
            str(payload.get("vote") or ""),
        )
    elif action == "fake_vote":
        simulation.cast_next_fake_vote()
    elif action == "fake_scenario":
        simulation.apply_fake_scenario(str(payload.get("scenario") or ""))
    elif action == "request_discussion":
        simulation.request_discussion()
    elif action == "choose_discussion":
        simulation.choose_discussion(str(payload.get("discussion_type") or ""))
    elif action == "end_discussion":
        simulation.end_discussion()
    elif action == "pause":
        simulation.pause()
    elif action == "resume":
        simulation.resume()
    elif action == "set_timer":
        simulation.set_timer(int(payload.get("seconds") or 300))
    elif action in {"finalize_vote", "retry_finalization"}:
        simulation.finalize(forced=True)
    elif action == "veto":
        _require_confirmation(payload, "применить учебное вето")
        simulation.veto()
    elif action == "oral_result":
        simulation.oral_result(str(payload.get("status") or ""))
    elif action == "next_bill":
        simulation.next_bill()
    elif action in {"finish_session", "cancel_session"}:
        _require_confirmation(payload, "завершить симуляцию")
        simulation.finish()
    else:
        raise ConsensusWebCommandError("unknown_action", "Неизвестная команда.")
    await refresh_consensus_simulation_projection(simulation)
    wake_operations_worker()
    return "Команда симулятора выполнена."


__all__ = [
    "ConsensusWebCommandError",
    "consensus_web_capabilities",
    "execute_consensus_web_command",
]
