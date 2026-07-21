from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord.ext import commands

from persistence import outbox_repository as _outbox_storage
from persistence import tvrs_repository as storage
from modules.consensus_core import (
    LiveConsensusSession,
)
from modules.consensus_runtime import (
    coordinator as _consensus,
    registry as _consensus_registry,
    repository as _consensus_repository,
    session_lock as consensus_session_lock,
)
from modules.consensus_service import ConsensusActor
from modules.async_safety import consensus_projection_lock, run_blocking_cancellation_safe
from modules.delivery_runtime import wake_delivery_worker
from modules.delivery_outbox import (
    DeliveryDeferred,
    DeliveryPermanentFailure,
    DeliveryReceipt,
    OutboxMessage,
)
from modules.discord_delivery import (
    DeliveryDestinationUnavailable as _DeliveryDestinationUnavailable,
    raise_classified_discord_error as _raise_classified_discord_error,
    resolve_delivery_member as _resolve_delivery_member,
)
from modules.operations_runtime import wake_operations_worker
from modules.tvrs_config import (
    TVRS_EMBED_COLOR,
    TVRS_PERMANENT_CHAIR_ID,
)
from modules.tvrs_formatting import (
    format_bill_number,
    now_local,
    role_label,
)
from modules.tvrs_embeds import build_discussion_embed
from modules.tvrs_delivery import (
    TVRS_CONTROL_DM_TOPIC,
    build_control_dm_deliveries,
    build_control_notice_deliveries,
    build_phase_announcement_delivery,
    delivery_marker,
    find_delivery_marker,
)
from modules.technical_log import log_technical_event

from modules.tvrs_presentation import (
    build_dm_vote_embed,
    build_live_vote_embed,
    build_registration_embed,
    consensus_bill_id,
    consensus_generation_matches,
    consensus_result_bill_id,
    edit_session_host_message,
    queue_lines,
)
from modules.tvrs_consensus_views import (
    TVRSConfirmView, TVRSHostVoteView, TVRSPermanentVoteView,
    TVRSRegistrationView, TVRSVoteView,
)
from modules.tvrs_discussion import cancel_vote_timer, session_voice_quorum_ready, update_all_vote_dms
from modules.tvrs_control_cleanup import (
    _control_cleanup_tasks,
    _prune_duplicate_control_panels,
    _schedule_control_cleanup,
)

_finalization_retry_tasks: dict[str, asyncio.Task] = {}

async def apply_veto_for_actor(*args, **kwargs):
    from modules.tvrs_decision import apply_veto_for_actor as _implementation
    return await _implementation(*args, **kwargs)

async def finalize_current_vote(*args, **kwargs):
    from modules.tvrs_decision import finalize_current_vote as _implementation
    return await _implementation(*args, **kwargs)

async def finish_session(*args, **kwargs):
    from modules.tvrs_decision import finish_session as _implementation
    return await _implementation(*args, **kwargs)


async def update_public_consensus_card(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    *,
    terminal: bool = False,
) -> None:
    """Best-effort projection; business state is already durable in SQLite."""

    from modules.tvrs_consensus_portal import ensure_public_consensus_card

    try:
        await ensure_public_consensus_card(
            bot,
            guild,
            session,
            terminal=terminal,
        )
    except Exception:
        traceback.print_exc()

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


async def _active_delivery_session(
    guild_id: int,
    session_key: str,
) -> LiveConsensusSession | None:
    session = _consensus_registry.find(session_key)
    if session is not None:
        return session
    snapshots = await asyncio.to_thread(_consensus_repository.active_snapshots, guild_id)
    if any(str(item.get("session_key") or "") == session_key for item in snapshots):
        raise RuntimeError("tvrs_control_session_not_restored")
    return None


