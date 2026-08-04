from __future__ import annotations

import asyncio
import traceback
import uuid
from datetime import datetime, timezone

import discord
from discord.ext import commands

from persistence import activity_repository as _activity_storage
from persistence import outbox_repository as _outbox_storage
from persistence import tvrs_repository as _tvrs_storage
from modules.async_safety import run_blocking_cancellation_safe
from modules.consensus_core import (
    ConsensusStateError,
    LiveConsensusSession,
)
from modules.consensus_finalization_recovery import retry_pending_finalization_once
from modules.consensus_health import assess_consensus_health
from modules.consensus_operational import collect_consensus_operational_state
from modules.consensus_runtime import (
    coordinator as _consensus,
    registry as _consensus_registry,
    repository as _consensus_repository,
    restored_guilds as _restored_consensus_guilds,
)
from modules.delivery_runtime import wake_delivery_worker
from modules.operations_runtime import wake_operations_worker
from modules.public_panel_runtime import register_public_panel_provider
from modules.tvrs_config import (
    TVRS_MATERIALS_CHANNEL_ID,
    TVRS_STICKY_DEBOUNCE_SECONDS,
)
from modules.tvrs_navigation_runtime import register_tvrs_hub_handler
from modules.tvrs_delivery import (
    build_control_dm_deliveries,
    build_control_notice_deliveries,
    build_discussion_invite_deliveries,
    build_phase_announcement_delivery,
    build_retry_bill_delivery,
    build_result_deliveries,
)
from modules.technical_log import log_technical_event

from modules.tvrs_presentation import (
    _open_tvrs_hub_impl,
    build_public_universality_embed,
    build_sticky_embed,
    consensus_bill_id,
)
from modules.tvrs_hub_views import TVRSPublicPanelView, TVRSStickyView
from modules.tvrs_consensus_views import TVRSConfirmView, TVRSRestoredVoteView
from modules.tvrs_decision import finalize_current_vote
from modules.tvrs_discussion import (
    cancel_vote_timer,
    check_realtime_quorum,
    schedule_vote_timer_task,
)

_sticky_locks: dict[int, asyncio.Lock] = {}
_sticky_tasks: dict[int, asyncio.Task] = {}
_consensus_recovery_tasks: dict[int, asyncio.Task] = {}
_consensus_restore_locks: dict[int, asyncio.Lock] = {}
_consensus_delivery_watchdog_task: asyncio.Task | None = None


async def _cleanup_restored_control_copies(
    guild: discord.Guild,
    user_id: int,
    keep_message_id: int,
) -> None:
    resolver = getattr(guild, "get_member", None)
    if resolver is None:
        return
    member = resolver(int(user_id))
    if member is None:
        fetch = getattr(guild, "fetch_member", None)
        if fetch is None:
            return
        try:
            member = await fetch(int(user_id))
        except discord.DiscordException:
            return
    try:
        dm_channel = member.dm_channel or await member.create_dm()
    except discord.DiscordException:
        return
    from modules.tvrs_control import _schedule_control_cleanup

    _schedule_control_cleanup(dm_channel, int(keep_message_id))

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
        old_id_raw = _activity_storage.get_meta(meta_key)
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
        _activity_storage.set_meta_value(meta_key, str(msg.id))


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
    from modules.tvrs_consensus_portal import TVRSConsensusEntryView
    from modules.tvrs_bill_editor import register_bill_workspace_views

    register_public_panel_provider(build_public_universality_embed, TVRSPublicPanelView)
    register_tvrs_hub_handler(_open_tvrs_hub_impl)
    bot.add_view(TVRSStickyView())
    bot.add_view(TVRSPublicPanelView())
    bot.add_view(TVRSConsensusEntryView())
    register_bill_workspace_views(bot)


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
    await recover_consensus_session(
        bot,
        guild,
        session,
        verify_discord_messages=True,
        retry_permanent_failures=True,
    )

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
                _outbox_storage.delivery_outbox_enqueue,
                topic=str(delivery["topic"]),
                dedupe_key=str(delivery["dedupe_key"]),
                payload=payload,
                max_attempts=int(delivery.get("max_attempts") or 8),
            )
        wake_delivery_worker()
        if result.retry_bill_number:
            retry = await asyncio.to_thread(
                _tvrs_storage.tvrs_get_bill_by_number,
                guild.id,
                result.retry_bill_number,
            )
            if retry is not None and not retry.get("message_id"):
                delivery = build_retry_bill_delivery(session, result, retry)
                await asyncio.to_thread(
                    _outbox_storage.delivery_outbox_enqueue,
                    topic=str(delivery["topic"]),
                    dedupe_key=str(delivery["dedupe_key"]),
                    payload=dict(delivery["payload"]),
                    max_attempts=int(delivery.get("max_attempts") or 8),
                )
                wake_delivery_worker()

    # The card is only a projection of durable state. Rebuild it last, after
    # interrupted finalization and timer recovery have settled the session.
    from modules.tvrs_control import update_public_consensus_card

    await update_public_consensus_card(
        bot,
        guild,
        session,
        terminal=session.finished,
    )


