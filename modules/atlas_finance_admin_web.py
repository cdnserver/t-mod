"""Strict administrator-only Atlas finance and legal publishing plane."""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

from modules.atlas_finance_integrations import openrouter_credit_snapshot
from modules.atlas_legal_pdf import legal_pdf_path, render_legal_pdf
from modules.consensus_web_auth import ConsensusWebPrincipal, csrf_matches, request_public_host
from persistence import activity_repository as activity_storage
from persistence import atlas_billing_repository as billing_storage
from persistence import atlas_finance_repository as finance_storage


AuthenticatedRequest = Callable[[web.Request], Awaitable[tuple[ConsensusWebPrincipal | None, bool]]]


def _json_ready(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else format(value, "f")
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _require_host(request: web.Request) -> None:
    host = request_public_host(request).split(":", 1)[0].strip().lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return
    if host != "ap.finance.tvr.lat":
        raise web.HTTPNotFound()


def register_atlas_finance_admin_routes(
    app: web.Application,
    *,
    guild_id: int,
    asset_dir: Path,
    authenticate: AuthenticatedRequest,
) -> None:
    async def principal(request: web.Request, *, mutate: bool = False) -> ConsensusWebPrincipal:
        _require_host(request)
        selected, legacy = await authenticate(request)
        if selected is None or legacy:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "login_required"}), content_type="application/json"
            )
        if not bool(getattr(selected, "administrator", False)):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "platform_administrator_required"}), content_type="application/json"
            )
        if mutate and not csrf_matches(request, selected):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "csrf_invalid"}), content_type="application/json"
            )
        return selected

    async def payload(request: web.Request, selected: ConsensusWebPrincipal) -> dict:
        if not csrf_matches(request, selected):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "csrf_invalid"}), content_type="application/json"
            )
        try:
            value = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "payload_invalid"}), content_type="application/json"
            ) from None
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "payload_invalid"}), content_type="application/json"
            )
        return value

    async def audit(selected: ConsensusWebPrincipal, action: str, target_type: str,
                    target_id: object, summary: str, details: dict | None = None) -> None:
        await asyncio.to_thread(
            activity_storage.bot_record_action,
            guild_id=int(guild_id), actor_id=int(selected.user_id), actor_display=str(selected.display_name),
            module="atlas_finance", action_kind=action, target_type=target_type,
            target_id=target_id, summary=summary, payload=details or {}, reversible=False,
        )

    async def page(request: web.Request) -> web.FileResponse:
        _require_host(request)
        selected, legacy = await authenticate(request)
        if selected is None or legacy:
            raise web.HTTPFound("/login?next=/atlas-finance-admin")
        if not bool(getattr(selected, "administrator", False)):
            raise web.HTTPForbidden(text="Atlas Administration Plane доступен только администратору.")
        response = web.FileResponse(asset_dir / "index.html")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        return response

    async def asset(request: web.Request) -> web.FileResponse:
        _require_host(request)
        name = str(request.match_info.get("name") or "")
        if name not in {"app.js", "style.css", "favicon.svg"}:
            raise web.HTTPNotFound()
        response = web.FileResponse(asset_dir / name)
        response.headers["Cache-Control"] = "no-cache"
        return response

    async def bootstrap(request: web.Request) -> web.Response:
        selected = await principal(request)
        try:
            days = int(request.query.get("days") or 30)
            rate = Decimal(str(request.query.get("usd_rub_rate") or os.getenv("ATLAS_FINANCE_USD_RUB_RATE", "95")))
        except (ValueError, InvalidOperation):
            raise web.HTTPBadRequest(text=json.dumps({"error": "finance_filter_invalid"}), content_type="application/json") from None
        dashboard, content, legal = await asyncio.gather(
            asyncio.to_thread(finance_storage.finance_dashboard, days=days, usd_rub_rate=rate),
            asyncio.to_thread(finance_storage.content_manifest, include_drafts=True),
            asyncio.to_thread(finance_storage.legal_revisions, include_drafts=True),
        )
        return web.json_response(_json_ready({
            "dashboard": dashboard, "content": content, "legal": legal,
            "viewer": {"id": int(selected.user_id), "name": str(selected.display_name), "csrf_token": str(selected.csrf_token)},
        }), headers={"Cache-Control": "no-store"})

    async def external_status(request: web.Request) -> web.Response:
        await principal(request)
        result = await openrouter_credit_snapshot()
        await asyncio.to_thread(
            finance_storage.save_external_snapshot, "openrouter", payload=result,
            status=str(result.get("status") or "unknown"), error_text=str(result.get("message") or ""),
        )
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def orders(request: web.Request) -> web.Response:
        await principal(request)
        try:
            result = await asyncio.to_thread(
                finance_storage.list_orders, status=request.query.get("status", ""),
                query=request.query.get("q", ""), limit=int(request.query.get("limit") or 100),
            )
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(_json_ready({"orders": result}), headers={"Cache-Control": "no-store"})

    async def reconcile_order(request: web.Request) -> web.Response:
        await principal(request, mutate=True)
        return web.json_response(
            {
                "error": "atlas_payments_disabled",
                "message": "Внешняя сверка отключена вместе с денежными операциями.",
            },
            status=410,
        )

    async def account(request: web.Request) -> web.Response:
        await principal(request)
        value = str(request.query.get("user_id") or "").strip()
        if not value.isdigit() or not 15 <= len(value) <= 22:
            return web.json_response({"error": "atlas_billing_user_id_invalid"}, status=400)
        result = await asyncio.to_thread(billing_storage.atlas_billing_summary, int(value))
        return web.json_response(_json_ready({"user_id": value, "summary": result}))

    async def grant(request: web.Request) -> web.Response:
        selected = await principal(request, mutate=True)
        data = await payload(request, selected)
        value = str(data.get("user_id") or "").strip()
        if not value.isdigit() or not 15 <= len(value) <= 22:
            return web.json_response({"error": "atlas_billing_user_id_invalid"}, status=400)
        key = str(request.headers.get("X-Idempotency-Key") or data.get("request_key") or "")
        try:
            if str(data.get("kind") or "tokens") == "subscription":
                result = await asyncio.to_thread(
                    finance_storage.grant_subscription, int(value), plan_code=str(data.get("plan_code") or ""),
                    days=int(data.get("days") or 30), actor_user_id=int(selected.user_id),
                    reason=str(data.get("reason") or ""), request_key=key,
                )
                action = "subscription_grant"
                summary = f"Выдана подписка {data.get('plan_code')} пользователю {value}"
            else:
                result = await asyncio.to_thread(
                    billing_storage.atlas_admin_grant_tokens, int(value), actor_user_id=int(selected.user_id),
                    amount_tokens=int(data.get("amount_tokens") or 0), reason=str(data.get("reason") or ""),
                    request_key=key,
                )
                action = "token_grant"
                summary = f"Начислены Atlas Token пользователю {value}"
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if result.get("created"):
            await audit(selected, action, "member", value, summary, {"request_key": key})
        return web.json_response(_json_ready(result), status=201)

    async def cost(request: web.Request) -> web.Response:
        selected = await principal(request, mutate=True)
        data = await payload(request, selected)
        try:
            result = await asyncio.to_thread(
                finance_storage.add_cost, category=str(data.get("category") or "other"),
                title=str(data.get("title") or ""), amount_kopecks=int(data.get("amount_kopecks") or 0),
                occurred_at=str(data.get("occurred_at") or ""), note=str(data.get("note") or ""),
                actor_user_id=int(selected.user_id),
                request_key=str(request.headers.get("X-Idempotency-Key") or data.get("request_key") or ""),
            )
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        await audit(selected, "cost_create", "finance_cost", result["id"], f"Добавлен расход: {result['title']}", result)
        return web.json_response(_json_ready(result), status=201)

    async def content(request: web.Request) -> web.Response:
        selected = await principal(request, mutate=request.method != "GET")
        if request.method == "GET":
            return web.json_response(_json_ready(await asyncio.to_thread(finance_storage.content_manifest, include_drafts=True)))
        data = await payload(request, selected)
        try:
            result = await asyncio.to_thread(
                finance_storage.save_content, str(data.get("slot_key") or ""),
                content_text=str(data.get("content_text") or ""), publish=bool(data.get("publish")),
                actor_user_id=int(selected.user_id),
                request_key=str(request.headers.get("X-Idempotency-Key") or data.get("request_key") or ""),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        await audit(selected, "content_publish" if data.get("publish") else "content_draft",
                    "content_slot", result["slot_key"], f"Обновлён текст {result['slot_key']}", {"revision_id": result["id"]})
        return web.json_response(result, status=201)

    async def legal(request: web.Request) -> web.Response:
        selected = await principal(request, mutate=request.method != "GET")
        if request.method == "GET":
            result = await asyncio.to_thread(finance_storage.legal_revisions, request.query.get("document", ""), include_drafts=True)
            return web.json_response(_json_ready({"revisions": result}))
        data = await payload(request, selected)
        try:
            result = await asyncio.to_thread(
                finance_storage.create_legal_revision, str(data.get("document_key") or ""),
                version_label=str(data.get("version_label") or ""), title=str(data.get("title") or ""),
                summary=str(data.get("summary") or ""), effective_from=str(data.get("effective_from") or ""),
                blocks=data.get("blocks"), actor_user_id=int(selected.user_id),
                request_key=str(request.headers.get("X-Idempotency-Key") or data.get("request_key") or ""),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        await audit(selected, "legal_draft_create", "legal_revision", result["id"],
                    f"Создан черновик {result['document_key']} · {result['version_label']}")
        return web.json_response(_json_ready(result), status=201)

    async def publish_legal(request: web.Request) -> web.Response:
        selected = await principal(request, mutate=True)
        try:
            revision_id = int(request.match_info.get("revision_id") or 0)
        except ValueError:
            raise web.HTTPNotFound() from None
        revision = await asyncio.to_thread(finance_storage.legal_revision, revision_id)
        if revision is None:
            raise web.HTTPNotFound()
        try:
            _, digest = await asyncio.to_thread(render_legal_pdf, revision)
            await asyncio.to_thread(finance_storage.set_legal_pdf_hash, revision_id, digest)
            result = await asyncio.to_thread(finance_storage.publish_legal_revision, revision_id, actor_user_id=int(selected.user_id))
        except (OSError, RuntimeError, ValueError) as exc:
            return web.json_response({"error": "atlas_legal_publish_failed", "message": str(exc)}, status=500)
        await audit(selected, "legal_publish", "legal_revision", revision_id,
                    f"Опубликована редакция {result['document_key']} · {result['version_label']}", {"sha256": digest})
        return web.json_response(_json_ready(result))

    async def revision_pdf(request: web.Request) -> web.FileResponse:
        await principal(request)
        try:
            revision_id = int(request.match_info.get("revision_id") or 0)
        except ValueError:
            raise web.HTTPNotFound() from None
        revision = await asyncio.to_thread(finance_storage.legal_revision, revision_id)
        if revision is None:
            raise web.HTTPNotFound()
        path = legal_pdf_path(revision)
        if not path.is_file():
            path, digest = await asyncio.to_thread(render_legal_pdf, revision)
            await asyncio.to_thread(finance_storage.set_legal_pdf_hash, revision_id, digest)
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    app.router.add_get("/atlas-finance-admin", page)
    app.router.add_get("/atlas-finance-admin/", page)
    app.router.add_get("/atlas-finance-admin/assets/{name}", asset)
    app.router.add_get("/api/admin/atlas-finance/bootstrap", bootstrap)
    app.router.add_get("/api/admin/atlas-finance/external", external_status)
    app.router.add_get("/api/admin/atlas-finance/orders", orders)
    app.router.add_post("/api/admin/atlas-finance/orders/{order_id}/reconcile", reconcile_order)
    app.router.add_get("/api/admin/atlas-finance/account", account)
    app.router.add_post("/api/admin/atlas-finance/grants", grant)
    app.router.add_post("/api/admin/atlas-finance/costs", cost)
    app.router.add_route("*", "/api/admin/atlas-finance/content", content)
    app.router.add_route("*", "/api/admin/atlas-finance/legal", legal)
    app.router.add_post("/api/admin/atlas-finance/legal/{revision_id}/publish", publish_legal)
    app.router.add_get("/api/admin/atlas-finance/legal/{revision_id}/pdf", revision_pdf)


__all__ = ["register_atlas_finance_admin_routes"]