async def _record_dm_failure(
    *,
    guild_id: int,
    session_key: str,
    user_id: int,
    phase: str,
    bill_id: int,
    error: BaseException,
) -> None:
    async with consensus_session_lock(guild_id):
        current = _consensus_registry.find(session_key)
        if current is None or not _control_delivery_matches(
            current,
            phase=phase,
            bill_id=bill_id,
            user_id=user_id,
        ):
            return

        def persist_failure() -> None:
            with _consensus.mutation(current):
                current.participants[user_id].dm_failed = True
                _consensus.save(
                    current,
                    f"{phase}_control_unavailable",
                    details={
                        "user_id": user_id,
                        "bill_id": bill_id or None,
                        "error": f"{type(error).__name__}: {str(error)[:300]}",
                        "fallback": "server_portal",
                    },
                )

        await run_blocking_cancellation_safe(persist_failure)


async def deliver_consensus_control_dm(
    message: OutboxMessage,
    bot: commands.Bot | discord.Client,
) -> DeliveryReceipt:
    """Serialize all writes to the participant's shared consensus message."""

    payload = message.payload
    try:
        session_key = str(payload.get("session_key") or "")
        user_id = int(payload.get("user_id") or 0)
    except (TypeError, ValueError):
        raise DeliveryPermanentFailure("tvrs_control_payload_invalid")
    if not session_key or user_id <= 0:
        return await _deliver_consensus_control_dm(message, bot)
    async with consensus_projection_lock(session_key, user_id):
        return await _deliver_consensus_control_dm(message, bot)


