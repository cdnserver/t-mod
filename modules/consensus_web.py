"""Read-only, token-protected LAN dashboard for live consensus."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from aiohttp import web
from discord.ext import commands

from modules.consensus_runtime import active_sessions
from persistence import activity_repository as meta_storage
from persistence import tvrs_repository as tvrs_storage


CONSENSUS_WEB_ENABLED = os.getenv(
    "CONSENSUS_WEB_ENABLED",
    "true",
).strip().lower() in {"1", "true", "yes", "on"}
CONSENSUS_WEB_HOST = os.getenv("CONSENSUS_WEB_HOST", "0.0.0.0").strip() or "0.0.0.0"
try:
    CONSENSUS_WEB_PORT = max(
        1,
        min(65535, int(os.getenv("CONSENSUS_WEB_PORT", "8787") or 8787)),
    )
except (TypeError, ValueError):
    CONSENSUS_WEB_PORT = 8787
CONSENSUS_WEB_PUBLIC_NAME = (
    os.getenv("CONSENSUS_WEB_PUBLIC_NAME", "t.consensus").strip()
    or "t.consensus"
)


def _configured_guild_id() -> int:
    raw = (
        os.getenv("CONSENSUS_WEB_GUILD_ID")
        or os.getenv("DISCORD_GUILD_ID")
        or "0"
    ).strip()
    return int(raw) if raw.isdigit() else 0


CONSENSUS_WEB_GUILD_ID = _configured_guild_id()
_TOKEN_META_KEY = "consensus_web:access_token:v1"
_ASSET_DIR = Path(__file__).resolve().parents[1] / "web" / "consensus"
_runner: web.AppRunner | None = None
_start_lock = asyncio.Lock()
_runtime_token: str | None = None
_failed_auth: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=40))


def _access_token() -> str:
    global _runtime_token
    if _runtime_token:
        return _runtime_token
    configured = os.getenv("CONSENSUS_WEB_TOKEN", "").strip()
    if configured:
        if len(configured) < 16:
            raise RuntimeError("CONSENSUS_WEB_TOKEN должен содержать не меньше 16 символов")
        _runtime_token = configured
        return configured
    stored = str(meta_storage.get_meta(_TOKEN_META_KEY) or "").strip()
    if len(stored) >= 16:
        _runtime_token = stored
        return stored
    generated = secrets.token_urlsafe(24)
    meta_storage.set_meta_value(_TOKEN_META_KEY, generated)
    _runtime_token = generated
    print(
        "Consensus web access key generated. "
        f"Open the private Discord settings panel to view it: {generated}",
        flush=True,
    )
    return generated


def consensus_web_url() -> str:
    return f"http://{CONSENSUS_WEB_PUBLIC_NAME}:{CONSENSUS_WEB_PORT}"


def _result_payload(result: Any, guild_id: int) -> dict[str, Any]:
    if isinstance(result, dict):
        get = result.get
    else:
        def get(key: str, default: Any = None) -> Any:
            return getattr(result, key, default)
    source_channel_id = int(get("source_channel_id") or 0)
    source_message_id = int(get("source_message_id") or 0)
    block_votes = get("block_votes") or {}
    if isinstance(block_votes, str):
        try:
            block_votes = json.loads(block_votes)
        except (TypeError, ValueError, json.JSONDecodeError):
            block_votes = {}
    return {
        "bill_number": int(get("bill_number") or 0),
        "title": str(get("title") or ""),
        "status": str(get("status") or ""),
        "internal_percent": float(get("internal_percent") or 0.0),
        "overall_percent": float(get("overall_percent") or 0.0),
        "opposed_percent": float(get("opposed_percent") or 0.0),
        "block_votes": {
            str(key): str(value)
            for key, value in dict(block_votes).items()
        },
        "source_url": (
            f"https://discord.com/channels/{guild_id}/"
            f"{source_channel_id}/{source_message_id}"
            if source_channel_id and source_message_id
            else None
        ),
    }


async def build_consensus_web_state(
    bot: discord.Client,
    guild_id: int,
) -> dict[str, Any]:
    guild = bot.get_guild(int(guild_id))
    queue_rows, recent_rows = await asyncio.gather(
        asyncio.to_thread(tvrs_storage.tvrs_queue_bills, int(guild_id), 20),
        asyncio.to_thread(tvrs_storage.tvrs_recent_live_results, int(guild_id), 12),
    )
    session = active_sessions.get(int(guild_id))
    state: dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "guild": {
            "id": int(guild_id),
            "name": str(getattr(guild, "name", "") or "Товарищество"),
        },
        "active": session is not None and not session.finished,
        "session": None,
        "queue": [
            {
                "bill_number": int(row.get("bill_number") or 0),
                "title": str(row.get("title") or ""),
                "status": str(row.get("status") or ""),
                "source_url": (
                    f"https://discord.com/channels/{guild_id}/"
                    f"{int(row.get('channel_id') or 0)}/"
                    f"{int(row.get('message_id') or 0)}"
                    if row.get("channel_id") and row.get("message_id")
                    else None
                ),
            }
            for row in queue_rows
        ],
        "recent_results": [
            _result_payload(row, int(guild_id))
            for row in recent_rows
        ],
    }
    if session is None:
        return state

    participants = sorted(
        session.participants.values(),
        key=lambda item: (
            0 if item.voting_block else 1,
            str(item.display_name).casefold(),
        ),
    )
    confirmed = session.confirmed_participants()
    current_bill = dict(session.current_bill or {})
    reveal_blocks = (
        session.stage in {"after_result", "finished"}
        and bool(session.results)
    )
    latest_result = session.results[-1] if reveal_blocks else None
    voted_ids = set(
        getattr(latest_result, "votes", {})
        if latest_result is not None
        else session.votes
    )
    block_votes = (
        dict(getattr(latest_result, "block_votes", {}) or {})
        if latest_result is not None
        else {}
    )
    blocks = {
        key: (
            str(block_votes.get(key) or "inactive")
            if reveal_blocks
            else ("hidden" if session.stage in {"voting", "finalizing"} else "pending")
        )
        for key in ("first", "second", "third", "consensus")
    }
    state["active"] = not session.finished
    state["session"] = {
        "key": session.session_key,
        "revision": int(session.revision),
        "plenary_number": int(session.plenary_number),
        "stage": str(session.stage),
        "stage_label": {
            "registration": "Регистрация",
            "voting": "Голосование",
            "finalizing": "Фиксация результата",
            "discussion_type": "Выбор дискуссии",
            "discussion": "Дискуссия",
            "paused": "Пауза",
            "after_result": "Результат проекта",
            "finished": "Завершён",
            "cancelled": "Отменён",
        }.get(str(session.stage), str(session.stage)),
        "leader": {
            "id": int(session.leader_id),
            "name": str(session.leader_display),
        },
        "current_bill": (
            {
                "bill_number": int(current_bill.get("bill_number") or 0),
                "title": str(current_bill.get("title") or ""),
                "source_url": (
                    f"https://discord.com/channels/{guild_id}/"
                    f"{int(current_bill.get('channel_id') or 0)}/"
                    f"{int(current_bill.get('message_id') or 0)}"
                    if current_bill.get("channel_id")
                    and current_bill.get("message_id")
                    else None
                ),
            }
            if current_bill
            else (
                {
                    "bill_number": int(latest_result.bill_number),
                    "title": str(latest_result.title),
                    "source_url": (
                        f"https://discord.com/channels/{guild_id}/"
                        f"{int(latest_result.source_channel_id or 0)}/"
                        f"{int(latest_result.source_message_id or 0)}"
                        if latest_result.source_channel_id
                        and latest_result.source_message_id
                        else None
                    ),
                }
                if latest_result is not None
                else None
            )
        ),
        "timer_deadline": (
            session.timer_deadline.isoformat()
            if session.timer_deadline is not None
            else None
        ),
        "quorum": {
            "confirmed": len(confirmed),
            "invited": len(participants),
            "ready": bool(session.quorum_ready()),
            "percent": round(
                len(confirmed) / len(participants) * 100.0,
                1,
            )
            if participants
            else 0.0,
        },
        "voting": {
            "received": sum(
                participant.user_id in voted_ids
                for participant in confirmed
            ),
            "expected": len(confirmed),
            "directions_hidden": session.stage in {"voting", "finalizing"},
        },
        "blocks": blocks,
        "participants": [
            {
                "id": int(participant.user_id),
                "name": str(participant.display_name),
                "kind": (
                    {
                        "first": "Первый сопредседатель",
                        "second": "Второй сопредседатель",
                        "third": "Третий сопредседатель",
                    }.get(participant.voting_block, "Участник")
                ),
                "confirmed": bool(participant.confirmed),
                "voted": participant.user_id in voted_ids,
                "dm_ready": bool(
                    participant.dm_message_id
                    or participant.vote_message_id
                )
                and not participant.dm_failed,
            }
            for participant in participants
        ],
        "results": [
            _result_payload(result, int(guild_id))
            for result in session.results
        ],
    }
    return state


def _request_token(request: web.Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


@web.middleware
async def _security_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    if request.path.startswith("/api/") and request.path != "/api/health":
        remote = str(request.remote or "unknown")
        loop = asyncio.get_running_loop()
        now = loop.time()
        failures = _failed_auth[remote]
        while failures and now - failures[0] > 300:
            failures.popleft()
        if len(failures) >= 30:
            return web.json_response(
                {"error": "too_many_attempts"},
                status=429,
            )
        supplied = _request_token(request)
        if not supplied or not hmac.compare_digest(supplied, _access_token()):
            failures.append(now)
            return web.json_response({"error": "unauthorized"}, status=401)
        failures.clear()
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    )
    return response


def create_consensus_web_app(
    bot: discord.Client,
    *,
    guild_id: int,
) -> web.Application:
    app = web.Application(middlewares=[_security_middleware], client_max_size=64 * 1024)

    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(_ASSET_DIR / "index.html")

    async def asset(request: web.Request) -> web.FileResponse:
        name = str(request.match_info["name"])
        if name not in {"app.js", "style.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(_ASSET_DIR / name)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def state(_: web.Request) -> web.Response:
        return web.json_response(
            await build_consensus_web_state(bot, int(guild_id)),
            dumps=lambda value: json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    app.router.add_get("/", index)
    app.router.add_get("/assets/{name}", asset)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/state", state)
    return app


async def ensure_consensus_web_server(bot: discord.Client) -> web.AppRunner | None:
    global _runner
    if not CONSENSUS_WEB_ENABLED:
        return None
    async with _start_lock:
        if _runner is not None:
            return _runner
        guild_id = CONSENSUS_WEB_GUILD_ID
        if guild_id <= 0 and getattr(bot, "guilds", None):
            guild_id = int(bot.guilds[0].id)
        if guild_id <= 0:
            raise RuntimeError("CONSENSUS_WEB_GUILD_ID не настроен")
        _access_token()
        runner = web.AppRunner(
            create_consensus_web_app(bot, guild_id=guild_id),
            access_log=None,
        )
        await runner.setup()
        try:
            await web.TCPSite(
                runner,
                host=CONSENSUS_WEB_HOST,
                port=CONSENSUS_WEB_PORT,
            ).start()
        except Exception:
            await runner.cleanup()
            raise
        _runner = runner
        print(
            f"Consensus web panel: {consensus_web_url()} "
            f"(listening on {CONSENSUS_WEB_HOST}:{CONSENSUS_WEB_PORT})",
            flush=True,
        )
        return runner


async def open_consensus_web_info(interaction: discord.Interaction) -> None:
    permissions = getattr(interaction.user, "guild_permissions", None)
    if (
        interaction.guild is None
        or permissions is None
        or not permissions.administrator
    ):
        await interaction.response.send_message(
            "Веб-панель доступна только администратору.",
            ephemeral=True,
        )
        return
    token = _access_token()
    embed = discord.Embed(
        title="🖥️ Локальная панель консенсуса",
        description=(
            f"Адрес: **{consensus_web_url()}**\n"
            f"Ключ доступа: ||`{token}`||\n\n"
            "Панель работает только пока запущен контейнер T-Mod. "
            "Не публикуйте порт в интернете и не передавайте ключ участникам."
        ),
        color=0xD9D9D9,
    )
    embed.add_field(
        name="Если имя не открывается",
        value=(
            "На другом компьютере добавьте в файл hosts строку "
            f"`IP_СЕРВЕРА t.consensus`, затем откройте адрес с портом "
            f"`{CONSENSUS_WEB_PORT}`."
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


def setup_consensus_web(bot: commands.Bot) -> None:
    async def consensus_web_ready() -> None:
        try:
            await ensure_consensus_web_server(bot)
        except Exception as exc:
            print(
                f"Consensus web panel failed: {type(exc).__name__}: {exc}",
                flush=True,
            )

    bot.add_listener(consensus_web_ready, "on_ready")


__all__ = [
    "CONSENSUS_WEB_ENABLED",
    "CONSENSUS_WEB_GUILD_ID",
    "CONSENSUS_WEB_HOST",
    "CONSENSUS_WEB_PORT",
    "CONSENSUS_WEB_PUBLIC_NAME",
    "build_consensus_web_state",
    "consensus_web_url",
    "create_consensus_web_app",
    "ensure_consensus_web_server",
    "open_consensus_web_info",
    "setup_consensus_web",
]
