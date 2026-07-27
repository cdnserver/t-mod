from __future__ import annotations

import discord
from discord.ext import commands

from persistence import tvrs_repository as storage
from modules.consensus_core import (
    ConsensusStateError,
    LiveConsensusSession,
    LiveResult,
)
from modules.consensus_runtime import (
    coordinator as _consensus,
    registry as _consensus_registry,
    session_lock as consensus_session_lock,
)
from modules.consensus_service import ConsensusActor
from modules.async_safety import run_blocking_cancellation_safe
from modules.delivery_runtime import wake_delivery_worker
from modules.operations_runtime import wake_operations_worker
from modules.tvrs_config import (
    TVRS_PERMANENT_CHAIR_ID,
)
from modules.tvrs_embeds import (
    build_final_summary_embed,
    build_result_embed,
)
from modules.tvrs_formatting import (
    result_status_text,
)
from modules.tvrs_delivery import (
    build_retry_bill_delivery,
    build_result_deliveries,
    build_session_summary_deliveries,
)

from modules.tvrs_presentation import (
    calculate_consensus,
    consensus_bill_id,
    consensus_generation_matches,
    edit_session_host_message,
)
from modules.tvrs_consensus_views import TVRSAfterResultView
from modules.tvrs_discussion import cancel_vote_timer
from modules.tvrs_control import (
    clear_finalization_retry,
    schedule_finalization_retry,
    update_public_consensus_card,
)

