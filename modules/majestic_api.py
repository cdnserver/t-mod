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

    def marketplace(self, category: str, server_id: int, *, use_cache: bool = True) -> JsonValue:
        clean_category = str(category).strip().lower()
        if clean_category not in MARKETPLACE_CATEGORIES:
            raise ValueError(f"Неизвестная категория маркетплейса: {category}")
        if int(server_id) <= 0:
            raise ValueError("server_id должен быть положительным числом.")
        return self.get_json(
            f"/v1/ext/marketplace/{clean_category}/{int(server_id)}",
            use_cache=use_cache,
        )

    async def marketplace_async(self, category: str, server_id: int, *, use_cache: bool = True) -> JsonValue:
        return await asyncio.to_thread(self.marketplace, category, server_id, use_cache=use_cache)


_default_client: MajesticApiClient | None = None
_default_client_lock = threading.Lock()


def get_majestic_api_client() -> MajesticApiClient:
    """Return the shared client so all bot features use one cache and budget."""
    global _default_client
    with _default_client_lock:
        if _default_client is None:
            _default_client = MajesticApiClient()
        return _default_client
