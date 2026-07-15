from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import os
import re
import threading
import traceback
import unicodedata
from typing import Any, Callable

import discord
from discord import app_commands
from discord.ext import commands

import storage
from modules.control_center import log_technical_event
from modules.majestic_api import (
    MajesticApiConfigurationError,
    MajesticApiDisabledError,
    MajesticApiError,
    MajesticApiResponseError,
    get_majestic_api_client,
)


def _env_int(name: str, default: int) -> int:
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
MARKET_REFRESH_SECONDS = max(60, _env_int("MARKET_REFRESH_SECONDS", 60))
MARKET_HISTORY_RETENTION_DAYS = max(30, _env_int("MARKET_HISTORY_RETENTION_DAYS", 400))
MARKET_EMBED_COLOR = _env_int("MARKET_EMBED_COLOR", 0xD9D9D9)
MARKET_MAX_ALERTS_PER_USER = max(1, min(_env_int("MARKET_MAX_ALERTS_PER_USER", 20), 25))
MARKET_ALERT_MAX_DELIVERY_ATTEMPTS = max(1, _env_int("MARKET_ALERT_MAX_DELIVERY_ATTEMPTS", 3))
MARKET_ALERT_RETRY_SECONDS = max(60, _env_int("MARKET_ALERT_RETRY_SECONDS", 600))
MARKET_SEARCH_LIMIT = 25
MARKET_POPULAR_QUERY = "Популярное по продажам"

_worker_task: asyncio.Task[None] | None = None


def normalize_market_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def _transliterate_ru(value: str) -> str:
    mapping = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    return "".join(mapping.get(char, char) for char in normalize_market_text(value))


def _text_match_score(query: str, name: str) -> int:
    if not query or not name:
        return 0
    if query == name:
        return 15_000
    if name.startswith(query):
        return 12_000 - min(1000, len(name) - len(query))
    if query in name:
        return 10_000 - min(1000, name.index(query) * 10)

    query_tokens = query.split()
    name_tokens = name.split()
    if query_tokens and all(
        any(token.startswith(part) or part.startswith(token) for token in name_tokens)
        for part in query_tokens
    ):
        return 9000 + sum(min(len(part), 20) for part in query_tokens)

    whole_ratio = SequenceMatcher(None, query, name).ratio()
    token_ratio = max(
        (SequenceMatcher(None, part, token).ratio() for part in query_tokens for token in name_tokens),
        default=0.0,
    )
    ratio = max(whole_ratio, token_ratio * 0.92)
    return int(ratio * 8000) if ratio >= 0.52 else 0


def _market_search_score(query: str, item: dict[str, Any]) -> int:
    category = str(item.get("category") or MARKET_CATEGORY)
    external_id = normalize_market_text(item.get("external_id") or item.get("item_id") or "")
    raw_internal_id = str(item.get("item_id") or "")
    if category == "items" and query.lstrip("#") == raw_internal_id and (query.startswith("#") or query.isdigit()):
        return 20_000
    if query == external_id:
        return 18_000
    name = str(item.get("normalized_name") or normalize_market_text(item.get("item_name") or ""))
    queries = {query, _transliterate_ru(query)}
    names = {name, external_id, _transliterate_ru(name)}
    return max((_text_match_score(candidate_query, candidate_name) for candidate_query in queries for candidate_name in names), default=0)


def _market_internal_id(category: str, external_id: str) -> int:
    clean_category = str(category).strip().lower()
    clean_external_id = str(external_id).strip()
    if clean_category == "items" and clean_external_id.isdigit():
        return int(clean_external_id)
    digest = hashlib.blake2b(
        f"{clean_category}:{clean_external_id}".encode("utf-8"),
        digest_size=8,
        person=b"tmodmkt",
    ).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


def _clean_market_category(category: str | None) -> str:
    clean = str(category or MARKET_CATEGORY).strip().lower()
    return clean if clean in MARKET_CATEGORY_INFO else MARKET_CATEGORY


def _category_info(category: str) -> dict[str, str]:
    return MARKET_CATEGORY_INFO[_clean_market_category(category)]


def _item_identity(item: dict[str, Any]) -> str:
    category = _clean_market_category(item.get("category"))
    external_id = str(item.get("external_id") or item.get("item_id") or "")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    if category == "items":
        return f"#{external_id}"
    if category == "vehicles":
        return f"model: {external_id}"
    gender = "муж." if int(metadata.get("gender") or 0) == 0 else "жен."
    kind = "аксессуар" if int(metadata.get("isProp") or 0) else "одежда"
    return (
        f"{gender} · {kind} · c{int(metadata.get('component') or 0)} / "
        f"d{int(metadata.get('drawable') or 0)} / t{int(metadata.get('texture') or 0)}"
    )


def _quantity_label(category: str) -> str:
    return _category_info(category)["quantity"]


def _quantity_value(item: dict[str, Any]) -> int:
    category = _clean_market_category(item.get("category"))
    if category == "clothes":
        return int(item.get("sold_count") or 0)
    return int(item.get("total_count") or 0)


@dataclass(frozen=True, slots=True)
class MarketSearchHit:
    item: dict[str, Any]
    score: int


