"""Environment-backed configuration for T-Mod Music."""

from __future__ import annotations

import os
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (AttributeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except (AttributeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


MUSIC_ENABLED = _env_bool("MUSIC_ENABLED", True)
MUSIC_DEFAULT_VOLUME = _env_float(
    "MUSIC_DEFAULT_VOLUME",
    0.45,
    minimum=0.05,
    maximum=1.0,
)
MUSIC_QUEUE_LIMIT = _env_int("MUSIC_QUEUE_LIMIT", 50, minimum=1, maximum=200)
MUSIC_SEARCH_CONCURRENCY = _env_int(
    "MUSIC_SEARCH_CONCURRENCY",
    2,
    minimum=1,
    maximum=10,
)
MUSIC_SEARCH_COOLDOWN_SECONDS = _env_float(
    "MUSIC_SEARCH_COOLDOWN_SECONDS",
    2.0,
    minimum=0.0,
    maximum=30.0,
)
MUSIC_MAX_TRACK_SECONDS = _env_int(
    "MUSIC_MAX_TRACK_SECONDS",
    10_800,
    minimum=60,
    maximum=86_400,
)
MUSIC_ALLOW_LIVE = _env_bool("MUSIC_ALLOW_LIVE", False)
MUSIC_IDLE_DISCONNECT_SECONDS = _env_int(
    "MUSIC_IDLE_DISCONNECT_SECONDS",
    120,
    minimum=15,
    maximum=3600,
)
MUSIC_PANEL_REFRESH_SECONDS = _env_float(
    "MUSIC_PANEL_REFRESH_SECONDS",
    1.0,
    minimum=1.0,
    maximum=60.0,
)
MUSIC_YTDLP_COOKIE_FILE_RAW = os.getenv("MUSIC_YTDLP_COOKIE_FILE", "").strip()
MUSIC_YTDLP_COOKIE_FILE = (
    Path(MUSIC_YTDLP_COOKIE_FILE_RAW) if MUSIC_YTDLP_COOKIE_FILE_RAW else None
)
MUSIC_YTDLP_PROXY = os.getenv("MUSIC_YTDLP_PROXY", "").strip()

MUSIC_VOICE_CONTROL_ENABLED = _env_bool("MUSIC_VOICE_CONTROL_ENABLED", True)
MUSIC_WAKE_TIMEOUT_SECONDS = _env_int(
    "MUSIC_WAKE_TIMEOUT_SECONDS",
    8,
    minimum=3,
    maximum=30,
)
MUSIC_SPEECH_SILENCE_SECONDS = _env_float(
    "MUSIC_SPEECH_SILENCE_SECONDS",
    0.6,
    minimum=0.35,
    maximum=3.0,
)
MUSIC_SPEECH_MIN_SECONDS = _env_float(
    "MUSIC_SPEECH_MIN_SECONDS",
    0.25,
    minimum=0.1,
    maximum=2.0,
)
MUSIC_SPEECH_MAX_SECONDS = _env_float(
    "MUSIC_SPEECH_MAX_SECONDS",
    8.0,
    minimum=2.0,
    maximum=20.0,
)
MUSIC_STT_MODEL = os.getenv(
    "MUSIC_STT_MODEL",
    "openai/gpt-4o-mini-transcribe",
).strip()
MUSIC_STT_ACCURACY_MODEL = os.getenv(
    "MUSIC_STT_ACCURACY_MODEL",
    "openai/gpt-4o-transcribe",
).strip()
_MUSIC_STT_FALLBACK_MODELS_RAW = os.getenv(
    "MUSIC_STT_FALLBACK_MODELS",
    (f"qwen/qwen3-asr-flash-2026-02-10,{MUSIC_STT_ACCURACY_MODEL}"),
)
MUSIC_STT_FALLBACK_MODELS = tuple(
    dict.fromkeys(
        model.strip()
        for model in _MUSIC_STT_FALLBACK_MODELS_RAW.split(",")
        if model.strip() and model.strip() != MUSIC_STT_MODEL
    )
)
MUSIC_STT_LANGUAGE = os.getenv("MUSIC_STT_LANGUAGE", "ru").strip() or "ru"
MUSIC_STT_API_URL = os.getenv(
    "MUSIC_STT_API_URL",
    "https://openrouter.ai/api/v1/audio/transcriptions",
).strip()
MUSIC_STT_TIMEOUT_SECONDS = _env_int(
    "MUSIC_STT_TIMEOUT_SECONDS",
    15,
    minimum=5,
    maximum=120,
)
MUSIC_STT_QUEUE_LIMIT = _env_int(
    "MUSIC_STT_QUEUE_LIMIT",
    8,
    minimum=1,
    maximum=20,
)
MUSIC_STT_WORKERS = _env_int(
    "MUSIC_STT_WORKERS",
    2,
    minimum=1,
    maximum=4,
)
MUSIC_SIGNAL_VOLUME = _env_float(
    "MUSIC_SIGNAL_VOLUME",
    0.32,
    minimum=0.05,
    maximum=0.8,
)


__all__ = [name for name in globals() if name.startswith("MUSIC_")]
