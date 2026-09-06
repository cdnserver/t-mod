"""Public legal notices and the personal-data rights request endpoint."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import discord
from aiohttp import web

from modules.consensus_web_auth import signed_session_identity
from modules.control_center_runtime import resolve_registered_channel
from modules.global_log_runtime import emit_global_event, hash_remote
from persistence import privacy_repository as privacy_storage


_RECEIPT_RE = re.compile(r"^[A-Za-z0-9_-]{16,100}$")
_TYPE_LABELS = {
    "withdraw": "Отзыв согласия",
    "erase": "Удаление данных",
    "access": "Доступ к данным",
    "rectify": "Исправление данных",
    "export": "Экспорт данных",
    "restrict": "Ограничение обработки",
    "object": "Возражение против обработки",
    "question": "Вопрос о данных",
}


def _remote_address(request: web.Request) -> str:
    forwarded = str(request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    return forwarded or str(request.remote or "")


def register_legal_web_routes(
    app: web.Application,
    bot: discord.Client,
    *,
    guild_id: int,
    asset_dir: Path,
) -> None:
    """Attach canonical public policy pages and a resilient DSR form."""

    attempts: dict[str, deque[float]] = defaultdict(deque)
    accepted_receipts: dict[str, float] = {}

    async def legal_page(_: web.Request) -> web.FileResponse:
        return web.FileResponse(asset_dir / "legal.html")

    async def notify_technical_team(item: dict[str, Any]) -> bool:
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return False
        try:
            channel = await resolve_registered_channel(guild, "tech_log")
            if channel is None or not callable(getattr(channel, "send", None)):
                return False
            kind = str(item.get("request_type") or "question")
            identity = (
                str(item.get("discord_id") or "")
                or str(item.get("account_login") or "")
                or "не указан"
            )
            embed = discord.Embed(
                title=f"Запрос о персональных данных · {item['request_code']}",
                description=(str(item.get("details") or "Без дополнительного описания"))[:3000],
                color=0x8CEAB3,
            )
            embed.add_field(name="Действие", value=_TYPE_LABELS.get(kind, kind), inline=True)
            embed.add_field(name="Идентификатор", value=identity[:1024], inline=True)
            embed.add_field(
                name="Адрес для ответа",
                value=str(item.get("requester_email") or "—")[:1024],
                inline=False,
            )
            embed.add_field(name="Объём", value=str(item.get("scope") or "—")[:1024], inline=False)
            embed.set_footer(text="T-Mod · запрос субъекта данных · требуется проверка личности")
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return True
        except Exception:
            return False

    async def deliver_notification(item: dict[str, Any]) -> bool:
        delivered = await notify_technical_team(item)
        await asyncio.to_thread(
            privacy_storage.record_privacy_notification,
            int(item["id"]),
            delivered=delivered,
            error=None if delivered else "technical_channel_unavailable",
        )
        return delivered

    async def create_request(request: web.Request) -> web.Response:
        if request.content_type != "application/json":
            return web.json_response(
                {"error": "privacy_json_required", "message": "Форма передана в неверном формате."},
                status=415,
            )
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return web.json_response(
                {"error": "privacy_payload_invalid", "message": "Не удалось прочитать форму."},
                status=400,
            )
        if not isinstance(body, dict):
            return web.json_response(
                {"error": "privacy_payload_invalid", "message": "Не удалось прочитать форму."},
                status=400,
            )
        # Quiet honeypot: real users never see or populate this field.
        if str(body.get("website") or "").strip():
            return web.json_response({"ok": True, "request_code": "DSR-RECEIVED"})
        if body.get("acknowledge") is not True:
            return web.json_response(
                {
                    "error": "privacy_acknowledgement_required",
                    "message": "Подтвердите точность контактных данных.",
                },
                status=400,
            )
        receipt = str(body.get("receipt") or "").strip()
        if not _RECEIPT_RE.fullmatch(receipt):
            return web.json_response(
                {"error": "privacy_receipt_invalid", "message": "Обновите страницу и повторите отправку."},
                status=400,
            )

        now = time.monotonic()
        for key, expires_at in list(accepted_receipts.items()):
            if expires_at <= now:
                accepted_receipts.pop(key, None)
        remote_hash = hash_remote(_remote_address(request)) or "unknown"
        bucket = attempts[remote_hash]
        while bucket and now - bucket[0] > 3600:
            bucket.popleft()
        if receipt not in accepted_receipts and len(bucket) >= 4:
            return web.json_response(
                {
                    "error": "privacy_rate_limited",
                    "message": "Слишком много обращений. Повторите попытку через час или напишите администратору.",
                },
                status=429,
                headers={"Retry-After": "3600"},
            )

        identity = signed_session_identity(request, expected_guild_id=int(guild_id))
        account_user_id = int(identity[1]) if identity is not None else None
        try:
            item, created = await asyncio.to_thread(
                privacy_storage.create_privacy_request,
                receipt_key=receipt,
                request_type=body.get("request_type"),
                requester_email=body.get("email"),
                account_login=body.get("account_login"),
                discord_id=body.get("discord_id"),
                account_user_id=account_user_id,
                scope=body.get("scope"),
                details=body.get("details"),
                remote_hash=remote_hash,
                user_agent=request.headers.get("User-Agent"),
            )
        except ValueError as exc:
            messages = {
                "privacy_request_type_invalid": "Выберите, что T-Mod должен сделать с данными.",
                "privacy_email_invalid": "Укажите действующий адрес электронной почты.",
                "privacy_discord_id_invalid": "Discord ID должен состоять только из цифр.",
                "privacy_identity_required": "Укажите логин T-Mod или Discord ID, чтобы мы нашли ваши данные.",
                "privacy_scope_invalid": "Укажите, каких сервисов и данных касается запрос.",
                "privacy_receipt_invalid": "Обновите страницу и повторите отправку.",
            }
            code = str(exc)
            return web.json_response(
                {"error": code, "message": messages.get(code, "Проверьте заполнение формы.")},
                status=400,
            )
        if receipt not in accepted_receipts:
            bucket.append(now)
        accepted_receipts[receipt] = now + 3600

        notified = False
        if created:
            notified = await deliver_notification(item)
            emit_global_event(
                {
                    "source_service": "legal",
                    "source_type": "privacy_request",
                    "event_type": "privacy_request_received",
                    "severity": "info",
                    "actor_user_id": account_user_id,
                    "guild_id": int(guild_id),
                    "target_type": "privacy_request",
                    "target_id": str(item.get("request_code") or ""),
                    "summary": (
                        f"Получен запрос о данных {item.get('request_code')} · "
                        f"{_TYPE_LABELS.get(str(item.get('request_type') or ''), 'обращение')}"
                    ),
                    "details": {"notified": notified, "status": "received"},
                }
            )
        return web.json_response(
            {
                "ok": True,
                "created": created,
                "request_code": str(item.get("request_code") or ""),
                "created_at": item.get("created_at"),
                "message": "Запрос принят. Сохраните его номер.",
            },
            status=201 if created else 200,
        )

    for path in ("/legal", "/privacy", "/terms", "/cookies", "/data-request"):
        app.router.add_get(path, legal_page)
        app.router.add_get(f"{path}/", legal_page)
    app.router.add_post("/api/privacy/requests", create_request)

    notification_tasks: list[asyncio.Task[None]] = []

    async def retry_notifications() -> None:
        await asyncio.sleep(10)
        while True:
            pending = await asyncio.to_thread(
                privacy_storage.list_unnotified_privacy_requests,
                20,
            )
            for item in pending:
                await deliver_notification(item)
            await asyncio.sleep(300)

    async def start_notification_retry(_: web.Application) -> None:
        notification_tasks.append(
            asyncio.create_task(
                retry_notifications(),
                name="privacy-request-notifications",
            )
        )

    async def stop_notification_retry(_: web.Application) -> None:
        for task in notification_tasks:
            task.cancel()
        if notification_tasks:
            await asyncio.gather(*notification_tasks, return_exceptions=True)

    app.on_startup.append(start_notification_retry)
    app.on_cleanup.append(stop_notification_retry)


__all__ = ["register_legal_web_routes"]
