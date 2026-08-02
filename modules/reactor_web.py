"""Role-aware Reactor web surfaces and their live operational APIs."""

from __future__ import annotations

import asyncio
import json
import logging
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
from modules.reactor_legislation import (
    ReactorLegislationError,
    cancel_workspace,
    create_workspace,
    generate_workspace_draft,
    legislation_snapshot,
    publish_workspace,
    save_workspace,
)
from modules.tvrs_bill_editor import refresh_bill_workspace_panel
from modules.web_snapshot_cache import AsyncSnapshotCache
from persistence import activity_repository as activity_storage
from persistence import admin_dashboard_repository as dashboard_storage
from persistence import bill_workspace_repository as workspace_storage
from persistence import finance_repository as finance_storage
from persistence import market_repository as market_storage
from persistence import outbox_repository as outbox_storage
from persistence import profile_repository as profile_storage
from persistence import reactor_repository as reactor_storage
from persistence import tvrs_repository as tvrs_storage


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
                f"quick_check: {database.get('integrity')} · {database.get('latency_ms')} мс"
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

    async def personal_request(request: web.Request) -> ConsensusWebPrincipal:
        principal, legacy = await authenticate(request)
        if legacy or principal is None:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "personal_login_required"}),
                content_type="application/json",
            )
        return principal

    async def admin_request(request: web.Request) -> ConsensusWebPrincipal:
        principal = await personal_request(request)
        if not principal.administrator:
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
            "csrf_token": str(principal.csrf_token),
        }

    async def build_member_home(principal: ConsensusWebPrincipal) -> dict[str, Any]:
        (
            (profile, _characters),
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
            ),
        )
        positions = _member_positions(principal.member)
        return {
            "profile": asdict(profile) if profile is not None else None,
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

    async def legislation_get(request: web.Request) -> web.Response:
        principal = await personal_request(request)
        snapshot, cache_state = await legislation_cache.get(
            int(principal.user_id),
            lambda: asyncio.to_thread(
                legislation_snapshot,
                int(guild_id),
                int(principal.user_id),
                limit=120,
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

    async def legislation_command(request: web.Request) -> web.Response:
        principal = await personal_request(request)
        body = await json_body(request, principal)
        action = str(body.get("action") or "").strip().lower()
        try:
            if action == "create":
                workspace, created = await asyncio.to_thread(
                    create_workspace,
                    int(guild_id),
                    int(principal.user_id),
                    str(principal.display_name),
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
                bill, created = await publish_workspace(
                    bot,
                    int(guild_id),
                    int(principal.user_id),
                    str(principal.display_name),
                    body,
                )
                member_home_cache.invalidate()
                legislation_cache.invalidate(int(principal.user_id))
                return web.json_response(
                    {"ok": True, "created": created, "bill": bill}
                )
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
            )
            return web.json_response(
                {
                    "error": exc.code,
                    "message": str(exc),
                    "workspace": snapshot.get("workspace"),
                },
                status=exc.status,
            )

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
                    for item in payload["items"]:
                        await asyncio.to_thread(
                            reactor_storage.reactor_put_notification,
                            guild_id=int(guild_id),
                            user_id=user_id,
                            severity=str(item["severity"]),
                            kind="attention",
                            title=str(item["title"]),
                            body=str(item["detail"]),
                            route=str(item["route"]),
                            dedupe_key=str(item["key"]),
                        )
                    await asyncio.to_thread(
                        reactor_storage.reactor_resolve_notifications,
                        int(guild_id),
                        user_id,
                        "attention",
                        [str(item["key"]) for item in payload["items"]],
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
    app.router.add_get("/api/reactor/home", member_home)
    app.router.add_get("/api/reactor/legislation", legislation_get)
    app.router.add_post("/api/reactor/legislation", legislation_command)
    app.router.add_post("/api/reactor/preferences", preferences)
    app.router.add_get("/api/reactor/notifications", notifications)
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
