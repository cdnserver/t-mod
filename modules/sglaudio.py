import asyncio
import base64
import io
import json
import os
import re
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import discord
from discord.ext import commands
import requests

from persistence import sgl_repository as storage
from localization import safe_command_description, safe_command_name, t


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


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on", "y", "да"}


SGLAUDIO_COMMAND_NAME = safe_command_name("sglaudio.commands.sglaudio_name", "sglaudio")
SGLAUDIO_COMMAND_DESCRIPTION = safe_command_description("sglaudio.commands.sglaudio_description", "Generate AI audio")
SGLAUDIO_COMMAND_CHANNEL_ID = env_int("SGLAUDIO_COMMAND_CHANNEL_ID", env_int("SGBUREAU_COMMAND_CHANNEL_ID", 1500837640067485717))
SGLAUDIO_OUTPUT_CHANNEL_ID = env_int("SGLAUDIO_OUTPUT_CHANNEL_ID", 1500254662765449317)
SGLAUDIO_ALLOWED_ROLE_ID = env_int("SGLAUDIO_ALLOWED_ROLE_ID", 1488207163879985233)
SGLAUDIO_EMBED_COLOR = env_color("SGLAUDIO_EMBED_COLOR", "0xD9D9D9")
SGLAUDIO_TIMEOUT_SECONDS = env_int("SGLAUDIO_TIMEOUT_SECONDS", 600)
SGLAUDIO_STATUS_INTERVAL_SECONDS = max(1, env_int("SGLAUDIO_STATUS_INTERVAL_SECONDS", 1))
SGLAUDIO_MAX_DISCORD_FILE_BYTES = env_int("SGLAUDIO_MAX_DISCORD_FILE_BYTES", 24 * 1024 * 1024)
SGLAUDIO_OUTPUT_DIR = Path(os.getenv("SGLAUDIO_OUTPUT_DIR", "/app/persistent/data/audio_outputs"))
SGLAUDIO_MIN_SECONDS_BETWEEN_REQUESTS = max(0, env_int("SGLAUDIO_MIN_SECONDS_BETWEEN_REQUESTS", 45))

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/lyria-3-pro-preview").strip()
OPENROUTER_FALLBACK_MODEL = os.getenv("OPENROUTER_FALLBACK_MODEL", "google/lyria-3-clip-preview").strip()
OPENROUTER_REFERER = os.getenv("OPENROUTER_REFERER", "").strip()
OPENROUTER_TITLE = os.getenv("OPENROUTER_TITLE", "T-Mod").strip()
OPENROUTER_API_URL = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions").strip()
OPENROUTER_AUDIO_FORMAT = os.getenv("OPENROUTER_AUDIO_FORMAT", "wav").strip().lower() or "wav"
OPENROUTER_AUDIO_VOICE = os.getenv("OPENROUTER_AUDIO_VOICE", "").strip()
OPENROUTER_AUDIO_MODALITIES = [
    item.strip() for item in os.getenv("OPENROUTER_AUDIO_MODALITIES", "text,audio").split(",") if item.strip()
]
if "audio" not in OPENROUTER_AUDIO_MODALITIES:
    OPENROUTER_AUDIO_MODALITIES.append("audio")
OPENROUTER_STREAM_AUDIO = env_bool("OPENROUTER_STREAM_AUDIO", True)
OPENROUTER_AUDIO_RETRY_ATTEMPTS = max(1, env_int("OPENROUTER_AUDIO_RETRY_ATTEMPTS", 4))
OPENROUTER_AUDIO_RETRY_DELAY_SECONDS = max(0, env_int("OPENROUTER_AUDIO_RETRY_DELAY_SECONDS", 18))
OPENROUTER_AUDIO_STRICT_PROMPT = env_bool("OPENROUTER_AUDIO_STRICT_PROMPT", True)
OPENROUTER_FORCE_NO_FALLBACKS = env_bool("OPENROUTER_FORCE_NO_FALLBACKS", False)
OPENROUTER_DEBUG_SAVE_STREAM = env_bool("OPENROUTER_DEBUG_SAVE_STREAM", True)
OPENROUTER_AUDIO_START_MAX_ATTEMPTS = max(0, env_int("OPENROUTER_AUDIO_START_MAX_ATTEMPTS", 0))
OPENROUTER_AUDIO_START_RETRY_DELAY_SECONDS = max(1, env_int("OPENROUTER_AUDIO_START_RETRY_DELAY_SECONDS", 20))