async def reconcile_consensus_liveness(
    bot: commands.Bot,
    guild: discord.Guild,
    session: LiveConsensusSession,
) -> bool:
    """Resume durable business actions that lost their in-memory coroutine."""

    if session.finished:
        return False
    if assess_consensus_health(session).critical:
        return False
    if session.stage == "finalizing":
        await retry_pending_finalization_once(bot, guild, session)
        return True
    if session.stage != "voting" or session.current_bill is None:
        return False
    bill_id = consensus_bill_id(session)
    if session.all_voted():
        await finalize_current_vote(
            bot,
            guild,
            session,
            forced=False,
            expected_bill_id=bill_id,
        )
        return True
    if session.timer_deadline is None:
        return False
    remaining = int(
        (session.timer_deadline - datetime.now(timezone.utc)).total_seconds()
    )
    if remaining <= 0:
        await finalize_current_vote(
            bot,
            guild,
            session,
            forced=True,
            expected_bill_id=bill_id,
        )
        return True
    timer_task = session.timer_task
    if timer_task is None or timer_task.done():
        schedule_vote_timer_task(
            bot,
            guild,
            session,
            remaining,
            bill_id=bill_id,
        )
        return True
    return False


async def repair_safe_consensus_invariants(
    session: LiveConsensusSession,
) -> tuple[str, ...]:
    """Persist unambiguous cleanup before touching runtime projections."""

    async with _consensus_registry.lock(session.guild_id):
        repaired = await run_blocking_cancellation_safe(
            _consensus.repair_safe_invariants,
            session,
        )
    if "timer_outside_voting" in repaired or "partial_timer_state" in repaired:
        await cancel_vote_timer(session)
    if repaired:
        wake_operations_worker()
    return tuple(repaired)


async def requeue_consensus_dead_deliveries(session: LiveConsensusSession) -> int:
    revived = await asyncio.to_thread(
        _outbox_storage.delivery_outbox_requeue_dead_for_consensus,
        session.session_key,
        guild_id=session.guild_id,
    )
    if revived:
        wake_delivery_worker()
    return len(revived)


async def recover_consensus_session(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    *,
    verify_discord_messages: bool,
    retry_permanent_failures: bool,
) -> dict[str, object]:
    """Run the safe automatic recovery pipeline in dependency order."""

    repaired = await repair_safe_consensus_invariants(session)
    # Production guilds expose the channel cache. Lightweight adapters used by
    # migrations/tests do not; absence of that interface is unknown state, not
    # proof that quorum disappeared.
    if callable(getattr(guild, "get_channel", None)):
        await check_realtime_quorum(bot, guild, session)
    liveness_changed = await reconcile_consensus_liveness(bot, guild, session)
    # Permanent failures are retried only during an explicit/full recovery
    # (startup or chair action), never on every watchdog tick.
    revived = (
        await requeue_consensus_dead_deliveries(session)
        if retry_permanent_failures
        else 0
    )
    queued = await reconcile_current_consensus_deliveries(
        bot,
        guild,
        session,
        verify_discord_messages=verify_discord_messages,
        retry_permanent_failures=retry_permanent_failures,
    )
    return {
        "repaired": repaired,
        "liveness_changed": liveness_changed,
        "revived": revived,
        "queued": queued,
    }


