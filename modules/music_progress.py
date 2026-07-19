"""Monotonic playback clock shared by the player and public presentation."""

from __future__ import annotations

import time
from typing import Any


def start_playback_clock(session: Any, *, now: float | None = None) -> None:
    session.track_started_at = time.monotonic() if now is None else float(now)
    session.track_paused_at = None
    session.track_paused_seconds = 0.0


def pause_playback_clock(session: Any, *, now: float | None = None) -> None:
    if session.track_started_at is None or session.track_paused_at is not None:
        return
    session.track_paused_at = time.monotonic() if now is None else float(now)


def resume_playback_clock(session: Any, *, now: float | None = None) -> None:
    if session.track_paused_at is None:
        return
    timestamp = time.monotonic() if now is None else float(now)
    session.track_paused_seconds += max(0.0, timestamp - session.track_paused_at)
    session.track_paused_at = None


def reset_playback_clock(session: Any) -> None:
    session.track_started_at = None
    session.track_paused_at = None
    session.track_paused_seconds = 0.0


def playback_elapsed_seconds(session: Any, *, now: float | None = None) -> int:
    if session.track_started_at is None:
        return 0
    timestamp = (
        session.track_paused_at
        if session.track_paused_at is not None
        else (time.monotonic() if now is None else float(now))
    )
    elapsed = timestamp - session.track_started_at - session.track_paused_seconds
    return max(0, int(elapsed))


def progress_bar(elapsed: int, duration: int | None, *, width: int = 14) -> str:
    if not duration or duration <= 0:
        pulse = max(0, int(elapsed)) % max(1, width)
        return "".join("◆" if index == pulse else "─" for index in range(width))
    ratio = max(0.0, min(1.0, elapsed / duration))
    marker = min(width - 1, round((width - 1) * ratio))
    return "".join(
        "━" if index < marker else "●" if index == marker else "─"
        for index in range(width)
    )


__all__ = [
    "pause_playback_clock",
    "playback_elapsed_seconds",
    "progress_bar",
    "reset_playback_clock",
    "resume_playback_clock",
    "start_playback_clock",
]