async def _deliver_consensus_control_dm(
    message: OutboxMessage,
    bot: commands.Bot | discord.Client,
) -> DeliveryReceipt:
    """Deliver one canonical control panel and persist its Discord receipt."""

    payload = message.payload
    try:
        payload_version = int(payload.get("payload_version") or 0)
        guild_id = int(payload.get("guild_id") or 0)
        session_key = str(payload.get("session_key") or "")
        phase = str(payload.get("phase") or "")
        bill_id = int(payload.get("bill_id") or 0)
        user_id = int(payload.get("user_id") or 0)
    except (TypeError, ValueError) as exc:
        raise DeliveryPermanentFailure("tvrs_control_payload_invalid") from exc
    if payload_version != 1:
        raise DeliveryPermanentFailure("tvrs_control_payload_version_unsupported")
    if guild_id <= 0 or user_id <= 0 or phase not in {"registration", "voting"}:
        raise DeliveryPermanentFailure("tvrs_control_payload_invalid")

    session = await _active_delivery_session(guild_id, session_key)
    if session is None or not _control_delivery_matches(
        session,
        phase=phase,
        bill_id=bill_id,
        user_id=user_id,
    ):
        return DeliveryReceipt()
    if not await asyncio.to_thread(
        _outbox_storage.delivery_outbox_is_current_supersession,
        message.id,
        topic=message.topic,
        supersede_key=message.supersede_key,
    ):
        return DeliveryReceipt()

    guild = bot.get_guild(guild_id)
    if guild is None:
        raise RuntimeError(f"tvrs_delivery_guild_unavailable:{guild_id}")
    participant = session.participants[user_id]
    try:
        member = await _resolve_delivery_member(guild, user_id)
        if phase == "registration":
            queue = await asyncio.to_thread(queue_lines, guild_id, 10)
            embed = discord.Embed(
                title="Пленарный консенсус Товарищества",
                description=(
                    f"Ведущий: <@{session.leader_id}>\n"
                    f"Роль: **{role_label(participant)}**"
                ),
                color=TVRS_EMBED_COLOR,
                timestamp=now_local(),
            )
            embed.add_field(name="Очередь законопроектов", value=queue, inline=False)
            view: discord.ui.View = TVRSConfirmView(session_key, user_id)
            content: str | None = "Подтвердите участие в консенсусе."
            existing_message_id = int(participant.dm_message_id or 0)
        else:
            embed = await asyncio.to_thread(build_dm_vote_embed, session, participant)
            view = (
                TVRSPermanentVoteView(session_key, user_id, bill_id=bill_id)
                if participant.permanent
                else TVRSVoteView(session_key, user_id, bill_id=bill_id)
            )
            content = str(payload.get("content") or "").strip()[:1000] or None
            existing_message_id = int(
                participant.vote_message_id or participant.dm_message_id or 0
            )

        marker = delivery_marker(message.dedupe_key)
        embed.set_footer(text=marker)
        dm_channel = member.dm_channel or await member.create_dm()
        sent = None
        needs_cleanup = False
        if existing_message_id:
            try:
                sent = await dm_channel.fetch_message(existing_message_id)
            except discord.NotFound:
                sent = None
                needs_cleanup = True
        # A marker scan is only needed after an uncertain previous attempt.
        # Avoiding it on the normal path removes one REST request per member.
        if sent is None and (message.attempts > 1 or existing_message_id):
            sent = await find_delivery_marker(dm_channel, marker)
            needs_cleanup = needs_cleanup or sent is not None
        # Discord resolution and history lookup can take seconds.  Re-check the
        # durable owner immediately before mutating the shared message.
        current = await _active_delivery_session(guild_id, session_key)
        if current is None or not _control_delivery_matches(
            current,
            phase=phase,
            bill_id=bill_id,
            user_id=user_id,
        ) or not await asyncio.to_thread(
            _outbox_storage.delivery_outbox_is_current_supersession,
            message.id,
            topic=message.topic,
            supersede_key=message.supersede_key,
        ):
            return DeliveryReceipt(message_id=int(sent.id) if sent is not None else None)
        created_new = sent is None
        if sent is None:
            sent = await member.send(content=content, embed=embed, view=view)
        else:
            try:
                await sent.edit(content=content, embed=embed, view=view)
            except discord.NotFound:
                # The panel can be deleted between fetch and edit.  This is a
                # missing projection, not a permanently closed DM destination.
                sent = await member.send(content=content, embed=embed, view=view)
                created_new = True
                needs_cleanup = True
        message_id = int(sent.id)
    except DeliveryDeferred:
        raise
    except _DeliveryDestinationUnavailable as exc:
        await _record_dm_failure(
            guild_id=guild_id,
            session_key=session_key,
            user_id=user_id,
            phase=phase,
            bill_id=bill_id,
            error=exc,
        )
        raise
    except discord.DiscordException as exc:
        try:
            _raise_classified_discord_error(exc, missing_is_permanent=True)
        except _DeliveryDestinationUnavailable as classified:
            await _record_dm_failure(
                guild_id=guild_id,
                session_key=session_key,
                user_id=user_id,
                phase=phase,
                bill_id=bill_id,
                error=classified,
            )
            raise

    async with consensus_session_lock(guild_id):
        current = _consensus_registry.find(session_key)
        state_matches = current is not None and _control_delivery_matches(
            current,
            phase=phase,
            bill_id=bill_id,
            user_id=user_id,
        )
        supersession_matches = await asyncio.to_thread(
            _outbox_storage.delivery_outbox_is_current_supersession,
            message.id,
            topic=message.topic,
            supersede_key=message.supersede_key,
        )
        if not state_matches or not supersession_matches:
            try:
                if created_new and hasattr(sent, "delete"):
                    await sent.delete()
                elif not state_matches:
                    await sent.edit(view=None)
            except discord.DiscordException:
                pass
            return DeliveryReceipt(message_id=message_id)

        def persist_receipt() -> None:
            with _consensus.mutation(current):
                target = current.participants[user_id]
                if phase == "registration":
                    changed = int(target.dm_message_id or 0) != message_id or target.dm_failed
                    target.dm_message_id = message_id
                else:
                    changed = (
                        int(target.vote_message_id or 0) != message_id
                        or int(target.vote_bill_id or 0) != bill_id
                        or target.dm_failed
                    )
                    target.vote_message_id = message_id
                    target.vote_bill_id = bill_id
                target.dm_failed = False
                if not changed:
                    return
                _consensus.save(
                    current,
                    f"{phase}_control_delivered",
                    details={
                        "user_id": user_id,
                        "message_id": message_id,
                        "bill_id": bill_id or None,
                    },
                )

        await run_blocking_cancellation_safe(persist_receipt)
    if needs_cleanup:
        _schedule_control_cleanup(dm_channel, message_id)
    return DeliveryReceipt(message_id=message_id)


