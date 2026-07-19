from __future__ import annotations

import asyncio
import traceback
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
from modules.delivery_runtime import wake_delivery_worker
from modules.delivery_outbox import DeliveryReceipt, OutboxMessage
from modules.operations_runtime import wake_operations_worker
from modules.tvrs_config import (
    TVRS_EMBED_COLOR,
    TVRS_PERMANENT_CHAIR_ID,
)
from modules.tvrs_formatting import (
    now_local,
    role_label,
)
from modules.tvrs_delivery import (
    build_control_dm_deliveries,
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

_finalization_retry_tasks: dict[str, asyncio.Task] = {}
_control_cleanup_tasks: set[asyncio.Task] = set()


async def _prune_duplicate_control_panels(channel: Any, keep_message_id: int) -> None:
    """Delete obsolete interactive TVRS copies without touching result cards."""

    history = getattr(channel, "history", None)
    if history is None:
        return
    try:
        async for candidate in history(limit=100):
            if int(getattr(candidate, "id", 0) or 0) == int(keep_message_id):
                continue
            if getattr(getattr(candidate, "author", None), "bot", False) is not True:
                continue
            if not getattr(candidate, "components", None):
                continue
            embeds = getattr(candidate, "embeds", ())
            is_control = any(
                str(getattr(getattr(embed, "footer", None), "text", "") or "").startswith(
                    "TVRS • результат зафиксирован • ref "
                )
                or "пленарный консенсус" in str(getattr(embed, "title", "") or "").lower()
                for embed in embeds
            )
            if not is_control:
                continue
            try:
                await candidate.delete()
            except discord.DiscordException:
                continue
    except (discord.DiscordException, AttributeError, TypeError, ValueError):
        return


def _schedule_control_cleanup(channel: Any, keep_message_id: int) -> None:
    task = asyncio.create_task(
        _prune_duplicate_control_panels(channel, keep_message_id),
        name=f"tvrs-control-cleanup:{int(keep_message_id)}",
    )
    _control_cleanup_tasks.add(task)
    task.add_done_callback(_control_cleanup_tasks.discard)

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
        view = (
            TVRSPermanentVoteView(session_key, user_id, bill_id=bill_id)
            if participant.permanent
            else TVRSVoteView(session_key, user_id, bill_id=bill_id)
        )
        bill = session.current_bill or {}
        number = str(bill.get("bill_number") or "—")
        title = " ".join(str(bill.get("title") or "Законопроект").split())[:160]
        content = None
        # Keep exactly one reusable control panel. A separate marker-fenced
        # notification below creates the unread event that Discord does not
        # produce when a message is merely edited.
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
                    target.vote_bill_id = bill_id
                target.dm_failed = False
                _consensus.save(
                    current,
                    f"{phase}_control_delivered",
                    details={"user_id": user_id, "message_id": message_id, "bill_id": bill_id or None},
                )

        await asyncio.to_thread(persist_receipt)
    if phase == "voting":
        # The notice identity belongs to the bill, not to a delivery retry or
        # manual recovery generation. This guarantees one unread notification
        # per participant and bill even when the outbox job is reconstructed.
        notice_marker = delivery_marker(
            f"consensus:{session_key}:bill:{bill_id}:notice:{user_id}"
        )
        notice = await find_delivery_marker(dm_channel, notice_marker)
        if notice is None:
            jump_url = str(getattr(sent, "jump_url", "") or "")
            notice_embed = discord.Embed(
                title="🔔 Новое голосование",
                description=(
                    f"Открыт законопроект №{number}: **{title}**\n"
                    + (
                        f"[Открыть единую панель голосования]({jump_url})"
                        if jump_url
                        else "Откройте актуальную панель T-Mod в этом диалоге."
                    )
                ),
                color=TVRS_EMBED_COLOR,
                timestamp=now_local(),
            )
            notice_embed.set_footer(text=notice_marker)
            await member.send(embed=notice_embed)
    _schedule_control_cleanup(dm_channel, message_id)
    return DeliveryReceipt(message_id=message_id)


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

__all__ = ['_finalization_retry_tasks', '_control_cleanup_tasks', '_control_delivery_matches', '_prune_duplicate_control_panels', 'deliver_consensus_control_dm', 'update_public_consensus_card', 'update_host_registration_message', 'update_host_vote_message', 'begin_next_bill_vote', 'notify_participants', 'retry_pending_finalization_once', 'schedule_finalization_retry', 'clear_finalization_retry']
