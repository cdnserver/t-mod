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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import discord
from aiohttp import web
from discord.ext import commands

from modules.consensus_admin_web import register_admin_web_routes
from modules.consensus_core import (
    ConsensusStateError,
    session_from_snapshot,
)
from modules.consensus_runtime import active_sessions
from modules.consensus_simulator import get_consensus_simulation
from modules.consensus_web_auth import (
    ConsensusWebAuthError,
    ConsensusWebPrincipal,
    PERSISTENT_SESSION_LIFETIME_SECONDS,
    clear_session_cookie,
    consensus_web_entry_url as _authenticated_entry_url,
    consume_entry_ticket,
    create_session_token,
    csrf_matches,
    resolve_principal,
    set_session_cookie,
    signed_session_identity,
)
from modules.consensus_web_control import (
    ConsensusWebCommandError,
    consensus_web_capabilities,
    execute_consensus_web_command,
)
from modules.tvrs_presentation import is_chair
from modules.web_snapshot_cache import AsyncSnapshotCache
from modules.reactor_web import register_reactor_web_routes
from modules.atlas_web import register_atlas_web_routes
from modules.games_web import register_games_web_routes
from modules.sgl_web import register_sgl_web_routes
from persistence import activity_repository as meta_storage
from persistence import tvrs_repository as tvrs_storage
from persistence import web_auth_repository as credential_storage
from persistence import profile_repository as profile_storage
from persistence import reactor_repository as reactor_storage
from persistence import consensus_schedule_repository as schedule_storage
from persistence import global_ban_repository as global_ban_storage
from persistence import legislation_repository as legislation_storage
from modules.consensus_schedule import public_schedule_payload
from modules.consensus_artifacts import generate_session_report


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
    os.getenv("CONSENSUS_WEB_PUBLIC_NAME", "t.consensus").strip() or "t.consensus"
)
_GLOBAL_BAN_REQUEST_KEY = web.RequestKey("global_ban", object)


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
try:
    CONSENSUS_BALLOT_UNLOCK_SECONDS = max(
        0,
        min(3600, int(os.getenv("CONSENSUS_BALLOT_UNLOCK_SECONDS", "90") or 90)),
    )
except (TypeError, ValueError):
    CONSENSUS_BALLOT_UNLOCK_SECONDS = 90


def _configured_surface_url(variable: str, fallback: str) -> str:
    value = os.getenv(variable, fallback).strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return fallback
    return value


REACTOR_WEB_PUBLIC_URL = _configured_surface_url(
    "REACTOR_WEB_PUBLIC_URL",
    "https://reactor.tvr.lat",
)
PORTAL_WEB_PUBLIC_URL = _configured_surface_url(
    "PORTAL_WEB_PUBLIC_URL",
    "https://tvr.lat",
)
ATLAS_WEB_PUBLIC_URL = _configured_surface_url(
    "ATLAS_WEB_PUBLIC_URL",
    "https://atlas.tvr.lat",
)
ZIGMUND_WEB_PUBLIC_URL = _configured_surface_url(
    "ZIGMUND_WEB_PUBLIC_URL",
    "https://zigmund.tvr.lat",
)
SGL_WEB_PUBLIC_URL = _configured_surface_url(
    "SGL_WEB_PUBLIC_URL",
    "https://sgl.tvr.lat",
)
OVR_WEB_PUBLIC_URL = _configured_surface_url(
    "OVR_WEB_PUBLIC_URL",
    "https://ovr.tvr.lat",
)


def _configured_guild_id() -> int:
    raw = (
        os.getenv("CONSENSUS_WEB_GUILD_ID") or os.getenv("DISCORD_GUILD_ID") or "0"
    ).strip()
    return int(raw) if raw.isdigit() else 0


CONSENSUS_WEB_GUILD_ID = _configured_guild_id()
_TOKEN_META_KEY = "consensus_web:access_token:v1"
_ASSET_DIR = Path(__file__).resolve().parents[1] / "web" / "consensus"
_ATLAS_ASSET_DIR = Path(__file__).resolve().parents[1] / "web" / "atlas"
_runner: web.AppRunner | None = None
_start_lock = asyncio.Lock()
_runtime_token: str | None = None
_failed_auth: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=40))
_login_failures: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=30))
_command_rate: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=80))
_command_receipts: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}