FORMAT_TO_MIME = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "aac": "audio/aac",
    "m4a": "audio/mp4",
    "pcm16": "audio/wav",
}

AUDIO_URL_RE = re.compile(r"^https?://\S+\.(mp3|wav|m4a|ogg|flac|aac)(\?\S*)?$", re.IGNORECASE)
DATA_AUDIO_RE = re.compile(r"^data:(audio/[a-zA-Z0-9.+-]+);base64,(.+)$", re.DOTALL)

MIME_TO_EXT = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/aac": "aac",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
}


@dataclass(slots=True)
class GeneratedAudio:
    data: bytes
    filename: str
    mime_type: str
    raw_preview: str | None = None


@dataclass(slots=True)
class StreamParseResult:
    audio: GeneratedAudio | None
    text_preview: str
    raw_preview: str


class NoAudioFromOpenRouter(RuntimeError):
    pass


class RetriableOpenRouterError(RuntimeError):
    pass


_audio_generation_lock: asyncio.Lock | None = None
_last_generation_finished_monotonic: float = 0.0


def get_generation_lock() -> asyncio.Lock:
    global _audio_generation_lock
    if _audio_generation_lock is None:
        _audio_generation_lock = asyncio.Lock()
    return _audio_generation_lock


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def seconds_since(start: datetime) -> int:
    return max(0, int((now_utc() - start).total_seconds()))


