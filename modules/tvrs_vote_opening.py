"""Explicit transition from bill presentation to live voting."""

from __future__ import annotations

import discord
from discord.ext import commands

from modules.async_safety import run_blocking_cancellation_safe
from modules.consensus_core import ConsensusStateError, LiveConsensusSession
from modules.consensus_runtime import (
    coordinator as _consensus,
    registry as _consensus_registry,
    session_lock as consensus_session_lock,
)
from modules.consensus_service import ConsensusActor
from modules.delivery_runtime import wake_delivery_worker
from modules.operations_runtime import wake_operations_worker
from modules.tvrs_delivery import (
    build_control_dm_deliveries,
    build_control_notice_deliveries,
)
from modules.tvrs_discussion import session_voice_quorum_ready
from modules.tvrs_presentation import consensus_generation_matches


async def open_current_bill_vote(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    *,
    expected_bill_id: int | None = None,
    expected_revision: int | None = None,
    actor: ConsensusActor | None = None,
) -> None:
    """Open voting controls for a bill that is already visible to senators."""

    bill_id = int((session.current_bill or {}).get("id") or 0)
    if session.finished or session.stage != "presentation" or bill_id <= 0:
        raise ConsensusStateError("Законопроект сейчас нельзя поставить на воут.")
    if expected_bill_id is not None and bill_id != int(expected_bill_id):
        raise ConsensusStateError("Пульт относится к другому законопроекту.")
    voice_ok, voice_reason = session_voice_quorum_ready(guild, session)
    if not voice_ok:
        raise ConsensusStateError(voice_reason)
    selected_actor = actor or ConsensusActor(
        session.leader_id,
        session.leader_display,
    )
    async with consensus_session_lock(session.guild_id):
        if expected_revision is not None and int(session.revision) != int(
            expected_revision
        ):
            raise ConsensusStateError(
                "Состояние заседания уже изменилось. Обновите пульт."
            )
        if (
            session.finished
            or session.stage != "presentation"
            or int((session.current_bill or {}).get("id") or 0) != bill_id
        ):
            raise ConsensusStateError("Законопроект уже поставлен на воут.")
        deliveries = build_control_dm_deliveries(
            session,
            phase="voting",
            bill_id=bill_id,
        )
        deliveries.extend(build_control_notice_deliveries(session, bill_id=bill_id))
        await run_blocking_cancellation_safe(
            _consensus.open_voting,
            session,
            actor=selected_actor,
            deliveries=deliveries,
        )
    wake_delivery_worker()
    wake_operations_worker()
    from modules.tvrs_control import update_host_vote_message

    await update_host_vote_message(bot, guild, session)


async def open_vote_from_interaction(
    interaction: discord.Interaction,
    session_key: str,
    bill_id: int,
) -> None:
    """Apply the host button with the same generation fences as every view."""

    if interaction.guild is None:
        await interaction.response.send_message(
            "Команда работает только на сервере Discord.",
            ephemeral=True,
        )
        return
    session = _consensus_registry.find(session_key)
    if session is None or not consensus_generation_matches(
        session,
        stage="presentation",
        bill_id=bill_id,
    ):
        await interaction.response.send_message(
            "Этот законопроект уже поставлен на воут.",
            ephemeral=True,
        )
        return
    await interaction.response.defer()
    try:
        await open_current_bill_vote(
            interaction.client,
            interaction.guild,
            session,
            expected_bill_id=bill_id,
            expected_revision=int(session.revision),
            actor=ConsensusActor(
                interaction.user.id,
                getattr(interaction.user, "display_name", str(interaction.user)),
            ),
        )
    except ConsensusStateError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)


__all__ = ["open_current_bill_vote", "open_vote_from_interaction"]
