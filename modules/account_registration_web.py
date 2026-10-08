"""Public account wizard; the browser secret never leaves its HttpOnly cookie."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import time
from collections import deque
from pathlib import Path

import discord
from aiohttp import web

from modules.consensus_web_auth import has_trusted_forwarded_host, request_public_host, request_public_secure
from persistence import account_registration_repository as registration
from persistence import global_ban_repository as bans
from persistence import profile_repository as profiles

COOKIE = "tmod_registration"
API = "/api/account-registration"
ERRORS = {
    "registration_rate_limited": "Слишком много новых кодов. Попробуйте позже или используйте уже выданный код.",
    "registration_expired": "Время мастера истекло. Получите новый код и подтвердите Discord заново.",
    "registration_discord_required": "Сначала подтвердите код командой /master в Discord.",
    "registration_account_exists": "У этого Discord уже есть аккаунт. Войдите в него; мастер не меняет существующие пароли.",
    "registration_blocked": "Создание аккаунта недоступно. Обратитесь к администрации.",
    "registration_membership_required": "Сначала вступите в Discord Товарищества, затем повторите проверку.",
    "web_login_invalid": "Логин: от 3 до 32 латинских букв, цифр, точек, дефисов или подчёркиваний. Начните с буквы или цифры.",
    "web_login_taken": "Этот логин уже занят. Выберите другой.",
    "web_pin_invalid": "PIN должен состоять ровно из 8 цифр.",
    "password_length": "Пароль должен содержать от 12 до 128 символов.",
    "profile_preferred_name_invalid": "Как к вам обращаться: от 2 до 24 символов.",
    "profile_character_full_name_required": "Укажите имя и фамилию персонажа через пробел.",
    "profile_nickname_invalid": "Ник персонажа: от 2 до 48 символов.",
    "profile_static_invalid": "Статик: от 1 до 12 цифр.",
    "profile_static_taken": "Один из статиков уже привязан или повторяется. Проверьте персонажей.",
    "profile_character_limit": "В аккаунте может быть не более трёх персонажей, включая уже добавленных.",
    "registration_conflict": "Данные изменились во время сохранения. Проверьте аккаунт или выберите другой логин.",
}


def registration_remote(request: web.Request) -> str:
    """Walk the proxy chain from the trusted gateway, never from a spoofable prefix."""
    remote = str(request.remote or "unknown")
    if not has_trusted_forwarded_host(request):
        return remote
    chain = request.headers.get("X-Forwarded-For", "").split(",")
    if len(chain) > 8:
        return remote
    for item in reversed(chain):
        try:
            address = ipaddress.ip_address(item.strip())
        except ValueError:
            return remote
        if not (address.is_private or address.is_loopback or address.is_link_local):
            return str(address)
    return remote


async def verified_discord_member(bot, guild_id: int, user_id: int):
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        raise web.HTTPServiceUnavailable(text="Discord временно недоступен. Повторите позже.")
    # Fetch rather than trust a stale member cache after somebody leaves.
    try:
        return await asyncio.wait_for(guild.fetch_member(int(user_id)), timeout=8)
    except discord.NotFound:
        raise ValueError("registration_membership_required") from None
    except (discord.HTTPException, asyncio.TimeoutError):
        raise web.HTTPServiceUnavailable(text="Не удалось проверить Discord. Повторите позже.") from None


def register_account_registration_routes(app: web.Application, bot, *, guild_id: int, asset_dir: Path) -> None:
    attempts: dict[str, deque] = {}

    def fail(code: str, status: int = 400) -> web.Response:
        return web.json_response({"error": code, "message": ERRORS.get(code, "Не удалось выполнить шаг. Проверьте данные и повторите.")}, status=status, headers={"Cache-Control": "no-store"})

    def same_origin(request: web.Request) -> bool:
        origin = request.headers.get("Origin", "")
        expected = ("https" if request_public_secure(request) else "http") + "://" + request_public_host(request)
        return origin == expected and request.content_type == "application/json"

    def csrf(token: str) -> str:
        return registration.digest("registration-csrf:" + token)

    async def page(_: web.Request):
        return web.FileResponse(asset_dir / "register.html")

    async def start(request: web.Request):
        if not same_origin(request):
            return fail("origin_failed", 403)
        now = time.monotonic()
        for key in list(attempts):
            while attempts[key] and attempts[key][0] < now - 3600:
                attempts[key].popleft()
            if not attempts[key]:
                del attempts[key]
        remote = registration_remote(request)
        bucket = attempts.setdefault(remote, deque(maxlen=8))
        if len(bucket) >= 8 or len(attempts) > 10000:
            return fail("registration_rate_limited", 429)
        bucket.append(now)
        try:
            created = await asyncio.to_thread(registration.start_registration, guild_id, previous_token=request.cookies.get(COOKIE, ""))
        except ValueError as exc:
            return fail(str(exc), 503)
        token = created.pop("browser_token")
        response = web.json_response({**created, "csrf_token": csrf(token), "status": "waiting"}, status=201, headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE, token, path=API, max_age=registration.LIFETIME, httponly=True,
                            secure=request_public_secure(request), samesite="Strict")
        return response

    async def state(request: web.Request):
        token = request.cookies.get(COOKIE, "")
        if not token or len(token) > 100:
            return fail("registration_expired", 410)
        try:
            result = await asyncio.to_thread(registration.registration_status, guild_id, token)
            if result["user_id"] and result["status"] == "claimed":
                if await asyncio.to_thread(bans.is_globally_banned, guild_id, int(result["user_id"])):
                    return fail("registration_blocked", 423)
                characters = await asyncio.to_thread(profiles.list_profile_characters, guild_id, int(result["user_id"]))
                result["characters"] = [{"nickname": item.nickname, "static_id": item.static_id} for item in characters]
        except ValueError as exc:
            return fail(str(exc), 410)
        return web.json_response({**result, "csrf_token": csrf(token)}, headers={"Cache-Control": "no-store"})

    async def finish(request: web.Request):
        token = request.cookies.get(COOKIE, "")
        if not same_origin(request) or not token or len(token) > 100 or not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), csrf(token)):
            return fail("csrf_failed", 403)
        if request.content_length and request.content_length > 12000:
            return fail("invalid_payload", 413)
        try:
            raw = bytearray()
            async with asyncio.timeout(10):
                async for chunk in request.content.iter_chunked(4096):
                    raw.extend(chunk)
                    if len(raw) > 12000:
                        return fail("invalid_payload", 413)
            body = json.loads(raw)
            if not isinstance(body, dict) or any(not isinstance(body.get(key), str) for key in ("login", "password", "kind", "preferred_name")):
                return fail("invalid_payload")
            if body.get("consent") is not True:
                return fail("registration_consent_required")
            status = await asyncio.to_thread(registration.registration_status, guild_id, token)
            if status["status"] != "completed":
                if not status["user_id"]:
                    return fail("registration_discord_required", 409)
                await verified_discord_member(bot, guild_id, int(status["user_id"]))
            result = await asyncio.to_thread(registration.complete_registration, guild_id, token,
                login=body["login"], secret=body["password"], kind=body["kind"], preferred_name=body["preferred_name"], characters=body.get("characters", []))
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return fail("invalid_payload")
        except asyncio.TimeoutError:
            return fail("request_timeout", 408)
        except ValueError as exc:
            code = str(exc)
            return fail(code, 410 if code == "registration_expired" else 423 if code == "registration_blocked" else 409 if code in {"web_login_taken", "registration_account_exists", "registration_conflict"} else 400)
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    app.router.add_get("/register", page)
    app.router.add_post(API + "/start", start)
    app.router.add_get(API + "/status", state)
    app.router.add_post(API + "/complete", finish)
