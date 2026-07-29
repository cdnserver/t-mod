"""T-Mod control plane for the isolated Discord user-client emulator."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands


def _enabled(name: str, default: bool = False) -> bool:
    fallback = "true" if default else "false"
    return os.getenv(name, fallback).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    value = int(raw) if raw.isdigit() else default
    return max(minimum, min(maximum, value))


def _color(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, hex(default)), 0)
    except (TypeError, ValueError):
        return default
    return max(0, min(0xFFFFFF, value))


BROWSER_STREAM_ENABLED = _enabled("BROWSER_STREAM_ENABLED")
BROWSER_STREAM_API_URL = (
    os.getenv(
        "BROWSER_STREAM_API_URL",
        "http://discord-browser-stream:8790",
    ).strip()
    or "http://discord-browser-stream:8790"
).rstrip("/")
BROWSER_STREAM_CONTROL_TOKEN = os.getenv(
    "BROWSER_STREAM_CONTROL_TOKEN",
    "",
).strip()
BROWSER_STREAM_ALLOWED_ROLE_ID = _integer(
    "BROWSER_STREAM_ALLOWED_ROLE_ID",
    1488207163879985233,
    0,
    2**63 - 1,
)
BROWSER_STREAM_TIMEOUT_SECONDS = _integer(
    "BROWSER_STREAM_TIMEOUT_SECONDS",
    8,
    2,
    30,
)
BROWSER_STREAM_EMBED_COLOR = _color(
    "BROWSER_STREAM_EMBED_COLOR",
    0xD9D9D9,
)


class BrowserStreamError(RuntimeError):
    """Raised when the emulated Discord client cannot fulfil a command."""


def browser_stream_authorized(member: discord.Member) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    if permissions and (
        permissions.administrator or permissions.manage_guild
    ):
        return True
    return BROWSER_STREAM_ALLOWED_ROLE_ID > 0 and any(
        int(getattr(role, "id", 0)) == BROWSER_STREAM_ALLOWED_ROLE_ID
        for role in getattr(member, "roles", ())
    )


def normalize_browser_stream_url(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    if len(cleaned) > 2048:
        raise ValueError("browser_stream_invalid_url")
    parsed = urlsplit(cleaned)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("browser_stream_invalid_url")
    return cleaned


class BrowserStreamClient:
    def __init__(
        self,
        *,
        base_url: str = BROWSER_STREAM_API_URL,
        control_token: str = BROWSER_STREAM_CONTROL_TOKEN,
        timeout_seconds: int = BROWSER_STREAM_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.control_token = str(control_token).strip()
        self.timeout_seconds = int(timeout_seconds)

    def _headers(self) -> dict[str, str]:
        if not self.control_token:
            raise BrowserStreamError("Ключ управления трансляцией не настроен.")
        return {
            "Authorization": f"Bearer {self.control_token}",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self._headers(),
                    json=dict(payload) if payload is not None else None,
                ) as response:
                    try:
                        body = await response.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        body = {}
                    if response.status >= 400:
                        error = str(body.get("error") or response.reason)
                        raise BrowserStreamError(
                            f"Сервис отклонил команду: {error}."
                        )
                    return dict(body)
        except BrowserStreamError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise BrowserStreamError(
                "Сервис трансляции недоступен. Проверьте контейнер "
                "discord-browser-stream."
            ) from exc

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/status")

    async def command(
        self,
        action: str,
        *,
        url: str | None = None,
        guild_id: int | None = None,
        voice_channel_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": str(action)}
        if url:
            payload["url"] = url
        if guild_id:
            payload["guild_id"] = str(guild_id)
        if voice_channel_id:
            payload["voice_channel_id"] = str(voice_channel_id)
        return await self._request(
            "POST",
            "/command",
            payload=payload,
        )


def browser_stream_embed(payload: Mapping[str, Any]) -> discord.Embed:
    state = str(payload.get("state") or "idle")
    active = bool(payload.get("active")) or state in {
        "starting",
        "streaming",
        "stopping",
    }
    title = (
        "📺 Трансляция запускается"
        if state == "starting"
        else "📺 Трансляция останавливается"
        if state == "stopping"
        else "📺 Трансляция активна"
        if active
        else "📺 Трансляция остановлена"
    )
    embed = discord.Embed(
        title=title,
        description=(
            "Отдельный тестовый Discord-клиент передаёт вкладку Chromium "
            "как демонстрацию экрана."
            if active
            else "Эмулируемый клиент сейчас не ведёт демонстрацию экрана."
        ),
        color=BROWSER_STREAM_EMBED_COLOR,
    )
    url = str(payload.get("url") or "").strip()
    if url:
        embed.add_field(name="Страница", value=url[:1024], inline=False)
    account = str(payload.get("account") or "").strip()
    if account:
        embed.add_field(name="Аккаунт", value=account[:1024], inline=True)
    voice_channel_id = str(payload.get("voice_channel_id") or "").strip()
    if voice_channel_id:
        embed.add_field(
            name="Голосовой канал",
            value=f"<#{voice_channel_id}>",
            inline=True,
        )
    last_error = str(payload.get("last_error") or "").strip()
    if last_error:
        embed.add_field(
            name="Последняя ошибка клиента",
            value=last_error[:1024],
            inline=False,
        )
    embed.set_footer(
        text=(
            "Изолированный пользовательский клиент · "
            "user token не передаётся основному боту"
        )
    )
    return embed


SCREEN_ACTIONS = [
    app_commands.Choice(name="Запустить или сменить страницу", value="start"),
    app_commands.Choice(name="Показать состояние", value="status"),
    app_commands.Choice(name="Остановить трансляцию", value="stop"),
    app_commands.Choice(name="Остановить и выйти из голоса", value="leave"),
]


def setup_browser_stream(
    bot: commands.Bot,
    remember_command_activity: Callable[[discord.Interaction, str, str], None],
) -> BrowserStreamClient:
    client = BrowserStreamClient()
    bot.browser_stream_client = client

    @bot.tree.command(
        name="screen",
        description="Управление браузером тестового Discord-клиента",
    )
    @app_commands.describe(
        action="Действие с трансляцией",
        url="Страница для запуска; без адреса повторяется последняя",
    )
    @app_commands.choices(action=SCREEN_ACTIONS)
    async def screen(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        url: str | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(
            interaction.user,
            discord.Member,
        ):
            await interaction.response.send_message(
                "Команда работает только на сервере Discord.",
                ephemeral=True,
            )
            return
        if not browser_stream_authorized(interaction.user):
            await interaction.response.send_message(
                "У вас нет доступа к управлению браузерной трансляцией.",
                ephemeral=True,
            )
            return
        if not BROWSER_STREAM_ENABLED:
            await interaction.response.send_message(
                "Эмулятор Discord-клиента отключён в настройках сервера.",
                ephemeral=True,
            )
            return

        try:
            normalized_url = normalize_browser_stream_url(url)
        except ValueError:
            await interaction.response.send_message(
                "Укажите полный адрес, начинающийся с `https://` или `http://`.",
                ephemeral=True,
            )
            return

        voice_channel_id = getattr(
            getattr(interaction.user, "voice", None),
            "channel",
            None,
        )
        voice_channel_id = getattr(voice_channel_id, "id", None)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if action.value == "status":
                payload = await client.status()
            else:
                payload = await client.command(
                    action.value,
                    url=normalized_url,
                    guild_id=interaction.guild.id,
                    voice_channel_id=voice_channel_id,
                )
        except BrowserStreamError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        remember_command_activity(interaction, "command_screen", "/screen")
        await interaction.followup.send(
            embed=browser_stream_embed(payload),
            ephemeral=True,
        )

    return client


__all__ = [
    "BrowserStreamClient",
    "BrowserStreamError",
    "browser_stream_authorized",
    "browser_stream_embed",
    "normalize_browser_stream_url",
    "setup_browser_stream",
]
