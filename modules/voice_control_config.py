"""Environment-backed configuration for the reusable voice-control platform."""

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


VOICE_LOCAL_STT_ENABLED = _env_bool("VOICE_LOCAL_STT_ENABLED", True)
VOICE_LOCAL_STT_MODEL = os.getenv("VOICE_LOCAL_STT_MODEL", "tiny").strip() or "tiny"
VOICE_LOCAL_STT_DEVICE = os.getenv("VOICE_LOCAL_STT_DEVICE", "cpu").strip() or "cpu"
VOICE_LOCAL_STT_COMPUTE_TYPE = (
    os.getenv("VOICE_LOCAL_STT_COMPUTE_TYPE", "int8").strip() or "int8"
)
VOICE_LOCAL_STT_CPU_THREADS = _env_int(
    "VOICE_LOCAL_STT_CPU_THREADS",
    2,
    minimum=1,
    maximum=16,
)
VOICE_LOCAL_STT_WORKERS = _env_int(
    "VOICE_LOCAL_STT_WORKERS",
    1,
    minimum=1,
    maximum=4,
)
VOICE_LOCAL_STT_WARMUP = _env_bool("VOICE_LOCAL_STT_WARMUP", True)
VOICE_LOCAL_STT_WAIT_SECONDS = _env_float(
    "VOICE_LOCAL_STT_WAIT_SECONDS",
    0.75,
    minimum=0.1,
    maximum=3.0,
)
VOICE_MODEL_CACHE_DIR = Path(
    os.getenv("VOICE_MODEL_CACHE_DIR", "/app/persistent/models/voice").strip()
    or "/app/persistent/models/voice"
)
VOICE_FAST_COMMAND_CONFIDENCE = _env_float(
    "VOICE_FAST_COMMAND_CONFIDENCE",
    0.58,
    minimum=0.0,
    maximum=1.0,
)
VOICE_CAUTION_COMMAND_CONFIDENCE = _env_float(
    "VOICE_CAUTION_COMMAND_CONFIDENCE",
    0.76,
    minimum=0.0,
    maximum=1.0,
)
VOICE_BACKGROUND_REJECT_CONFIDENCE = _env_float(
    "VOICE_BACKGROUND_REJECT_CONFIDENCE",
    0.66,
    minimum=0.0,
    maximum=1.0,
)
VOICE_DIAGNOSTIC_TIMEOUT_SECONDS = _env_int(
    "VOICE_DIAGNOSTIC_TIMEOUT_SECONDS",
    12,
    minimum=5,
    maximum=30,
)


__all__ = [name for name in globals() if name.startswith("VOICE_")]
