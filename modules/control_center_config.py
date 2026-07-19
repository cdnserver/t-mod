"""Configuration and channel topology for the operational control center."""

from __future__ import annotations

from dataclasses import dataclass
import os


def env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw, 0)
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def positive_env_int(name: str, default: int) -> int:
    value = env_int(name, default)
    return value if value > 0 else default


OPERATIONS_CATEGORY_ID = env_int("OPERATIONS_CATEGORY_ID", 1526605447878934589)
CONTROL_PANEL_CHANNEL_ID = env_int("CONTROL_PANEL_CHANNEL_ID", 0)
MUSIC_PANEL_CHANNEL_ID = env_int("MUSIC_PANEL_CHANNEL_ID", 0)
ACTIVE_TASKS_CHANNEL_ID = positive_env_int(
    "ACTIVE_TASKS_CHANNEL_ID", 1526606213826220198
)
WORKSHOP_CHANNEL_ID = positive_env_int("WORKSHOP_CHANNEL_ID", 1526606361369116672)
FINANCE_LOG_CHANNEL_ID = positive_env_int(
    "FINANCE_EVENT_LOG_CHANNEL_ID", 1526606262035550369
)
TECH_LOG_CHANNEL_ID = env_int("TECH_LOG_CHANNEL_ID", 0)
REPORTS_CHANNEL_ID = env_int("REPORTS_CHANNEL_ID", 0)
BOT_SETTINGS_CHANNEL_ID = env_int("BOT_SETTINGS_CHANNEL_ID", 0)
BOT_TEST_CHANNEL_ID = env_int("BOT_TEST_CHANNEL_ID", 0)
OPERATIONS_AUTO_CREATE_CHANNELS = env_bool("OPERATIONS_AUTO_CREATE_CHANNELS", True)
CONTROL_CENTER_COLOR = env_int("CONTROL_CENTER_COLOR", 0xD9D9D9)


@dataclass(frozen=True, slots=True)
class ChannelSpec:
    key: str
    name: str
    env_name: str
    configured_id: int
    topic: str


CHANNEL_SPECS: tuple[ChannelSpec, ...] = (
    ChannelSpec(
        "control_panel",
        "панель-управления",
        "CONTROL_PANEL_CHANNEL_ID",
        CONTROL_PANEL_CHANNEL_ID,
        "Единое публичное меню T-Mod. Все рабочие ответы открываются лично пользователю.",
    ),
    ChannelSpec(
        "music",
        "музыкальный-центр",
        "MUSIC_PANEL_CHANNEL_ID",
        MUSIC_PANEL_CHANNEL_ID,
        "Единая живая панель музыки T-Mod. Нажатия и ответы остаются личными.",
    ),
    ChannelSpec(
        "active_tasks",
        "активные-задачи",
        "ACTIVE_TASKS_CHANNEL_ID",
        ACTIVE_TASKS_CHANNEL_ID,
        "Только активные задачи Товарищества и ссылки на их исходные сообщения.",
    ),
    ChannelSpec(
        "workshop",
        "мастерская",
        "WORKSHOP_CHANNEL_ID",
        WORKSHOP_CHANNEL_ID,
        "Рецепты, планы, напоминания и история крафтов Товарищества.",
    ),
    ChannelSpec(
        "finance_log",
        "лог-финансы",
        "FINANCE_EVENT_LOG_CHANNEL_ID",
        FINANCE_LOG_CHANNEL_ID,
        "Сверки казны и неизменяемая история финансовых операций.",
    ),
    ChannelSpec(
        "tech_log",
        "лог-тех",
        "TECH_LOG_CHANNEL_ID",
        TECH_LOG_CHANNEL_ID,
        "Состояние T-Mod, запуски и технические ошибки.",
    ),
    ChannelSpec(
        "reports",
        "отчёты",
        "REPORTS_CHANNEL_ID",
        REPORTS_CHANNEL_ID,
        "Каркас регулярных отчётов Товарищества.",
    ),
    ChannelSpec(
        "settings",
        "настройки-бота",
        "BOT_SETTINGS_CHANNEL_ID",
        BOT_SETTINGS_CHANNEL_ID,
        "Диагностика и безопасное обслуживание T-Mod администраторами.",
    ),
    ChannelSpec(
        "test",
        "тест-бота",
        "BOT_TEST_CHANNEL_ID",
        BOT_TEST_CHANNEL_ID,
        "Безопасная проверка доступности T-Mod без записей в рабочие журналы.",
    ),
)
CHANNEL_SPEC_BY_KEY = {spec.key: spec for spec in CHANNEL_SPECS}

PANEL_MARKERS = {
    "control_panel": "tmod-public-control-panel",
    "music": "tmod-music-public-panel",
    "reports": "tmod-reports-scaffold",
    "settings": "tmod-settings-panel",
    "test": "tmod-test-panel",
    "tech_log": "tmod-tech-health",
}
