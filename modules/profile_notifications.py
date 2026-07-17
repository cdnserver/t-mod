"""Personal DM policy shared by optional notification producers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from persistence import profile_context as storage


NOTIFICATION_KINDS = frozenset({"market", "craft", "consensus", "finance", "system"})
NOTIFICATION_FIELD_BY_KIND = {
    "market": "dm_market",
    "craft": "dm_craft",
    "consensus": "dm_consensus",
    "finance": "dm_finance",
    "system": "dm_system",
}
PROFILE_TIMEZONE_NAME = os.getenv(
    "PROFILE_TIMEZONE",
    os.getenv("LOCAL_TIMEZONE", "Europe/Riga"),
).strip() or "Europe/Riga"
try:
    PROFILE_TIMEZONE = ZoneInfo(PROFILE_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    PROFILE_TIMEZONE_NAME = "UTC"
    PROFILE_TIMEZONE = ZoneInfo("UTC")


@dataclass(frozen=True, slots=True)
class NotificationDecision:
    allowed: bool
    reason: str
    resume_at: datetime | None = None


def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def quiet_hours_resume_at(
    *,
    start_minute: int,
    end_minute: int,
    now: datetime | None = None,
) -> datetime | None:
    current_utc = _as_utc(now)
    local_now = current_utc.astimezone(PROFILE_TIMEZONE)
    start = max(0, min(int(start_minute), 1439))
    end = max(0, min(int(end_minute), 1439))
    if start == end:
        return None
    current_minute = local_now.hour * 60 + local_now.minute
    crosses_midnight = start > end
    is_quiet = (
        start <= current_minute < end
        if not crosses_midnight
        else current_minute >= start or current_minute < end
    )
    if not is_quiet:
        return None
    end_date = local_now.date()
    if crosses_midnight and current_minute >= start:
        end_date += timedelta(days=1)
    local_end = datetime.combine(
        end_date,
        time(hour=end // 60, minute=end % 60),
        tzinfo=PROFILE_TIMEZONE,
    )
    return local_end.astimezone(timezone.utc)


def evaluate_profile_notification(
    guild_id: int,
    user_id: int,
    kind: str,
    *,
    now: datetime | None = None,
    critical: bool = False,
) -> NotificationDecision:
    clean_kind = str(kind or "").strip().lower()
    if clean_kind not in NOTIFICATION_KINDS:
        raise ValueError("profile_notification_kind_invalid")
    if critical:
        return NotificationDecision(True, "critical")
    profile = storage.get_member_profile(int(guild_id), int(user_id))
    if profile is None:
        return NotificationDecision(True, "default")
    if not profile.dm_notifications:
        return NotificationDecision(False, "all_dm_disabled")
    field = NOTIFICATION_FIELD_BY_KIND[clean_kind]
    if not bool(getattr(profile, field, True)):
        return NotificationDecision(False, f"{clean_kind}_dm_disabled")
    if profile.quiet_hours_enabled:
        resume_at = quiet_hours_resume_at(
            start_minute=profile.quiet_start_minute,
            end_minute=profile.quiet_end_minute,
            now=now,
        )
        if resume_at is not None:
            return NotificationDecision(False, "quiet_hours", resume_at)
    return NotificationDecision(True, "allowed")


__all__ = [
    "NOTIFICATION_KINDS",
    "NotificationDecision",
    "PROFILE_TIMEZONE_NAME",
    "evaluate_profile_notification",
    "quiet_hours_resume_at",
]
