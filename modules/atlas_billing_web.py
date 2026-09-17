"""Public Atlas commerce page and authenticated token dashboard."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

from modules.atlas_billing import (
    atlas_billing_catalog,
    atlas_plan,
    atlas_token_pack,
    robokassa_config,
    robokassa_payment_fields,
    robokassa_result_is_valid,
)
from modules.consensus_web_auth import (
    ConsensusWebPrincipal,
    csrf_matches,
    request_public_host,
)
from persistence import atlas_billing_repository as billing_storage


AuthenticatedRequest = Callable[
    [web.Request],
    Awaitable[tuple[ConsensusWebPrincipal | None, bool]],
]


_RECEIPT_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _json_ready(value):
    """Normalize PostgreSQL numeric aggregates before aiohttp JSON encoding.

    SQLite returns integer ``SUM`` values as ``int`` while psycopg returns
    ``Decimal`` for the same query.  Keeping this conversion at the HTTP
    boundary gives both database backends one stable API contract.
    """

    if isinstance(value, Decimal):
        integral = value.to_integral_value()
        return int(integral) if value == integral else format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def require_atlas_billing_host(request: web.Request) -> None:
    host = request_public_host(request).split(":", 1)[0].strip().lower()
    # The in-process test server and direct loopback diagnostics are
    # intentionally allowed. Public T-Mod hosts must not mirror sales pages:
    # Robokassa reviews one canonical store at atlas.tvr.lat.
    if (host == "tvr.lat" or host.endswith(".tvr.lat")) and host != "atlas.tvr.lat":
        raise web.HTTPNotFound()


def register_atlas_billing_web_routes(
    app: web.Application,
    *,
    asset_dir: Path,
    authenticate: AuthenticatedRequest,
    ecosystem_asset_dir: Path | None = None,
) -> None:
    public_pages = {
        "/atlas-billing": "index.html",
        "/atlas-billing/": "index.html",
        "/account": "account.html",
        "/account/": "account.html",
        "/legal": "legal.html",
        "/legal/": "legal.html",
        "/offer": "offer.html",
        "/offer/": "offer.html",
        "/refunds": "refunds.html",
        "/refunds/": "refunds.html",
        "/privacy": "privacy.html",
        "/privacy/": "privacy.html",
        "/terms": "terms.html",
        "/terms/": "terms.html",
        "/data-request": "data-request.html",
        "/data-request/": "data-request.html",
        "/contacts": "contacts.html",
        "/contacts/": "contacts.html",
        "/atlas-billing/success": "payment-result.html",
        "/atlas-billing/fail": "payment-result.html",
    }

    async def page(request: web.Request) -> web.FileResponse:
        host = request_public_host(request).split(":", 1)[0].strip().lower()
        if (
            host == "tvr.lat"
            and ecosystem_asset_dir is not None
            and request.path.rstrip("/")
            in {"/legal", "/privacy", "/terms", "/data-request"}
        ):
            response = web.FileResponse(ecosystem_asset_dir / "legal.html")
            response.headers["Cache-Control"] = "no-cache"
            return response
        require_atlas_billing_host(request)
        filename = public_pages.get(request.path)
        if filename is None:
            raise web.HTTPNotFound()
        response = web.FileResponse(asset_dir / filename)
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Robots-Tag"] = (
            "noindex, nofollow" if filename == "payment-result.html" else "index, follow"
        )
        return response

    async def asset(request: web.Request) -> web.FileResponse:
        require_atlas_billing_host(request)
        name = str(request.match_info.get("name") or "")
        if name not in {
            "app.js", "account.js", "style.css", "account.css", "store.css", "legal.css", "legal.js", "payment.js",
            "favicon.svg",
        }:
            raise web.HTTPNotFound()
        response = web.FileResponse(asset_dir / name)
        response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=86400"
        return response

    async def document(request: web.Request) -> web.FileResponse:
        require_atlas_billing_host(request)
        name = str(request.match_info.get("name") or "")
        if name not in {"atlas-public-offer.pdf", "atlas-privacy-policy.pdf"}:
            raise web.HTTPNotFound()
        path = asset_dir / "documents" / name
        if not path.is_file():
            raise web.HTTPNotFound()
        response = web.FileResponse(path)
        response.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
        response.headers["Content-Disposition"] = f'inline; filename="{name}"'
        return response

    async def principal(request: web.Request) -> ConsensusWebPrincipal:
        selected, legacy = await authenticate(request)
        if selected is None or legacy:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "login_required"}),
                content_type="application/json",
            )
        return selected

    async def catalog(request: web.Request) -> web.Response:
        require_atlas_billing_host(request)
        config = robokassa_config()
        configured = bool(config["enabled"] and config["password1"] and config["password2"])
        return web.json_response(
            {
                **atlas_billing_catalog(),
                "payments": {
                    "enabled": configured,
                    "configured": configured,
                    "test_mode": bool(config["test_mode"]),
                    "mode": "test" if config["test_mode"] else "live",
                },
                "personal_data": {
                    "localization_ready": bool(config["personal_data_localization_ready"]),
                    "primary_region": str(config["personal_data_primary_region"] or "not-configured"),
                    "live_payments_allowed": bool(
                        config["personal_data_localization_ready"]
                        and config["personal_data_primary_region"] == "RU"
                    ),
                },
            }
        )

    async def summary(request: web.Request) -> web.Response:
        require_atlas_billing_host(request)
        selected = await principal(request)
        result = await asyncio.to_thread(
            billing_storage.atlas_billing_summary,
            int(selected.user_id),
        )
        return web.json_response(
            _json_ready({
                **result,
                "viewer": {
                    "id": int(selected.user_id),
                    "name": str(selected.display_name),
                    "csrf_token": str(selected.csrf_token),
                },
            }),
            headers={"Cache-Control": "no-store"},
        )

    async def admin_metrics(request: web.Request) -> web.Response:
        require_atlas_billing_host(request)
        selected = await principal(request)
        if not bool(getattr(selected, "administrator", False)):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "administrator_required"}),
                content_type="application/json",
            )
        try:
            days = int(request.query.get("days") or 30)
        except ValueError:
            days = 30
        result = await asyncio.to_thread(
            billing_storage.atlas_billing_admin_metrics,
            days=days,
        )
        return web.json_response(_json_ready(result))

    async def order_status(request: web.Request) -> web.Response:
        require_atlas_billing_host(request)
        selected = await principal(request)
        try:
            order_id = int(request.match_info.get("order_id") or 0)
        except ValueError:
            raise web.HTTPNotFound() from None
        order = await asyncio.to_thread(billing_storage.atlas_payment_order, order_id)
        if order is None or int(order.get("user_id") or 0) != int(selected.user_id):
            raise web.HTTPNotFound()
        return web.json_response(
            {
                "id": int(order["id"]),
                "status": str(order["status"]),
                "product_kind": str(order["product_kind"]),
                "product_code": str(order["product_code"]),
                "amount_kopecks": int(order["amount_kopecks"]),
                "atlas_tokens": int(order["atlas_tokens"]),
                "created_at": order.get("created_at"),
                "paid_at": order.get("paid_at"),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def checkout(request: web.Request) -> web.Response:
        require_atlas_billing_host(request)
        selected = await principal(request)
        if not csrf_matches(request, selected):
            return web.json_response({"error": "csrf_invalid"}, status=403)
        payment_config = robokassa_config()
        if not (
            payment_config["enabled"]
            and payment_config["password1"]
            and payment_config["password2"]
        ):
            return web.json_response(
                {
                    "error": "atlas_billing_payments_not_configured",
                    "message": "Оплата пока закрыта. Баланс и расход уже учитываются.",
                },
                status=503,
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "payload_invalid"}, status=400)
        product_kind = str(payload.get("product_kind") or "").strip().lower()
        product_code = str(payload.get("product_code") or "").strip().lower()
        receipt_email = str(payload.get("receipt_email") or "").strip().lower()[:254]
        if not _RECEIPT_EMAIL_RE.fullmatch(receipt_email):
            return web.json_response(
                {
                    "error": "atlas_billing_receipt_email_invalid",
                    "message": "Укажите действующий email для электронного кассового чека.",
                },
                status=400,
            )
        if not bool(payment_config["test_mode"]) and not (
            bool(payment_config["personal_data_localization_ready"])
            and str(payment_config["personal_data_primary_region"]) == "RU"
        ):
            return web.json_response(
                {
                    "error": "atlas_personal_data_localization_required",
                    "message": "Боевые платежи закрыты до ввода российской первичной базы персональных данных.",
                },
                status=503,
            )
        checkout_key = str(payload.get("checkout_key") or "").strip()[:100]
        if len(checkout_key) < 16:
            return web.json_response({"error": "atlas_billing_checkout_key_required"}, status=400)
        try:
            if product_kind == "subscription":
                plan = atlas_plan(product_code)
                if plan.monthly_price_rub <= 0:
                    raise ValueError("atlas_billing_product_invalid")
                amount_kopecks = plan.monthly_price_rub * 100
                atlas_tokens = plan.monthly_tokens
                plan_code = plan.code
                description = (
                    f"Вычислительный пакет Atlas {plan.name}: "
                    f"{plan.monthly_tokens:,} AT на 30 дней"
                ).replace(",", " ")
            elif product_kind == "token_pack":
                pack = atlas_token_pack(product_code)
                amount_kopecks = int(pack["price_rub"]) * 100
                atlas_tokens = int(pack["atlas_tokens"])
                plan_code = None
                description = (
                    f"Пополнение вычислительного резерва Atlas: "
                    f"{atlas_tokens:,} AT"
                ).replace(",", " ")
            else:
                raise ValueError("atlas_billing_product_invalid")
            order = await asyncio.to_thread(
                billing_storage.atlas_create_payment_order,
                int(selected.user_id),
                product_kind=product_kind,
                product_code=product_code,
                amount_kopecks=amount_kopecks,
                atlas_tokens=atlas_tokens,
                plan_code=plan_code,
                checkout_key=checkout_key,
            )
            fields = robokassa_payment_fields(
                invoice_id=int(order["id"]),
                amount_kopecks=amount_kopecks,
                description=description,
                user_id=int(selected.user_id),
                receipt_email=receipt_email,
            )
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(
            _json_ready({
                "order": order,
                "payment": {
                    "method": "POST",
                    "url": robokassa_config()["payment_url"],
                    "fields": fields,
                },
            }),
            status=201,
        )

    async def payment_result(request: web.Request) -> web.Response:
        require_atlas_billing_host(request)
        values = dict(request.query)
        if request.can_read_body:
            values.update(dict(await request.post()))
        if not robokassa_result_is_valid(values):
            return web.Response(text="bad signature", status=403)
        try:
            order_id = int(values.get("InvId") or values.get("InvoiceID") or 0)
            order = await asyncio.to_thread(
                billing_storage.atlas_payment_order,
                order_id,
            )
            if order is None or int(order["user_id"]) != int(values.get("Shp_user") or 0):
                raise ValueError("order mismatch")
            received = Decimal(str(values.get("OutSum") or "0")).quantize(Decimal("0.01"))
            expected = (Decimal(int(order["amount_kopecks"])) / 100).quantize(Decimal("0.01"))
            if received != expected:
                raise ValueError("amount mismatch")
            await asyncio.to_thread(
                billing_storage.atlas_settle_payment_order,
                order_id,
                provider_operation_id=str(values.get("OpKey") or ""),
            )
        except (InvalidOperation, TypeError, ValueError):
            return web.Response(text="order mismatch", status=400)
        return web.Response(text=f"OK{order_id}", content_type="text/plain")

    for route in public_pages:
        app.router.add_get(route, page)
    app.router.add_get("/atlas-billing/assets/{name}", asset)
    app.router.add_get("/documents/{name}", document)
    app.router.add_get("/api/atlas/billing/catalog", catalog)
    app.router.add_get("/api/atlas/billing/summary", summary)
    app.router.add_get("/api/atlas/billing/orders/{order_id}", order_status)
    app.router.add_get("/api/atlas/billing/admin/metrics", admin_metrics)
    app.router.add_post("/api/atlas/billing/checkout", checkout)
    app.router.add_route("*", "/api/atlas/billing/robokassa/result", payment_result)


__all__ = ["register_atlas_billing_web_routes", "require_atlas_billing_host"]
