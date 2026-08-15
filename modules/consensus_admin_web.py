"""Read-only administrative web routes shared by the T-Mod web server."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import discord
from aiohttp import web

from modules.admin_domain_commands import (
    BILL_ERROR_MESSAGES,
    CRAFT_ERROR_MESSAGES,
    FINANCE_ERROR_MESSAGES,
    execute_bill_command,
    execute_craft_command,
    execute_finance_command,
)
from modules.consensus_web_auth import ConsensusWebPrincipal, csrf_matches
from modules.consensus_schedule import (
    cancel_schedule_discord_event,
    parse_schedule_time,
    schedule_event_sync_message,
    send_schedule_invitations,
    sync_schedule_discord_event,
)
from modules.delivery_runtime import wake_delivery_worker
from modules.music_runtime_errors import MusicRuntimeError
from modules.profile import PROFILE_ROLE_HIERARCHY
from modules.reliability import reliability_snapshot
from modules.tvrs_config import (
    TVRS_CONSENSUS_VOICE_CHANNEL_ID,
    TVRS_DEFAULT_NEXT_PLENARY_NUMBER,
    TVRS_SENATOR_ROLE_ID,
)
from modules.tvrs_presentation import is_chair
from modules.web_snapshot_cache import AsyncSnapshotCache
from persistence import admin_dashboard_repository as dashboard_storage
from persistence import activity_repository as activity_storage
from persistence import bill_workspace_repository as workspace_storage
from persistence import broadcast_repository as broadcast_storage
from persistence import craft_repository as craft_storage
from persistence import consensus_schedule_repository as schedule_storage
from persistence import finance_repository as finance_storage
from persistence import market_repository as market_storage
from persistence import profile_repository as profile_storage
from persistence import sgl_archive_repository as sgl_archive_storage
from persistence import tvrs_repository as tvrs_storage
from persistence import voice_control_repository as voice_storage
from persistence import web_portal_repository as portal_storage
from persistence import web_auth_repository as web_auth_storage
from persistence.database_guard import check_live_database, create_database_backup


ADMIN_BROADCAST_TOPIC = "admin.broadcast.dm.v1"
ADMIN_SECTION_LABELS = {
    "overview": "Обзор системы",
    "modules": "Все системы T-Mod",
    "audit": "Аудит действий",
    "treasury": "Казна",
    "craft": "Крафты",
    "market": "Рынок RU15",
    "bills": "Консенсус и законопроекты",
    "sgl": "Бюро СГЛ",
    "members": "Участники",
    "communications": "Уведомления",
    "media": "Музыка и голос",
    "profile": "Мой профиль",
    "discord": "Discord-аудит",
    "system": "Технический контур",
    "atlas": "T-Mod Atlas",
    "atlas_ai": "Доступ к Atlas AI",
    "minecraft": "Minecraft",
    "ovr": "Отдел внешней разведки",
}


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


AuthenticatedRequest = Callable[
    [web.Request],
    Awaitable[tuple[ConsensusWebPrincipal | None, bool]],
]

PORTAL_CAPABILITIES = (
    {
        "id": "consensus",
        "title": "Консенсус",
        "description": "Заседания, очередь, проекты, результаты и симуляция.",
        "section": "bills",
        "scope": "member",
        "state": "integrated",
    },
    {
        "id": "audit",
        "title": "Универсальный аудит",
        "description": "Все значимые действия и безопасные отмены.",
        "section": "audit",
        "scope": "administrator",
        "state": "integrated",
    },
    {
        "id": "finance",
        "title": "Казна",
        "description": "Баланс, движение средств, причины и сверки.",
        "section": "treasury",
        "scope": "administrator",
        "state": "integrated",
    },
    {
        "id": "craft",
        "title": "Крафты",
        "description": "Рецепты, планы, материалы, циклы и экономика.",
        "section": "craft",
        "scope": "administrator",
        "state": "integrated",
    },
    {
        "id": "market",
        "title": "Рынок RU15",
        "description": "Предметы, автомобили, одежда, история и сигналы.",
        "section": "market",
        "scope": "member",
        "state": "integrated",
    },
    {
        "id": "sgl",
        "title": "Бюро СГЛ",
        "description": "Кейсы, чеки, архивы, клиенты и сотрудники.",
        "section": "sgl",
        "scope": "administrator",
        "state": "integrated",
        "url": "https://sgl.tvr.lat/",
    },
    {
        "id": "members",
        "title": "Участники",
        "description": "Профили, персонажи, активность и зоны ответственности.",
        "section": "members",
        "scope": "administrator",
        "state": "integrated",
    },
    {
        "id": "communications",
        "title": "Коммуникации",
        "description": "Глобальные уведомления и отчёты доставки.",
        "section": "communications",
        "scope": "administrator",
        "state": "integrated",
    },
    {
        "id": "media",
        "title": "Музыка и голос",
        "description": "YouTube, очередь воспроизведения и голосовое управление.",
        "section": "media",
        "scope": "administrator",
        "state": "integrated",
    },
    {
        "id": "profile",
        "title": "Мой профиль",
        "description": "Персонажи, приватность, уведомления и микрофон.",
        "section": "profile",
        "scope": "member",
        "state": "integrated",
    },
    {
        "id": "discord",
        "title": "Discord Intelligence",
        "description": "События, каналы, участники и источники.",
        "section": "discord",
        "scope": "administrator",
        "state": "integrated",
    },
    {
        "id": "system",
        "title": "Система",
        "description": "Очереди доставки, фоновые задачи и техническое здоровье.",
        "section": "system",
        "scope": "administrator",
        "state": "integrated",
    },
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


def _query_int(
    request: web.Request,
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = 100,
) -> int:
    try:
        value = int(request.query.get(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


def _optional_query_id(request: web.Request, name: str) -> int | None:
    raw = str(request.query.get(name, "")).strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": f"invalid_{name}"}),
            content_type="application/json",
        )
    if value <= 0:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": f"invalid_{name}"}),
            content_type="application/json",
        )
    return value


def _batch_payload(batch: dict[str, Any] | None) -> dict[str, Any] | None:
    if batch is None:
        return None
    return {
        "id": int(batch.get("id") or 0),
        "quantity": int(batch.get("quantity") or 0),
        "status": str(batch.get("status") or ""),
        "started_at": batch.get("started_at"),
        "due_at": batch.get("due_at"),
        "completed_at": batch.get("completed_at"),
        "started_by": {
            "id": int(batch.get("started_by_id") or 0) or None,
            "name": str(batch.get("started_by_display") or "Не указан"),
        },
    }


def _craft_plan_payload(
    plan: dict[str, Any],
    guild_id: int,
) -> dict[str, Any]:
    recipe = plan.get("recipe") if isinstance(plan.get("recipe"), dict) else {}
    active_batch = (
        plan.get("active_batch") if isinstance(plan.get("active_batch"), dict) else None
    )
    last_batch = (
        plan.get("last_batch") if isinstance(plan.get("last_batch"), dict) else None
    )
    message_id = int(plan.get("message_id") or 0)
    channel_id = int(plan.get("thread_id") or plan.get("channel_id") or 0)
    materials = []
    for material in plan.get("materials") or []:
        if not isinstance(material, dict):
            continue
        materials.append(
            {
                "id": int(material.get("id") or 0),
                "name": str(material.get("material_name") or "Материал"),
                "quantity_per_unit": int(material.get("quantity_per_unit") or 0),
                "required_total": int(material.get("required_total") or 0),
                "stock_quantity": int(material.get("stock_quantity") or 0),
                "purchased_quantity": int(material.get("purchased_quantity") or 0),
                "spent_total": int(material.get("spent_total") or 0),
            }
        )
    return {
        "id": int(plan.get("id") or 0),
        "stage": str(plan.get("stage") or "procurement"),
        "product_name": str(
            plan.get("product_name_snapshot")
            or recipe.get("product_name")
            or "Неизвестный продукт"
        ),
        "recipe_version": int(plan.get("recipe_version") or 1),
        "responsible": {
            "id": int(plan.get("responsible_id") or 0) or None,
            "name": str(plan.get("responsible_display") or "Не назначен"),
        },
        "created_by": {
            "id": int(plan.get("created_by_id") or 0) or None,
            "name": str(plan.get("created_by_display") or "Не указан"),
        },
        "attempts_total": int(plan.get("attempts_total") or 0),
        "attempts_queued": int(plan.get("attempts_queued") or 0),
        "attempts_completed": int(plan.get("attempts_completed") or 0),
        "product_stock": int(plan.get("product_stock") or 0),
        "final_product_qty": int(plan.get("final_product_qty") or 0),
        "estimated_unit_price": (
            int(plan["estimated_unit_price"])
            if plan.get("estimated_unit_price") is not None
            else None
        ),
        "market_listed_qty": int(plan.get("market_listed_qty") or 0),
        "sold_qty": int(plan.get("sold_qty") or 0),
        "total_revenue": int(plan.get("total_revenue") or 0),
        "purchase_cost_total": int(plan.get("purchase_cost_total") or 0),
        "purchase_count": int(plan.get("purchase_count") or 0),
        "sale_count": int(plan.get("sale_count") or 0),
        "materials": materials,
        "active_batch": _batch_payload(active_batch),
        "last_batch": _batch_payload(last_batch),
        "created_at": plan.get("created_at"),
        "updated_at": plan.get("updated_at"),
        "completed_at": plan.get("completed_at"),
        "discord_url": (
            _discord_message_url(int(guild_id), channel_id, message_id)
            if message_id
            else (
                f"https://discord.com/channels/{int(guild_id)}/{channel_id}"
                if channel_id
                else None
            )
        ),
    }


def register_admin_web_routes(
    app: web.Application,
    bot: discord.Client,
    *,
    guild_id: int,
    asset_dir: Path,
    authenticate: AuthenticatedRequest,
) -> None:
    """Attach protected administrative views and audited commands."""

    command_receipts: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
    overview_cache = AsyncSnapshotCache[int, dict[str, Any]](
        ttl_seconds=8,
        max_stale_seconds=180,
    )

    async def admin_index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(asset_dir / "admin.html")

    async def administrative_request(
        request: web.Request,
    ) -> ConsensusWebPrincipal:
        principal, legacy_read_only = await authenticate(request)
        if legacy_read_only or principal is None:
            raise web.HTTPForbidden(
                text=json.dumps(
                    {
                        "error": "personal_login_required",
                        "message": (
                            "Откройте персональную ссылку админ-центра из Discord."
                        ),
                    },
                    ensure_ascii=False,
                ),
                content_type="application/json",
            )
        section_map = {
            "overview": "overview",
            "actions": "audit",
            "finance": "treasury",
            "crafts": "craft",
            "discord": "discord",
            "registry": "modules",
            "market": "market",
            "bills": "bills",
            "sgl": "sgl",
            "members": "members",
            "communications": "communications",
            "media": "media",
            "profile": "profile",
            "system": "system",
            "atlas": "atlas",
        }
        endpoint = request.path.removeprefix("/api/admin/").split("/", 1)[0]
        section = section_map.get(endpoint, "overview")
        if endpoint == "link":
            section = {
                "craft-plan": "craft",
                "bill": "bills",
                "sgl-case": "sgl",
                "member": "members",
                "market": "market",
                "audit": "audit",
                "discord-event": "discord",
            }.get(str(request.match_info.get("kind") or ""), "overview")
        grants = [] if principal.administrator else await asyncio.to_thread(
            web_auth_storage.web_section_grants, int(guild_id), int(principal.user_id)
        )
        if not principal.administrator and section not in {row["section"] for row in grants}:
            raise web.HTTPForbidden(
                text=json.dumps(
                    {
                        "error": "section_access_required",
                        "message": "Для этого раздела доступ не выдан.",
                    },
                    ensure_ascii=False,
                ),
                content_type="application/json",
            )
        return principal

    async def access_control(request: web.Request) -> web.Response:
        principal, legacy = await authenticate(request)
        if legacy or principal is None or not principal.administrator:
            raise web.HTTPForbidden(text='{"error":"administrator_required"}', content_type="application/json")
        if request.method == "POST":
            if not csrf_matches(request, principal):
                raise web.HTTPForbidden(text='{"error":"csrf_failed"}', content_type="application/json")
            try:
                body = await request.json()
            except (json.JSONDecodeError, TypeError):
                body = None
            if not isinstance(body, dict):
                return web.json_response({"error": "invalid_payload"}, status=400)
            member = None
            try:
                user_id = int(body.get("user_id"))
                section = str(body.get("section") or "").strip().lower()
                enabled = body.get("enabled", True) is True
                if enabled:
                    guild = bot.get_guild(int(guild_id))
                    member = guild.get_member(user_id) if guild is not None else None
                    if member is None and guild is not None:
                        try:
                            member = await guild.fetch_member(user_id)
                        except discord.DiscordException:
                            member = None
                    if member is None and section == "atlas_ai":
                        fetch_user = getattr(bot, "fetch_user", None)
                        try:
                            member = await fetch_user(user_id) if callable(fetch_user) else None
                        except discord.DiscordException:
                            member = None
                    if member is None:
                        return web.json_response(
                            {
                                "error": "user_not_found",
                                "message": "Пользователь с таким Discord ID не найден.",
                            },
                            status=404,
                        )
                changed = await asyncio.to_thread(
                    web_auth_storage.web_set_section_grant,
                    int(guild_id),
                    user_id,
                    section,
                    enabled=enabled,
                    granted_by_id=int(principal.user_id),
                )
            except (TypeError, ValueError):
                return web.json_response(
                    {
                        "error": "web_section_grant_invalid",
                        "message": "Проверьте Discord ID и выбранный раздел.",
                    },
                    status=400,
                )
            dm_sent = False
            if enabled and changed and member is not None:
                label = ADMIN_SECTION_LABELS.get(section, section)
                atlas_access = section == "atlas_ai"
                ovr_access = section == "ovr"
                target_url = (
                    "https://atlas.tvr.lat/"
                    if atlas_access
                    else "https://tvr.lat/reactor#ovr"
                    if ovr_access
                    else f"https://reactor.tvr.lat/admin#/{section}"
                )
                embed = discord.Embed(
                    title="Доступ к Atlas AI" if atlas_access else "Доступ к Ядерному Реактору",
                    description=(
                        f"Вам открыт раздел **{label}**.\n\n"
                        "Войдите с вашим логином и восьмизначным PIN."
                    ),
                    color=0x68E0B7,
                    url=target_url,
                )
                embed.add_field(
                    name="Открыть раздел",
                    value=(
                        f"[Открыть → {label}]({target_url})"
                    ),
                    inline=False,
                )
                embed.set_footer(text=f"Доступ выдал {principal.display_name} · T-Mod")
                try:
                    await member.send(
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    dm_sent = True
                except discord.DiscordException:
                    dm_sent = False
            await asyncio.to_thread(
                activity_storage.bot_record_action,
                guild_id=int(guild_id),
                actor_id=int(principal.user_id),
                actor_display=str(principal.display_name),
                module="admin",
                action_kind="web_section_grant" if enabled else "web_section_revoke",
                target_type="member",
                target_id=user_id,
                summary=(
                    f"Выдан доступ к разделу {section}"
                    if enabled
                    else f"Отозван доступ к разделу {section}"
                ),
                payload={"section": section, "dm_sent": dm_sent},
                reversible=False,
            )
            if enabled and not changed:
                result_message = "Этот доступ уже был выдан ранее."
            elif enabled and dm_sent:
                result_message = "Доступ выдан, уведомление отправлено в ЛС."
            elif enabled:
                result_message = (
                    "Доступ выдан, но отправить уведомление в ЛС не удалось."
                )
            elif changed:
                result_message = "Доступ отозван."
            else:
                result_message = "Этот доступ уже был отозван ранее."
        else:
            result_message = None
            changed = False
            dm_sent = False
        guild = bot.get_guild(int(guild_id))
        grants = await asyncio.to_thread(
            web_auth_storage.web_section_grants,
            int(guild_id),
        )
        projected_grants = []
        for grant in grants:
            grant_member = guild.get_member(int(grant["user_id"])) if guild else None
            projected_grants.append(
                {
                    **grant,
                    "user_id_text": str(grant["user_id"]),
                    "member_name": str(
                        getattr(grant_member, "display_name", "")
                        or f"Discord {grant['user_id']}"
                    ),
                    "section_label": ADMIN_SECTION_LABELS.get(
                        str(grant["section"]), str(grant["section"])
                    ),
                }
            )
        return web.json_response({
            **context(principal),
            "sections": [
                {"id": section, "label": ADMIN_SECTION_LABELS[section]}
                for section in ADMIN_SECTION_LABELS
                if section in web_auth_storage.WEB_GRANTABLE_SECTIONS
            ],
            "grants": projected_grants,
            "changed": changed,
            "dm_sent": dm_sent,
            "message": result_message,
        })

    async def access_self(request: web.Request) -> web.Response:
        principal, legacy = await authenticate(request)
        if legacy or principal is None:
            raise web.HTTPForbidden(text='{"error":"personal_login_required"}', content_type="application/json")
        sections = (
            sorted(web_auth_storage.WEB_GRANTABLE_SECTIONS)
            if principal.administrator
            else [row["section"] for row in await asyncio.to_thread(
                web_auth_storage.web_section_grants, int(guild_id), int(principal.user_id)
            ) if row["section"] != "atlas_ai"]
        )
        if not sections:
            raise web.HTTPForbidden(text='{"error":"administrator_required"}', content_type="application/json")
        return web.json_response({
            **context(principal),
            "sections": sections,
            "administrator": bool(principal.administrator),
        })

    def context(principal: ConsensusWebPrincipal) -> dict[str, Any]:
        guild = bot.get_guild(int(guild_id))
        return {
            "viewer": {
                "id": int(principal.user_id),
                "name": str(principal.display_name),
                "administrator": bool(principal.administrator),
                "csrf_token": str(principal.csrf_token),
                "roles": [
                    {
                        "id": int(getattr(role, "id", 0) or 0),
                        "name": str(getattr(role, "name", "") or ""),
                    }
                    for role in getattr(principal.member, "roles", ())
                    if int(getattr(role, "id", 0) or 0) > 0
                ],
            },
            "guild": {
                "id": int(guild_id),
                "name": str(getattr(guild, "name", "") or "Товарищество"),
            },
        }

    async def linked_record(request: web.Request) -> web.Response:
        """Resolve one record addressed by a stable, authenticated web link."""
        principal = await administrative_request(request)
        kind = str(request.match_info.get("kind", "")).strip().lower()
        record_key = str(request.match_info.get("record_key", "")).strip()
        item: dict[str, Any] | None = None

        if kind == "craft-plan":
            try:
                plan_id = int(record_key)
            except (TypeError, ValueError):
                plan_id = 0
            plan = await asyncio.to_thread(
                craft_storage.craft_get_plan,
                plan_id,
                int(guild_id),
            )
            if plan is not None:
                item = _craft_plan_payload(plan, int(guild_id))
        elif kind == "bill":
            try:
                bill_id = int(record_key)
            except (TypeError, ValueError):
                bill_id = 0
            bill, result = await asyncio.gather(
                asyncio.to_thread(tvrs_storage.tvrs_get_bill_dict_by_id, bill_id),
                asyncio.to_thread(
                    tvrs_storage.tvrs_latest_live_result_for_bill,
                    int(guild_id),
                    bill_id,
                ),
            )
            if bill is not None and int(bill.get("guild_id") or 0) == int(guild_id):
                item = dict(bill)
                if result is not None:
                    for key, value in result.items():
                        item[f"result_{key}"] = value
        elif kind == "workspace":
            try:
                workspace_id = int(record_key)
            except (TypeError, ValueError):
                workspace_id = 0
            workspace = await asyncio.to_thread(
                workspace_storage.get_bill_workspace,
                workspace_id,
            )
            if workspace is not None and int(workspace.get("guild_id") or 0) == int(
                guild_id
            ):
                item = workspace
        else:
            item = await asyncio.to_thread(
                portal_storage.portal_linked_record,
                int(guild_id),
                kind,
                record_key,
            )

        if item is None:
            return web.json_response(
                {
                    "error": "linked_record_not_found",
                    "message": "Запись не найдена или ссылка больше недоступна.",
                },
                status=404,
            )

        if kind == "discord-event":
            item["discord_url"] = _discord_message_url(
                int(guild_id),
                item.get("channel_id"),
                item.get("message_id"),
            )
        if kind == "member":
            guild = bot.get_guild(int(guild_id))
            get_member = getattr(guild, "get_member", None)
            member = get_member(int(item["user_id"])) if callable(get_member) else None
            positions = _member_positions(member)
            item["legal_positions"] = positions
            item["legal_status"] = (
                str(positions[0]["label"]) if positions else "Прихожанин"
            )

        title_templates = {
            "audit": "Запись аудита",
            "finance": f"Финансовая операция #{record_key}",
            "craft-plan": f"Крафт #{record_key}",
            "craft-event": f"Событие крафта #{record_key}",
            "discord-event": "Discord-событие",
            "market": str(item.get("item_name") or "Объект рынка"),
            "bill": f"Законопроект №{item.get('bill_number') or record_key}",
            "workspace": str(item.get("title") or f"Редактор #{record_key}"),
            "case": f"Кейс СГЛ №{item.get('case_number') or record_key}",
            "archive": f"Архив кейса №{item.get('case_number') or record_key}",
            "member": str(
                item.get("display_name") or item.get("name") or f"Участник {record_key}"
            ),
            "broadcast": f"Кампания #{record_key}",
        }
        return web.json_response(
            {
                **context(principal),
                "kind": kind,
                "record_key": record_key,
                "title": title_templates.get(kind, f"Запись #{record_key}"),
                "item": item,
            }
        )

    async def command_request(
        request: web.Request,
    ) -> tuple[ConsensusWebPrincipal, dict[str, Any], str]:
        principal = await administrative_request(request)
        now = time.monotonic()
        for key, (expires_at, _) in list(command_receipts.items()):
            if expires_at <= now:
                command_receipts.pop(key, None)
        if not csrf_matches(request, principal):
            raise web.HTTPForbidden(
                text=json.dumps(
                    {
                        "error": "csrf_failed",
                        "message": "Обновите панель и повторите действие.",
                    },
                    ensure_ascii=False,
                ),
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
        idempotency_key = request.headers.get("X-Idempotency-Key", "").strip()
        if not 12 <= len(idempotency_key) <= 120:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "idempotency_key_required"}),
                content_type="application/json",
            )
        return principal, body, idempotency_key

    def music_snapshot() -> dict[str, Any]:
        manager = getattr(bot, "music_manager", None)
        if manager is None:
            return {
                "available": False,
                "connected": False,
                "message": "Музыкальный менеджер ещё не инициализирован.",
            }
        session = manager.get(int(guild_id))
        if session is None:
            return {
                "available": True,
                "connected": False,
                "voice_receive_available": bool(manager.voice_receive_available),
                "queue": [],
                "current": None,
            }
        guild = bot.get_guild(int(guild_id))
        voice_client = getattr(guild, "voice_client", None) if guild else None
        return {
            "available": True,
            "connected": True,
            "voice_receive_available": bool(manager.voice_receive_available),
            "voice_channel_id": int(session.voice_channel_id),
            "text_channel_id": int(session.text_channel_id),
            "connected_by": {
                "id": int(session.connected_by_id),
                "name": str(session.connected_by_display),
            },
            "current": asdict(session.current) if session.current is not None else None,
            "queue": [asdict(track) for track in list(session.queue)],
            "volume_percent": round(float(session.volume) * 100),
            "paused": bool(
                voice_client is not None
                and callable(getattr(voice_client, "is_paused", None))
                and voice_client.is_paused()
            ),
            "playing": bool(
                voice_client is not None
                and callable(getattr(voice_client, "is_playing", None))
                and voice_client.is_playing()
            ),
            "voice_users": len(session.voice_users),
            "armed_users": len(session.armed_until),
            "last_notice": session.last_notice,
            "last_error": session.last_error,
        }

    async def build_overview_snapshot(days: int) -> dict[str, Any]:
        (
            counts,
            finance_state,
            finance_stats,
            craft_stats,
            active_plans,
            actions,
            discord_stats,
            discord_events,
        ) = await asyncio.gather(
            asyncio.to_thread(
                dashboard_storage.admin_dashboard_counts,
                int(guild_id),
                days=days,
            ),
            asyncio.to_thread(
                finance_storage.finance_get_latest_state,
                int(guild_id),
            ),
            asyncio.to_thread(
                finance_storage.finance_stats,
                int(guild_id),
                days,
            ),
            asyncio.to_thread(
                craft_storage.craft_stats,
                int(guild_id),
                days,
            ),
            asyncio.to_thread(
                craft_storage.craft_active_plans,
                int(guild_id),
                12,
            ),
            asyncio.to_thread(
                dashboard_storage.admin_audit_actions,
                int(guild_id),
                limit=10,
            ),
            asyncio.to_thread(
                dashboard_storage.admin_discord_stats,
                int(guild_id),
                days=min(days, 90),
            ),
            asyncio.to_thread(
                dashboard_storage.admin_discord_events,
                int(guild_id),
                days=min(days, 90),
                limit=10,
            ),
        )
        guild = bot.get_guild(int(guild_id))
        bot_user = getattr(bot, "user", None)
        raw_latency = getattr(bot, "latency", None)
        try:
            latency_ms = max(0, round(float(raw_latency) * 1000))
        except (TypeError, ValueError):
            latency_ms = None
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "days": days,
            "counts": counts,
            "finance": {
                "state": finance_state,
                "stats": finance_stats,
            },
            "craft": {
                "stats": craft_stats,
                "active_plans": [
                    _craft_plan_payload(plan, int(guild_id)) for plan in active_plans
                ],
            },
            "audit": actions,
            "discord": {
                "stats": discord_stats,
                "events": {
                    **discord_events,
                    "items": [
                        {
                            **item,
                            "discord_url": _discord_message_url(
                                int(guild_id),
                                item.get("channel_id"),
                                item.get("message_id"),
                            ),
                        }
                        for item in discord_events["items"]
                    ],
                },
            },
            "system": {
                "bot": str(bot_user or "T-Mod"),
                "connected": guild is not None,
                "latency_ms": latency_ms,
                "members": len(getattr(guild, "members", ()) or ()),
                "channels": len(getattr(guild, "channels", ()) or ()),
            },
        }

    async def overview(request: web.Request) -> web.Response:
        principal = await administrative_request(request)
        days = _query_int(request, "days", 30, minimum=1, maximum=365)
        snapshot, cache_state = await overview_cache.get(
            days,
            lambda: build_overview_snapshot(days),
            force=request.query.get("fresh") == "1",
        )
        response = web.json_response(
            {
                **context(principal),
                **snapshot,
                "cache_state": cache_state,
            }
        )
        response.headers["X-T-Mod-Cache"] = cache_state
        return response

    async def actions(request: web.Request) -> web.Response:
        principal = await administrative_request(request)
        result = await asyncio.to_thread(
            dashboard_storage.admin_audit_actions,
            int(guild_id),
            module=str(request.query.get("module", "")).strip() or None,
            status=str(request.query.get("status", "")).strip() or None,
            actor_id=_optional_query_id(request, "actor_id"),
            query=str(request.query.get("q", "")).strip() or None,
            limit=_query_int(request, "limit", 50, minimum=1, maximum=100),
            offset=_query_int(
                request,
                "offset",
                0,
                minimum=0,
                maximum=50_000,
            ),
        )
        return web.json_response({**context(principal), **result})

    async def finance(request: web.Request) -> web.Response:
        principal = await administrative_request(request)
        days = _query_int(request, "days", 30, minimum=1, maximum=365)
        try:
            latest, stats, events = await asyncio.gather(
                asyncio.to_thread(
                    finance_storage.finance_get_latest_state,
                    int(guild_id),
                ),
                asyncio.to_thread(
                    finance_storage.finance_stats,
                    int(guild_id),
                    days,
                ),
                asyncio.to_thread(
                    dashboard_storage.admin_finance_events,
                    int(guild_id),
                    days=days,
                    event_kind=(str(request.query.get("kind", "")).strip() or None),
                    actor_id=_optional_query_id(request, "actor_id"),
                    code=str(request.query.get("code", "")).strip() or None,
                    query=str(request.query.get("q", "")).strip() or None,
                    limit=_query_int(
                        request,
                        "limit",
                        50,
                        minimum=1,
                        maximum=100,
                    ),
                    offset=_query_int(
                        request,
                        "offset",
                        0,
                        minimum=0,
                        maximum=50_000,
                    ),
                ),
            )
        except ValueError as exc:
            return web.json_response(
                {"error": str(exc), "message": "Некорректный фильтр казны."},
                status=400,
            )
        return web.json_response(
            {
                **context(principal),
                "state": latest,
                "stats": stats,
                **events,
            }
        )

    async def crafts(request: web.Request) -> web.Response:
        principal = await administrative_request(request)
        days = _query_int(request, "days", 30, minimum=1, maximum=365)
        stats, active, recent, recipes, events = await asyncio.gather(
            asyncio.to_thread(
                craft_storage.craft_stats,
                int(guild_id),
                days,
            ),
            asyncio.to_thread(
                craft_storage.craft_active_plans,
                int(guild_id),
                50,
            ),
            asyncio.to_thread(
                craft_storage.craft_recent_plans,
                int(guild_id),
                50,
            ),
            asyncio.to_thread(
                craft_storage.craft_list_recipes,
                int(guild_id),
                active_only=False,
                limit=100,
            ),
            asyncio.to_thread(
                dashboard_storage.admin_craft_events,
                int(guild_id),
                query=str(request.query.get("q", "")).strip() or None,
                limit=_query_int(
                    request,
                    "limit",
                    50,
                    minimum=1,
                    maximum=100,
                ),
                offset=_query_int(
                    request,
                    "offset",
                    0,
                    minimum=0,
                    maximum=50_000,
                ),
            ),
        )
        return web.json_response(
            {
                **context(principal),
                "stats": stats,
                "active_plans": [
                    _craft_plan_payload(plan, int(guild_id)) for plan in active
                ],
                "recent_plans": [
                    _craft_plan_payload(plan, int(guild_id)) for plan in recent
                ],
                "recipes": recipes,
                "events": events,
            }
        )

    async def craft_command(request: web.Request) -> web.Response:
        principal, body, idempotency_key = await command_request(request)
        now = time.monotonic()
        receipt_key = (int(principal.user_id), idempotency_key)
        cached = command_receipts.get(receipt_key)
        if cached is not None and cached[0] > now:
            return web.json_response(cached[1])
        if body.get("confirmed") is not True:
            return web.json_response(
                {
                    "error": "craft_confirmation_required",
                    "message": "Подтвердите производственную операцию.",
                },
                status=400,
            )
        try:
            result = await execute_craft_command(
                bot,
                guild_id=int(guild_id),
                actor_id=int(principal.user_id),
                actor_display=str(principal.display_name),
                body=body,
            )
        except (TypeError, ValueError) as exc:
            code = str(exc).split(":", 1)[0]
            return web.json_response(
                {
                    "error": str(exc),
                    "message": CRAFT_ERROR_MESSAGES.get(
                        code,
                        "Не удалось выполнить операцию крафта. Проверьте стадию и введённые данные.",
                    ),
                },
                status=400,
            )
        except (discord.DiscordException, OSError, RuntimeError) as exc:
            return web.json_response(
                {
                    "error": type(exc).__name__,
                    "message": "Discord временно не завершил операцию. Состояние проверено и небезопасный незакреплённый план удалён.",
                },
                status=503,
            )
        payload = {
            "ok": True,
            "message": result["message"],
            "plan": (
                _craft_plan_payload(result["plan"], int(guild_id))
                if isinstance(result.get("plan"), dict)
                else None
            ),
            "recipe": result.get("recipe"),
            "projection_warning": result.get("projection_warning"),
        }
        command_receipts[receipt_key] = (now + 300.0, payload)
        return web.json_response(payload)

    async def finance_command(request: web.Request) -> web.Response:
        principal, body, idempotency_key = await command_request(request)
        now = time.monotonic()
        receipt_key = (int(principal.user_id), idempotency_key)
        cached = command_receipts.get(receipt_key)
        if cached is not None and cached[0] > now:
            return web.json_response(cached[1])
        if body.get("confirmed") is not True:
            return web.json_response(
                {
                    "error": "finance_confirmation_required",
                    "message": "Подтвердите изменение финансового реестра.",
                },
                status=400,
            )
        try:
            result = await execute_finance_command(
                guild_id=int(guild_id),
                actor_id=int(principal.user_id),
                actor_display=str(principal.display_name),
                idempotency_key=idempotency_key,
                body=body,
            )
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {
                    "error": str(exc),
                    "message": FINANCE_ERROR_MESSAGES.get(
                        str(exc),
                        "Не удалось изменить казну. Проверьте сумму, причину и код.",
                    ),
                },
                status=400,
            )
        payload = {"ok": True, **result}
        command_receipts[receipt_key] = (now + 300.0, payload)
        return web.json_response(payload)

    async def bill_command(request: web.Request) -> web.Response:
        principal, body, idempotency_key = await command_request(request)
        now = time.monotonic()
        receipt_key = (int(principal.user_id), idempotency_key)
        cached = command_receipts.get(receipt_key)
        if cached is not None and cached[0] > now:
            return web.json_response(cached[1])
        if body.get("confirmed") is not True:
            return web.json_response(
                {
                    "error": "bill_confirmation_required",
                    "message": "Подтвердите изменение реестра законопроектов.",
                },
                status=400,
            )
        try:
            result = await execute_bill_command(
                guild_id=int(guild_id),
                actor_id=int(principal.user_id),
                actor_display=str(principal.display_name),
                body=body,
            )
        except (TypeError, ValueError) as exc:
            code = str(exc).split(":", 1)[0]
            return web.json_response(
                {
                    "error": str(exc),
                    "message": BILL_ERROR_MESSAGES.get(
                        code,
                        "Проект не изменён: проверьте данные и состояние консенсуса.",
                    ),
                },
                status=400,
            )
        payload = {"ok": True, **result}
        command_receipts[receipt_key] = (now + 300.0, payload)
        return web.json_response(payload)

    async def consensus_schedule_command(request: web.Request) -> web.Response:
        principal, body, idempotency_key = await command_request(request)
        if not is_chair(principal.member):
            return web.json_response(
                {
                    "error": "consensus_schedule_chair_required",
                    "message": "Планировать заседание может только председатель или сопредседатель.",
                },
                status=403,
            )
        now = time.monotonic()
        receipt_key = (int(principal.user_id), idempotency_key)
        cached = command_receipts.get(receipt_key)
        if cached is not None and cached[0] > now:
            return web.json_response(cached[1])
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return web.json_response(
                {"error": "guild_unavailable", "message": "Discord-сервер временно недоступен."},
                status=503,
            )
        action = str(body.get("action") or "save").strip().lower()
        warning = None
        count = None
        try:
            current = await asyncio.to_thread(
                schedule_storage.get_upcoming_consensus_schedule,
                int(guild_id),
            )
            if action == "save":
                starts_at = parse_schedule_time(
                    str(body.get("scheduled_for") or "").replace("T", " ")
                )
                schedule_id = int(body.get("schedule_id") or 0) or None
                if current is not None and schedule_id is None:
                    schedule_id = int(current["id"])
                schedule = await asyncio.to_thread(
                    schedule_storage.save_consensus_schedule,
                    guild_id=int(guild_id),
                    plenary_number=int(
                        body.get("plenary_number")
                        or await asyncio.to_thread(
                            tvrs_storage.tvrs_get_next_plenary_number,
                            int(guild_id),
                            TVRS_DEFAULT_NEXT_PLENARY_NUMBER,
                        )
                    ),
                    title=str(body.get("title") or ""),
                    description=str(body.get("description") or ""),
                    invitation_text=str(body.get("invitation_text") or ""),
                    scheduled_for=starts_at,
                    duration_minutes=int(body.get("duration_minutes") or 90),
                    voice_channel_id=int(
                        body.get("voice_channel_id")
                        or TVRS_CONSENSUS_VOICE_CHANNEL_ID
                    ),
                    actor_id=int(principal.user_id),
                    actor_display=str(principal.display_name),
                    schedule_id=schedule_id,
                    expected_revision=(
                        int(body.get("expected_revision") or 0)
                        if schedule_id is not None
                        else None
                    ),
                )
                try:
                    schedule = await sync_schedule_discord_event(guild, schedule)
                except (discord.DiscordException, ValueError) as exc:
                    warning = f"{schedule_event_sync_message(exc)} Нажмите «Синхронизировать»."
                message = "План заседания сохранён."
            elif action == "sync":
                if current is None:
                    raise ValueError("consensus_schedule_missing")
                schedule = await sync_schedule_discord_event(guild, current)
                message = "Событие Discord синхронизировано."
            elif action == "invite":
                if current is None:
                    raise ValueError("consensus_schedule_missing")
                schedule, count = await send_schedule_invitations(
                    guild,
                    current,
                    principal.member,
                )
                message = f"Приглашения поставлены в очередь: {count}."
            elif action == "cancel":
                if body.get("confirmed") is not True or current is None:
                    raise ValueError("consensus_schedule_confirmation_required")
                schedule = await asyncio.to_thread(
                    schedule_storage.cancel_consensus_schedule,
                    int(current["id"]),
                    guild_id=int(guild_id),
                    expected_revision=int(current["revision"]),
                )
                try:
                    await cancel_schedule_discord_event(guild, schedule)
                except discord.DiscordException as exc:
                    warning = f"План отменён, но событие Discord не ответило: {type(exc).__name__}."
                message = "План заседания отменён."
            else:
                raise ValueError("consensus_schedule_action_invalid")
        except (TypeError, ValueError, discord.DiscordException) as exc:
            code = str(exc)
            messages = {
                "consensus_schedule_time_invalid": "Укажите дату и время начала.",
                "consensus_schedule_time_past": "Начало должно быть хотя бы через одну минуту.",
                "consensus_schedule_time_too_far": "Заседание можно планировать максимум на год вперёд.",
                "consensus_schedule_title_invalid": "Название должно содержать от 3 до 100 символов.",
                "consensus_schedule_description_invalid": "Описание не должно быть длиннее 1000 символов.",
                "consensus_schedule_invitation_invalid": "Текст приглашения не должен быть длиннее 1500 символов.",
                "consensus_schedule_duration_invalid": "Продолжительность должна быть от 15 до 480 минут.",
                "consensus_schedule_conflict": "План уже изменён в другом окне. Обновите раздел.",
                "consensus_schedule_missing": "Сначала создайте план заседания.",
                "consensus_schedule_recipients_empty": "Не найдено участников для приглашения.",
                "consensus_schedule_voice_channel_missing": "Выбранный голосовой канал недоступен.",
                "consensus_schedule_voice_channel_invalid": "Выберите голосовой или сценический канал.",
                "consensus_schedule_event_permission_missing": "T-Mod не хватает права создавать события Discord.",
                "consensus_schedule_confirmation_required": "Подтвердите отмену заседания.",
            }
            return web.json_response(
                {
                    "error": code,
                    "message": messages.get(code, "Не удалось изменить план заседания."),
                },
                status=409 if code == "consensus_schedule_conflict" else 400,
            )
        payload = {
            "ok": True,
            "message": message,
            "warning": warning,
            "recipient_count": count,
            "schedule": schedule,
        }
        command_receipts[receipt_key] = (now + 300.0, payload)
        await asyncio.to_thread(
            activity_storage.bot_record_action,
            guild_id=int(guild_id),
            actor_id=int(principal.user_id),
            actor_display=str(principal.display_name),
            module="consensus",
            action_kind=f"consensus_schedule_{action}",
            target_type="consensus_schedule",
            target_id=int(schedule.get("id") or 0),
            summary=message,
            payload={"source": "nuclear-reactor", "warning": warning},
            reversible=False,
        )
        return web.json_response(payload)

    async def sgl_command(request: web.Request) -> web.Response:
        principal, body, idempotency_key = await command_request(request)
        now = time.monotonic()
        receipt_key = (int(principal.user_id), idempotency_key)
        cached = command_receipts.get(receipt_key)
        if cached is not None and cached[0] > now:
            return web.json_response(cached[1])
        if body.get("confirmed") is not True:
            return web.json_response(
                {
                    "error": "sgl_confirmation_required",
                    "message": "Подтвердите восстановление юридического архива.",
                },
                status=400,
            )
        if str(body.get("action") or "").strip().lower() != "restore_archive":
            return web.json_response(
                {"error": "sgl_action_invalid", "message": "Неизвестная операция Бюро."},
                status=400,
            )
        try:
            case_number = int(body.get("case_number") or 0)
        except (TypeError, ValueError):
            case_number = 0
        archive = await asyncio.to_thread(
            sgl_archive_storage.get_sgl_case_archive,
            int(guild_id),
            case_number,
        )
        guild = bot.get_guild(int(guild_id))
        if archive is None or guild is None:
            return web.json_response(
                {"error": "sgl_archive_not_found", "message": "Архив кейса не найден."},
                status=404,
            )
        try:
            from modules.sgl_archive_restore import start_case_restoration

            channel, created = await start_case_restoration(
                bot,  # type: ignore[arg-type]
                guild,
                archive,
                principal.member,  # type: ignore[arg-type]
            )
        except (discord.DiscordException, OSError, RuntimeError) as exc:
            return web.json_response(
                {
                    "error": type(exc).__name__,
                    "message": "Не удалось создать временный канал архива. Повторите после проверки Discord.",
                },
                status=503,
            )
        message = (
            "Временный канал архива создан."
            if created
            else "Этот архив уже восстановлен; открыт существующий канал."
        )
        payload = {
            "ok": True,
            "message": message,
            "channel_id": int(channel.id),
            "discord_url": f"https://discord.com/channels/{int(guild_id)}/{int(channel.id)}",
        }
        command_receipts[receipt_key] = (now + 300.0, payload)
        await asyncio.to_thread(
            activity_storage.bot_record_action,
            guild_id=int(guild_id),
            actor_id=int(principal.user_id),
            actor_display=str(principal.display_name),
            module="sgl",
            action_kind="web_archive_restore",
            target_type="sgl_archive",
            target_id=int(archive.id),
            summary=f"Восстановлен архив кейса №{case_number:03d}",
            payload={"source": "nuclear-reactor", "channel_id": int(channel.id)},
            reversible=False,
        )
        return web.json_response(payload)

    async def discord_audit(request: web.Request) -> web.Response:
        principal = await administrative_request(request)
        days = _query_int(request, "days", 7, minimum=1, maximum=365)
        stats, events = await asyncio.gather(
            asyncio.to_thread(
                dashboard_storage.admin_discord_stats,
                int(guild_id),
                days=days,
            ),
            asyncio.to_thread(
                dashboard_storage.admin_discord_events,
                int(guild_id),
                days=days,
                event_type=(str(request.query.get("type", "")).strip() or None),
                user_id=_optional_query_id(request, "user_id"),
                channel_id=_optional_query_id(request, "channel_id"),
                query=str(request.query.get("q", "")).strip() or None,
                limit=_query_int(
                    request,
                    "limit",
                    50,
                    minimum=1,
                    maximum=100,
                ),
                offset=_query_int(
                    request,
                    "offset",
                    0,
                    minimum=0,
                    maximum=50_000,
                ),
            ),
        )
        events["items"] = [
            {
                **item,
                "discord_url": _discord_message_url(
                    int(guild_id),
                    item.get("channel_id"),
                    item.get("message_id"),
                ),
            }
            for item in events["items"]
        ]
        return web.json_response(
            {
                **context(principal),
                "stats": stats,
                **events,
            }
        )

    async def registry(request: web.Request) -> web.Response:
        principal = await administrative_request(request)
        registry_data = await asyncio.to_thread(
            portal_storage.portal_registry,
            int(guild_id),
        )
        return web.json_response(
            {
                **context(principal),
                "capabilities": list(PORTAL_CAPABILITIES),
                "registry": registry_data,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def market(request: web.Request) -> web.Response:
        principal = await administrative_request(request)
        try:
            result = await asyncio.to_thread(
                portal_storage.portal_market_search,
                server_id=str(request.query.get("server", "RU15")),
                category=str(request.query.get("category", "items")),
                query=str(request.query.get("q", "")).strip() or None,
                sort=str(request.query.get("sort", "popular")),
                limit=_query_int(
                    request,
                    "limit",
                    50,
                    minimum=1,
                    maximum=100,
                ),
                offset=_query_int(
                    request,
                    "offset",
                    0,
                    minimum=0,
                    maximum=50_000,
                ),
            )
        except ValueError as exc:
            return web.json_response(
                {"error": str(exc), "message": "Некорректный фильтр рынка."},
                status=400,
            )
        alerts, alert_stats = await asyncio.gather(
            asyncio.to_thread(
                market_storage.market_list_user_alerts,
                int(principal.user_id),
                25,
            ),
            asyncio.to_thread(
                market_storage.market_alert_stats,
                result["server_id"],
                result["category"],
            ),
        )
        return web.json_response(
            {
                **context(principal),
                **result,
                "my_alerts": alerts,
                "alert_stats": alert_stats,
            }
        )

    async def market_alert_command(request: web.Request) -> web.Response:
        principal, body, idempotency_key = await command_request(request)
        now = time.monotonic()
        receipt_key = (int(principal.user_id), idempotency_key)
        cached = command_receipts.get(receipt_key)
        if cached is not None and cached[0] > now:
            return web.json_response(cached[1])
        action = str(body.get("action") or "").strip().lower()
        try:
            if action == "upsert":
                server_id = str(body.get("server_id") or "RU15")
                category = str(body.get("category") or "items")
                item_id = int(body.get("item_id") or 0)
                item = await asyncio.to_thread(
                    market_storage.market_get_item,
                    server_id,
                    item_id,
                    category,
                )
                if item is None:
                    raise ValueError("market_alert_item_not_found")
                alert = await asyncio.to_thread(
                    market_storage.market_upsert_alert,
                    discord_user_id=int(principal.user_id),
                    user_display=str(principal.display_name),
                    guild_id=int(guild_id),
                    server_id=server_id,
                    category=category,
                    item_id=item_id,
                    target_price=int(body.get("target_price") or 0),
                    min_quantity=int(body.get("min_quantity") or 0),
                    current_source_updated_at=item.get("source_updated_at"),
                )
                message = "Персональный сигнал рынка сохранён."
            elif action in {"pause", "resume"}:
                alert = await asyncio.to_thread(
                    market_storage.market_set_alert_status,
                    int(principal.user_id),
                    int(body.get("alert_id") or 0),
                    "paused" if action == "pause" else "active",
                )
                if alert is None:
                    raise ValueError("market_alert_not_found")
                message = (
                    "Сигнал приостановлен."
                    if action == "pause"
                    else "Сигнал снова активен."
                )
            elif action == "delete":
                deleted = await asyncio.to_thread(
                    market_storage.market_delete_alert,
                    int(principal.user_id),
                    int(body.get("alert_id") or 0),
                )
                if not deleted:
                    raise ValueError("market_alert_not_found")
                alert = None
                message = "Сигнал удалён."
            else:
                raise ValueError("market_alert_action_invalid")
        except (TypeError, ValueError) as exc:
            messages = {
                "market_alert_item_not_found": "Позиция отсутствует в актуальном каталоге.",
                "market_alert_not_found": "Сигнал уже удалён или недоступен.",
                "market_alert_limit_reached": "Достигнут лимит персональных сигналов.",
                "market_alert_thresholds_must_be_positive": (
                    "Цена и минимальное количество должны быть больше нуля."
                ),
                "market_alert_thresholds_too_large": "Указано слишком большое значение.",
            }
            return web.json_response(
                {
                    "error": str(exc),
                    "message": messages.get(
                        str(exc),
                        "Не удалось изменить рыночный сигнал.",
                    ),
                },
                status=400,
            )
        result = {"ok": True, "message": message, "alert": alert}
        command_receipts[receipt_key] = (now + 300.0, result)
        await asyncio.to_thread(
            activity_storage.bot_record_action,
            guild_id=int(guild_id),
            actor_id=int(principal.user_id),
            actor_display=str(principal.display_name),
            module="market",
            action_kind=f"web_market_alert_{action}",
            target_type="market_alert",
            target_id=int((alert or {}).get("id") or body.get("alert_id") or 0),
            summary=f"Изменён персональный сигнал рынка: {action}",
            payload={"source": "t-control"},
            reversible=False,
        )
        return web.json_response(result)

    async def bills_registry(request: web.Request) -> web.Response:
        principal = await administrative_request(request)
        catalog, workspaces, sessions, recent_results, schedule, schedule_history, next_plenary = await asyncio.gather(
            asyncio.to_thread(
                tvrs_storage.tvrs_public_bill_catalog,
                int(guild_id),
                250,
            ),
            asyncio.to_thread(
                workspace_storage.list_open_bill_workspaces,
                int(guild_id),
            ),
            asyncio.to_thread(
                tvrs_storage.tvrs_consensus_active_sessions,
                int(guild_id),
            ),
            asyncio.to_thread(
                tvrs_storage.tvrs_recent_live_results,
                int(guild_id),
                50,
            ),
            asyncio.to_thread(
                schedule_storage.get_upcoming_consensus_schedule,
                int(guild_id),
            ),
            asyncio.to_thread(
                schedule_storage.list_consensus_schedules,
                int(guild_id),
                limit=8,
            ),
            asyncio.to_thread(
                tvrs_storage.tvrs_get_next_plenary_number,
                int(guild_id),
                TVRS_DEFAULT_NEXT_PLENARY_NUMBER,
            ),
        )
        clean_query = str(request.query.get("q", "")).strip().casefold()
        status = str(request.query.get("status", "")).strip()
        items = []
        for bill in catalog:
            if status and str(bill.get("status") or "") != status:
                continue
            searchable = " ".join(
                (
                    str(bill.get("bill_number") or ""),
                    str(bill.get("title") or ""),
                    str(bill.get("author_display") or ""),
                    str(bill.get("summary") or ""),
                )
            ).casefold()
            if clean_query and clean_query not in searchable:
                continue
            items.append(bill)
        return web.json_response(
            {
                **context(principal),
                "items": items,
                "total": len(items),
                "workspaces": workspaces,
                "active_sessions": sessions,
                "recent_results": recent_results,
                "schedule": schedule,
                "schedule_history": schedule_history,
                "can_manage_schedule": bool(is_chair(principal.member)),
                "schedule_defaults": {
                    "plenary_number": int(next_plenary),
                    "duration_minutes": 90,
                    "voice_channel_id": int(TVRS_CONSENSUS_VOICE_CHANNEL_ID),
                },
                "voice_channels": [
                    {"id": int(channel.id), "name": str(channel.name)}
                    for channel in getattr(bot.get_guild(int(guild_id)), "voice_channels", ())
                ],
            }
        )

    async def sgl_registry(request: web.Request) -> web.Response:
        principal = await administrative_request(request)
        cases, archives = await asyncio.gather(
            asyncio.to_thread(
                portal_storage.portal_sgl_cases,
                int(guild_id),
                status=str(request.query.get("status", "")).strip() or None,
                query=str(request.query.get("q", "")).strip() or None,
                limit=_query_int(
                    request,
                    "limit",
                    50,
                    minimum=1,
                    maximum=100,
                ),
                offset=_query_int(
                    request,
                    "offset",
                    0,
                    minimum=0,
                    maximum=50_000,
                ),
            ),
            asyncio.to_thread(
                portal_storage.portal_sgl_archives,
                int(guild_id),
                query=str(request.query.get("q", "")).strip() or None,
                limit=25,
                offset=0,
            ),
        )
        return web.json_response(
            {
                **context(principal),
                "cases": cases,
                "archives": archives,
            }
        )

    async def members_registry(request: web.Request) -> web.Response:
        principal = await administrative_request(request)
        result = await asyncio.to_thread(
            portal_storage.portal_members,
            int(guild_id),
            query=str(request.query.get("q", "")).strip() or None,
            limit=_query_int(
                request,
                "limit",
                50,
                minimum=1,
                maximum=100,
            ),
            offset=_query_int(
                request,
                "offset",
                0,
                minimum=0,
                maximum=50_000,
            ),
        )
        guild = bot.get_guild(int(guild_id))
        get_member = getattr(guild, "get_member", None)
        for item in result["items"]:
            member = get_member(int(item["user_id"])) if callable(get_member) else None
            positions = _member_positions(member)
            item["legal_positions"] = positions
            item["legal_status"] = (
                str(positions[0]["label"]) if positions else "Прихожанин"
            )
        return web.json_response({**context(principal), **result})

    async def communications(request: web.Request) -> web.Response:
        principal = await administrative_request(request)
        result = await asyncio.to_thread(
            portal_storage.portal_broadcasts,
            int(guild_id),
            limit=_query_int(
                request,
                "limit",
                50,
                minimum=1,
                maximum=100,
            ),
            offset=_query_int(
                request,
                "offset",
                0,
                minimum=0,
                maximum=50_000,
            ),
        )
        return web.json_response({**context(principal), **result})

    async def send_communication(request: web.Request) -> web.Response:
        principal, body, idempotency_key = await command_request(request)
        now = time.monotonic()
        receipt_key = (int(principal.user_id), idempotency_key)
        cached = command_receipts.get(receipt_key)
        if cached is not None and cached[0] > now:
            return web.json_response(cached[1])
        if body.get("confirmed") is not True:
            return web.json_response(
                {
                    "error": "broadcast_confirmation_required",
                    "message": "Подтвердите отправку уведомления сенаторам.",
                },
                status=400,
            )
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return web.json_response(
                {
                    "error": "guild_unavailable",
                    "message": "Сервер Discord временно недоступен.",
                },
                status=503,
            )
        role = guild.get_role(int(TVRS_SENATOR_ROLE_ID))
        if role is None:
            return web.json_response(
                {
                    "error": "broadcast_senator_role_missing",
                    "message": "Роль сенатора не найдена на сервере.",
                },
                status=400,
            )
        try:
            if not getattr(guild, "chunked", True):
                await guild.chunk(cache=True)
        except discord.DiscordException as exc:
            return web.json_response(
                {
                    "error": type(exc).__name__,
                    "message": "Не удалось загрузить актуальный список сенаторов.",
                },
                status=503,
            )
        recipients = [
            (int(member.id), str(member.display_name))
            for member in getattr(role, "members", ())
            if not bool(getattr(member, "bot", False))
        ]
        if not recipients:
            return web.json_response(
                {
                    "error": "broadcast_recipients_empty",
                    "message": "На сервере нет сенаторов для отправки.",
                },
                status=400,
            )
        try:
            draft = await asyncio.to_thread(
                broadcast_storage.create_broadcast_draft,
                guild_id=int(guild_id),
                author_id=int(principal.user_id),
                author_display=str(principal.display_name),
                kind=str(body.get("kind") or "global"),
                title=str(body.get("title") or ""),
                body=str(body.get("body") or ""),
                link_url=str(body.get("link_url") or ""),
            )
            broadcast, _ = await asyncio.to_thread(
                broadcast_storage.activate_broadcast,
                int(draft["id"]),
                author_id=int(principal.user_id),
                recipients=recipients,
                delivery_topic=ADMIN_BROADCAST_TOPIC,
            )
        except ValueError as exc:
            messages = {
                "broadcast_kind_invalid": "Неизвестный тип уведомления.",
                "broadcast_title_invalid": "Заголовок должен содержать 3–180 символов.",
                "broadcast_body_invalid": "Текст должен содержать 5–3000 символов.",
                "broadcast_link_invalid": "Ссылка должна начинаться с http:// или https://.",
                "broadcast_recipients_empty": "На сервере нет сенаторов для отправки.",
            }
            return web.json_response(
                {
                    "error": str(exc),
                    "message": messages.get(
                        str(exc),
                        "Не удалось подготовить уведомление.",
                    ),
                },
                status=400,
            )
        wake_delivery_worker()
        result = {
            "ok": True,
            "message": (
                f"Уведомление поставлено в очередь для {len(recipients)} сенаторов."
            ),
            "broadcast": broadcast,
        }
        command_receipts[receipt_key] = (now + 300.0, result)
        await asyncio.to_thread(
            activity_storage.bot_record_action,
            guild_id=int(guild_id),
            actor_id=int(principal.user_id),
            actor_display=str(principal.display_name),
            module="broadcast",
            action_kind="web_broadcast_queued",
            target_type="admin_broadcast",
            target_id=int(broadcast["id"]),
            summary=(
                f"Веб-рассылка поставлена в очередь: "
                f"{str(broadcast.get('title') or '')[:180]}"
            ),
            payload={
                "source": "t-control",
                "recipient_count": len(recipients),
                "kind": str(broadcast.get("kind") or ""),
            },
            reversible=False,
        )
        return web.json_response(result)

    async def profile(request: web.Request) -> web.Response:
        principal = await administrative_request(request)
        (profile_record, characters), voice_profile = await asyncio.gather(
            asyncio.to_thread(
                profile_storage.get_profile_snapshot,
                int(guild_id),
                int(principal.user_id),
            ),
            asyncio.to_thread(
                voice_storage.get_voice_user_profile,
                int(guild_id),
                int(principal.user_id),
            ),
        )
        return web.json_response(
            {
                **context(principal),
                "legal_positions": _member_positions(principal.member),
                "profile": (
                    asdict(profile_record) if profile_record is not None else None
                ),
                "characters": [asdict(character) for character in characters],
                "voice": (asdict(voice_profile) if voice_profile is not None else None),
            }
        )

    async def update_profile_preference(
        request: web.Request,
    ) -> web.Response:
        principal, body, idempotency_key = await command_request(request)
        now = time.monotonic()
        receipt_key = (int(principal.user_id), idempotency_key)
        cached = command_receipts.get(receipt_key)
        if cached is not None and cached[0] > now:
            return web.json_response(cached[1])
        preference = str(body.get("preference") or "").strip()
        enabled = body.get("enabled")
        allowed = {
            "show_activity",
            "show_availability",
            "show_position",
            "show_characters",
            "show_join_date",
            "show_directory",
            "dm_notifications",
            "dm_market",
            "dm_craft",
            "dm_consensus",
            "dm_finance",
            "dm_system",
            "quiet_hours_enabled",
        }
        if preference not in allowed or not isinstance(enabled, bool):
            return web.json_response(
                {
                    "error": "profile_preference_invalid",
                    "message": "Не удалось распознать настройку профиля.",
                },
                status=400,
            )
        try:
            updated = await asyncio.to_thread(
                profile_storage.update_member_profile_preferences,
                int(guild_id),
                int(principal.user_id),
                **{preference: enabled},
            )
        except ValueError as exc:
            return web.json_response(
                {
                    "error": str(exc),
                    "message": (
                        "Сначала настройте начало и конец тихих часов в /account."
                        if str(exc) == "profile_quiet_hours_invalid"
                        else "Не удалось сохранить настройку профиля."
                    ),
                },
                status=400,
            )
        result = {
            "ok": True,
            "message": "Настройка профиля сохранена.",
            "profile": asdict(updated),
        }
        command_receipts[receipt_key] = (now + 300.0, result)
        await asyncio.to_thread(
            activity_storage.bot_record_action,
            guild_id=int(guild_id),
            actor_id=int(principal.user_id),
            actor_display=str(principal.display_name),
            module="profile",
            action_kind="web_profile_preference",
            target_type="member_profile",
            target_id=int(principal.user_id),
            summary=f"Изменена настройка профиля: {preference}",
            payload={"source": "t-control", "enabled": enabled},
            reversible=False,
        )
        return web.json_response(result)

    async def update_character_visibility(
        request: web.Request,
    ) -> web.Response:
        principal, body, idempotency_key = await command_request(request)
        now = time.monotonic()
        receipt_key = (int(principal.user_id), idempotency_key)
        cached = command_receipts.get(receipt_key)
        if cached is not None and cached[0] > now:
            return web.json_response(cached[1])
        try:
            character_id = int(body.get("character_id") or 0)
        except (TypeError, ValueError):
            character_id = 0
        is_public = body.get("is_public")
        if character_id <= 0 or not isinstance(is_public, bool):
            return web.json_response(
                {
                    "error": "profile_character_visibility_invalid",
                    "message": "Не удалось распознать персонажа.",
                },
                status=400,
            )
        try:
            character = await asyncio.to_thread(
                profile_storage.set_profile_character_visibility,
                int(guild_id),
                int(principal.user_id),
                character_id,
                is_public=is_public,
            )
        except ValueError as exc:
            return web.json_response(
                {
                    "error": str(exc),
                    "message": "Персонаж не найден или уже изменён.",
                },
                status=400,
            )
        result = {
            "ok": True,
            "message": (
                "Персонаж открыт в профиле."
                if is_public
                else "Персонаж скрыт из профиля."
            ),
            "character": asdict(character),
        }
        command_receipts[receipt_key] = (now + 300.0, result)
        await asyncio.to_thread(
            activity_storage.bot_record_action,
            guild_id=int(guild_id),
            actor_id=int(principal.user_id),
            actor_display=str(principal.display_name),
            module="profile",
            action_kind="web_character_visibility",
            target_type="profile_character",
            target_id=character_id,
            summary=(
                "Персонаж открыт в профиле"
                if is_public
                else "Персонаж скрыт из профиля"
            ),
            payload={"source": "t-control", "is_public": is_public},
            reversible=False,
        )
        return web.json_response(result)

    async def system(request: web.Request) -> web.Response:
        principal = await administrative_request(request)
        system_data, reliability = await asyncio.gather(
            asyncio.to_thread(
                portal_storage.portal_system,
                int(guild_id),
                limit=50,
            ),
            reliability_snapshot(force=request.query.get("fresh") == "1"),
        )
        guild = bot.get_guild(int(guild_id))
        return web.json_response(
            {
                **context(principal),
                **system_data,
                "reliability": reliability,
                "music": music_snapshot(),
                "runtime": {
                    "ready": bool(getattr(bot, "is_ready", lambda: False)()),
                    "guild_available": guild is not None,
                    "latency_ms": (
                        round(float(getattr(bot, "latency", 0.0)) * 1000)
                        if getattr(bot, "latency", None) is not None
                        else None
                    ),
                    "members_cached": len(getattr(guild, "members", ()) or ()),
                    "channels_cached": len(getattr(guild, "channels", ()) or ()),
                },
            }
        )

    async def system_action(request: web.Request) -> web.Response:
        principal, body, idempotency_key = await command_request(request)
        now = time.monotonic()
        receipt_key = (int(principal.user_id), idempotency_key)
        cached = command_receipts.get(receipt_key)
        if cached is not None and cached[0] > now:
            return web.json_response(cached[1])
        action = str(body.get("action") or "").strip().lower()
        if action == "backup_database":
            result = await asyncio.to_thread(
                create_database_backup,
                "manual",
                note=(
                    f"Nuclear Reactor · {principal.display_name} "
                    f"({principal.user_id})"
                ),
            )
            message = "Проверенная резервная копия SQLite создана."
            action_kind = "database_backup_created"
        elif action == "check_database":
            result = await asyncio.to_thread(
                check_live_database,
                full=bool(body.get("full")),
            )
            message = (
                "Проверка SQLite завершена: повреждений не найдено."
                if result.get("ok")
                else "Проверка SQLite обнаружила проблему."
            )
            action_kind = "database_integrity_checked"
        else:
            return web.json_response(
                {
                    "error": "system_action_invalid",
                    "message": "Неизвестное действие технического контура.",
                },
                status=400,
            )
        payload = {"ok": True, "message": message, "result": result}
        command_receipts[receipt_key] = (now + 300.0, payload)
        await asyncio.to_thread(
            activity_storage.bot_record_action,
            guild_id=int(guild_id),
            actor_id=int(principal.user_id),
            actor_display=str(principal.display_name),
            module="reliability",
            action_kind=action_kind,
            target_type="sqlite_database",
            target_id=str(result.get("name") or "tmod.db"),
            summary=message,
            payload={"source": "nuclear-reactor", "result": result},
            reversible=False,
        )
        return web.json_response(payload)

    async def media_status(request: web.Request) -> web.Response:
        principal = await administrative_request(request)
        return web.json_response(
            {
                **context(principal),
                "music": music_snapshot(),
            }
        )

    async def media_command(request: web.Request) -> web.Response:
        principal, body, idempotency_key = await command_request(request)
        now = time.monotonic()
        receipt_key = (int(principal.user_id), idempotency_key)
        cached = command_receipts.get(receipt_key)
        if cached is not None and cached[0] > now:
            return web.json_response(cached[1])
        target = str(body.get("target") or "").strip().lower()
        action = str(body.get("action") or "").strip().lower()
        result: dict[str, Any]
        try:
            if target == "music":
                manager = getattr(bot, "music_manager", None)
                if manager is None:
                    raise MusicRuntimeError("Музыкальный модуль пока недоступен.")
                member = principal.member
                if action == "connect":
                    guild = bot.get_guild(int(guild_id))
                    fallback_channel = getattr(guild, "system_channel", None)
                    panel_service = getattr(
                        manager,
                        "public_panel_service",
                        None,
                    )
                    if panel_service is not None and guild is not None:
                        try:
                            panel_message = await panel_service.refresh_guild(guild)
                        except (discord.DiscordException, OSError):
                            panel_message = None
                        panel_channel = getattr(
                            panel_message,
                            "channel",
                            None,
                        )
                        if panel_channel is not None:
                            fallback_channel = panel_channel
                    text_channel_id = int(
                        body.get("text_channel_id")
                        or getattr(fallback_channel, "id", 0)
                        or 0
                    )
                    if text_channel_id <= 0:
                        raise MusicRuntimeError(
                            "Не найден текстовый канал для статуса музыки."
                        )
                    await manager.connect(member, text_channel_id)
                elif action == "enqueue":
                    await manager.enqueue(member, str(body.get("query") or ""))
                elif action in {"pause", "resume", "skip", "stop", "disconnect"}:
                    await getattr(manager, action)(member)
                elif action == "volume":
                    await manager.set_volume(
                        member,
                        int(body.get("percent") or 50),
                    )
                elif action == "voice":
                    await manager.toggle_voice_control(member)
                elif action == "arm":
                    await manager.arm_voice_command(member)
                else:
                    raise ValueError("media_action_invalid")
                result = {
                    "ok": True,
                    "message": "Команда музыки выполнена.",
                    "music": music_snapshot(),
                }
            else:
                raise ValueError("media_target_invalid")
        except (
            MusicRuntimeError,
            ValueError,
            TypeError,
        ) as exc:
            return web.json_response(
                {
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
                status=400,
            )
        await asyncio.to_thread(
            activity_storage.bot_record_action,
            guild_id=int(guild_id),
            actor_id=int(principal.user_id),
            actor_display=str(principal.display_name),
            module="media",
            action_kind=f"{target}_{action}",
            target_type=target,
            target_id=int(guild_id),
            summary=f"Веб-команда {target}: {action}",
            payload={"source": "t-control"},
            reversible=False,
        )
        command_receipts[receipt_key] = (now + 300.0, result)
        return web.json_response(result)

    app.router.add_get("/admin", admin_index)
    app.router.add_get("/admin/", admin_index)
    app.router.add_get(
        "/api/admin/link/{kind}/{record_key}",
        linked_record,
    )
    app.router.add_get("/api/admin/overview", overview)
    app.router.add_get("/api/admin/actions", actions)
    app.router.add_get("/api/admin/finance", finance)
    app.router.add_post("/api/admin/finance/command", finance_command)
    app.router.add_get("/api/admin/crafts", crafts)
    app.router.add_post("/api/admin/crafts/command", craft_command)
    app.router.add_get("/api/admin/discord", discord_audit)
    app.router.add_get("/api/admin/registry", registry)
    app.router.add_get("/api/admin/market", market)
    app.router.add_post(
        "/api/admin/market/alert",
        market_alert_command,
    )
    app.router.add_get("/api/admin/bills", bills_registry)
    app.router.add_post("/api/admin/bills/command", bill_command)
    app.router.add_post(
        "/api/admin/bills/schedule",
        consensus_schedule_command,
    )
    app.router.add_get("/api/admin/sgl", sgl_registry)
    app.router.add_post("/api/admin/sgl/command", sgl_command)
    app.router.add_get("/api/admin/members", members_registry)
    app.router.add_get("/api/admin/communications", communications)
    app.router.add_post(
        "/api/admin/communications/send",
        send_communication,
    )
    app.router.add_get("/api/admin/profile", profile)
    app.router.add_post(
        "/api/admin/profile/preference",
        update_profile_preference,
    )
    app.router.add_post(
        "/api/admin/profile/character",
        update_character_visibility,
    )
    app.router.add_get("/api/admin/system", system)
    app.router.add_post("/api/admin/system/action", system_action)
    app.router.add_get("/api/admin/access", access_control)
    app.router.add_post("/api/admin/access", access_control)
    app.router.add_get("/api/admin/access/self", access_self)
    app.router.add_get("/api/admin/media", media_status)
    app.router.add_post("/api/admin/media/command", media_command)

    warm_tasks: list[asyncio.Task[None]] = []

    async def warm_overview() -> None:
        try:
            await overview_cache.get(30, lambda: build_overview_snapshot(30))
        except Exception:
            # A cache warmup must never prevent the web server from starting.
            pass

    async def begin_warmup(_: web.Application) -> None:
        warm_tasks.append(asyncio.create_task(warm_overview()))

    async def stop_warmup(_: web.Application) -> None:
        for task in warm_tasks:
            if not task.done():
                task.cancel()
        if warm_tasks:
            await asyncio.gather(*warm_tasks, return_exceptions=True)

    app.on_startup.append(begin_warmup)
    app.on_cleanup.append(stop_warmup)


__all__ = [
    "AuthenticatedRequest",
    "register_admin_web_routes",
]
