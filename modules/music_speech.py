"""Speech packet ingestion, priority queue and worker lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from modules.music_audio import SpeechSegment
from modules.music_config import (
    MUSIC_SPEECH_COMMAND_SILENCE_SECONDS,
    MUSIC_STT_TIMEOUT_SECONDS,
)


log = logging.getLogger(__name__)
_BACKGROUND_MAX_AGE_SECONDS = 4.0
_URGENT_MAX_AGE_SECONDS = 12.0
_SPEECH_POLL_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class SpeechWorkItem:
    segment: SpeechSegment
    urgent: bool
    queued_at: float


class SpeechWorkQueue:
    """Bounded priority queue that preserves per-speaker ordering."""

    def __init__(self, maxsize: int) -> None:
        self.maxsize = max(1, int(maxsize))
        self._urgent: deque[SpeechWorkItem] = deque()
        self._background: deque[SpeechWorkItem] = deque()
        self._active_users: set[int] = set()
        self._available = asyncio.Event()
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._urgent) + len(self._background)

    @staticmethod
    def _expired(item: SpeechWorkItem, now: float) -> bool:
        limit = _URGENT_MAX_AGE_SECONDS if item.urgent else _BACKGROUND_MAX_AGE_SECONDS
        return now - item.queued_at > limit

    def _prune(self, now: float) -> None:
        for bucket in (self._urgent, self._background):
            kept = deque(item for item in bucket if not self._expired(item, now))
            self.dropped += len(bucket) - len(kept)
            bucket.clear()
            bucket.extend(kept)

    def offer(
        self,
        segment: SpeechSegment,
        *,
        urgent: bool,
        now: float | None = None,
    ) -> bool:
        timestamp = time.monotonic() if now is None else float(now)
        self._prune(timestamp)
        if len(self) >= self.maxsize:
            if urgent and self._background:
                self._background.popleft()
                self.dropped += 1
            else:
                self.dropped += 1
                return False
        item = SpeechWorkItem(segment, bool(urgent), timestamp)
        (self._urgent if urgent else self._background).append(item)
        self._available.set()
        return True

    def _take_available(self) -> SpeechWorkItem | None:
        for bucket in (self._urgent, self._background):
            for index, item in enumerate(bucket):
                user_id = int(item.segment.user_id)
                if user_id in self._active_users:
                    continue
                del bucket[index]
                self._active_users.add(user_id)
                return item
        return None

    async def get(self) -> SpeechWorkItem:
        while True:
            self._prune(time.monotonic())
            item = self._take_available()
            if item is not None:
                return item
            self._available.clear()
            item = self._take_available()
            if item is not None:
                return item
            await self._available.wait()

    def done(self, user_id: int) -> None:
        self._active_users.discard(int(user_id))
        self._available.set()

    def clear(self) -> None:
        self._urgent.clear()
        self._background.clear()
        self._active_users.clear()
        self._available.set()


def accept_voice_packet(manager: Any, guild_id: int, user_id: int, pcm: bytes) -> None:
    session = manager.get(guild_id)
    loop = manager._loop
    if session is None or loop is None or user_id not in session.voice_users:
        return
    now = time.monotonic()
    armed = bool(
        user_id in session.one_shot_voice_users
        or user_id in getattr(session, "diagnostic_users", ())
        or session.armed_until.get(user_id, 0) >= now
    )
    completed = session.segmenter.accept(
        user_id,
        pcm,
        now=now,
        silence_seconds=(MUSIC_SPEECH_COMMAND_SILENCE_SECONDS if armed else None),
    )
    for segment in completed:
        loop.call_soon_threadsafe(manager._enqueue_speech_segment, guild_id, segment)


def enqueue_speech_segment(manager: Any, guild_id: int, segment: SpeechSegment) -> None:
    session = manager.get(guild_id)
    if (
        session is None
        or segment.user_id not in session.voice_users
        or session.speech_queue is None
    ):
        return
    now = time.monotonic()
    urgent = bool(
        segment.user_id in session.one_shot_voice_users
        or segment.user_id in getattr(session, "diagnostic_users", ())
        or session.armed_until.get(segment.user_id, 0) >= now
    )
    if not session.speech_queue.offer(segment, urgent=urgent, now=now):
        return
    if urgent:
        session.armed_until[segment.user_id] = now + MUSIC_STT_TIMEOUT_SECONDS + 5


async def speech_worker(manager: Any, session: Any) -> None:
    try:
        while session.voice_users and session.speech_queue is not None:
            item = await session.speech_queue.get()
            segment = item.segment
            try:
                if segment.user_id not in session.voice_users:
                    continue
                age_ms = round((time.monotonic() - item.queued_at) * 1000)
                capture_ms = (
                    round((time.monotonic() - segment.ended_at) * 1000)
                    if segment.ended_at > 0
                    else age_ms
                )
                if capture_ms >= 1_000:
                    log.info(
                        "Music STT queue delay: guild=%s user=%s urgent=%s "
                        "queue_ms=%s capture_ms=%s",
                        session.guild_id,
                        segment.user_id,
                        item.urgent,
                        age_ms,
                        capture_ms,
                    )
                await manager._process_speech_segment(session, segment)
            except Exception as exc:
                session.last_error = f"Голосовое управление: {str(exc)[:500]}"
                await manager._release_one_shot_user(session, segment.user_id)
                await manager.publish_status(session)
            finally:
                if session.speech_queue is not None:
                    session.speech_queue.done(segment.user_id)
    except asyncio.CancelledError:
        return


async def speech_sweeper(manager: Any, session: Any) -> None:
    try:
        while session.voice_users:
            await asyncio.sleep(_SPEECH_POLL_SECONDS)
            for segment in session.segmenter.drain_ready():
                manager._enqueue_speech_segment(session.guild_id, segment)
            if await manager._expire_armed_commands(session, time.monotonic()):
                return
    except asyncio.CancelledError:
        return


__all__ = [
    "SpeechWorkItem",
    "SpeechWorkQueue",
    "accept_voice_packet",
    "enqueue_speech_segment",
    "speech_sweeper",
    "speech_worker",
]
