"""External provider adapters for YouTube metadata and OpenRouter STT."""

from __future__ import annotations

import base64
import io
import logging
import os
import threading
import time
import wave
from typing import Any

import requests

from modules.music_config import (
    MUSIC_ALLOW_LIVE,
    MUSIC_MAX_TRACK_SECONDS,
    MUSIC_STT_API_URL,
    MUSIC_STT_FALLBACK_MODELS,
    MUSIC_STT_LANGUAGE,
    MUSIC_STT_MODEL,
    MUSIC_STT_TIMEOUT_SECONDS,
    MUSIC_YTDLP_COOKIE_FILE,
    MUSIC_YTDLP_PROXY,
)
from modules.music_stt_audio import (
    discord_pcm_to_stt_wav,
    prepare_discord_pcm_for_stt,
)
from modules.music_domain import MusicInputError, MusicTrack, prepare_youtube_input

try:
    import yt_dlp
except ImportError:  # pragma: no cover - exercised by deployment diagnostics.
    yt_dlp = None  # type: ignore[assignment]


log = logging.getLogger(__name__)


class MusicProviderError(RuntimeError):
    pass


class YoutubeResolver:
    @staticmethod
    def _safe_provider_error(exc: Exception) -> str:
        text = str(exc)
        if MUSIC_YTDLP_PROXY:
            text = text.replace(MUSIC_YTDLP_PROXY, "<proxy>")
        return text[:500]

    def _options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "format": "bestaudio/best",
            "default_search": "ytsearch1",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "socket_timeout": 20,
            "retries": 2,
            "fragment_retries": 2,
        }
        if MUSIC_YTDLP_COOKIE_FILE is not None and MUSIC_YTDLP_COOKIE_FILE.is_file():
            options["cookiefile"] = str(MUSIC_YTDLP_COOKIE_FILE)
        if MUSIC_YTDLP_PROXY:
            options["proxy"] = MUSIC_YTDLP_PROXY
        return options

    @staticmethod
    def _first_entry(info: Any) -> dict[str, Any]:
        if not isinstance(info, dict):
            raise MusicProviderError("YouTube вернул неизвестный формат ответа.")
        entries = info.get("entries")
        if entries is not None:
            entry = next((item for item in entries if isinstance(item, dict)), None)
            if entry is None:
                raise MusicProviderError("По вашему запросу ничего не найдено.")
            return entry
        return info

    def resolve(
        self, value: str, *, requester_id: int, requester_display: str
    ) -> MusicTrack:
        if yt_dlp is None:
            raise MusicProviderError("Компонент yt-dlp не установлен в контейнере.")
        target = prepare_youtube_input(value)
        try:
            with yt_dlp.YoutubeDL(self._options()) as ydl:
                entry = self._first_entry(ydl.extract_info(target, download=False))
        except MusicInputError:
            raise
        except Exception as exc:
            raise MusicProviderError(
                "Не удалось получить композицию с YouTube: "
                f"{self._safe_provider_error(exc)}"
            ) from exc
        duration_raw = entry.get("duration")
        duration = int(duration_raw) if duration_raw is not None else None
        is_live = bool(entry.get("is_live") or entry.get("live_status") == "is_live")
        if is_live and not MUSIC_ALLOW_LIVE:
            raise MusicInputError(
                "Прямые трансляции отключены; выберите обычную композицию."
            )
        if duration is not None and duration > MUSIC_MAX_TRACK_SECONDS:
            raise MusicInputError(
                f"Композиция длиннее разрешённого лимита ({MUSIC_MAX_TRACK_SECONDS // 60} мин)."
            )
        webpage_url = str(
            entry.get("webpage_url")
            or entry.get("original_url")
            or entry.get("url")
            or ""
        ).strip()
        if not webpage_url:
            raise MusicProviderError(
                "YouTube не вернул ссылку на найденную композицию."
            )
        # yt-dlp output is still validated before we store and later re-open it.
        # This keeps provider-shaped data from turning into an arbitrary URL fetch.
        prepare_youtube_input(webpage_url)
        return MusicTrack(
            title=str(entry.get("title") or "Без названия")[:300],
            webpage_url=webpage_url,
            duration_seconds=duration,
            uploader=str(entry.get("uploader") or entry.get("channel") or "")[:180]
            or None,
            thumbnail_url=str(entry.get("thumbnail") or "")[:1000] or None,
            requested_by_id=int(requester_id),
            requested_by_display=str(requester_display)[:180],
        )

    def stream_url(self, track: MusicTrack) -> str:
        if yt_dlp is None:
            raise MusicProviderError("Компонент yt-dlp не установлен в контейнере.")
        try:
            with yt_dlp.YoutubeDL(self._options()) as ydl:
                entry = self._first_entry(
                    ydl.extract_info(track.webpage_url, download=False)
                )
        except Exception as exc:
            raise MusicProviderError(
                "Не удалось обновить аудиопоток YouTube: "
                f"{self._safe_provider_error(exc)}"
            ) from exc
        stream_url = str(entry.get("url") or "").strip()
        if not stream_url:
            raise MusicProviderError("YouTube не вернул воспроизводимый аудиопоток.")
        return stream_url


def pcm_to_wav(
    pcm: bytes,
    *,
    sample_rate: int = 48_000,
    channels: int = 2,
    sample_width: int = 2,
) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


