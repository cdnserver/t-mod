"""Public Phoenix portal and authenticated admission application API."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import discord
from aiohttp import web

from modules.admission import (
    ensure_fellowship_role,
    evaluate_answers,
    notify_chair_desks,
    notify_ovr_desks,
    public_questions,
)
from modules.consensus_web_auth import csrf_matches, resolve_principal
from modules.delivery_runtime import wake_delivery_worker
from modules.tvrs_config import TVRS_FELLOWSHIP_ROLE_ID, TVRS_SENATOR_ROLE_ID
from modules.tvrs_presentation import is_chair
from persistence import admission_repository as admission_storage
from persistence import profile_repository as profile_storage


logger = logging.getLogger(__name__)


def _can_review(principal: Any) -> bool:
    if bool(getattr(principal, "administrator", False)):
        return True
    member = getattr(principal, "member", None)
    if member is None or not hasattr(member, "guild_permissions"):
        return False
    return bool(is_chair(member))


def _character_payload(character: Any) -> dict[str, Any]:
    return {
        "id": int(getattr(character, "id", 0) or 0),
        "nickname": str(getattr(character, "nickname", "") or ""),
        "static_id": str(getattr(character, "static_id", "") or ""),
        "position": int(getattr(character, "position", 0) or 0),
    }


def register_admission_web_routes(
    app: web.Application,
    bot: discord.Client,
    *,
    guild_id: int,
    asset_dir: Path,
) -> None:
    receipts: dict[tuple[int, str], dict[str, Any]] = {}

    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(asset_dir / "admission.html")

    async def asset(request: web.Request) -> web.FileResponse:
        name = str(request.match_info.get("name") or "")
        if name not in {
            "admission.css",
            "admission.js",
            "phoenix-senate-banner.webp",
        }:
            raise web.HTTPNotFound()
        response = web.FileResponse(asset_dir / name)
        if name.endswith(".webp"):
            response.content_type = "image/webp"
        return response

    async def bootstrap(request: web.Request) -> web.Response:
        principal = await resolve_principal(request, bot, guild_id=int(guild_id))
        payload: dict[str, Any] = {
            "authenticated": principal is not None,
            "account_required": principal is None,
            "questions": public_questions(),
            "application": None,
            "events": [],
            "characters": [],
            "csrf_token": None,
            "viewer": None,
            "can_review": False,
            "pending_reviews": [],
        }
        if principal is None:
            return web.json_response(payload)
        characters, detail = await asyncio.gather(
            asyncio.to_thread(
                profile_storage.list_profile_characters,
                int(guild_id),
                int(principal.user_id),
            ),
            asyncio.to_thread(
                admission_storage.application_detail,
                int(guild_id),
                int(principal.user_id),
            ),
        )
        role_ids = {
            int(getattr(role, "id", 0) or 0)
            for role in getattr(principal.member, "roles", ())
        }
        can_review = _can_review(principal)
        pending_reviews = (
            await asyncio.to_thread(
                admission_storage.pending_leadership_applications,
                int(guild_id),
            )
            if can_review
            else []
        )
        payload.update(
            {
                "account_required": not bool(characters),
                "already_senator": int(TVRS_SENATOR_ROLE_ID) in role_ids,
                "already_member": int(TVRS_FELLOWSHIP_ROLE_ID) in role_ids,
                "can_review": can_review,
                "pending_reviews": pending_reviews,
                "viewer": {
                    "id": int(principal.user_id),
                    "name": str(principal.display_name),
                    "tier": str(principal.account_tier),
                },
                "csrf_token": str(principal.csrf_token),
                "characters": [_character_payload(item) for item in characters[:3]],
                "application": detail["application"] if detail else None,
                "events": detail["events"] if detail else [],
            }
        )
        response = web.json_response(payload)
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        return response

    async def submit(request: web.Request) -> web.Response:
        principal = await resolve_principal(request, bot, guild_id=int(guild_id))
        if principal is None:
            return web.json_response(
                {
                    "error": "admission_account_required",
                    "message": "Сначала войдите в T-Mod аккаунт.",
                },
                status=401,
            )
        if not csrf_matches(request, principal):
            return web.json_response(
                {
                    "error": "csrf_failed",
                    "message": "Сессия обновилась. Перезагрузите страницу и повторите.",
                },
                status=403,
            )
        idempotency_key = str(request.headers.get("X-Idempotency-Key") or "").strip()
        if not 12 <= len(idempotency_key) <= 120:
            return web.json_response(
                {
                    "error": "idempotency_key_required",
                    "message": "Форма не получила ключ защиты от повторной отправки.",
                },
                status=400,
            )
        receipt_key = (int(principal.user_id), idempotency_key)
        if receipt_key in receipts:
            return web.json_response(receipts[receipt_key])
        role_ids = {
            int(getattr(role, "id", 0) or 0)
            for role in getattr(principal.member, "roles", ())
        }
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            body = None
        if not isinstance(body, dict):
            return web.json_response(
                {"error": "invalid_payload", "message": "Форма повреждена."},
                status=400,
            )
        # Cached v2 clients did not send the field and represented the Senate
        # flow exclusively, so omission deliberately keeps the old behaviour.
        application_kind = str(
            body.get("application_kind") or "senate"
        ).strip().lower()
        if application_kind not in admission_storage.ADMISSION_KINDS:
            return web.json_response(
                {
                    "error": "admission_kind_invalid",
                    "message": "Выберите: Товарищество или Сенат Товарищества.",
                },
                status=400,
            )
        if application_kind == "senate" and int(TVRS_SENATOR_ROLE_ID) in role_ids:
            return web.json_response(
                {
                    "error": "admission_already_senator",
                    "message": "У вас уже есть мандат сенатора Товарищества.",
                },
                status=409,
            )
        if application_kind == "community" and int(TVRS_FELLOWSHIP_ROLE_ID) in role_ids:
            return web.json_response(
                {
                    "error": "admission_already_member",
                    "message": "Вы уже являетесь участником Товарищества.",
                },
                status=409,
            )
        account_characters = await asyncio.to_thread(
            profile_storage.list_profile_characters,
            int(guild_id),
            int(principal.user_id),
        )
        if not account_characters:
            return web.json_response(
                {
                    "error": "admission_character_required",
                    "message": "Добавьте хотя бы одного персонажа через /account в ЛС с T-Mod.",
                },
                status=409,
            )
        try:
            answers, traits = (
                evaluate_answers(body.get("answers"))
                if application_kind == "senate"
                else ({}, {})
            )
            detail = await asyncio.to_thread(
                admission_storage.submit_application,
                guild_id=int(guild_id),
                user_id=int(principal.user_id),
                user_display=str(principal.display_name),
                forum_url=str(body.get("forum_url") or ""),
                characters=body.get("characters"),
                answers=answers,
                traits=traits,
                motivation=str(body.get("motivation") or ""),
                contribution=str(body.get("contribution") or ""),
                availability=str(body.get("availability") or ""),
                application_kind=application_kind,
            )
        except ValueError as exc:
            code = str(exc)
            messages = {
                "admission_application_already_exists": "Заявка уже существует. Откройте её статус на этой странице.",
                "admission_answers_incomplete": "Ответьте на каждый вопрос ситуационного теста.",
                "admission_forum_url_invalid": "Укажите полную ссылку на форумный профиль.",
                "admission_characters_invalid": "Укажите от одного до трёх персонажей.",
                "admission_character_nickname_invalid": "У каждого персонажа должны быть имя и фамилия.",
                "admission_character_static_invalid": "Статик должен состоять только из цифр.",
                "admission_character_static_duplicate": "У персонажей не может повторяться статик.",
                "admission_motivation_invalid": "Расскажите подробнее, почему хотите вступить.",
                "admission_contribution_invalid": "Опишите, чем сможете быть полезны.",
                "admission_availability_invalid": "Укажите вашу доступность.",
                "admission_kind_invalid": "Выберите траекторию вступления.",
            }
            status = 409 if code == "admission_application_already_exists" else 400
            return web.json_response(
                {
                    "error": code,
                    "message": messages.get(code, "Проверьте заполнение анкеты."),
                },
                status=status,
            )
        response_payload = {
            "ok": True,
            "application": detail["application"],
            "events": detail["events"],
        }
        receipts[receipt_key] = response_payload
        if len(receipts) > 500:
            receipts.pop(next(iter(receipts)))
        get_guild = getattr(bot, "get_guild", None)
        guild = get_guild(int(guild_id)) if callable(get_guild) else None
        if guild is not None:
            try:
                if application_kind == "community":
                    await notify_chair_desks(guild, detail["application"])
                else:
                    await notify_ovr_desks(guild, detail["application"])
            except Exception:  # noqa: BLE001 - application is already durable
                logger.exception("Could not project Phoenix application to review desks")
        wake_delivery_worker()
        return web.json_response(response_payload, status=201)

    async def review(request: web.Request) -> web.Response:
        principal = await resolve_principal(request, bot, guild_id=int(guild_id))
        if principal is None:
            return web.json_response(
                {"error": "admission_account_required", "message": "Требуется вход."},
                status=401,
            )
        if not _can_review(principal):
            return web.json_response(
                {
                    "error": "admission_chair_required",
                    "message": "Решения по заявкам принимает Совет председателей.",
                },
                status=403,
            )
        if not csrf_matches(request, principal):
            return web.json_response(
                {"error": "csrf_failed", "message": "Сессия обновилась."},
                status=403,
            )
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            body = None
        if not isinstance(body, dict):
            return web.json_response(
                {"error": "invalid_payload", "message": "Решение повреждено."},
                status=400,
            )
        action = str(body.get("action") or "").strip().lower()
        if action not in {"approve", "deny"}:
            return web.json_response(
                {"error": "admission_decision_invalid", "message": "Выберите решение."},
                status=400,
            )
        try:
            application, changed = await asyncio.to_thread(
                admission_storage.record_leadership_decision,
                int(body.get("application_id") or 0),
                guild_id=int(guild_id),
                approved=action == "approve",
                actor_id=int(principal.user_id),
                actor_display=str(principal.display_name),
                note=str(body.get("note") or ""),
            )
        except ValueError as exc:
            messages = {
                "admission_decision_note_required": "Добавьте краткую мотивировку решения.",
                "admission_application_not_found": "Заявка не найдена.",
                "admission_leadership_kind_invalid": "Эта заявка относится к Сенату.",
                "admission_leadership_transition_invalid": "Заявка уже рассмотрена.",
            }
            return web.json_response(
                {"error": str(exc), "message": messages.get(str(exc), "Решение не сохранено.")},
                status=409,
            )
        role_projected = False
        guild = bot.get_guild(int(guild_id)) if callable(getattr(bot, "get_guild", None)) else None
        if changed and action == "approve" and guild is not None:
            try:
                role_projected = await ensure_fellowship_role(guild, application)
            except RuntimeError:
                logger.exception("Fellowship role projection will be retried")
        wake_delivery_worker()
        return web.json_response(
            {
                "ok": True,
                "changed": changed,
                "role_projected": role_projected,
                "application": application,
            }
        )

    app.router.add_get("/admission", index)
    app.router.add_get("/admission/", index)
    app.router.add_get("/admission-assets/{name}", asset)
    app.router.add_get("/api/admission", bootstrap)
    app.router.add_post("/api/admission", submit)
    app.router.add_post("/api/admission/review", review)


__all__ = ["register_admission_web_routes"]