def _access_token() -> str:
    global _runtime_token
    if _runtime_token:
        return _runtime_token
    configured = os.getenv("CONSENSUS_WEB_TOKEN", "").strip()
    if configured:
        if len(configured) < 16:
            raise RuntimeError(
                "CONSENSUS_WEB_TOKEN должен содержать не меньше 16 символов"
            )
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
    base_url = consensus_web_url()
    if destination == "/admin" and REACTOR_WEB_PUBLIC_URL:
        base_url = REACTOR_WEB_PUBLIC_URL
    elif destination == "/reactor" and PORTAL_WEB_PUBLIC_URL:
        base_url = PORTAL_WEB_PUBLIC_URL
    elif destination == "/atlas" and ATLAS_WEB_PUBLIC_URL:
        base_url = ATLAS_WEB_PUBLIC_URL
    elif destination == "/sgl" and SGL_WEB_PUBLIC_URL:
        base_url = SGL_WEB_PUBLIC_URL
    return _authenticated_entry_url(
        base_url,
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

    author_id = int(bill.get("author_id") or bill.get("bill_author_id") or 0)
    author_display = str(
        bill.get("author_display") or bill.get("bill_author_display") or ""
    ).strip()
    return {
        "id": int(bill.get("id") or result_value("bill_id") or 0),
        "bill_number": int(bill.get("bill_number") or result_value("bill_number") or 0),
        "title": str(
            bill.get("title") or bill.get("bill_title") or result_value("title") or ""
        ),
        "summary": str(bill.get("summary") or bill.get("bill_summary") or "").strip(),
        "materials": str(
            bill.get("materials") or bill.get("bill_materials") or ""
        ).strip(),
        "author": {
            "id": author_id or None,
            "name": author_display
            or (f"Участник {author_id}" if author_id else "Автор не указан"),
        },
        "decision_category": str(
            bill.get("decision_category")
            or bill.get("bill_decision_category")
            or result_value("decision_category")
            or "ordinary"
        ),
        "created_at": (
            str(bill.get("created_at") or bill.get("bill_created_at") or "") or None
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
            str(key): str(value) for key, value in dict(block_votes).items()
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
        "decision_category": str(row.get("decision_category") or "ordinary"),
        "status": str(row.get("status") or ""),
        "source_url": _discord_message_url(
            guild_id,
            row.get("channel_id"),
            row.get("message_id"),
        ),
        "result": (
            {
                "status": result_status,
                "internal_percent": float(row.get("result_internal_percent") or 0.0),
                "overall_percent": float(row.get("result_overall_percent") or 0.0),
                "opposed_percent": float(row.get("result_opposed_percent") or 0.0),
                "required_percent": float(row.get("result_required_percent") or 0.0),
                "resolution_method": str(row.get("result_resolution_method") or "vote"),
                "created_at": (str(row.get("result_created_at") or "") or None),
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
    reveal_blocks = session.stage in {"after_result", "finished"} and bool(
        session.results
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
        "created_at": session.created_at.isoformat(),
        "stage": str(session.stage),
        "stage_label": {
            "registration": "Регистрация",
            "presentation": "Представление законопроекта",
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
        "timer_seconds": (
            int(session.timer_seconds)
            if getattr(session, "timer_seconds", None) is not None
            else None
        ),
        "timer_added_seconds": max(
            0,
            int(getattr(session, "timer_added_seconds", 0) or 0),
        ),
        "timer_last_added_seconds": (
            int(session.timer_last_added_seconds)
            if getattr(session, "timer_last_added_seconds", None) is not None
            else None
        ),
        "timer_last_adjusted_at": (
            session.timer_last_adjusted_at.isoformat()
            if getattr(session, "timer_last_adjusted_at", None) is not None
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
                participant.user_id in voted_ids for participant in confirmed
            ),
            "expected": len(confirmed),
            "directions_hidden": session.stage in {"voting", "finalizing"},
        },
        "rules": {
            "acceptance_percent": float(
                getattr(session.rules, "acceptance_percent", 50.0) or 50.0
            ),
            "quorum_percent": float(
                getattr(session.rules, "quorum_percent", 50.0) or 50.0
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
                    else bool(participant.dm_message_id or participant.vote_message_id)
                    and not participant.dm_failed
                ),
            }
            for participant in participants
        ],
        "results": [
            _result_payload(result, int(guild_id)) for result in session.results
        ],
    }


def _utc_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ballot_unlock_at(schedule: dict[str, Any] | None) -> datetime | None:
    if schedule is None:
        return None
    scheduled_for = _utc_datetime(schedule.get("scheduled_for"))
    if scheduled_for is None:
        return None
    return scheduled_for - timedelta(seconds=CONSENSUS_BALLOT_UNLOCK_SECONDS)


def _broadcast_phase(
    *,
    session: Any | None,
    schedule: dict[str, Any] | None,
    latest_finished: dict[str, Any] | None,
    now: datetime,
) -> str:
    if session is not None and not session.finished:
        return "preparing" if session.stage == "registration" else "live"
    if schedule is not None and str(schedule.get("status") or "") == "scheduled":
        return "scheduled"
    finished_at = _utc_datetime((latest_finished or {}).get("finished_at"))
    if finished_at is not None and now - finished_at <= timedelta(hours=12):
        return "completed"
    return "idle"


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
    if principal is None and not legacy_read_only:
        requested_mode = "live"
    if requested_mode not in {"live", "simulation"}:
        requested_mode = (
            "live"
            if live_session is not None
            else ("simulation" if simulation is not None else "live")
        )
    elif requested_mode == "simulation" and simulation is None:
        requested_mode = "live"
    selected_simulation = requested_mode == "simulation"
    session = (
        simulation.session
        if selected_simulation and simulation
        else (live_session if not selected_simulation else None)
    )

    latest_finished_snapshot: dict[str, Any] | None = None
    if selected_simulation:
        queue_rows = simulation.queue_bills(3) if simulation else []
        recent_rows = list(session.results[-12:]) if session is not None else []
        schedule_row = None
    else:
        if live_session is not None:
            def schedule_loader() -> dict[str, Any] | None:
                return schedule_storage.get_consensus_schedule_for_session(
                    guild_id,
                    str(live_session.session_key),
                ) or schedule_storage.get_upcoming_consensus_schedule(guild_id)
        else:
            def schedule_loader() -> dict[str, Any] | None:
                return schedule_storage.get_upcoming_consensus_schedule(guild_id)
        queue_rows, recent_rows, schedule_row, latest_finished_snapshot = await asyncio.gather(
            asyncio.to_thread(tvrs_storage.tvrs_queue_bills, guild_id, 20),
            asyncio.to_thread(tvrs_storage.tvrs_recent_live_results, guild_id, 12),
            asyncio.to_thread(schedule_loader),
            asyncio.to_thread(
                tvrs_storage.tvrs_latest_finished_consensus_session,
                guild_id,
            ),
        )

    available_modes = ["live"]
    if simulation is not None and principal is not None:
        available_modes.append("simulation")
    viewer_participant = (
        session.participants.get(int(principal.user_id))
        if principal is not None and session is not None
        else None
    )
    viewer_vote_source = (
        session.results[-1].votes
        if session is not None
        and session.stage in {"after_result", "finished"}
        and session.results
        else (session.votes if session is not None else {})
    )
    viewer_vote = (
        str(viewer_vote_source.get(int(principal.user_id)) or "") or None
        if principal is not None
        and session is not None
        and viewer_participant is not None
        else None
    )
    now = datetime.now(timezone.utc)
    ballot_schedule = schedule_row
    if session is not None and schedule_row is not None:
        started_key = str(schedule_row.get("started_session_key") or "")
        if started_key and started_key != str(session.session_key):
            ballot_schedule = None
        elif not started_key and str(schedule_row.get("status") or "") == "scheduled":
            ballot_schedule = (
                schedule_row
                if int(schedule_row.get("plenary_number") or 0)
                == int(session.plenary_number)
                else None
            )
    ballot_unlock_at = _ballot_unlock_at(ballot_schedule)
    ballot_time_open = ballot_unlock_at is None or now >= ballot_unlock_at
    viewer_ballot_available = bool(
        viewer_participant is not None
        and viewer_participant.confirmed
        and ballot_time_open
    )
    viewer_can_vote = bool(
        session is not None
        and session.stage == "voting"
        and session.current_bill is not None
        and viewer_ballot_available
    )
    last_session_payload: dict[str, Any] | None = None
    finished_session = None
    if latest_finished_snapshot is not None:
        try:
            finished_session = session_from_snapshot(latest_finished_snapshot)
        except (ConsensusStateError, TypeError, ValueError):
            finished_session = None
        if finished_session is not None:
            last_session_payload = _session_payload(
                finished_session,
                guild_id,
                simulation=False,
            )
            last_session_payload["finished_at"] = str(
                latest_finished_snapshot.get("finished_at") or ""
            ) or None
    phase = _broadcast_phase(
        session=session,
        schedule=schedule_row,
        latest_finished=latest_finished_snapshot,
        now=now,
    )
    state: dict[str, Any] = {
        "updated_at": now.isoformat(),
        "guild": {
            "id": guild_id,
            "name": str(getattr(guild, "name", "") or "Товарищество"),
        },
        "mode": requested_mode,
        "mode_label": "Симуляция" if selected_simulation else "Рабочий контур",
        "available_modes": available_modes,
        "active": session is not None and not session.finished,
        "broadcast_phase": phase,
        "schedule": public_schedule_payload(schedule_row),
        "session": None,
        "last_session": last_session_payload,
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
                "decision_category": str(row.get("decision_category") or "ordinary"),
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
        "recent_results": [_result_payload(row, guild_id) for row in recent_rows],
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
            "participant": viewer_participant is not None,
            "confirmed": bool(viewer_participant and viewer_participant.confirmed),
            "participant_kind": (
                str(viewer_participant.kind) if viewer_participant is not None else None
            ),
            "ballot_available": viewer_ballot_available,
            "ballot_unlock_at": (
                ballot_unlock_at.isoformat() if ballot_unlock_at is not None else None
            ),
            "can_vote": viewer_can_vote,
            "vote": viewer_vote,
            "csrf_token": principal.csrf_token if principal else None,
        },
        "capabilities": consensus_web_capabilities(
            mode=requested_mode,
            session=session,
            principal=principal,
        ),
    }
    if (
        session is None
        and principal is not None
        and finished_session is not None
        and phase == "completed"
    ):
        completed_participant = finished_session.participants.get(int(principal.user_id))
        if completed_participant is not None and completed_participant.confirmed:
            state["viewer"].update(
                {
                    "participant": True,
                    "confirmed": True,
                    "participant_kind": str(completed_participant.kind),
                    "ballot_available": True,
                    "can_vote": False,
                    "post_session": True,
                }
            )
    if principal is None and last_session_payload is not None:
        last_session_payload["participants"] = []
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
            bill_row = recent_bill_rows.get(int(current_result.get("bill_id") or 0))
            if bill_row is not None:
                current_result["bill"] = _bill_payload(
                    bill_row,
                    guild_id,
                    fallback_result=bill_row,
                )
    state["session"] = session_payload
    if (
        principal is not None
        and int(session.leader_id) == int(principal.user_id)
        and session.stage in {"voting", "finalizing"}
    ):
        # A live ballot direction is privileged operational data.  It is
        # projected only into the verified leader's response, never into the
        # public broadcast or another administrator's session.
        for participant_payload in session_payload["participants"]:
            participant_id = int(participant_payload.get("id") or 0)
            participant_payload["vote"] = (
                str(session.votes.get(participant_id) or "") or None
            )
    if principal is None:
        # The broadcast is public, but the named roster and private delivery
        # diagnostics remain visible only after a verified T-Mod login.
        session_payload["participants"] = []
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


def _canonical_surface_location(request: web.Request) -> str | None:
    """Keep every public T-Mod surface on its own canonical hostname."""

    path = str(request.path or "/")
    next_path = str(request.query.get("next") or "/")
    consensus_url = CONSENSUS_WEB_PUBLIC_URL or "https://consensus.tvr.lat"
    target_url: str | None = None

    def belongs_to(prefix: str) -> bool:
        return path == prefix or path.startswith(f"{prefix}/")

    if path == "/":
        target_url = consensus_url
    elif belongs_to("/host"):
        target_url = consensus_url
    elif belongs_to("/admin"):
        target_url = REACTOR_WEB_PUBLIC_URL
    elif belongs_to("/reactor") or belongs_to("/games"):
        target_url = PORTAL_WEB_PUBLIC_URL
    elif belongs_to("/atlas"):
        target_url = ATLAS_WEB_PUBLIC_URL
    elif belongs_to("/sgl"):
        target_url = SGL_WEB_PUBLIC_URL
    elif belongs_to("/ovr") or path.startswith("/api/ovr"):
        target_url = OVR_WEB_PUBLIC_URL
    elif belongs_to("/egg"):
        target_url = ZIGMUND_WEB_PUBLIC_URL
    elif path in {"/login", "/auth/ticket"}:
        if next_path == "/admin":
            target_url = REACTOR_WEB_PUBLIC_URL
        elif next_path in {"/reactor", "/games"}:
            target_url = PORTAL_WEB_PUBLIC_URL
        elif next_path == "/atlas":
            target_url = ATLAS_WEB_PUBLIC_URL
        elif next_path == "/sgl":
            target_url = SGL_WEB_PUBLIC_URL
        elif next_path == "/ovr":
            target_url = OVR_WEB_PUBLIC_URL
        else:
            target_url = consensus_url
    if not target_url:
        return None

    target = urlsplit(target_url)
    current_host = str(request.host or "").strip().lower().rstrip(".")
    if current_host.startswith("["):
        current_hostname = current_host[1:].split("]", 1)[0]
    else:
        current_hostname = current_host.split(":", 1)[0]
    ecosystem_hosts = {
        str(urlsplit(value).hostname or "").lower()
        for value in (
            consensus_url,
            REACTOR_WEB_PUBLIC_URL,
            PORTAL_WEB_PUBLIC_URL,
            ATLAS_WEB_PUBLIC_URL,
            ZIGMUND_WEB_PUBLIC_URL,
            SGL_WEB_PUBLIC_URL,
            OVR_WEB_PUBLIC_URL,
        )
    }
    target_hostname = str(target.hostname or "").lower()
    if current_hostname not in ecosystem_hosts or current_hostname == target_hostname:
        return None
    return f"{target.scheme}://{target.netloc}{request.rel_url}"


@web.middleware
async def _security_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    started_at = time.perf_counter()
    try:
        canonical_location = _canonical_surface_location(request)
        if canonical_location is not None:
            raise web.HTTPPermanentRedirect(location=canonical_location)
        response = await handler(request)
    except ConnectionResetError:
        # The browser can close a request while navigating away or cancelling a
        # stream. This is not an application failure and must not reach aiohttp's
        # error logger (or the GitHub defect inbox).
        response = web.Response(status=499)
    except web.HTTPException as exc:
        _apply_security_headers(exc, request_path=request.path)
        exc.headers["Server-Timing"] = (
            f'app;dur={(time.perf_counter() - started_at) * 1000:.1f}'
        )
        raise
    _apply_security_headers(response, request_path=request.path)
    response.headers["Server-Timing"] = (
        f'app;dur={(time.perf_counter() - started_at) * 1000:.1f}'
    )
    return response


def _apply_security_headers(
    response: web.StreamResponse,
    *,
    request_path: str = "",
) -> None:
    path = str(request_path or "")
    if path.startswith(("/assets/", "/sgl/assets/")):
        if path.endswith((".woff2", ".mp3")):
            response.headers["Cache-Control"] = (
                "public, max-age=2592000, immutable"
            )
        else:
            response.headers["Cache-Control"] = (
                "public, max-age=300, stale-while-revalidate=86400"
            )
    elif path == "/favicon.ico":
        response.headers["Cache-Control"] = "public, max-age=86400"
    else:
        # HTML and all personalized API projections must never be stored by a
        # shared cache. Read projections use an in-process SWR cache instead.
        response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), geolocation=(), payment=(), usb=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' "
        "'sha256-0IYaU6NkDTflYaDbUR4nMFteY9tDTb1ADhuFP1o95po=' "
        "'sha256-kivcxaEPD+v/Ecc3Z+TNAW/Uf1rs+0/EwVf6c/m1dKc='; "
        "img-src 'self' data:; connect-src 'self' https://api.open-meteo.com; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; object-src 'none'; form-action 'self'"
    )


def create_consensus_web_app(
    bot: discord.Client,
    *,
    guild_id: int,
) -> web.Application:
    try:
        upload_mib = int(os.getenv("MINECRAFT_UPLOAD_MAX_MIB", "128").strip())
    except (TypeError, ValueError):
        upload_mib = 128
    client_max_size = (max(1, min(upload_mib, 512)) + 2) * 1024 * 1024

    @web.middleware
    async def global_ban_middleware(
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        identity = signed_session_identity(
            request,
            expected_guild_id=int(guild_id),
        )
        ban = None
        if identity is not None:
            ban = await asyncio.to_thread(
                global_ban_storage.get_global_ban,
                int(guild_id),
                int(identity[1]),
            )
        request[_GLOBAL_BAN_REQUEST_KEY] = ban
        if ban is not None:
            allowed = (
                request.path in {"/banned", "/api/banned", "/api/health", "/favicon.ico"}
                or request.path.startswith("/assets/")
            )
            if not allowed:
                if request.path.startswith("/api/"):
                    return web.json_response(
                        {
                            "error": "globally_banned",
                            "message": "Доступ к экосистеме T-Mod заблокирован.",
                        },
                        status=423,
                    )
                raise web.HTTPSeeOther(location="/banned")
        return await handler(request)

    app = web.Application(
        middlewares=[_security_middleware, global_ban_middleware],
        client_max_size=client_max_size,
    )
    state_cache = AsyncSnapshotCache[
        tuple[str, int, str, bool], dict[str, Any]
    ](
        ttl_seconds=1.5,
        max_stale_seconds=15,
    )

    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(_ASSET_DIR / "index.html")

    async def tasks_page(request: web.Request) -> web.StreamResponse:
        principal = await resolve_principal(request, bot, guild_id=int(guild_id))
        if principal is None or not principal.guild_member:
            raise web.HTTPSeeOther(location="/login?next=/tasks")
        return web.FileResponse(_ASSET_DIR / "tasks.html")

    async def banned_page(request: web.Request) -> web.StreamResponse:
        if request.get(_GLOBAL_BAN_REQUEST_KEY) is None:
            raise web.HTTPSeeOther(location="/")
        return web.FileResponse(_ASSET_DIR / "banned.html")

    async def banned_state(request: web.Request) -> web.Response:
        ban = request.get(_GLOBAL_BAN_REQUEST_KEY)
        if ban is None:
            return web.json_response({"active": False})
        return web.json_response(
            {
                "active": True,
                "user_id": str(ban.get("user_id") or ""),
                "reason": str(ban.get("reason") or "Причина не указана."),
                "issued_at": ban.get("issued_at"),
                "reference": f"GB-{int(ban.get('revision') or 1):03d}",
            }
        )

    async def tasks_api(request: web.Request) -> web.Response:
        principal = await resolve_principal(request, bot, guild_id=int(guild_id))
        if principal is None or not principal.guild_member:
            return web.json_response({"error": "member_login_required"}, status=401)
        if request.method == "GET":
            tasks = await asyncio.to_thread(
                legislation_storage.task_board,
                int(guild_id),
            )
            return web.json_response(
                {
                    "viewer": {
                        "id": str(principal.user_id),
                        "name": principal.display_name,
                        "csrf_token": principal.csrf_token,
                    },
                    "tasks": tasks,
                }
            )
        if not csrf_matches(request, principal):
            return web.json_response({"error": "csrf_failed"}, status=403)
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            body = None
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid_payload"}, status=400)
        try:
            action = str(body.get("action") or "").strip().lower()
            if action == "create":
                task = await asyncio.to_thread(
                    legislation_storage.create_task,
                    guild_id=int(guild_id),
                    title=str(body.get("title") or ""),
                    description=str(body.get("description") or ""),
                    priority=str(body.get("priority") or "normal"),
                    assignee_id=None,
                    assignee_display=None,
                    due_at=str(body.get("due_at") or "") or None,
                    actor_id=int(principal.user_id),
                    actor_display=str(principal.display_name),
                )
            elif action == "update":
                task = await asyncio.to_thread(
                    legislation_storage.update_task,
                    int(body.get("task_id") or 0),
                    guild_id=int(guild_id),
                    expected_revision=int(body.get("expected_revision") or 0),
                    status=str(body.get("status") or ""),
                )
            else:
                return web.json_response({"error": "task_action_invalid"}, status=400)
        except (TypeError, ValueError) as exc:
            code = str(exc) or "task_invalid"
            return web.json_response(
                {
                    "error": code,
                    "message": (
                        "Доска изменилась. Обновите страницу и повторите."
                        if "revision" in code
                        else "Проверьте название, срок и состояние задачи."
                    ),
                },
                status=409 if "revision" in code else 400,
            )
        return web.json_response({"ok": True, "task": task})

    async def host_page(request: web.Request) -> web.StreamResponse:
        principal = await resolve_principal(
            request,
            bot,
            guild_id=int(guild_id),
        )
        if principal is None:
            raise web.HTTPSeeOther(location="/login?next=/host")
        live_session = active_sessions.get(int(guild_id))
        simulation = get_consensus_simulation(int(guild_id))
        is_active_leader = bool(
            (live_session is not None and int(live_session.leader_id) == int(principal.user_id))
            or (simulation is not None and int(simulation.leader_id) == int(principal.user_id))
        )
        if not (principal.administrator or is_chair(principal.member) or is_active_leader):
            raise web.HTTPForbidden(text="Суфлёр доступен только ведущему и председателям.")
        return web.FileResponse(_ASSET_DIR / "host.html")

    async def egg(_: web.Request) -> web.FileResponse:
        return web.FileResponse(_ASSET_DIR / "egg.html")

    async def asset(request: web.Request) -> web.FileResponse:
        name = str(request.match_info["name"])
        if name not in {
            "app.js",
            "host.css",
            "host.js",
            "style.css",
            "chamber.css",
            "consensus-v4.css",
            "egg.css",
            "egg.js",
            "zigmund-murchalki.mp3",
            "admin.css",
            "fonts.css",
            "admin.js",
            "reactor.js",
            "tab-signal.js",
            "favicon.svg",
            "portal.css",
            "portal-theme.css",
            "portal-focus.css",
            "portal.js",
            "ovr.css",
            "ovr.js",
            "games.css",
            "games.js",
            "manrope-cyrillic.woff2",
            "manrope-latin.woff2",
            "unbounded-cyrillic.woff2",
            "unbounded-latin.woff2",
            "source-serif-cyrillic.woff2",
            "source-serif-latin.woff2",
            "login.css",
            "login.js",
            "tasks.css",
            "tasks.js",
            "banned.css",
            "banned.js",
            "ban-seal.svg",
        }:
            raise web.HTTPNotFound()
        response = web.FileResponse(_ASSET_DIR / name)
        if name.endswith(".woff2"):
            response.content_type = "font/woff2"
        return response

    async def login_page(request: web.Request) -> web.StreamResponse:
        next_path = (
            str(request.query.get("next"))
            if request.query.get("next") in {"/admin", "/reactor", "/atlas", "/games", "/sgl", "/ovr", "/host", "/tasks"}
            else "/"
        )
        principal = await resolve_principal(request, bot, guild_id=int(guild_id))
        if principal is not None:
            if not principal.guild_member and next_path != "/atlas":
                raise web.HTTPSeeOther(location="/atlas")
            if next_path != "/admin" or principal.administrator:
                raise web.HTTPSeeOther(location=next_path)
            grants = await asyncio.to_thread(
                credential_storage.web_section_grants,
                int(guild_id),
                int(principal.user_id),
            )
            if any(str(item.get("section")) != "atlas_ai" for item in grants):
                raise web.HTTPSeeOther(location=next_path)
        return web.FileResponse(_ASSET_DIR / "login.html")

    async def favicon(_: web.Request) -> web.FileResponse:
        return web.FileResponse(_ASSET_DIR / "favicon.svg")

    async def health(request: web.Request) -> web.Response:
        discord_ready = bool(getattr(bot, "is_ready", lambda: False)())
        require_ready = request.query.get("ready") == "1"
        status = "ok" if discord_ready or not require_ready else "starting"
        return web.json_response(
            {"status": status, "discord_ready": discord_ready},
            status=200 if status == "ok" else 503,
        )

    async def authenticated_request(
        request: web.Request,
    ) -> tuple[ConsensusWebPrincipal | None, bool]:
        principal = await resolve_principal(
            request,
            bot,
            guild_id=int(guild_id),
        )
        if principal is not None:
            if (
                not principal.guild_member
                and not request.path.startswith("/api/atlas")
                and request.path != "/api/sgl/bootstrap"
                and request.path != "/api/desktop/v1/bootstrap"
            ):
                raise web.HTTPForbidden(
                    text=json.dumps(
                        {
                            "error": "zero_account_scope",
                            "message": "Нулевой аккаунт не имеет доступа к Товариществу.",
                        },
                        ensure_ascii=False,
                    ),
                    content_type="application/json",
                )
            _failed_auth[_request_remote(request)].clear()
            return principal, False
        supplied = _request_token(request)
        if supplied and hmac.compare_digest(supplied, _access_token()):
            return None, True
        if request.path == "/api/sgl/bootstrap":
            # The SGL landing is public. The route itself returns only contact
            # information unless a verified manager identity is present.
            return None, False
        if request.path == "/api/desktop/v1/bootstrap":
            # The desktop shell needs a deterministic login-required response
            # without consuming the shared brute-force failure budget while it
            # performs its lightweight background refresh.
            return None, False
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

    async def public_consensus_request(
        request: web.Request,
    ) -> tuple[ConsensusWebPrincipal | None, bool]:
        """Resolve an identity when present without locking the public broadcast."""

        principal = await resolve_principal(
            request,
            bot,
            guild_id=int(guild_id),
        )
        if principal is not None and principal.guild_member:
            return principal, False
        supplied = _request_token(request)
        if supplied and hmac.compare_digest(supplied, _access_token()):
            return None, True
        return None, False

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
        if await asyncio.to_thread(
            global_ban_storage.is_globally_banned,
            int(guild_id),
            int(user_id),
        ):
            raise web.HTTPForbidden(text="Доступ к экосистеме T-Mod заблокирован.")
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
        mode = "simulation" if request.query.get("mode") == "simulation" else "live"
        destination = (
            str(request.query.get("next"))
            if request.query.get("next") in {"/admin", "/reactor", "/atlas", "/games", "/sgl", "/ovr", "/host", "/tasks"}
            else f"/?mode={mode}"
        )
        if destination == "/host" and mode == "simulation":
            destination = "/host?mode=simulation"
        response = web.Response(
            status=302,
            headers={"Location": destination},
        )
        set_session_cookie(
            response,
            token,
            secure=bool(CONSENSUS_WEB_PUBLIC_URL or request.secure),
            request_host=request.host,
        )
        return response

    async def credential_login(request: web.Request) -> web.Response:
        remote = _request_remote(request)
        now = asyncio.get_running_loop().time()
        desktop_client = request.query.get("client") == "desktop"
        attempts = _login_failures[remote]
        while attempts and now - attempts[0] > 10 * 60:
            attempts.popleft()
        next_path = (
            str(request.query.get("next"))
            if request.query.get("next") in {"/admin", "/reactor", "/atlas", "/games", "/sgl", "/ovr", "/host", "/tasks"}
            else "/"
        )
        if len(attempts) >= 15:
            raise web.HTTPSeeOther(
                location=f"/login?{urlencode({'next': next_path, 'error': 'locked'})}"
            )
        try:
            body = await request.post()
        except (ValueError, web.HTTPException):
            body = {}
        result = await asyncio.to_thread(
            credential_storage.authenticate_web_credential,
            int(guild_id),
            str(body.get("login") or "")[:64],
            str(body.get("pin") or "")[:32],
        )
        if result.status != "ok" or result.credential is None:
            attempts.append(now)
            if result.notify_owner and result.user_id:
                await asyncio.to_thread(
                    reactor_storage.reactor_put_notification,
                    guild_id=int(guild_id),
                    user_id=int(result.user_id),
                    severity=(
                        "critical" if result.status == "reset_required" else "warning"
                    ),
                    kind="security",
                    title=(
                        "Веб-доступ заблокирован"
                        if result.status == "reset_required"
                        else "Неудачная попытка входа"
                    ),
                    body=(
                        "Три неверных PIN. Используйте /reset в ЛС с ботом."
                        if result.status == "reset_required"
                        else f"Неверный PIN: попытка {result.failed_attempts}/3 · {remote}"
                    ),
                    route="/reactor",
                    dedupe_key="web-login-security",
                )
                guild = bot.get_guild(int(guild_id))
                member = guild.get_member(int(result.user_id)) if guild is not None else None
                fetch_member = getattr(guild, "fetch_member", None)
                if member is None and callable(fetch_member):
                    try:
                        member = await fetch_member(int(result.user_id))
                    except discord.DiscordException:
                        member = None
                get_user = getattr(bot, "get_user", None)
                recipient = member or (
                    get_user(int(result.user_id)) if callable(get_user) else None
                )
                if recipient is None:
                    fetch_user = getattr(bot, "fetch_user", None)
                    try:
                        recipient = (
                            await fetch_user(int(result.user_id))
                            if callable(fetch_user)
                            else None
                        )
                    except discord.DiscordException:
                        recipient = None
                if recipient is not None and callable(getattr(recipient, "send", None)):
                    embed = discord.Embed(
                        title=(
                            "Веб-доступ T-Mod заблокирован"
                            if result.status == "reset_required"
                            else "Неудачная попытка веб-входа"
                        ),
                        description=(
                            "После трёх неверных PIN доступ закрыт. Отправьте боту "
                            "команду `/reset` в личных сообщениях и задайте новый PIN."
                            if result.status == "reset_required"
                            else (
                                f"Кто-то ввёл неверный PIN для вашего логина. "
                                f"Попытка **{result.failed_attempts}/3**."
                            )
                        ),
                        color=(
                            0xED4245 if result.status == "reset_required" else 0xF0B232
                        ),
                    )
                    embed.add_field(
                        name="Источник",
                        value=f"`{remote}` · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                        inline=False,
                    )
                    try:
                        await recipient.send(
                            embed=embed,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.DiscordException:
                        pass
            error = "reset_required" if result.status == "reset_required" else "invalid"
            raise web.HTTPSeeOther(
                location=f"/login?{urlencode({'next': next_path, 'error': error})}"
            )
        if await asyncio.to_thread(
            global_ban_storage.is_globally_banned,
            int(guild_id),
            int(result.credential.user_id),
        ):
            raise web.HTTPForbidden(text="Доступ к экосистеме T-Mod заблокирован.")
        characters = await asyncio.to_thread(
            profile_storage.list_profile_characters,
            int(guild_id),
            int(result.credential.user_id),
        )
        if not characters:
            raise web.HTTPSeeOther(
                location=f"/login?{urlencode({'next': next_path, 'error': 'character_required'})}"
            )
        guild = bot.get_guild(int(guild_id))
        member = guild.get_member(int(result.credential.user_id)) if guild is not None else None
        if member is None and guild is not None:
            try:
                member = await guild.fetch_member(int(result.credential.user_id))
            except discord.DiscordException:
                member = None
        grants = await asyncio.to_thread(
            credential_storage.web_section_grants,
            int(guild_id),
            int(result.credential.user_id),
        )
        sections = {str(item["section"]) for item in grants}
        is_administrator = bool(member and member.guild_permissions.administrator)
        if member is None and not desktop_client:
            if next_path != "/atlas" or "atlas_ai" not in sections:
                raise web.HTTPSeeOther(
                    location="/login?next=%2Fatlas&error=atlas_access"
                )
        elif next_path == "/admin" and not is_administrator:
            if not (sections - {"atlas_ai"}):
                raise web.HTTPSeeOther(
                    location="/login?next=%2Fadmin&error=administrator"
                )
        attempts.clear()
        token, _ = create_session_token(
            guild_id=int(guild_id),
            user_id=int(result.credential.user_id),
            lifetime_seconds=PERSISTENT_SESSION_LIFETIME_SECONDS,
            session_version=int(result.credential.session_version),
        )
        response = web.Response(
            status=303,
            headers={"Location": next_path},
        )
        set_session_cookie(
            response,
            token,
            secure=bool(CONSENSUS_WEB_PUBLIC_URL or request.secure),
            max_age=PERSISTENT_SESSION_LIFETIME_SECONDS,
            request_host=request.host,
        )
        return response

    async def logout(request: web.Request) -> web.Response:
        host = str(request.host or "").split(":", 1)[0].lower()
        destination = (
            "/login?next=/admin"
            if host.startswith("reactor.")
            else "/login?next=/ovr"
            if host.startswith("ovr.")
            else "/login?next=/reactor"
            if host == "tvr.lat"
            else "/"
        )
        response = web.Response(
            status=302,
            headers={"Location": destination},
        )
        clear_session_cookie(
            response,
            secure=bool(CONSENSUS_WEB_PUBLIC_URL or request.secure),
            request_host=request.host,
        )
        return response

    async def state(request: web.Request) -> web.Response:
        principal, legacy_read_only = await public_consensus_request(request)
        requested_mode = str(request.query.get("mode") or "").strip().lower()
        cache_key = (
            requested_mode,
            int(principal.user_id) if principal is not None else 0,
            str(principal.csrf_token) if principal is not None else "legacy",
            bool(legacy_read_only),
        )
        snapshot, cache_state = await state_cache.get(
            cache_key,
            lambda: build_consensus_web_state(
                bot,
                int(guild_id),
                mode=requested_mode,
                principal=principal,
                legacy_read_only=legacy_read_only,
            ),
            force=request.query.get("fresh") == "1",
        )
        response = web.json_response(
            {**snapshot, "cache_state": cache_state},
            dumps=lambda value: json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        response.headers["X-T-Mod-Cache"] = cache_state
        return response

    async def bills(request: web.Request) -> web.Response:
        principal, legacy_read_only = await public_consensus_request(request)
        requested_mode = (
            "simulation" if request.query.get("mode") == "simulation" else "live"
        )
        if requested_mode == "simulation":
            if principal is None and not legacy_read_only:
                raise web.HTTPForbidden(
                    text=json.dumps({"error": "simulation_login_required"}),
                    content_type="application/json",
                )
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
                int(result.bill_number) for result in simulation.session.results
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
                "items": [_catalog_bill_payload(row, int(guild_id)) for row in rows],
            },
        )

    async def bill_detail(request: web.Request) -> web.Response:
        principal, legacy_read_only = await public_consensus_request(request)
        try:
            bill_id = int(request.match_info["bill_id"])
        except (TypeError, ValueError):
            raise web.HTTPNotFound()
        if bill_id <= 0:
            raise web.HTTPNotFound()

        requested_mode = (
            "simulation" if request.query.get("mode") == "simulation" else "live"
        )
        if requested_mode == "simulation":
            if principal is None and not legacy_read_only:
                raise web.HTTPForbidden(
                    text=json.dumps({"error": "simulation_login_required"}),
                    content_type="application/json",
                )
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
        if raw_bill is None or int(raw_bill.get("guild_id") or 0) != int(guild_id):
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

    async def consensus_report(request: web.Request) -> web.StreamResponse:
        session_key = str(request.match_info.get("session_key") or "").strip()
        if not session_key or len(session_key) > 180:
            raise web.HTTPNotFound()
        snapshot = await asyncio.to_thread(
            tvrs_storage.tvrs_latest_finished_consensus_session,
            int(guild_id),
        )
        if snapshot is None or str(snapshot.get("session_key") or "") != session_key:
            raise web.HTTPNotFound()
        try:
            finished_session = session_from_snapshot(snapshot)
        except (ConsensusStateError, TypeError, ValueError) as exc:
            raise web.HTTPServiceUnavailable(text="Протокол временно недоступен.") from exc
        report = await asyncio.to_thread(generate_session_report, finished_session)
        response = web.FileResponse(report)
        response.content_type = "application/pdf"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="consensus-{int(finished_session.plenary_number):02d}-protocol.pdf"'
        )
        return response

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
                {
                    "error": "csrf_failed",
                    "message": "Обновите панель и повторите действие.",
                },
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
                {
                    "error": "invalid_payload",
                    "message": "Некорректные параметры команды.",
                },
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
        state_cache.invalidate()
        _command_receipts[receipt_key] = (now + 300.0, response_payload)
        return web.json_response(response_payload)

    app.router.add_get("/", index)
    app.router.add_get("/tasks", tasks_page)
    app.router.add_get("/tasks/", tasks_page)
    app.router.add_get("/banned", banned_page)
    app.router.add_get("/host", host_page)
    app.router.add_get("/host/", host_page)
    app.router.add_get("/login", login_page)
    app.router.add_get("/egg", egg)
    app.router.add_get("/egg/", egg)
    app.router.add_get("/assets/{name}", asset)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/auth/ticket", ticket_login)
    app.router.add_post("/auth/login", credential_login)
    app.router.add_get("/auth/logout", logout)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/banned", banned_state)
    app.router.add_get("/api/tasks", tasks_api)
    app.router.add_post("/api/tasks", tasks_api)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/bills", bills)
    app.router.add_get("/api/bills/{bill_id}", bill_detail)
    app.router.add_get("/api/reports/{session_key}/consensus.pdf", consensus_report)
    app.router.add_post("/api/command", command)
    register_admin_web_routes(
        app,
        bot,
        guild_id=int(guild_id),
        asset_dir=_ASSET_DIR,
        authenticate=authenticated_request,
    )
    register_reactor_web_routes(
        app,
        bot,
        guild_id=int(guild_id),
        asset_dir=_ASSET_DIR,
        authenticate=authenticated_request,
    )
    register_games_web_routes(
        app,
        bot,
        guild_id=int(guild_id),
        asset_dir=_ASSET_DIR,
        authenticate=authenticated_request,
    )
    register_atlas_web_routes(
        app,
        bot,
        guild_id=int(guild_id),
        asset_dir=_ATLAS_ASSET_DIR,
        authenticate=authenticated_request,
    )
    register_sgl_web_routes(
        app,
        bot,
        guild_id=int(guild_id),
        asset_dir=Path(__file__).resolve().parents[1] / "web" / "sgl",
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
    host_url = consensus_web_entry_url(
        guild_id=interaction.guild.id,
        user_id=interaction.user.id,
        destination="/host",
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
    view.add_item(
        discord.ui.Button(
            label="Суфлёр ведущего",
            emoji="🎤",
            style=discord.ButtonStyle.link,
            url=host_url,
        )
    )
    embed.add_field(
        name=("Прямой HTTPS" if public else "Если имя не открывается"),
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
