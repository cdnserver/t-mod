"""Role-aware Reactor web surfaces and their live operational APIs."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import discord
from aiohttp import web

from modules.consensus_runtime import active_sessions
from modules.consensus_web_auth import ConsensusWebPrincipal, csrf_matches
from modules.minecraft_control import (
    MinecraftControlError,
    minecraft_execute,
    minecraft_rcon,
    minecraft_status,
)
from modules.minecraft_files import (
    MinecraftFilesError,
    minecraft_backup_path,
    minecraft_create_backup,
    minecraft_delete_backup,
    minecraft_download_path,
    minecraft_files_config,
    minecraft_finish_upload,
    minecraft_list_backups,
    minecraft_list_directory,
    minecraft_list_plugins,
    minecraft_list_trash,
    minecraft_make_directory,
    minecraft_prepare_upload,
    minecraft_read_text,
    minecraft_repair_plugin_permissions,
    minecraft_rename_path,
    minecraft_restore_backup,
    minecraft_restore_trash,
    minecraft_set_plugin_state,
    minecraft_storage_overview,
    minecraft_tail_log,
    minecraft_trash_path,
    minecraft_write_text,
)
from modules.profile import PROFILE_ROLE_HIERARCHY
from modules.member_identity import fellowship_discord_nickname, nickname_change_exempt
from modules.ovr_artifacts import (
    encrypt_investigation_report,
    generate_investigation_report,
)
from modules.technical_log import log_technical_event
from modules.admission import process_ovr_decision
from modules.reactor_legislation import (
    ReactorLegislationError,
    cancel_workspace,
    create_workspace,
    generate_workspace_draft,
    legislation_snapshot,
    moderate_workspace,
    publish_workspace,
    save_workspace,
)
from modules.reactor_preparation_service import build_reactor_preparation_payload
from modules.tvrs_bill_editor import refresh_bill_workspace_panel
from modules.tvrs_presentation import is_chair
from modules.web_snapshot_cache import AsyncSnapshotCache
from modules.desktop_bootstrap_service import (
    DesktopBootstrapError,
    build_desktop_bootstrap_payload,
)
from persistence import activity_repository as activity_storage
from persistence import admin_dashboard_repository as dashboard_storage
from persistence import bill_workspace_repository as workspace_storage
from persistence import finance_repository as finance_storage
from persistence import market_repository as market_storage
from persistence import ovr_repository as ovr_storage
from persistence import admission_repository as admission_storage
from persistence import outbox_repository as outbox_storage
from persistence import profile_repository as profile_storage
from persistence import reactor_repository as reactor_storage
from persistence import tvrs_repository as tvrs_storage
from persistence import web_auth_repository as web_auth_storage


AuthenticatedRequest = Callable[
    [web.Request],
    Awaitable[tuple[ConsensusWebPrincipal | None, bool]],
]


logger = logging.getLogger(__name__)



def _member_positions(member: Any) -> list[dict[str, Any]]:
    role_ids = {
        int(getattr(role, "id", 0) or 0) for role in getattr(member, "roles", ())
    }
    return [
        {
            "role_id": int(role_id),
            "emoji": str(emoji),
            "label": str(label),
            "description": str(description),
        }
        for role_id, emoji, label, description in PROFILE_ROLE_HIERARCHY
        if int(role_id) in role_ids
    ]


def _primary_character(profile: Any, characters: list[Any]) -> Any | None:
    primary_id = int(getattr(profile, "primary_character_id", 0) or 0)
    return next(
        (item for item in characters if int(getattr(item, "id", 0)) == primary_id),
        characters[0] if characters else None,
    )


def _nickname_state(member: Any, profile: Any, characters: list[Any]) -> dict[str, Any]:
    exempt = nickname_change_exempt(member)
    character = _primary_character(profile, characters)
    preferred_name = str(getattr(profile, "preferred_name", "") or "").strip()
    expected = None
    error = None
    if character is not None and preferred_name:
        try:
            expected = fellowship_discord_nickname(
                str(character.nickname),
                str(character.static_id),
                preferred_name,
            )
        except ValueError as exc:
            error = str(exc)
    current = str(getattr(member, "nick", "") or "").strip() or None
    return {
        "exempt": exempt,
        "expected": expected,
        "current": current,
        "synced": bool(exempt or (expected and current == expected)),
        "error": error,
    }


def _consensus_summary(guild_id: int) -> dict[str, Any]:
    runtime = active_sessions.get(int(guild_id))
    if runtime is not None:
        return {
            "active": True,
            "stage": str(getattr(runtime, "stage", "active")),
            "session_key": str(getattr(runtime, "session_key", "")),
            "plenary_number": int(getattr(runtime, "plenary_number", 0) or 0),
            "bill_number": int(getattr(runtime, "bill_number", 0) or 0),
            "source": "runtime",
        }
    restored = tvrs_storage.tvrs_consensus_active_sessions(int(guild_id))
    if restored:
        snapshot = restored[-1]
        return {
            "active": True,
            "stage": str(snapshot.get("stage") or "restoring"),
            "session_key": str(snapshot.get("session_key") or ""),
            "plenary_number": int(snapshot.get("plenary_number") or 0),
            "bill_number": int(snapshot.get("bill_number") or 0),
            "source": "persistent",
        }
    queue = tvrs_storage.tvrs_queue_bills(int(guild_id), limit=20)
    return {
        "active": False,
        "stage": "idle",
        "queued_bills": len(queue),
        "source": "persistent",
    }


def _member_treasury_summary(guild_id: int) -> dict[str, Any]:
    """Return collective treasury totals without audit-only information."""

    state = finance_storage.finance_get_latest_state(int(guild_id))
    stats = finance_storage.finance_stats(int(guild_id), days=30)
    report = state.get("latest_report") or {}
    latest = state.get("latest_event") or {}
    return {
        "balance": state.get("estimated_balance"),
        "reported_balance": report.get("amount"),
        "report_date": report.get("report_date") or report.get("created_at"),
        "last_activity_at": latest.get("created_at"),
        "movements_after_report": int(state.get("movements_after_report") or 0),
        "period_days": int(stats.get("days") or 30),
        "deposits": int(stats.get("deposits") or 0),
        "withdrawals": int(stats.get("withdrawals") or 0),
        "net_flow": int(stats.get("net_flow") or 0),
        "movement_count": int(stats.get("movement_count") or 0),
    }


async def _minecraft_status_snapshot() -> dict[str, Any]:
    """Bound Minecraft probes so they can never stall the whole Reactor."""

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(minecraft_status),
            timeout=3.0,
        )
    except TimeoutError:
        return {
            "configured": True,
            "online": False,
            "state": "unknown",
            "address": "mc.tvr.lat",
            "error": "Проверка Minecraft превысила 3 секунды; повторяем в фоне.",
        }


async def _health_snapshot(
    bot: discord.Client,
    guild_id: int,
    *,
    minecraft_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    database, outbox, catalogs = await asyncio.gather(
        asyncio.to_thread(reactor_storage.reactor_database_health),
        asyncio.to_thread(outbox_storage.delivery_outbox_counts),
        asyncio.gather(
            *(
                asyncio.to_thread(
                    market_storage.market_catalog_status,
                    "RU15",
                    category,
                )
                for category in ("items", "vehicles", "clothes")
            )
        ),
    )
    minecraft = (
        minecraft_snapshot
        if minecraft_snapshot is not None
        else await _minecraft_status_snapshot()
    )
    guild = bot.get_guild(int(guild_id))
    latency = getattr(bot, "latency", None)
    try:
        latency_ms = max(0, round(float(latency) * 1000))
    except (TypeError, ValueError):
        latency_ms = None
    is_ready = getattr(bot, "is_ready", None)
    discord_state = (
        "ok"
        if guild is not None and callable(is_ready) and bool(is_ready())
        else "critical"
    )
    delivery_dead = int(outbox.get("dead", 0) or 0)
    delivery_retry = int(outbox.get("retry", 0) or 0)
    delivery_state = (
        "critical" if delivery_dead else "warning" if delivery_retry else "ok"
    )
    market_state = "ok"
    if not any(catalogs):
        market_state = "warning"
    elif any(str(item.get("last_error") or "").strip() for item in catalogs):
        market_state = "warning"
    minecraft_state = (
        "ok"
        if minecraft.get("online")
        else "warning"
        if minecraft.get("configured")
        else "disabled"
    )
    components = [
        {
            "id": "discord",
            "title": "Discord",
            "status": discord_state,
            "detail": (
                f"{latency_ms} мс · {len(getattr(guild, 'members', ()) or ())} участников"
                if guild is not None and latency_ms is not None
                else "соединение недоступно"
            ),
        },
        {
            "id": "database",
            "title": "SQLite",
            "status": str(database.get("status") or "critical"),
            "detail": (
                f"{str(database.get('journal_mode') or 'БД').upper()} · "
                f"ответ {database.get('latency_ms')} мс"
            ),
        },
        {
            "id": "delivery",
            "title": "Доставка",
            "status": delivery_state,
            "detail": (
                f"открыто {sum(int(outbox.get(key, 0) or 0) for key in ('pending', 'processing', 'retry'))}"
                f" · ошибок {delivery_dead}"
            ),
        },
        {
            "id": "market",
            "title": "Majestic Market",
            "status": market_state,
            "detail": f"каталогов готово {sum(1 for item in catalogs if item)}/3",
        },
        {
            "id": "minecraft",
            "title": "Minecraft",
            "status": minecraft_state,
            "detail": (
                f"{minecraft.get('players_online', 0)}/{minecraft.get('players_max', 0)} игроков"
                if minecraft.get("online")
                else str(minecraft.get("error") or "контур не настроен")
            ),
        },
    ]
    statuses = {str(item["status"]) for item in components}
    overall = (
        "critical"
        if "critical" in statuses
        else "warning"
        if "warning" in statuses
        else "nominal"
    )
    return {
        "overall": overall,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "components": components,
        "outbox": outbox,
        "minecraft": minecraft,
    }


async def _attention_snapshot(
    bot: discord.Client,
    guild_id: int,
    *,
    health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if health is None:
        counts, health = await asyncio.gather(
            asyncio.to_thread(
                dashboard_storage.admin_dashboard_counts,
                int(guild_id),
                days=30,
            ),
            _health_snapshot(bot, int(guild_id)),
        )
    else:
        counts = await asyncio.to_thread(
            dashboard_storage.admin_dashboard_counts,
            int(guild_id),
            days=30,
        )
    items: list[dict[str, Any]] = []

    def add(
        key: str,
        severity: str,
        title: str,
        detail: str,
        route: str,
        count: int,
    ) -> None:
        if int(count) <= 0:
            return
        items.append(
            {
                "key": key,
                "severity": severity,
                "title": title,
                "detail": detail,
                "route": route,
                "count": int(count),
            }
        )

    add(
        "delivery-dead",
        "critical",
        "Недоставленные сообщения",
        "Очередь исчерпала попытки; требуется вмешательство.",
        "#/system",
        int(counts.get("outbox_dead") or 0),
    )
    add(
        "craft-overdue",
        "warning",
        "Просроченные циклы крафта",
        "Срок цикла прошёл, но выпуск не принят.",
        "#/craft",
        int(counts.get("overdue_batches") or 0),
    )
    add(
        "finance-retry",
        "warning",
        "Повтор доставки финансов",
        "Есть финансовые уведомления после неудачной попытки.",
        "#/treasury",
        int(counts.get("finance_retries") or 0),
    )
    add(
        "craft-active",
        "info",
        "Крафты в работе",
        "Производственный контур ожидает действий или завершения.",
        "#/craft",
        int(counts.get("active_crafts") or 0),
    )
    consensus = await asyncio.to_thread(_consensus_summary, int(guild_id))
    if consensus.get("active"):
        items.append(
            {
                "key": "consensus-active",
                "severity": "info",
                "title": "Консенсус идёт",
                "detail": f"Текущая стадия: {consensus.get('stage', 'active')}",
                "route": "https://consensus.tvr.lat/",
                "count": 1,
            }
        )
    for component in health["components"]:
        if component["status"] in {"critical", "warning"}:
            items.append(
                {
                    "key": f"health-{component['id']}",
                    "severity": component["status"],
                    "title": f"Контур: {component['title']}",
                    "detail": component["detail"],
                    "route": "#/system"
                    if component["id"] != "minecraft"
                    else "#/minecraft",
                    "count": 1,
                }
            )
    order = {"critical": 0, "warning": 1, "info": 2, "success": 3}
    items.sort(
        key=lambda item: (order.get(str(item["severity"]), 9), str(item["title"]))
    )
    return {
        "items": items,
        "total": sum(int(item["count"]) for item in items),
        "health": health,
        "consensus": consensus,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def register_reactor_web_routes(
    app: web.Application,
    bot: discord.Client,
    *,
    guild_id: int,
    asset_dir: Path,
    authenticate: AuthenticatedRequest,
) -> None:
    """Attach member Reactor, admin live services, and safe Minecraft control."""

    minecraft_receipts: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
    attention_projection_signatures: dict[int, str] = {}
    attention_projection_lock = asyncio.Lock()
    health_cache = AsyncSnapshotCache[str, dict[str, Any]](
        ttl_seconds=12,
        max_stale_seconds=180,
    )
    minecraft_cache = AsyncSnapshotCache[str, dict[str, Any]](
        ttl_seconds=2,
        max_stale_seconds=60,
    )
    attention_cache = AsyncSnapshotCache[str, dict[str, Any]](
        ttl_seconds=8,
        max_stale_seconds=120,
    )
    member_home_cache = AsyncSnapshotCache[
        tuple[int, str], dict[str, Any]
    ](
        ttl_seconds=6,
        max_stale_seconds=60,
    )
    legislation_cache = AsyncSnapshotCache[int, dict[str, Any]](
        ttl_seconds=10,
        max_stale_seconds=90,
    )

    async def cached_minecraft(
        *, force: bool = False
    ) -> tuple[dict[str, Any], str]:
        return await minecraft_cache.get(
            "status",
            _minecraft_status_snapshot,
            force=force,
        )

    async def build_health() -> dict[str, Any]:
        minecraft, _ = await cached_minecraft()
        return await _health_snapshot(
            bot,
            int(guild_id),
            minecraft_snapshot=minecraft,
        )

    async def cached_health(*, force: bool = False) -> tuple[dict[str, Any], str]:
        return await health_cache.get(
            "health",
            build_health,
            force=force,
        )

    async def build_attention() -> dict[str, Any]:
        health, _ = await cached_health()
        return await _attention_snapshot(bot, int(guild_id), health=health)

    async def cached_attention(
        *,
        force: bool = False,
    ) -> tuple[dict[str, Any], str]:
        return await attention_cache.get(
            "attention",
            build_attention,
            force=force,
        )

    async def reactor_index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(asset_dir / "portal.html")

    async def ovr_index(request: web.Request) -> web.StreamResponse:
        principal, legacy = await authenticate(request)
        if legacy or principal is None:
            raise web.HTTPSeeOther(location="/login?next=/ovr")
        if not principal.guild_member:
            raise web.HTTPForbidden(text="Портал ОВР доступен только участникам сервера.")
        if not await has_ovr_access(principal):
            raise web.HTTPForbidden(
                text="Доступ к порталу ОВР выдаётся администратором вручную."
            )
        return web.FileResponse(asset_dir / "ovr.html")

    async def personal_request(request: web.Request) -> ConsensusWebPrincipal:
        principal, legacy = await authenticate(request)
        if legacy or principal is None:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "personal_login_required"}),
                content_type="application/json",
            )
        if not principal.guild_member:
            raise web.HTTPForbidden(
                text=json.dumps(
                    {
                        "error": "zero_account_reactor_forbidden",
                        "message": "Нулевой аккаунт не имеет доступа к данным Товарищества.",
                    },
                    ensure_ascii=False,
                ),
                content_type="application/json",
            )
        return principal

    def can_moderate_bills(principal: ConsensusWebPrincipal) -> bool:
        return bool(principal.administrator or is_chair(principal.member))

    async def has_ovr_access(principal: ConsensusWebPrincipal) -> bool:
        if principal.administrator:
            return True
        grants = await asyncio.to_thread(
            web_auth_storage.web_section_grants,
            int(guild_id),
            int(principal.user_id),
        )
        return any(str(row.get("section") or "") == "ovr" for row in grants)

    async def admin_request(request: web.Request) -> ConsensusWebPrincipal:
        principal = await personal_request(request)
        minecraft_granted = False
        if not principal.administrator and "/minecraft" in request.path:
            grants = await asyncio.to_thread(
                web_auth_storage.web_section_grants,
                int(guild_id),
                int(principal.user_id),
            )
            minecraft_granted = any(row["section"] == "minecraft" for row in grants)
        if not principal.administrator and not minecraft_granted:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "administrator_required"}),
                content_type="application/json",
            )
        return principal

    async def json_body(
        request: web.Request,
        principal: ConsensusWebPrincipal,
    ) -> dict[str, Any]:
        if not csrf_matches(request, principal):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "csrf_failed"}),
                content_type="application/json",
            )
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            body = None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "invalid_payload"}),
                content_type="application/json",
            )
        return body

    def minecraft_error(exc: MinecraftFilesError | OSError) -> web.Response:
        if isinstance(exc, OSError):
            return web.json_response(
                {
                    "error": "minecraft_filesystem_error",
                    "message": "Операционная система не разрешила файловую операцию.",
                },
                status=500,
            )
        if exc.code in {
            "minecraft_path_missing",
            "minecraft_backup_missing",
            "minecraft_trash_missing",
        }:
            status = 404
        elif exc.code in {"minecraft_upload_too_large", "minecraft_edit_too_large"}:
            status = 413
        elif exc.code in {
            "minecraft_file_changed",
            "minecraft_path_exists",
            "minecraft_upload_conflict",
            "minecraft_plugin_conflict",
            "minecraft_restore_conflict",
            "minecraft_etag_required",
        }:
            status = 409
        elif exc.code in {
            "minecraft_path_protected",
            "minecraft_path_outside_root",
            "minecraft_symlink_forbidden",
            "minecraft_download_forbidden",
        }:
            status = 403
        else:
            status = 400
        return web.json_response(
            {"error": exc.code, "message": str(exc)},
            status=status,
        )

    def minecraft_receipt(
        request: web.Request,
        principal: ConsensusWebPrincipal,
    ) -> tuple[tuple[int, str], dict[str, Any] | None]:
        receipt_key = str(request.headers.get("X-Idempotency-Key") or "").strip()
        if not 12 <= len(receipt_key) <= 120:
            raise MinecraftFilesError(
                "idempotency_key_required",
                "Для операции требуется уникальный идентификатор запроса.",
            )
        now = asyncio.get_running_loop().time()
        for key, (expires_at, _) in list(minecraft_receipts.items()):
            if expires_at <= now:
                minecraft_receipts.pop(key, None)
        receipt = (int(principal.user_id), receipt_key)
        cached = minecraft_receipts.get(receipt)
        return receipt, cached[1] if cached is not None else None

    def remember_minecraft_receipt(
        receipt: tuple[int, str],
        payload: dict[str, Any],
    ) -> None:
        minecraft_receipts[receipt] = (
            asyncio.get_running_loop().time() + 300.0,
            payload,
        )

    async def record_minecraft_action(
        principal: ConsensusWebPrincipal,
        action: str,
        *,
        target_type: str = "minecraft_server",
        target_id: str = "mc.tvr.lat",
        summary: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await asyncio.to_thread(
            activity_storage.bot_record_action,
            guild_id=int(guild_id),
            actor_id=int(principal.user_id),
            actor_display=str(principal.display_name),
            module="minecraft",
            action_kind=f"minecraft_{action}",
            target_type=target_type,
            target_id=target_id,
            summary=summary or f"Minecraft: выполнено действие {action}",
            payload={"source": "nuclear-reactor", **(payload or {})},
            reversible=False,
        )

    def viewer(principal: ConsensusWebPrincipal) -> dict[str, Any]:
        return {
            "id": int(principal.user_id),
            "name": str(principal.display_name),
            "administrator": bool(principal.administrator),
            "account_tier": str(principal.account_tier),
            "csrf_token": str(principal.csrf_token),
        }

    async def build_member_home(principal: ConsensusWebPrincipal) -> dict[str, Any]:
        (
            (profile, characters),
            notifications,
            layout,
            consensus,
            treasury,
            legislation,
        ) = await asyncio.gather(
            asyncio.to_thread(
                profile_storage.get_profile_snapshot,
                int(guild_id),
                int(principal.user_id),
            ),
            asyncio.to_thread(
                reactor_storage.reactor_list_notifications,
                int(guild_id),
                int(principal.user_id),
                limit=30,
            ),
            asyncio.to_thread(
                reactor_storage.reactor_get_layout,
                int(guild_id),
                int(principal.user_id),
                "member",
            ),
            asyncio.to_thread(_consensus_summary, int(guild_id)),
            asyncio.to_thread(
                _member_treasury_summary,
                int(guild_id),
            ),
            asyncio.to_thread(
                legislation_snapshot,
                int(guild_id),
                int(principal.user_id),
                # Keep the first paint compact; the complete registry is
                # fetched independently after the shell becomes interactive.
                limit=12,
                moderator=can_moderate_bills(principal),
            ),
        )
        positions = _member_positions(principal.member)
        profile_payload = asdict(profile) if profile is not None else None
        preferred_name = str(getattr(profile, "preferred_name", "") or "").strip()
        onboarding_required = bool(profile is not None and profile.directory_required)
        joined_at = getattr(principal.member, "joined_at", None)
        return {
            "profile": profile_payload,
            "characters": [asdict(character) for character in characters],
            "onboarding": {
                "required": onboarding_required,
                "completed": bool(getattr(profile, "onboarding_completed_at", None)),
                "has_character": bool(characters),
            },
            "mandate": {
                "preferred_name": preferred_name or None,
                "joined_at": joined_at.isoformat() if joined_at is not None else None,
                "nickname": _nickname_state(principal.member, profile, characters),
            },
            "legal_positions": positions,
            "legal_status": positions[0]["label"] if positions else "Прихожанин",
            "notifications": notifications,
            "layout": layout,
            "consensus": consensus,
            "treasury": treasury,
            "legislation": legislation,
            "links": {
                "consensus": "https://consensus.tvr.lat/",
                "admin": "https://reactor.tvr.lat/admin"
                if principal.administrator
                else None,
            },
        }

    async def member_home(request: web.Request) -> web.Response:
        principal = await personal_request(request)
        snapshot, cache_state = await member_home_cache.get(
            (int(principal.user_id), str(principal.csrf_token)),
            lambda: build_member_home(principal),
            force=request.query.get("fresh") == "1",
        )
        response = web.json_response(
            {
                "viewer": viewer(principal),
                **snapshot,
                "cache_state": cache_state,
            }
        )
        response.headers["X-T-Mod-Cache"] = cache_state
        return response

    async def _onboarding_command(request: web.Request) -> web.Response:
        principal = await personal_request(request)
        body = await json_body(request, principal)
        action = str(body.get("action") or "complete").strip().lower()
        if action not in {"complete", "save", "sync_nickname"}:
            return web.json_response(
                {"error": "onboarding_action_invalid", "message": "Неизвестное действие."},
                status=400,
            )
        profile, characters = await asyncio.to_thread(
            profile_storage.get_profile_snapshot,
            int(guild_id),
            int(principal.user_id),
        )
        if action != "sync_nickname":
            candidate_character = _primary_character(profile, characters)
            candidate_name = (
                str(candidate_character.nickname)
                if candidate_character is not None
                else str(body.get("character_nickname") or "")
            )
            candidate_static = (
                str(candidate_character.static_id)
                if candidate_character is not None
                else str(body.get("character_static") or "")
            )
            # Validate the exact Discord projection before committing the
            # profile, so an overlong identity cannot become a hidden failure.
            fellowship_discord_nickname(
                candidate_name,
                candidate_static,
                str(body.get("preferred_name") or ""),
            )
            profile, characters = await asyncio.to_thread(
                profile_storage.complete_member_onboarding,
                int(guild_id),
                int(principal.user_id),
                preferred_name=str(body.get("preferred_name") or ""),
                character_nickname=str(body.get("character_nickname") or ""),
                character_static=str(body.get("character_static") or ""),
                biography=str(body.get("biography") or ""),
                contribution=str(body.get("contribution") or ""),
                responsibilities=str(body.get("responsibilities") or ""),
                membership_since=str(body.get("membership_since") or "") or None,
            )

        nickname = _nickname_state(principal.member, profile, characters)
        if not nickname["exempt"] and nickname["expected"] and not nickname["synced"]:
            try:
                await asyncio.wait_for(
                    principal.member.edit(
                        nick=str(nickname["expected"]),
                        reason="T-Mod: завершение онбординга участника Товарищества",
                    ),
                    timeout=8.0,
                )
                nickname = _nickname_state(principal.member, profile, characters)
                # discord.py updates the object in normal operation, while
                # lightweight test doubles may not.
                nickname["current"] = str(nickname["expected"])
                nickname["synced"] = True
            except (discord.DiscordException, TimeoutError) as exc:
                nickname["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
                await log_technical_event(
                    bot,
                    principal.member.guild,
                    title="Не синхронизирован ник участника",
                    details=(
                        f"Участник: <@{principal.user_id}> (`{principal.user_id}`)\n"
                        f"Ожидалось: `{nickname['expected']}`\n"
                        f"Ошибка: `{nickname['error']}`"
                    ),
                    dedupe_key=f"member-nickname-sync:{principal.user_id}",
                    cooldown_seconds=1800,
                )
        member_home_cache.invalidate()
        return web.json_response(
            {
                "ok": True,
                "profile": asdict(profile) if profile is not None else None,
                "characters": [asdict(character) for character in characters],
                "nickname": nickname,
            }
        )

    async def onboarding_command(request: web.Request) -> web.Response:
        try:
            return await _onboarding_command(request)
        except ValueError as exc:
            code = str(exc)
            messages = {
                "profile_preferred_name_invalid": "Укажите имя для обращения: от 2 до 24 символов без знака |.",
                "profile_nickname_invalid": "Имя персонажа должно содержать от 2 до 48 символов.",
                "profile_character_full_name_required": "Укажите имя и фамилию персонажа через пробел.",
                "profile_static_invalid": "Статик должен состоять из 1–12 цифр.",
                "profile_static_taken": "Этот статик уже закреплён за другим персонажем.",
                "profile_character_conflict": "Не удалось закрепить персонажа из-за конфликта данных.",
                "profile_discord_nickname_too_long": "Итоговый ник Discord длиннее 32 символов. Сократите фамилию или имя для обращения.",
                "profile_biography_invalid": "Расскажите о себе в 3–500 символах.",
                "profile_contribution_invalid": "Опишите деятельность в 3–500 символах.",
                "profile_responsibilities_invalid": "Укажите зону ответственности в 3–700 символах.",
                "profile_membership_since_invalid": "Проверьте дату вступления.",
            }
            return web.json_response(
                {"error": code, "message": messages.get(code, "Проверьте данные мандата.")},
                status=409 if code in {"profile_static_taken", "profile_character_conflict"} else 400,
            )

    async def legislation_get(request: web.Request) -> web.Response:
        principal = await personal_request(request)
        snapshot, cache_state = await legislation_cache.get(
            int(principal.user_id),
            lambda: asyncio.to_thread(
                legislation_snapshot,
                int(guild_id),
                int(principal.user_id),
                limit=120,
                moderator=can_moderate_bills(principal),
            ),
            force=request.query.get("fresh") == "1",
        )
        response = web.json_response(
            {
                "viewer": viewer(principal),
                **snapshot,
                "cache_state": cache_state,
            }
        )
        response.headers["X-T-Mod-Cache"] = cache_state
        return response

    async def preparation_get(request: web.Request) -> web.Response:
        """Project only upcoming bills and this member's private readiness.

        The full sheet remains behind the existing per-bill preparation API.
        Keeping this small list out of the home snapshot makes the Reactor's
        first paint fast while never exposing a person's notes or draft vote.
        """

        principal = await personal_request(request)
        payload = await asyncio.to_thread(
            build_reactor_preparation_payload,
            int(guild_id),
            int(principal.user_id),
        )
        response = web.json_response(
            payload
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    async def legislation_command(request: web.Request) -> web.Response:
        principal = await personal_request(request)
        body = await json_body(request, principal)
        action = str(body.get("action") or "").strip().lower()
        actor_profile = await asyncio.to_thread(
            profile_storage.get_member_profile,
            int(guild_id),
            int(principal.user_id),
        )
        actor_display = str(
            getattr(actor_profile, "preferred_name", "")
            or principal.display_name
        )
        try:
            if action == "create":
                workspace, created = await asyncio.to_thread(
                    create_workspace,
                    int(guild_id),
                    int(principal.user_id),
                    actor_display,
                )
                member_home_cache.invalidate()
                legislation_cache.invalidate(int(principal.user_id))
                return web.json_response(
                    {"ok": True, "created": created, "workspace": workspace}
                )
            if action == "save":
                workspace = await asyncio.to_thread(
                    save_workspace,
                    int(guild_id),
                    int(principal.user_id),
                    body,
                )
                raw = await asyncio.to_thread(
                    workspace_storage.get_bill_workspace,
                    int(workspace["id"]),
                )
                if raw is not None:
                    try:
                        await refresh_bill_workspace_panel(bot, raw)
                    except Exception:  # noqa: BLE001 - draft is already durable
                        logger.exception("Could not refresh Discord bill editor panel")
                member_home_cache.invalidate()
                legislation_cache.invalidate(int(principal.user_id))
                return web.json_response({"ok": True, "workspace": workspace})
            if action == "ai":
                try:
                    workspace, clarification = await asyncio.to_thread(
                        generate_workspace_draft,
                        int(guild_id),
                        int(principal.user_id),
                        body,
                    )
                except (RuntimeError, ValueError) as exc:
                    if isinstance(exc, ReactorLegislationError):
                        raise
                    current = await asyncio.to_thread(
                        legislation_snapshot,
                        int(guild_id),
                        int(principal.user_id),
                        limit=1,
                        moderator=can_moderate_bills(principal),
                    )
                    code = str(exc)
                    message = (
                        "ИИ-редактор пока не настроен. Черновик сохранён, продолжите вручную."
                        if code == "bill_editor_ai_not_configured"
                        else "ИИ не смог подготовить корректный текст. Черновик сохранён — попробуйте ещё раз."
                    )
                    member_home_cache.invalidate()
                    legislation_cache.invalidate(int(principal.user_id))
                    return web.json_response(
                        {
                            "error": code,
                            "message": message,
                            "workspace": current.get("workspace"),
                        },
                        status=503,
                    )
                raw = await asyncio.to_thread(
                    workspace_storage.get_bill_workspace,
                    int(workspace["id"]),
                )
                if raw is not None:
                    try:
                        await refresh_bill_workspace_panel(bot, raw)
                    except Exception:  # noqa: BLE001 - draft is already durable
                        logger.exception("Could not refresh Discord bill editor panel")
                member_home_cache.invalidate()
                legislation_cache.invalidate(int(principal.user_id))
                return web.json_response(
                    {
                        "ok": True,
                        "workspace": workspace,
                        "clarification": clarification,
                    }
                )
            if action == "publish":
                workspace, created = await publish_workspace(
                    bot,
                    int(guild_id),
                    int(principal.user_id),
                    actor_display,
                    body,
                )
                member_home_cache.invalidate()
                legislation_cache.invalidate()
                return web.json_response(
                    {
                        "ok": True,
                        "created": created,
                        "workspace": workspace,
                        "message": "Законопроект передан на модерацию.",
                    }
                )
            if action == "moderate":
                if not can_moderate_bills(principal):
                    return web.json_response(
                        {
                            "error": "bill_moderator_required",
                            "message": "Решение может принять только руководство Товарищества.",
                        },
                        status=403,
                    )
                original = await asyncio.to_thread(
                    workspace_storage.get_bill_workspace,
                    int(body.get("workspace_id") or 0),
                )
                workspace = await moderate_workspace(
                    bot,
                    int(guild_id),
                    int(principal.user_id),
                    actor_display,
                    body,
                )
                if original is not None:
                    moderation_state = str(
                        (workspace.get("moderation") or {}).get("status") or ""
                    )
                    title = {
                        "approved": "Законопроект одобрен",
                        "changes_requested": "Законопроект нужно дополнить",
                        "rejected": "Законопроект отклонён",
                    }.get(moderation_state, "Решение по законопроекту")
                    await asyncio.to_thread(
                        reactor_storage.reactor_put_notification,
                        guild_id=int(guild_id),
                        user_id=int(original.get("author_id") or 0),
                        severity=(
                            "success"
                            if moderation_state == "approved"
                            else "warning"
                        ),
                        kind="bill_moderation",
                        title=title,
                        body=str(
                            (workspace.get("moderation") or {}).get("note")
                            or workspace.get("title")
                            or "Откройте личный портфель, чтобы увидеть решение."
                        ),
                        dedupe_key=(
                            f"bill-moderation:{int(workspace['id'])}:"
                            f"{int((workspace.get('moderation') or {}).get('round') or 0)}:"
                            f"{moderation_state}"
                        ),
                        route="#my-bills",
                    )
                    guild = bot.get_guild(int(guild_id))
                    if guild is not None:
                        asyncio.create_task(
                            log_technical_event(
                                bot,
                                guild,
                                title="Модерация законопроекта",
                                details=(
                                    f"Проект: **{workspace.get('title') or 'Без названия'}**\n"
                                    f"Решение: **{moderation_state or 'обновлено'}**\n"
                                    f"Модератор: **{actor_display}** (`{int(principal.user_id)}`)\n"
                                    f"Комментарий: {str((workspace.get('moderation') or {}).get('note') or '—')[:1200]}"
                                ),
                                level="info",
                                dedupe_key=(
                                    f"bill-moderation:{int(workspace['id'])}:"
                                    f"{int((workspace.get('moderation') or {}).get('round') or 0)}"
                                ),
                                cooldown_seconds=0,
                                component="legislation",
                            ),
                            name=f"bill-moderation-audit-{int(workspace['id'])}",
                        )
                member_home_cache.invalidate()
                legislation_cache.invalidate()
                return web.json_response({"ok": True, "workspace": workspace})
            if action == "cancel":
                await asyncio.to_thread(
                    cancel_workspace,
                    int(guild_id),
                    int(principal.user_id),
                    body,
                )
                member_home_cache.invalidate()
                legislation_cache.invalidate(int(principal.user_id))
                return web.json_response({"ok": True})
            return web.json_response(
                {
                    "error": "bill_action_invalid",
                    "message": "Неизвестное действие редактора.",
                },
                status=400,
            )
        except ReactorLegislationError as exc:
            snapshot = await asyncio.to_thread(
                legislation_snapshot,
                int(guild_id),
                int(principal.user_id),
                limit=1,
                moderator=can_moderate_bills(principal),
            )
            return web.json_response(
                {
                    "error": exc.code,
                    "message": str(exc),
                    "workspace": snapshot.get("workspace"),
                },
                status=exc.status,
            )
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {
                    "error": str(exc),
                    "message": "Проверьте данные и актуальность карточки.",
                },
                status=409 if "revision" in str(exc) else 400,
            )

    async def ovr_get(request: web.Request) -> web.Response:
        principal = await personal_request(request)
        if not await has_ovr_access(principal):
            return web.json_response(
                {
                    "error": "ovr_access_required",
                    "message": "Доступ к порталу ОВР выдаётся администратором вручную.",
                },
                status=403,
            )
        requested_case_id = str(request.query.get("case_id") or "").strip()
        if requested_case_id:
            if not requested_case_id.isdigit():
                return web.json_response(
                    {"error": "ovr_case_id_invalid", "message": "Неверный номер расследования."},
                    status=400,
                )
            try:
                detail = await asyncio.to_thread(
                    ovr_storage.case_detail,
                    int(requested_case_id),
                    guild_id=int(guild_id),
                )
                detail["admission"] = await asyncio.to_thread(
                    admission_storage.application_for_case,
                    int(requested_case_id),
                    guild_id=int(guild_id),
                )
            except ValueError as exc:
                return web.json_response(
                    {"error": str(exc), "message": "Расследование не найдено."},
                    status=404,
                )
            return web.json_response(
                {"viewer": viewer(principal), "full_access": True, "detail": detail}
            )
        cases = await asyncio.to_thread(
            ovr_storage.list_cases,
            int(guild_id),
            actor_id=int(principal.user_id),
            full_access=True,
        )
        return web.json_response(
            {
                "viewer": viewer(principal),
                "full_access": True,
                "cases": cases,
            }
        )

    async def ovr_command(request: web.Request) -> web.Response:
        principal = await personal_request(request)
        if not await has_ovr_access(principal):
            return web.json_response(
                {
                    "error": "ovr_access_required",
                    "message": "Доступ к порталу ОВР выдаётся администратором вручную.",
                },
                status=403,
            )
        body = await json_body(request, principal)
        action = str(body.get("action") or "").strip().lower()
        actor_profile = await asyncio.to_thread(
            profile_storage.get_member_profile,
            int(guild_id),
            int(principal.user_id),
        )
        actor_display = str(
            getattr(actor_profile, "preferred_name", "") or principal.display_name
        )

        async def enriched(detail: dict[str, Any]) -> dict[str, Any]:
            selected_case = detail.get("case") or {}
            selected_case_id = int(selected_case.get("id") or 0)
            if selected_case_id > 0:
                detail["admission"] = await asyncio.to_thread(
                    admission_storage.application_for_case,
                    selected_case_id,
                    guild_id=int(guild_id),
                )
            return detail

        def audit_ovr(
            *,
            selected_action: str,
            selected_case_id: int,
            revision: int = 0,
            extra: str = "",
            level: str = "info",
        ) -> None:
            guild = bot.get_guild(int(guild_id))
            if guild is None:
                return
            asyncio.create_task(
                log_technical_event(
                    bot,
                    guild,
                    title="ОВР · действие в расследовании",
                    details=(
                        f"Дело: **{int(selected_case_id)}**\n"
                        f"Действие: **{selected_action}**\n"
                        f"Сотрудник: **{actor_display}** (`{int(principal.user_id)}`)"
                        + (f"\n{extra}" if extra else "")
                    ),
                    level=level,
                    dedupe_key=(
                        f"ovr:{int(selected_case_id)}:{selected_action}:"
                        f"{int(revision)}"
                    ),
                    cooldown_seconds=0,
                    component="ovr",
                ),
                name=f"ovr-audit-{int(selected_case_id)}-{selected_action}",
            )
        try:
            if action == "create":
                case = await asyncio.to_thread(
                    ovr_storage.create_case,
                    guild_id=int(guild_id),
                    first_name=str(body.get("first_name") or ""),
                    last_name=str(body.get("last_name") or ""),
                    static_id=str(body.get("static_id") or ""),
                    discord_text=str(body.get("discord_text") or ""),
                    discord_user_id=(
                        int(body["discord_user_id"])
                        if str(body.get("discord_user_id") or "").isdigit()
                        else None
                    ),
                    forum_url=str(body.get("forum_url") or "") or None,
                    additional_info=str(body.get("additional_info") or ""),
                    case_kind=str(body.get("case_kind") or "admission"),
                    priority=str(body.get("priority") or "normal"),
                    classification=str(body.get("classification") or "restricted"),
                    objective=str(body.get("objective") or ""),
                    actor_id=int(principal.user_id),
                    actor_display=actor_display,
                )
                audit_ovr(
                    selected_action="create",
                    selected_case_id=int(case["id"]),
                    revision=int(case.get("revision") or 1),
                    extra=f"Кандидат: **{case.get('first_name')} {case.get('last_name')}**",
                )
                return web.json_response({"ok": True, "case": case})
            case_id = int(body.get("case_id") or 0)
            expected_revision = int(body.get("expected_revision") or 0)
            common = {
                "guild_id": int(guild_id),
                "expected_revision": expected_revision,
                "actor_id": int(principal.user_id),
                "actor_display": actor_display,
            }
            if action == "material_add":
                detail = await asyncio.to_thread(
                    ovr_storage.add_material,
                    case_id,
                    **common,
                    kind=str(body.get("kind") or "document"),
                    title=str(body.get("title") or ""),
                    content=str(body.get("content") or ""),
                    source_url=str(body.get("source_url") or ""),
                    reliability=str(body.get("reliability") or "unrated"),
                )
                detail = await enriched(detail)
                audit_ovr(selected_action=action, selected_case_id=case_id, revision=int(detail["case"]["revision"]))
                return web.json_response({"ok": True, "detail": detail})
            if action == "material_status":
                detail = await asyncio.to_thread(
                    ovr_storage.set_material_status,
                    case_id,
                    **common,
                    material_id=int(body.get("material_id") or 0),
                    status=str(body.get("status") or "new"),
                )
                detail = await enriched(detail)
                audit_ovr(selected_action=action, selected_case_id=case_id, revision=int(detail["case"]["revision"]))
                return web.json_response({"ok": True, "detail": detail})
            if action == "relation_add":
                detail = await asyncio.to_thread(
                    ovr_storage.add_relation,
                    case_id,
                    **common,
                    person_name=str(body.get("person_name") or ""),
                    relation_type=str(body.get("relation_type") or ""),
                    static_id=str(body.get("static_id") or ""),
                    discord_text=str(body.get("discord_text") or ""),
                    details=str(body.get("details") or ""),
                    confidence=str(body.get("confidence") or "unrated"),
                )
                detail = await enriched(detail)
                audit_ovr(selected_action=action, selected_case_id=case_id, revision=int(detail["case"]["revision"]))
                return web.json_response({"ok": True, "detail": detail})
            if action == "task_add":
                assignee_id = (
                    int(body["assignee_id"])
                    if str(body.get("assignee_id") or "").isdigit()
                    else None
                )
                detail = await asyncio.to_thread(
                    ovr_storage.add_task,
                    case_id,
                    **common,
                    title=str(body.get("title") or ""),
                    description=str(body.get("description") or ""),
                    priority=str(body.get("priority") or "normal"),
                    assignee_id=assignee_id,
                    assignee_display=str(body.get("assignee_display") or ""),
                    due_at=str(body.get("due_at") or ""),
                )
                detail = await enriched(detail)
                audit_ovr(selected_action=action, selected_case_id=case_id, revision=int(detail["case"]["revision"]))
                return web.json_response({"ok": True, "detail": detail})
            if action == "task_status":
                detail = await asyncio.to_thread(
                    ovr_storage.set_task_status,
                    case_id,
                    **common,
                    task_id=int(body.get("task_id") or 0),
                    status=str(body.get("status") or "todo"),
                )
                detail = await enriched(detail)
                audit_ovr(selected_action=action, selected_case_id=case_id, revision=int(detail["case"]["revision"]))
                return web.json_response({"ok": True, "detail": detail})
            case = await asyncio.to_thread(
                ovr_storage.update_case,
                case_id,
                **common,
                action=action,
                note=str(body.get("note") or ""),
                findings=(
                    str(body.get("findings")) if "findings" in body else None
                ),
                nowa_links=(
                    str(body.get("nowa_links")) if "nowa_links" in body else None
                ),
                risk_level=(
                    str(body.get("risk_level")) if "risk_level" in body else None
                ),
                objective=(str(body.get("objective")) if "objective" in body else None),
                executive_summary=(
                    str(body.get("executive_summary"))
                    if "executive_summary" in body else None
                ),
                hypothesis=(str(body.get("hypothesis")) if "hypothesis" in body else None),
                aliases=(str(body.get("aliases")) if "aliases" in body else None),
                affiliations=(
                    str(body.get("affiliations")) if "affiliations" in body else None
                ),
                priority=(str(body.get("priority")) if "priority" in body else None),
                classification=(
                    str(body.get("classification"))
                    if "classification" in body else None
                ),
            )
            pipeline_sync = "not_applicable"
            if action in {"approve", "deny"}:
                guild = bot.get_guild(int(guild_id))
                if guild is None:
                    pipeline_sync = "pending"
                else:
                    try:
                        application = await process_ovr_decision(
                            bot,
                            guild,
                            case_id=case_id,
                            approved=action == "approve",
                            actor_id=int(principal.user_id),
                            actor_display=actor_display,
                            note=str(body.get("note") or ""),
                        )
                        pipeline_sync = "complete" if application is not None else "not_applicable"
                    except Exception as exc:  # noqa: BLE001 - OVR decision is already durable
                        pipeline_sync = "pending"
                        await log_technical_event(
                            bot,
                            guild,
                            title="Phoenix · решение ОВР ожидает синхронизации",
                            details=(
                                f"Дело: **{case_id}**\n"
                                f"Решение ОВР сохранено. Цепочка вступления будет "
                                f"повторена автоматически.\n"
                                f"Ошибка: `{type(exc).__name__}: {str(exc)[:900]}`"
                            ),
                            dedupe_key=f"admission-ovr-sync:{case_id}",
                            cooldown_seconds=300,
                            exception=exc,
                            component="admission",
                        )
            detail = await asyncio.to_thread(
                ovr_storage.case_detail,
                int(case["id"]),
                guild_id=int(guild_id),
            )
            detail = await enriched(detail)
            audit_ovr(
                selected_action=action,
                selected_case_id=case_id,
                revision=int(case.get("revision") or 0),
                extra=(
                    f"Решение: **{'допущен' if action == 'approve' else 'не допущен'}**\n"
                    f"Синхронизация Phoenix: **{pipeline_sync}**"
                    if action in {"approve", "deny"}
                    else ""
                ),
            )
            return web.json_response(
                {
                    "ok": True,
                    "case": case,
                    "detail": detail,
                    "pipeline_sync": pipeline_sync,
                }
            )
        except (TypeError, ValueError) as exc:
            code = str(exc)
            messages = {
                "ovr_case_revision_conflict": "Карточка уже изменилась у другого сотрудника. Обновите расследование.",
                "ovr_case_closed": "Завершённое расследование сначала необходимо открыть повторно.",
                "ovr_note_required": "Добавьте служебное обоснование действия.",
                "ovr_material_empty": "Добавьте описание материала или ссылку на источник.",
                "ovr_case_not_found": "Расследование не найдено.",
                "ovr_case_transition_invalid": "Этот переход недоступен на текущем этапе расследования.",
            }
            return web.json_response(
                {
                    "error": code,
                    "message": messages.get(code, "Проверьте заполнение полей расследования."),
                },
                status=409 if "revision" in code or code in {"ovr_case_closed"} else 400,
            )

    async def ovr_report(request: web.Request) -> web.Response:
        principal = await personal_request(request)
        if not await has_ovr_access(principal):
            return web.json_response(
                {
                    "error": "ovr_access_required",
                    "message": "Доступ к порталу ОВР выдаётся администратором вручную.",
                },
                status=403,
            )
        body = await json_body(request, principal)
        raw_case_id = str(request.match_info.get("case_id") or "").strip()
        if not raw_case_id.isdigit():
            return web.json_response(
                {"error": "ovr_case_id_invalid", "message": "Неверный номер расследования."},
                status=400,
            )
        password = str(body.get("password") or "")
        confirmation = str(body.get("password_confirmation") or "")
        if password != confirmation:
            return web.json_response(
                {
                    "error": "ovr_report_password_mismatch",
                    "message": "Пароли отчёта не совпадают.",
                },
                status=400,
            )
        if len(password) < 8 or len(password) > 128:
            return web.json_response(
                {
                    "error": "ovr_report_password_invalid",
                    "message": "Пароль отчёта должен содержать от 8 до 128 символов.",
                },
                status=400,
            )
        try:
            detail = await asyncio.to_thread(
                ovr_storage.case_detail,
                int(raw_case_id),
                guild_id=int(guild_id),
            )

            def build_encrypted_report() -> tuple[bytes, str]:
                case = detail["case"]
                filename = f"ovr-{int(case.get('case_number') or 0):03d}-dossier.pdf"
                with tempfile.TemporaryDirectory(prefix="tmod-ovr-report-") as directory:
                    temporary = Path(directory)
                    plain = temporary / "report.pdf"
                    encrypted = temporary / filename
                    generate_investigation_report(
                        detail,
                        destination=plain,
                        force=True,
                    )
                    encrypt_investigation_report(plain, encrypted, password)
                    payload = encrypted.read_bytes()
                    if not payload:
                        raise OSError("ovr_report_empty")
                    return payload, filename

            payload, filename = await asyncio.to_thread(build_encrypted_report)
        except ValueError as exc:
            code = str(exc)
            return web.json_response(
                {
                    "error": code,
                    "message": (
                        "Расследование не найдено."
                        if code == "ovr_case_not_found"
                        else "Не удалось проверить параметры защищённого отчёта."
                    ),
                },
                status=404 if code == "ovr_case_not_found" else 400,
            )
        except Exception:
            logger.exception(
                "OVR PDF report generation failed for case %s",
                raw_case_id,
            )
            return web.json_response(
                {
                    "error": "ovr_report_generation_failed",
                    "message": "Не удалось сформировать защищённый отчёт. Попробуйте ещё раз.",
                },
                status=500,
            )

        response = web.Response(body=payload, content_type="application/pdf")
        response.headers["Content-Disposition"] = (
            f"attachment; filename*=UTF-8''{quote(filename)}"
        )
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def preferences(request: web.Request) -> web.Response:
        principal = await personal_request(request)
        body = await json_body(request, principal)
        surface = str(body.get("surface") or "member")
        if surface == "admin" and not principal.administrator:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "administrator_required"}),
                content_type="application/json",
            )
        try:
            layout = await asyncio.to_thread(
                reactor_storage.reactor_set_layout,
                int(guild_id),
                int(principal.user_id),
                surface,
                body.get("layout") if isinstance(body.get("layout"), list) else [],
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        member_home_cache.invalidate()
        return web.json_response({"ok": True, "layout": layout})

    async def notifications(request: web.Request) -> web.Response:
        principal = await personal_request(request)
        payload = await asyncio.to_thread(
            reactor_storage.reactor_list_notifications,
            int(guild_id),
            int(principal.user_id),
            unread_only=request.query.get("unread") == "1",
            limit=100,
        )
        return web.json_response({"viewer": viewer(principal), **payload})

    async def desktop_bootstrap(request: web.Request) -> web.Response:
        """Return the small, stable projection consumed by T-Mod Desktop.

        The desktop shell deliberately receives neither the session cookie nor
        Discord credentials.  Electron keeps the shared HttpOnly cookie in its
        persistent network partition and calls this endpoint on behalf of the
        local shell.
        """

        principal, legacy = await authenticate(request)
        if legacy or principal is None:
            return web.json_response(
                {
                    "error": "desktop_login_required",
                    "login_url": "https://tvr.lat/login?next=/reactor",
                },
                status=401,
            )

        try:
            payload = await build_desktop_bootstrap_payload(
                headers=request.headers,
                principal=principal,
                guild_id=int(guild_id),
            )
        except DesktopBootstrapError as exc:
            return web.json_response(
                {"error": exc.error, "message": exc.message},
                status=exc.status,
                headers={"Cache-Control": "private, no-store"},
            )
        return web.json_response(
            payload,
            headers={"Cache-Control": "private, no-store"},
        )

    async def read_notifications(request: web.Request) -> web.Response:
        principal = await personal_request(request)
        body = await json_body(request, principal)
        raw_ids = body.get("ids")
        ids = raw_ids if isinstance(raw_ids, list) else None
        updated = await asyncio.to_thread(
            reactor_storage.reactor_mark_notifications_read,
            int(guild_id),
            int(principal.user_id),
            ids,
        )
        member_home_cache.invalidate()
        return web.json_response({"ok": True, "updated": updated})

    async def attention(request: web.Request) -> web.Response:
        principal = await admin_request(request)
        cached, cache_state = await cached_attention(
            force=request.query.get("fresh") == "1",
        )
        # Notification projection and personal layout are request-specific; never
        # mutate the shared snapshot kept in the fast cache.
        payload = {
            **cached,
            "items": [dict(item) for item in cached.get("items", ())],
            "cache_state": cache_state,
        }
        user_id = int(principal.user_id)
        projection_signature = json.dumps(
            [
                (
                    item.get("key"),
                    item.get("severity"),
                    item.get("title"),
                    item.get("detail"),
                    item.get("route"),
                )
                for item in payload["items"]
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if attention_projection_signatures.get(user_id) != projection_signature:
            async with attention_projection_lock:
                if attention_projection_signatures.get(user_id) != projection_signature:
                    await asyncio.to_thread(
                        reactor_storage.reactor_sync_notifications,
                        int(guild_id),
                        user_id,
                        "attention",
                        [
                            {
                                "severity": str(item["severity"]),
                                "title": str(item["title"]),
                                "body": str(item["detail"]),
                                "route": str(item["route"]),
                                "dedupe_key": str(item["key"]),
                            }
                            for item in payload["items"]
                        ],
                    )
                    attention_projection_signatures[user_id] = projection_signature
        payload["layout"] = await asyncio.to_thread(
            reactor_storage.reactor_get_layout,
            int(guild_id),
            int(principal.user_id),
            "admin",
        )
        payload["viewer"] = viewer(principal)
        response = web.json_response(payload)
        response.headers["X-T-Mod-Cache"] = cache_state
        return response

    async def health(request: web.Request) -> web.Response:
        principal = await admin_request(request)
        snapshot, cache_state = await cached_health(
            force=request.query.get("fresh") == "1",
        )
        response = web.json_response(
            {
                "viewer": viewer(principal),
                **snapshot,
                "cache_state": cache_state,
            }
        )
        response.headers["X-T-Mod-Cache"] = cache_state
        return response

    async def search(request: web.Request) -> web.Response:
        principal = await admin_request(request)
        query = str(request.query.get("q") or "").strip()
        items = await asyncio.to_thread(
            reactor_storage.reactor_global_search,
            int(guild_id),
            query,
            limit=40,
        )
        return web.json_response(
            {"viewer": viewer(principal), "query": query, "items": items}
        )

    async def events(request: web.Request) -> web.Response:
        principal = await admin_request(request)
        try:
            after_id = max(0, int(request.query.get("after") or 0))
        except (TypeError, ValueError):
            after_id = 0
        items = await asyncio.to_thread(
            reactor_storage.reactor_event_feed,
            int(guild_id),
            after_id=after_id,
            limit=60,
        )
        return web.json_response({"viewer": viewer(principal), "items": items})

    async def event_stream(request: web.Request) -> web.StreamResponse:
        await admin_request(request)
        try:
            cursor = max(0, int(request.query.get("after") or 0))
        except (TypeError, ValueError):
            cursor = 0
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        try:
            await response.write(b"retry: 3000\n\n")
            for tick in range(1800):
                items = await asyncio.to_thread(
                    reactor_storage.reactor_event_feed,
                    int(guild_id),
                    after_id=cursor,
                    limit=30,
                )
                for item in items:
                    cursor = max(cursor, int(item.get("id") or 0))
                    encoded = json.dumps(
                        item, ensure_ascii=False, separators=(",", ":")
                    )
                    await response.write(
                        f"id: {cursor}\nevent: activity\ndata: {encoded}\n\n".encode()
                    )
                if not items and tick % 10 == 0:
                    await response.write(b": heartbeat\n\n")
                await asyncio.sleep(2)
        except (
            asyncio.CancelledError,
            ConnectionError,
            ConnectionResetError,
            RuntimeError,
        ):
            pass
        return response

    async def minecraft_get(request: web.Request) -> web.Response:
        principal = await admin_request(request)
        status, cache_state = await cached_minecraft(
            force=request.query.get("fresh") == "1",
        )
        return web.json_response(
            {
                "viewer": viewer(principal),
                **status,
                "cache_state": cache_state,
            }
        )

    async def minecraft_files_get(request: web.Request) -> web.Response:
        principal = await admin_request(request)
        try:
            listing, overview = await asyncio.gather(
                asyncio.to_thread(
                    minecraft_list_directory,
                    request.query.get("path", ""),
                ),
                asyncio.to_thread(minecraft_storage_overview),
            )
        except (MinecraftFilesError, OSError) as exc:
            return minecraft_error(exc)
        return web.json_response(
            {"viewer": viewer(principal), **listing, "storage": overview}
        )

    async def minecraft_file_get(request: web.Request) -> web.Response:
        principal = await admin_request(request)
        try:
            item = await asyncio.to_thread(
                minecraft_read_text,
                request.query.get("path", ""),
            )
        except (MinecraftFilesError, OSError) as exc:
            return minecraft_error(exc)
        return web.json_response({"viewer": viewer(principal), "item": item})

    async def minecraft_file_download(request: web.Request) -> web.StreamResponse:
        await admin_request(request)
        try:
            path = await asyncio.to_thread(
                minecraft_download_path,
                request.query.get("path", ""),
            )
        except (MinecraftFilesError, OSError) as exc:
            return minecraft_error(exc)
        response = web.FileResponse(path)
        response.headers["Content-Disposition"] = (
            f"attachment; filename*=UTF-8''{quote(path.name)}"
        )
        return response

    async def minecraft_file_action(request: web.Request) -> web.Response:
        principal = await admin_request(request)
        body = await json_body(request, principal)
        try:
            receipt, cached = minecraft_receipt(request, principal)
            if cached is not None:
                return web.json_response(cached)
            action = str(body.get("action") or "").strip().lower()
            target_id = str(body.get("path") or body.get("trash_id") or "minecraft")
            if action == "save":
                item = await asyncio.to_thread(
                    minecraft_write_text,
                    body.get("path"),
                    body.get("content"),
                    expected_etag=str(body.get("etag") or "") or None,
                )
            elif action == "mkdir":
                item = await asyncio.to_thread(
                    minecraft_make_directory,
                    body.get("path"),
                    body.get("name"),
                )
            elif action == "rename":
                item = await asyncio.to_thread(
                    minecraft_rename_path,
                    body.get("path"),
                    body.get("name"),
                )
            elif action == "trash":
                if body.get("confirmed") is not True:
                    return web.json_response(
                        {"error": "confirmation_required"},
                        status=409,
                    )
                item = await asyncio.to_thread(
                    minecraft_trash_path,
                    body.get("path"),
                )
            elif action == "restore_trash":
                if body.get("confirmed") is not True:
                    return web.json_response(
                        {"error": "confirmation_required"},
                        status=409,
                    )
                item = await asyncio.to_thread(
                    minecraft_restore_trash,
                    body.get("trash_id"),
                )
            elif action == "plugin_state":
                if body.get("confirmed") is not True:
                    return web.json_response(
                        {"error": "confirmation_required"},
                        status=409,
                    )
                item = await asyncio.to_thread(
                    minecraft_set_plugin_state,
                    body.get("path"),
                    bool(body.get("enabled")),
                )
            else:
                raise MinecraftFilesError(
                    "minecraft_file_action_invalid",
                    "Неизвестное действие файлового менеджера.",
                )
        except (MinecraftFilesError, OSError) as exc:
            return minecraft_error(exc)
        result = {"ok": True, "action": action, "item": item}
        await record_minecraft_action(
            principal,
            f"file_{action}",
            target_type="minecraft_file",
            target_id=target_id,
            summary=f"Minecraft-файлы: {action} · {target_id}",
        )
        remember_minecraft_receipt(receipt, result)
        return web.json_response(result)

    async def minecraft_upload(request: web.Request) -> web.Response:
        principal = await admin_request(request)
        if not csrf_matches(request, principal):
            return web.json_response({"error": "csrf_failed"}, status=403)
        temporary = None
        try:
            receipt, cached = minecraft_receipt(request, principal)
            if cached is not None:
                return web.json_response(cached)
            if not request.content_type.startswith("multipart/"):
                raise MinecraftFilesError(
                    "minecraft_upload_multipart_required",
                    "Ожидается multipart-загрузка файла.",
                )
            config = minecraft_files_config()
            reader = await request.multipart()
            directory = ""
            overwrite = False
            filename = ""
            size = 0
            temporary = await asyncio.to_thread(minecraft_prepare_upload)
            handle = await asyncio.to_thread(temporary.open, "wb")
            try:
                async for field in reader:
                    if field.name == "path":
                        directory = (await field.text()).strip()
                    elif field.name == "overwrite":
                        overwrite = (await field.text()).strip().lower() in {
                            "1",
                            "true",
                            "yes",
                        }
                    elif field.name == "file":
                        if filename:
                            raise MinecraftFilesError(
                                "minecraft_upload_single_file",
                                "Загружайте файлы по одному.",
                            )
                        filename = str(field.filename or "")
                        while chunk := await field.read_chunk(size=256 * 1024):
                            size += len(chunk)
                            if size > config.max_upload_bytes:
                                raise MinecraftFilesError(
                                    "minecraft_upload_too_large",
                                    "Файл превышает лимит загрузки.",
                                )
                            await asyncio.to_thread(handle.write, chunk)
            finally:
                await asyncio.to_thread(handle.close)
            if not filename:
                raise MinecraftFilesError(
                    "minecraft_upload_file_required",
                    "Выберите файл для загрузки.",
                )
            item = await asyncio.to_thread(
                minecraft_finish_upload,
                directory,
                filename,
                temporary,
                size,
                overwrite=overwrite,
            )
            temporary = None
        except (MinecraftFilesError, OSError) as exc:
            return minecraft_error(exc)
        finally:
            if temporary is not None:
                await asyncio.to_thread(temporary.unlink, missing_ok=True)
        result = {"ok": True, "action": "upload", "item": item}
        await record_minecraft_action(
            principal,
            "file_upload",
            target_type="minecraft_file",
            target_id=str(item.get("path") or filename),
            summary=f"Minecraft-файлы: загружен {item.get('path') or filename}",
            payload={"size": size},
        )
        remember_minecraft_receipt(receipt, result)
        return web.json_response(result)

    async def minecraft_plugins_get(request: web.Request) -> web.Response:
        principal = await admin_request(request)
        try:
            items = await asyncio.to_thread(minecraft_list_plugins)
        except (MinecraftFilesError, OSError) as exc:
            return minecraft_error(exc)
        return web.json_response({"viewer": viewer(principal), "items": items})

    async def minecraft_backups_get(request: web.Request) -> web.Response:
        principal = await admin_request(request)
        try:
            backups, trash = await asyncio.gather(
                asyncio.to_thread(minecraft_list_backups),
                asyncio.to_thread(minecraft_list_trash),
            )
        except (MinecraftFilesError, OSError) as exc:
            return minecraft_error(exc)
        return web.json_response(
            {"viewer": viewer(principal), "items": backups, "trash": trash}
        )

    async def minecraft_backup_download(request: web.Request) -> web.StreamResponse:
        await admin_request(request)
        try:
            path = await asyncio.to_thread(
                minecraft_backup_path,
                request.query.get("id", ""),
            )
        except (MinecraftFilesError, OSError) as exc:
            return minecraft_error(exc)
        response = web.FileResponse(path)
        response.headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
        return response

    async def minecraft_backup_action(request: web.Request) -> web.Response:
        principal = await admin_request(request)
        body = await json_body(request, principal)
        try:
            receipt, cached = minecraft_receipt(request, principal)
            if cached is not None:
                return web.json_response(cached)
            action = str(body.get("action") or "").strip().lower()
            if action == "create":
                status = await asyncio.to_thread(minecraft_status)
                paused = False
                try:
                    if status.get("online"):
                        await asyncio.to_thread(minecraft_rcon, "save-off")
                        paused = True
                        await asyncio.to_thread(minecraft_rcon, "save-all flush")
                    item = await asyncio.to_thread(
                        minecraft_create_backup,
                        body.get("label"),
                    )
                finally:
                    if paused:
                        try:
                            await asyncio.to_thread(minecraft_rcon, "save-on")
                        except MinecraftControlError:
                            pass
            elif action == "delete":
                if body.get("confirmed") is not True:
                    return web.json_response(
                        {"error": "confirmation_required"},
                        status=409,
                    )
                item = await asyncio.to_thread(
                    minecraft_delete_backup,
                    body.get("id"),
                )
            elif action == "restore":
                if body.get("confirmed") is not True:
                    return web.json_response(
                        {"error": "confirmation_required"},
                        status=409,
                    )
                status = await asyncio.to_thread(minecraft_status)
                if status.get("online"):
                    return web.json_response(
                        {
                            "error": "minecraft_restore_requires_offline",
                            "message": (
                                "Для восстановления остановите Minecraft-контейнер, "
                                "чтобы активный мир не перезаписал файлы."
                            ),
                        },
                        status=409,
                    )
                item = await asyncio.to_thread(
                    minecraft_restore_backup,
                    body.get("id"),
                )
            else:
                raise MinecraftFilesError(
                    "minecraft_backup_action_invalid",
                    "Неизвестное действие резервной копии.",
                )
        except (MinecraftFilesError, MinecraftControlError, OSError) as exc:
            if isinstance(exc, (MinecraftFilesError, OSError)):
                return minecraft_error(exc)
            return web.json_response(
                {"error": "minecraft_backup_failed", "message": str(exc)},
                status=409,
            )
        result = {"ok": True, "action": action, "item": item}
        await record_minecraft_action(
            principal,
            f"backup_{action}",
            target_type="minecraft_backup",
            target_id=str(item.get("id") or "backup"),
            summary=f"Minecraft-бэкап: {action} · {item.get('id') or 'backup'}",
        )
        remember_minecraft_receipt(receipt, result)
        return web.json_response(result)

    async def minecraft_log_get(request: web.Request) -> web.Response:
        principal = await admin_request(request)
        try:
            lines = int(request.query.get("lines") or 250)
        except (TypeError, ValueError):
            lines = 250
        try:
            payload = await asyncio.to_thread(minecraft_tail_log, lines)
        except (MinecraftFilesError, OSError) as exc:
            return minecraft_error(exc)
        return web.json_response({"viewer": viewer(principal), **payload})

    async def minecraft_command(request: web.Request) -> web.Response:
        principal = await admin_request(request)
        body = await json_body(request, principal)
        try:
            receipt, cached = minecraft_receipt(request, principal)
        except MinecraftFilesError as exc:
            return minecraft_error(exc)
        if cached is not None:
            return web.json_response(cached)
        if body.get("confirmed") is not True:
            return web.json_response(
                {
                    "error": "confirmation_required",
                    "message": "Подтвердите действие в окне Реактора.",
                },
                status=409,
            )
        action = str(body.get("action") or "")
        try:
            if action in {"start", "restart"}:
                await asyncio.to_thread(minecraft_repair_plugin_permissions)
            result = await asyncio.to_thread(minecraft_execute, action, body)
        except MinecraftControlError as exc:
            return web.json_response(
                {"error": "minecraft_command_failed", "message": str(exc)},
                status=409,
            )
        await record_minecraft_action(
            principal,
            action,
            summary=f"Minecraft: принята команда {action}",
        )
        remember_minecraft_receipt(receipt, result)
        minecraft_cache.invalidate()
        health_cache.invalidate()
        attention_cache.invalidate()
        return web.json_response(result)

    app.router.add_get("/reactor", reactor_index)
    app.router.add_get("/reactor/", reactor_index)
    app.router.add_get("/ovr", ovr_index)
    app.router.add_get("/ovr/", ovr_index)
    app.router.add_get("/api/reactor/home", member_home)
    app.router.add_post("/api/reactor/onboarding", onboarding_command)
    app.router.add_get("/api/reactor/legislation", legislation_get)
    app.router.add_get("/api/reactor/preparation", preparation_get)
    app.router.add_post("/api/reactor/legislation", legislation_command)
    app.router.add_get("/api/reactor/ovr", ovr_get)
    app.router.add_post("/api/reactor/ovr", ovr_command)
    app.router.add_get("/api/ovr", ovr_get)
    app.router.add_post("/api/ovr", ovr_command)
    app.router.add_post("/api/ovr/{case_id}/report.pdf", ovr_report)
    app.router.add_post("/api/reactor/preferences", preferences)
    app.router.add_get("/api/reactor/notifications", notifications)
    app.router.add_get("/api/desktop/v1/bootstrap", desktop_bootstrap)
    app.router.add_post("/api/reactor/notifications/read", read_notifications)
    app.router.add_get("/api/admin/reactor/attention", attention)
    app.router.add_get("/api/admin/reactor/health", health)
    app.router.add_get("/api/admin/reactor/search", search)
    app.router.add_get("/api/admin/reactor/events", events)
    app.router.add_get("/api/admin/reactor/events/stream", event_stream)
    app.router.add_get("/api/admin/reactor/minecraft", minecraft_get)
    app.router.add_post("/api/admin/reactor/minecraft", minecraft_command)
    app.router.add_get("/api/admin/reactor/minecraft/files", minecraft_files_get)
    app.router.add_get("/api/admin/reactor/minecraft/file", minecraft_file_get)
    app.router.add_get(
        "/api/admin/reactor/minecraft/download",
        minecraft_file_download,
    )
    app.router.add_post(
        "/api/admin/reactor/minecraft/file",
        minecraft_file_action,
    )
    app.router.add_post(
        "/api/admin/reactor/minecraft/upload",
        minecraft_upload,
    )
    app.router.add_get(
        "/api/admin/reactor/minecraft/plugins",
        minecraft_plugins_get,
    )
    app.router.add_get(
        "/api/admin/reactor/minecraft/backups",
        minecraft_backups_get,
    )
    app.router.add_post(
        "/api/admin/reactor/minecraft/backups",
        minecraft_backup_action,
    )
    app.router.add_get(
        "/api/admin/reactor/minecraft/backup/download",
        minecraft_backup_download,
    )
    app.router.add_get(
        "/api/admin/reactor/minecraft/log",
        minecraft_log_get,
    )

    warm_tasks: list[asyncio.Task[None]] = []

    async def warm_reactor() -> None:
        try:
            await cached_attention()
        except Exception:
            # Telemetry must not delay or prevent the web server from starting.
            pass

    async def begin_warmup(_: web.Application) -> None:
        warm_tasks.append(asyncio.create_task(warm_reactor()))

    async def stop_warmup(_: web.Application) -> None:
        for task in warm_tasks:
            if not task.done():
                task.cancel()
        if warm_tasks:
            await asyncio.gather(*warm_tasks, return_exceptions=True)

    app.on_startup.append(begin_warmup)
    app.on_cleanup.append(stop_warmup)


__all__ = ["register_reactor_web_routes"]
