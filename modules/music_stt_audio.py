"""Deterministic, dependency-free audio preparation for speech recognition."""

from __future__ import annotations

import io
import math
import sys
import wave
from array import array
from dataclasses import dataclass


DISCORD_SAMPLE_RATE = 48_000
DISCORD_CHANNELS = 2
SAMPLE_WIDTH = 2
STT_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = STT_SAMPLE_RATE // 50  # 20 ms
_EDGE_PADDING_FRAMES = 5
_WAV_PADDING_SAMPLES = STT_SAMPLE_RATE * 80 // 1000


@dataclass(frozen=True, slots=True)
class PreparedSpeech:
    wav: bytes
    input_duration_ms: int
    output_duration_ms: int
    speech_duration_ms: int
    peak: int
    rms: int
    has_speech: bool


def _pcm16_samples(data: bytes) -> array:
    usable = len(data) - (len(data) % SAMPLE_WIDTH)
    samples = array("h")
    samples.frombytes(bytes(data[:usable]))
    if sys.byteorder == "big":  # Discord PCM and WAV PCM are little-endian.
        samples.byteswap()
    return samples


def discord_pcm_to_mono_16k(pcm: bytes) -> array:
    """Downmix 48 kHz stereo PCM and decimate it to 16 kHz mono."""

    samples = _pcm16_samples(pcm)
    usable = len(samples) - (len(samples) % 6)
    return array(
        "h",
        (sum(samples[index : index + 6]) // 6 for index in range(0, usable, 6)),
    )


def mono_16k_to_wav(samples: array) -> bytes:
    payload = array("h", samples)
    if sys.byteorder == "big":
        payload.byteswap()
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(SAMPLE_WIDTH)
        target.setframerate(STT_SAMPLE_RATE)
        target.writeframes(payload.tobytes())
    return output.getvalue()


def discord_pcm_to_stt_wav(pcm: bytes) -> bytes:
    """Convert Discord PCM without gating or normalization."""

    return mono_16k_to_wav(discord_pcm_to_mono_16k(pcm))


def _rms(samples: array) -> int:
    if not samples:
        return 0
    return math.isqrt(sum(int(value) * int(value) for value in samples) // len(samples))


def _center(samples: array) -> array:
    if not samples:
        return array("h")
    offset = round(sum(samples) / len(samples))
    if abs(offset) < 8:
        return array("h", samples)
    return array(
        "h",
        (max(-32_768, min(32_767, int(value) - offset)) for value in samples),
    )


def _active_span(samples: array) -> tuple[int, int, int]:
    frame_rms = [
        _rms(samples[index : index + _FRAME_SAMPLES])
        for index in range(0, len(samples), _FRAME_SAMPLES)
    ]
    if not frame_rms:
        return 0, 0, 0
    peak_rms = max(frame_rms)
    if peak_rms < 80:
        return 0, 0, 0
    ordered = sorted(frame_rms)
    noise_floor = ordered[min(len(ordered) - 1, len(ordered) // 5)]
    threshold = max(80, min(round(peak_rms * 0.35), noise_floor * 5 // 2 + 40))
    active = [index for index, value in enumerate(frame_rms) if value >= threshold]
    if len(active) < 3:
        return 0, 0, 0
    first = max(0, active[0] - _EDGE_PADDING_FRAMES)
    last = min(len(frame_rms), active[-1] + _EDGE_PADDING_FRAMES + 1)
    return first * _FRAME_SAMPLES, min(len(samples), last * _FRAME_SAMPLES), len(active)


def _normalize(samples: array) -> tuple[array, int, int]:
    current_rms = _rms(samples)
    peak = max((abs(int(value)) for value in samples), default=0)
    if current_rms <= 0 or peak <= 0:
        return samples, peak, current_rms
    # Normal microphone levels need no rewrite. Boost only genuinely quiet
    # speech, keeping the common path fast and preserving its waveform.
    if current_rms >= 700:
        return samples, peak, current_rms
    desired_gain = max(1.0, min(3.5, 1_800 / current_rms))
    headroom_gain = 30_000 / peak
    gain = min(desired_gain, headroom_gain)
    if gain <= 1.08:
        return samples, peak, current_rms
    normalized = array(
        "h",
        [max(-32_768, min(32_767, round(int(value) * gain))) for value in samples],
    )
    return normalized, round(peak * gain), round(current_rms * gain)


def prepare_discord_pcm_for_stt(pcm: bytes) -> PreparedSpeech:
    """Trim non-speech edges, normalize quiet speech and produce compact WAV."""

    input_duration_ms = round(
        len(pcm) / (DISCORD_SAMPLE_RATE * DISCORD_CHANNELS * SAMPLE_WIDTH) * 1000
    )
    mono = _center(discord_pcm_to_mono_16k(pcm))
    start, end, active_frames = _active_span(mono)
    if end <= start:
        return PreparedSpeech(b"", input_duration_ms, 0, 0, 0, 0, False)
    speech, peak, rms = _normalize(mono[start:end])
    padded = array("h", [0]) * _WAV_PADDING_SAMPLES
    padded.extend(speech)
    padded.extend(array("h", [0]) * _WAV_PADDING_SAMPLES)
    speech_duration_ms = active_frames * 20
    return PreparedSpeech(
        wav=mono_16k_to_wav(padded),
        input_duration_ms=input_duration_ms,
        output_duration_ms=round(len(padded) / STT_SAMPLE_RATE * 1000),
        speech_duration_ms=speech_duration_ms,
        peak=peak,
        rms=rms,
        has_speech=True,
    )


__all__ = [
    "PreparedSpeech",
    "discord_pcm_to_mono_16k",
    "discord_pcm_to_stt_wav",
    "mono_16k_to_wav",
    "prepare_discord_pcm_for_stt",
]