async def deliver_consensus_control_notice(
    message: OutboxMessage,
    bot: commands.Bot | discord.Client,
) -> DeliveryReceipt:
    """Create the unread voting event independently from the control panel."""

    payload = message.payload
    if int(payload.get("payload_version") or 0) != 1:
        raise DeliveryPermanentFailure("tvrs_control_notice_payload_version_unsupported")
    guild_id = int(payload.get("guild_id") or 0)
    session_key = str(payload.get("session_key") or "")
    bill_id = int(payload.get("bill_id") or 0)
    user_id = int(payload.get("user_id") or 0)
    if guild_id <= 0 or bill_id <= 0 or user_id <= 0:
        raise DeliveryPermanentFailure("tvrs_control_notice_payload_invalid")
    session = await _active_delivery_session(guild_id, session_key)
    if session is None or not _control_delivery_matches(
        session,
        phase="voting",
        bill_id=bill_id,
        user_id=user_id,
    ):
        return DeliveryReceipt()
    participant = session.participants[user_id]
    panel_message_id = int(participant.vote_message_id or 0)
    if panel_message_id <= 0 or int(participant.vote_bill_id or 0) != bill_id:
        control_scope = f"consensus:{session_key}:control:{user_id}"
        latest_control = await asyncio.to_thread(
            _outbox_storage.delivery_outbox_latest_supersession,
            topic=TVRS_CONTROL_DM_TOPIC,
            supersede_key=control_scope,
        )
        if participant.dm_failed or str((latest_control or {}).get("status") or "") in {
            "dead",
            "cancelled",
        }:
            # The public entry panel is the durable fallback.  A notice must not
            # spin forever when its private destination is conclusively closed.
            return DeliveryReceipt()
        raise DeliveryDeferred(
            datetime.now(timezone.utc) + timedelta(seconds=2),
            "control_panel_pending",
        )
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise RuntimeError(f"tvrs_delivery_guild_unavailable:{guild_id}")
    try:
        member = await _resolve_delivery_member(guild, user_id)
        dm_channel = member.dm_channel or await member.create_dm()
        marker = delivery_marker(message.dedupe_key)
        if message.attempts > 1 or bool(payload.get("recover_marker")):
            previous = await find_delivery_marker(dm_channel, marker)
            if previous is not None:
                return DeliveryReceipt(message_id=int(previous.id))
        bill = session.current_bill or {}
        jump_url = (
            f"https://discord.com/channels/@me/{int(dm_channel.id)}/{panel_message_id}"
        )
        embed = discord.Embed(
            title="🔔 Новое голосование",
            description=(
                f"Открыт законопроект №{format_bill_number(int(bill.get('bill_number') or 0))}: "
                f"**{' '.join(str(bill.get('title') or 'Законопроект').split())[:160]}**\n"
                f"[Открыть единую панель голосования]({jump_url})"
            ),
            color=TVRS_EMBED_COLOR,
            timestamp=now_local(),
        )
        embed.set_footer(text=marker)
        sent = await member.send(embed=embed)
        return DeliveryReceipt(message_id=int(sent.id))
    except DeliveryDeferred:
        raise
    except _DeliveryDestinationUnavailable as exc:
        await _record_dm_failure(
            guild_id=guild_id,
            session_key=session_key,
            user_id=user_id,
            phase="voting",
            bill_id=bill_id,
            error=exc,
        )
        # The canonical panel and server fallback remain usable. A closed-DM
        # notice is not allowed to poison the already successful panel job.
        return DeliveryReceipt()
    except discord.DiscordException as exc:
        try:
            _raise_classified_discord_error(exc, missing_is_permanent=True)
        except _DeliveryDestinationUnavailable as classified:
            await _record_dm_failure(
                guild_id=guild_id,
                session_key=session_key,
                user_id=user_id,
                phase="voting",
                bill_id=bill_id,
                error=classified,
            )
            return DeliveryReceipt()


