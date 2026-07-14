import asyncio
import io
import os
import re
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import discord
from discord.ext import commands
import requests

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


ZIGMUND_COMMAND_NAME = safe_command_name("zigmund.commands.zigmund_name", "zigmund")
ZIGMUND_COMMAND_DESCRIPTION = safe_command_description("zigmund.commands.zigmund_description", "Сгенерировать изображение через Zigmund AI")
ZIGMUND_COMMAND_CHANNEL_ID = env_int("ZIGMUND_COMMAND_CHANNEL_ID", env_int("SGLAUDIO_COMMAND_CHANNEL_ID", 1500837640067485717))
ZIGMUND_ALLOWED_ROLE_ID = env_int("ZIGMUND_ALLOWED_ROLE_ID", env_int("SGLAUDIO_ALLOWED_ROLE_ID", 1488207163879985233))
ZIGMUND_EMBED_COLOR = env_color("ZIGMUND_EMBED_COLOR", "0xD9D9D9")
ZIGMUND_TIMEOUT_SECONDS = env_int("ZIGMUND_TIMEOUT_SECONDS", 300)
ZIGMUND_OUTPUT_DIR = Path(os.getenv("ZIGMUND_OUTPUT_DIR", "/app/persistent/data/zigmund_outputs"))
ZIGMUND_PROMPT_PREFIX = os.getenv("ZIGMUND_PROMPT_PREFIX", "Сгенерируй аниме эччи картинку.").strip()
ZIGMUND_SEND_AS_SPOILER = os.getenv("ZIGMUND_SEND_AS_SPOILER", "true").strip().lower() in {"1", "true", "yes", "on", "y", "да"}
ZIGMUND_MAX_ATTEMPTS = max(1, env_int("ZIGMUND_MAX_ATTEMPTS", 3))
ZIGMUND_RETRY_DELAY_SECONDS = max(1, env_int("ZIGMUND_RETRY_DELAY_SECONDS", 8))

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_API_URL = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions").strip()
OPENROUTER_REFERER = os.getenv("OPENROUTER_REFERER", "").strip()
OPENROUTER_TITLE = os.getenv("OPENROUTER_TITLE", "T-Mod").strip()
ZIGMUND_MODEL = os.getenv("ZIGMUND_MODEL", "x-ai/grok-imagine-image-quality").strip()

IMAGE_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
DATA_IMAGE_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.DOTALL)

MIME_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


@dataclass(slots=True)
class GeneratedImage:
    data: bytes
    filename: str
    mime_type: str
    raw_preview: str | None = None


