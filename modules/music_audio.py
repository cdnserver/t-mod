"""Low-level PCM and Discord voice-receive adapters for T-Mod Music.

This module contains no queue or playback policy. It translates Discord audio
packets into bounded per-user speech segments and provides small PCM sources.
"""

from __future__ import annotations

import math
import logging
import threading
import time
from array import array
from dataclasses import dataclass
from typing import Any

import discord

try:
    import davey
except ImportError:  # pragma: no cover - discord.py voice requires this in production.
    davey = None  # type: ignore[assignment]

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


log = logging.getLogger(__name__)


PCM_SAMPLE_RATE = 48_000
PCM_CHANNELS = 2
PCM_SAMPLE_WIDTH = 2
PCM_BYTES_PER_SECOND = PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_SAMPLE_WIDTH
DAVE_PLAINTEXT_RECHECK_SECONDS = 2.0


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
            self._decoders: dict[int, discord.opus.Decoder] = {}
            self._decode_drops: dict[tuple[int, str], int] = {}
            self._dave_plaintext_until: dict[int, float] = {}
            self._dave_recoveries: dict[int, int] = {}

        def wants_opus(self) -> bool:
            # The extension's shared PCM router stops completely on one bad
            # Opus packet. Decode per speaker here so a corrupt packet can be
            # isolated without losing the whole voice receiver.
            return True

        def _drop_packet(
            self,
            user_id: int,
            stage: str,
            error: object,
            *,
            reset_opus: bool = False,
        ) -> None:
            key = (int(user_id), str(stage))
            count = self._decode_drops.get(key, 0) + 1
            self._decode_drops[key] = count
            if reset_opus:
                self._decoders[user_id] = discord.opus.Decoder()
            if count == 1 or count % 25 == 0:
                log.warning(
                    "Dropped unreadable Discord voice packet: "
                    "guild=%s user=%s stage=%s count=%s error=%s",
                    self.guild_id,
                    user_id,
                    stage,
                    count,
                    error,
                )

        @staticmethod
        def _safe_error(error: object) -> str:
            text = str(error).replace("\n", " ").replace("\r", " ").strip()
            return (text or type(error).__name__)[:300]

        def _dave_context(self, user_id: int) -> tuple[int, Any | None, bool]:
            voice_client = getattr(self, "voice_client", None)
            connection = getattr(voice_client, "_connection", None)
            protocol_version = int(getattr(connection, "dave_protocol_version", 0) or 0)
            session = getattr(connection, "dave_session", None)
            ready = bool(session is not None and getattr(session, "ready", False))
            can_passthrough = False
            if ready:
                checker = getattr(session, "can_passthrough", None)
                if callable(checker):
                    try:
                        can_passthrough = bool(checker(int(user_id)))
                    except Exception:
                        can_passthrough = False
            should_decrypt = ready and (protocol_version > 0 or can_passthrough)
            return protocol_version, session, should_decrypt

        def _decrypt_dave(self, user_id: int, opus: bytes, session: Any) -> bytes:
            if davey is None:
                raise RuntimeError("davey is unavailable")
            try:
                decrypted = session.decrypt(
                    int(user_id),
                    davey.MediaType.audio,
                    opus,
                )
            except Exception as exc:
                raise ValueError(self._safe_error(exc)) from exc
            if not decrypted:
                raise ValueError("DAVE returned an empty payload")
            return bytes(decrypted)

        def _decode_opus(self, user_id: int, opus: bytes) -> bytes:
            decoder = self._decoders.get(user_id)
            if decoder is None:
                decoder = discord.opus.Decoder()
                self._decoders[user_id] = decoder
            return bytes(decoder.decode(opus, fec=False))

        def _probe_plaintext_opus(self, user_id: int, opus: bytes) -> bytes | None:
            """Accept a DAVE transition packet only if libopus validates it."""

            decoder = discord.opus.Decoder()
            try:
                pcm = decoder.decode(opus, fec=False)
            except discord.opus.OpusError:
                return None
            self._decoders[user_id] = decoder
            self._dave_plaintext_until[user_id] = (
                time.monotonic() + DAVE_PLAINTEXT_RECHECK_SECONDS
            )
            return bytes(pcm)

        def _note_dave_recovery(self, user_id: int, error: object) -> None:
            count = self._dave_recoveries.get(user_id, 0) + 1
            self._dave_recoveries[user_id] = count
            if count == 1 or count % 250 == 0:
                log.warning(
                    "Recovered a valid plaintext Opus packet during a DAVE "
                    "transition: guild=%s user=%s count=%s dave_error=%s",
                    self.guild_id,
                    user_id,
                    count,
                    self._safe_error(error),
                )

        def write(self, user, data) -> None:
            if user is None or getattr(user, "bot", False):
                return
            opus = getattr(data, "opus", None)
            if not opus:
                return
            user_id = int(user.id)
            opus = bytes(opus)

            # During a DAVE transition Discord can temporarily send valid
            # plaintext Opus even while the negotiated protocol is still
            # reported as active. Keep a tiny validated passthrough window,
            # but immediately retry DAVE if the stream becomes encrypted.
            if self._dave_plaintext_until.get(user_id, 0) > time.monotonic():
                try:
                    pcm = self._decode_opus(user_id, opus)
                except discord.opus.OpusError:
                    self._dave_plaintext_until.pop(user_id, None)
                    self._decoders.pop(user_id, None)
                else:
                    self.manager.accept_voice_packet(
                        self.guild_id,
                        user_id,
                        pcm,
                    )
                    return

            protocol_version, session, should_decrypt = self._dave_context(user_id)
            if should_decrypt:
                try:
                    opus = self._decrypt_dave(user_id, opus, session)
                except Exception as exc:
                    # Never pass arbitrary encrypted bytes to STT. A fresh
                    # libopus decoder must first prove this is a valid
                    # plaintext transition packet.
                    pcm = self._probe_plaintext_opus(user_id, opus)
                    if pcm is None:
                        self._decoders.pop(user_id, None)
                        self._drop_packet(
                            user_id,
                            "dave",
                            self._safe_error(exc),
                        )
                        return
                    self._note_dave_recovery(user_id, exc)
                    self.manager.accept_voice_packet(
                        self.guild_id,
                        user_id,
                        pcm,
                    )
                    return
            elif protocol_version > 0:
                pcm = self._probe_plaintext_opus(user_id, opus)
                if pcm is None:
                    self._drop_packet(
                        user_id,
                        "dave",
                        "DAVE session is not ready and packet is not plaintext Opus",
                    )
                    return
                self._note_dave_recovery(user_id, "DAVE session is not ready")
                self.manager.accept_voice_packet(
                    self.guild_id,
                    user_id,
                    pcm,
                )
                return

            try:
                pcm = self._decode_opus(user_id, opus)
            except discord.opus.OpusError as exc:
                self._drop_packet(
                    user_id,
                    "opus",
                    exc,
                    reset_opus=True,
                )
                return
            self.manager.accept_voice_packet(
                self.guild_id,
                user_id,
                pcm,
            )

        def cleanup(self) -> None:
            self._decoders.clear()
            self._decode_drops.clear()
            self._dave_plaintext_until.clear()
            self._dave_recoveries.clear()

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