async def deliver_consensus_phase_announcement(
    message: OutboxMessage,
    bot: commands.Bot | discord.Client,
) -> DeliveryReceipt:
    """Publish one aggregate server fallback for registration or voting."""

    payload = message.payload
    if int(payload.get("payload_version") or 0) != 1:
        raise DeliveryPermanentFailure("tvrs_phase_announcement_payload_version_unsupported")
    guild_id = int(payload.get("guild_id") or 0)
    session_key = str(payload.get("session_key") or "")
    channel_id = int(payload.get("channel_id") or 0)
    phase = str(payload.get("phase") or "")
    bill_id = int(payload.get("bill_id") or 0)
    if guild_id <= 0 or channel_id <= 0 or phase not in {"registration", "voting"}:
        raise DeliveryPermanentFailure("tvrs_phase_announcement_payload_invalid")
    session = await _active_delivery_session(guild_id, session_key)
    if session is None or session.finished:
        return DeliveryReceipt()
    if phase == "registration" and session.stage != "registration":
        return DeliveryReceipt()
    if phase == "voting" and (
        session.stage not in {"voting", "paused", "discussion_type", "discussion"}
        or int((session.current_bill or {}).get("id") or 0) != bill_id
    ):
        return DeliveryReceipt()
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise RuntimeError(f"tvrs_delivery_guild_unavailable:{guild_id}")
    channel = guild.get_channel(channel_id) or bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.DiscordException as exc:
            _raise_classified_discord_error(exc, missing_is_permanent=True)
    if not hasattr(channel, "send"):
        raise RuntimeError(f"tvrs_delivery_channel_unavailable:{channel_id}")
    marker = delivery_marker(message.dedupe_key)
    if message.attempts > 1:
        previous = await find_delivery_marker(channel, marker)
        if previous is not None:
            return DeliveryReceipt(message_id=int(previous.id))
    user_ids = sorted({int(item) for item in payload.get("user_ids") or [] if int(item) > 0})
    content = " ".join(f"<@{user_id}>" for user_id in user_ids) or None
    if phase == "registration":
        title = "🟦 Открыта регистрация на консенсус"
        description = (
            "Подтвердите участие в личном пульте. Если ЛС закрыты, нажмите кнопку "
            "под этим сообщением — действия на сервере полностью равнозначны ЛС."
        )
    else:
        title = "⚖️ Открыто новое голосование"
        description = (
            f"Законопроект №{format_bill_number(int(payload.get('bill_number') or 0))}: "
            f"**{str(payload.get('bill_title') or 'Законопроект')[:220]}**\n"
            "Голосуйте через личный пульт в ЛС или через кнопку ниже."
        )
    embed = discord.Embed(
        title=title,
        description=description,
        color=TVRS_EMBED_COLOR,
        timestamp=now_local(),
    )
    embed.set_footer(text=marker)
    from modules.tvrs_consensus_portal import TVRSConsensusEntryView

    try:
        sent = await channel.send(
            content=content,
            embed=embed,
            view=TVRSConsensusEntryView(),
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=False,
                everyone=False,
            ),
        )
    except discord.DiscordException as exc:
        _raise_classified_discord_error(exc, missing_is_permanent=True)
    return DeliveryReceipt(message_id=int(sent.id))