async def _saved_control_message_exists(
    guild: discord.Guild,
    user_id: int,
    message_id: int,
) -> bool | None:
    """Return True/False, or None when Discord cannot answer reliably."""

    resolver = getattr(guild, "get_member", None)
    if not callable(resolver):
        # Lightweight adapters cannot verify Discord state.  Trusting the
        # durable receipt is safer than manufacturing a duplicate message.
        return True
    member = resolver(int(user_id))
    if member is None:
        fetch_member = getattr(guild, "fetch_member", None)
        if not callable(fetch_member):
            return True
        try:
            member = await fetch_member(int(user_id))
        except (discord.NotFound, discord.Forbidden):
            return False
        except discord.HTTPException:
            return None
    try:
        dm_channel = member.dm_channel or await member.create_dm()
    except (discord.NotFound, discord.Forbidden):
        return False
    except discord.HTTPException:
        return None
    fetch_message = getattr(dm_channel, "fetch_message", None)
    if fetch_message is None:
        # Lightweight test adapters and old discord.py shims cannot validate;
        # preserve the durable receipt instead of creating a possible duplicate.
        return True
    try:
        await fetch_message(int(message_id))
        return True
    except discord.NotFound:
        return False
    except discord.Forbidden:
        return False
    except discord.HTTPException:
        return None


async def _enqueue_control_jobs(
    session: LiveConsensusSession,
    *,
    phase: str,
    user_ids: set[int],
    retry_permanent_failures: bool,
    generation: str,
    replace_live: bool = False,
) -> set[int]:
    if not user_ids:
        return set()
    if phase == "voting" and (
        session.current_bill is None or consensus_bill_id(session) <= 0
    ):
        # A pause between bills is a valid state.  Recovery must never invent
        # a bill identity merely to rebuild an old voting panel.
        return set()
    deliveries = build_control_dm_deliveries(
        session,
        phase=phase,
        bill_id=consensus_bill_id(session) if phase == "voting" else None,
        generation=str(generation),
    )
    created: set[int] = set()
    for delivery in deliveries:
        payload = dict(delivery.get("payload") or {})
        user_id = int(payload.get("user_id") or 0)
        participant = session.participants.get(user_id)
        if user_id not in user_ids or participant is None:
            continue
        if participant.dm_failed and not retry_permanent_failures:
            continue
        _, was_created = await asyncio.to_thread(
            _outbox_storage.delivery_outbox_ensure_current,
            topic=str(delivery["topic"]),
            dedupe_key=str(delivery["dedupe_key"]),
            payload=payload,
            max_attempts=int(delivery.get("max_attempts") or 120),
            priority=int(delivery.get("priority") or 0),
            supersede_key=delivery.get("supersede_key"),
            replace_live=replace_live,
        )
        if was_created:
            created.add(user_id)
    return created


async def _enqueue_repair_control_jobs(
    session: LiveConsensusSession,
    *,
    phase: str,
    user_ids: set[int],
    retry_permanent_failures: bool,
) -> set[int]:
    return await _enqueue_control_jobs(
        session,
        phase=phase,
        user_ids=user_ids,
        retry_permanent_failures=retry_permanent_failures,
        generation=f"repair-r{int(session.revision)}-{uuid.uuid4().hex[:12]}",
    )


async def _enqueue_current_control_projection(
    session: LiveConsensusSession,
    *,
    phase: str,
    user_ids: set[int],
    retry_permanent_failures: bool,
) -> set[int]:
    """Enqueue one idempotent projection for this exact durable state."""

    return await _enqueue_control_jobs(
        session,
        phase=phase,
        user_ids=user_ids,
        retry_permanent_failures=retry_permanent_failures,
        generation=f"projection-r{int(session.revision)}-{session.stage}",
        replace_live=True,
    )


async def _enqueue_semantic_delivery(delivery: dict) -> bool:
    row = await asyncio.to_thread(
        _outbox_storage.delivery_outbox_enqueue,
        topic=str(delivery["topic"]),
        dedupe_key=str(delivery["dedupe_key"]),
        payload=dict(delivery.get("payload") or {}),
        max_attempts=int(delivery.get("max_attempts") or 8),
        priority=int(delivery.get("priority") or 0),
        supersede_key=delivery.get("supersede_key"),
    )
    return str(row.get("status") or "") in {"pending", "retry", "processing"}