class OpenRouterTranscriber:
    def __init__(self, *, http: Any | None = None) -> None:
        self.api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        self.referer = os.getenv("OPENROUTER_REFERER", "").strip()
        self.title = os.getenv("OPENROUTER_TITLE", "T-Mod").strip()
        self.primary_model = MUSIC_STT_MODEL
        self.accuracy_models = MUSIC_STT_FALLBACK_MODELS
        self.accuracy_model = self.accuracy_models[0] if self.accuracy_models else ""
        # A persistent session reuses DNS/TLS connections between utterances.
        self.http = http or requests.Session()
        self._health_lock = threading.Lock()
        self._model_health: dict[str, tuple[int, float]] = {}

    @property
    def configured(self) -> bool:
        return bool(
            self.api_key
            and self.api_key
            not in {"YOUR_OPENROUTER_KEY_HERE", "YOUR_OPENROUTER_API_KEY_HERE"}
        )

    def _require_healthy_model(self, model: str) -> None:
        with self._health_lock:
            failures, blocked_until = self._model_health.get(model, (0, 0.0))
        if blocked_until > time.monotonic():
            wait = max(1, round(blocked_until - time.monotonic()))
            raise MusicProviderError(
                f"Модель распознавания {model} восстанавливается; повтор через {wait} сек."
            )
        if failures and blocked_until:
            with self._health_lock:
                self._model_health[model] = (failures, 0.0)

    def _record_model_failure(
        self,
        model: str,
        *,
        cooldown: float,
        immediate: bool = False,
    ) -> None:
        with self._health_lock:
            failures, _ = self._model_health.get(model, (0, 0.0))
            failures += 1
            blocked_until = (
                time.monotonic() + cooldown if immediate or failures >= 2 else 0.0
            )
            self._model_health[model] = (failures, blocked_until)

    def _record_model_success(self, model: str) -> None:
        with self._health_lock:
            self._model_health.pop(model, None)

    def transcribe_pcm(self, pcm: bytes, *, model: str | None = None) -> str:
        if not self.configured:
            raise MusicProviderError(
                "Для голосового управления не настроен OPENROUTER_API_KEY."
            )
        if not pcm:
            return ""
        selected_model = str(model or self.primary_model).strip()
        if not selected_model:
            raise MusicProviderError("Не настроена модель распознавания речи.")
        self._require_healthy_model(selected_model)
        prepared = prepare_discord_pcm_for_stt(pcm)
        if not prepared.has_speech:
            log.debug(
                "Music STT skipped non-speech audio: input_ms=%s",
                prepared.input_duration_ms,
            )
            return ""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.referer:
            headers["HTTP-Referer"] = self.referer
        if self.title:
            headers["X-OpenRouter-Title"] = self.title
        payload = {
            "model": selected_model,
            "input_audio": {
                "data": base64.b64encode(prepared.wav).decode("ascii"),
                "format": "wav",
            },
            "language": MUSIC_STT_LANGUAGE,
            "temperature": 0,
        }
        started_at = time.monotonic()
        last_error = ""
        for attempt in range(2):
            try:
                response = self.http.post(
                    MUSIC_STT_API_URL,
                    headers=headers,
                    json=payload,
                    timeout=(3.05, MUSIC_STT_TIMEOUT_SECONDS),
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt == 0 and not isinstance(exc, requests.Timeout):
                    time.sleep(0.25)
                    continue
                self._record_model_failure(selected_model, cooldown=10.0)
                raise MusicProviderError(
                    f"OpenRouter STT временно недоступен: {last_error[:500]}"
                ) from exc
            if response.status_code == 429 and attempt == 0:
                retry_after = response.headers.get("Retry-After", "1")
                try:
                    delay = max(0.25, min(1.25, float(retry_after)))
                except ValueError:
                    delay = 0.5
                time.sleep(delay)
                continue
            if response.status_code >= 400:
                permanent = response.status_code in {400, 401, 402, 403, 404}
                self._record_model_failure(
                    selected_model,
                    cooldown=300.0 if permanent else 10.0,
                    immediate=permanent,
                )
                raise MusicProviderError(
                    f"OpenRouter STT: HTTP {response.status_code} "
                    f"для модели {selected_model}."
                )
            try:
                data = response.json()
            except ValueError as exc:
                self._record_model_failure(selected_model, cooldown=10.0)
                raise MusicProviderError(
                    "OpenRouter STT вернул не JSON-ответ."
                ) from exc
            transcript = str(data.get("text") or "").strip()
            self._record_model_success(selected_model)
            request_ms = round((time.monotonic() - started_at) * 1000)
            usage = data.get("usage") if isinstance(data, dict) else None
            cost = usage.get("cost") if isinstance(usage, dict) else None
            report = log.info if request_ms >= 1_200 else log.debug
            report(
                "Music STT completed: model=%s audio_ms=%s speech_ms=%s "
                "request_ms=%s text_chars=%s cost=%s",
                selected_model[:100],
                prepared.output_duration_ms,
                prepared.speech_duration_ms,
                request_ms,
                len(transcript),
                cost if cost is not None else "unknown",
            )
            return transcript
        raise MusicProviderError(
            f"OpenRouter STT временно недоступен: {last_error[:500]}"
        )


__all__ = [
    "MusicProviderError",
    "OpenRouterTranscriber",
    "YoutubeResolver",
    "discord_pcm_to_stt_wav",
    "pcm_to_wav",
]