def member_has_audio_access(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(role.id == SGLAUDIO_ALLOWED_ROLE_ID for role in member.roles)


def infer_mime_and_ext(data: bytes, mime: str | None = None) -> tuple[str, str]:
    if mime:
        normalized = mime.split(";")[0].strip().lower()
        return normalized, MIME_TO_EXT.get(normalized, "mp3")
    if data.startswith(b"RIFF") and b"WAVE" in data[:16]:
        return "audio/wav", "wav"
    if data.startswith(b"ID3") or data[:2] == b"\xff\xfb":
        return "audio/mpeg", "mp3"
    if data.startswith(b"OggS"):
        return "audio/ogg", "ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac", "flac"
    if b"ftyp" in data[:32]:
        return "audio/mp4", "m4a"
    return "audio/mpeg", "mp3"


def looks_like_audio_bytes(data: bytes) -> bool:
    if len(data) < 128:
        return False
    return (
        data.startswith(b"RIFF")
        or data.startswith(b"ID3")
        or data[:2] == b"\xff\xfb"
        or data.startswith(b"OggS")
        or data.startswith(b"fLaC")
        or b"ftyp" in data[:32]
    )


def decode_base64_audio(value: str, mime: str | None = None, trusted_audio_context: bool = False) -> GeneratedAudio | None:
    text = value.strip()
    match = DATA_AUDIO_RE.match(text)
    if match:
        mime_type = match.group(1)
        payload = match.group(2)
    else:
        mime_type = mime
        payload = text

    if len(payload) < 512:
        return None
    try:
        payload = re.sub(r"\s+", "", payload)
        data = base64.b64decode(payload, validate=True)
    except Exception:
        return None

    if not trusted_audio_context and not looks_like_audio_bytes(data):
        return None

    resolved_mime, ext = infer_mime_and_ext(data, mime_type)
    return GeneratedAudio(data=data, filename=f"tmod_audio.{ext}", mime_type=resolved_mime)


def download_audio(url: str, timeout: int) -> GeneratedAudio:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    data = response.content
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower() or None
    mime, ext = infer_mime_and_ext(data, content_type if content_type and content_type.startswith("audio/") else None)
    return GeneratedAudio(data=data, filename=f"tmod_audio.{ext}", mime_type=mime)


def find_audio_in_json(obj: Any, timeout: int, path: str = "") -> GeneratedAudio | None:
    if isinstance(obj, dict):
        lower_keys = {str(k).lower(): v for k, v in obj.items()}
        audio_context = "audio" in path.lower() or any("audio" in str(k).lower() for k in obj.keys())

        for key in ("url", "audio_url", "download_url"):
            value = lower_keys.get(key)
            if isinstance(value, str):
                if value.startswith("data:audio/"):
                    audio = decode_base64_audio(value, trusted_audio_context=True)
                    if audio:
                        return audio
                if AUDIO_URL_RE.match(value) or ("audio" in value.lower() and value.startswith("http")):
                    return download_audio(value, timeout)
            if isinstance(value, dict):
                nested = find_audio_in_json(value, timeout, path=f"{path}.{key}")
                if nested:
                    return nested

        for key in ("data", "base64", "b64_json", "audio", "content"):
            value = lower_keys.get(key)
            if isinstance(value, str):
                mime = None
                for mime_key in ("mime_type", "mime", "content_type"):
                    if isinstance(lower_keys.get(mime_key), str):
                        mime = str(lower_keys[mime_key])
                        break
                audio = decode_base64_audio(value, mime=mime, trusted_audio_context=audio_context or key in {"audio", "b64_json"})
                if audio:
                    return audio

        for key, value in obj.items():
            nested = find_audio_in_json(value, timeout, path=f"{path}.{key}")
            if nested:
                return nested

    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            nested = find_audio_in_json(value, timeout, path=f"{path}[{index}]")
            if nested:
                return nested

    elif isinstance(obj, str):
        value = obj.strip()
        if value.startswith("data:audio/"):
            return decode_base64_audio(value, trusted_audio_context=True)
        if AUDIO_URL_RE.match(value):
            return download_audio(value, timeout)

    return None


def normalize_prompt_for_audio(prompt: str, attempt: int = 1) -> str:
    if not OPENROUTER_AUDIO_STRICT_PROMPT:
        return prompt
    extra = ""
    if attempt > 1:
        extra = (
            "\n\nIMPORTANT RETRY INSTRUCTION: The previous request did not return audio chunks. "
            "Do not return lyrics, timestamps, JSON, chord labels, explanations, or a text-only song plan. "
            "Return actual audio output only."
        )
    return (
        "Generate a complete music audio file from this prompt. "
        "The response must include real audio output through delta.audio.data streaming chunks. "
        "Do not answer with only lyrics, sections, timestamps, text, or a composition plan."
        f"{extra}\n\nPrompt: {prompt}"
    )


def model_sequence() -> list[str]:
    models: list[str] = []
    for model in (OPENROUTER_MODEL, OPENROUTER_FALLBACK_MODEL):
        model = (model or "").strip()
        if model and model not in models:
            models.append(model)
    return models or ["google/lyria-3-pro-preview"]


def build_openrouter_payload(prompt: str, stream_audio: bool, *, model: str, attempt: int) -> dict[str, Any]:
    audio_config: dict[str, str] = {"format": OPENROUTER_AUDIO_FORMAT}
    if OPENROUTER_AUDIO_VOICE:
        audio_config["voice"] = OPENROUTER_AUDIO_VOICE

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": normalize_prompt_for_audio(prompt, attempt=attempt),
            }
        ],
        "modalities": OPENROUTER_AUDIO_MODALITIES,
        "audio": audio_config,
    }
    if stream_audio:
        payload["stream"] = True
    if OPENROUTER_FORCE_NO_FALLBACKS:
        payload["provider"] = {"allow_fallbacks": False}
    return payload


def collect_text_from_openrouter_json(obj: Any) -> str:
    if isinstance(obj, dict):
        choices = obj.get("choices")
        if isinstance(choices, list):
            parts: list[str] = []
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message") or choice.get("delta")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        parts.append(content)
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and isinstance(item.get("text"), str):
                                parts.append(item["text"])
                if isinstance(choice.get("text"), str):
                    parts.append(choice["text"])
            if parts:
                return "\n".join(parts)
        for value in obj.values():
            nested = collect_text_from_openrouter_json(value)
            if nested:
                return nested
    elif isinstance(obj, list):
        for value in obj:
            nested = collect_text_from_openrouter_json(value)
            if nested:
                return nested
    elif isinstance(obj, str):
        return obj
    return ""


