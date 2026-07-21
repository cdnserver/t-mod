"""Lifecycle and handler registry for the shared delivery worker."""

from __future__ import annotations

import asyncio
import json
import traceback
from typing import Any

from persistence import outbox_repository as storage
from modules.delivery_outbox import DeliveryHandler, OutboxDispatcher, StorageOutboxRepository
from modules.technical_log import log_technical_event


_dispatcher = OutboxDispatcher(StorageOutboxRepository())
_wakeup = asyncio.Event()
_worker_task: asyncio.Task[Any] | None = None
_DEFAULT_CONCURRENCY = 4
_MAX_CONCURRENCY = 16
_DELIVERIES_PER_SLOT = 25


def register_delivery_handler(topic: str, handler: DeliveryHandler) -> None:
    _dispatcher.register(topic, handler)


def wake_delivery_worker() -> None:
    _wakeup.set()


async def _report_dead_deliveries(bot: Any) -> None:
    rows = await asyncio.to_thread(storage.delivery_outbox_unreported_dead, 25)
    for row in rows:
        try:
            item_id = int(row["id"])
            try:
                payload = json.loads(str(row.get("payload_json") or "{}"))
                guild_id = int(payload.get("guild_id") or 0) if isinstance(payload, dict) else 0
            except (TypeError, ValueError, json.JSONDecodeError):
                guild_id = 0
            guild = bot.get_guild(guild_id) if guild_id else None
            reported = False
            if guild is not None:
                try:
                    reported = await log_technical_event(
                        bot,
                        guild,
                        title="Доставка события остановлена",
                        details=(
                            f"Outbox ID: `{item_id}`\n"
                            f"Тема: `{str(row.get('topic') or '')[:120]}`\n"
                            f"Попыток: `{int(row.get('attempts') or 0)}/{int(row.get('max_attempts') or 0)}`\n"
                            f"Ошибка: `{str(row.get('last_error') or 'unknown')[:700]}`\n"
                            "После устранения причины: `/tvrs_admin target:delivery action:retry identifier:ID`."
                        ),
                        dedupe_key=f"delivery-dead:{item_id}",
                        cooldown_seconds=300,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    traceback.print_exc()
            if not reported:
                # The container log is the durable fallback when the guild or its
                # technical channel is unavailable. Marking the row prevents an
                # old/poison guild from starving newer dead-letter alerts.
                print(
                    f"DEAD DELIVERY #{item_id} topic={row.get('topic')} error={row.get('last_error')}",
                    flush=True,
                )
            await asyncio.to_thread(storage.delivery_outbox_mark_dead_notified, item_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A malformed row must never prevent the rest of the batch from being
            # reported and acknowledged through the console fallback.
            traceback.print_exc()


async def _run_delivery_round(concurrency: int) -> int:
    async def run_slot() -> int:
        try:
            # Each slot keeps draining while the other slots may be waiting on
            # a slow destination.  This avoids a round barrier after only one
            # message without exceeding the configured handler concurrency.
            return await _dispatcher.run_once(limit=_DELIVERIES_PER_SLOT)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
            return 0

    results = await asyncio.gather(*(run_slot() for _ in range(concurrency)))
    return sum(results)


async def delivery_worker(
    bot: Any,
    *,
    idle_seconds: float = 30.0,
    concurrency: int | None = None,
) -> None:
    parallelism = max(
        1,
        min(
            _MAX_CONCURRENCY,
            _DEFAULT_CONCURRENCY if concurrency is None else int(concurrency),
        ),
    )
    while not bot.is_closed():
        if hasattr(bot, "is_ready") and not bot.is_ready():
            waiter = getattr(bot, "wait_until_ready", None)
            if waiter is not None:
                await waiter()
            if bot.is_closed():
                return
        # Clear before claiming: a wakeup racing the claim remains set and is
        # observed below, while all older wakeups are represented by database rows.
        _wakeup.clear()
        try:
            processed = await _run_delivery_round(parallelism)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
            processed = 0
        try:
            await _report_dead_deliveries(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Reporting must never be able to stop the delivery engine itself.
            traceback.print_exc()
        if processed:
            continue
        sleep_seconds = max(0.05, float(idle_seconds))
        try:
            next_due = await asyncio.to_thread(storage.delivery_outbox_next_due_delay)
        except Exception:
            traceback.print_exc()
            next_due = None
        if next_due is not None:
            if next_due <= 0:
                continue
            sleep_seconds = min(sleep_seconds, max(0.05, float(next_due)))
        try:
            await asyncio.wait_for(_wakeup.wait(), timeout=sleep_seconds)
        except asyncio.TimeoutError:
            pass


def ensure_delivery_worker(bot: Any) -> asyncio.Task[Any]:
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(delivery_worker(bot), name="delivery-outbox-worker")
    return _worker_task


def setup_delivery(bot: Any) -> None:
    @bot.listen("on_ready")
    async def delivery_on_ready() -> None:
        ensure_delivery_worker(bot)
        wake_delivery_worker()
