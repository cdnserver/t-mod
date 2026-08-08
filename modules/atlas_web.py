"""Independent Atlas product surface hosted by the T-Mod web runtime."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Awaitable, Callable

import discord
from aiohttp import web

from modules.atlas_ai import AtlasAIError, atlas_ai_health, atlas_answer, atlas_index_source
from modules.consensus_web_auth import ConsensusWebPrincipal, csrf_matches
from persistence import atlas_repository as storage
from persistence import web_auth_repository as web_auth_storage


AuthenticatedRequest = Callable[
    [web.Request],
    Awaitable[tuple[ConsensusWebPrincipal | None, bool]],
]


def register_atlas_web_routes(
    app: web.Application,
    bot: discord.Client,
    *,
    guild_id: int,
    asset_dir: Path,
    authenticate: AuthenticatedRequest,
) -> None:
    rates: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=30))
    receipts: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
    indexing_tasks: set[asyncio.Task[None]] = set()

    async def atlas_index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(asset_dir / "index.html")

    async def atlas_asset(request: web.Request) -> web.FileResponse:
        name = str(request.match_info.get("name") or "")
        if name not in {"app.js", "style.css", "favicon.svg"}:
            raise web.HTTPNotFound()
        response = web.FileResponse(asset_dir / name)
        response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=86400"
        return response

    async def principal(request: web.Request) -> ConsensusWebPrincipal:
        selected, legacy = await authenticate(request)
        if legacy or selected is None:
            raise web.HTTPUnauthorized(
                text=json.dumps(
                    {
                        "error": "atlas_login_required",
                        "message": "Войдите в Atlas через учётную запись T-Mod.",
                    },
                    ensure_ascii=False,
                ),
                content_type="application/json",
            )
        return selected

    async def body(request: web.Request, selected: ConsensusWebPrincipal) -> dict[str, Any]:
        if not csrf_matches(request, selected):
            raise web.HTTPForbidden(
                text='{"error":"csrf_failed","message":"Защитная сессия устарела."}',
                content_type="application/json",
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(
                text='{"error":"invalid_payload","message":"Некорректные данные запроса."}',
                content_type="application/json",
            )
        return payload

    async def dashboard_for(selected: ConsensusWebPrincipal) -> dict[str, Any]:
        if not selected.administrator:
            return {
                "preview": True,
                "viewer": {
                    "id": int(selected.user_id),
                    "name": str(selected.display_name),
                    "administrator": False,
                    "csrf_token": str(selected.csrf_token),
                },
                "release": {
                    "status": "closed_preview",
                    "modules": ["Atlas AI", "Документы", "База знаний", "Forum Desk"],
                },
            }
        dashboard, health = await asyncio.gather(
            asyncio.to_thread(
                storage.atlas_dashboard,
                int(guild_id),
                int(selected.user_id),
                str(selected.display_name),
            ),
            atlas_ai_health(),
        )
        return {
            **dashboard,
            "viewer": {
                "id": int(selected.user_id),
                "name": str(selected.display_name),
                "administrator": bool(selected.administrator),
                "csrf_token": str(selected.csrf_token),
            },
            "ai": health,
            "capabilities": [
                "chat",
                "documents.create",
                "knowledge.add" if selected.administrator else "knowledge.read",
            ],
        }

    async def bootstrap(request: web.Request) -> web.Response:
        selected = await principal(request)
        payload = await dashboard_for(selected)
        return web.json_response(payload)

    async def onboarding(request: web.Request) -> web.Response:
        selected = await principal(request)
        if not selected.administrator:
            raise web.HTTPForbidden(text='{"error":"atlas_closed_preview"}', content_type="application/json")
        payload = await body(request, selected)
        dashboard = await asyncio.to_thread(
            storage.atlas_dashboard,
            int(guild_id),
            int(selected.user_id),
            str(selected.display_name),
        )
        result = await asyncio.to_thread(
            storage.atlas_update_onboarding,
            int(dashboard["organization"]["id"]),
            int(selected.user_id),
            step=int(payload.get("step") or 0),
            profile=payload.get("profile") if isinstance(payload.get("profile"), dict) else {},
        )
        return web.json_response({"membership": result})

    def check_rate(user_id: int) -> None:
        now = time.monotonic()
        bucket = rates[int(user_id)]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= 12:
            raise web.HTTPTooManyRequests(
                text='{"error":"atlas_rate_limited","message":"Слишком много запросов. Подождите минуту."}',
                content_type="application/json",
            )
        bucket.append(now)

    def cached_receipt(user_id: int, request: web.Request) -> tuple[str, dict[str, Any] | None]:
        key = str(request.headers.get("X-Idempotency-Key") or "").strip()[:100]
        if not key:
            raise web.HTTPBadRequest(
                text='{"error":"idempotency_required","message":"Повторите действие из актуальной панели."}',
                content_type="application/json",
            )
        now = time.monotonic()
        for receipt_key, (expires, _) in list(receipts.items()):
            if expires <= now:
                receipts.pop(receipt_key, None)
        saved = receipts.get((int(user_id), key))
        return key, saved[1] if saved else None

    async def chat(request: web.Request) -> web.Response:
        selected = await principal(request)
        if not selected.administrator:
            raise web.HTTPForbidden(text='{"error":"atlas_closed_preview"}', content_type="application/json")
        payload = await body(request, selected)
        receipt_key, cached = cached_receipt(selected.user_id, request)
        if cached is not None:
            return web.json_response(cached)
        check_rate(selected.user_id)
        question = str(payload.get("question") or "").strip()
        dashboard = await asyncio.to_thread(
            storage.atlas_dashboard,
            int(guild_id),
            int(selected.user_id),
            str(selected.display_name),
        )
        organization_id = int(dashboard["organization"]["id"])
        try:
            answer = await atlas_answer(organization_id, question)
        except AtlasAIError as exc:
            return web.json_response(
                {"error": exc.code, "message": str(exc), "retryable": exc.retryable},
                status=503 if exc.retryable or exc.code.endswith("not_configured") else 400,
            )
        thread_id = await asyncio.to_thread(
            storage.atlas_create_thread,
            organization_id,
            int(selected.user_id),
            question[:100],
        )
        await asyncio.to_thread(storage.atlas_add_message, thread_id, "user", question)
        await asyncio.to_thread(
            storage.atlas_add_message,
            thread_id,
            "assistant",
            answer["answer"],
            citations=answer["citations"],
            model=answer["model"],
            latency_ms=answer["latency_ms"],
        )
        response = {**answer, "thread_id": thread_id}
        receipts[(int(selected.user_id), receipt_key)] = (time.monotonic() + 300, response)
        return web.json_response(response)

    async def documents(request: web.Request) -> web.Response:
        selected = await principal(request)
        if not selected.administrator:
            raise web.HTTPForbidden(text='{"error":"atlas_closed_preview"}', content_type="application/json")
        dashboard = await asyncio.to_thread(
            storage.atlas_dashboard,
            int(guild_id),
            int(selected.user_id),
            str(selected.display_name),
        )
        organization_id = int(dashboard["organization"]["id"])
        if request.method == "GET":
            return web.json_response(
                {"items": await asyncio.to_thread(storage.atlas_documents, organization_id)}
            )
        payload = await body(request, selected)
        try:
            created = await asyncio.to_thread(
                storage.atlas_create_document,
                organization_id,
                int(selected.user_id),
                title=str(payload.get("title") or ""),
                template_id=int(payload["template_id"]) if payload.get("template_id") else None,
                fields=payload.get("fields") if isinstance(payload.get("fields"), dict) else {},
                rendered_text=str(payload.get("rendered_text") or ""),
            )
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc), "message": "Документ не создан."}, status=400)
        return web.json_response({"document": created}, status=201)

    async def knowledge(request: web.Request) -> web.Response:
        selected = await principal(request)
        if not selected.administrator:
            raise web.HTTPForbidden(
                text='{"error":"atlas_knowledge_admin_required"}',
                content_type="application/json",
            )
        payload = await body(request, selected)
        dashboard = await asyncio.to_thread(
            storage.atlas_dashboard,
            int(guild_id),
            int(selected.user_id),
            str(selected.display_name),
        )
        try:
            source = await asyncio.to_thread(
                storage.atlas_add_knowledge,
                int(dashboard["organization"]["id"]),
                int(selected.user_id),
                title=str(payload.get("title") or ""),
                content=str(payload.get("content") or ""),
                source_kind=str(payload.get("source_kind") or "memo"),
                source_url=str(payload.get("source_url") or "") or None,
            )
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {"error": str(exc), "message": "Источник не добавлен."},
                status=400,
            )

        async def index_in_background() -> None:
            try:
                point_ids = await atlas_index_source(source)
                await asyncio.to_thread(
                    storage.atlas_mark_knowledge_indexed,
                    int(source["id"]),
                    point_id=point_ids[0] if point_ids else None,
                )
            except AtlasAIError as exc:
                await asyncio.to_thread(
                    storage.atlas_mark_knowledge_indexed,
                    int(source["id"]),
                    point_id=None,
                    error=str(exc),
                )
            except Exception as exc:
                await asyncio.to_thread(
                    storage.atlas_mark_knowledge_indexed,
                    int(source["id"]),
                    point_id=None,
                    error=f"{type(exc).__name__}: {exc}",
                )

        task = asyncio.create_task(index_in_background(), name=f"atlas-index-{int(source['id'])}")
        indexing_tasks.add(task)
        task.add_done_callback(indexing_tasks.discard)
        return web.json_response(
            {"source": source, "queued": True, "message": "Источник принят и индексируется в фоне."},
            status=202,
        )

    async def admin_overview(request: web.Request) -> web.Response:
        selected = await principal(request)
        if not selected.administrator:
            grants = await asyncio.to_thread(
                web_auth_storage.web_section_grants,
                int(guild_id),
                int(selected.user_id),
            )
            if not any(str(item["section"]) == "atlas" for item in grants):
                raise web.HTTPForbidden(text='{"error":"atlas_admin_required"}', content_type="application/json")
        snapshot, health = await asyncio.gather(
            asyncio.to_thread(storage.atlas_admin_snapshot, int(guild_id)),
            atlas_ai_health(),
        )
        guild = bot.get_guild(int(guild_id))
        return web.json_response(
            {
                **snapshot,
                "ai": health,
                "viewer": {
                    "id": int(selected.user_id),
                    "name": str(selected.display_name),
                    "administrator": bool(selected.administrator),
                    "csrf_token": str(selected.csrf_token),
                },
                "guild": {
                    "id": int(guild_id),
                    "name": str(getattr(guild, "name", "T-Mod")),
                },
            }
        )

    app.router.add_get("/atlas", atlas_index)
    app.router.add_get("/atlas/", atlas_index)
    app.router.add_get("/atlas/assets/{name}", atlas_asset)
    app.router.add_get("/api/atlas/bootstrap", bootstrap)
    app.router.add_post("/api/atlas/onboarding", onboarding)
    app.router.add_post("/api/atlas/chat", chat)
    app.router.add_get("/api/atlas/documents", documents)
    app.router.add_post("/api/atlas/documents", documents)
    app.router.add_post("/api/atlas/knowledge", knowledge)
    app.router.add_get("/api/admin/atlas", admin_overview)


__all__ = ["register_atlas_web_routes"]