def parse_streaming_audio_response(response: requests.Response) -> StreamParseResult:
    audio_chunks: list[str] = []
    transcript_chunks: list[str] = []
    text_chunks: list[str] = []
    raw_preview_lines: list[str] = []

    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        if len(raw_preview_lines) < 8:
            raw_preview_lines.append(data[:700])
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue

        choices = chunk.get("choices") if isinstance(chunk, dict) else None
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str):
                text_chunks.append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        text_chunks.append(item["text"])

            audio = delta.get("audio") or delta.get("output_audio")
            if isinstance(audio, dict):
                value = audio.get("data") or audio.get("b64_json") or audio.get("base64")
                if isinstance(value, str) and value.strip():
                    audio_chunks.append(value.strip())
                transcript = audio.get("transcript") or audio.get("text")
                if isinstance(transcript, str):
                    transcript_chunks.append(transcript)
            elif isinstance(audio, str) and audio.strip():
                audio_chunks.append(audio.strip())

    text_preview = ("".join(transcript_chunks) or "".join(text_chunks)).strip()[:1800]
    raw_preview = "\n".join(raw_preview_lines)[:3000]

    if not audio_chunks:
        return StreamParseResult(audio=None, text_preview=text_preview, raw_preview=raw_preview)

    payload = re.sub(r"\s+", "", "".join(audio_chunks))
    try:
        data = base64.b64decode(payload, validate=False)
    except Exception as exc:
        raise NoAudioFromOpenRouter(f"Audio chunks were present, but base64 decoding failed: {exc}") from exc

    requested_mime = FORMAT_TO_MIME.get(OPENROUTER_AUDIO_FORMAT, "audio/wav")
    mime, ext = infer_mime_and_ext(data, requested_mime)
    audio = GeneratedAudio(
        data=data,
        filename=f"tmod_audio.{ext}",
        mime_type=mime,
        raw_preview=text_preview if text_preview else None,
    )
    return StreamParseResult(audio=audio, text_preview=text_preview, raw_preview=raw_preview)


def no_audio_error_text(model: str, attempt: int, text_preview: str, raw_preview: str) -> str:
    details = []
    if text_preview:
        details.append(f"Text preview: {text_preview[:900]}")
    if raw_preview:
        details.append(f"Raw stream preview: {raw_preview[:900]}")
    detail_text = "\n".join(details) if details else "No text preview was returned."
    return t(
        "sglaudio.errors.no_audio_stream_retry_exhausted",
        model=model,
        attempts=attempt,
        body=detail_text[:1700],
    )


def request_openrouter_once(prompt: str, *, model: str, attempt: int) -> GeneratedAudio:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if OPENROUTER_STREAM_AUDIO else "application/json",
    }
    if OPENROUTER_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_REFERER
    if OPENROUTER_TITLE:
        headers["X-OpenRouter-Title"] = OPENROUTER_TITLE

    payload = build_openrouter_payload(prompt, OPENROUTER_STREAM_AUDIO, model=model, attempt=attempt)

    response = requests.post(
        OPENROUTER_API_URL,
        headers=headers,
        json=payload,
        timeout=SGLAUDIO_TIMEOUT_SECONDS,
        stream=OPENROUTER_STREAM_AUDIO,
    )

    if response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
        preview = response.text[:1000]
        raise RetriableOpenRouterError(t("sglaudio.errors.openrouter_http", status=response.status_code, body=preview))

    if response.status_code >= 400:
        preview = response.text[:1000]
        raise RuntimeError(t("sglaudio.errors.openrouter_http", status=response.status_code, body=preview))

    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if content_type.startswith("audio/"):
        mime, ext = infer_mime_and_ext(response.content, content_type)
        return GeneratedAudio(data=response.content, filename=f"tmod_audio.{ext}", mime_type=mime)

    if OPENROUTER_STREAM_AUDIO:
        parsed = parse_streaming_audio_response(response)
        if parsed.audio:
            return parsed.audio
        raise NoAudioFromOpenRouter(no_audio_error_text(model, attempt, parsed.text_preview, parsed.raw_preview))

    try:
        payload_json = response.json()
    except ValueError:
        text = response.text.strip()
        audio = decode_base64_audio(text, trusted_audio_context=True)
        if audio:
            return audio
        raise RuntimeError(t("sglaudio.errors.no_json", body=text[:1000]))

    audio = find_audio_in_json(payload_json, SGLAUDIO_TIMEOUT_SECONDS)
    if audio:
        return audio

    text_output = collect_text_from_openrouter_json(payload_json).strip()
    if text_output:
        preview = text_output[:1500]
        raise NoAudioFromOpenRouter(t("sglaudio.errors.text_only_response", body=preview))

    preview = json.dumps(payload_json, ensure_ascii=False)[:1500]
    raise NoAudioFromOpenRouter(t("sglaudio.errors.no_audio_in_response", body=preview))


