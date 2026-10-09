"""Unified account login routes; independent of the Senate page/controller."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import discord
from aiohttp import web

from modules.account_security_web import issue_challenge, FactorDeliveryError
from modules.consensus_web_auth import (
    ConsensusWebAuthError, PERSISTENT_SESSION_LIFETIME_SECONDS,
    clear_session_cookie, consume_entry_ticket, create_session_token,
    member_has_fellowship_access, request_public_host, request_public_secure,
    resolve_principal, set_session_cookie, signed_session_identity,
)
from modules.global_log_runtime import emit_global_event
from persistence import account_security_repository as security_storage
from persistence import web_auth_repository as credential_storage
from persistence import reactor_repository as reactor_storage
from persistence import global_ban_repository as global_ban_storage
from persistence import telegram_repository as telegram_storage


def register_account_auth_routes(
    app: web.Application, bot, *, guild_id: int, asset_dir: Path,
    request_remote, login_failures, secure_default: bool = False,
) -> None:
    """Register existing client-compatible entry points in one account module."""
    async def login_page(request: web.Request) -> web.StreamResponse:
        next_path = (
            str(request.query.get("next"))
            if request.query.get("next") in {"/admin", "/reactor", "/atlas", "/atlas-billing", "/account", "/account-home", "/games", "/sgl", "/ovr", "/host", "/tasks", "/admission"}
            else "/account-home" if request_public_host(request).split(":", 1)[0].lower() == "account.tvr.lat" else "/"
        )
        principal = await resolve_principal(request, bot, guild_id=int(guild_id))
        if principal is not None:
            if not principal.guild_member and next_path not in {"/atlas", "/atlas-billing", "/account", "/account-home", "/admission"}:
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
        return web.FileResponse(asset_dir / "login.html")

    async def ticket_login(request: web.Request) -> web.Response:
        try:
            _, user_id = consume_entry_ticket(
                request.query.get("ticket", ""),
                expected_guild_id=int(guild_id),
            )
        except ConsensusWebAuthError as exc:
            if str(exc) == "ticket_storage_unavailable":
                raise web.HTTPServiceUnavailable(
                    text="T-Mod временно не может безопасно подтвердить ссылку. Повторите через минуту."
                ) from exc
            raise web.HTTPUnauthorized(
                text="Ссылка недействительна или уже использована. Откройте новую из Discord."
            ) from exc
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
            if request.query.get("next") in {"/admin", "/reactor", "/atlas", "/atlas-billing", "/account", "/account-home", "/games", "/sgl", "/ovr", "/host", "/tasks", "/admission"}
            else f"/?mode={mode}"
        )
        if destination == "/host" and mode == "simulation":
            destination = "/host?mode=simulation"
        if (
            destination in {"/reactor", "/games", "/tasks"}
            and not member_has_fellowship_access(member)
        ):
            destination = "/admission"
        response = web.Response(
            status=302,
            headers={"Location": destination},
        )
        set_session_cookie(
            response,
            token,
            secure=bool(secure_default or request_public_secure(request)),
            request_host=request_public_host(request),
        )
        return response

    async def telegram_code_login(request: web.Request) -> web.Response:
        """Authenticate with a one-time code delivered to the user's linked Telegram."""

        remote = request_remote(request)
        now = asyncio.get_running_loop().time()
        attempts = login_failures[remote]
        while attempts and now - attempts[0] > 10 * 60:
            attempts.popleft()
        allowed_next = {
            "/admin", "/reactor", "/atlas", "/atlas-billing", "/account", "/account-home",
            "/games", "/sgl", "/ovr", "/host", "/tasks", "/admission",
        }
        next_path = str(request.query.get("next") or "/")
        if next_path not in allowed_next:
            next_path = "/"
        if len(attempts) >= 8:
            raise web.HTTPSeeOther(
                location=f"/login?{urlencode({'next': next_path, 'error': 'locked'})}"
            )
        try:
            body = await request.post()
        except (ValueError, web.HTTPException):
            body = {}
        code = str(body.get("code") or "")[:16]
        try:
            result = await asyncio.to_thread(
                telegram_storage.consume_telegram_login_code,
                code,
                guild_id=int(guild_id),
            )
        except Exception as exc:
            raise web.HTTPServiceUnavailable(
                text="Вход по Telegram временно недоступен. Повторите попытку позже."
            ) from exc
        if not result.get("ok"):
            attempts.append(now)
            error = "locked" if len(attempts) >= 8 else "telegram_invalid"
            raise web.HTTPSeeOther(
                location=f"/login?{urlencode({'next': next_path, 'error': error})}"
            )
        user_id = int(result["discord_user_id"])
        if await asyncio.to_thread(
            global_ban_storage.is_globally_banned,
            int(guild_id),
            user_id,
        ):
            raise web.HTTPSeeOther(location="/banned")
        credential = await asyncio.to_thread(
            credential_storage.get_web_credential,
            int(guild_id),
            user_id,
        )
        if credential is None:
            raise web.HTTPSeeOther(
                location=f"/login?{urlencode({'next': next_path, 'error': 'account_missing'})}"
            )

        guild = bot.get_guild(int(guild_id))
        member = guild.get_member(user_id) if guild is not None else None
        if member is None and guild is not None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.DiscordException:
                member = None
        if guild is None and next_path not in {"/atlas", "/atlas-billing", "/account", "/account-home", "/admission"}:
            raise web.HTTPServiceUnavailable(text="Сервер Discord пока недоступен.")
        if member is None and next_path not in {"/atlas", "/atlas-billing", "/account", "/account-home", "/admission"}:
            raise web.HTTPSeeOther(
                location=f"/login?{urlencode({'next': next_path, 'error': 'membership'})}"
            )
        if (
            member is not None
            and next_path in {"/reactor", "/games", "/tasks"}
            and not member_has_fellowship_access(member)
        ):
            next_path = "/admission"

        grants = await asyncio.to_thread(
            credential_storage.web_section_grants,
            int(guild_id),
            user_id,
        )
        sections = {str(item["section"]) for item in grants}
        is_administrator = bool(member and member.guild_permissions.administrator)
        if next_path == "/admin" and not is_administrator and not (sections - {"atlas_ai"}):
            raise web.HTTPSeeOther(
                location=f"/login?{urlencode({'next': next_path, 'error': 'administrator'})}"
            )
        if next_path == "/atlas" and not is_administrator and "atlas_ai" not in sections:
            raise web.HTTPSeeOther(
                location=f"/login?{urlencode({'next': next_path, 'error': 'atlas_access'})}"
            )

        attempts.clear()
        token, _ = create_session_token(
            guild_id=int(guild_id),
            user_id=user_id,
            lifetime_seconds=PERSISTENT_SESSION_LIFETIME_SECONDS,
            session_version=int(credential.session_version),
        )
        response = web.Response(status=303, headers={"Location": next_path})
        set_session_cookie(
            response,
            token,
            secure=bool(secure_default or request_public_secure(request)),
            max_age=PERSISTENT_SESSION_LIFETIME_SECONDS,
            request_host=request_public_host(request),
        )
        emit_global_event({
            "event_type": "auth.telegram_login.success",
            "summary": "Вход в T‑Mod выполнен через одноразовый Telegram-код",
            "guild_id": int(guild_id),
            "actor_user_id": user_id,
            "details": {"destination": next_path, "remote": remote},
        })
        return response

    async def credential_login_impl(request: web.Request) -> web.Response:
        if request.query.get("client") == "browser":
            origin = ("https" if request_public_secure(request) else "http") + "://" + request_public_host(request)
            if request.headers.get("Origin") != origin:
                return web.json_response({"ok": False, "error": "origin_failed"}, status=403, headers={"Cache-Control": "no-store"})
        remote = request_remote(request)
        now = asyncio.get_running_loop().time()
        desktop_client = request.query.get("client") == "desktop"
        attempts = login_failures[remote]
        while attempts and now - attempts[0] > 10 * 60:
            attempts.popleft()
        next_path = (
            str(request.query.get("next"))
            if request.query.get("next") in {"/admin", "/reactor", "/atlas", "/atlas-billing", "/account", "/account-home", "/games", "/sgl", "/ovr", "/host", "/tasks", "/admission"}
            else "/account-home" if request.query.get("client") == "browser" else "/"
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
            str(body.get("pin") or "")[:128],
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
        desktop_token = str(
            request.headers.get("X-TMod-Install-Token") or ""
        ).strip()
        if desktop_token:
            await asyncio.to_thread(
                global_ban_storage.bind_desktop_installation,
                int(guild_id),
                int(result.credential.user_id),
                desktop_token,
                platform=str(request.headers.get("X-TMod-Desktop-Platform") or ""),
                app_version=str(request.headers.get("X-TMod-Desktop-Version") or ""),
                device_fingerprint=str(request.headers.get("X-TMod-Device-Fingerprint") or ""),
                hardware_fingerprint=str(request.headers.get("X-TMod-Hardware-Fingerprint") or ""),
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
        if (
            not desktop_client
            and member is not None
            and next_path in {"/reactor", "/games", "/tasks"}
            and not member_has_fellowship_access(member)
        ):
            next_path = "/admission"
        if member is None and not desktop_client:
            if next_path in {"/admission", "/atlas-billing", "/account", "/account-home"}:
                pass
            elif next_path != "/atlas" or "atlas_ai" not in sections:
                raise web.HTTPSeeOther(
                    location="/login?next=%2Fatlas&error=atlas_access"
                )
        elif next_path == "/admin" and not is_administrator:
            if not (sections - {"atlas_ai"}):
                raise web.HTTPSeeOther(
                    location="/login?next=%2Fadmin&error=administrator"
                )
        factor = await asyncio.to_thread(security_storage.state, int(guild_id), int(result.credential.user_id))
        blackbird_login = desktop_client and str(request.headers.get("X-TMod-Desktop-Edition", "")).strip().lower() == "blackbird"
        if factor["mfa_method"] and blackbird_login:
            nonce = str(body.get("challenge") or "")[:64]
            code = str(body.get("code") or "")[:32]
            try:
                valid = nonce and code and await asyncio.to_thread(security_storage.verify, int(guild_id), int(result.credential.user_id), code, nonce, "login")
            except ValueError:
                return web.json_response({"error": "factor_temporarily_unavailable"}, status=503, headers={"Cache-Control": "no-store"})
            if not valid:
                attempts.append(now)
                try:
                    if not nonce:
                        nonce = await issue_challenge(bot, int(guild_id), int(result.credential.user_id), "login", factor["mfa_method"])
                except FactorDeliveryError as exc:
                    return web.json_response({"error": "two_factor_required", "challenge": exc.nonce, "method": factor["mfa_method"], "delivery_failed": True}, status=202, headers={"Cache-Control": "no-store"})
                except ValueError:
                    return web.json_response({"error": "factor_delivery_unavailable"}, status=503)
                return web.json_response({"error": "two_factor_required", "challenge": nonce, "method": factor["mfa_method"]}, status=202, headers={"Cache-Control": "no-store"})
        attempts.clear()
        token, _ = create_session_token(
            guild_id=int(guild_id),
            user_id=int(result.credential.user_id),
            lifetime_seconds=PERSISTENT_SESSION_LIFETIME_SECONDS,
            session_version=int(result.credential.session_version),
            mfa_verified=blackbird_login,
            account_security_version=factor["security_version"],
        )
        response = web.Response(
            status=303,
            headers={"Location": next_path},
        )
        set_session_cookie(
            response,
            token,
            secure=bool(secure_default or request_public_secure(request)),
            max_age=PERSISTENT_SESSION_LIFETIME_SECONDS,
            request_host=request_public_host(request),
        )
        return response

    async def credential_login(request: web.Request) -> web.Response:
        # Desktop authentication is a JSON transaction, not a page navigation.
        # Chromium can filter manual cross-origin redirect responses; retain
        # Set-Cookie but avoid making a redirect the proof of a successful login.
        try:
            response = await credential_login_impl(request)
        except web.HTTPSeeOther as redirect:
            if request.query.get("client") not in {"desktop", "browser"}:
                raise
            target = urlsplit(redirect.location)
            error = parse_qs(target.query).get("error", [""])[0]
            allowed = {"invalid", "locked", "reset_required", "character_required",
                       "atlas_access", "administrator", "membership"}
            if target.path == "/banned":
                error, status = "banned", 423
            else:
                error = error if error in allowed else "login_failed"
                status = 429 if error == "locked" else 401
            return web.json_response({"ok": False, "error": error}, status=status,
                                     headers={"Cache-Control": "no-store"})
        if request.query.get("client") in {"desktop", "browser"} and response.status == 303:
            destination = response.headers.get("Location", "/account-home")
            response.set_status(200)
            response.headers.pop("Location", None)
            response.content_type = "application/json"
            response.text = json.dumps({"ok": True, **({"destination": destination} if request.query.get("client") == "browser" else {})})
            response.headers["Cache-Control"] = "no-store"
        return response

    async def logout(request: web.Request) -> web.Response:
        public_host = request_public_host(request)
        host = public_host.split(":", 1)[0].lower()
        identity = signed_session_identity(request, expected_guild_id=int(guild_id))
        if identity is not None:
            await asyncio.to_thread(
                credential_storage.invalidate_web_sessions,
                int(identity[0]),
                int(identity[1]),
            )
        destination = (
            "/login?next=/admin"
            if host.startswith("reactor.")
            else "/login?next=/ovr"
            if host.startswith("ovr.")
            else "/login?next=/reactor"
            if host == "tvr.lat"
            else "/login?next=/account-home"
            if host == "account.tvr.lat"
            else "/"
        )
        response = web.Response(
            status=302,
            headers={"Location": destination},
        )
        clear_session_cookie(
            response,
            secure=bool(secure_default or request_public_secure(request)),
            request_host=public_host,
        )
        return response

    app.router.add_get("/login", login_page)
    app.router.add_get("/auth/ticket", ticket_login)
    app.router.add_post("/auth/telegram", telegram_code_login)
    app.router.add_post("/auth/login", credential_login)
    app.router.add_get("/auth/logout", logout)
