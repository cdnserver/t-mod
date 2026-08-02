"""Safe foundation for the future Majestic external API integration.

The module deliberately has no Discord commands and performs no requests on import.
It can be enabled later through the persistent environment file and called from a
worker thread so synchronous HTTP never blocks the Discord event loop.
"""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
import os
import re
import threading
import time
from typing import Any, Callable

import requests


JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
MARKETPLACE_CATEGORIES = frozenset(
    {
        "vehicles",
        "items",
        "houses",
        "apartments",
        "warehouses",
        "offices",
        "clothes",
    }
)
SERVER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on", "y", "да"}


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _parse_api_keys() -> tuple[str, ...]:
    raw_values = [os.getenv("MAJESTIC_API_KEY", ""), os.getenv("MAJESTIC_API_KEYS", "")]
    keys: list[str] = []
    for raw in raw_values:
        for value in raw.replace(";", ",").replace("\n", ",").split(","):
            clean = value.strip()
            if clean and clean not in keys and "YOUR_" not in clean.upper():
                keys.append(clean)
    return tuple(keys)


@dataclass(frozen=True, slots=True)
class MajesticApiConfig:
    enabled: bool = False
    base_url: str = "https://api.majestic-files.net"
    api_keys: tuple[str, ...] = field(default_factory=tuple, repr=False)
    language: str = "ru"
    server_id: str = "RU15"
    requests_per_window: int = 5
    window_seconds: int = 60
    timeout_seconds: float = 15.0
    cache_ttl_seconds: int = 60
    max_retries: int = 1
    retry_backoff_seconds: float = 1.0

    @classmethod
    def from_env(cls) -> MajesticApiConfig:
        return cls(
            enabled=_env_bool("MAJESTIC_API_ENABLED", False),
            base_url=os.getenv("MAJESTIC_API_BASE_URL", "https://api.majestic-files.net").strip().rstrip("/"),
            api_keys=_parse_api_keys(),
            language=os.getenv("MAJESTIC_API_LANGUAGE", "ru").strip() or "ru",
            server_id=os.getenv("MAJESTIC_SERVER_ID", "RU15").strip().upper() or "RU15",
            requests_per_window=_env_int("MAJESTIC_API_REQUESTS_PER_WINDOW", 5, minimum=1),
            window_seconds=_env_int("MAJESTIC_API_WINDOW_SECONDS", 60, minimum=1),
            timeout_seconds=_env_float("MAJESTIC_API_TIMEOUT_SECONDS", 15.0, minimum=1.0),
            cache_ttl_seconds=_env_int("MAJESTIC_API_CACHE_TTL_SECONDS", 60, minimum=0),
            max_retries=_env_int("MAJESTIC_API_MAX_RETRIES", 1, minimum=0, maximum=3),
            retry_backoff_seconds=_env_float("MAJESTIC_API_RETRY_BACKOFF_SECONDS", 1.0),
        )

    def validate_for_request(self) -> None:
        if not self.enabled:
            raise MajesticApiDisabledError("Интеграция Majestic API отключена.")
        if not self.api_keys:
            raise MajesticApiConfigurationError("Не задан MAJESTIC_API_KEY.")
        if not self.base_url.startswith("https://"):
            raise MajesticApiConfigurationError("MAJESTIC_API_BASE_URL должен использовать HTTPS.")

    def safe_summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "configured_key_count": len(self.api_keys),
            "language": self.language,
            "server_id": self.server_id,
            "requests_per_window": self.requests_per_window,
            "window_seconds": self.window_seconds,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "key_pool_mode": "primary-only",
        }


class MajesticApiError(RuntimeError):
    """Base exception that never includes API credentials."""


class MajesticApiDisabledError(MajesticApiError):
    pass


class MajesticApiConfigurationError(MajesticApiError):
    pass


class MajesticApiTransportError(MajesticApiError):
    pass


class MajesticApiResponseError(MajesticApiError):
    pass