def member_has_zigmund_access(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(role.id == ZIGMUND_ALLOWED_ROLE_ID for role in member.roles)


def infer_mime_and_ext(data: bytes, mime: str | None = None) -> tuple[str, str]:
    if mime:
        normalized = mime.split(";")[0].strip().lower()
        return normalized, MIME_TO_EXT.get(normalized, "png")
    if data.startswith(b"\x89PNG"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "image/webp", "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    return "image/png", "png"


def looks_like_image_bytes(data: bytes) -> bool:
    return (
        data.startswith(b"\x89PNG")
        or data.startswith(b"\xff\xd8\xff")
        or (data.startswith(b"RIFF") and b"WEBP" in data[:16])
        or data.startswith((b"GIF87a", b"GIF89a"))
    )


def decode_data_url(value: str) -> GeneratedImage | None:
    import base64
    match = DATA_IMAGE_RE.match(value.strip())
    if not match:
        return None
    mime = match.group(1)
    b64 = re.sub(r"\s+", "", match.group(2))
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if not looks_like_image_bytes(data):
        return None
    mime, ext = infer_mime_and_ext(data, mime)
    return GeneratedImage(data=data, filename=f"zigmund.{ext}", mime_type=mime)


def download_image(url: str, timeout: int) -> GeneratedImage | None:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    ctype = response.headers.get("Content-Type", "").split(";")[0].strip().lower() or None
    data = response.content
    if not looks_like_image_bytes(data) and not (ctype and ctype.startswith("image/")):
        return None
    mime, ext = infer_mime_and_ext(data, ctype)
    return GeneratedImage(data=data, filename=f"zigmund.{ext}", mime_type=mime)


def maybe_extract_image_from_string(value: str, timeout: int) -> GeneratedImage | None:
    text = value.strip()
    if text.startswith("data:image/"):
        return decode_data_url(text)
    for url in IMAGE_URL_RE.findall(text):
        lower = url.lower()
        if any(ext in lower for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]) or "image" in lower:
            try:
                image = download_image(url, timeout)
                if image:
                    return image
            except Exception:
                continue
    return None


def find_image_in_json(obj: Any, timeout: int, path: str = "") -> GeneratedImage | None:
    if isinstance(obj, dict):
        lower = {str(k).lower(): v for k, v in obj.items()}

        # Common image payload structures.
        image_url = lower.get("image_url")
        if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
            value = image_url.get("url")
            if value.startswith("data:image/"):
                image = decode_data_url(value)
                if image:
                    return image
            try:
                image = download_image(value, timeout)
                if image:
                    return image
            except Exception:
                pass
        elif isinstance(image_url, str):
            image = maybe_extract_image_from_string(image_url, timeout)
            if image:
                return image

        for key in ("url", "image", "data", "b64_json", "content"):
            value = lower.get(key)
            if isinstance(value, str):
                image = maybe_extract_image_from_string(value, timeout)
                if image:
                    return image
            elif isinstance(value, dict) or isinstance(value, list):
                image = find_image_in_json(value, timeout, path=f"{path}.{key}")
                if image:
                    return image

        for _, value in obj.items():
            if isinstance(value, (dict, list, str)):
                image = find_image_in_json(value, timeout, path=path)
                if image:
                    return image
    elif isinstance(obj, list):
        for item in obj:
            image = find_image_in_json(item, timeout, path=path)
            if image:
                return image
    elif isinstance(obj, str):
        return maybe_extract_image_from_string(obj, timeout)
    return None


def call_openrouter_generate_image(user_prompt: str) -> GeneratedImage:
    full_prompt = f"{ZIGMUND_PROMPT_PREFIX} {user_prompt}".strip()
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    if OPENROUTER_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_REFERER
    if OPENROUTER_TITLE:
        headers["X-OpenRouter-Title"] = OPENROUTER_TITLE

    payload = {
        "model": ZIGMUND_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": full_prompt,
                    }
                ],
            }
        ],
    }

    last_error = None
    for attempt in range(1, ZIGMUND_MAX_ATTEMPTS + 1):
        response = None
        try:
            response = requests.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=ZIGMUND_TIMEOUT_SECONDS)
            response.raise_for_status()
            body = response.json()
            image = find_image_in_json(body, timeout=ZIGMUND_TIMEOUT_SECONDS)
            if image:
                preview = str(body)
                image.raw_preview = preview[:1800]
                return image
            last_error = RuntimeError(t("zigmund.errors.no_image_in_response", body=str(body)[:1800]))
        except Exception as exc:
            last_error = exc
        if attempt < ZIGMUND_MAX_ATTEMPTS:
            import time
            time.sleep(ZIGMUND_RETRY_DELAY_SECONDS)
    if last_error:
        raise last_error
    raise RuntimeError(t("zigmund.errors.unknown_generation_error"))


def save_image_file(image: GeneratedImage) -> Path:
    ZIGMUND_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(image.filename).suffix or ".png"
    import datetime
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    out = ZIGMUND_OUTPUT_DIR / f"zigmund_{stamp}{suffix}"
    out.write_bytes(image.data)
    return out