async def deliver_consensus_discussion_invite(
    message: OutboxMessage,
    bot: commands.Bot | discord.Client,
) -> DeliveryReceipt:
    """Deliver and receipt one discussion reply anchor."""

    payload = message.payload
    if int(payload.get("payload_version") or 0) != 1:
        raise DeliveryPermanentFailure("tvrs_discussion_invite_payload_version_unsupported")
    guild_id = int(payload.get("guild_id") or 0)
    session_key = str(payload.get("session_key") or "")
    bill_id = int(payload.get("bill_id") or 0)
    channel_id = int(payload.get("channel_id") or 0)
    user_id = int(payload.get("user_id") or 0)
    if min(guild_id, bill_id, channel_id, user_id) <= 0:
        raise DeliveryPermanentFailure("tvrs_discussion_invite_payload_invalid")
    session = await _active_delivery_session(guild_id, session_key)
    participant = session.participants.get(user_id) if session is not None else None
    if (
        session is None
        or session.stage != "discussion"
        or int((session.current_bill or {}).get("id") or 0) != bill_id
        or int(session.discussion_channel_id or 0) != channel_id
        or participant is None
        or user_id not in session.discussion_allowed_user_ids
    ):
        return DeliveryReceipt()
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise RuntimeError(f"tvrs_delivery_guild_unavailable:{guild_id}")
    try:
        member = await _resolve_delivery_member(guild, user_id)
        dm_channel = member.dm_channel or await member.create_dm()
        marker = delivery_marker(message.dedupe_key)
        if message.attempts > 1:
            previous = await find_delivery_marker(dm_channel, marker)
            if previous is not None:
                sent = previous
            else:
                sent = None
        else:
            sent = None
        if sent is None:
            embed = build_discussion_embed(session)
            embed.set_footer(text=marker)
            sent = await member.send(
                content=(
                    "Дискуссия начата. Ответьте именно на это сообщение, чтобы бот "
                    "перенёс вашу позицию и материалы в канал дискуссии."
                ),
                embed=embed,
            )
        message_id = int(sent.id)
    except _DeliveryDestinationUnavailable as exc:
        await _record_dm_failure(
            guild_id=guild_id,
            session_key=session_key,
            user_id=user_id,
            phase="voting",
            bill_id=bill_id,
            error=exc,
        )
        raise
    except discord.DiscordException as exc:
        try:
            _raise_classified_discord_error(exc, missing_is_permanent=True)
        except _DeliveryDestinationUnavailable as classified:
            await _record_dm_failure(
                guild_id=guild_id,
                session_key=session_key,
                user_id=user_id,
                phase="voting",
                bill_id=bill_id,
                error=classified,
            )
            raise
    async with consensus_session_lock(guild_id):
        current = _consensus_registry.find(session_key)
        if (
            current is None
            or current.stage != "discussion"
            or int((current.current_bill or {}).get("id") or 0) != bill_id
        ):
            return DeliveryReceipt(message_id=message_id)

        def persist_receipt() -> None:
            with _consensus.mutation(current):
                target = current.participants[user_id]
                changed = (
                    int(target.discussion_message_id or 0) != message_id
                    or target.dm_failed
                )
                target.discussion_message_id = message_id
                target.dm_failed = False
                if not changed:
                    return
                _consensus.save(
                    current,
                    "discussion_invitation_delivered",
                    details={
                        "user_id": user_id,
                        "message_id": message_id,
                        "bill_id": bill_id,
                    },
                )

        await run_blocking_cancellation_safe(persist_receipt)
    return DeliveryReceipt(message_id=message_id)


async def enqueue_current_control_projection(
    guild: discord.Guild,
    session: LiveConsensusSession,
    *,
    content: str | None = None,
) -> int:
    """Queue a convergent participant-panel refresh for the current state.

    Lifecycle code calls this instead of performing a sequential DM loop.  A
    later revision supersedes queued older projections for each participant.
    """

    if int(guild.id) != int(session.guild_id) or session.finished:
        return 0
    if session.stage not in {"voting", "paused", "discussion_type", "discussion"}:
        return 0
    bill_id = int((session.current_bill or {}).get("id") or 0)
    if bill_id <= 0:
        return 0
    deliveries = build_control_dm_deliveries(
        session,
        phase="voting",
        bill_id=bill_id,
        generation=f"projection-r{int(session.revision)}-{session.stage}",
    )
    queued = 0
    for delivery in deliveries:
        payload = dict(delivery.get("payload") or {})
        if content:
            payload["content"] = str(content)[:1000]
        row = await asyncio.to_thread(
            _outbox_storage.delivery_outbox_enqueue,
            topic=str(delivery["topic"]),
            dedupe_key=str(delivery["dedupe_key"]),
            payload=payload,
            max_attempts=int(delivery.get("max_attempts") or 120),
            priority=int(delivery.get("priority") or 0),
            supersede_key=delivery.get("supersede_key"),
        )
        if str(row.get("status") or "") in {"pending", "retry", "processing"}:
            queued += 1
    if queued:
        wake_delivery_worker()
    return queued


