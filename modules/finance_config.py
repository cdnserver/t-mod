"""Environment-backed settings for the Finance feature."""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

from modules.control_center_config import FINANCE_LOG_CHANNEL_ID


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw, 0)
    except (TypeError, ValueError):
        return default


_configured_finance_log_id = env_int("FINANCE_EVENT_LOG_CHANNEL_ID", FINANCE_LOG_CHANNEL_ID)
FINANCE_EVENT_LOG_CHANNEL_ID = (
    _configured_finance_log_id if _configured_finance_log_id > 0 else FINANCE_LOG_CHANNEL_ID
)
FINANCE_DAILY_CHANNEL_ID = FINANCE_EVENT_LOG_CHANNEL_ID
FINANCE_COMMAND_CHANNEL_ID = 0
FINANCE_ADMIN_USER_ID = env_int("FINANCE_ADMIN_USER_ID", 902235631952998410)
FINANCE_REPORT_HOUR = max(0, min(23, env_int("FINANCE_REPORT_HOUR", 18)))
FINANCE_REPORT_MINUTE = max(0, min(59, env_int("FINANCE_REPORT_MINUTE", 0)))
FINANCE_EMBED_COLOR = env_int("FINANCE_EMBED_COLOR", 0xD9D9D9)
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "Europe/Riga"))

MAX_MONEY_AMOUNT = 10**15
GAME_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ"
