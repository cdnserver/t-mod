"""Durable at-least-once delivery with lease fencing.

The dispatcher deliberately has no Discord dependency and exposes ``run_once``
so workers are testable without sleeps.  Handlers must be idempotent whenever
the destination supports it: an external service may accept a message just
before this process dies and therefore before the receipt can be committed.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Protocol

from persistence import outbox_repository as storage


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    id: int
    topic: str
    dedupe_key: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_token: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "OutboxMessage":
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("outbox_payload_invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("outbox_payload_must_be_object")
        token = str(row.get("lease_token") or "")
        if not token:
            raise ValueError("outbox_message_without_lease")
        return cls(
            id=int(row["id"]),
            topic=str(row["topic"]),
            dedupe_key=str(row["dedupe_key"]),
            payload=payload,
            attempts=int(row.get("attempts") or 0),
            max_attempts=int(row.get("max_attempts") or 1),
            lease_token=token,
        )


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    message_id: int | None = None


class OutboxRepository(Protocol):
    def claim(self, *, worker_id: str, limit: int, lease_seconds: int, now: datetime) -> list[OutboxMessage]: ...

    def renew(self, message: OutboxMessage, *, lease_seconds: int, now: datetime) -> bool: ...

    def delivered(self, message: OutboxMessage, receipt: DeliveryReceipt, *, now: datetime) -> bool: ...

    def failed(
        self,
        message: OutboxMessage,
        error: Exception,
        *,
        retry_at: datetime,
        now: datetime,
        permanent: bool = False,
    ) -> bool: ...


class StorageOutboxRepository:
    """Adapter around the compatibility-safe storage facade."""

    def claim(self, *, worker_id: str, limit: int, lease_seconds: int, now: datetime) -> list[OutboxMessage]:
        rows = storage.delivery_outbox_claim(
            worker_id=worker_id,
            limit=limit,
            lease_seconds=lease_seconds,
            now=_as_utc(now).isoformat(),
        )
        messages: list[OutboxMessage] = []
        for row in rows:
            try:
                messages.append(OutboxMessage.from_row(row))
            except ValueError as exc:
                storage.delivery_outbox_mark_failed(
                    int(row["id"]),
                    lease_token=str(row.get("lease_token") or ""),
                    error=f"{type(exc).__name__}: {exc}",
                    retry_at=_as_utc(now).isoformat(),
                    permanent=True,
                    now=_as_utc(now).isoformat(),
                )
        return messages

    def renew(self, message: OutboxMessage, *, lease_seconds: int, now: datetime) -> bool:
        return storage.delivery_outbox_renew_lease(
            message.id,
            lease_token=message.lease_token,
            lease_seconds=lease_seconds,
            now=_as_utc(now).isoformat(),
        )

    def delivered(self, message: OutboxMessage, receipt: DeliveryReceipt, *, now: datetime) -> bool:
        return storage.delivery_outbox_mark_delivered(
            message.id,
            lease_token=message.lease_token,
            message_id=receipt.message_id,
            now=_as_utc(now).isoformat(),
        )

    def failed(
        self,
        message: OutboxMessage,
        error: Exception,
        *,
        retry_at: datetime,
        now: datetime,
        permanent: bool = False,
    ) -> bool:
        return storage.delivery_outbox_mark_failed(
            message.id,
            lease_token=message.lease_token,
            error=f"{type(error).__name__}: {error}",
            retry_at=_as_utc(retry_at).isoformat(),
            permanent=permanent,
            now=_as_utc(now).isoformat(),
        )


DeliveryHandler = Callable[[OutboxMessage], Awaitable[DeliveryReceipt | int | None]]


class OutboxDispatcher:
    def __init__(
        self,
        repository: OutboxRepository,
        *,
        worker_id: str | None = None,
        clock: Callable[[], datetime] = utc_now,
        lease_seconds: int = 90,
        heartbeat_seconds: float | None = None,
        base_retry_seconds: int = 5,
        max_retry_seconds: int = 3600,
    ) -> None:
        self.repository = repository
        self.worker_id = worker_id or f"delivery-{uuid.uuid4().hex}"
        self.clock = clock
        self.lease_seconds = max(5, int(lease_seconds))
        self.heartbeat_seconds = (
            max(0.01, float(heartbeat_seconds))
            if heartbeat_seconds is not None
            else max(1.0, float(self.lease_seconds) / 3.0)
        )
        self.base_retry_seconds = max(1, int(base_retry_seconds))
        self.max_retry_seconds = max(self.base_retry_seconds, int(max_retry_seconds))
        self.handlers: dict[str, DeliveryHandler] = {}

    def register(self, topic: str, handler: DeliveryHandler) -> None:
        clean_topic = str(topic).strip()
        if not clean_topic:
            raise ValueError("outbox_topic_required")
        self.handlers[clean_topic] = handler

    def retry_delay(self, attempts: int) -> int:
        return min(self.max_retry_seconds, self.base_retry_seconds * (2 ** max(0, int(attempts) - 1)))

    async def _run_handler_with_heartbeat(
        self,
        message: OutboxMessage,
        handler: DeliveryHandler,
    ) -> DeliveryReceipt | int | None:
        """Keep ownership while a slow external request is in flight."""

        task = asyncio.create_task(handler(message), name=f"outbox-handler:{message.id}")
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=self.heartbeat_seconds)
                if task in done:
                    return await task
                renewed = await asyncio.to_thread(
                    self.repository.renew,
                    message,
                    lease_seconds=self.lease_seconds,
                    now=_as_utc(self.clock()),
                )
                if not renewed:
                    raise RuntimeError("outbox_lease_lost")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def run_once(self, *, limit: int = 25) -> int:
        processed = 0
        # Lease immediately before handling each item.  Claiming a large batch
        # and then processing it sequentially lets the tail expire before its
        # handler starts, so another worker could legitimately reclaim it.
        for _ in range(max(1, min(int(limit), 100))):
            claimed_at = _as_utc(self.clock())
            messages = await asyncio.to_thread(
                self.repository.claim,
                worker_id=self.worker_id,
                limit=1,
                lease_seconds=self.lease_seconds,
                now=claimed_at,
            )
            if not messages:
                break
            message = messages[0]
            processed += 1
            handler = self.handlers.get(message.topic)
            try:
                if handler is None:
                    raise LookupError(f"outbox_handler_not_registered:{message.topic}")
                raw_receipt = await self._run_handler_with_heartbeat(message, handler)
                receipt = (
                    raw_receipt
                    if isinstance(raw_receipt, DeliveryReceipt)
                    else DeliveryReceipt(message_id=int(raw_receipt) if raw_receipt else None)
                )
            except Exception as exc:
                failed_at = _as_utc(self.clock())
                retry_at = failed_at + timedelta(seconds=self.retry_delay(message.attempts))
                await asyncio.to_thread(
                    self.repository.failed,
                    message,
                    exc,
                    retry_at=retry_at,
                    now=failed_at,
                    permanent=handler is None,
                )
                continue
            await asyncio.to_thread(
                self.repository.delivered,
                message,
                receipt,
                now=_as_utc(self.clock()),
            )
        return processed
