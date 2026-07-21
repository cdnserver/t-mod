"""Read-only Discord/runtime diagnostics for a live consensus."""

from __future__ import annotations

import asyncio

import discord

from persistence import outbox_repository as _outbox_storage
from modules.consensus_core import LiveConsensusSession
from modules.consensus_recovery_plan import ConsensusOperationalState
from modules.tvrs_config import TVRS_CONSENSUS_VOICE_CHANNEL_ID
from modules.tvrs_discussion import session_voice_quorum_ready
from modules.tvrs_presentation import consensus_bill_id


async def collect_consensus_operational_state(
    guild: discord.Guild,
    session: LiveConsensusSession,
) -> ConsensusOperationalState:
    """Collect current Discord and worker facts without mutating the session."""

    get_channel = getattr(guild, "get_channel", None)
    get_member = getattr(guild, "get_member", None)
    public_channel = get_channel(int(session.channel_id)) if callable(get_channel) else None
    voice_channel = (
        get_channel(int(TVRS_CONSENSUS_VOICE_CHANNEL_ID)) if callable(get_channel) else None
    )
    voice_available = isinstance(voice_channel, discord.VoiceChannel)
    voice_ids = (
        {int(member.id) for member in voice_channel.members if not member.bot}
        if voice_available
        else set()
    )
    leader = get_member(int(session.leader_id)) if callable(get_member) else None
    missing_members = (
        tuple(
            sorted(
                int(user_id)
                for user_id in session.participants
                if get_member(int(user_id)) is None
            )
        )
        if callable(get_member)
        else ()
    )
    quorum_ready, quorum_reason = session_voice_quorum_ready(guild, session)
    discussion_available = None
    if session.stage == "discussion":
        discussion_available = bool(
            session.discussion_channel_id
            and callable(get_channel)
            and get_channel(int(session.discussion_channel_id)) is not None
        )
    timer_running = None
    if session.stage == "voting" and session.timer_deadline is not None:
        timer_running = bool(session.timer_task is not None and not session.timer_task.done())
    bill_id = consensus_bill_id(session)
    missing_controls: list[int] = []
    if session.stage == "registration":
        missing_controls = [
            int(item.user_id)
            for item in session.participants.values()
            if item.user_id != session.leader_id
            and not item.confirmed
            and (not item.dm_message_id or item.dm_failed)
        ]
    elif session.stage in {"voting", "paused", "discussion_type", "discussion"}:
        missing_controls = [
            int(item.user_id)
            for item in session.confirmed_participants()
            if item.user_id != session.leader_id
            and (
                not item.vote_message_id
                or int(item.vote_bill_id or 0) != bill_id
                or item.dm_failed
            )
        ]
    delivery = await asyncio.to_thread(
        _outbox_storage.delivery_outbox_consensus_status,
        session.session_key,
        guild_id=session.guild_id,
    )
    return ConsensusOperationalState(
        leader_present=(leader is not None) if callable(get_member) else None,
        leader_in_voice=(int(session.leader_id) in voice_ids) if voice_available else False,
        voice_channel_available=voice_available,
        quorum_ready=quorum_ready,
        quorum_reason=quorum_reason,
        discussion_channel_available=discussion_available,
        timer_task_running=timer_running,
        public_channel_available=(public_channel is not None) if callable(get_channel) else None,
        host_control_bound=bool(session.host_message_id or session.host_message_obj),
        missing_member_ids=missing_members,
        missing_control_user_ids=tuple(sorted(missing_controls)),
        delivery_counts=tuple(
            sorted(
                (str(status), int(count))
                for status, count in dict(delivery.get("counts") or {}).items()
            )
        ),
        dead_delivery_errors=tuple(
            str(error)[:200] for error in tuple(delivery.get("dead_errors") or ())[:3]
        ),
    )


__all__ = ["collect_consensus_operational_state"]
