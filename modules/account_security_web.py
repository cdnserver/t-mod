"""Authenticated security settings and delivery of verification challenges."""
from __future__ import annotations

import asyncio
import json
import os
from aiohttp import ClientSession, ClientTimeout, web
from persistence import account_security_repository as security
from persistence import web_auth_repository as credentials
from persistence import telegram_repository as telegram
from persistence import atlas_billing_repository as billing
from modules.consensus_web_auth import csrf_matches, create_session_token, set_session_cookie, request_public_host, request_public_secure, PERSISTENT_SESSION_LIFETIME_SECONDS


class FactorDeliveryError(ValueError):
    def __init__(self, nonce: str):
        super().__init__("delivery_unavailable")
        self.nonce = nonce


async def issue_challenge(bot, guild: int, user: int, purpose: str, method: str) -> str:
    nonce, code = await asyncio.to_thread(security.challenge, guild, user, purpose, method)
    text = f"Blackbird · код подтверждения: {code}\nДействует 5 минут. Никому не передавайте код. Если вы не запрашивали его, не вводите его."
    try:
        if method == "discord":
            recipient = bot.get_user(user) or await asyncio.wait_for(bot.fetch_user(user), timeout=10)
            await asyncio.wait_for(recipient.send(text), timeout=10)
        elif method == "telegram":
            link = await asyncio.to_thread(telegram.get_link_by_discord, guild, user)
            token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            if not link or not token:
                raise ValueError("telegram_unavailable")
            async with ClientSession(timeout=ClientTimeout(total=10)) as session:
                async with session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": link["telegram_chat_id"], "text": text, "protect_content": True}) as response:
                    result = await response.json()
                    if not response.ok or not result.get("ok"):
                        raise ValueError("delivery_unavailable")
    except Exception as exc:
        # Do not include provider URLs/token/OTP in a public error or logs.
        raise FactorDeliveryError(nonce) from None
    return nonce


def refresh_cookie(response, request, guild: int, user: int, version: int) -> None:
    credential = credentials.get_web_credential(guild, user)
    if not credential:
        return
    token, _ = create_session_token(guild_id=guild, user_id=user, session_version=credential.session_version, mfa_verified=True, account_security_version=version, lifetime_seconds=PERSISTENT_SESSION_LIFETIME_SECONDS)
    set_session_cookie(response, token, secure=request_public_secure(request), max_age=PERSISTENT_SESSION_LIFETIME_SECONDS, request_host=request_public_host(request))


def register_account_security_routes(app, bot, *, guild_id: int, authenticate) -> None:
    async def principal(request, *, write=False):
        selected, legacy = await authenticate(request)
        if selected is None or legacy:
            raise web.HTTPUnauthorized()
        if write and not csrf_matches(request, selected):
            raise web.HTTPForbidden()
        return selected

    async def snapshot(request):
        selected = await principal(request)
        user = int(selected.user_id)
        current, link, credential = await asyncio.gather(
            asyncio.to_thread(security.state, guild_id, user),
            asyncio.to_thread(telegram.get_link_by_discord, guild_id, user),
            asyncio.to_thread(credentials.get_web_credential, guild_id, user),
        )
        return web.json_response({"csrf_token": selected.csrf_token, "credential_kind": current["credential_kind"], "mfa_method": current["mfa_method"], "mfa_available": security.configured(), "recovery_remaining": len(json.loads(current.get("recovery_hashes", "[]"))), "login": credential.login if credential else "", "links": {"discord": str(user), "telegram": {"username": link.get("telegram_username", ""), "linked_at": link.get("created_at")} if link else None}}, headers={"Cache-Control": "no-store"})

    async def bill_snapshot(request):
        selected = await principal(request)
        from modules.atlas_billing_web import _json_ready
        result = await asyncio.to_thread(billing.atlas_billing_summary, int(selected.user_id))
        return web.json_response(_json_ready(result), headers={"Cache-Control": "no-store"})

    async def mutate(request):
        selected = await principal(request, write=True)
        user = int(selected.user_id)
        try:
            if len(await request.read()) > 8192:
                raise ValueError("invalid_request")
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("invalid_request")
            action = str(data.get("action", ""))
            if action not in {"challenge", "credential", "enroll", "confirm", "disable"}:
                raise ValueError("action_invalid")
            await asyncio.to_thread(security.require_credential, guild_id, user, str(data.get("current", ""))[:128])
            current = await asyncio.to_thread(security.state, guild_id, user)
            session_epoch = current["security_version"]
            response_data = {"ok": True}
            if action == "challenge":
                if not current["mfa_method"]:
                    raise ValueError("factor_not_enabled")
                try:
                    response_data["challenge"] = await issue_challenge(bot, guild_id, user, "manage", current["mfa_method"])
                except FactorDeliveryError as exc:
                    response_data.update(challenge=exc.nonce, delivery_failed=True)
            else:
                if current["mfa_method"]:
                    valid = await asyncio.to_thread(security.verify, guild_id, user, str(data.get("code", ""))[:32], str(data.get("challenge", ""))[:64], "manage")
                    if not valid:
                        raise ValueError("verification_invalid")
                if action == "credential":
                    await asyncio.to_thread(security.change_credential, guild_id, user, str(data.get("current", "")), str(data.get("value", "")), str(data.get("kind", "")), current["security_version"])
                    session_epoch += 1
                elif action == "enroll":
                    method = str(data.get("method", ""))
                    if method == "telegram" and not await asyncio.to_thread(telegram.get_link_by_discord, guild_id, user):
                        raise ValueError("telegram_not_linked")
                    credential = await asyncio.to_thread(credentials.get_web_credential, guild_id, user)
                    response_data.update(await asyncio.to_thread(security.begin_enrollment, guild_id, user, method, credential.login))
                    session_epoch = response_data["security_version"]
                    response_data["challenge"] = await issue_challenge(bot, guild_id, user, "enroll", method)
                elif action == "confirm":
                    if not await asyncio.to_thread(security.verify, guild_id, user, str(data.get("code", ""))[:32], str(data.get("challenge", ""))[:64], "enroll"):
                        raise ValueError("verification_invalid")
                    response_data["recovery_codes"] = await asyncio.to_thread(security.enable, guild_id, user, str(data.get("challenge", ""))[:64])
                    session_epoch += 1
                elif action == "disable":
                    await asyncio.to_thread(security.disable, guild_id, user, current["security_version"])
                    session_epoch += 1
            response = web.json_response(response_data, headers={"Cache-Control": "no-store"})
            await asyncio.to_thread(refresh_cookie, response, request, guild_id, user, session_epoch)
            from modules.global_log_runtime import emit_global_event
            emit_global_event({"source_service": "account", "source_type": "security", "event_type": f"account.security.{action}", "guild_id": guild_id, "actor_user_id": user, "summary": f"Настройки защиты аккаунта · {action}", "details": {"action": action, "credential_kind": str(data.get("kind", "")), "factor_method": str(data.get("method", ""))}})
            return response
        except ValueError as exc:
            allowed = {"invalid_request", "action_invalid", "current_credential_invalid", "password_length", "web_pin_invalid", "credential_kind_invalid", "factor_not_enabled", "factor_already_enabled", "factor_invalid", "verification_invalid", "telegram_not_linked", "security_key_unavailable", "challenge_rate_limited", "delivery_unavailable"}
            return web.json_response({"error": str(exc) if str(exc) in allowed else "invalid_request"}, status=400, headers={"Cache-Control": "no-store"})

    app.router.add_get("/api/account/security", snapshot)
    app.router.add_post("/api/account/security", mutate)
    app.router.add_get("/api/account/billing", bill_snapshot)
