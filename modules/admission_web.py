"""Public Phoenix portal and authenticated admission application API."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import discord
from aiohttp import web

from modules.admission import evaluate_answers, notify_ovr_desks, public_questions
from modules.consensus_web_auth import csrf_matches, resolve_principal
from modules.delivery_runtime import wake_delivery_worker
from modules.tvrs_config import TVRS_SENATOR_ROLE_ID
from persistence import admission_repository as admission_storage
from persistence import profile_repository as profile_storage


logger = logging.getLogger(__name__)


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
        payload.update(
            {
                "account_required": not bool(characters),
                "already_senator": int(TVRS_SENATOR_ROLE_ID) in role_ids,
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
        if int(TVRS_SENATOR_ROLE_ID) in role_ids:
            return web.json_response(
                {
                    "error": "admission_already_senator",
                    "message": "У вас уже есть мандат сенатора Товарищества.",
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
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            body = None
        if not isinstance(body, dict):
            return web.json_response(
                {"error": "invalid_payload", "message": "Форма повреждена."},
                status=400,
            )
        try:
            answers, traits = evaluate_answers(body.get("answers"))
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
                await notify_ovr_desks(guild, detail["application"])
            except Exception:  # noqa: BLE001 - application is already durable
                logger.exception("Could not project Phoenix application to OVR desks")
        wake_delivery_worker()
        return web.json_response(response_payload, status=201)

    app.router.add_get("/admission", index)
    app.router.add_get("/admission/", index)
    app.router.add_get("/admission-assets/{name}", asset)
    app.router.add_get("/api/admission", bootstrap)
    app.router.add_post("/api/admission", submit)


__all__ = ["register_admission_web_routes"]
