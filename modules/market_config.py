"""Environment-backed settings and immutable category metadata for Market."""

from __future__ import annotations

import os


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


MARKET_SERVER_ID = os.getenv("MAJESTIC_SERVER_ID", "RU15").strip().upper() or "RU15"
MARKET_CATEGORY = "items"
MARKET_CATEGORY_INFO: dict[str, dict[str, str]] = {
    "items": {
        "label": "Предметы",
        "singular": "Предмет",
        "emoji": "📦",
        "search_hint": "железная руда, аптечка, #39",
        "quantity": "Размещено",
    },
    "vehicles": {
        "label": "Автомобили",
        "singular": "Автомобиль",
        "emoji": "🚗",
        "search_hint": "Pegassi, фаджио, Sultan",
        "quantity": "Размещено",
    },
    "clothes": {
        "label": "Одежда",
        "singular": "Вариант одежды",
        "emoji": "👕",
        "search_hint": "черная сумка, очки, куртка",
        "quantity": "Продано",
    },
}
MARKET_CATEGORIES = tuple(MARKET_CATEGORY_INFO)
MARKET_REFRESH_SECONDS = max(60, env_int("MARKET_REFRESH_SECONDS", 60))
MARKET_HISTORY_RETENTION_DAYS = max(30, env_int("MARKET_HISTORY_RETENTION_DAYS", 400))
MARKET_EMBED_COLOR = env_int("MARKET_EMBED_COLOR", 0xD9D9D9)
MARKET_MAX_ALERTS_PER_USER = max(1, min(env_int("MARKET_MAX_ALERTS_PER_USER", 20), 25))
MARKET_ALERT_MAX_DELIVERY_ATTEMPTS = max(1, env_int("MARKET_ALERT_MAX_DELIVERY_ATTEMPTS", 3))
MARKET_ALERT_RETRY_SECONDS = max(60, env_int("MARKET_ALERT_RETRY_SECONDS", 600))
MARKET_SEARCH_LIMIT = 25
MARKET_POPULAR_QUERY = "Популярное по продажам"


__all__ = [name for name in globals() if name.startswith("MARKET_")] + ["env_int"]