async def reconcile_current_consensus_deliveries(
    bot: commands.Bot,
    guild: discord.Guild,
    session: LiveConsensusSession,
    *,
    verify_discord_messages: bool,
    retry_permanent_failures: bool,
) -> int:
    """Converge every active participant toward one current control panel."""

    if session.finished:
        return 0
    missing: set[int] = set()
    projection_targets: set[int] = set()
    uncertain = False
    phase: str | None = None
    bill_id = consensus_bill_id(session)
    if session.stage == "registration":
        phase = "registration"
        candidates = [
            item
            for item in session.participants.values()
            if not item.confirmed and item.user_id != session.leader_id
        ]
        projection_targets = {int(item.user_id) for item in candidates}
        for participant in candidates:
            message_id = int(participant.dm_message_id or 0)
            exists = (
                await _saved_control_message_exists(
                    guild,
                    participant.user_id,
                    message_id,
                )
                if message_id and verify_discord_messages
                else bool(message_id)
            )
            if exists is None:
                uncertain = True
                continue
            if not exists:
                missing.add(participant.user_id)
                continue
            if verify_discord_messages:
                try:
                    register_restored_view(
                        bot,
                        TVRSConfirmView(session.session_key, participant.user_id),
                        message_id=message_id,
                    )
                except RuntimeError:
                    missing.add(participant.user_id)
                    uncertain = True
                    continue
                await _cleanup_restored_control_copies(
                    guild,
                    participant.user_id,
                    message_id,
                )
    elif (
        session.stage in {
            "presentation",
            "voting",
            "paused",
            "discussion_type",
            "discussion",
        }
        and session.current_bill is not None
        and bill_id > 0
    ):
        phase = (
            "presentation"
            if session.stage == "presentation"
            or (
                session.stage == "paused"
                and session.previous_stage == "presentation"
            )
            else "voting"
        )
        candidates = [
            item
            for item in session.confirmed_participants()
            if item.user_id != session.leader_id
        ]
        projection_targets = {int(item.user_id) for item in candidates}
        for participant in candidates:
            message_id = int(participant.vote_message_id or 0)
            receipt_matches = message_id > 0 and int(participant.vote_bill_id or 0) == bill_id
            exists = (
                await _saved_control_message_exists(
                    guild,
                    participant.user_id,
                    message_id,
                )
                if receipt_matches and verify_discord_messages
                else receipt_matches
            )
            if exists is None:
                uncertain = True
                continue
            # Non-voting stages must be projected once after restart so stale
            # voting buttons are removed even when the receipt still exists.
            if not exists or (
                verify_discord_messages
                and session.stage not in {"presentation", "voting"}
            ):
                missing.add(participant.user_id)
                continue
            if verify_discord_messages and session.stage == "voting":
                try:
                    register_restored_view(
                        bot,
                        TVRSRestoredVoteView(
                            session.session_key,
                            participant.user_id,
                            bill_id=bill_id,
                        ),
                        message_id=message_id,
                    )
                except RuntimeError:
                    missing.add(participant.user_id)
                    uncertain = True
                    continue
                await _cleanup_restored_control_copies(
                    guild,
                    participant.user_id,
                    message_id,
                )

    queued = 0
    if phase is not None:
        # Every durable revision/stage owns one stable projection generation.
        # Startup and the watchdog both call this path; the semantic dedupe key
        # prevents periodic duplicate jobs while healing a lost enqueue after
        # pause/resume/discussion transitions.
        projected = await _enqueue_current_control_projection(
            session,
            phase=phase,
            user_ids=projection_targets,
            retry_permanent_failures=retry_permanent_failures,
        )
        queued += len(projected)
        repaired = await _enqueue_repair_control_jobs(
            session,
            phase=phase,
            user_ids=missing - projected,
            retry_permanent_failures=retry_permanent_failures,
        )
        queued += len(repaired)

    # Reconstruct split notification/fallback jobs introduced after older
    # active snapshots were created. Stable semantic keys make this idempotent.
    if session.stage == "registration":
        queued += int(
            await _enqueue_semantic_delivery(
                build_phase_announcement_delivery(session, phase="registration")
            )
        )
    elif (
        session.stage == "presentation"
        and session.current_bill is not None
        and bill_id > 0
    ):
        queued += int(
            await _enqueue_semantic_delivery(
                build_phase_announcement_delivery(
                    session,
                    phase="presentation",
                    bill_id=bill_id,
                )
            )
        )
    elif (
        session.stage in {"voting", "paused", "discussion_type", "discussion"}
        and not (
            session.stage == "paused" and session.previous_stage == "presentation"
        )
        and session.current_bill is not None
        and bill_id > 0
    ):
        for delivery in build_control_notice_deliveries(session, bill_id=bill_id):
            delivery = dict(delivery)
            delivery["payload"] = {
                **dict(delivery.get("payload") or {}),
                "recover_marker": True,
            }
            queued += int(await _enqueue_semantic_delivery(delivery))
        queued += int(
            await _enqueue_semantic_delivery(
                build_phase_announcement_delivery(
                    session,
                    phase="voting",
                    bill_id=bill_id,
                )
            )
        )
    if (
        session.stage == "discussion"
        and session.current_bill is not None
        and bill_id > 0
        and int(session.discussion_channel_id or 0) > 0
        and str(session.discussion_type or "").strip()
    ):
        allowed_user_ids = {
            int(user_id)
            for user_id in session.discussion_allowed_user_ids
            if int(user_id) in session.participants
        }
        discussion_jobs = build_discussion_invite_deliveries(
            session,
            channel_id=int(session.discussion_channel_id),
            discussion_type=str(session.discussion_type),
            allowed_user_ids=allowed_user_ids,
        )
        for delivery in discussion_jobs:
            user_id = int(dict(delivery.get("payload") or {}).get("user_id") or 0)
            participant = session.participants.get(user_id)
            if participant is not None and participant.discussion_message_id:
                continue
            if (
                participant is not None
                and participant.dm_failed
                and not retry_permanent_failures
            ):
                continue
            _, was_created = await asyncio.to_thread(
                _outbox_storage.delivery_outbox_ensure_current,
                topic=str(delivery["topic"]),
                dedupe_key=(
                    f"{delivery['dedupe_key']}:repair-r{int(session.revision)}-"
                    f"{uuid.uuid4().hex[:8]}"
                ),
                payload=dict(delivery.get("payload") or {}),
                max_attempts=int(delivery.get("max_attempts") or 120),
                priority=int(delivery.get("priority") or 0),
                supersede_key=delivery.get("supersede_key"),
            )
            queued += int(was_created)
    if queued:
        wake_delivery_worker()
    if uncertain:
        raise RuntimeError("consensus_control_verification_temporarily_unavailable")
    return queued


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


