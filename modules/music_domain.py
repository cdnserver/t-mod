"""Pure rules for music queries, queue presentation and Russian voice commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal
from urllib.parse import urlparse


MusicAction = Literal[
    "play",
    "pause",
    "resume",
    "skip",
    "stop",
    "disconnect",
    "queue",
    "now",
    "volume_up",
    "volume_down",
    "volume_set",
]

_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)
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
_LEADING_FILLERS = (
    "пожалуйста",
    "давай",
    "можешь",
    "можешь пожалуйста",
    "будь добр",
    "будь добра",
)
_RUSSIAN_VOLUME = {
    "пять": 5,
    "десять": 10,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
    "сто": 100,
}


class MusicInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MusicTrack:
    title: str
    webpage_url: str
    duration_seconds: int | None
    uploader: str | None = None
    thumbnail_url: str | None = None
    requested_by_id: int | None = None
    requested_by_display: str | None = None


@dataclass(frozen=True, slots=True)
class VoiceCommand:
    action: MusicAction
    query: str | None = None
    volume_percent: int | None = None


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
    """Identify a likely STT typo without ever treating it as a real wake word."""

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


def prepare_youtube_input(value: str) -> str:
    query = str(value or "").strip()
    if not query:
        raise MusicInputError("Укажите название композиции или ссылку YouTube.")
    if len(query) > 240:
        raise MusicInputError("Запрос слишком длинный: максимум 240 символов.")
    parsed = urlparse(query)
    if parsed.scheme or parsed.netloc:
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or host not in _YOUTUBE_HOSTS:
            raise MusicInputError(
                "Поддерживаются только ссылки youtube.com и youtu.be."
            )
        return query
    return f"ytsearch1:{query}"


def _after_prefix(text: str, prefixes: tuple[str, ...]) -> str | None:
    for prefix in prefixes:
        if text == prefix:
            return ""
        marker = f"{prefix} "
        if text.startswith(marker):
            return text[len(marker) :].strip()
    return None


def _strip_leading_fillers(text: str) -> str:
    current = text
    changed = True
    while changed:
        changed = False
        for filler in _LEADING_FILLERS:
            marker = f"{filler} "
            if current.startswith(marker):
                current = current[len(marker) :].strip()
                changed = True
                break
    return current


def parse_voice_command(value: str) -> VoiceCommand | None:
    text = _strip_leading_fillers(normalize_speech(value))
    if not text:
        return None

    if text in {
        "пауза",
        "паузу",
        "поуза",
        "приостанови",
        "приостановить",
        "поставь на паузу",
        "поставить на паузу",
        "замри",
    }:
        return VoiceCommand("pause")
    if text in {
        "продолжи",
        "продолжай",
        "продолжить",
        "продолжи музыку",
        "возобнови",
        "возобновить",
        "сними с паузы",
        "играй дальше",
        "играй",
        "включи музыку",
    }:
        return VoiceCommand("resume")
    if text in {
        "стоп",
        "останови",
        "остановить",
        "останови воспроизведение",
        "стоп музыка",
        "останови музыку",
        "выключи",
        "выключи музыку",
        "очисти очередь",
    }:
        return VoiceCommand("stop")
    if text in {
        "дальше",
        "следующая",
        "следующую",
        "следующий",
        "следущий",
        "следующий трек",
        "переключи",
        "переключить",
        "пропусти",
        "пропустить",
        "пропусти трек",
        "скип",
    }:
        return VoiceCommand("skip")
    if text in {
        "уйди",
        "отключись",
        "отключиться",
        "выйди из канала",
        "покинь канал",
    }:
        return VoiceCommand("disconnect")
    if text in {"очередь", "покажи очередь", "что в очереди"}:
        return VoiceCommand("queue")
    if text in {"что играет", "что сейчас играет", "текущий трек"}:
        return VoiceCommand("now")
    if text in {"громче", "сделай громче", "прибавь громкость"}:
        return VoiceCommand("volume_up")
    if text in {"тише", "сделай тише", "убавь громкость"}:
        return VoiceCommand("volume_down")

    volume_match = re.fullmatch(
        r"(?:громкость|поставь громкость|установи громкость)\s+(\d{1,3})(?:\s*процентов|\s*%)?",
        text,
    )
    if volume_match:
        return VoiceCommand(
            "volume_set",
            volume_percent=max(5, min(100, int(volume_match.group(1)))),
        )

    volume_words = re.fullmatch(
        r"(?:громкость|поставь громкость|установи громкость)\s+"
        r"([а-я]+)(?:\s+процентов)?",
        text,
    )
    if volume_words and volume_words.group(1) in _RUSSIAN_VOLUME:
        return VoiceCommand(
            "volume_set",
            volume_percent=_RUSSIAN_VOLUME[volume_words.group(1)],
        )

    query = _after_prefix(
        text,
        (
            "включи песню",
            "включи трек",
            "включай",
            "включи",
            "включить песню",
            "включить трек",
            "включить",
            "вруби песню",
            "вруби трек",
            "вруби",
            "запусти песню",
            "запусти трек",
            "запусти",
            "поставь песню",
            "поставь трек",
            "поставь",
            "поставить песню",
            "поставить трек",
            "поставить",
            "проиграй",
            "проиграть",
            "сыграй",
            "найди и включи",
            "найти и включить",
            "найди",
            "играй",
        ),
    )
    if query:
        query = re.sub(
            r"^(?:(?:мне|пожалуйста|песню|трек|музыку)\s+)+",
            "",
            query,
        ).strip()
        if query:
            return VoiceCommand("play", query=query)
    return None


def format_duration(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "неизвестно"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def clip_track_title(value: str, limit: int = 90) -> str:
    text = _SPACE_RE.sub(" ", str(value or "Без названия")).strip()
    return text if len(text) <= limit else f"{text[: max(1, limit - 1)].rstrip()}…"


__all__ = [
    "MusicAction",
    "MusicInputError",
    "MusicTrack",
    "VoiceCommand",
    "clip_track_title",
    "format_duration",
    "is_wake_word_candidate",
    "normalize_speech",
    "parse_voice_command",
    "prepare_youtube_input",
    "split_wake_word",
]