class MarketCatalogService:
    """Reusable local market catalog backed by Majestic snapshots and SQLite."""

    def __init__(self, server_id: str = MARKET_SERVER_ID, category: str = MARKET_CATEGORY) -> None:
        self.server_id = str(server_id).strip().upper()
        self.category = str(category).strip().lower()
        self._sync_lock = asyncio.Lock()
        self._cache_lock = threading.Lock()
        self._cache_version: tuple[str, int] | None = None
        self._cached_items: tuple[dict[str, Any], ...] = ()

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cache_version = None
            self._cached_items = ()

    def status(self) -> dict[str, Any]:
        return storage.market_catalog_status(self.server_id, self.category)

    def _items(self) -> tuple[dict[str, Any], ...]:
        status = self.status()
        version = (str(status.get("source_updated_at") or ""), int(status.get("record_count") or 0))
        with self._cache_lock:
            if self._cache_version == version and self._cached_items:
                return self._cached_items
        rows = tuple(storage.market_list_items(self.server_id, self.category, limit=10000))
        with self._cache_lock:
            self._cache_version = version
            self._cached_items = rows
        return rows

    def search(self, query: str, limit: int = MARKET_SEARCH_LIMIT) -> list[MarketSearchHit]:
        clean_query = normalize_market_text(query)
        if not clean_query:
            return []
        hits = [
            MarketSearchHit(item=item, score=score)
            for item in self._items()
            if (score := _market_search_score(clean_query, item)) > 0
        ]
        hits.sort(
            key=lambda hit: (
                -hit.score,
                -int(hit.item.get("sold_count") or 0),
                str(hit.item.get("item_name") or "").casefold(),
            )
        )
        return hits[: max(1, min(int(limit), MARKET_SEARCH_LIMIT))]

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        return storage.market_get_item(self.server_id, int(item_id), self.category)

    def popular(self, limit: int = 10) -> list[MarketSearchHit]:
        rows = storage.market_popular_items(self.server_id, self.category, limit)
        return [MarketSearchHit(item=row, score=0) for row in rows]

    def history(self, item_id: int, limit: int = 30) -> list[dict[str, Any]]:
        return storage.market_item_history(self.server_id, int(item_id), self.category, limit)

    def alert(self, discord_user_id: int, item_id: int) -> dict[str, Any] | None:
        return storage.market_get_alert(discord_user_id, self.server_id, item_id, self.category)

    def alerts(self, discord_user_id: int) -> list[dict[str, Any]]:
        return storage.market_list_user_alerts(discord_user_id, MARKET_MAX_ALERTS_PER_USER)

    async def sync(self, *, use_cache: bool = False) -> dict[str, Any]:
        async with self._sync_lock:
            client = get_majestic_api_client()
            snapshot = await client.marketplace_snapshot_async(
                self.category,
                self.server_id,
                use_cache=use_cache,
            )
            fetched_at = datetime.now(timezone.utc).isoformat()
            source_updated_at = snapshot.summary.last_updated or fetched_at
            rows = [
                {
                    "item_id": _market_internal_id(self.category, item.external_id),
                    "external_id": item.external_id,
                    "item_name": item.item_name,
                    "normalized_name": normalize_market_text(item.item_name),
                    "metadata": item.metadata,
                    "total_count": item.total_count,
                    "sold_count": item.sold_count,
                    "average_price": item.average_price,
                    "min_price": item.min_price,
                    "max_price": item.max_price,
                }
                for item in snapshot.entries
            ]
            previous = await asyncio.to_thread(self.status)
            previous_count = int(previous.get("record_count") or 0)
            if previous_count >= 100 and len(rows) * 2 < previous_count:
                raise MajesticApiResponseError(
                    f"Majestic вернул только {len(rows)} из прежних {previous_count} записей; "
                    "локальный каталог сохранён без замены."
                )
            status = await asyncio.to_thread(
                storage.market_replace_snapshot,
                server_id=snapshot.summary.server_id or self.server_id,
                category=self.category,
                server_name=snapshot.summary.server_name,
                source_updated_at=source_updated_at,
                period_days=snapshot.summary.period_days,
                items=rows,
                fetched_at=fetched_at,
                history_retention_days=MARKET_HISTORY_RETENTION_DAYS,
            )
            self.invalidate_cache()
            return status

    async def ensure_ready(self) -> dict[str, Any]:
        status = await asyncio.to_thread(self.status)
        if int(status.get("record_count") or 0) > 0:
            return status
        try:
            return await self.sync(use_cache=True)
        except MajesticApiError:
            return await asyncio.to_thread(self.status)


market_catalogs = {
    category: MarketCatalogService(category=category)
    for category in MARKET_CATEGORIES
}
# Backwards-compatible alias used by integrations that expect the item catalog.
market_catalog = market_catalogs[MARKET_CATEGORY]


def get_market_catalog(category: str | None = None) -> MarketCatalogService:
    return market_catalogs[_clean_market_category(category)]


