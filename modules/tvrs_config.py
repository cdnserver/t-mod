"""Configuration boundary for the TVRS domain.

Only environment parsing and immutable process-level settings belong here.
Keeping Discord workflows out of this module makes it safe to import from
presenters, adapters and tests without creating dependency cycles.
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

from localization import safe_command_description, safe_command_name


def env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def env_color(name: str, default: str = "0xD9D9D9") -> int:
    raw = os.getenv(name, default).strip()
    try:
        return int(raw, 16) if raw.lower().startswith("0x") else int(raw)
    except (TypeError, ValueError):
        return 0xD9D9D9


def env_int_tuple(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.getenv(name, ",".join(str(item) for item in default))
    values: list[int] = []
    for item in raw.split(","):
        try:
            value = int(item.strip())
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in values:
            values.append(value)
    return tuple(values) or tuple(default)


TVRS_MATERIALS_CHANNEL_ID = env_int("TVRS_MATERIALS_CHANNEL_ID", 1492583702641774642)
TVRS_BILLS_CHANNEL_ID = env_int("TVRS_BILLS_CHANNEL_ID", 1492471371085643937)
TVRS_CONSENSUS_VOICE_CHANNEL_ID = env_int("TVRS_CONSENSUS_VOICE_CHANNEL_ID", 1519419533667078145)
TVRS_DIRECTORY_CHANNEL_ID = env_int("TVRS_DIRECTORY_CHANNEL_ID", 1540003355735232582)
TVRS_FELLOWSHIP_ROLE_ID = env_int(
    "TVRS_FELLOWSHIP_ROLE_ID", 1557387535376719902
)
TVRS_SENATOR_ROLE_ID = env_int("TVRS_SENATOR_ROLE_ID", 1500563715622174881)
TVRS_PERMANENT_CHAIR_ID = env_int("TVRS_PERMANENT_CHAIR_ID", 811862068214890537)
TVRS_COCHAIR_IDS = env_int_tuple(
    "TVRS_COCHAIR_IDS",
    (
        721577061143019555,
        902235631952998410,
        811862068214890537,
    ),
)
TVRS_CHAIR_ROLE_ID = env_int("TVRS_CHAIR_ROLE_ID", 1488207163879985233)
TVRS_DEFAULT_NEXT_BILL_NUMBER = env_int("TVRS_DEFAULT_NEXT_BILL_NUMBER", 9)
TVRS_DEFAULT_NEXT_PLENARY_NUMBER = env_int("TVRS_DEFAULT_NEXT_PLENARY_NUMBER", 4)
TVRS_EMBED_COLOR = env_color("TVRS_EMBED_COLOR", "0xD9D9D9")
TVRS_STICKY_DEBOUNCE_SECONDS = max(1, env_int("TVRS_STICKY_DEBOUNCE_SECONDS", 2))
TVRS_DISCUSSION_CATEGORY_ID = env_int("TVRS_DISCUSSION_CATEGORY_ID", 1496802341377020067)
TVRS_TIMER_OPTIONS: tuple[tuple[str, int], ...] = (
    ("30 сек", 30),
    ("1 мин", 60),
    ("3 мин", 180),
    ("5 мин", 300),
)
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "Europe/Riga"))

TVRS_COMMAND_NAME = safe_command_name("tvrs.commands.tvrs_name", "tvrs")
TVRS_COMMAND_DESCRIPTION = safe_command_description(
    "tvrs.commands.tvrs_description",
    "Открыть Универсалитет Товарищества",
)
TVRS_DIRECTORY_COMMAND_NAME = safe_command_name(
    "tvrs.commands.directory_name",
    "tvrs_directory",
)
TVRS_DIRECTORY_COMMAND_DESCRIPTION = safe_command_description(
    "tvrs.commands.directory_description",
    "Управление живым реестром ответственных и Сената",
)
TVRS_SETBILL_COMMAND_NAME = safe_command_name("tvrs.commands.setbill_name", "tvrs_setbill")
TVRS_SETBILL_COMMAND_DESCRIPTION = safe_command_description(
    "tvrs.commands.setbill_description",
    "Изменить последний принятый законопроект",
)
TVRS_STICKY_COMMAND_NAME = safe_command_name("tvrs.commands.sticky_name", "tvrs_sticky")
TVRS_STICKY_COMMAND_DESCRIPTION = safe_command_description(
    "tvrs.commands.sticky_description",
    "Обновить сообщение подачи законопроектов",
)
TVRS_ADMIN_COMMAND_NAME = safe_command_name("tvrs.commands.admin_name", "tvrs_admin")
TVRS_ADMIN_COMMAND_DESCRIPTION = safe_command_description(
    "tvrs.commands.admin_description",
    "Администрирование законопроектов и консенсусов",
)


__all__ = [name for name in globals() if name.startswith("TVRS_")] + [
    "LOCAL_TZ",
    "env_color",
    "env_int",
]