async def _reconcile_restored_session_once(
    bot: commands.Bot,
    guild: discord.Guild,
    session: LiveConsensusSession,
) -> bool:
    """Serialize on-ready/retry races for one guild and report one outcome."""

    guild_id = int(session.guild_id)
    lock = _consensus_restore_locks.setdefault(guild_id, asyncio.Lock())
    async with lock:
        if guild_id in _restored_consensus_guilds:
            return False
        health = assess_consensus_health(session)
        if health.critical:
            await log_technical_event(
                bot,
                guild,
                title="Консенсус требует проверки председателя",
                details=(
                    f"Сессия: `{session.session_key[:120]}`\n"
                    + "\n".join(f"• {item.message}" for item in health.critical[:5])
                    + "\nОткройте `/tvrs` → **Восстановление**."
                ),
                dedupe_key=f"consensus-health:{session.session_key}",
                cooldown_seconds=300,
            )
        try:
            await reconcile_restored_consensus_session(bot, guild, session)
        except Exception as exc:
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
            return False
        _restored_consensus_guilds.add(guild_id)
        retry_task = _consensus_recovery_tasks.pop(guild_id, None)
        if (
            retry_task is not None
            and retry_task is not asyncio.current_task()
            and not retry_task.done()
        ):
            retry_task.cancel()
        return True


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
            except (
                ConsensusStateError,
                KeyError,
                TypeError,
                ValueError,
                OverflowError,
                AttributeError,
            ) as exc:
                print(
                    "Consensus snapshot requires manual recovery: "
                    f"guild={guild_id} error={type(exc).__name__}: {str(exc)[:300]}"
                )
                await log_technical_event(
                    bot,
                    guild,
                    title="Сессия консенсуса сохранена для ручного восстановления",
                    details=(
                        f"Сессия: `{str(snapshot.get('session_key') or 'неизвестно')[:120]}`\n"
                        f"Причина: `{type(exc).__name__}: {str(exc)[:700]}`\n"
                        "Бот не изменял заседание и законопроект. Проверьте снимок и журнал событий вручную."
                    ),
                    dedupe_key=f"consensus-snapshot-integrity:{guild_id}",
                    cooldown_seconds=300,
                )
                schedule_consensus_recovery_retry(bot, guild_id)
                continue
        if session is None:
            continue
        if await _reconcile_restored_session_once(bot, guild, session):
            restored_count += 1

    if restored_count:
        wake_operations_worker()
    return restored_count


