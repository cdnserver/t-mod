"""Runtime for durable Atlas jobs with fenced workers and bounded retries."""

from __future__ import annotations

import asyncio
import socket
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from persistence import atlas_job_repository as jobs


AtlasJobProgress = Callable[[dict[str, Any]], Awaitable[None]]
AtlasJobHandler = Callable[[dict[str, Any], AtlasJobProgress], Awaitable[dict[str, Any] | None]]
AtlasJobErrorHandler = Callable[[dict[str, Any], BaseException], Awaitable[None]]


class AtlasJobWorker:
    def __init__(
        self,
        *,
        worker_id: str | None = None,
        concurrency: int = 2,
        lease_seconds: int = 120,
        poll_seconds: float = 1.0,
        on_error: AtlasJobErrorHandler | None = None,
    ) -> None:
        self.worker_id = str(
            worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:10]}"
        )[:120]
        self.concurrency = max(1, min(8, int(concurrency)))
        self.lease_seconds = max(30, int(lease_seconds))
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.on_error = on_error
        self._handlers: dict[str, AtlasJobHandler] = {}
        self._runner: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()

    def register(self, job_type: str, handler: AtlasJobHandler) -> None:
        clean_type = str(job_type or "").strip().lower()
        if not clean_type or clean_type in self._handlers:
            raise ValueError("atlas_job_handler_duplicate")
        self._handlers[clean_type] = handler

    def start(self) -> None:
        if self._runner is not None and not self._runner.done():
            return
        if not self._handlers:
            raise ValueError("atlas_job_handlers_required")
        self._stopping.clear()
        self._runner = asyncio.create_task(self._run(), name="atlas-job-worker")

    def wake(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        self._stopping.set()
        self._wake.set()
        if self._runner is not None:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        self._runner = None

    async def run_once(self) -> int:
        if not self._handlers:
            return 0
        claimed = await asyncio.to_thread(
            jobs.atlas_job_claim,
            worker_id=self.worker_id,
            job_types=tuple(self._handlers),
            limit=self.concurrency,
            lease_seconds=self.lease_seconds,
        )
        if not claimed:
            return 0
        await asyncio.gather(*(self._execute(item) for item in claimed))
        return len(claimed)

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                processed = 0
            if processed:
                await asyncio.sleep(0)
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    async def _execute(self, job: dict[str, Any]) -> None:
        handler = self._handlers.get(str(job.get("job_type") or ""))
        if handler is None:
            return
        job_id = int(job["id"])
        token = str(job["lease_token"])
        lease_lost = asyncio.Event()

        async def heartbeat() -> None:
            interval = max(10, self.lease_seconds // 3)
            while not lease_lost.is_set():
                await asyncio.sleep(interval)
                renewed = await asyncio.to_thread(
                    jobs.atlas_job_renew,
                    job_id,
                    lease_token=token,
                    lease_seconds=self.lease_seconds,
                )
                if not renewed:
                    lease_lost.set()
                    return

        async def report(progress: dict[str, Any]) -> None:
            if lease_lost.is_set():
                raise RuntimeError("atlas_job_lease_lost")
            updated = await asyncio.to_thread(
                jobs.atlas_job_progress,
                job_id,
                lease_token=token,
                progress=dict(progress or {}),
            )
            if not updated:
                lease_lost.set()
                raise RuntimeError("atlas_job_lease_lost")

        heartbeat_task = asyncio.create_task(
            heartbeat(), name=f"atlas-job-heartbeat-{job_id}"
        )
        try:
            result = await handler(job, report)
            if lease_lost.is_set():
                return
            await asyncio.to_thread(
                jobs.atlas_job_succeed,
                job_id,
                lease_token=token,
                result=dict(result or {}),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retryable = bool(getattr(exc, "retryable", True))
            delay = min(300, 5 * (2 ** max(0, int(job.get("attempts") or 1) - 1)))
            await asyncio.to_thread(
                jobs.atlas_job_fail,
                job_id,
                lease_token=token,
                error=f"{type(exc).__name__}: {exc}",
                retry_delay_seconds=delay,
                retryable=retryable,
            )
            if self.on_error is not None:
                await self.on_error(job, exc)
        finally:
            lease_lost.set()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)


__all__ = ["AtlasJobWorker"]
