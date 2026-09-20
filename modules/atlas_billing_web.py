"""Public Atlas commerce page and authenticated token dashboard."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

from modules.atlas_billing import (
    atlas_billing_catalog,
)
from modules.consensus_web_auth import (
    ConsensusWebPrincipal,
    request_public_host,
)
from modules.atlas_legal_pdf import legal_pdf_path
from persistence import atlas_billing_repository as billing_storage
from persistence import atlas_finance_repository as finance_storage


AuthenticatedRequest = Callable[
    [web.Request],
    Awaitable[tuple[ConsensusWebPrincipal | None, bool]],
]


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
    # The in-process test server and direct loopback diagnostics are allowed.
    # Public T-Mod hosts must not mirror the Atlas account surface.
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
            "index, follow"
        )
        return response

    async def asset(request: web.Request) -> web.FileResponse:
        require_atlas_billing_host(request)
        name = str(request.match_info.get("name") or "")
        if name not in {
            "app.js", "account.js", "style.css", "account.css", "store.css", "legal.css", "legal.js",
            "favicon.svg",
        }:
            raise web.HTTPNotFound()
        response = web.FileResponse(asset_dir / name)
        response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=86400"
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
        return web.json_response(
            {
                **atlas_billing_catalog(),
                "payments": {
                    "enabled": False,
                    "configured": False,
                    "mode": "disabled",
                    "message": "Платные операции отключены.",
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

    async def public_content(request: web.Request) -> web.Response:
        require_atlas_billing_host(request)
        result = await asyncio.to_thread(finance_storage.content_manifest)
        return web.json_response(_json_ready(result), headers={"Cache-Control": "public, max-age=60"})

    async def legal_manifest(request: web.Request) -> web.Response:
        require_atlas_billing_host(request)
        document_key = request.query.get("document", "")
        try:
            revisions, current = await asyncio.gather(
                asyncio.to_thread(finance_storage.legal_revisions, document_key),
                asyncio.to_thread(finance_storage.current_legal_revision, document_key)
                if document_key else asyncio.sleep(0, result=None),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(
            _json_ready({
                "revisions": revisions,
                "current_revision_id": int(current["id"]) if current else None,
            }),
            headers={"Cache-Control": "public, max-age=60"},
        )

    async def current_legal(request: web.Request) -> web.Response:
        require_atlas_billing_host(request)
        try:
            revision = await asyncio.to_thread(
                finance_storage.current_legal_revision,
                str(request.match_info.get("document") or ""),
            )
        except ValueError:
            raise web.HTTPNotFound() from None
        if revision is None:
            raise web.HTTPNotFound()
        return web.json_response(_json_ready(revision), headers={"Cache-Control": "public, max-age=60"})

    async def public_legal_pdf(request: web.Request) -> web.FileResponse:
        require_atlas_billing_host(request)
        try:
            revision_id = int(request.match_info.get("revision_id") or 0)
        except ValueError:
            raise web.HTTPNotFound() from None
        revision = await asyncio.to_thread(finance_storage.legal_revision, revision_id)
        if revision is None or str(revision.get("status")) not in {"published", "archived"}:
            raise web.HTTPNotFound()
        path = legal_pdf_path(revision)
        if not path.is_file() or not revision.get("pdf_sha256"):
            raise web.HTTPNotFound()
        response = web.FileResponse(path)
        response.headers["Cache-Control"] = "public, max-age=86400, immutable"
        response.headers["Content-Disposition"] = (
            f'inline; filename="atlas-{revision["document_key"]}-v{int(revision["version_number"])}.pdf"'
        )
        return response

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
        return web.json_response(
            {
                "error": "atlas_payments_disabled",
                "message": (
                    "Платные подключения и пополнение Atlas Token временно "
                    "недоступны."
                ),
            },
            status=410,
        )

    for route in public_pages:
        app.router.add_get(route, page)
    app.router.add_get("/atlas-billing/assets/{name}", asset)
    app.router.add_get("/api/atlas/billing/catalog", catalog)
    app.router.add_get("/api/atlas/billing/summary", summary)
    app.router.add_get("/api/atlas/billing/orders/{order_id}", order_status)
    app.router.add_get("/api/atlas/billing/admin/metrics", admin_metrics)
    app.router.add_get("/api/atlas/billing/content", public_content)
    app.router.add_get("/api/atlas/legal/revisions", legal_manifest)
    app.router.add_get("/api/atlas/legal/{document}/current", current_legal)
    app.router.add_get("/api/atlas/legal/revisions/{revision_id}/pdf", public_legal_pdf)
    app.router.add_post("/api/atlas/billing/checkout", checkout)


__all__ = ["register_atlas_billing_web_routes", "require_atlas_billing_host"]