def call_openrouter_generate_audio(prompt: str) -> GeneratedAudio:
    if not OPENROUTER_API_KEY or OPENROUTER_API_KEY in {"YOUR_OPENROUTER_API_KEY_HERE", "paste_openrouter_key_here"}:
        raise RuntimeError(t("sglaudio.errors.api_key_missing"))

    errors: list[str] = []
    models = model_sequence()
    total_attempts = 0
    cycle = 0

    # Lyria through OpenRouter can intermittently answer with text/lyrics instead
    # of audio chunks. For /sglaudio we keep trying until a real audio stream is
    # received. Set OPENROUTER_AUDIO_START_MAX_ATTEMPTS=0 for unlimited retries.
    while True:
        cycle += 1
        for model in models:
            for attempt in range(1, OPENROUTER_AUDIO_RETRY_ATTEMPTS + 1):
                total_attempts += 1
                try:
                    return request_openrouter_once(prompt, model=model, attempt=attempt)
                except NoAudioFromOpenRouter as exc:
                    errors.append(f"cycle {cycle} / {model} attempt {attempt}: {exc}")
                    if OPENROUTER_AUDIO_START_MAX_ATTEMPTS and total_attempts >= OPENROUTER_AUDIO_START_MAX_ATTEMPTS:
                        joined = "\n\n".join(errors[-5:])
                        raise RuntimeError(t("sglaudio.errors.no_audio_after_retries", attempts=total_attempts, body=joined[:1800]))
                    time.sleep(OPENROUTER_AUDIO_RETRY_DELAY_SECONDS if attempt < OPENROUTER_AUDIO_RETRY_ATTEMPTS else OPENROUTER_AUDIO_START_RETRY_DELAY_SECONDS)
                    continue
                except RetriableOpenRouterError as exc:
                    errors.append(f"cycle {cycle} / {model} attempt {attempt}: {exc}")
                    if OPENROUTER_AUDIO_START_MAX_ATTEMPTS and total_attempts >= OPENROUTER_AUDIO_START_MAX_ATTEMPTS:
                        joined = "\n\n".join(errors[-5:])
                        raise RuntimeError(t("sglaudio.errors.no_audio_after_retries", attempts=total_attempts, body=joined[:1800]))
                    time.sleep(OPENROUTER_AUDIO_RETRY_DELAY_SECONDS if attempt < OPENROUTER_AUDIO_RETRY_ATTEMPTS else OPENROUTER_AUDIO_START_RETRY_DELAY_SECONDS)
                    continue


def save_audio_file(record_id: int, audio: GeneratedAudio) -> Path:
    SGLAUDIO_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(audio.filename).suffix or ".mp3"
    file_path = SGLAUDIO_OUTPUT_DIR / f"sglaudio_{record_id:06d}{suffix}"
    file_path.write_bytes(audio.data)
    return file_path


async def edit_status(interaction: discord.Interaction, content: str) -> None:
    try:
        await interaction.edit_original_response(content=content)
    except (discord.NotFound, discord.HTTPException):
        pass