class PublishZigmundView(discord.ui.View):
    def __init__(self, requester_id: int, prompt: str, file_path: str) -> None:
        super().__init__(timeout=3600)
        self.requester_id = requester_id
        self.prompt = prompt
        self.file_path = file_path

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(t("zigmund.errors.not_for_you"), ephemeral=True)
            return False
        return True

    @discord.ui.button(label=t("zigmund.preview.publish_button"), style=discord.ButtonStyle.secondary)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        path = Path(self.file_path)
        if not path.exists():
            await interaction.response.send_message(t("zigmund.errors.file_missing"), ephemeral=True)
            return
        data = path.read_bytes()
        file = discord.File(io.BytesIO(data), filename=path.name, spoiler=ZIGMUND_SEND_AS_SPOILER)
        embed = discord.Embed(
            title=t("zigmund.channel.title"),
            description=t("zigmund.channel.description", requester=interaction.user.mention),
            color=ZIGMUND_EMBED_COLOR,
        )
        embed.add_field(name=t("zigmund.channel.prompt_field"), value=self.prompt[:1024], inline=False)
        embed.set_footer(text=t("zigmund.channel.footer", model=ZIGMUND_MODEL))
        await interaction.channel.send(file=file, embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await interaction.response.send_message(t("zigmund.status.published"), ephemeral=True)


class ZigmundPromptModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title=t("zigmund.modal.title"), timeout=600)
        self.prompt = discord.ui.TextInput(
            label=t("zigmund.modal.prompt_label"),
            placeholder=t("zigmund.modal.prompt_placeholder"),
            style=discord.TextStyle.paragraph,
            min_length=2,
            max_length=1900,
            required=True,
        )
        self.add_item(self.prompt)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(t("zigmund.errors.guild_only"), ephemeral=True)
            return
        if interaction.channel_id != ZIGMUND_COMMAND_CHANNEL_ID:
            await interaction.response.send_message(t("zigmund.errors.wrong_channel", channel_id=ZIGMUND_COMMAND_CHANNEL_ID), ephemeral=True)
            return
        if not member_has_zigmund_access(interaction.user):
            await interaction.response.send_message(t("zigmund.errors.no_permission", role_id=ZIGMUND_ALLOWED_ROLE_ID), ephemeral=True)
            return
        if not OPENROUTER_API_KEY or OPENROUTER_API_KEY in {"YOUR_OPENROUTER_API_KEY_HERE", "paste_openrouter_key_here"}:
            await interaction.response.send_message(t("zigmund.errors.api_key_missing"), ephemeral=True)
            return

        prompt = str(self.prompt.value).strip()
        if not prompt:
            await interaction.response.send_message(t("zigmund.errors.empty_prompt"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.edit_original_response(content=t("zigmund.status.generating"), embed=None, view=None, attachments=[])
            image = await asyncio.to_thread(call_openrouter_generate_image, prompt)
            file_path = await asyncio.to_thread(save_image_file, image)
            file = discord.File(io.BytesIO(image.data), filename=file_path.name, spoiler=ZIGMUND_SEND_AS_SPOILER)
            embed = discord.Embed(
                title=t("zigmund.preview.title"),
                description=t("zigmund.preview.description"),
                color=ZIGMUND_EMBED_COLOR,
            )
            embed.add_field(name=t("zigmund.channel.prompt_field"), value=prompt[:1024], inline=False)
            embed.set_footer(text=t("zigmund.preview.footer", model=ZIGMUND_MODEL))
            await interaction.edit_original_response(content=t("zigmund.status.done_private"), embed=embed, attachments=[file], view=PublishZigmundView(interaction.user.id, prompt, str(file_path)))
        except Exception as exc:
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            await interaction.edit_original_response(content=t("zigmund.status.failed", error=str(exc)[:1800]), embed=None, view=None, attachments=[])

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        traceback.print_exception(type(error), error, error.__traceback__)
        if interaction.response.is_done():
            await interaction.followup.send(t("zigmund.errors.modal_failed", error=error), ephemeral=True)
        else:
            await interaction.response.send_message(t("zigmund.errors.modal_failed", error=error), ephemeral=True)


def setup_zigmund(bot: commands.Bot, remember_command_activity: Callable[[discord.Interaction, str, str], None]) -> None:
    @bot.tree.command(name=ZIGMUND_COMMAND_NAME, description=ZIGMUND_COMMAND_DESCRIPTION)
    async def zigmund(interaction: discord.Interaction) -> None:
        remember_command_activity(interaction, "command_zigmund", "/zigmund")
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(t("zigmund.errors.guild_only"), ephemeral=True)
            return
        if interaction.channel_id != ZIGMUND_COMMAND_CHANNEL_ID:
            await interaction.response.send_message(t("zigmund.errors.wrong_channel", channel_id=ZIGMUND_COMMAND_CHANNEL_ID), ephemeral=True)
            return
        if not member_has_zigmund_access(interaction.user):
            await interaction.response.send_message(t("zigmund.errors.no_permission", role_id=ZIGMUND_ALLOWED_ROLE_ID), ephemeral=True)
            return
        if not OPENROUTER_API_KEY or OPENROUTER_API_KEY in {"YOUR_OPENROUTER_API_KEY_HERE", "paste_openrouter_key_here"}:
            await interaction.response.send_message(t("zigmund.errors.api_key_missing"), ephemeral=True)
            return
        await interaction.response.send_modal(ZigmundPromptModal())
