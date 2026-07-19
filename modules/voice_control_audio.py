"""Deterministic audio conditioning shared by every voice-controlled module."""

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
_FRAME_SAMPLES = STT_SAMPLE_RATE // 50
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
    raw_peak: int = 0
    raw_rms: int = 0
    noise_floor: int = 0
    clipping_percent: float = 0.0


def _pcm16_samples(data: bytes) -> array:
    usable = len(data) - (len(data) % SAMPLE_WIDTH)
    samples = array("h")
    samples.frombytes(bytes(data[:usable]))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def apply_pcm_gain(pcm: bytes, gain: float) -> bytes:
    """Apply a calibrated gain without allowing integer overflow."""

    clean_gain = max(0.5, min(4.0, float(gain)))
    if abs(clean_gain - 1.0) < 0.03 or not pcm:
        return pcm
    samples = _pcm16_samples(pcm)
    conditioned = array(
        "h",
        (
            max(-32_768, min(32_767, round(int(sample) * clean_gain)))
            for sample in samples
        ),
    )
    if sys.byteorder == "big":
        conditioned.byteswap()
    return conditioned.tobytes()


def discord_pcm_to_mono_16k(pcm: bytes) -> array:
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


def _active_span(samples: array) -> tuple[int, int, int, int]:
    frame_rms = [
        _rms(samples[index : index + _FRAME_SAMPLES])
        for index in range(0, len(samples), _FRAME_SAMPLES)
    ]
    if not frame_rms:
        return 0, 0, 0, 0
    peak_rms = max(frame_rms)
    quiet_frames = sorted(value for value in frame_rms if value <= peak_rms * 0.22)
    noise_floor = (
        quiet_frames[min(len(quiet_frames) - 1, len(quiet_frames) // 2)]
        if len(quiet_frames) >= 2
        else 0
    )
    if peak_rms < 45:
        return 0, 0, 0, noise_floor
    noise_threshold = noise_floor * 2 + 30 if noise_floor else round(peak_rms * 0.2)
    threshold = max(45, min(round(peak_rms * 0.32), noise_threshold))
    active = [index for index, value in enumerate(frame_rms) if value >= threshold]
    if len(active) < 2:
        return 0, 0, 0, noise_floor
    first = max(0, active[0] - _EDGE_PADDING_FRAMES)
    last = min(len(frame_rms), active[-1] + _EDGE_PADDING_FRAMES + 1)
    return (
        first * _FRAME_SAMPLES,
        min(len(samples), last * _FRAME_SAMPLES),
        len(active),
        noise_floor,
    )


def _normalize(samples: array) -> tuple[array, int, int]:
    current_rms = _rms(samples)
    peak = max((abs(int(value)) for value in samples), default=0)
    if current_rms <= 0 or peak <= 0 or current_rms >= 900:
        return samples, peak, current_rms
    desired_gain = max(1.0, min(6.0, 2_200 / current_rms))
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
    input_duration_ms = round(
        len(pcm) / (DISCORD_SAMPLE_RATE * DISCORD_CHANNELS * SAMPLE_WIDTH) * 1000
    )
    mono = _center(discord_pcm_to_mono_16k(pcm))
    start, end, active_frames, noise_floor = _active_span(mono)
    if end <= start:
        return PreparedSpeech(
            b"", input_duration_ms, 0, 0, 0, 0, False, noise_floor=noise_floor
        )
    raw_speech = mono[start:end]
    raw_rms = _rms(raw_speech)
    raw_peak = max((abs(int(value)) for value in raw_speech), default=0)
    clipped = sum(1 for value in raw_speech if abs(int(value)) >= 32_000)
    clipping_percent = round(clipped / max(1, len(raw_speech)) * 100, 2)
    speech, peak, rms = _normalize(raw_speech)
    padded = array("h", [0]) * _WAV_PADDING_SAMPLES
    padded.extend(speech)
    padded.extend(array("h", [0]) * _WAV_PADDING_SAMPLES)
    return PreparedSpeech(
        wav=mono_16k_to_wav(padded),
        input_duration_ms=input_duration_ms,
        output_duration_ms=round(len(padded) / STT_SAMPLE_RATE * 1000),
        speech_duration_ms=active_frames * 20,
        peak=peak,
        rms=rms,
        has_speech=True,
        raw_peak=raw_peak,
        raw_rms=raw_rms,
        noise_floor=noise_floor,
        clipping_percent=clipping_percent,
    )


__all__ = [
    "DISCORD_CHANNELS",
    "DISCORD_SAMPLE_RATE",
    "PreparedSpeech",
    "SAMPLE_WIDTH",
    "STT_SAMPLE_RATE",
    "apply_pcm_gain",
    "discord_pcm_to_mono_16k",
    "discord_pcm_to_stt_wav",
    "mono_16k_to_wav",
    "prepare_discord_pcm_for_stt",
]
