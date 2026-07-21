"""Durable retry loop for consensus result finalization."""

from __future__ import annotations

import asyncio
import traceback

import discord
from discord.ext import commands

from modules.consensus_core import LiveConsensusSession
from modules.consensus_service import ConsensusActor
from modules.technical_log import log_technical_event
from modules.tvrs_config import TVRS_PERMANENT_CHAIR_ID


_finalization_retry_tasks: dict[str, asyncio.Task] = {}


async def retry_pending_finalization_once(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
) -> bool:
    if session.stage != "finalizing":
        return True
    from modules.tvrs_decision import (
        apply_veto_for_actor,
        finalize_current_vote,
        record_oral_result,
    )

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
    elif pending.get("kind") == "oral":
        await record_oral_result(
            bot,
            guild,
            session,
            ConsensusActor(
                int(pending.get("actor_id") or session.leader_id),
                str(pending.get("actor_display") or session.leader_display),
            ),
            status=str(pending.get("oral_status") or "rejected"),
            note=str(pending.get("oral_note") or "Устное решение"),
            expected_bill_id=int(pending.get("bill_id") or 0),
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
    "clear_finalization_retry",
    "retry_pending_finalization_once",
    "schedule_finalization_retry",
]