def ensure_consensus_delivery_watchdog(bot: commands.Bot) -> asyncio.Task:
    """Continuously heal missing transient control deliveries."""

    global _consensus_delivery_watchdog_task
    if (
        _consensus_delivery_watchdog_task is not None
        and not _consensus_delivery_watchdog_task.done()
    ):
        return _consensus_delivery_watchdog_task

    is_closed = getattr(bot, "is_closed", None)
    if not callable(is_closed):
        async def unsupported_adapter() -> None:
            return

        _consensus_delivery_watchdog_task = asyncio.create_task(
            unsupported_adapter(),
            name="tvrs-consensus-watchdog-unavailable",
        )
        return _consensus_delivery_watchdog_task

    async def runner() -> None:
        while not is_closed():
            try:
                await asyncio.sleep(60)
                for session in list(_consensus_registry.sessions.values()):
                    if session.finished:
                        continue
                    guild = bot.get_guild(session.guild_id)
                    if guild is None:
                        continue
                    try:
                        health = assess_consensus_health(session)
                        if health.critical:
                            await log_technical_event(
                                bot,
                                guild,
                                title="Нарушена целостность активного консенсуса",
                                details=(
                                    f"Сессия: `{session.session_key[:120]}`\n"
                                    + "\n".join(f"• {item.message}" for item in health.critical[:5])
                                    + "\nАвтоматические проекции продолжат восстанавливаться; решение принимает председатель через **Восстановление**."
                                ),
                                dedupe_key=f"consensus-health:{session.session_key}",
                                cooldown_seconds=300,
                            )
                        await recover_consensus_session(
                            bot,
                            guild,
                            session,
                            verify_discord_messages=False,
                            retry_permanent_failures=False,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        traceback.print_exc()
            except asyncio.CancelledError:
                raise
            except Exception:
                traceback.print_exc()

    _consensus_delivery_watchdog_task = asyncio.create_task(
        runner(),
        name="tvrs-consensus-delivery-watchdog",
    )
    return _consensus_delivery_watchdog_task


async def tvrs_ensure_sticky_all(bot: commands.Bot) -> None:
    await restore_tvrs_consensus_sessions(bot)
    ensure_consensus_delivery_watchdog(bot)
    for guild in bot.guilds:
        if _consensus_registry.get(guild.id) is not None:
            continue
        try:
            await ensure_sticky_message(bot, guild, force_repost=False)
        except Exception:
            traceback.print_exc()

__all__ = ['_sticky_locks', '_sticky_tasks', '_consensus_recovery_tasks', '_consensus_restore_locks', '_cleanup_restored_control_copies', 'get_materials_channel', 'ensure_sticky_message', 'schedule_sticky_refresh', 'register_tvrs_persistent_views', 'register_restored_view', 'reconcile_restored_consensus_session', 'reconcile_consensus_liveness', 'repair_safe_consensus_invariants', 'collect_consensus_operational_state', 'requeue_consensus_dead_deliveries', 'recover_consensus_session', 'reconcile_current_consensus_deliveries', 'schedule_consensus_recovery_retry', 'restore_tvrs_consensus_sessions', 'ensure_consensus_delivery_watchdog', 'tvrs_ensure_sticky_all']
