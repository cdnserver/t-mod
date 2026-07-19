"""Latency-aware multi-provider speech-to-intent orchestration."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from modules.music_audio import SpeechSegment
from modules.music_config import (
    MUSIC_STT_HEDGE_DELAY_SECONDS,
    MUSIC_WAKE_TIMEOUT_SECONDS,
)
from modules.music_domain import (
    VoiceCommand,
    is_wake_word_candidate,
    parse_voice_command,
    split_wake_word,
)


log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _TranscriptIntent:
    text: str
    woke: bool
    remainder: str
    command: VoiceCommand | None

    @property
    def decisive(self) -> bool:
        return self.command is not None or (self.woke and not self.remainder)


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


async def _transcribe_primary(manager: Any, pcm: bytes) -> tuple[str, str]:
    return "primary", await _transcribe(manager, pcm)


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
            if intent.decisive:
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


async def _hedged_command_transcript(
    manager: Any,
    pcm: bytes,
    models: tuple[str, ...],
) -> tuple[_TranscriptIntent | None, list[Exception]]:
    """Start accuracy providers if the primary command misses its latency budget."""

    primary_task = asyncio.create_task(
        _transcribe_primary(manager, pcm),
        name="music-stt:primary",
    )
    tasks: list[asyncio.Task[tuple[str, str]]] = [primary_task]
    errors: list[Exception] = []
    wake_result: _TranscriptIntent | None = None
    try:
        done, _ = await asyncio.wait(
            {primary_task},
            timeout=MUSIC_STT_HEDGE_DELAY_SECONDS,
        )
        if done:
            try:
                _, text = primary_task.result()
            except Exception as exc:
                errors.append(exc)
            else:
                intent = _analyze_transcript(text, armed=True)
                if intent.decisive:
                    return intent, errors
                if intent.woke:
                    wake_result = intent

        fallback_tasks = [
            asyncio.create_task(
                _transcribe_named(manager, pcm, model),
                name=f"music-stt:{model}",
            )
            for model in models
        ]
        tasks.extend(fallback_tasks)
        pending = [task for task in tasks if not task.done()]
        for completed in asyncio.as_completed(pending):
            try:
                model, text = await completed
            except Exception as exc:
                errors.append(exc)
                continue
            intent = _analyze_transcript(text, armed=True)
            if intent.decisive:
                log.info(
                    "Music STT hedge accepted: model=%s action=%s wake=%s",
                    model[:100],
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
    """Use a fast primary path and hedge latency-sensitive active commands."""

    was_armed = (
        segment.user_id in session.one_shot_voice_users
        or session.armed_until.get(segment.user_id, 0) >= time.monotonic()
    )
    models = _fallback_models(manager.transcriber)
    if was_armed and models:
        hedged, errors = await _hedged_command_transcript(
            manager,
            segment.pcm,
            models,
        )
        if hedged is not None:
            return await manager._handle_transcript(
                session,
                segment.user_id,
                hedged.text,
            )
        if len(errors) == len(models) + 1:
            raise errors[0]
        primary_error = None
        primary = _analyze_transcript("", armed=True)
    else:
        primary_error = None
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
    models = models if fallback_needed and not was_armed else ()
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


__all__ = ["process_speech_segment"]
