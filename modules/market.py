from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
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


def _market_search_score(query: str, item: dict[str, Any]) -> int:
    name = str(item.get("normalized_name") or normalize_market_text(item.get("item_name") or ""))
    if not query or not name:
        return 0
    raw_id = str(item.get("item_id") or "")
    if query.lstrip("#") == raw_id and (query.startswith("#") or query.isdigit()):
        return 20_000
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
            snapshot = await client.marketplace_items_snapshot_async(
                self.server_id,
                use_cache=use_cache,
            )
            fetched_at = datetime.now(timezone.utc).isoformat()
            source_updated_at = snapshot.summary.last_updated or fetched_at
            rows = [
                {
                    "item_id": item.item_id,
                    "item_name": item.item_name,
                    "normalized_name": normalize_market_text(item.item_name),
                    "total_count": item.total_count,
                    "sold_count": item.sold_count,
                    "average_price": item.average_price,
                    "min_price": item.min_price,
                    "max_price": item.max_price,
                }
                for item in snapshot.items
            ]
            previous = await asyncio.to_thread(self.status)
            previous_count = int(previous.get("record_count") or 0)
            if previous_count >= 100 and len(rows) * 2 < previous_count:
                raise MajesticApiResponseError(
                    f"Majestic вернул только {len(rows)} из прежних {previous_count} предметов; "
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


market_catalog = MarketCatalogService()


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


def market_home_embed(status: dict[str, Any]) -> discord.Embed:
    ready = int(status.get("record_count") or 0) > 0
    server_name = str(status.get("server_name") or "Phoenix")
    embed = discord.Embed(
        title=f"📈 Рынок {MARKET_SERVER_ID} · {server_name}",
        description=(
            "Личный поиск по каталогу Majestic. Введите русское название, часть слова, ID или даже "
            "небольшую опечатку — T-Mod найдёт подходящие предметы локально, без отдельного запроса к API."
        ),
        color=MARKET_EMBED_COLOR if ready else discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    if ready:
        embed.add_field(
            name="📚 Каталог T-Mod",
            value=(
                f"Предметов: **{_number(int(status.get('record_count') or 0))}**\n"
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
        name="Как искать",
        value="Например: `железная руда`, `аптечка`, `пром металлы`, `#39`.",
        inline=False,
    )
    embed.add_field(
        name="🔔 Личные сигналы",
        value=(
            "В карточке предмета можно задать максимальную цену и минимальное количество. "
            "T-Mod проверит условие один раз на каждом **новом срезе Majestic** и отправит личное сообщение."
        ),
        inline=False,
    )
    embed.add_field(
        name="Важно о ценах",
        value=(
            "Это статистический ориентир Majestic за период, а не список активных объявлений в эту секунду. "
            "В карточке всегда показаны период и свежесть среза."
        ),
        inline=False,
    )
    embed.set_footer(text="Меню видно только вам • поиск и просмотр сигналов не расходуют API-запросы")
    return embed


def market_results_embed(query: str, hits: list[MarketSearchHit], status: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔎 Результаты: {query[:80]}",
        description=(
            "Выберите предмет в списке под карточкой. Результаты отсортированы по точности названия, "
            "а при равенстве — по объёму продаж."
        ),
        color=MARKET_EMBED_COLOR,
    )
    lines = []
    for index, hit in enumerate(hits[:10], 1):
        item = hit.item
        lines.append(
            f"`{index:02d}` **{str(item.get('item_name') or 'Без названия')[:70]}**\n"
            f"      средняя **{_price(item.get('average_price'))}** · продаж **{_number(item.get('sold_count'))}**"
        )
    embed.add_field(name=f"Найдено вариантов: {len(hits)}", value="\n".join(lines)[:4000], inline=False)
    embed.add_field(
        name="Актуальность",
        value=f"Срез Majestic обновлён {_discord_time(status.get('source_updated_at'))}.",
        inline=False,
    )
    embed.set_footer(text="Подробная карточка откроется лично в этом же меню")
    return embed


def market_empty_embed(query: str, status: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title="🔍 Ничего не найдено",
        description=(
            f"По запросу **{query[:100]}** нет уверенных совпадений. Попробуйте часть названия, "
            "например `руда`, `аптеч` или `ткань`."
        ),
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="Каталог",
        value=(
            f"Загружено предметов: **{_number(int(status.get('record_count') or 0))}** · "
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
    name = str(item.get("item_name") or "Без названия")
    embed = discord.Embed(
        title=f"📦 {name[:240]}",
        description=f"Предмет `#{int(item.get('item_id') or 0)}` · **{MARKET_SERVER_ID} · {status.get('server_name') or 'Phoenix'}**",
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
    embed.add_field(
        name="📊 Активность за период",
        value=(
            f"Размещено: **{_number(item.get('total_count'))}**\n"
            f"Продано: **{_number(item.get('sold_count'))}**\n"
            f"Период: **{int(status.get('period_days') or 0) or '—'} дней**"
        ),
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
            value="Можно получить ЛС, когда новый срез покажет подходящую цену и количество.",
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
            value="Найдите предмет, откройте карточку и нажмите **«Настроить сигнал»**.",
            inline=False,
        )
    else:
        lines = []
        for alert in alerts[:MARKET_MAX_ALERTS_PER_USER]:
            item_id = int(alert.get("item_id") or 0)
            item_name = str(alert.get("item_name") or f"Предмет #{item_id}")[:70]
            lines.append(
                f"**{item_name}** `#{item_id}`\n"
                f"{_alert_status_text(alert)} · ≤ **{_price(alert.get('target_price'))}** · "
                f"от **{_number(alert.get('min_quantity'))} шт.**"
            )
        embed.add_field(name=f"Сигналов: {len(alerts)} / {MARKET_MAX_ALERTS_PER_USER}", value="\n\n".join(lines)[:4000], inline=False)
    embed.add_field(
        name="Свежесть рынка",
        value=(
            f"Текущий срез: {_discord_time(status.get('source_updated_at'))}\n"
            f"T-Mod проверяет API раз в **{MARKET_REFRESH_SECONDS} сек.**, но Majestic может обновлять сам срез реже."
        ),
        inline=False,
    )
    embed.set_footer(text="Выберите сигнал ниже, чтобы открыть предмет и изменить его")
    return embed


def market_alert_dm_embed(notification: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title="🔔 Рыночный сигнал сработал",
        description=(
            f"Новый срез **{notification.get('server_id') or MARKET_SERVER_ID}** подходит под ваши условия "
            f"для предмета **{str(notification.get('item_name') or 'Без названия')[:180]}**."
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Найдено в новом срезе",
        value=(
            f"Минимальная статистическая цена: **{_price(notification.get('observed_price'))}**\n"
            f"Количество размещений: **{_number(notification.get('observed_quantity'))} шт.**"
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
            f"Сигнал `#{int(notification.get('item_id') or 0)}` завершён после этого ЛС. "
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


async def _edit_market_home(interaction: discord.Interaction, requester_id: int, back_to_tvrs: bool) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    status = await market_catalog.ensure_ready()
    await interaction.edit_original_response(
        content=None,
        embed=market_home_embed(status),
        view=MarketHomeView(requester_id, back_to_tvrs=back_to_tvrs),
    )


async def _show_item(
    interaction: discord.Interaction,
    requester_id: int,
    item_id: int,
    *,
    query: str | None,
    back_to_tvrs: bool,
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    item, status, history, alert = await asyncio.gather(
        asyncio.to_thread(market_catalog.get_item, item_id),
        asyncio.to_thread(market_catalog.status),
        asyncio.to_thread(market_catalog.history, item_id, 30),
        asyncio.to_thread(market_catalog.alert, requester_id, item_id),
    )
    if item is None:
        await interaction.edit_original_response(
            embed=market_empty_embed(f"#{item_id}", status),
            view=MarketHomeView(requester_id, back_to_tvrs=back_to_tvrs),
        )
        return
    await interaction.edit_original_response(
        content=None,
        embed=market_item_embed(item, status, history, alert),
        view=MarketItemView(
            requester_id,
            int(item["item_id"]),
            query=query,
            back_to_tvrs=back_to_tvrs,
            alert=alert,
        ),
    )


class MarketSearchModal(discord.ui.Modal, title="Поиск на рынке RU15"):
    query = discord.ui.TextInput(
        label="Название или ID предмета",
        placeholder="Например: железная руда, аптечка, #39",
        min_length=1,
        max_length=80,
    )

    def __init__(self, requester_id: int, *, back_to_tvrs: bool = False) -> None:
        super().__init__()
        self.requester_id = int(requester_id)
        self.back_to_tvrs = bool(back_to_tvrs)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Это личное рыночное меню открыто не для вас.", ephemeral=True)
            return
        await interaction.response.defer()
        clean_query = str(self.query.value).strip()
        await market_catalog.ensure_ready()
        hits, status = await asyncio.gather(
            asyncio.to_thread(market_catalog.search, clean_query, MARKET_SEARCH_LIMIT),
            asyncio.to_thread(market_catalog.status),
        )
        if hits and hits[0].score >= 15_000:
            item = hits[0].item
            history, alert = await asyncio.gather(
                asyncio.to_thread(market_catalog.history, int(item["item_id"]), 30),
                asyncio.to_thread(market_catalog.alert, self.requester_id, int(item["item_id"])),
            )
            await interaction.edit_original_response(
                content=None,
                embed=market_item_embed(item, status, history, alert),
                view=MarketItemView(
                    self.requester_id,
                    int(item["item_id"]),
                    query=clean_query,
                    back_to_tvrs=self.back_to_tvrs,
                    alert=alert,
                ),
            )
            return
        if not hits:
            await interaction.edit_original_response(
                content=None,
                embed=market_empty_embed(clean_query, status),
                view=MarketHomeView(self.requester_id, back_to_tvrs=self.back_to_tvrs),
            )
            return
        await interaction.edit_original_response(
            content=None,
            embed=market_results_embed(clean_query, hits, status),
            view=MarketResultsView(
                self.requester_id,
                clean_query,
                hits,
                back_to_tvrs=self.back_to_tvrs,
            ),
        )


class MarketAlertModal(discord.ui.Modal):
    def __init__(
        self,
        requester_id: int,
        item: dict[str, Any],
        *,
        query: str | None,
        back_to_tvrs: bool,
        alert: dict[str, Any] | None,
    ) -> None:
        item_name = str(item.get("item_name") or f"Предмет #{int(item.get('item_id') or 0)}")
        super().__init__(title=f"Сигнал · {item_name}"[:45])
        self.requester_id = int(requester_id)
        self.item_id = int(item.get("item_id") or 0)
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
        item = await asyncio.to_thread(market_catalog.get_item, self.item_id)
        if item is None:
            await interaction.response.send_message("Предмет уже отсутствует в текущем каталоге.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await asyncio.to_thread(
                storage.market_upsert_alert,
                discord_user_id=self.requester_id,
                user_display=getattr(interaction.user, "display_name", str(interaction.user)),
                guild_id=interaction.guild_id,
                server_id=MARKET_SERVER_ID,
                category=MARKET_CATEGORY,
                item_id=self.item_id,
                target_price=target_price,
                min_quantity=min_quantity,
                current_source_updated_at=item.get("source_updated_at"),
                max_alerts_per_user=MARKET_MAX_ALERTS_PER_USER,
            )
        except ValueError as exc:
            message = {
                "market_alert_limit_reached": f"Достигнут лимит: {MARKET_MAX_ALERTS_PER_USER} сигналов на пользователя.",
                "market_alert_item_not_found": "Предмет уже отсутствует в текущем каталоге.",
            }.get(str(exc), "Не удалось сохранить сигнал. Проверьте введённые значения.")
            await interaction.followup.send(message, ephemeral=True)
            return
        await _show_item(
            interaction,
            self.requester_id,
            self.item_id,
            query=self.query,
            back_to_tvrs=self.back_to_tvrs,
        )


class MarketHomeView(MarketBaseView):
    def __init__(self, requester_id: int, *, back_to_tvrs: bool = False) -> None:
        super().__init__(requester_id, back_to_tvrs=back_to_tvrs)
        if not back_to_tvrs:
            self.remove_item(self.tvrs)

    @discord.ui.button(label="Найти предмет", emoji="🔎", style=discord.ButtonStyle.primary, row=0)
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            MarketSearchModal(self.requester_id, back_to_tvrs=self.back_to_tvrs)
        )

    @discord.ui.button(label="Популярное", emoji="🔥", style=discord.ButtonStyle.secondary, row=0)
    async def popular(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        hits, status = await asyncio.gather(
            asyncio.to_thread(market_catalog.popular, 15),
            asyncio.to_thread(market_catalog.status),
        )
        if not hits:
            status = await market_catalog.ensure_ready()
            hits = await asyncio.to_thread(market_catalog.popular, 15)
        if not hits:
            await interaction.edit_original_response(
                embed=market_empty_embed("популярное", status),
                view=self,
            )
            return
        await interaction.edit_original_response(
            embed=market_results_embed(MARKET_POPULAR_QUERY, hits, status),
            view=MarketResultsView(
                self.requester_id,
                MARKET_POPULAR_QUERY,
                hits,
                back_to_tvrs=self.back_to_tvrs,
            ),
        )

    @discord.ui.button(label="Мои сигналы", emoji="🔔", style=discord.ButtonStyle.secondary, row=0)
    async def alerts(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        alerts, status = await asyncio.gather(
            asyncio.to_thread(market_catalog.alerts, self.requester_id),
            asyncio.to_thread(market_catalog.status),
        )
        await interaction.edit_original_response(
            content=None,
            embed=market_alerts_embed(alerts, status),
            view=MarketAlertsView(
                self.requester_id,
                alerts,
                back_to_tvrs=self.back_to_tvrs,
            ),
        )

    @discord.ui.button(label="Обновить меню", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_market_home(interaction, self.requester_id, self.back_to_tvrs)

    @discord.ui.button(label="В Товарищество", emoji="🏛️", style=discord.ButtonStyle.secondary, row=1)
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
        back_to_tvrs: bool,
    ) -> None:
        self.requester_id = int(requester_id)
        self.query = query
        self.back_to_tvrs = back_to_tvrs
        options = []
        for hit in hits[:MARKET_SEARCH_LIMIT]:
            item = hit.item
            options.append(
                discord.SelectOption(
                    label=str(item.get("item_name") or "Без названия")[:100],
                    value=str(int(item.get("item_id") or 0)),
                    description=(
                        f"Средняя {_price(item.get('average_price'))} · продаж {_number(item.get('sold_count'))}"
                    )[:100],
                    emoji="📦",
                )
            )
        super().__init__(
            placeholder="Выберите предмет для подробной карточки",
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
            options.append(
                discord.SelectOption(
                    label=str(alert.get("item_name") or f"Предмет #{item_id}")[:100],
                    value=str(item_id),
                    description=(
                        f"{_alert_status_text(alert)} · до {_price(alert.get('target_price'))} · "
                        f"от {_number(alert.get('min_quantity'))} шт."
                    )[:100],
                    emoji="🔔",
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
        await _show_item(
            interaction,
            self.requester_id,
            int(self.values[0]),
            query=None,
            back_to_tvrs=self.back_to_tvrs,
        )


class MarketAlertsView(MarketBaseView):
    def __init__(
        self,
        requester_id: int,
        alerts: list[dict[str, Any]],
        *,
        back_to_tvrs: bool = False,
    ) -> None:
        super().__init__(requester_id, back_to_tvrs=back_to_tvrs)
        if alerts:
            self.add_item(MarketAlertSelect(requester_id, alerts, back_to_tvrs=back_to_tvrs))

    @discord.ui.button(label="Новый поиск", emoji="🔎", style=discord.ButtonStyle.primary, row=1)
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            MarketSearchModal(self.requester_id, back_to_tvrs=self.back_to_tvrs)
        )

    @discord.ui.button(label="Главная рынка", emoji="📈", style=discord.ButtonStyle.secondary, row=1)
    async def home(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_market_home(interaction, self.requester_id, self.back_to_tvrs)


class MarketResultsView(MarketBaseView):
    def __init__(
        self,
        requester_id: int,
        query: str,
        hits: list[MarketSearchHit],
        *,
        back_to_tvrs: bool = False,
    ) -> None:
        super().__init__(requester_id, back_to_tvrs=back_to_tvrs)
        self.query = str(query)
        self.add_item(
            MarketItemSelect(
                requester_id,
                self.query,
                hits,
                back_to_tvrs=back_to_tvrs,
            )
        )

    @discord.ui.button(label="Новый поиск", emoji="🔎", style=discord.ButtonStyle.primary, row=1)
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            MarketSearchModal(self.requester_id, back_to_tvrs=self.back_to_tvrs)
        )

    @discord.ui.button(label="Главная рынка", emoji="📈", style=discord.ButtonStyle.secondary, row=1)
    async def home(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_market_home(interaction, self.requester_id, self.back_to_tvrs)


class MarketItemView(MarketBaseView):
    def __init__(
        self,
        requester_id: int,
        item_id: int,
        *,
        query: str | None,
        back_to_tvrs: bool = False,
        alert: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(requester_id, back_to_tvrs=back_to_tvrs)
        self.item_id = int(item_id)
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
        loader = market_catalog.popular if self.query == MARKET_POPULAR_QUERY else market_catalog.search
        hits, status = await asyncio.gather(
            (
                asyncio.to_thread(loader, MARKET_SEARCH_LIMIT)
                if self.query == MARKET_POPULAR_QUERY
                else asyncio.to_thread(loader, self.query, MARKET_SEARCH_LIMIT)
            ),
            asyncio.to_thread(market_catalog.status),
        )
        if not hits:
            await _edit_market_home(interaction, self.requester_id, self.back_to_tvrs)
            return
        await interaction.response.edit_message(
            embed=market_results_embed(self.query, hits, status),
            view=MarketResultsView(
                self.requester_id,
                self.query,
                hits,
                back_to_tvrs=self.back_to_tvrs,
            ),
        )

    @discord.ui.button(label="Новый поиск", emoji="🔎", style=discord.ButtonStyle.primary, row=0)
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            MarketSearchModal(self.requester_id, back_to_tvrs=self.back_to_tvrs)
        )

    @discord.ui.button(label="Главная рынка", emoji="📈", style=discord.ButtonStyle.secondary, row=0)
    async def home(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_market_home(interaction, self.requester_id, self.back_to_tvrs)

    @discord.ui.button(label="Настроить сигнал", emoji="🔔", style=discord.ButtonStyle.primary, row=1)
    async def configure_alert(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        item = await asyncio.to_thread(market_catalog.get_item, self.item_id)
        if item is None:
            await interaction.response.send_message("Предмет уже отсутствует в текущем каталоге.", ephemeral=True)
            return
        await interaction.response.send_modal(
            MarketAlertModal(
                self.requester_id,
                item,
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
            query=self.query,
            back_to_tvrs=self.back_to_tvrs,
        )

    @discord.ui.button(label="Включить снова", emoji="▶️", style=discord.ButtonStyle.success, row=1)
    async def resume_alert(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.alert is None:
            return
        await interaction.response.defer()
        item = await asyncio.to_thread(market_catalog.get_item, self.item_id)
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
        try:
            if client.config.enabled and client.config.api_keys:
                before = await asyncio.to_thread(market_catalog.status)
                status = await market_catalog.sync(use_cache=False)
                await asyncio.to_thread(
                    storage.market_evaluate_alerts,
                    MARKET_SERVER_ID,
                    str(status.get("source_updated_at") or ""),
                    MARKET_CATEGORY,
                )
                if int(before.get("consecutive_failures") or 0) > 0:
                    for guild in bot.guilds:
                        await log_technical_event(
                            bot,
                            guild,
                            title="Majestic API восстановлен",
                            details=(
                                f"Каталог {MARKET_SERVER_ID} снова обновляется. "
                                f"Предметов: {int(status.get('record_count') or 0)}."
                            ),
                            level="info",
                            dedupe_key="market-sync-recovered",
                            cooldown_seconds=600,
                        )
        except asyncio.CancelledError:
            raise
        except (MajesticApiDisabledError, MajesticApiConfigurationError):
            pass
        except Exception as exc:
            traceback.print_exc()
            await asyncio.to_thread(
                storage.market_record_sync_error,
                MARKET_SERVER_ID,
                MARKET_CATEGORY,
                f"{type(exc).__name__}: {str(exc)[:350]}",
            )
            for guild in bot.guilds:
                await log_technical_event(
                    bot,
                    guild,
                    title="Ошибка синхронизации рынка",
                    details=f"RU15/items: `{type(exc).__name__}`. Локальный каталог продолжает работать.",
                    level="warning",
                    dedupe_key=f"market-sync:{type(exc).__name__}",
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
    back_to_tvrs: bool,
) -> None:
    status = await market_catalog.ensure_ready()
    clean_query = str(query or "").strip()
    if not clean_query:
        await interaction.followup.send(
            embed=market_home_embed(status),
            view=MarketHomeView(interaction.user.id, back_to_tvrs=back_to_tvrs),
            ephemeral=True,
        )
        return
    hits = await asyncio.to_thread(market_catalog.search, clean_query, MARKET_SEARCH_LIMIT)
    if hits and hits[0].score >= 15_000:
        item = hits[0].item
        history, alert = await asyncio.gather(
            asyncio.to_thread(market_catalog.history, int(item["item_id"]), 30),
            asyncio.to_thread(market_catalog.alert, interaction.user.id, int(item["item_id"])),
        )
        await interaction.followup.send(
            embed=market_item_embed(item, status, history, alert),
            view=MarketItemView(
                interaction.user.id,
                int(item["item_id"]),
                query=clean_query,
                back_to_tvrs=back_to_tvrs,
                alert=alert,
            ),
            ephemeral=True,
        )
        return
    if not hits:
        await interaction.followup.send(
            embed=market_empty_embed(clean_query, status),
            view=MarketHomeView(interaction.user.id, back_to_tvrs=back_to_tvrs),
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        embed=market_results_embed(clean_query, hits, status),
        view=MarketResultsView(
            interaction.user.id,
            clean_query,
            hits,
            back_to_tvrs=back_to_tvrs,
        ),
        ephemeral=True,
    )


async def send_market_panel(interaction: discord.Interaction, *, back_to_tvrs: bool = False) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    await _send_market_response(interaction, query=None, back_to_tvrs=back_to_tvrs)


def setup_market(
    bot: commands.Bot,
    remember_command_activity: Callable[[discord.Interaction, str, str], None] | None = None,
) -> None:
    @bot.tree.command(name="market", description="Найти предмет и посмотреть рыночную статистику RU15")
    @app_commands.describe(query="Русское название, часть слова или ID предмета")
    async def market(interaction: discord.Interaction, query: str | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда работает в любом канале сервера.", ephemeral=True)
            return
        if remember_command_activity is not None:
            remember_command_activity(interaction, "command_market", "/market")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _send_market_response(interaction, query=query, back_to_tvrs=False)

    @market.autocomplete("query")
    async def market_query_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        del interaction
        if normalize_market_text(current):
            hits = await asyncio.to_thread(market_catalog.search, current, MARKET_SEARCH_LIMIT)
        else:
            hits = await asyncio.to_thread(market_catalog.popular, MARKET_SEARCH_LIMIT)
        return [
            app_commands.Choice(
                name=(
                    f"{str(hit.item.get('item_name') or 'Без названия')} · "
                    f"средняя {_price(hit.item.get('average_price'))}"
                )[:100],
                value=f"#{int(hit.item.get('item_id') or 0)}",
            )
            for hit in hits[:MARKET_SEARCH_LIMIT]
        ]

    async def market_ready_listener() -> None:
        global _worker_task
        if _worker_task is None or _worker_task.done():
            _worker_task = asyncio.create_task(market_worker(bot), name="tmod-market-worker")

    bot.add_listener(market_ready_listener, "on_ready")
