"""Low-latency, bounded text-to-speech for Atlas field surfaces.

The provider call is deliberately isolated from the web adapter.  A failed or
overloaded AI voice never blocks Atlas: callers receive an explicit signal to
use the operating system voice already available on the client.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

import aiohttp


_DEFAULT_MODEL = "elevenlabs/eleven-v4-turbo"
_DEFAULT_VOICES = ("george", "sarah", "daniel", "river")
_LEGACY_VOICES = ("ara", "eve", "rex", "sal", "leo")
_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_CITATION_RE = re.compile(r"\[(?:\d{1,2})(?:\s*,\s*(?:[^\]\n]{1,60}))?\]")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]{1,200})\]\([^\s)]+\)")
_MARKDOWN_RE = re.compile(r"(?:\*\*|__|~~|`{1,3})")
_SPACE_RE = re.compile(r"[ \t\f\v]+")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _speech_endpoint() -> str:
    explicit = os.getenv("ATLAS_TTS_API_URL", "").strip()
    if explicit:
        return explicit
    chat = os.getenv(
        "OPENROUTER_API_URL",
        "https://openrouter.ai/api/v1/chat/completions",
    ).strip()
    if "/chat/completions" in chat:
        return chat.rsplit("/chat/completions", 1)[0] + "/audio/speech"
    return "https://openrouter.ai/api/v1/audio/speech"


def _tts_api_key() -> str:
    """Prefer a least-privilege Atlas voice key without breaking existing installs."""

    return (
        os.getenv("ATLAS_TTS_API_KEY", "").strip()
        or os.getenv("OPENROUTER_API_KEY", "").strip()
    )


def _configured_voices() -> tuple[str, ...]:
    model = os.getenv("ATLAS_TTS_MODEL", _DEFAULT_MODEL)
    defaults = _DEFAULT_VOICES if model.startswith("elevenlabs/") else _LEGACY_VOICES
    raw = os.getenv("ATLAS_TTS_VOICES", ",".join(defaults))
    voices: list[str] = []
    for item in raw.split(","):
        voice = item.strip()
        if _VOICE_ID_RE.fullmatch(voice) and voice not in voices:
            voices.append(voice)
    return tuple(voices) or defaults


@dataclass(frozen=True, slots=True)
class AtlasTTSConfig:
    enabled: bool
    api_key: str
    api_url: str
    model: str
    voices: tuple[str, ...]
    default_voice: str
    timeout_seconds: float
    max_text_chars: int
    max_audio_bytes: int
    cache_ttl_seconds: int
    cache_max_items: int
    cache_max_bytes: int
    concurrency: int

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.api_key
            and not self.api_key.upper().startswith("YOUR_")
            and self.api_url.startswith("https://")
            and self.model
        )


def atlas_tts_config() -> AtlasTTSConfig:
    voices = _configured_voices()
    default_voice = os.getenv("ATLAS_TTS_DEFAULT_VOICE", voices[0]).strip()
    if default_voice not in voices:
        default_voice = voices[0]
    return AtlasTTSConfig(
        enabled=_env_bool("ATLAS_TTS_ENABLED", True),
        api_key=_tts_api_key(),
        api_url=_speech_endpoint(),
        model=os.getenv("ATLAS_TTS_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL,
        voices=voices,
        default_voice=default_voice,
        timeout_seconds=_env_float(
            "ATLAS_TTS_TIMEOUT_SECONDS", 8.0, minimum=3.0, maximum=20.0
        ),
        max_text_chars=_env_int(
            "ATLAS_TTS_MAX_TEXT_CHARS", 1200, minimum=200, maximum=2400
        ),
        max_audio_bytes=_env_int(
            "ATLAS_TTS_MAX_AUDIO_BYTES",
            4 * 1024 * 1024,
            minimum=256 * 1024,
            maximum=12 * 1024 * 1024,
        ),
        cache_ttl_seconds=_env_int(
            "ATLAS_TTS_CACHE_TTL_SECONDS", 3600, minimum=60, maximum=86_400
        ),
        cache_max_items=_env_int(
            "ATLAS_TTS_CACHE_MAX_ITEMS", 96, minimum=8, maximum=512
        ),
        cache_max_bytes=_env_int(
            "ATLAS_TTS_CACHE_MAX_BYTES",
            24 * 1024 * 1024,
            minimum=2 * 1024 * 1024,
            maximum=128 * 1024 * 1024,
        ),
        concurrency=_env_int("ATLAS_TTS_CONCURRENCY", 3, minimum=1, maximum=8),
    )


def atlas_tts_spoken_text(value: Any, *, max_chars: int) -> str:
    """Turn a compact Markdown answer into natural speech without citations."""

    text = str(value or "").replace("\r", "\n").strip()
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    text = _CITATION_RE.sub("", text)
    text = _MARKDOWN_RE.sub("", text)
    text = re.sub(r"(?m)^\s{0,3}(?:[-*•]|\d+[.)])\s+", "", text)
    text = _SPACE_RE.sub(" ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) > max_chars:
        prefix = text[: max_chars + 1]
        boundary = max(
            prefix.rfind(". ", 0, max_chars),
            prefix.rfind("! ", 0, max_chars),
            prefix.rfind("? ", 0, max_chars),
        )
        if boundary < max_chars // 2:
            boundary = prefix.rfind(" ", 0, max_chars)
        text = prefix[: max(1, boundary + 1)].rstrip(" ,;:-") + "…"
    return text


@dataclass(frozen=True, slots=True)
class AtlasTTSResult:
    audio: bytes | None
    content_type: str
    provider: str
    model: str
    voice: str
    cache_hit: bool = False
    fallback: bool = False
    reason: str = ""
    generation_id: str = ""


ProviderRequest = Callable[
    [str, str, float], Awaitable[tuple[bytes, str, str]]
]


class AtlasTTSService:
    """OpenRouter TTS with coalescing, bounded cache and a fast system fallback."""

    def __init__(
        self,
        config: AtlasTTSConfig | None = None,
        *,
        provider_request: ProviderRequest | None = None,
    ) -> None:
        self.config = config or atlas_tts_config()
        self._provider_request_override = provider_request
        self._session: aiohttp.ClientSession | None = None
        self._slots = asyncio.Semaphore(self.config.concurrency)
        self._cache: OrderedDict[str, tuple[float, AtlasTTSResult]] = OrderedDict()
        self._cache_bytes = 0
        self._inflight: dict[str, asyncio.Task[AtlasTTSResult]] = {}
        self._failures = 0
        self._circuit_until = 0.0
        self._last_failure_reason = ""

    def voices_payload(self) -> dict[str, Any]:
        labels = {
            "george": ("Георг", "ElevenLabs · глубокий и тёплый"),
            "sarah": ("Сара", "ElevenLabs · мягкий и ясный"),
            "daniel": ("Даниэль", "ElevenLabs · спокойный"),
            "river": ("Ривер", "ElevenLabs · нейтральный"),
            "ara": ("Ara", "Спокойный полевой голос"),
            "eve": ("Eve", "Ясный и живой"),
            "rex": ("Rex", "Низкий и собранный"),
            "sal": ("Sal", "Нейтральный"),
            "leo": ("Leo", "Уверенный"),
        }
        voices = []
        for voice in self.config.voices:
            label, description = labels.get(
                voice.casefold(), (voice.replace("_", " ").title(), "AI-голос Atlas")
            )
            voices.append(
                {
                    "id": voice,
                    "name": label,
                    "description": description,
                    "provider": "openrouter",
                }
            )
        availability = {
            "state": (
                "unconfigured"
                if not self.config.configured
                else "degraded"
                if self._last_failure_reason
                else "ready"
            ),
            "reason": self._last_failure_reason or (
                "" if self.config.configured else "credentials_or_endpoint_missing"
            ),
        }
        if self._circuit_until > time.monotonic():
            availability = {"state": "recovering", "reason": "provider_recovering"}
        return {
            "configured": self.config.configured,
            "provider": "openrouter" if self.config.configured else "system",
            "model": self.config.model if self.config.configured else None,
            "default_voice": self.config.default_voice,
            "voices": voices if self.config.configured else [],
            "formats": ["mp3"],
            "fallback": {
                "provider": "system",
                "client_side": True,
                "automatic": True,
            },
            "availability": availability,
        }

    async def preview(self, voice: str | None = None) -> AtlasTTSResult:
        return await self.synthesize(
            "Atlas на связи. Я нашёл главное и готов подсказать следующий шаг.",
            voice=voice,
            speed=1.04,
        )

    async def synthesize(
        self,
        text: Any,
        *,
        voice: str | None = None,
        speed: float = 1.0,
    ) -> AtlasTTSResult:
        spoken = atlas_tts_spoken_text(text, max_chars=self.config.max_text_chars)
        if not spoken:
            raise ValueError("atlas_tts_text_required")
        selected_voice = str(voice or self.config.default_voice).strip()
        if self.config.model.startswith("elevenlabs/") and selected_voice in _LEGACY_VOICES:
            selected_voice = self.config.default_voice
        if selected_voice not in self.config.voices:
            raise ValueError("atlas_tts_voice_invalid")
        try:
            selected_speed = float(speed)
        except (TypeError, ValueError) as exc:
            raise ValueError("atlas_tts_speed_invalid") from exc
        if not 0.8 <= selected_speed <= 1.25:
            raise ValueError("atlas_tts_speed_invalid")
        if not self.config.configured:
            return self._fallback(selected_voice, "not_configured")
        if self._circuit_until > time.monotonic():
            return self._fallback(selected_voice, "provider_recovering")

        key = hashlib.sha256(
            f"{self.config.model}\0{selected_voice}\0{selected_speed:.2f}\0{spoken}".encode(
                "utf-8"
            )
        ).hexdigest()
        cached = self._cache_get(key)
        if cached is not None:
            return replace(cached, cache_hit=True)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._synthesize_uncached(key, spoken, selected_voice, selected_speed)
            )
            self._inflight[key] = task
            task.add_done_callback(lambda _task, cache_key=key: self._inflight.pop(cache_key, None))
        return await asyncio.shield(task)

    async def close(self) -> None:
        tasks = tuple(self._inflight.values())
        self._inflight.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()

    async def _synthesize_uncached(
        self,
        key: str,
        text: str,
        voice: str,
        speed: float,
    ) -> AtlasTTSResult:
        acquired = False
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=0.35)
            acquired = True
        except asyncio.TimeoutError:
            return self._fallback(voice, "busy")
        try:
            try:
                audio, content_type, generation_id = await self._provider_request(
                    text, voice, speed
                )
            except Exception:  # noqa: BLE001 - TTS must always degrade to local speech.
                self._record_failure()
                return self._fallback(voice, "provider_unavailable")
            self._failures = 0
            self._circuit_until = 0.0
            self._last_failure_reason = ""
            result = AtlasTTSResult(
                audio=audio,
                content_type=content_type,
                provider="openrouter",
                model=self.config.model,
                voice=voice,
                generation_id=generation_id,
            )
            self._cache_put(key, result)
            return result
        finally:
            if acquired:
                self._slots.release()

    async def _provider_request(
        self, text: str, voice: str, speed: float
    ) -> tuple[bytes, str, str]:
        if self._provider_request_override is not None:
            return await self._provider_request_override(text, voice, speed)
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self.config.timeout_seconds,
                connect=min(3.0, self.config.timeout_seconds),
                sock_read=self.config.timeout_seconds,
            )
            connector = aiohttp.TCPConnector(limit=self.config.concurrency, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        referer = os.getenv("OPENROUTER_REFERER", "https://atlas.tvr.lat").strip()
        title = os.getenv("ATLAS_OPENROUTER_TITLE", "T-Mod Atlas").strip()
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-OpenRouter-Title"] = title
        async with self._session.post(
            self.config.api_url,
            headers=headers,
            json={
                "model": self.config.model,
                "input": text,
                "voice": voice,
                "response_format": "mp3",
                # v4/v3 reject speed; pace comes from natural phrasing.
                **({"speed": speed} if not self.config.model.startswith(("elevenlabs/eleven-v4", "elevenlabs/eleven-v3")) else {}),
            },
        ) as response:
            if response.status >= 400:
                await response.read()
                raise aiohttp.ClientResponseError(
                    response.request_info,
                    response.history,
                    status=response.status,
                    message="Atlas TTS provider rejected the request",
                    headers=response.headers,
                )
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type not in {"audio/mpeg", "audio/mp3"}:
                await response.read()
                raise ValueError("atlas_tts_provider_format_invalid")
            chunks = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                chunks.extend(chunk)
                if len(chunks) > self.config.max_audio_bytes:
                    raise ValueError("atlas_tts_provider_audio_invalid")
            audio = bytes(chunks)
            if not audio or len(audio) > self.config.max_audio_bytes:
                raise ValueError("atlas_tts_provider_audio_invalid")
            return (
                audio,
                "audio/mpeg",
                str(response.headers.get("X-Generation-Id") or "")[:160],
            )

    def _fallback(self, voice: str, reason: str) -> AtlasTTSResult:
        return AtlasTTSResult(
            audio=None,
            content_type="",
            provider="system",
            model=self.config.model,
            voice=voice,
            fallback=True,
            reason=reason,
        )

    def _record_failure(self) -> None:
        self._failures += 1
        self._last_failure_reason = "provider_unavailable"
        if self._failures >= 3:
            self._circuit_until = time.monotonic() + 30.0

    def _cache_get(self, key: str) -> AtlasTTSResult | None:
        saved = self._cache.get(key)
        if saved is None:
            return None
        expires, result = saved
        if expires <= time.monotonic():
            self._cache.pop(key, None)
            self._cache_bytes -= len(result.audio or b"")
            return None
        self._cache.move_to_end(key)
        return result

    def _cache_put(self, key: str, result: AtlasTTSResult) -> None:
        size = len(result.audio or b"")
        if size <= 0 or size > self.config.cache_max_bytes:
            return
        previous = self._cache.pop(key, None)
        if previous is not None:
            self._cache_bytes -= len(previous[1].audio or b"")
        self._cache[key] = (time.monotonic() + self.config.cache_ttl_seconds, result)
        self._cache_bytes += size
        while (
            len(self._cache) > self.config.cache_max_items
            or self._cache_bytes > self.config.cache_max_bytes
        ):
            _, (_, removed) = self._cache.popitem(last=False)
            self._cache_bytes -= len(removed.audio or b"")


__all__ = [
    "AtlasTTSConfig",
    "AtlasTTSResult",
    "AtlasTTSService",
    "atlas_tts_config",
    "atlas_tts_spoken_text",
]
