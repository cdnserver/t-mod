"""Pure rules for music queries, queue presentation and Russian voice commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
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
    r"т[иы]?\s*[-–—]?\s*мод|ти\s+мод|t\s*[-–—]?\s*mod|teamod|"
    r"сборщик\s+риса"
    r")(?=\s|$)",
    re.IGNORECASE,
)
_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^0-9a-zа-яё%\s-]+", re.IGNORECASE)


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


def parse_voice_command(value: str) -> VoiceCommand | None:
    text = normalize_speech(value)
    if not text:
        return None

    if text in {"пауза", "приостанови", "поставь на паузу", "замри"}:
        return VoiceCommand("pause")
    if text in {
        "продолжи",
        "продолжай",
        "возобнови",
        "сними с паузы",
        "играй дальше",
        "играй",
        "включи музыку",
    }:
        return VoiceCommand("resume")
    if text in {
        "стоп",
        "останови",
        "останови музыку",
        "выключи",
        "выключи музыку",
        "очисти очередь",
    }:
        return VoiceCommand("stop")
    if text in {
        "дальше",
        "следующая",
        "следующий трек",
        "пропусти",
        "пропусти трек",
        "скип",
    }:
        return VoiceCommand("skip")
    if text in {"уйди", "отключись", "выйди из канала", "покинь канал"}:
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

    query = _after_prefix(
        text,
        (
            "включи песню",
            "включи трек",
            "включи",
            "поставь песню",
            "поставь трек",
            "поставь",
            "проиграй",
            "найди и включи",
            "найди",
            "играй",
        ),
    )
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
    "normalize_speech",
    "parse_voice_command",
    "prepare_youtube_input",
    "split_wake_word",
]