async def ensure_sticky_message(*args, **kwargs):
    from modules.tvrs_recovery import ensure_sticky_message as _implementation
    return await _implementation(*args, **kwargs)

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
                raise ConsensusStateError(
                    "Текущий законопроект уже закрыт; голосование не фиксировалось."
                )
            await run_blocking_cancellation_safe(
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
            source_channel_id=(
                int(bill["channel_id"]) if bill.get("channel_id") else None
            ),
            source_message_id=(
                int(bill["message_id"]) if bill.get("message_id") else None
            ),
            decision_category=str(
                calc.get("decision_category")
                or bill.get("decision_category")
                or "ordinary"
            ),
            required_percent=float(
                calc.get("required_percent")
                or calc.get("acceptance_percent")
                or session.rules.acceptance_percent
            ),
            opposed_percent=float(calc.get("opposed_percent") or 0.0),
            block_votes={
                str(key): str(value)
                for key, value in dict(calc.get("block_votes") or {}).items()
            },
        )
        deliveries = build_result_deliveries(
            session,
            result,
            participant_content="Голосование по текущему законопроекту завершено.",
        )
        try:
            await run_blocking_cancellation_safe(
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
    await update_public_consensus_card(bot, guild, session)
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
                raise ConsensusStateError("Текущий законопроект уже закрыт; вето не применено.")
            await run_blocking_cancellation_safe(
                _consensus.claim_finalization,
                session,
                kind="veto",
                actor=actor,
                veto_authorized=actor.user_id == TVRS_PERMANENT_CHAIR_ID,
            )
            await cancel_vote_timer(session)
        elif session.stage != "finalizing" or (session.pending_action or {}).get("kind") != "veto":
            raise ConsensusStateError(
                "Голосование уже перешло к другому решению; вето не применено."
            )
        if session.current_bill is None:
            raise ConsensusStateError("Текущий законопроект уже закрыт; вето не применено.")
        bill = dict(session.current_bill)
        try:
            retry = await run_blocking_cancellation_safe(
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
            source_channel_id=(
                int(bill["channel_id"]) if bill.get("channel_id") else None
            ),
            source_message_id=(
                int(bill["message_id"]) if bill.get("message_id") else None
            ),
            decision_category=str(
                bill.get("decision_category") or "ordinary"
            ),
            required_percent=float(
                {
                    "ordinary": session.rules.acceptance_percent,
                    "heavy": session.rules.heavy_acceptance_percent,
                    "unanimous": session.rules.unanimous_acceptance_percent,
                }.get(
                    str(bill.get("decision_category") or "ordinary"),
                    session.rules.acceptance_percent,
                )
            ),
            veto_by_id=actor.user_id,
            retry_bill_number=(int(retry["bill_number"]) if retry else None),
            resolution_method="veto",
            resolved_by_id=actor.user_id,
            resolved_by_display=actor.display_name,
        )
        deliveries = build_result_deliveries(
            session,
            result,
            participant_content="Постоянный председатель применил право вето. Голосование отменено.",
        )
        if retry is not None:
            deliveries.append(build_retry_bill_delivery(session, result, retry))
        try:
            await run_blocking_cancellation_safe(
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
    await update_public_consensus_card(bot, guild, session)
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


async def record_oral_result(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    actor: ConsensusActor,
    *,
    status: str,
    note: str,
    expected_bill_id: int | None = None,
) -> LiveResult:
    """Record a chair-authorized oral decision without inventing vote totals."""

    clean_status = str(status).strip().lower()
    if clean_status not in {"accepted", "rejected"}:
        raise ConsensusStateError("Устный итог должен быть принят или отклонён.")
    clean_note = " ".join(str(note).split()).strip()[:1000]
    if len(clean_note) < 3:
        raise ConsensusStateError("Укажите краткое основание устного решения.")
    if actor.user_id is None:
        raise ConsensusStateError("Не удалось определить председателя, фиксирующего решение.")

    async with consensus_session_lock(session.guild_id):
        if expected_bill_id is not None and consensus_bill_id(session) != int(expected_bill_id):
            raise ConsensusStateError("Устное решение относится к уже сменившемуся проекту.")
        if session.stage != "finalizing":
            await run_blocking_cancellation_safe(
                _consensus.claim_finalization,
                session,
                kind="oral",
                actor=actor,
                oral_authorized=True,
                action_details={"oral_status": clean_status, "oral_note": clean_note},
            )
            await cancel_vote_timer(session)
        elif (session.pending_action or {}).get("kind") != "oral":
            raise ConsensusStateError("Проект уже фиксируется другим способом.")

        pending = dict(session.pending_action or {})
        clean_status = str(pending.get("oral_status") or clean_status)
        clean_note = str(pending.get("oral_note") or clean_note)
        if session.current_bill is None:
            raise ConsensusStateError("Текущий законопроект уже закрыт.")
        bill = dict(session.current_bill)
        result = LiveResult(
            bill_id=int(bill["id"]),
            bill_number=int(bill["bill_number"]),
            title=str(bill.get("title") or ""),
            status=clean_status,
            internal_percent=0.0,
            overall_percent=0.0,
            internal_active=False,
            votes=dict(session.votes),
            source_channel_id=(
                int(bill["channel_id"]) if bill.get("channel_id") else None
            ),
            source_message_id=(
                int(bill["message_id"]) if bill.get("message_id") else None
            ),
            decision_category=str(
                bill.get("decision_category") or "ordinary"
            ),
            required_percent=float(
                {
                    "ordinary": session.rules.acceptance_percent,
                    "heavy": session.rules.heavy_acceptance_percent,
                    "unanimous": session.rules.unanimous_acceptance_percent,
                }.get(
                    str(bill.get("decision_category") or "ordinary"),
                    session.rules.acceptance_percent,
                )
            ),
            resolution_method="oral",
            resolution_note=clean_note,
            resolved_by_id=int(pending.get("actor_id") or actor.user_id),
            resolved_by_display=str(pending.get("actor_display") or actor.display_name or actor.user_id),
        )
        deliveries = build_result_deliveries(
            session,
            result,
            participant_content="Устное решение по текущему законопроекту внесено в систему.",
        )
        try:
            await run_blocking_cancellation_safe(
                _consensus.complete_result_atomically,
                session,
                result,
                bill_status=clean_status,
                result_summary=f"{result_status_text(clean_status)} • устное решение: {clean_note}",
                event_type="oral_result_recorded",
                actor=actor,
                details={"resolution_method": "oral", "resolution_note": clean_note},
                deliveries=deliveries,
            )
        except Exception:
            schedule_finalization_retry(bot, guild, session)
            raise

    clear_finalization_retry(session.session_key)
    wake_delivery_worker()
    wake_operations_worker()
    await update_public_consensus_card(bot, guild, session)
    async with consensus_session_lock(session.guild_id):
        if _consensus_registry.find(session.session_key) is session and session.stage == "after_result":
            await edit_session_host_message(
                session,
                embed=build_result_embed(result, session),
                view=TVRSAfterResultView(session.session_key),
            )
    return result


async def finish_session(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    channel,
    *,
    expected_stage: str | None = None,
    expected_bill_id: int | None = None,
    expected_result_bill_id: int | None = None,
    actor: ConsensusActor | None = None,
    cancelled: bool = False,
    reason: str | None = None,
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
        effective_actor = actor or ConsensusActor(session.leader_id, session.leader_display)
        deliveries = [] if cancelled else build_session_summary_deliveries(session)
        finish_kwargs = {
            "actor": effective_actor,
            "deliveries": deliveries,
        }
        if cancelled:
            finish_kwargs["cancelled"] = True
        if reason is not None:
            finish_kwargs["reason"] = reason
        await run_blocking_cancellation_safe(
            _consensus.finish_atomically,
            session,
            **finish_kwargs,
        )
    wake_delivery_worker()
    await cancel_vote_timer(session)
    embed = build_final_summary_embed(session)
    async with consensus_session_lock(session.guild_id):
        if _consensus_registry.find(session.session_key) is session and session.finished:
            await edit_session_host_message(session, embed=embed, view=None)
    await update_public_consensus_card(bot, guild, session, terminal=True)
    await ensure_sticky_message(bot, guild, force_repost=True)
    _consensus_registry.remove(guild.id, session_key=session.session_key)
    wake_operations_worker()

__all__ = ['finalize_current_vote', 'apply_veto', 'apply_veto_for_actor', 'record_oral_result', 'finish_session']
