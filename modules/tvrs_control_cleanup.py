from __future__ import annotations

import asyncio
from typing import Any

import discord


_control_cleanup_tasks: dict[int, asyncio.Task] = {}
_control_cleanup_keep_ids: dict[int, int] = {}
_control_cleanup_locks: dict[int, asyncio.Lock] = {}
_control_cleanup_semaphore = asyncio.Semaphore(1)


def _cleanup_channel_key(channel: Any) -> int:
    return int(getattr(channel, "id", 0) or id(channel))


async def _prune_duplicate_control_panels(
    channel: Any,
    keep_message_id: int,
    *,
    max_deletes: int = 5,
) -> None:
    """Delete obsolete interactive TVRS copies without touching result cards."""

    history = getattr(channel, "history", None)
    if history is None:
        return
    channel_key = _cleanup_channel_key(channel)
    selected_keep = int(keep_message_id)
    # Publish the newest canonical id before the first await. Two cleanup
    # generations therefore cannot delete each other's panel.
    _control_cleanup_keep_ids[channel_key] = selected_keep
    lock = _control_cleanup_locks.setdefault(channel_key, asyncio.Lock())
    async with lock, _control_cleanup_semaphore:
        if _control_cleanup_keep_ids.get(channel_key) != selected_keep:
            return
        deleted = 0
        try:
            async for candidate in history(limit=30):
                if _control_cleanup_keep_ids.get(channel_key) != selected_keep:
                    return
                if int(getattr(candidate, "id", 0) or 0) == selected_keep:
                    continue
                if getattr(getattr(candidate, "author", None), "bot", False) is not True:
                    continue
                if not getattr(candidate, "components", None):
                    continue
                embeds = getattr(candidate, "embeds", ())
                is_control = any(
                    str(
                        getattr(getattr(embed, "footer", None), "text", "") or ""
                    ).startswith("TVRS • результат зафиксирован • ref ")
                    or "пленарный консенсус"
                    in str(getattr(embed, "title", "") or "").lower()
                    for embed in embeds
                )
                if not is_control:
                    continue
                try:
                    await candidate.delete()
                    deleted += 1
                except discord.DiscordException:
                    continue
                if deleted >= max(1, int(max_deletes)):
                    return
        except (discord.DiscordException, AttributeError, TypeError, ValueError):
            return


def _schedule_control_cleanup(channel: Any, keep_message_id: int) -> None:
    channel_key = _cleanup_channel_key(channel)
    _control_cleanup_keep_ids[channel_key] = int(keep_message_id)
    existing = _control_cleanup_tasks.get(channel_key)
    if existing is not None and not existing.done():
        return

    async def runner() -> None:
        try:
            # Cleanup is maintenance, never part of the critical delivery path.
            await asyncio.sleep(2)
            canonical = _control_cleanup_keep_ids.get(channel_key)
            if canonical:
                await _prune_duplicate_control_panels(channel, canonical)
        finally:
            if _control_cleanup_tasks.get(channel_key) is asyncio.current_task():
                _control_cleanup_tasks.pop(channel_key, None)

    _control_cleanup_tasks[channel_key] = asyncio.create_task(
        runner(),
        name=f"tvrs-control-cleanup:{channel_key}",
    )


__all__ = [
    "_control_cleanup_tasks",
    "_prune_duplicate_control_panels",
    "_schedule_control_cleanup",
]
