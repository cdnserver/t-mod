"""Low-level PCM and Discord voice-receive adapters for T-Mod Music.

This module contains no queue or playback policy. It translates Discord audio
packets into bounded per-user speech segments and provides small PCM sources.
"""

from __future__ import annotations

import math
import threading
import time
from array import array
from dataclasses import dataclass
from typing import Any

import discord

from modules.music_config import (
    MUSIC_SIGNAL_VOLUME,
    MUSIC_SPEECH_MAX_SECONDS,
    MUSIC_SPEECH_MIN_SECONDS,
    MUSIC_SPEECH_SILENCE_SECONDS,
)

try:
    from discord.ext import voice_recv
except ImportError:  # pragma: no cover - deployment diagnostics cover this path.
    voice_recv = None  # type: ignore[assignment]


PCM_SAMPLE_RATE = 48_000
PCM_CHANNELS = 2
PCM_SAMPLE_WIDTH = 2
PCM_BYTES_PER_SECOND = PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_SAMPLE_WIDTH


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    user_id: int
    pcm: bytes


@dataclass(slots=True)
class _SpeechBuffer:
    data: bytearray
    started_at: float
    last_packet_at: float


class SpeechSegmenter:
    """Thread-safe segmentation of per-user decoded PCM packet streams."""

    def __init__(self) -> None:
        self._buffers: dict[int, _SpeechBuffer] = {}
        self._lock = threading.Lock()
        self._minimum_bytes = int(PCM_BYTES_PER_SECOND * MUSIC_SPEECH_MIN_SECONDS)
        self._maximum_bytes = int(PCM_BYTES_PER_SECOND * MUSIC_SPEECH_MAX_SECONDS)

    def _finish_locked(self, user_id: int) -> SpeechSegment | None:
        state = self._buffers.pop(int(user_id), None)
        if state is None or len(state.data) < self._minimum_bytes:
            return None
        return SpeechSegment(int(user_id), bytes(state.data))

    def accept(
        self,
        user_id: int,
        pcm: bytes,
        *,
        now: float | None = None,
    ) -> list[SpeechSegment]:
        if not pcm:
            return []
        timestamp = time.monotonic() if now is None else float(now)
        completed: list[SpeechSegment] = []
        with self._lock:
            state = self._buffers.get(int(user_id))
            if (
                state is not None
                and timestamp - state.last_packet_at >= MUSIC_SPEECH_SILENCE_SECONDS
            ):
                segment = self._finish_locked(int(user_id))
                if segment is not None:
                    completed.append(segment)
                state = None
            if state is None:
                state = _SpeechBuffer(bytearray(), timestamp, timestamp)
                self._buffers[int(user_id)] = state
            state.data.extend(pcm)
            state.last_packet_at = timestamp
            if len(state.data) >= self._maximum_bytes:
                segment = self._finish_locked(int(user_id))
                if segment is not None:
                    completed.append(segment)
        return completed

    def drain_ready(self, *, now: float | None = None) -> list[SpeechSegment]:
        timestamp = time.monotonic() if now is None else float(now)
        completed: list[SpeechSegment] = []
        with self._lock:
            ready = [
                user_id
                for user_id, state in self._buffers.items()
                if timestamp - state.last_packet_at >= MUSIC_SPEECH_SILENCE_SECONDS
            ]
            for user_id in ready:
                segment = self._finish_locked(user_id)
                if segment is not None:
                    completed.append(segment)
        return completed

    def clear(self) -> None:
        with self._lock:
            self._buffers.clear()

    def discard(self, user_id: int) -> None:
        """Forget an unfinished utterance as soon as that user opts out."""

        with self._lock:
            self._buffers.pop(int(user_id), None)


def listening_tone_pcm(
    frequency: float = 880.0,
    *,
    duration: float = 0.18,
    volume: float = MUSIC_SIGNAL_VOLUME,
) -> bytes:
    frames = max(1, int(PCM_SAMPLE_RATE * duration))
    fade_frames = max(1, int(PCM_SAMPLE_RATE * min(0.025, duration / 3)))
    samples = array("h")
    amplitude = int(32_767 * max(0.0, min(1.0, volume)))
    for index in range(frames):
        envelope = 1.0
        if index < fade_frames:
            envelope = index / fade_frames
        elif index > frames - fade_frames:
            envelope = max(0.0, (frames - index) / fade_frames)
        value = int(
            amplitude
            * envelope
            * math.sin(2.0 * math.pi * frequency * index / PCM_SAMPLE_RATE)
        )
        samples.extend((value, value))
    return samples.tobytes()


class PCMBytesSource(discord.AudioSource):
    def __init__(self, data: bytes) -> None:
        self._data = memoryview(bytes(data))
        self._offset = 0
        self._frame_bytes = int(PCM_BYTES_PER_SECOND * 0.02)

    def read(self) -> bytes:
        if self._offset >= len(self._data):
            return b""
        chunk = self._data[self._offset : self._offset + self._frame_bytes].tobytes()
        self._offset += len(chunk)
        if len(chunk) < self._frame_bytes:
            chunk += b"\x00" * (self._frame_bytes - len(chunk))
        return chunk

    def is_opus(self) -> bool:
        return False


class SignalMixerSource(discord.AudioSource):
    """PCM source wrapper that can overlay a short wake acknowledgement."""

    def __init__(self, source: discord.PCMVolumeTransformer) -> None:
        self.source = source
        self._signal = memoryview(b"")
        self._signal_offset = 0
        self._signal_lock = threading.Lock()

    @property
    def volume(self) -> float:
        return float(self.source.volume)

    @volume.setter
    def volume(self, value: float) -> None:
        self.source.volume = max(0.0, min(1.0, float(value)))

    def trigger_signal(self) -> None:
        signal = listening_tone_pcm()
        with self._signal_lock:
            self._signal = memoryview(signal)
            self._signal_offset = 0

    def read(self) -> bytes:
        base = self.source.read()
        if not base:
            return b""
        with self._signal_lock:
            if self._signal_offset >= len(self._signal):
                return base
            overlay = self._signal[
                self._signal_offset : self._signal_offset + len(base)
            ].tobytes()
            self._signal_offset += len(overlay)
        if len(overlay) < len(base):
            overlay += b"\x00" * (len(base) - len(overlay))
        base_samples = array("h")
        signal_samples = array("h")
        base_samples.frombytes(base)
        signal_samples.frombytes(overlay)
        mixed = array(
            "h",
            (
                max(-32_768, min(32_767, left + right))
                for left, right in zip(base_samples, signal_samples, strict=False)
            ),
        )
        return mixed.tobytes()

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self.source.cleanup()


if voice_recv is not None:

    class TModVoiceSink(voice_recv.AudioSink):  # type: ignore[misc]
        def __init__(self, manager: Any, guild_id: int) -> None:
            super().__init__()
            self.manager = manager
            self.guild_id = int(guild_id)

        def wants_opus(self) -> bool:
            return False

        def write(self, user, data) -> None:
            if user is None or getattr(user, "bot", False):
                return
            pcm = getattr(data, "pcm", None)
            if pcm:
                self.manager.accept_voice_packet(
                    self.guild_id,
                    int(user.id),
                    bytes(pcm),
                )

        def cleanup(self) -> None:
            return None

else:

    class TModVoiceSink:  # pragma: no cover - constructed only with dependency.
        pass


__all__ = [
    "PCMBytesSource",
    "SignalMixerSource",
    "SpeechSegment",
    "SpeechSegmenter",
    "TModVoiceSink",
    "listening_tone_pcm",
    "voice_recv",
]
