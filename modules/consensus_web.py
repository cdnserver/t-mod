"""Authenticated observer dashboard and leader console for live consensus."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
import secrets
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import discord
from aiohttp import web
from discord.ext import commands

from modules.consensus_admin_web import register_admin_web_routes
from modules.consensus_core import ConsensusStateError
from modules.consensus_runtime import active_sessions
from modules.consensus_simulator import get_consensus_simulation
from modules.consensus_web_auth import (
    ConsensusWebAuthError,
    ConsensusWebPrincipal,
    clear_session_cookie,
    consensus_web_entry_url as _authenticated_entry_url,
    consume_entry_ticket,
    create_session_token,
    csrf_matches,
    resolve_principal,
    set_session_cookie,
)
from modules.consensus_web_control import (
    ConsensusWebCommandError,
    consensus_web_capabilities,
    execute_consensus_web_command,
)
from modules.tvrs_presentation import is_chair
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


def _configured_public_url() -> str:
    value = os.getenv("CONSENSUS_WEB_PUBLIC_URL", "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        print(
            "CONSENSUS_WEB_PUBLIC_URL ignored: use a plain HTTPS URL "
            "without credentials, query or fragment.",
            flush=True,
        )
        return ""
    return value


CONSENSUS_WEB_PUBLIC_URL = _configured_public_url()


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
_command_rate: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=80))
_command_receipts: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}


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
        "Consensus web access key generated and stored. "
        "Open the private Discord settings panel to view it.",
        flush=True,
    )
    return generated


def consensus_web_url() -> str:
    if CONSENSUS_WEB_PUBLIC_URL:
        return CONSENSUS_WEB_PUBLIC_URL
    return f"http://{CONSENSUS_WEB_PUBLIC_NAME}:{CONSENSUS_WEB_PORT}"


def consensus_web_entry_url(
    *,
    guild_id: int,
    user_id: int,
    mode: str = "live",
    destination: str = "/",
) -> str:
    return _authenticated_entry_url(
        consensus_web_url(),
        guild_id=guild_id,
        user_id=user_id,
        mode=mode,
        destination=destination,
    )


def _discord_message_url(
    guild_id: int,
    channel_id: Any,
    message_id: Any,
) -> str | None:
    selected_channel_id = int(channel_id or 0)
    selected_message_id = int(message_id or 0)
    if selected_channel_id <= 0 or selected_message_id <= 0:
        return None
    return (
        f"https://discord.com/channels/{int(guild_id)}/"
        f"{selected_channel_id}/{selected_message_id}"
    )


def _bill_payload(
    bill: dict[str, Any],
    guild_id: int,
    *,
    fallback_result: Any | None = None,
) -> dict[str, Any] | None:
    if not bill and fallback_result is None:
        return None

    def result_value(key: str, default: Any = None) -> Any:
        if fallback_result is None:
            return default
        if isinstance(fallback_result, dict):
            return fallback_result.get(key, default)
        return getattr(fallback_result, key, default)

    author_id = int(
        bill.get("author_id")
        or bill.get("bill_author_id")
        or 0
    )
    author_display = str(
        bill.get("author_display")
        or bill.get("bill_author_display")
        or ""
    ).strip()
    return {
        "id": int(bill.get("id") or result_value("bill_id") or 0),
        "bill_number": int(
            bill.get("bill_number")
            or result_value("bill_number")
            or 0
        ),
        "title": str(
            bill.get("title")
            or bill.get("bill_title")
            or result_value("title")
            or ""
        ),
        "summary": str(
            bill.get("summary")
            or bill.get("bill_summary")
            or ""
        ).strip(),
        "materials": str(
            bill.get("materials")
            or bill.get("bill_materials")
            or ""
        ).strip(),
        "author": {
            "id": author_id or None,
            "name": author_display or (
                f"Участник {author_id}" if author_id else "Автор не указан"
            ),
        },
        "decision_category": str(
            bill.get("decision_category")
            or bill.get("bill_decision_category")
            or result_value("decision_category")
            or "ordinary"
        ),
        "created_at": (
            str(
                bill.get("created_at")
                or bill.get("bill_created_at")
                or ""
            )
            or None
        ),
        "source_url": (
            _discord_message_url(
                guild_id,
                bill.get("channel_id"),
                bill.get("message_id"),
            )
            or _discord_message_url(
                guild_id,
                result_value("source_channel_id"),
                result_value("source_message_id"),
            )
            or bill.get("source_url")
        ),
    }


def _result_payload(result: Any, guild_id: int) -> dict[str, Any]:
    if isinstance(result, dict):
        get = result.get
    else:
        def get(key: str, default: Any = None) -> Any:
            return getattr(result, key, default)
    source_channel_id = int(get("source_channel_id") or 0)
    source_message_id = int(get("source_message_id") or 0)
    block_votes = get("block_votes") or get("block_votes_json") or {}
    if isinstance(block_votes, str):
        try:
            block_votes = json.loads(block_votes)
        except (TypeError, ValueError, json.JSONDecodeError):
            block_votes = {}
    return {
        "bill_id": int(get("bill_id") or 0),
        "bill_number": int(get("bill_number") or 0),
        "title": str(get("title") or get("bill_title") or ""),
        "status": str(get("status") or ""),
        "internal_percent": float(get("internal_percent") or 0.0),
        "overall_percent": float(get("overall_percent") or 0.0),
        "opposed_percent": float(get("opposed_percent") or 0.0),
        "required_percent": float(get("required_percent") or 0.0),
        "decision_category": str(get("decision_category") or "ordinary"),
        "resolution_method": str(get("resolution_method") or "vote"),
        "resolution_note": str(get("resolution_note") or "").strip() or None,
        "block_votes": {
            str(key): str(value)
            for key, value in dict(block_votes).items()
        },
        "source_url": _discord_message_url(
            guild_id,
            source_channel_id,
            source_message_id,
        ),
        "bill": _bill_payload(
            {
                "id": get("bill_id"),
                "bill_number": get("bill_number"),
                "bill_title": get("bill_title") or get("title"),
                "bill_author_id": get("bill_author_id"),
                "bill_author_display": get("bill_author_display"),
                "bill_summary": get("bill_summary"),
                "bill_materials": get("bill_materials"),
                "bill_decision_category": get("bill_decision_category"),
                "bill_created_at": get("bill_created_at"),
                "source_url": _discord_message_url(
                    guild_id,
                    source_channel_id,
                    source_message_id,
                ),
            },
            guild_id,
            fallback_result=result,
        ),
    }


def _catalog_bill_payload(
    row: dict[str, Any],
    guild_id: int,
) -> dict[str, Any]:
    author_id = int(row.get("author_id") or 0)
    result_status = str(row.get("result_status") or "").strip()
    return {
        "id": int(row.get("id") or 0),
        "bill_number": int(row.get("bill_number") or 0),
        "title": str(row.get("title") or ""),
        "author": {
            "id": author_id or None,
            "name": str(row.get("author_display") or "").strip()
            or (f"Участник {author_id}" if author_id else "Автор не указан"),
        },
        "created_at": str(row.get("created_at") or "") or None,
        "decision_category": str(
            row.get("decision_category") or "ordinary"
        ),
        "status": str(row.get("status") or ""),
        "source_url": _discord_message_url(
            guild_id,
            row.get("channel_id"),
            row.get("message_id"),
        ),
        "result": (
            {
                "status": result_status,
                "internal_percent": float(
                    row.get("result_internal_percent") or 0.0
                ),
                "overall_percent": float(
                    row.get("result_overall_percent") or 0.0
                ),
                "opposed_percent": float(
                    row.get("result_opposed_percent") or 0.0
                ),
                "required_percent": float(
                    row.get("result_required_percent") or 0.0
                ),
                "resolution_method": str(
                    row.get("result_resolution_method") or "vote"
                ),
                "created_at": (
                    str(row.get("result_created_at") or "") or None
                ),
            }
            if result_status
            else None
        ),
    }


def _simulation_bill_payload(
    simulation: Any,
    bill_number: int,
) -> dict[str, Any]:
    clean_number = max(1, int(bill_number))
    return {
        "id": 900_000 + clean_number,
        "bill_number": clean_number,
        "title": f"Учебный законопроект №{clean_number}",
        "summary": (
            "Тестовый проект для проверки регистрации, голосования, "
            "дискуссии, паузы и фиксации результата."
        ),
        "materials": "Учебные материалы отсутствуют.",
        "author_id": int(simulation.leader_id),
        "author_display": "Симулятор T-Mod",
        "decision_category": "ordinary",
        "created_at": None,
        "status": "queued",
    }


def _session_payload(
    session: Any,
    guild_id: int,
    *,
    simulation: bool,
    current_bill_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    participants = sorted(
        session.participants.values(),
        key=lambda item: (
            0 if item.voting_block else 1,
            str(item.display_name).casefold(),
        ),
    )
    confirmed = session.confirmed_participants()
    current_bill = dict(current_bill_details or session.current_bill or {})
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
    return {
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
        "current_bill": _bill_payload(
            current_bill,
            guild_id,
            fallback_result=latest_result,
        ),
        "current_result": (
            _result_payload(latest_result, guild_id)
            if latest_result is not None
            else None
        ),
        "timer_deadline": (
            session.timer_deadline.isoformat()
            if session.timer_deadline is not None
            else None
        ),
        "pause_reason": str(getattr(session, "paused_reason", "") or ""),
        "discussion": {
            "type": str(getattr(session, "discussion_type", "") or ""),
            "channel_url": (
                f"https://discord.com/channels/{guild_id}/"
                f"{int(session.discussion_channel_id)}"
                if getattr(session, "discussion_channel_id", None)
                else None
            ),
        },
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
        "rules": {
            "acceptance_percent": float(
                getattr(session.rules, "acceptance_percent", 50.0)
                or 50.0
            ),
            "quorum_percent": float(
                getattr(session.rules, "quorum_percent", 50.0)
                or 50.0
            ),
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
                "dm_ready": (
                    True
                    if simulation
                    else bool(
                        participant.dm_message_id
                        or participant.vote_message_id
                    )
                    and not participant.dm_failed
                ),
            }
            for participant in participants
        ],
        "results": [
            _result_payload(result, int(guild_id))
            for result in session.results
        ],
    }


async def build_consensus_web_state(
    bot: discord.Client,
    guild_id: int,
    *,
    mode: str | None = None,
    principal: ConsensusWebPrincipal | None = None,
    legacy_read_only: bool = False,
) -> dict[str, Any]:
    guild_id = int(guild_id)
    guild = bot.get_guild(guild_id)
    live_session = active_sessions.get(guild_id)
    simulation = get_consensus_simulation(guild_id)
    requested_mode = str(mode or "").strip().lower()
    if requested_mode not in {"live", "simulation"}:
        requested_mode = "live" if live_session is not None else (
            "simulation" if simulation is not None else "live"
        )
    elif requested_mode == "simulation" and simulation is None:
        requested_mode = "live"
    selected_simulation = requested_mode == "simulation"
    session = simulation.session if selected_simulation and simulation else (
        live_session if not selected_simulation else None
    )

    if selected_simulation:
        queue_rows = simulation.queue_bills(3) if simulation else []
        recent_rows = list(session.results[-12:]) if session is not None else []
    else:
        queue_rows, recent_rows = await asyncio.gather(
            asyncio.to_thread(tvrs_storage.tvrs_queue_bills, guild_id, 20),
            asyncio.to_thread(tvrs_storage.tvrs_recent_live_results, guild_id, 12),
        )

    available_modes = ["live"]
    if simulation is not None:
        available_modes.append("simulation")
    state: dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "guild": {
            "id": guild_id,
            "name": str(getattr(guild, "name", "") or "Товарищество"),
        },
        "mode": requested_mode,
        "mode_label": "Симуляция" if selected_simulation else "Рабочий контур",
        "available_modes": available_modes,
        "active": session is not None and not session.finished,
        "session": None,
        "queue": [
            {
                "id": int(row.get("id") or 0),
                "bill_number": int(row.get("bill_number") or 0),
                "title": str(row.get("title") or ""),
                "status": str(row.get("status") or ""),
                "author": {
                    "id": int(row.get("author_id") or 0) or None,
                    "name": str(row.get("author_display") or "").strip()
                    or "Автор не указан",
                },
                "created_at": str(row.get("created_at") or "") or None,
                "decision_category": str(
                    row.get("decision_category") or "ordinary"
                ),
                "source_url": (
                    f"https://discord.com/channels/{guild_id}/"
                    f"{int(row.get('channel_id') or 0)}/"
                    f"{int(row.get('message_id') or 0)}"
                    if row.get("channel_id") and row.get("message_id")
                    else row.get("source_url")
                ),
            }
            for row in queue_rows
        ],
        "recent_results": [
            _result_payload(row, guild_id)
            for row in recent_rows
        ],
        "viewer": {
            "authenticated": principal is not None,
            "legacy_read_only": bool(legacy_read_only),
            "id": int(principal.user_id) if principal else None,
            "name": (
                str(principal.display_name)
                if principal
                else ("Совместимый просмотр" if legacy_read_only else "")
            ),
            "administrator": bool(principal and principal.administrator),
            "chair": bool(principal and is_chair(principal.member)),
            "leader": bool(
                principal
                and session is not None
                and int(session.leader_id) == int(principal.user_id)
            ),
            "csrf_token": principal.csrf_token if principal else None,
        },
        "capabilities": consensus_web_capabilities(
            mode=requested_mode,
            session=session,
            principal=principal,
        ),
    }
    if session is None:
        return state

    current_bill_details = dict(session.current_bill or {})
    if not current_bill_details and session.results:
        latest_bill_id = int(session.results[-1].bill_id or 0)
        if selected_simulation:
            current_bill_details = {
                "id": latest_bill_id,
                "bill_number": int(session.results[-1].bill_number),
                "title": str(session.results[-1].title),
                "summary": (
                    "Тестовый проект для проверки регистрации, голосования, "
                    "дискуссии, паузы и фиксации результата."
                ),
                "materials": "Учебные материалы отсутствуют.",
                "author_id": int(session.leader_id),
                "author_display": "Симулятор T-Mod",
                "decision_category": "ordinary",
            }
        elif latest_bill_id > 0:
            current_bill_details = dict(
                await asyncio.to_thread(
                    tvrs_storage.tvrs_get_bill_dict_by_id,
                    latest_bill_id,
                )
                or {}
            )

    state["active"] = not session.finished
    session_payload = _session_payload(
        session,
        guild_id,
        simulation=selected_simulation,
        current_bill_details=current_bill_details,
    )
    if not selected_simulation:
        recent_bill_rows = {
            int(row.get("bill_id") or 0): row
            for row in recent_rows
            if isinstance(row, dict) and int(row.get("bill_id") or 0) > 0
        }
        for result in session_payload["results"]:
            bill_row = recent_bill_rows.get(int(result.get("bill_id") or 0))
            if bill_row is not None:
                result["bill"] = _bill_payload(
                    bill_row,
                    guild_id,
                    fallback_result=bill_row,
                )
        current_result = session_payload.get("current_result")
        if isinstance(current_result, dict):
            bill_row = recent_bill_rows.get(
                int(current_result.get("bill_id") or 0)
            )
            if bill_row is not None:
                current_result["bill"] = _bill_payload(
                    bill_row,
                    guild_id,
                    fallback_result=bill_row,
                )
    state["session"] = session_payload
    return state


def _request_token(request: web.Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _request_remote(request: web.Request) -> str:
    remote = str(request.remote or "").strip()
    try:
        proxy_address = ipaddress.ip_address(remote)
    except ValueError:
        proxy_address = None
    if proxy_address is not None and (
        proxy_address.is_private or proxy_address.is_loopback
    ):
        forwarded = request.headers.get("X-Forwarded-For", "")
        candidate = forwarded.rsplit(",", 1)[-1].strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return remote[:64] or "unknown"


@web.middleware
async def _security_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        _apply_security_headers(exc)
        raise
    _apply_security_headers(response)
    return response


def _apply_security_headers(response: web.StreamResponse) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' "
        "'sha256-0IYaU6NkDTflYaDbUR4nMFteY9tDTb1ADhuFP1o95po=' "
        "'sha256-kivcxaEPD+v/Ecc3Z+TNAW/Uf1rs+0/EwVf6c/m1dKc='; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; object-src 'none'; form-action 'self'"
    )


def create_consensus_web_app(
    bot: discord.Client,
    *,
    guild_id: int,
) -> web.Application:
    app = web.Application(middlewares=[_security_middleware], client_max_size=64 * 1024)

    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(_ASSET_DIR / "index.html")

    async def egg(_: web.Request) -> web.FileResponse:
        return web.FileResponse(_ASSET_DIR / "egg.html")

    async def asset(request: web.Request) -> web.FileResponse:
        name = str(request.match_info["name"])
        if name not in {
            "app.js",
            "style.css",
            "egg.css",
            "egg.js",
            "zigmund-murchalki.mp3",
            "admin.css",
            "admin.js",
        }:
            raise web.HTTPNotFound()
        return web.FileResponse(_ASSET_DIR / name)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def authenticated_request(
        request: web.Request,
    ) -> tuple[ConsensusWebPrincipal | None, bool]:
        principal = await resolve_principal(
            request,
            bot,
            guild_id=int(guild_id),
        )
        if principal is not None:
            _failed_auth[_request_remote(request)].clear()
            return principal, False
        supplied = _request_token(request)
        if supplied and hmac.compare_digest(supplied, _access_token()):
            return None, True
        remote = _request_remote(request)
        now = asyncio.get_running_loop().time()
        failures = _failed_auth[remote]
        while failures and now - failures[0] > 300:
            failures.popleft()
        if len(failures) >= 30:
            raise web.HTTPTooManyRequests(
                text=json.dumps({"error": "too_many_attempts"}),
                content_type="application/json",
            )
        failures.append(now)
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "unauthorized"}),
            content_type="application/json",
        )

    async def ticket_login(request: web.Request) -> web.Response:
        try:
            _, user_id = consume_entry_ticket(
                request.query.get("ticket", ""),
                expected_guild_id=int(guild_id),
            )
        except ConsensusWebAuthError:
            raise web.HTTPUnauthorized(
                text="Ссылка недействительна или уже использована. Откройте новую из Discord."
            )
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            raise web.HTTPServiceUnavailable(text="Сервер Discord пока недоступен.")
        member = guild.get_member(int(user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except discord.DiscordException as exc:
                raise web.HTTPForbidden(
                    text="Участник больше не состоит на сервере."
                ) from exc
        token, _ = create_session_token(
            guild_id=int(guild_id),
            user_id=int(member.id),
        )
        mode = (
            "simulation"
            if request.query.get("mode") == "simulation"
            else "live"
        )
        destination = (
            "/admin"
            if request.query.get("next") == "/admin"
            else f"/?mode={mode}"
        )
        response = web.HTTPFound(location=destination)
        set_session_cookie(
            response,
            token,
            secure=bool(CONSENSUS_WEB_PUBLIC_URL),
        )
        return response

    async def logout(_: web.Request) -> web.Response:
        response = web.HTTPFound(location="/")
        clear_session_cookie(
            response,
            secure=bool(CONSENSUS_WEB_PUBLIC_URL),
        )
        return response

    async def state(request: web.Request) -> web.Response:
        principal, legacy_read_only = await authenticated_request(request)
        return web.json_response(
            await build_consensus_web_state(
                bot,
                int(guild_id),
                mode=request.query.get("mode"),
                principal=principal,
                legacy_read_only=legacy_read_only,
            ),
            dumps=lambda value: json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    async def bills(request: web.Request) -> web.Response:
        await authenticated_request(request)
        requested_mode = (
            "simulation"
            if request.query.get("mode") == "simulation"
            else "live"
        )
        if requested_mode == "simulation":
            simulation = get_consensus_simulation(int(guild_id))
            if simulation is None:
                return web.json_response(
                    {"mode": "simulation", "items": []},
                )
            numbers = {
                int(simulation.bill_number),
                *range(
                    int(simulation.bill_number) + 1,
                    int(simulation.bill_number) + 4,
                ),
            }
            numbers.update(
                int(result.bill_number)
                for result in simulation.session.results
            )
            results = {
                int(result.bill_number): _result_payload(
                    result,
                    int(guild_id),
                )
                for result in simulation.session.results
            }
            items = []
            for number in sorted(numbers, reverse=True):
                bill = _simulation_bill_payload(simulation, number)
                catalog_item = _catalog_bill_payload(
                    bill,
                    int(guild_id),
                )
                catalog_item["result"] = results.get(number)
                items.append(catalog_item)
            return web.json_response(
                {"mode": "simulation", "items": items},
            )

        rows = await asyncio.to_thread(
            tvrs_storage.tvrs_public_bill_catalog,
            int(guild_id),
            200,
        )
        return web.json_response(
            {
                "mode": "live",
                "items": [
                    _catalog_bill_payload(row, int(guild_id))
                    for row in rows
                ],
            },
        )

    async def bill_detail(request: web.Request) -> web.Response:
        await authenticated_request(request)
        try:
            bill_id = int(request.match_info["bill_id"])
        except (TypeError, ValueError):
            raise web.HTTPNotFound()
        if bill_id <= 0:
            raise web.HTTPNotFound()

        requested_mode = (
            "simulation"
            if request.query.get("mode") == "simulation"
            else "live"
        )
        if requested_mode == "simulation":
            simulation = get_consensus_simulation(int(guild_id))
            bill_number = bill_id - 900_000
            if (
                simulation is None
                or bill_number <= 0
                or bill_number > int(simulation.bill_number) + 3
            ):
                raise web.HTTPNotFound()
            raw_bill = _simulation_bill_payload(
                simulation,
                bill_number,
            )
            result = next(
                (
                    item
                    for item in simulation.session.results
                    if int(item.bill_number) == bill_number
                ),
                None,
            )
            return web.json_response(
                {
                    "bill": _bill_payload(raw_bill, int(guild_id)),
                    "result": (
                        _result_payload(result, int(guild_id))
                        if result is not None
                        else None
                    ),
                },
            )

        raw_bill = await asyncio.to_thread(
            tvrs_storage.tvrs_get_bill_dict_by_id,
            bill_id,
        )
        if (
            raw_bill is None
            or int(raw_bill.get("guild_id") or 0) != int(guild_id)
        ):
            raise web.HTTPNotFound()
        result = await asyncio.to_thread(
            tvrs_storage.tvrs_latest_live_result_for_bill,
            int(guild_id),
            bill_id,
        )
        return web.json_response(
            {
                "bill": _bill_payload(raw_bill, int(guild_id)),
                "result": (
                    _result_payload(result, int(guild_id))
                    if result is not None
                    else None
                ),
            },
        )

    async def command(request: web.Request) -> web.Response:
        principal, legacy_read_only = await authenticated_request(request)
        if legacy_read_only or principal is None:
            return web.json_response(
                {
                    "error": "personal_login_required",
                    "message": "Для управления откройте персональную ссылку из Discord.",
                },
                status=403,
            )
        if not csrf_matches(request, principal):
            return web.json_response(
                {"error": "csrf_failed", "message": "Обновите панель и повторите действие."},
                status=403,
            )
        rate_now = asyncio.get_running_loop().time()
        recent_commands = _command_rate[int(principal.user_id)]
        while recent_commands and rate_now - recent_commands[0] > 60:
            recent_commands.popleft()
        if len(recent_commands) >= 60:
            return web.json_response(
                {
                    "error": "command_rate_limited",
                    "message": "Слишком много команд. Подождите несколько секунд.",
                },
                status=429,
            )
        recent_commands.append(rate_now)
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response(
                {"error": "invalid_json", "message": "Некорректная команда."},
                status=400,
            )
        if not isinstance(body, dict):
            return web.json_response(
                {"error": "invalid_payload", "message": "Некорректная команда."},
                status=400,
            )
        idempotency_key = request.headers.get("X-Idempotency-Key", "").strip()
        if len(idempotency_key) < 12 or len(idempotency_key) > 120:
            return web.json_response(
                {
                    "error": "idempotency_key_required",
                    "message": "Команда не имеет ключа защиты от повтора.",
                },
                status=400,
            )
        now = time.monotonic()
        for key, (expires_at, _) in list(_command_receipts.items()):
            if expires_at <= now:
                _command_receipts.pop(key, None)
        receipt_key = (int(principal.user_id), idempotency_key)
        cached = _command_receipts.get(receipt_key)
        if cached is not None:
            return web.json_response(cached[1])
        try:
            revision = int(body.get("revision") or 0)
            bill_id = body.get("bill_id")
            bill_id = int(bill_id) if bill_id is not None else None
            payload = body.get("payload") or {}
            if not isinstance(payload, dict):
                raise ValueError("payload")
            command_guild = bot.get_guild(int(guild_id))
            if command_guild is None:
                return web.json_response(
                    {
                        "error": "guild_unavailable",
                        "message": "Связь бота с Discord временно недоступна.",
                    },
                    status=503,
                )
            message = await execute_consensus_web_command(
                bot,
                command_guild,
                principal,
                mode=str(body.get("mode") or "live"),
                action=str(body.get("action") or ""),
                session_key=str(body.get("session_key") or ""),
                revision=revision,
                bill_id=bill_id,
                payload=payload,
            )
        except ConsensusWebCommandError as exc:
            return web.json_response(
                {
                    "error": exc.code,
                    "message": str(exc),
                    "details": exc.details,
                },
                status=exc.status,
            )
        except ConsensusStateError as exc:
            return web.json_response(
                {
                    "error": "state_conflict",
                    "message": str(exc),
                },
                status=409,
            )
        except (ValueError, TypeError):
            return web.json_response(
                {"error": "invalid_payload", "message": "Некорректные параметры команды."},
                status=400,
            )
        except Exception as exc:
            traceback.print_exc()
            return web.json_response(
                {
                    "error": "command_failed",
                    "message": (
                        "Команда не выполнена. Состояние можно безопасно обновить "
                        "и повторить действие."
                    ),
                    "type": type(exc).__name__,
                },
                status=500,
            )
        response_payload = {
            "ok": True,
            "message": message,
            "state": await build_consensus_web_state(
                bot,
                int(guild_id),
                mode=str(body.get("mode") or "live"),
                principal=principal,
            ),
        }
        _command_receipts[receipt_key] = (now + 300.0, response_payload)
        return web.json_response(response_payload)

    app.router.add_get("/", index)
    app.router.add_get("/egg", egg)
    app.router.add_get("/egg/", egg)
    app.router.add_get("/assets/{name}", asset)
    app.router.add_get("/auth/ticket", ticket_login)
    app.router.add_get("/auth/logout", logout)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/bills", bills)
    app.router.add_get("/api/bills/{bill_id}", bill_detail)
    app.router.add_post("/api/command", command)
    register_admin_web_routes(
        app,
        bot,
        guild_id=int(guild_id),
        asset_dir=_ASSET_DIR,
        authenticate=authenticated_request,
    )
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
    public = bool(CONSENSUS_WEB_PUBLIC_URL)
    personal_url = consensus_web_entry_url(
        guild_id=interaction.guild.id,
        user_id=interaction.user.id,
    )
    admin_url = consensus_web_entry_url(
        guild_id=interaction.guild.id,
        user_id=interaction.user.id,
        destination="/admin",
    )
    embed = discord.Embed(
        title=(
            "🖥️ Административная веб-система"
            if public
            else "🖥️ Локальная административная веб-система"
        ),
        description=(
            f"Адрес: **{consensus_web_url()}**\n"
            f"Резервный ключ просмотра: ||`{token}`||\n\n"
            "Админ-центр и панель консенсуса работают только пока запущен "
            "контейнер T-Mod. "
            + (
                "Публичный HTTPS принимает защищённый reverse proxy Caddy."
                if public
                else "Не публикуйте порт в интернете."
            )
            + " Управление доступно только после персонального входа из Discord."
        ),
        color=0xD9D9D9,
    )
    embed.add_field(
        name="Режимы доступа",
        value=(
            "Кнопки ниже открывают персональную сессию и проверяют ваши роли "
            "при каждом действии. Резервный ключ даёт только просмотр и не "
            "позволяет управлять заседанием или читать административные аудиты."
        ),
        inline=False,
    )
    view = discord.ui.View(timeout=600)
    view.add_item(
        discord.ui.Button(
            label="Открыть админ-центр",
            emoji="🛡️",
            style=discord.ButtonStyle.link,
            url=admin_url,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Панель консенсуса",
            emoji="🏛️",
            style=discord.ButtonStyle.link,
            url=personal_url,
        )
    )
    embed.add_field(
        name=(
            "Прямой HTTPS"
            if public
            else "Если имя не открывается"
        ),
        value=(
            "Caddy автоматически выпускает и продлевает сертификат. "
            "На роутере должны быть направлены только TCP-порты 80 и 443; "
            "внутренний порт 8787 публиковать нельзя."
            if public
            else (
                "На другом компьютере добавьте в файл hosts строку "
                f"`IP_СЕРВЕРА t.consensus`, затем откройте адрес с портом "
                f"`{CONSENSUS_WEB_PORT}`."
            )
        ),
        inline=False,
    )
    await interaction.response.send_message(
        embed=embed,
        view=view,
        ephemeral=True,
    )


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
    "CONSENSUS_WEB_PUBLIC_URL",
    "build_consensus_web_state",
    "consensus_web_entry_url",
    "consensus_web_url",
    "create_consensus_web_app",
    "ensure_consensus_web_server",
    "open_consensus_web_info",
    "setup_consensus_web",
]
