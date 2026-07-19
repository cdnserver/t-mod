"""Optional low-latency local recognizer for wake words and short commands."""

from __future__ import annotations

import asyncio
import io
import logging
import math
import threading
import time
from typing import Any

from modules.voice_control_audio import prepare_discord_pcm_for_stt
from modules.voice_control_config import (
    VOICE_LOCAL_STT_COMPUTE_TYPE,
    VOICE_LOCAL_STT_CPU_THREADS,
    VOICE_LOCAL_STT_DEVICE,
    VOICE_LOCAL_STT_ENABLED,
    VOICE_LOCAL_STT_MODEL,
    VOICE_LOCAL_STT_WARMUP,
    VOICE_LOCAL_STT_WORKERS,
    VOICE_MODEL_CACHE_DIR,
)
from modules.voice_control_domain import LocalTranscript

try:
    from faster_whisper import WhisperModel
except ImportError:  # pragma: no cover - deployment diagnostics expose this state.
    WhisperModel = None  # type: ignore[assignment]


log = logging.getLogger(__name__)
_HOTWORDS = (
    "банан, т мод, сборщик риса, включи, пауза, продолжи, стоп, дальше, "
    "громче, тише, очередь, отключись"
)


class LocalVoiceRecognizer:
    """Lazy faster-whisper adapter that never blocks the cloud fallback."""

    def __init__(self) -> None:
        self.enabled = bool(VOICE_LOCAL_STT_ENABLED)
        self.model_name = VOICE_LOCAL_STT_MODEL
        self._model: Any | None = None
        self._load_lock = threading.Lock()
        self._transcribe_lock = threading.Lock()
        self._warmup_task: asyncio.Task[None] | None = None
        self._last_error: str | None = None
        self._retry_after = 0.0

    @property
    def installed(self) -> bool:
        return WhisperModel is not None

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def status(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.installed:
            return "not_installed"
        if self.ready:
            return "ready"
        if self._warmup_task is not None and not self._warmup_task.done():
            return "loading"
        if self._last_error:
            return "degraded"
        return "cold"

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def start_warmup(self, *, force: bool = False) -> None:
        if not force and not VOICE_LOCAL_STT_WARMUP:
            return
        if not self.enabled or not self.installed or self.ready:
            return
        if time.monotonic() < self._retry_after:
            return
        if self._warmup_task is None or self._warmup_task.done():
            self._warmup_task = asyncio.create_task(
                self._warmup(),
                name="voice-local-stt-warmup",
            )

    async def _warmup(self) -> None:
        try:
            await asyncio.to_thread(self._load)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            self._retry_after = time.monotonic() + 60.0
            log.warning("Local Voice Control model unavailable: %s", self._last_error)
        else:
            self._last_error = None
            log.info(
                "Local Voice Control ready: model=%s device=%s compute=%s",
                self.model_name,
                VOICE_LOCAL_STT_DEVICE,
                VOICE_LOCAL_STT_COMPUTE_TYPE,
            )

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if WhisperModel is None:
            raise RuntimeError("faster-whisper is not installed")
        with self._load_lock:
            if self._model is None:
                VOICE_MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                self._model = WhisperModel(
                    self.model_name,
                    device=VOICE_LOCAL_STT_DEVICE,
                    compute_type=VOICE_LOCAL_STT_COMPUTE_TYPE,
                    cpu_threads=VOICE_LOCAL_STT_CPU_THREADS,
                    num_workers=VOICE_LOCAL_STT_WORKERS,
                    download_root=str(VOICE_MODEL_CACHE_DIR),
                )
        return self._model

    async def transcribe_pcm(self, pcm: bytes) -> LocalTranscript | None:
        if not self.enabled or not self.installed:
            return None
        if not self.ready:
            # With eager warmup disabled, the first phrase starts the download
            # in the background and safely falls back to the cloud path. Later
            # phrases use the cached local model once it is ready.
            self.start_warmup(force=True)
            return None
        return await asyncio.to_thread(self._transcribe, pcm)

    def _transcribe(self, pcm: bytes) -> LocalTranscript | None:
        prepared = prepare_discord_pcm_for_stt(pcm)
        if not prepared.has_speech:
            return None
        model = self._load()
        started = time.monotonic()
        with self._transcribe_lock:
            segments, info = model.transcribe(
                io.BytesIO(prepared.wav),
                language="ru",
                beam_size=1,
                best_of=1,
                temperature=0,
                condition_on_previous_text=False,
                word_timestamps=False,
                vad_filter=False,
                hotwords=_HOTWORDS,
            )
            collected = list(segments)
        text = " ".join(str(segment.text or "").strip() for segment in collected).strip()
        if not text:
            return None
        confidences = [
            max(
                0.0,
                min(
                    1.0,
                    math.exp(float(getattr(segment, "avg_logprob", -2.0)))
                    * (1.0 - float(getattr(segment, "no_speech_prob", 0.0))),
                ),
            )
            for segment in collected
        ]
        acoustic = sum(confidences) / max(1, len(confidences))
        language_probability = float(getattr(info, "language_probability", 1.0) or 0.0)
        confidence = max(0.0, min(1.0, acoustic * (0.75 + 0.25 * language_probability)))
        return LocalTranscript(
            text=text,
            confidence=round(confidence, 3),
            latency_ms=round((time.monotonic() - started) * 1000),
            model=f"local/{self.model_name}",
        )


__all__ = ["LocalVoiceRecognizer"]