class MajesticApiHttpError(MajesticApiError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = int(status_code)
        super().__init__(f"Majestic API вернул HTTP {self.status_code}: {message}")


class MajesticApiUnauthorizedError(MajesticApiHttpError):
    pass


class MajesticApiAccessError(MajesticApiHttpError):
    pass


class MajesticApiRateLimitError(MajesticApiHttpError):
    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        suffix = f" Повторить не раньше чем через {retry_after_seconds:g} сек." if retry_after_seconds else ""
        super().__init__(429, message + suffix)


class MajesticApiServerError(MajesticApiHttpError):
    pass


@dataclass(frozen=True, slots=True)
class MajesticMarketplaceSummary:
    category: str
    server_id: str
    server_name: str
    record_count: int
    total_count: int | None
    total_sold: int | None
    overall_average_price: int | float | None
    last_updated: str | None
    period_days: int | None

    @classmethod
    def from_payload(cls, category: str, payload: JsonValue) -> MajesticMarketplaceSummary:
        if not isinstance(payload, dict) or payload.get("status") is not True:
            raise MajesticApiResponseError("Majestic API вернул неуспешный ответ.")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise MajesticApiResponseError("В ответе Majestic API отсутствует объект result.")

        statistics = next(
            (
                value
                for key, value in result.items()
                if str(key).lower().endswith("statistics") and isinstance(value, list)
            ),
            [],
        )
        total_key = f"total{category[:1].upper()}{category[1:]}"

        def optional_int(value: Any) -> int | None:
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        average = result.get("overallAveragePrice")
        if not isinstance(average, (int, float)):
            average = None
        return cls(
            category=category,
            server_id=str(result.get("serverId") or "—"),
            server_name=str(result.get("serverName") or "—"),
            record_count=len(statistics),
            total_count=optional_int(result.get(total_key)),
            total_sold=optional_int(result.get("totalSold")),
            overall_average_price=average,
            last_updated=str(result["lastUpdated"]) if result.get("lastUpdated") else None,
            period_days=optional_int(result.get("periodDays")),
        )


@dataclass(frozen=True, slots=True)
class MajesticMarketplaceItem:
    item_id: int
    item_name: str
    total_count: int
    sold_count: int
    average_price: int | None
    min_price: int | None
    max_price: int | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MajesticMarketplaceItem:
        try:
            item_id = int(payload["itemId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Majestic itemId отсутствует или некорректен.") from exc
        item_name = str(payload.get("itemName") or "").strip() or f"Неизвестный предмет #{item_id}"
        if item_id < 0:
            raise ValueError("Majestic itemId не может быть отрицательным.")

        def non_negative(value: Any, *, optional: bool = False) -> int | None:
            if value is None and optional:
                return None
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return None if optional else 0

        return cls(
            item_id=item_id,
            item_name=item_name,
            total_count=int(non_negative(payload.get("totalCount")) or 0),
            sold_count=int(non_negative(payload.get("soldCount")) or 0),
            average_price=non_negative(payload.get("averagePrice"), optional=True),
            min_price=non_negative(payload.get("minPrice"), optional=True),
            max_price=non_negative(payload.get("maxPrice"), optional=True),
        )


@dataclass(frozen=True, slots=True)
class MajesticMarketplaceEntry:
    external_id: str
    item_name: str
    total_count: int
    sold_count: int
    average_price: int | None
    min_price: int | None
    max_price: int | None
    metadata: dict[str, Any]

    @classmethod
    def from_payload(cls, category: str, payload: dict[str, Any]) -> MajesticMarketplaceEntry:
        clean_category = str(category).strip().lower()

        def non_negative(value: Any, *, optional: bool = False) -> int | None:
            if value is None and optional:
                return None
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return None if optional else 0

        if clean_category == "items":
            try:
                raw_id = int(payload["itemId"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Majestic itemId отсутствует или некорректен.") from exc
            if raw_id < 0:
                raise ValueError("Majestic itemId не может быть отрицательным.")
            external_id = str(raw_id)
            item_name = str(payload.get("itemName") or "").strip() or f"Неизвестный предмет #{raw_id}"
            metadata: dict[str, Any] = {"item_id": raw_id, "quantity_metric": "total_count"}
            total_count = int(non_negative(payload.get("totalCount")) or 0)
        elif clean_category == "vehicles":
            external_id = str(payload.get("model") or "").strip().lower()
            if not external_id:
                raise ValueError("Majestic vehicle model отсутствует.")
            item_name = str(payload.get("modelName") or "").strip() or external_id
            metadata = {
                "model": external_id,
                "model_name": item_name,
                "quantity_metric": "total_count",
            }
            total_count = int(non_negative(payload.get("totalCount")) or 0)
        elif clean_category == "clothes":
            required = ("gender", "component", "drawable", "texture", "isProp")
            try:
                values = {key: int(payload[key]) for key in required}
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Majestic вернул неполный идентификатор варианта одежды.") from exc
            if any(value < 0 for value in values.values()):
                raise ValueError("Majestic вернул отрицательный идентификатор одежды.")
            if values["gender"] not in {0, 1} or values["isProp"] not in {0, 1}:
                raise ValueError("Majestic вернул некорректный тип варианта одежды.")
            external_id = ":".join(str(values[key]) for key in required)
            item_name = str(payload.get("itemName") or "").strip() or f"Неизвестная одежда {external_id}"
            metadata = {
                **values,
                "quantity_metric": "sold_count",
            }
            # Clothes do not expose totalCount. Keep the searchable quantity metric useful and
            # explicit by mirroring soldCount; the UI labels it as sales rather than availability.
            total_count = int(non_negative(payload.get("soldCount")) or 0)
        else:
            raise ValueError(f"Категория {category} не поддерживает подробный каталог.")

        return cls(
            external_id=external_id,
            item_name=item_name,
            total_count=total_count,
            sold_count=int(non_negative(payload.get("soldCount")) or 0),
            average_price=non_negative(payload.get("averagePrice"), optional=True),
            min_price=non_negative(payload.get("minPrice"), optional=True),
            max_price=non_negative(payload.get("maxPrice"), optional=True),
            metadata=metadata,
        )


@dataclass(frozen=True, slots=True)
class MajesticMarketplaceSnapshot:
    summary: MajesticMarketplaceSummary
    entries: tuple[MajesticMarketplaceEntry, ...]

    @classmethod
    def from_payload(cls, category: str, payload: JsonValue) -> MajesticMarketplaceSnapshot:
        clean_category = str(category).strip().lower()
        statistics_keys = {
            "items": "itemStatistics",
            "vehicles": "vehicleStatistics",
            "clothes": "clothesStatistics",
        }
        statistics_key = statistics_keys.get(clean_category)
        if statistics_key is None:
            raise MajesticApiResponseError(f"Подробный каталог {clean_category} пока не поддерживается.")
        summary = MajesticMarketplaceSummary.from_payload(clean_category, payload)
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
            raise MajesticApiResponseError("В ответе Majestic API отсутствует объект каталога.")
        raw_entries = payload["result"].get(statistics_key)
        if not isinstance(raw_entries, list):
            raise MajesticApiResponseError(f"В ответе Majestic API отсутствует {statistics_key}.")
        entries: list[MajesticMarketplaceEntry] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            try:
                entries.append(MajesticMarketplaceEntry.from_payload(clean_category, raw))
            except ValueError:
                continue
        if not entries:
            raise MajesticApiResponseError(f"Majestic API вернул пустой каталог {clean_category}.")
        if len(entries) != len(raw_entries):
            raise MajesticApiResponseError(f"Majestic API вернул неполные строки {clean_category}.")
        if len({entry.external_id for entry in entries}) != len(entries):
            raise MajesticApiResponseError(f"Majestic API вернул повторяющиеся ключи {clean_category}.")
        return cls(summary=summary, entries=tuple(entries))


@dataclass(frozen=True, slots=True)
class MajesticItemsSnapshot:
    summary: MajesticMarketplaceSummary
    items: tuple[MajesticMarketplaceItem, ...]

    @classmethod
    def from_payload(cls, payload: JsonValue) -> MajesticItemsSnapshot:
        snapshot = MajesticMarketplaceSnapshot.from_payload("items", payload)
        items = tuple(
            MajesticMarketplaceItem(
                item_id=int(entry.external_id),
                item_name=entry.item_name,
                total_count=entry.total_count,
                sold_count=entry.sold_count,
                average_price=entry.average_price,
                min_price=entry.min_price,
                max_price=entry.max_price,
            )
            for entry in snapshot.entries
        )
        return cls(summary=snapshot.summary, items=items)


class SlidingWindowLimiter:
    """Thread-safe process-wide request budget for one client instance."""

    def __init__(
        self,
        max_calls: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_calls = max(1, int(max_calls))
        self.window_seconds = max(0.001, float(window_seconds))
        self._clock = clock
        self._sleep = sleeper
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        boundary = now - self.window_seconds
        while self._calls and self._calls[0] <= boundary:
            self._calls.popleft()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                self._prune(now)
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait_seconds = max(0.001, self.window_seconds - (now - self._calls[0]))
            self._sleep(wait_seconds)

    def remaining(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return max(0, self.max_calls - len(self._calls))


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    value: JsonValue


class MajesticApiClient:
    """Synchronous API client intended to be called through ``asyncio.to_thread``."""

    def __init__(
        self,
        config: MajesticApiConfig | None = None,
        *,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or MajesticApiConfig.from_env()
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._clock = clock
        self._sleep = sleeper
        self._limiter = SlidingWindowLimiter(
            self.config.requests_per_window,
            self.config.window_seconds,
            clock=clock,
            sleeper=sleeper,
        )
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], _CacheEntry] = {}
        self._cache_lock = threading.Lock()

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> MajesticApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def diagnostics(self) -> dict[str, Any]:
        with self._cache_lock:
            now = self._clock()
            live_entries = sum(entry.expires_at > now for entry in self._cache.values())
        return {
            **self.config.safe_summary(),
            "remaining_process_budget": self._limiter.remaining(),
            "live_cache_entries": live_entries,
        }

    @staticmethod
    def _validate_path(path: str) -> str:
        clean = "/" + path.strip().lstrip("/")
        if not clean.startswith("/v1/") or "://" in clean or ".." in clean:
            raise ValueError("Разрешены только относительные маршруты Majestic API версии v1.")
        return clean

    @staticmethod
    def _cache_key(path: str, params: dict[str, Any] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        normalized = tuple(sorted((str(key), str(value)) for key, value in (params or {}).items()))
        return path, normalized

    def _get_cached(self, key: tuple[str, tuple[tuple[str, str], ...]]) -> JsonValue | None:
        if self.config.cache_ttl_seconds <= 0:
            return None
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                self._cache.pop(key, None)
                return None
            return deepcopy(entry.value)

    def _set_cached(self, key: tuple[str, tuple[tuple[str, str], ...]], value: JsonValue) -> None:
        if self.config.cache_ttl_seconds <= 0:
            return
        with self._cache_lock:
            self._cache[key] = _CacheEntry(
                expires_at=self._clock() + self.config.cache_ttl_seconds,
                value=deepcopy(value),
            )

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            for key in ("errorDescription", "message", "error", "code"):
                if payload.get(key):
                    return str(payload[key])[:300]
        clean_text = " ".join(str(getattr(response, "text", "")).split())
        return clean_text[:300] or "неизвестная ошибка"

    @staticmethod
    def _retry_after(response: requests.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    def _raise_for_status(self, response: requests.Response) -> None:
        status = int(response.status_code)
        message = self._error_message(response)
        if status == 401:
            raise MajesticApiUnauthorizedError(status, message)
        if status == 403:
            raise MajesticApiAccessError(status, message)
        if status == 429:
            raise MajesticApiRateLimitError(message, self._retry_after(response))
        if status >= 500:
            raise MajesticApiServerError(status, message)
        if status >= 400:
            raise MajesticApiHttpError(status, message)

    def get_json(self, path: str, *, params: dict[str, Any] | None = None, use_cache: bool = True) -> JsonValue:
        self.config.validate_for_request()
        clean_path = self._validate_path(path)
        cache_key = self._cache_key(clean_path, params)
        if use_cache:
            cached = self._get_cached(cache_key)
            if cached is not None:
                return cached

        headers = {
            "Accept": "application/json",
            "User-Agent": "T-Mod/majestic-api-scaffold",
            "x-api-key": self.config.api_keys[0],
            "x-language": self.config.language,
        }
        url = self.config.base_url + clean_path
        for attempt in range(self.config.max_retries + 1):
            self._limiter.acquire()
            try:
                response = self._session.request(
                    "GET",
                    url,
                    headers=headers,
                    params=params,
                    timeout=self.config.timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt < self.config.max_retries:
                    self._sleep(self.config.retry_backoff_seconds * (attempt + 1))
                    continue
                raise MajesticApiTransportError(f"Majestic API недоступен: {type(exc).__name__}") from exc

            if response.status_code == 429 and attempt < self.config.max_retries:
                self._sleep(
                    self._retry_after(response)
                    or self.config.retry_backoff_seconds * (attempt + 1)
                )
                continue
            if response.status_code >= 500 and attempt < self.config.max_retries:
                self._sleep(self.config.retry_backoff_seconds * (attempt + 1))
                continue
            self._raise_for_status(response)
            try:
                payload: JsonValue = response.json()
            except ValueError as exc:
                raise MajesticApiResponseError("Majestic API вернул ответ не в формате JSON.") from exc
            if use_cache:
                self._set_cached(cache_key, payload)
            return payload
        raise AssertionError("unreachable")

    async def get_json_async(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> JsonValue:
        return await asyncio.to_thread(self.get_json, path, params=params, use_cache=use_cache)

    def _marketplace_server_id(self, server_id: str | int | None) -> str:
        clean_server_id = str(server_id if server_id is not None else self.config.server_id).strip().upper()
        if not SERVER_ID_PATTERN.fullmatch(clean_server_id):
            raise ValueError("server_id должен состоять только из букв, цифр, дефиса или подчёркивания.")
        return clean_server_id

    def marketplace(
        self,
        category: str,
        server_id: str | int | None = None,
        *,
        use_cache: bool = True,
    ) -> JsonValue:
        clean_category = str(category).strip().lower()
        if clean_category not in MARKETPLACE_CATEGORIES:
            raise ValueError(f"Неизвестная категория маркетплейса: {category}")
        clean_server_id = self._marketplace_server_id(server_id)
        return self.get_json(
            f"/v1/ext/marketplace/{clean_category}/{clean_server_id}",
            use_cache=use_cache,
        )

    async def marketplace_async(
        self,
        category: str,
        server_id: str | int | None = None,
        *,
        use_cache: bool = True,
    ) -> JsonValue:
        return await asyncio.to_thread(self.marketplace, category, server_id, use_cache=use_cache)

    def marketplace_summary(
        self,
        category: str = "items",
        server_id: str | int | None = None,
        *,
        use_cache: bool = True,
    ) -> MajesticMarketplaceSummary:
        clean_category = str(category).strip().lower()
        payload = self.marketplace(clean_category, server_id, use_cache=use_cache)
        return MajesticMarketplaceSummary.from_payload(clean_category, payload)

    async def marketplace_summary_async(
        self,
        category: str = "items",
        server_id: str | int | None = None,
        *,
        use_cache: bool = True,
    ) -> MajesticMarketplaceSummary:
        return await asyncio.to_thread(
            self.marketplace_summary,
            category,
            server_id,
            use_cache=use_cache,
        )

    def marketplace_snapshot(
        self,
        category: str,
        server_id: str | int | None = None,
        *,
        use_cache: bool = True,
    ) -> MajesticMarketplaceSnapshot:
        clean_category = str(category).strip().lower()
        payload = self.marketplace(clean_category, server_id, use_cache=use_cache)
        return MajesticMarketplaceSnapshot.from_payload(clean_category, payload)

    async def marketplace_snapshot_async(
        self,
        category: str,
        server_id: str | int | None = None,
        *,
        use_cache: bool = True,
    ) -> MajesticMarketplaceSnapshot:
        return await asyncio.to_thread(
            self.marketplace_snapshot,
            category,
            server_id,
            use_cache=use_cache,
        )

    def marketplace_items_snapshot(
        self,
        server_id: str | int | None = None,
        *,
        use_cache: bool = True,
    ) -> MajesticItemsSnapshot:
        payload = self.marketplace("items", server_id, use_cache=use_cache)
        return MajesticItemsSnapshot.from_payload(payload)

    async def marketplace_items_snapshot_async(
        self,
        server_id: str | int | None = None,
        *,
        use_cache: bool = True,
    ) -> MajesticItemsSnapshot:
        return await asyncio.to_thread(
            self.marketplace_items_snapshot,
            server_id,
            use_cache=use_cache,
        )


_default_client: MajesticApiClient | None = None
_default_client_lock = threading.Lock()


def get_majestic_api_client() -> MajesticApiClient:
    """Return the shared client so all bot features use one cache and budget."""
    global _default_client
    with _default_client_lock:
        if _default_client is None:
            _default_client = MajesticApiClient()
        return _default_client