async def status_updater(interaction: discord.Interaction, start: datetime, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        elapsed = seconds_since(start)
        await edit_status(interaction, t("sglaudio.status.generating_channel", seconds=elapsed, model=OPENROUTER_MODEL, channel_id=SGLAUDIO_OUTPUT_CHANNEL_ID))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SGLAUDIO_STATUS_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


async def get_audio_output_channel(bot: commands.Bot) -> Any:
    channel = bot.get_channel(SGLAUDIO_OUTPUT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(SGLAUDIO_OUTPUT_CHANNEL_ID)
        except discord.DiscordException as exc:
            raise RuntimeError(t("sglaudio.errors.output_channel_not_found", channel_id=SGLAUDIO_OUTPUT_CHANNEL_ID, error=exc)) from exc

    if not hasattr(channel, "send"):
        raise RuntimeError(t("sglaudio.errors.output_channel_not_sendable", channel_id=SGLAUDIO_OUTPUT_CHANNEL_ID))
    return channel


async def wait_for_global_audio_turn(interaction: discord.Interaction, start: datetime) -> None:
    global _last_generation_finished_monotonic
    lock = get_generation_lock()
    if lock.locked():
        await edit_status(interaction, t("sglaudio.status.queued", model=OPENROUTER_MODEL))
    await lock.acquire()
    wait_for = 0.0
    if SGLAUDIO_MIN_SECONDS_BETWEEN_REQUESTS > 0 and _last_generation_finished_monotonic > 0:
        wait_for = max(0.0, SGLAUDIO_MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_generation_finished_monotonic))
    if wait_for > 0:
        await edit_status(interaction, t("sglaudio.status.cooldown", seconds=int(wait_for), model=OPENROUTER_MODEL))
        await asyncio.sleep(wait_for)


def release_global_audio_turn() -> None:
    global _last_generation_finished_monotonic
    _last_generation_finished_monotonic = time.monotonic()
    lock = get_generation_lock()
    if lock.locked():
        lock.release()


class SGLAudioPromptModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title=t("sglaudio.modal.title"), timeout=600)
        self.prompt = discord.ui.TextInput(
            label=t("sglaudio.modal.prompt_label"),
            placeholder=t("sglaudio.modal.prompt_placeholder"),
            style=discord.TextStyle.paragraph,
            min_length=5,
            max_length=1900,
            required=True,
        )
        self.add_item(self.prompt)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(t("sglaudio.errors.guild_only"), ephemeral=True)
            return
        if interaction.channel_id != SGLAUDIO_COMMAND_CHANNEL_ID:
            await interaction.response.send_message(t("sglaudio.errors.wrong_channel", channel_id=SGLAUDIO_COMMAND_CHANNEL_ID), ephemeral=True)
            return
        if not member_has_audio_access(interaction.user):
            await interaction.response.send_message(t("sglaudio.errors.no_permission", role_id=SGLAUDIO_ALLOWED_ROLE_ID), ephemeral=True)
            return

        prompt = str(self.prompt.value).strip()
        if not prompt:
            await interaction.response.send_message(t("sglaudio.errors.empty_prompt"), ephemeral=True)
            return

        start = now_utc()
        record_id = await asyncio.to_thread(
            storage.create_audio_generation,
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id or 0,
            user_id=interaction.user.id,
            user_display=interaction.user.display_name,
            prompt=prompt,
            model=OPENROUTER_MODEL,
        )

        await interaction.response.defer(ephemeral=True, thinking=True)
        stop_event = asyncio.Event()
        updater = asyncio.create_task(status_updater(interaction, start, stop_event))
        turn_acquired = False

        try:
            await edit_status(interaction, t("sglaudio.status.started", id=record_id, model=OPENROUTER_MODEL))
            await wait_for_global_audio_turn(interaction, start)
            turn_acquired = True
            audio = await asyncio.to_thread(call_openrouter_generate_audio, prompt)
            elapsed = seconds_since(start)
            file_path = await asyncio.to_thread(save_audio_file, record_id, audio)

            if len(audio.data) > SGLAUDIO_MAX_DISCORD_FILE_BYTES:
                raise RuntimeError(t("sglaudio.errors.file_too_large", size=len(audio.data), limit=SGLAUDIO_MAX_DISCORD_FILE_BYTES, path=str(file_path)))

            output_channel = await get_audio_output_channel(interaction.client)
            embed = discord.Embed(
                title=t("sglaudio.channel.title"),
                description=t("sglaudio.channel.description", seconds=elapsed, model=OPENROUTER_MODEL, requester=interaction.user.mention),
                color=SGLAUDIO_EMBED_COLOR,
            )
            embed.add_field(name=t("sglaudio.channel.prompt_field"), value=prompt[:1000], inline=False)
            embed.set_footer(text=t("sglaudio.channel.footer", id=record_id))
            file = discord.File(io.BytesIO(audio.data), filename=file_path.name)
            output_message = await output_channel.send(
                content=t("sglaudio.channel.content", requester=interaction.user.mention, id=record_id),
                embed=embed,
                file=file,
            )

            await asyncio.to_thread(
                storage.update_audio_generation,
                record_id,
                status="sent",
                seconds_elapsed=elapsed,
                output_filename=str(file_path),
                output_mime=audio.mime_type,
                output_size_bytes=len(audio.data),
                dm_message_id=output_message.id,
                completed_at=now_utc().isoformat(),
            )
            stop_event.set()
            updater.cancel()
            try:
                await updater
            except asyncio.CancelledError:
                pass
            await edit_status(interaction, t("sglaudio.status.done_channel", seconds=elapsed, id=record_id, channel_id=SGLAUDIO_OUTPUT_CHANNEL_ID))
        except Exception as exc:
            elapsed = seconds_since(start)
            error_text = str(exc)
            await asyncio.to_thread(
                storage.update_audio_generation,
                record_id,
                status="failed",
                seconds_elapsed=elapsed,
                error=error_text[:3000],
                completed_at=now_utc().isoformat(),
            )
            traceback.print_exc()
            stop_event.set()
            updater.cancel()
            try:
                await updater
            except asyncio.CancelledError:
                pass
            await edit_status(interaction, t("sglaudio.status.failed", seconds=elapsed, error=error_text[:1800], id=record_id))
        finally:
            if turn_acquired:
                release_global_audio_turn()
            stop_event.set()
            if not updater.done():
                updater.cancel()
                try:
                    await updater
                except asyncio.CancelledError:
                    pass

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        traceback.print_exception(type(error), error, error.__traceback__)
        if interaction.response.is_done():
            await interaction.followup.send(t("sglaudio.errors.modal_failed", error=error), ephemeral=True)
        else:
            await interaction.response.send_message(t("sglaudio.errors.modal_failed", error=error), ephemeral=True)