async def update_host_registration_message(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession) -> None:
    edited = await edit_session_host_message(
        session,
        embed=build_registration_embed(session),
        view=TVRSRegistrationView(session.session_key),
    )
    if not edited and session.host_message_id:
        channel = guild.get_channel(session.channel_id)
        if isinstance(channel, discord.abc.Messageable):
            try:
                msg = await channel.fetch_message(session.host_message_id)  # type: ignore[attr-defined]
                await msg.edit(embed=build_registration_embed(session), view=TVRSRegistrationView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            except discord.DiscordException:
                pass
    await update_public_consensus_card(bot, guild, session)


async def update_host_vote_message(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession) -> None:
    edited = await edit_session_host_message(
        session,
        embed=build_live_vote_embed(session),
        view=TVRSHostVoteView(session.session_key),
    )
    if not edited and session.host_message_id:
        channel = guild.get_channel(session.channel_id)
        if isinstance(channel, discord.abc.Messageable):
            try:
                msg = await channel.fetch_message(session.host_message_id)  # type: ignore[attr-defined]
                await msg.edit(embed=build_live_vote_embed(session), view=TVRSHostVoteView(session.session_key), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            except discord.DiscordException:
                pass
    await update_public_consensus_card(bot, guild, session)


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
                try:
                    await run_blocking_cancellation_safe(
                        _consensus.pause,
                        session,
                        voice_reason,
                        automatic=True,
                        actor=None,
                    )
                finally:
                    if session.stage == "paused" and session.timer_deadline is None:
                        await cancel_vote_timer(session)
                paused_reason = voice_reason
        else:
            bills = await asyncio.to_thread(storage.tvrs_queue_bills, guild.id, 1)
            if bills:
                bill = bills[0]
                deliveries = build_control_dm_deliveries(
                    session,
                    phase="voting",
                    bill_id=int(bill["id"]),
                )
                deliveries.extend(
                    build_control_notice_deliveries(
                        session,
                        bill_id=int(bill["id"]),
                    )
                )
                # The candidate bill is not bound to the live session until
                # the atomic commit. Build the server fallback from a temporary
                # current-bill reference, then restore the pre-transition state.
                previous_bill = session.current_bill
                session.current_bill = dict(bill)
                try:
                    deliveries.append(
                        build_phase_announcement_delivery(
                            session,
                            phase="voting",
                            bill_id=int(bill["id"]),
                        )
                    )
                finally:
                    session.current_bill = previous_bill
                await run_blocking_cancellation_safe(
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
    await update_public_consensus_card(bot, guild, session)

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
                await run_blocking_cancellation_safe(
                    _consensus.bind_host_message,
                    session,
                    msg.id,
                    "host_panel_bound",
                )
                host_bound = True
        if not host_bound:
            try:
                await msg.edit(view=None)
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

__all__ = [
    "_finalization_retry_tasks",
    "_control_cleanup_tasks",
    "_control_delivery_matches",
    "_prune_duplicate_control_panels",
    "deliver_consensus_control_dm",
    "deliver_consensus_control_notice",
    "deliver_consensus_phase_announcement",
    "deliver_consensus_discussion_invite",
    "enqueue_current_control_projection",
    "update_public_consensus_card",
    "update_host_registration_message",
    "update_host_vote_message",
    "begin_next_bill_vote",
    "retry_pending_finalization_once",
    "schedule_finalization_retry",
    "clear_finalization_retry",
]
