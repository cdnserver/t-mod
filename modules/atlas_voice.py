"""Bounded ElevenLabs transport for Atlas calls; never stores microphone audio."""
from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import time
import wave
import unicodedata
from decimal import Decimal, ROUND_CEILING
from typing import Any

import aiohttp
from modules.atlas_tts import atlas_tts_spoken_text

VOICES = {"george": "Георг · глубокий", "sarah": "Сара · мягкий", "daniel": "Даниэль · спокойный", "river": "Ривер · нейтральный"}
MAX_WAV = 960_044  # 30 seconds, mono PCM16 at 16 kHz + WAV header


def call_agent_name(value: Any = "Atlas") -> str:
    if not isinstance(value, str):
        raise ValueError("voice_agent_name_invalid")
    name = " ".join(unicodedata.normalize("NFKC", value).split())
    if not 1 <= len(name) <= 40 or any(not (char.isalpha() or char.isdecimal() or char in " '-") for char in name):
        raise ValueError("voice_agent_name_invalid")
    if not any(char.isalpha() for char in name):
        raise ValueError("voice_agent_name_invalid")
    return name


def pop_voice_phrase(pending: str) -> tuple[str, str] | None:
    """Wait for a real speech boundary, not a streamed decimal or legal abbreviation."""
    for boundary in re.finditer(r'[.!?…]["»)]*\s+', pending):
        prefix = pending[:boundary.start()]
        if prefix.count("[") > prefix.count("]") or prefix.count("(") > prefix.count(")"):
            continue
        if pending[boundary.start()] == ".":
            token = re.search(r"([\w.]+)$", prefix)
            if token and token[1].casefold() in {"ст", "п", "ч", "г", "т", "д", "т.д", "т.п", "др", "руб", "им", "напр", "см"}:
                continue
        # Avoid tiny, choppy audio clips and wait for at least a short sentence.
        if boundary.end() < 24:
            continue
        return pending[:boundary.end()].strip(), pending[boundary.end():]
    if len(pending) > 320:
        end = pending.rfind(" ", 0, 300)
        if end > 0:
            return pending[:end].strip(), pending[end + 1:]
    return None


def validate_call_wav(audio: bytes) -> float:
    if not 44 <= len(audio) <= MAX_WAV:
        raise ValueError("voice_audio_size")
    try:
        with wave.open(io.BytesIO(audio), "rb") as clip:
            if (clip.getnchannels(), clip.getsampwidth(), clip.getframerate(), clip.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError("voice_audio_format")
            frames = clip.getnframes()
            if not 1600 <= frames <= 480000 or len(clip.readframes(frames)) != frames * 2:
                raise ValueError("voice_audio_duration")
            return frames / 16000
    except (wave.Error, EOFError) as exc:
        raise ValueError("voice_audio_format") from exc


def cost_microusd(value: Any) -> int:
    try:
        cost = Decimal(str(value))
        if not cost.is_finite() or cost < 0 or cost > 10:
            raise ValueError
        return int((cost * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    except Exception as exc:
        raise ValueError("voice_cost_unavailable") from exc


class AtlasCallSpeech:
    def __init__(self) -> None:
        self.key = os.getenv("ATLAS_CALL_API_KEY", "").strip() or os.getenv("OPENROUTER_API_KEY", "").strip()
        self.tts_model = os.getenv("ATLAS_CALL_TTS_MODEL", "elevenlabs/eleven-v4-turbo").strip()
        self.stt_model = os.getenv("ATLAS_CALL_STT_MODEL", "elevenlabs/scribe-v2").strip()
        self.session: aiohttp.ClientSession | None = None
        self.slots = asyncio.Semaphore(4)
        self.rate: tuple[float, Decimal] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.key and not self.key.upper().startswith("YOUR_") and os.getenv("ATLAS_CALL_ENABLED", "1").lower() not in {"0", "false", "off"})

    async def client(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25, connect=4, sock_read=20), connector=aiohttp.TCPConnector(limit=8, ttl_dns_cache=300))
        return self.session

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}", "HTTP-Referer": "https://atlas.tvr.lat", "X-OpenRouter-Title": "Blackbird Atlas Call"}

    async def read_bounded(self, response: aiohttp.ClientResponse, maximum: int) -> bytes:
        result = bytearray()
        async for chunk in response.content.iter_chunked(32768):
            result.extend(chunk)
            if len(result) > maximum:
                raise ValueError("voice_provider_response_size")
        return bytes(result)

    async def character_rate(self) -> Decimal:
        if self.rate and time.monotonic() < self.rate[0]:
            return self.rate[1]
        client = await self.client()
        async with client.get("https://openrouter.ai/api/v1/models?output_modalities=speech") as response:
            response.raise_for_status()
            import json
            catalog = json.loads(await self.read_bounded(response, 1024 * 1024))
        model = next((item for item in catalog.get("data", []) if item.get("id") == self.tts_model), None)
        if not model or not model.get("pricing") or "prompt" not in model["pricing"]:
            raise ValueError("voice_price_unavailable")
        rate = Decimal(str(model["pricing"]["prompt"]))
        if not rate.is_finite() or rate <= 0 or rate > 1:
            raise ValueError("voice_price_unavailable")
        self.rate = (time.monotonic() + 900, rate)
        return rate

    async def transcribe(self, audio: bytes) -> dict[str, Any]:
        validate_call_wav(audio)
        async with asyncio.timeout(30), self.slots:
            client = await self.client()
            async with client.post("https://openrouter.ai/api/v1/audio/transcriptions", headers=self.headers(), json={"model": self.stt_model, "input_audio": {"data": base64.b64encode(audio).decode("ascii"), "format": "wav"}, "language": "ru", "response_format": "json"}) as response:
                response.raise_for_status()
                import json
                result = json.loads(await self.read_bounded(response, 64 * 1024))
                generation = response.headers.get("X-Generation-Id", "")[:160]
            usage = result.get("usage") or {}
            measured = cost_microusd(usage.get("cost"))
            text = " ".join(str(result.get("text") or "").split())[:4000]
            return {"text": text, "model": self.stt_model, "generation": generation, "usage": {"provider_cost_microusd": measured, "model_calls": 1, "total_tokens": int(usage.get("total_tokens") or 0)}}

    async def speak(self, text: str, voice: str) -> dict[str, Any]:
        if voice not in VOICES:
            raise ValueError("voice_unknown")
        spoken = atlas_tts_spoken_text(text, max_chars=600)
        # Caller/model markup must never control the performance or clone a voice.
        spoken = re.sub(r"\[[^\]\n]*\]", "", spoken).strip()
        if not spoken:
            raise ValueError("voice_text_required")
        async with asyncio.timeout(30), self.slots:
            rate = await self.character_rate()
            client = await self.client()
            async with client.post("https://openrouter.ai/api/v1/audio/speech", headers=self.headers(), json={"model": self.tts_model, "input": spoken, "voice": voice, "response_format": "mp3"}) as response:
                response.raise_for_status()
                if response.headers.get("Content-Type", "").split(";", 1)[0] not in {"audio/mpeg", "audio/mp3"}:
                    raise ValueError("voice_audio_provider_format")
                audio = await self.read_bounded(response, 2 * 1024 * 1024)
                generation = response.headers.get("X-Generation-Id", "")[:160]
            if len(audio) < 64:
                raise ValueError("voice_audio_empty")
            return {"audio": audio, "model": self.tts_model, "generation": generation, "usage": {"provider_cost_microusd": cost_microusd(rate * len(spoken)), "model_calls": 1}, "estimated": True}
