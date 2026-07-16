"""Pure formatting helpers shared by TVRS Discord presenters."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from modules.consensus_core import (
    LiveConsensusSession,
    LiveParticipant,
    clean_stage_name as clean_consensus_stage_name,
)
from modules.tvrs_config import LOCAL_TZ


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(LOCAL_TZ)


def format_bill_number(number: int) -> str:
    return f"{int(number):03d}"


def format_dt(dt: datetime | None = None) -> str:
    return (dt or now_local()).strftime("%d.%m.%Y %H:%M")


def ru_ordinal(number: int) -> str:
    words = {
        1: "первый",
        2: "второй",
        3: "третий",
        4: "четвертый",
        5: "пятый",
        6: "шестой",
        7: "седьмой",
        8: "восьмой",
        9: "девятый",
        10: "десятый",
        11: "одиннадцатый",
        12: "двенадцатый",
        13: "тринадцатый",
        14: "четырнадцатый",
        15: "пятнадцатый",
        16: "шестнадцатый",
        17: "семнадцатый",
        18: "восемнадцатый",
        19: "девятнадцатый",
        20: "двадцатый",
    }
    return words.get(int(number), f"{number}-й")


def role_label(participant: LiveParticipant) -> str:
    if participant.permanent:
        return "ППС"
    return "Председатель" if participant.kind == "chair" else "Сенатор"


def status_icon(confirmed: bool) -> str:
    return "✅" if confirmed else "❌"


def materials_text(materials: str | None) -> str:
    if not materials or not materials.strip():
        return "Материалы не приложены."
    parts = [item.strip() for item in materials.split(",") if item.strip()]
    if not parts:
        return "Материалы не приложены."
    return "\n".join(f"• {item}" for item in parts)[:1000]


def clip_text(value: Any, limit: int = 1000, empty: str = "—") -> str:
    text = str(value or "").strip()
    if not text:
        return empty
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def progress_bar(percent: float, size: int = 12) -> str:
    bounded = max(0.0, min(100.0, float(percent or 0.0)))
    filled = round(size * bounded / 100.0)
    return "█" * filled + "░" * (size - filled)


def vote_progress(session: LiveConsensusSession) -> tuple[int, int, float]:
    participants = session.confirmed_participants()
    total = len(participants)
    voted = sum(participant.user_id in session.votes for participant in participants)
    percent = (voted / total * 100.0) if total else 0.0
    return voted, total, round(percent, 1)


def clean_stage_name(stage: str) -> str:
    return clean_consensus_stage_name(stage)


def vote_split_lines(session: LiveConsensusSession) -> tuple[str, str, str]:
    yes: list[str] = []
    no: list[str] = []
    waiting: list[str] = []
    participants = sorted(
        session.confirmed_participants(),
        key=lambda item: (item.kind != "chair", item.display_name.lower()),
    )
    for participant in participants:
        line = f"{participant.mention} · `{role_label(participant)}`"
        vote = session.votes.get(participant.user_id)
        if vote == "yes":
            yes.append(line)
        elif vote == "no":
            no.append(line)
        else:
            waiting.append(line)
    return (
        "\n".join(yes)[:1000] or "—",
        "\n".join(no)[:1000] or "—",
        "\n".join(waiting)[:1000] or "—",
    )


def format_timer(seconds: int | None) -> str:
    if not seconds:
        return "не установлен"
    if seconds < 60:
        return f"{seconds} сек."
    minutes = seconds // 60
    rest = seconds % 60
    return f"{minutes} мин. {rest} сек." if rest else f"{minutes} мин."


def remaining_timer_text(session: LiveConsensusSession) -> str:
    if not session.timer_deadline:
        return "Таймер не установлен."
    left = int((session.timer_deadline - datetime.now(timezone.utc)).total_seconds())
    if left <= 0:
        return "Время истекло."
    return f"Осталось примерно {format_timer(left)}."


def vote_label(vote: str | None) -> str:
    if vote == "yes":
        return "✅ За"
    if vote == "no":
        return "❌ Против"
    return "⏳ ожидается"


def result_status_text(status: str) -> str:
    return {
        "accepted": "принят",
        "rejected": "не принят",
        "vetoed": "вето",
    }.get(status, status)
