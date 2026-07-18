from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timezone

import discord
from discord.ext import commands

from persistence import activity_repository as _activity_storage
from persistence import outbox_repository as _outbox_storage
from persistence import tvrs_repository as _tvrs_storage
from modules.consensus_core import (
    ConsensusStateError,
    LiveConsensusSession,
)
from modules.consensus_runtime import (
    registry as _consensus_registry,
    repository as _consensus_repository,
    restored_guilds as _restored_consensus_guilds,
)
from modules.consensus_service import ConsensusActor
from modules.delivery_runtime import wake_delivery_worker
from modules.operations_runtime import wake_operations_worker
from modules.public_panel_runtime import register_public_panel_provider
from modules.tvrs_config import (
    TVRS_MATERIALS_CHANNEL_ID,
    TVRS_PERMANENT_CHAIR_ID,
    TVRS_STICKY_DEBOUNCE_SECONDS,
)
from modules.tvrs_navigation_runtime import register_tvrs_hub_handler
from modules.tvrs_delivery import (
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
from modules.tvrs_decision import apply_veto_for_actor, finalize_current_vote
from modules.tvrs_discussion import schedule_vote_timer_task

_sticky_locks: dict[int, asyncio.Lock] = {}
_sticky_tasks: dict[int, asyncio.Task] = {}
_consensus_recovery_tasks: dict[int, asyncio.Task] = {}

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
    register_public_panel_provider(build_public_universality_embed, TVRSPublicPanelView)
    register_tvrs_hub_handler(_open_tvrs_hub_impl)
    bot.add_view(TVRSStickyView())
    bot.add_view(TVRSPublicPanelView())


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
    if session.stage == "registration":
        for participant in session.participants.values():
            if participant.confirmed or not participant.dm_message_id:
                continue
            register_restored_view(
                bot,
                TVRSConfirmView(session.session_key, participant.user_id),
                message_id=participant.dm_message_id,
            )
    elif session.stage == "voting":
        for participant in session.confirmed_participants():
            if not participant.vote_message_id:
                continue
            register_restored_view(
                bot,
                TVRSRestoredVoteView(
                    session.session_key,
                    participant.user_id,
                    bill_id=consensus_bill_id(session),
                ),
                message_id=participant.vote_message_id,
            )

    if session.stage == "finalizing":
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

    if session.stage == "voting" and session.timer_deadline is not None:
        remaining = int((session.timer_deadline - datetime.now(timezone.utc)).total_seconds())
        if remaining <= 0:
            await finalize_current_vote(bot, guild, session, forced=True)
        else:
            schedule_vote_timer_task(bot, guild, session, remaining)

    # The card is only a projection of durable state. Rebuild it last, after
    # interrupted finalization and timer recovery have settled the session.
    from modules.tvrs_control import update_public_consensus_card

    await update_public_consensus_card(
        bot,
        guild,
        session,
        terminal=session.finished,
    )


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
            except (ConsensusStateError, KeyError, TypeError, ValueError) as exc:
                traceback.print_exc()
                await asyncio.to_thread(
                    _consensus_repository.quarantine,
                    str(snapshot.get("session_key") or ""),
                    f"{type(exc).__name__}: {exc}",
                )
                await log_technical_event(
                    bot,
                    guild,
                    title="Сессия консенсуса изолирована",
                    details=(
                        f"Сессия: `{str(snapshot.get('session_key') or 'неизвестно')[:120]}`\n"
                        f"Причина: `{type(exc).__name__}: {str(exc)[:700]}`\n"
                        "Текущий законопроект возвращён в очередь. Можно начать новое заседание."
                    ),
                    dedupe_key=f"consensus-quarantine:{guild_id}",
                    cooldown_seconds=300,
                )
                continue
        if session is None:
            continue
        try:
            await reconcile_restored_consensus_session(bot, guild, session)
        except Exception as exc:
            # A temporary Discord or storage outage must not mark recovery as
            # complete. A later on_ready pass can safely retry the same state.
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
            continue
        _restored_consensus_guilds.add(guild_id)
        retry_task = _consensus_recovery_tasks.pop(guild_id, None)
        if retry_task is not None and retry_task is not asyncio.current_task() and not retry_task.done():
            retry_task.cancel()
        restored_count += 1

    if restored_count:
        wake_operations_worker()
    return restored_count


async def tvrs_ensure_sticky_all(bot: commands.Bot) -> None:
    await restore_tvrs_consensus_sessions(bot)
    for guild in bot.guilds:
        if _consensus_registry.get(guild.id) is not None:
            continue
        try:
            await ensure_sticky_message(bot, guild, force_repost=False)
        except Exception:
            traceback.print_exc()

__all__ = ['_sticky_locks', '_sticky_tasks', '_consensus_recovery_tasks', 'get_materials_channel', 'ensure_sticky_message', 'schedule_sticky_refresh', 'register_tvrs_persistent_views', 'register_restored_view', 'reconcile_restored_consensus_session', 'schedule_consensus_recovery_retry', 'restore_tvrs_consensus_sessions', 'tvrs_ensure_sticky_all']