def setup_sglaudio(bot: commands.Bot, remember_command_activity: Callable[[discord.Interaction, str, str], None]) -> None:
    @bot.tree.command(name=SGLAUDIO_COMMAND_NAME, description=SGLAUDIO_COMMAND_DESCRIPTION)
    async def sglaudio(interaction: discord.Interaction) -> None:
        remember_command_activity(interaction, "command_sglaudio", "/sglaudio")
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(t("sglaudio.errors.guild_only"), ephemeral=True)
            return
        if interaction.channel_id != SGLAUDIO_COMMAND_CHANNEL_ID:
            await interaction.response.send_message(t("sglaudio.errors.wrong_channel", channel_id=SGLAUDIO_COMMAND_CHANNEL_ID), ephemeral=True)
            return
        if not member_has_audio_access(interaction.user):
            await interaction.response.send_message(t("sglaudio.errors.no_permission", role_id=SGLAUDIO_ALLOWED_ROLE_ID), ephemeral=True)
            return
        if not OPENROUTER_API_KEY or OPENROUTER_API_KEY in {"YOUR_OPENROUTER_API_KEY_HERE", "paste_openrouter_key_here"}:
            await interaction.response.send_message(t("sglaudio.errors.api_key_missing"), ephemeral=True)
            return
        await interaction.response.send_modal(SGLAudioPromptModal())
