"""Prioritized, multi-provider speech recognition orchestration."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from modules.music_audio import SpeechSegment
from modules.music_config import MUSIC_WAKE_TIMEOUT_SECONDS
from modules.music_domain import (
    VoiceCommand,
    is_wake_word_candidate,
    parse_voice_command,
    split_wake_word,
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


@dataclass(frozen=True, slots=True)
class _TranscriptIntent:
    text: str
    woke: bool
    remainder: str
    command: VoiceCommand | None

    @property
    def decisive(self) -> bool:
        return self.command is not None or (self.woke and not self.remainder)


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


def _analyze_transcript(text: str, *, armed: bool) -> _TranscriptIntent:
    woke, remainder = split_wake_word(text)
    command: VoiceCommand | None = None
    if woke and remainder:
        command = parse_voice_command(remainder)
    elif armed and not woke:
        command = parse_voice_command(text)
    return _TranscriptIntent(str(text or ""), woke, remainder, command)


def _fallback_models(transcriber: Any) -> tuple[str, ...]:
    configured = getattr(transcriber, "accuracy_models", None)
    if configured:
        return tuple(dict.fromkeys(str(model).strip() for model in configured if model))
    legacy = str(getattr(transcriber, "accuracy_model", "") or "").strip()
    return (legacy,) if legacy else ()


async def _transcribe(manager: Any, pcm: bytes, model: str | None = None) -> str:
    if model:
        return await asyncio.to_thread(
            manager.transcriber.transcribe_pcm,
            pcm,
            model=model,
        )
    return await asyncio.to_thread(manager.transcriber.transcribe_pcm, pcm)


async def _transcribe_named(manager: Any, pcm: bytes, model: str) -> tuple[str, str]:
    return model, await _transcribe(manager, pcm, model)


async def _accurate_transcript(
    manager: Any,
    pcm: bytes,
    models: tuple[str, ...],
    *,
    armed: bool,
) -> tuple[_TranscriptIntent | None, list[Exception]]:
    tasks = [
        asyncio.create_task(
            _transcribe_named(manager, pcm, model),
            name=f"music-stt:{model}",
        )
        for model in models
    ]
    errors: list[Exception] = []
    wake_result: _TranscriptIntent | None = None
    try:
        for completed in asyncio.as_completed(tasks):
            try:
                model, text = await completed
            except Exception as exc:
                errors.append(exc)
                continue
            intent = _analyze_transcript(text, armed=armed)
            if intent.command is not None or (intent.woke and not intent.remainder):
                log.info(
                    "Music STT fallback accepted: model=%s armed=%s action=%s wake=%s",
                    model[:100],
                    armed,
                    intent.command.action if intent.command else "wake",
                    intent.woke,
                )
                return intent, errors
            if intent.woke:
                wake_result = intent
        return wake_result, errors
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def process_speech_segment(
    manager: Any,
    session: Any,
    segment: SpeechSegment,
) -> bool:
    """Run fast STT first and race accurate models only for ambiguous speech."""

    was_armed = (
        segment.user_id in session.one_shot_voice_users
        or session.armed_until.get(segment.user_id, 0) >= time.monotonic()
    )
    primary_error: Exception | None = None
    try:
        primary_text = await _transcribe(manager, segment.pcm)
    except Exception as exc:
        primary_error = exc
        primary_text = ""
    primary = _analyze_transcript(primary_text, armed=was_armed)
    if primary.decisive:
        return await manager._handle_transcript(
            session,
            segment.user_id,
            primary.text,
        )

    fallback_needed = bool(
        primary_error
        or was_armed
        or primary.woke
        or is_wake_word_candidate(primary.text)
    )
    models = _fallback_models(manager.transcriber) if fallback_needed else ()
    if models:
        accurate, errors = await _accurate_transcript(
            manager,
            segment.pcm,
            models,
            armed=was_armed,
        )
        if accurate is not None:
            return await manager._handle_transcript(
                session,
                segment.user_id,
                accurate.text,
            )
        if primary_error is not None and len(errors) == len(models):
            raise primary_error
    elif primary_error is not None:
        raise primary_error

    if primary.woke:
        return await manager._handle_transcript(
            session,
            segment.user_id,
            primary.text,
        )
    if was_armed and segment.user_id in session.voice_users:
        session.armed_until[segment.user_id] = (
            time.monotonic() + MUSIC_WAKE_TIMEOUT_SECONDS
        )
        session.last_notice = (
            "Не расслышал команду — повторите после нисходящего сигнала."
        )
        await manager.play_listening_signal(session, failed=True)
        await manager.publish_status(session)
    return False


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
    "process_speech_segment",
    "speech_sweeper",
    "speech_worker",
]
