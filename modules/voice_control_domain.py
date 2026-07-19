"""Dependency-free wake-word, intent and microphone-quality rules."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable


_WAKE_RE = re.compile(
    r"(?:^|\s)(?:"
    r"т[иые]?\s*[-–—]?\s*мо[дт]|ти\s+мод|t\s*[-–—]?\s*mod|teamod|"
    r"сборщик(?:а)?\s+риса|банан"
    r")(?=\s|$)",
    re.IGNORECASE,
)
_WAKE_CANDIDATES = ("тмод", "тимод", "банан", "сборщикриса")
_WAKE_FALSE_POSITIVES = frozenset({"банк", "банка", "банки", "баран"})
_WAKE_ASR_CANDIDATES = frozenset({"тима", "тимад", "димод", "бонан"})
_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^0-9a-zа-яё%\s-]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class VoiceDomainSpec:
    """A reusable command domain registered by a feature such as music."""

    name: str
    parser: Callable[[str], Any | None]
    fast_actions: frozenset[str]
    cautious_actions: frozenset[str]


@dataclass(frozen=True, slots=True)
class TranscriptIntent:
    text: str
    woke: bool
    remainder: str
    command: Any | None

    @property
    def action(self) -> str | None:
        value = getattr(self.command, "action", None)
        return str(value) if value else None

    @property
    def decisive(self) -> bool:
        return self.command is not None or (self.woke and not self.remainder)


@dataclass(frozen=True, slots=True)
class LocalTranscript:
    text: str
    confidence: float
    latency_ms: int
    model: str


@dataclass(frozen=True, slots=True)
class LocalVoiceDecision:
    outcome: str  # accept, reject or fallback
    transcript: LocalTranscript | None
    intent: TranscriptIntent | None
    reason: str


@dataclass(frozen=True, slots=True)
class MicrophoneAssessment:
    quality_score: int
    quality_key: str
    quality_label: str
    raw_rms: int
    peak: int
    noise_floor: int
    snr_db: float
    clipping_percent: float
    recommended_gain: float
    summary: str


def normalize_speech(value: str) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip(" -")


def split_wake_word(value: str) -> tuple[bool, str]:
    normalized = normalize_speech(value)
    match = _WAKE_RE.search(normalized)
    if match is None:
        return False, normalized
    remainder = f"{normalized[: match.start()]} {normalized[match.end() :]}"
    return True, _SPACE_RE.sub(" ", remainder).strip(" -")


def is_wake_word_candidate(value: str) -> bool:
    """Identify a likely ASR typo without turning it into a real wake word."""

    normalized = normalize_speech(value)
    if not normalized or split_wake_word(normalized)[0]:
        return False
    tokens = normalized.split()
    if tokens and tokens[0] in _WAKE_FALSE_POSITIVES:
        return False
    if tokens and tokens[0] in _WAKE_ASR_CANDIDATES:
        return True
    for alias in _WAKE_CANDIDATES:
        word_count = 2 if alias == "сборщикриса" else 1
        candidate = "".join(tokens[:word_count])
        if abs(len(candidate) - len(alias)) > max(1, len(alias) // 5):
            continue
        threshold = 0.78 if len(alias) > 5 else 0.79
        if SequenceMatcher(None, candidate, alias).ratio() >= threshold:
            return True
    return False


def analyze_transcript(
    text: str,
    *,
    armed: bool,
    domain: VoiceDomainSpec,
) -> TranscriptIntent:
    woke, remainder = split_wake_word(text)
    command = None
    if woke and remainder:
        command = domain.parser(remainder)
    elif armed and not woke:
        command = domain.parser(text)
    return TranscriptIntent(str(text or ""), woke, remainder, command)


def decide_local_transcript(
    transcript: LocalTranscript | None,
    *,
    armed: bool,
    domain: VoiceDomainSpec,
    fast_confidence: float,
    cautious_confidence: float,
    reject_confidence: float,
) -> LocalVoiceDecision:
    if transcript is None or not transcript.text:
        return LocalVoiceDecision("fallback", transcript, None, "empty")
    intent = analyze_transcript(transcript.text, armed=armed, domain=domain)
    if intent.woke and not intent.remainder:
        if transcript.confidence >= fast_confidence:
            return LocalVoiceDecision("accept", transcript, intent, "wake")
        return LocalVoiceDecision("fallback", transcript, intent, "weak_wake")
    if intent.command is not None:
        action = intent.action or ""
        threshold = (
            cautious_confidence
            if action in domain.cautious_actions
            else fast_confidence
        )
        if action in domain.fast_actions and transcript.confidence >= threshold:
            return LocalVoiceDecision("accept", transcript, intent, "fast_command")
        return LocalVoiceDecision("fallback", transcript, intent, "complex_command")
    if (
        not armed
        and not intent.woke
        and not is_wake_word_candidate(transcript.text)
        and transcript.confidence >= reject_confidence
    ):
        return LocalVoiceDecision("reject", transcript, intent, "background_speech")
    return LocalVoiceDecision("fallback", transcript, intent, "ambiguous")


def assess_microphone(prepared: Any) -> MicrophoneAssessment:
    """Turn signal measurements into stable user-facing calibration advice."""

    raw_rms = max(0, int(getattr(prepared, "raw_rms", 0) or 0))
    peak = max(0, int(getattr(prepared, "raw_peak", 0) or 0))
    noise = max(0, int(getattr(prepared, "noise_floor", 0) or 0))
    clipping = max(0.0, float(getattr(prepared, "clipping_percent", 0.0) or 0.0))
    has_speech = bool(getattr(prepared, "has_speech", False))
    if not has_speech or raw_rms <= 0:
        return MicrophoneAssessment(
            0,
            "silent",
            "Речь не обнаружена",
            raw_rms,
            peak,
            noise,
            0.0,
            clipping,
            1.0,
            "T-Mod не нашёл устойчивую речь. Говорите ближе к микрофону после сигнала.",
        )
    snr_db = max(0.0, 20.0 * math.log10((raw_rms + 1) / (noise + 1)))
    level_score = min(40.0, max(0.0, raw_rms / 1_200 * 40.0))
    snr_score = min(45.0, snr_db / 24.0 * 45.0)
    clipping_penalty = min(30.0, clipping * 2.0)
    score = round(max(1.0, min(100.0, 15.0 + level_score + snr_score - clipping_penalty)))
    if snr_db < 6.0:
        recommended_gain = 1.0
    else:
        recommended_gain = max(0.75, min(4.0, 1_600 / max(1, raw_rms)))
    recommended_gain = round(recommended_gain, 2)
    if score >= 85:
        key, label = "excellent", "Отличный сигнал"
        summary = "Голос чистый и достаточно громкий; дополнительная настройка не требуется."
    elif score >= 68:
        key, label = "good", "Хороший сигнал"
        summary = "Распознавание должно работать стабильно даже для коротких команд."
    elif score >= 48:
        key, label = "fair", "Сигнал можно улучшить"
        summary = "Калибровка применит безопасное усиление; уменьшите фоновый шум."
    else:
        key, label = "poor", "Слабый сигнал"
        summary = "Подойдите ближе к микрофону и отключите шумные источники перед повторной проверкой."
    return MicrophoneAssessment(
        score,
        key,
        label,
        raw_rms,
        peak,
        noise,
        round(snr_db, 1),
        round(clipping, 2),
        recommended_gain,
        summary,
    )


__all__ = [
    "LocalTranscript",
    "LocalVoiceDecision",
    "MicrophoneAssessment",
    "TranscriptIntent",
    "VoiceDomainSpec",
    "analyze_transcript",
    "assess_microphone",
    "decide_local_transcript",
    "is_wake_word_candidate",
    "normalize_speech",
    "split_wake_word",
]