def _number(value: int | float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", " ")


def _price(value: int | float | None) -> str:
    return f"{_number(value)} $" if value is not None else "—"


def _positive_integer(value: str, label: str) -> int:
    raw = str(value or "").strip()
    if not raw or not re.fullmatch(r"[0-9\s.,_'’]+", raw):
        raise ValueError(f"{label}: введите целое число, например `25 000`.")
    compact = re.sub(r"[\s_'’]", "", raw)
    if "." in compact or "," in compact:
        if not re.fullmatch(r"[0-9]{1,3}(?:[.,][0-9]{3})+", compact):
            raise ValueError(f"{label}: точка и запятая допустимы только как разделители тысяч.")
    digits = re.sub(r"[^0-9]", "", compact)
    amount = int(digits or 0)
    if amount <= 0:
        raise ValueError(f"{label}: значение должно быть больше нуля.")
    if amount > 10**15:
        raise ValueError(f"{label}: значение слишком большое.")
    return amount


def _discord_time(value: str | None, style: str = "R") -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return f"<t:{int(parsed.timestamp())}:{style}>"
    except (TypeError, ValueError):
        return str(value)


def _alert_status_text(alert: dict[str, Any]) -> str:
    status = str(alert.get("status") or "paused")
    return {
        "active": "🟢 ждёт нового среза",
        "notifying": "🟡 условие найдено, готовится ЛС",
        "triggered": "✅ сработал",
        "paused": "⏸️ на паузе",
    }.get(status, "⚪ неактивен")


def _observed_market_price(item: dict[str, Any]) -> int | None:
    minimum = int(item.get("min_price") or 0)
    average = int(item.get("average_price") or 0)
    return minimum if minimum > 0 else average if average > 0 else None


def market_home_embed(status: dict[str, Any], category: str = MARKET_CATEGORY) -> discord.Embed:
    category = _clean_market_category(category)
    info = _category_info(category)
    ready = int(status.get("record_count") or 0) > 0
    embed = discord.Embed(
        title=f"{info['emoji']} Рынок {MARKET_SERVER_ID} · {info['label']}",
        description=(
            f"Личный поиск по каталогу **{info['label'].lower()}** Majestic. "
            "T-Mod ищет по локальному срезу: меню работает быстро и не расходует API-запрос на каждый поиск."
        ),
        color=MARKET_EMBED_COLOR if ready else discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    if ready:
        embed.add_field(
            name="📚 Каталог T-Mod",
            value=(
                f"Записей: **{_number(int(status.get('record_count') or 0))}**\n"
                f"Срез Majestic: {_discord_time(status.get('source_updated_at'))}"
            ),
            inline=True,
        )
        embed.add_field(
            name="🔄 Синхронизация",
            value=(
                f"Проверка: **раз в {MARKET_REFRESH_SECONDS} сек.**\n"
                f"Последний успех: {_discord_time(status.get('last_success_at'))}"
            ),
            inline=True,
        )
    else:
        embed.add_field(
            name="⏳ Каталог готовится",
            value="Первая синхронизация ещё не завершилась. Попробуйте обновить меню через несколько секунд.",
            inline=False,
        )
    embed.add_field(
        name="🔎 Как искать",
        value=f"Например: `{info['search_hint']}`. Поиск понимает фрагменты и небольшие опечатки.",
        inline=False,
    )
    embed.add_field(
        name="🔔 Личные сигналы",
        value=(
            f"В карточке {info['singular'].lower()} можно задать максимальную цену и минимальное количество. "
            "T-Mod проверит условие один раз на каждом **новом срезе Majestic** и отправит личное сообщение."
        ),
        inline=False,
    )
    embed.add_field(
        name="Важно о ценах",
        value=(
            "Это статистика Majestic за период, а не live-список объявлений. "
            + ("Для одежды API не отдаёт остаток: количество означает продажи за период." if category == "clothes" else "")
        ),
        inline=False,
    )
    embed.set_footer(text="Меню видно только вам • поиск и просмотр сигналов не расходуют API-запросы")
    return embed


def market_results_embed(
    query: str,
    hits: list[MarketSearchHit],
    status: dict[str, Any],
    category: str = MARKET_CATEGORY,
) -> discord.Embed:
    category = _clean_market_category(category)
    info = _category_info(category)
    embed = discord.Embed(
        title=f"🔎 Результаты: {query[:80]}",
        description=(
            f"Выберите {info['singular'].lower()} в списке под карточкой. Результаты отсортированы по точности названия, "
            "а при равенстве — по объёму продаж."
        ),
        color=MARKET_EMBED_COLOR,
    )
    lines = []
    for index, hit in enumerate(hits[:10], 1):
        item = hit.item
        lines.append(
            f"`{index:02d}` **{str(item.get('item_name') or 'Без названия')[:70]}**\n"
            f"      `{_item_identity(item)[:70]}` · средняя **{_price(item.get('average_price'))}** · продаж **{_number(item.get('sold_count'))}**"
        )
    embed.add_field(name=f"Найдено вариантов: {len(hits)}", value="\n".join(lines)[:4000], inline=False)
    embed.add_field(
        name="Актуальность",
        value=f"Срез Majestic обновлён {_discord_time(status.get('source_updated_at'))}.",
        inline=False,
    )
    embed.set_footer(text="Подробная карточка откроется лично в этом же меню")
    return embed


def market_empty_embed(query: str, status: dict[str, Any], category: str = MARKET_CATEGORY) -> discord.Embed:
    category = _clean_market_category(category)
    info = _category_info(category)
    embed = discord.Embed(
        title="🔍 Ничего не найдено",
        description=(
            f"По запросу **{query[:100]}** нет уверенных совпадений. "
            f"Попробуйте часть названия: `{info['search_hint']}`."
        ),
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="Каталог",
        value=(
            f"Загружено записей: **{_number(int(status.get('record_count') or 0))}** · "
            f"срез {_discord_time(status.get('source_updated_at'))}"
        ),
        inline=False,
    )
    embed.set_footer(text="Поиск понимает русский язык, е/ё и небольшие опечатки")
    return embed


def _trend_text(history: list[dict[str, Any]]) -> str:
    if len(history) < 2:
        return "История начнёт показывать изменение цены после следующего нового среза Majestic."
    current = history[0].get("average_price")
    previous = history[1].get("average_price")
    if current is None or previous in (None, 0):
        return f"Сохранено срезов: **{len(history)}**."
    delta = int(current) - int(previous)
    percent = delta / int(previous) * 100
    arrow = "↗️" if delta > 0 else "↘️" if delta < 0 else "➡️"
    sign = "+" if delta > 0 else ""
    return f"{arrow} **{sign}{_number(delta)} $** ({sign}{percent:.1f}%) к предыдущему срезу."


def market_item_embed(
    item: dict[str, Any],
    status: dict[str, Any],
    history: list[dict[str, Any]],
    alert: dict[str, Any] | None = None,
) -> discord.Embed:
    category = _clean_market_category(item.get("category"))
    info = _category_info(category)
    name = str(item.get("item_name") or "Без названия")
    embed = discord.Embed(
        title=f"{info['emoji']} {name[:240]}",
        description=(
            f"{info['singular']} · `{_item_identity(item)}` · "
            f"**{MARKET_SERVER_ID} · {status.get('server_name') or 'Phoenix'}**"
        ),
        color=MARKET_EMBED_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="💰 Рыночный ориентир",
        value=(
            f"Средняя цена: **{_price(item.get('average_price'))}**\n"
            f"Минимум: **{_price(item.get('min_price'))}**\n"
            f"Максимум: **{_price(item.get('max_price'))}**"
        ),
        inline=True,
    )
    activity = f"Продано: **{_number(item.get('sold_count'))}**\n"
    if category != "clothes":
        activity = f"{_quantity_label(category)}: **{_number(_quantity_value(item))}**\n" + activity
    embed.add_field(
        name="📊 Активность за период",
        value=activity + f"Период: **{int(status.get('period_days') or 0) or '—'} дней**",
        inline=True,
    )
    embed.add_field(name="📉 Динамика T-Mod", value=_trend_text(history), inline=False)
    if alert is not None:
        alert_value = (
            f"Статус: **{_alert_status_text(alert)}**\n"
            f"Цена не выше: **{_price(alert.get('target_price'))}**\n"
            f"Количество: **от {_number(alert.get('min_quantity'))} шт.**"
        )
        if alert.get("last_evaluated_source_at"):
            alert_value += f"\nПоследняя проверка: {_discord_time(alert.get('last_evaluated_source_at'))}"
        if alert.get("last_delivery_error"):
            alert_value += "\n⚠️ Доставка в ЛС не удалась; сигнал поставлен на паузу."
        embed.add_field(name="🔔 Ваш личный сигнал", value=alert_value, inline=False)
    else:
        embed.add_field(
            name="🔕 Личный сигнал не настроен",
            value=(
                "Можно получить ЛС, когда новый срез покажет подходящую цену и количество."
                + (" Для одежды количество = продано за период." if category == "clothes" else "")
            ),
            inline=False,
        )
    embed.add_field(
        name="🕒 Свежесть данных",
        value=(
            f"Majestic обновил срез: {_discord_time(item.get('source_updated_at'))}\n"
            f"T-Mod проверил API: {_discord_time(status.get('last_success_at'))}"
        ),
        inline=False,
    )
    embed.set_footer(text="Не является live-объявлением • сохранено в локальном каталоге T-Mod")
    return embed


def market_alerts_embed(alerts: list[dict[str, Any]], status: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title="🔔 Мои рыночные сигналы",
        description=(
            "Сигнал проверяется только при появлении нового статистического среза Majestic. "
            "После успешного ЛС он завершается — повторного спама по тем же данным не будет."
        ),
        color=MARKET_EMBED_COLOR,
    )
    if not alerts:
        embed.add_field(
            name="Пока пусто",
            value="Найдите рыночную позицию, откройте карточку и нажмите **«Настроить сигнал»**.",
            inline=False,
        )
    else:
        lines = []
        for alert in alerts[:MARKET_MAX_ALERTS_PER_USER]:
            item_id = int(alert.get("item_id") or 0)
            category = _clean_market_category(alert.get("category"))
            info = _category_info(category)
            item_name = str(alert.get("item_name") or f"Запись {item_id}")[:70]
            lines.append(
                f"{info['emoji']} **{item_name}** · `{_item_identity(alert)}`\n"
                f"{_alert_status_text(alert)} · ≤ **{_price(alert.get('target_price'))}** · "
                f"от **{_number(alert.get('min_quantity'))} шт.**"
            )
        embed.add_field(name=f"Сигналов: {len(alerts)} / {MARKET_MAX_ALERTS_PER_USER}", value="\n\n".join(lines)[:4000], inline=False)
    embed.add_field(
        name="Свежесть выбранного раздела",
        value=(
            f"Текущий срез: {_discord_time(status.get('source_updated_at'))}\n"
            f"T-Mod проверяет API раз в **{MARKET_REFRESH_SECONDS} сек.**, но Majestic может обновлять сам срез реже."
        ),
        inline=False,
    )
    embed.set_footer(text="Выберите сигнал ниже, чтобы открыть позицию и изменить его")
    return embed


def market_alert_dm_embed(notification: dict[str, Any]) -> discord.Embed:
    category = _clean_market_category(notification.get("category"))
    info = _category_info(category)
    embed = discord.Embed(
        title="🔔 Рыночный сигнал сработал",
        description=(
            f"Новый срез **{notification.get('server_id') or MARKET_SERVER_ID}** подходит под ваши условия "
            f"для {info['singular'].lower()} **{str(notification.get('item_name') or 'Без названия')[:180]}**."
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Найдено в новом срезе",
        value=(
            f"Минимальная статистическая цена: **{_price(notification.get('observed_price'))}**\n"
            f"{_quantity_label(category)}: **{_number(notification.get('observed_quantity'))} шт.**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Ваше условие",
        value=(
            f"Цена не выше: **{_price(notification.get('target_price'))}**\n"
            f"Количество: **от {_number(notification.get('min_quantity'))} шт.**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Актуальность",
        value=f"Срез Majestic: {_discord_time(notification.get('source_updated_at'))}",
        inline=False,
    )
    embed.add_field(
        name="Что дальше",
        value=(
            f"Сигнал `{_item_identity(notification)}` завершён после этого ЛС. "
            "Чтобы следить дальше, откройте `/market` → **«Мои сигналы»** и включите его снова."
        ),
        inline=False,
    )
    embed.set_footer(text="Статистика Majestic, не live-объявление • T-Mod не повторяет один и тот же срез")
    return embed


class MarketBaseView(discord.ui.View):
    def __init__(self, requester_id: int, *, back_to_tvrs: bool = False) -> None:
        super().__init__(timeout=900)
        self.requester_id = int(requester_id)
        self.back_to_tvrs = bool(back_to_tvrs)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Это личное рыночное меню открыто не для вас.", ephemeral=True)
            return False
        return True


class MarketCategorySelect(discord.ui.Select):
    def __init__(self, requester_id: int, category: str, *, back_to_tvrs: bool, row: int = 0) -> None:
        self.requester_id = int(requester_id)
        self.back_to_tvrs = bool(back_to_tvrs)
        category = _clean_market_category(category)
        options = [
            discord.SelectOption(
                label=info["label"],
                value=key,
                emoji=info["emoji"],
                description=f"Искать: {info['search_hint']}"[:100],
                default=key == category,
            )
            for key, info in MARKET_CATEGORY_INFO.items()
        ]
        super().__init__(
            placeholder="Выберите раздел рынка",
            min_values=1,
            max_values=1,
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await _edit_market_home(
            interaction,
            self.requester_id,
            self.back_to_tvrs,
            category=self.values[0],
        )


async def _edit_market_home(
    interaction: discord.Interaction,
    requester_id: int,
    back_to_tvrs: bool,
    *,
    category: str = MARKET_CATEGORY,
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    category = _clean_market_category(category)
    catalog = get_market_catalog(category)
    status = await catalog.ensure_ready()
    await interaction.edit_original_response(
        content=None,
        embed=market_home_embed(status, category),
        view=MarketHomeView(requester_id, category=category, back_to_tvrs=back_to_tvrs),
    )


async def _show_item(
    interaction: discord.Interaction,
    requester_id: int,
    item_id: int,
    *,
    category: str = MARKET_CATEGORY,
    query: str | None,
    back_to_tvrs: bool,
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    category = _clean_market_category(category)
    catalog = get_market_catalog(category)
    item, status, history, alert = await asyncio.gather(
        asyncio.to_thread(catalog.get_item, item_id),
        asyncio.to_thread(catalog.status),
        asyncio.to_thread(catalog.history, item_id, 30),
        asyncio.to_thread(catalog.alert, requester_id, item_id),
    )
    if item is None:
        await interaction.edit_original_response(
            embed=market_empty_embed(str(item_id), status, category),
            view=MarketHomeView(requester_id, category=category, back_to_tvrs=back_to_tvrs),
        )
        return
    await interaction.edit_original_response(
        content=None,
        embed=market_item_embed(item, status, history, alert),
        view=MarketItemView(
            requester_id,
            int(item["item_id"]),
            category=category,
            query=query,
            back_to_tvrs=back_to_tvrs,
            alert=alert,
        ),
    )


class MarketSearchModal(discord.ui.Modal):
    def __init__(
        self,
        requester_id: int,
        *,
        category: str = MARKET_CATEGORY,
        back_to_tvrs: bool = False,
    ) -> None:
        self.category = _clean_market_category(category)
        info = _category_info(self.category)
        super().__init__(title=f"Поиск · {info['label']} · RU15"[:45])
        self.requester_id = int(requester_id)
        self.back_to_tvrs = bool(back_to_tvrs)
        self.query = discord.ui.TextInput(
            label=f"Поиск: {info['singular'].lower()}"[:45],
            placeholder=f"Например: {info['search_hint']}"[:100],
            min_length=1,
            max_length=80,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Это личное рыночное меню открыто не для вас.", ephemeral=True)
            return
        await interaction.response.defer()
        clean_query = str(self.query.value).strip()
        catalog = get_market_catalog(self.category)
        await catalog.ensure_ready()
        hits, status = await asyncio.gather(
            asyncio.to_thread(catalog.search, clean_query, MARKET_SEARCH_LIMIT),
            asyncio.to_thread(catalog.status),
        )
        if hits and hits[0].score >= 15_000:
            item = hits[0].item
            history, alert = await asyncio.gather(
                asyncio.to_thread(catalog.history, int(item["item_id"]), 30),
                asyncio.to_thread(catalog.alert, self.requester_id, int(item["item_id"])),
            )
            await interaction.edit_original_response(
                content=None,
                embed=market_item_embed(item, status, history, alert),
                view=MarketItemView(
                    self.requester_id,
                    int(item["item_id"]),
                    category=self.category,
                    query=clean_query,
                    back_to_tvrs=self.back_to_tvrs,
                    alert=alert,
                ),
            )
            return
        if not hits:
            await interaction.edit_original_response(
                content=None,
                embed=market_empty_embed(clean_query, status, self.category),
                view=MarketHomeView(
                    self.requester_id,
                    category=self.category,
                    back_to_tvrs=self.back_to_tvrs,
                ),
            )
            return
        await interaction.edit_original_response(
            content=None,
            embed=market_results_embed(clean_query, hits, status, self.category),
            view=MarketResultsView(
                self.requester_id,
                clean_query,
                hits,
                category=self.category,
                back_to_tvrs=self.back_to_tvrs,
            ),
        )


class MarketAlertModal(discord.ui.Modal):
    def __init__(
        self,
        requester_id: int,
        item: dict[str, Any],
        *,
        category: str | None = None,
        query: str | None,
        back_to_tvrs: bool,
        alert: dict[str, Any] | None,
    ) -> None:
        item_name = str(item.get("item_name") or f"Позиция {int(item.get('item_id') or 0)}")
        super().__init__(title=f"Сигнал · {item_name}"[:45])
        self.requester_id = int(requester_id)
        self.item_id = int(item.get("item_id") or 0)
        self.category = _clean_market_category(category or item.get("category"))
        self.query = str(query or "")
        self.back_to_tvrs = bool(back_to_tvrs)
        suggested_price = alert.get("target_price") if alert else _observed_market_price(item)
        self.target_price = discord.ui.TextInput(
            label="Максимальная цена за штуку, $",
            placeholder="Например: 25 000",
            default=str(int(suggested_price)) if suggested_price else None,
            min_length=1,
            max_length=20,
        )
        self.min_quantity = discord.ui.TextInput(
            label="Минимальное количество в срезе",
            placeholder="Например: 10",
            default=str(int(alert.get("min_quantity") or 1)) if alert else "1",
            min_length=1,
            max_length=20,
        )
        self.add_item(self.target_price)
        self.add_item(self.min_quantity)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Это личное рыночное меню открыто не для вас.", ephemeral=True)
            return
        try:
            target_price = _positive_integer(str(self.target_price.value), "Цена")
            min_quantity = _positive_integer(str(self.min_quantity.value), "Количество")
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        catalog = get_market_catalog(self.category)
        item = await asyncio.to_thread(catalog.get_item, self.item_id)
        if item is None:
            await interaction.response.send_message("Позиция уже отсутствует в текущем каталоге.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await asyncio.to_thread(
                storage.market_upsert_alert,
                discord_user_id=self.requester_id,
                user_display=getattr(interaction.user, "display_name", str(interaction.user)),
                guild_id=interaction.guild_id,
                server_id=MARKET_SERVER_ID,
                category=self.category,
                item_id=self.item_id,
                target_price=target_price,
                min_quantity=min_quantity,
                current_source_updated_at=item.get("source_updated_at"),
                max_alerts_per_user=MARKET_MAX_ALERTS_PER_USER,
            )
        except ValueError as exc:
            message = {
                "market_alert_limit_reached": f"Достигнут лимит: {MARKET_MAX_ALERTS_PER_USER} сигналов на пользователя.",
                "market_alert_item_not_found": "Позиция уже отсутствует в текущем каталоге.",
            }.get(str(exc), "Не удалось сохранить сигнал. Проверьте введённые значения.")
            await interaction.followup.send(message, ephemeral=True)
            return
        await _show_item(
            interaction,
            self.requester_id,
            self.item_id,
            category=self.category,
            query=self.query,
            back_to_tvrs=self.back_to_tvrs,
        )


class MarketHomeView(MarketBaseView):
    def __init__(
        self,
        requester_id: int,
        *,
        category: str = MARKET_CATEGORY,
        back_to_tvrs: bool = False,
    ) -> None:
        super().__init__(requester_id, back_to_tvrs=back_to_tvrs)
        self.category = _clean_market_category(category)
        self.add_item(
            MarketCategorySelect(
                requester_id,
                self.category,
                back_to_tvrs=back_to_tvrs,
                row=0,
            )
        )
        self.search.label = f"Найти {_category_info(self.category)['singular'].lower()}"[:80]
        if not back_to_tvrs:
            self.remove_item(self.tvrs)

    @discord.ui.button(label="Найти", emoji="🔎", style=discord.ButtonStyle.primary, row=1)
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            MarketSearchModal(
                self.requester_id,
                category=self.category,
                back_to_tvrs=self.back_to_tvrs,
            )
        )

    @discord.ui.button(label="Популярное", emoji="🔥", style=discord.ButtonStyle.secondary, row=1)
    async def popular(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        catalog = get_market_catalog(self.category)
        hits, status = await asyncio.gather(
            asyncio.to_thread(catalog.popular, 15),
            asyncio.to_thread(catalog.status),
        )
        if not hits:
            status = await catalog.ensure_ready()
            hits = await asyncio.to_thread(catalog.popular, 15)
        if not hits:
            await interaction.edit_original_response(
                embed=market_empty_embed("популярное", status, self.category),
                view=self,
            )
            return
        await interaction.edit_original_response(
            embed=market_results_embed(MARKET_POPULAR_QUERY, hits, status, self.category),
            view=MarketResultsView(
                self.requester_id,
                MARKET_POPULAR_QUERY,
                hits,
                category=self.category,
                back_to_tvrs=self.back_to_tvrs,
            ),
        )

    @discord.ui.button(label="Мои сигналы", emoji="🔔", style=discord.ButtonStyle.secondary, row=1)
    async def alerts(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        alerts, status = await asyncio.gather(
            asyncio.to_thread(storage.market_list_user_alerts, self.requester_id, MARKET_MAX_ALERTS_PER_USER),
            asyncio.to_thread(get_market_catalog(self.category).status),
        )
        await interaction.edit_original_response(
            content=None,
            embed=market_alerts_embed(alerts, status),
            view=MarketAlertsView(
                self.requester_id,
                alerts,
                category=self.category,
                back_to_tvrs=self.back_to_tvrs,
            ),
        )

    @discord.ui.button(label="Обновить меню", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_market_home(
            interaction,
            self.requester_id,
            self.back_to_tvrs,
            category=self.category,
        )

    @discord.ui.button(label="В Товарищество", emoji="🏛️", style=discord.ButtonStyle.secondary, row=2)
    async def tvrs(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from modules.tvrs import open_tvrs_hub

        await open_tvrs_hub(interaction)


class MarketItemSelect(discord.ui.Select):
    def __init__(
        self,
        requester_id: int,
        query: str,
        hits: list[MarketSearchHit],
        *,
        category: str = MARKET_CATEGORY,
        back_to_tvrs: bool,
    ) -> None:
        self.requester_id = int(requester_id)
        self.query = query
        self.category = _clean_market_category(category)
        self.back_to_tvrs = back_to_tvrs
        options = []
        for hit in hits[:MARKET_SEARCH_LIMIT]:
            item = hit.item
            options.append(
                discord.SelectOption(
                    label=str(item.get("item_name") or "Без названия")[:100],
                    value=str(int(item.get("item_id") or 0)),
                    description=(
                        f"{_item_identity(item)} · ср. {_price(item.get('average_price'))} · продаж {_number(item.get('sold_count'))}"
                    )[:100],
                    emoji=_category_info(self.category)["emoji"],
                )
            )
        super().__init__(
            placeholder=f"Выберите: {_category_info(self.category)['label'].lower()}"[:150],
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await _show_item(
            interaction,
            self.requester_id,
            int(self.values[0]),
            category=self.category,
            query=self.query,
            back_to_tvrs=self.back_to_tvrs,
        )


class MarketAlertSelect(discord.ui.Select):
    def __init__(self, requester_id: int, alerts: list[dict[str, Any]], *, back_to_tvrs: bool) -> None:
        self.requester_id = int(requester_id)
        self.back_to_tvrs = bool(back_to_tvrs)
        options = []
        for alert in alerts[:25]:
            item_id = int(alert.get("item_id") or 0)
            category = _clean_market_category(alert.get("category"))
            options.append(
                discord.SelectOption(
                    label=str(alert.get("item_name") or f"Запись {item_id}")[:100],
                    value=f"{category}:{item_id}",
                    description=(
                        f"{_alert_status_text(alert)} · до {_price(alert.get('target_price'))} · "
                        f"от {_number(alert.get('min_quantity'))} шт."
                    )[:100],
                    emoji=_category_info(category)["emoji"],
                )
            )
        super().__init__(
            placeholder="Выберите сигнал для настройки",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        category, raw_item_id = self.values[0].split(":", 1)
        await _show_item(
            interaction,
            self.requester_id,
            int(raw_item_id),
            category=category,
            query=None,
            back_to_tvrs=self.back_to_tvrs,
        )


class MarketAlertsView(MarketBaseView):
    def __init__(
        self,
        requester_id: int,
        alerts: list[dict[str, Any]],
        *,
        category: str = MARKET_CATEGORY,
        back_to_tvrs: bool = False,
    ) -> None:
        super().__init__(requester_id, back_to_tvrs=back_to_tvrs)
        self.category = _clean_market_category(category)
        if alerts:
            self.add_item(MarketAlertSelect(requester_id, alerts, back_to_tvrs=back_to_tvrs))

    @discord.ui.button(label="Новый поиск", emoji="🔎", style=discord.ButtonStyle.primary, row=1)
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            MarketSearchModal(
                self.requester_id,
                category=self.category,
                back_to_tvrs=self.back_to_tvrs,
            )
        )

    @discord.ui.button(label="Главная рынка", emoji="📈", style=discord.ButtonStyle.secondary, row=1)
    async def home(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_market_home(
            interaction,
            self.requester_id,
            self.back_to_tvrs,
            category=self.category,
        )


class MarketResultsView(MarketBaseView):
    def __init__(
        self,
        requester_id: int,
        query: str,
        hits: list[MarketSearchHit],
        *,
        category: str = MARKET_CATEGORY,
        back_to_tvrs: bool = False,
    ) -> None:
        super().__init__(requester_id, back_to_tvrs=back_to_tvrs)
        self.query = str(query)
        self.category = _clean_market_category(category)
        self.add_item(
            MarketItemSelect(
                requester_id,
                self.query,
                hits,
                category=self.category,
                back_to_tvrs=back_to_tvrs,
            )
        )

    @discord.ui.button(label="Новый поиск", emoji="🔎", style=discord.ButtonStyle.primary, row=1)
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            MarketSearchModal(
                self.requester_id,
                category=self.category,
                back_to_tvrs=self.back_to_tvrs,
            )
        )

    @discord.ui.button(label="Главная рынка", emoji="📈", style=discord.ButtonStyle.secondary, row=1)
    async def home(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_market_home(
            interaction,
            self.requester_id,
            self.back_to_tvrs,
            category=self.category,
        )


class MarketItemView(MarketBaseView):
    def __init__(
        self,
        requester_id: int,
        item_id: int,
        *,
        category: str = MARKET_CATEGORY,
        query: str | None,
        back_to_tvrs: bool = False,
        alert: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(requester_id, back_to_tvrs=back_to_tvrs)
        self.item_id = int(item_id)
        self.category = _clean_market_category(category)
        self.query = str(query or "")
        self.alert = alert
        if not self.query:
            self.remove_item(self.results)
        if alert is None:
            self.remove_item(self.pause_alert)
            self.remove_item(self.resume_alert)
            self.remove_item(self.delete_alert)
        else:
            self.configure_alert.label = "Изменить сигнал"
            if str(alert.get("status") or "") in {"active", "notifying"}:
                self.remove_item(self.resume_alert)
            else:
                self.remove_item(self.pause_alert)

    @discord.ui.button(label="К результатам", emoji="⬅️", style=discord.ButtonStyle.secondary, row=0)
    async def results(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        catalog = get_market_catalog(self.category)
        loader = catalog.popular if self.query == MARKET_POPULAR_QUERY else catalog.search
        hits, status = await asyncio.gather(
            (
                asyncio.to_thread(loader, MARKET_SEARCH_LIMIT)
                if self.query == MARKET_POPULAR_QUERY
                else asyncio.to_thread(loader, self.query, MARKET_SEARCH_LIMIT)
            ),
            asyncio.to_thread(catalog.status),
        )
        if not hits:
            await _edit_market_home(
                interaction,
                self.requester_id,
                self.back_to_tvrs,
                category=self.category,
            )
            return
        await interaction.response.edit_message(
            embed=market_results_embed(self.query, hits, status, self.category),
            view=MarketResultsView(
                self.requester_id,
                self.query,
                hits,
                category=self.category,
                back_to_tvrs=self.back_to_tvrs,
            ),
        )

    @discord.ui.button(label="Новый поиск", emoji="🔎", style=discord.ButtonStyle.primary, row=0)
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            MarketSearchModal(
                self.requester_id,
                category=self.category,
                back_to_tvrs=self.back_to_tvrs,
            )
        )

    @discord.ui.button(label="Главная рынка", emoji="📈", style=discord.ButtonStyle.secondary, row=0)
    async def home(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_market_home(
            interaction,
            self.requester_id,
            self.back_to_tvrs,
            category=self.category,
        )

    @discord.ui.button(label="Настроить сигнал", emoji="🔔", style=discord.ButtonStyle.primary, row=1)
    async def configure_alert(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        item = await asyncio.to_thread(get_market_catalog(self.category).get_item, self.item_id)
        if item is None:
            await interaction.response.send_message("Позиция уже отсутствует в текущем каталоге.", ephemeral=True)
            return
        await interaction.response.send_modal(
            MarketAlertModal(
                self.requester_id,
                item,
                category=self.category,
                query=self.query,
                back_to_tvrs=self.back_to_tvrs,
                alert=self.alert,
            )
        )

    @discord.ui.button(label="Пауза", emoji="⏸️", style=discord.ButtonStyle.secondary, row=1)
    async def pause_alert(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.alert is None:
            return
        await interaction.response.defer()
        await asyncio.to_thread(
            storage.market_set_alert_status,
            self.requester_id,
            int(self.alert["id"]),
            "paused",
        )
        await _show_item(
            interaction,
            self.requester_id,
            self.item_id,
            category=self.category,
            query=self.query,
            back_to_tvrs=self.back_to_tvrs,
        )

    @discord.ui.button(label="Включить снова", emoji="▶️", style=discord.ButtonStyle.success, row=1)
    async def resume_alert(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.alert is None:
            return
        await interaction.response.defer()
        item = await asyncio.to_thread(get_market_catalog(self.category).get_item, self.item_id)
        await asyncio.to_thread(
            storage.market_set_alert_status,
            self.requester_id,
            int(self.alert["id"]),
            "active",
            current_source_updated_at=(item or {}).get("source_updated_at"),
        )
        await _show_item(
            interaction,
            self.requester_id,
            self.item_id,
            category=self.category,
            query=self.query,
            back_to_tvrs=self.back_to_tvrs,
        )

    @discord.ui.button(label="Удалить сигнал", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def delete_alert(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.alert is None:
            return
        await interaction.response.defer()
        await asyncio.to_thread(
            storage.market_delete_alert,
            self.requester_id,
            int(self.alert["id"]),
        )
        await _show_item(
            interaction,
            self.requester_id,
            self.item_id,
            category=self.category,
            query=self.query,
            back_to_tvrs=self.back_to_tvrs,
        )


async def dispatch_market_alerts(bot: commands.Bot) -> None:
    notifications = await asyncio.to_thread(storage.market_pending_alert_notifications, 25)
    for notification in notifications:
        try:
            user_id = int(notification["discord_user_id"])
            user = bot.get_user(user_id)
            if user is None:
                user = await bot.fetch_user(user_id)
            message = await user.send(
                embed=market_alert_dm_embed(notification),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await asyncio.to_thread(
                storage.market_mark_alert_delivery,
                int(notification["id"]),
                delivered=False,
                error=f"{type(exc).__name__}: {str(exc)[:350]}",
                max_attempts=MARKET_ALERT_MAX_DELIVERY_ATTEMPTS,
                retry_seconds=MARKET_ALERT_RETRY_SECONDS,
            )
            continue
        await asyncio.to_thread(
            storage.market_mark_alert_delivery,
            int(notification["id"]),
            delivered=True,
            dm_message_id=message.id,
            max_attempts=MARKET_ALERT_MAX_DELIVERY_ATTEMPTS,
            retry_seconds=MARKET_ALERT_RETRY_SECONDS,
        )


async def market_worker(bot: commands.Bot) -> None:
    client = get_majestic_api_client()
    while not bot.is_closed():
        started = asyncio.get_running_loop().time()
        if client.config.enabled and client.config.api_keys:
            for category, catalog in market_catalogs.items():
                try:
                    before = await asyncio.to_thread(catalog.status)
                    status = await catalog.sync(use_cache=False)
                    await asyncio.to_thread(
                        storage.market_evaluate_alerts,
                        MARKET_SERVER_ID,
                        str(status.get("source_updated_at") or ""),
                        category,
                    )
                    if int(before.get("consecutive_failures") or 0) > 0:
                        for guild in bot.guilds:
                            await log_technical_event(
                                bot,
                                guild,
                                title="Majestic API восстановлен",
                                details=(
                                    f"{_category_info(category)['label']}: каталог {MARKET_SERVER_ID} "
                                    f"снова обновляется, записей {int(status.get('record_count') or 0)}."
                                ),
                                level="info",
                                dedupe_key=f"market-sync-recovered:{category}",
                                cooldown_seconds=600,
                            )
                except asyncio.CancelledError:
                    raise
                except (MajesticApiDisabledError, MajesticApiConfigurationError):
                    break
                except Exception as exc:
                    traceback.print_exc()
                    await asyncio.to_thread(
                        storage.market_record_sync_error,
                        MARKET_SERVER_ID,
                        category,
                        f"{type(exc).__name__}: {str(exc)[:350]}",
                    )
                    for guild in bot.guilds:
                        await log_technical_event(
                            bot,
                            guild,
                            title="Ошибка синхронизации рынка",
                            details=(
                                f"{MARKET_SERVER_ID}/{category}: `{type(exc).__name__}`. "
                                "Локальный каталог продолжает работать."
                            ),
                            level="warning",
                            dedupe_key=f"market-sync:{category}:{type(exc).__name__}",
                            cooldown_seconds=900,
                        )
        try:
            await dispatch_market_alerts(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.sleep(max(1.0, MARKET_REFRESH_SECONDS - elapsed))


async def _send_market_response(
    interaction: discord.Interaction,
    *,
    query: str | None,
    category: str = MARKET_CATEGORY,
    back_to_tvrs: bool,
) -> None:
    category = _clean_market_category(category)
    catalog = get_market_catalog(category)
    status = await catalog.ensure_ready()
    clean_query = str(query or "").strip()
    if not clean_query:
        await interaction.followup.send(
            embed=market_home_embed(status, category),
            view=MarketHomeView(
                interaction.user.id,
                category=category,
                back_to_tvrs=back_to_tvrs,
            ),
            ephemeral=True,
        )
        return
    hits = await asyncio.to_thread(catalog.search, clean_query, MARKET_SEARCH_LIMIT)
    if hits and hits[0].score >= 15_000:
        item = hits[0].item
        history, alert = await asyncio.gather(
            asyncio.to_thread(catalog.history, int(item["item_id"]), 30),
            asyncio.to_thread(catalog.alert, interaction.user.id, int(item["item_id"])),
        )
        await interaction.followup.send(
            embed=market_item_embed(item, status, history, alert),
            view=MarketItemView(
                interaction.user.id,
                int(item["item_id"]),
                category=category,
                query=clean_query,
                back_to_tvrs=back_to_tvrs,
                alert=alert,
            ),
            ephemeral=True,
        )
        return
    if not hits:
        await interaction.followup.send(
            embed=market_empty_embed(clean_query, status, category),
            view=MarketHomeView(
                interaction.user.id,
                category=category,
                back_to_tvrs=back_to_tvrs,
            ),
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        embed=market_results_embed(clean_query, hits, status, category),
        view=MarketResultsView(
            interaction.user.id,
            clean_query,
            hits,
            category=category,
            back_to_tvrs=back_to_tvrs,
        ),
        ephemeral=True,
    )


async def send_market_panel(interaction: discord.Interaction, *, back_to_tvrs: bool = False) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    await _send_market_response(
        interaction,
        query=None,
        category=MARKET_CATEGORY,
        back_to_tvrs=back_to_tvrs,
    )


def setup_market(
    bot: commands.Bot,
    remember_command_activity: Callable[[discord.Interaction, str, str], None] | None = None,
) -> None:
    @bot.tree.command(name="market", description="Найти предмет, автомобиль или одежду на рынке RU15")
    @app_commands.describe(
        category="Раздел рынка",
        query="Название, часть слова, ID или model",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name=info["label"], value=key)
            for key, info in MARKET_CATEGORY_INFO.items()
        ]
    )
    async def market(
        interaction: discord.Interaction,
        category: app_commands.Choice[str] | None = None,
        query: str | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда работает в любом канале сервера.", ephemeral=True)
            return
        if remember_command_activity is not None:
            remember_command_activity(interaction, "command_market", "/market")
        await interaction.response.defer(ephemeral=True, thinking=True)
        selected_category = category.value if category is not None else MARKET_CATEGORY
        await _send_market_response(
            interaction,
            query=query,
            category=selected_category,
            back_to_tvrs=False,
        )

    @market.autocomplete("query")
    async def market_query_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        selected = getattr(getattr(interaction, "namespace", None), "category", None)
        if isinstance(selected, app_commands.Choice):
            selected = selected.value
        catalog = get_market_catalog(str(selected or MARKET_CATEGORY))
        if normalize_market_text(current):
            hits = await asyncio.to_thread(catalog.search, current, MARKET_SEARCH_LIMIT)
        else:
            hits = await asyncio.to_thread(catalog.popular, MARKET_SEARCH_LIMIT)
        return [
            app_commands.Choice(
                name=(
                    f"{str(hit.item.get('item_name') or 'Без названия')} · "
                    f"средняя {_price(hit.item.get('average_price'))}"
                )[:100],
                value=(
                    f"#{int(hit.item.get('item_id') or 0)}"
                    if catalog.category == "items"
                    else str(hit.item.get("external_id") or hit.item.get("item_name") or "")[:100]
                ),
            )
            for hit in hits[:MARKET_SEARCH_LIMIT]
        ]

    async def market_ready_listener() -> None:
        global _worker_task
        if _worker_task is None or _worker_task.done():
            _worker_task = asyncio.create_task(market_worker(bot), name="tmod-market-worker")

    bot.add_listener(market_ready_listener, "on_ready")
