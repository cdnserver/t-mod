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

from modules.atlas_ai import (
    AtlasAIError,
    atlas_ai_health,
    atlas_answer,
    atlas_index_source,
    atlas_probe_collection,
    atlas_reset_collection,
)
from modules.atlas_catalog import (
    atlas_catalog,
    atlas_normalize_knowledge_scope,
    atlas_normalize_scope,
)
from modules.atlas_knowledge import (
    ATLAS_KNOWLEDGE_MAX_FILE_BYTES,
    AtlasKnowledgeFileError,
    atlas_extract_knowledge_file,
)
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
    index_lock = asyncio.Lock()
    rebuild_task: asyncio.Task[None] | None = None

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
                "catalog": atlas_catalog(),
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
        profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
        try:
            server_code, faction_code = atlas_normalize_scope(
                str(profile.get("server_code") or "phoenix-15"),
                str(profile.get("faction_code") or "lspd"),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc), "message": "Выберите доступный сервер и фракцию."}, status=400)
        profile = {**profile, "server_code": server_code, "faction_code": faction_code}
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
            profile=profile,
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
        try:
            server_code, faction_code = atlas_normalize_scope(
                str(payload.get("server_code") or "phoenix-15"),
                str(payload.get("faction_code") or "lspd"),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc), "message": "Выберите доступный сервер и фракцию."}, status=400)
        dashboard = await asyncio.to_thread(
            storage.atlas_dashboard,
            int(guild_id),
            int(selected.user_id),
            str(selected.display_name),
        )
        organization_id = int(dashboard["organization"]["id"])
        try:
            answer = await atlas_answer(
                organization_id,
                question,
                server_code=server_code,
                faction_code=faction_code,
            )
        except AtlasAIError as exc:
            if exc.code in {
                "atlas_index_missing",
                "atlas_index_recovery_required",
            }:
                queue_index_reconciliation(
                    force_reset=exc.code == "atlas_index_recovery_required"
                )
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

    async def index_source(source: dict[str, Any]) -> None:
        point_ids = await atlas_index_source(source)
        await asyncio.to_thread(
            storage.atlas_mark_knowledge_indexed,
            int(source["id"]),
            point_id=point_ids[0] if point_ids else None,
        )

    async def mark_index_error(source: dict[str, Any], exc: BaseException) -> None:
        await asyncio.to_thread(
            storage.atlas_mark_knowledge_indexed,
            int(source["id"]),
            point_id=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    async def reconcile_knowledge_index(*, force_reset: bool = False) -> None:
        sources = await asyncio.to_thread(storage.atlas_indexable_knowledge_sources)
        if not sources:
            return
        async with index_lock:
            probe = await atlas_probe_collection()
            reset = force_reset or probe["status"] == "corrupted"
            if reset:
                await atlas_reset_collection()
            indexed_count = sum(item.get("status") == "indexed" for item in sources)
            rebuild_all = (
                reset
                or probe["status"] == "missing"
                or int(probe.get("points_count") or 0) < indexed_count
            )
            targets = (
                sources
                if rebuild_all
                else [item for item in sources if item.get("status") != "indexed"]
            )
            for source in targets:
                try:
                    await index_source(source)
                except Exception as exc:
                    await mark_index_error(source, exc)
                    if isinstance(exc, AtlasAIError) and exc.retryable:
                        raise

    def queue_index_reconciliation(*, force_reset: bool = False) -> None:
        nonlocal rebuild_task
        if rebuild_task is not None and not rebuild_task.done():
            return

        async def runner() -> None:
            reset_requested = force_reset
            for delay in (0, 3, 10, 30, 60):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    await reconcile_knowledge_index(force_reset=reset_requested)
                    return
                except Exception:
                    reset_requested = False
                    continue

        rebuild_task = asyncio.create_task(runner(), name="atlas-index-reconciliation")
        indexing_tasks.add(rebuild_task)
        rebuild_task.add_done_callback(indexing_tasks.discard)

    def queue_knowledge_index(source: dict[str, Any]) -> None:
        async def index_in_background() -> None:
            try:
                async with index_lock:
                    await index_source(source)
            except AtlasAIError as exc:
                await mark_index_error(source, exc)
                if exc.code == "qdrant_index_corrupted":
                    queue_index_reconciliation(force_reset=True)
            except Exception as exc:
                await mark_index_error(source, exc)

        task = asyncio.create_task(index_in_background(), name=f"atlas-index-{int(source['id'])}")
        indexing_tasks.add(task)
        task.add_done_callback(indexing_tasks.discard)

    async def start_index_reconciliation(_: web.Application) -> None:
        queue_index_reconciliation()

    async def stop_index_reconciliation(_: web.Application) -> None:
        for task in tuple(indexing_tasks):
            task.cancel()
        if indexing_tasks:
            await asyncio.gather(*tuple(indexing_tasks), return_exceptions=True)

    app.on_startup.append(start_index_reconciliation)
    app.on_cleanup.append(stop_index_reconciliation)

    async def knowledge(request: web.Request) -> web.Response:
        selected = await principal(request)
        if not selected.administrator:
            raise web.HTTPForbidden(
                text='{"error":"atlas_knowledge_admin_required"}',
                content_type="application/json",
            )
        dashboard = await asyncio.to_thread(
            storage.atlas_dashboard,
            int(guild_id),
            int(selected.user_id),
            str(selected.display_name),
        )
        organization_id = int(dashboard["organization"]["id"])
        if request.method == "GET":
            try:
                server_code, faction_code = atlas_normalize_scope(
                    str(request.query.get("server_code") or "phoenix-15"),
                    str(request.query.get("faction_code") or "lspd"),
                )
            except ValueError as exc:
                return web.json_response({"error": str(exc), "message": "Неизвестный раздел библиотеки."}, status=400)
            items = await asyncio.to_thread(
                storage.atlas_knowledge_sources,
                organization_id,
                server_code=server_code,
                faction_code=faction_code,
            )
            return web.json_response({"items": items, "server_code": server_code, "faction_code": faction_code})

        payload = await body(request, selected)
        try:
            server_code, faction_code = atlas_normalize_scope(
                str(payload.get("server_code") or "phoenix-15"),
                str(payload.get("faction_code") or "lspd"),
            )
            visibility_scope = atlas_normalize_knowledge_scope(
                str(payload.get("visibility_scope") or "server")
            )
            source = await asyncio.to_thread(
                storage.atlas_add_knowledge,
                organization_id,
                int(selected.user_id),
                title=str(payload.get("title") or ""),
                content=str(payload.get("content") or ""),
                source_kind=str(payload.get("source_kind") or "memo"),
                source_url=str(payload.get("source_url") or "") or None,
                server_code=server_code,
                faction_code=faction_code,
                visibility_scope=visibility_scope,
            )
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {"error": str(exc), "message": "Источник не добавлен."},
                status=400,
            )

        queue_knowledge_index(source)
        return web.json_response(
            {"source": source, "queued": True, "message": "Материал принят. Atlas готовит его для поиска."},
            status=202,
        )

    async def knowledge_upload(request: web.Request) -> web.Response:
        selected = await principal(request)
        if not selected.administrator:
            raise web.HTTPForbidden(text='{"error":"atlas_knowledge_admin_required"}', content_type="application/json")
        if not csrf_matches(request, selected):
            return web.json_response({"error": "csrf_failed", "message": "Защитная сессия устарела."}, status=403)
        if not request.content_type.startswith("multipart/"):
            return web.json_response({"error": "atlas_upload_multipart_required", "message": "Выберите файл для загрузки."}, status=400)

        values: dict[str, str] = {}
        filename = ""
        file_data = bytearray()
        try:
            reader = await request.multipart()
            async for field in reader:
                if field.name == "file":
                    if filename:
                        raise AtlasKnowledgeFileError("atlas_upload_single_file", "Загружайте материалы по одному.")
                    filename = str(field.filename or "")
                    while chunk := await field.read_chunk(size=256 * 1024):
                        file_data.extend(chunk)
                        if len(file_data) > ATLAS_KNOWLEDGE_MAX_FILE_BYTES:
                            raise AtlasKnowledgeFileError("atlas_file_too_large", "Файл превышает лимит 8 МБ.")
                elif field.name in {
                    "title",
                    "source_kind",
                    "source_url",
                    "server_code",
                    "faction_code",
                    "visibility_scope",
                }:
                    values[str(field.name)] = (await field.text()).strip()
            extracted = await asyncio.to_thread(atlas_extract_knowledge_file, filename, bytes(file_data))
            server_code, faction_code = atlas_normalize_scope(
                values.get("server_code", "phoenix-15"),
                values.get("faction_code", "lspd"),
            )
            visibility_scope = atlas_normalize_knowledge_scope(
                values.get("visibility_scope", "server")
            )
            dashboard = await asyncio.to_thread(
                storage.atlas_dashboard,
                int(guild_id),
                int(selected.user_id),
                str(selected.display_name),
            )
            source = await asyncio.to_thread(
                storage.atlas_add_knowledge,
                int(dashboard["organization"]["id"]),
                int(selected.user_id),
                title=values.get("title") or str(extracted["title"]),
                content=str(extracted["content"]),
                source_kind=values.get("source_kind") or "document",
                source_url=values.get("source_url") or None,
                server_code=server_code,
                faction_code=faction_code,
                visibility_scope=visibility_scope,
                original_filename=str(extracted["filename"]),
            )
        except (AtlasKnowledgeFileError, ValueError) as exc:
            return web.json_response(
                {"error": getattr(exc, "code", str(exc)), "message": str(exc)},
                status=400,
            )
        queue_knowledge_index(source)
        return web.json_response(
            {"source": source, "queued": True, "message": "Файл загружен. Atlas готовит его для поиска."},
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
    app.router.add_get("/api/atlas/knowledge", knowledge)
    app.router.add_post("/api/atlas/knowledge", knowledge)
    app.router.add_post("/api/atlas/knowledge/upload", knowledge_upload)
    app.router.add_get("/api/admin/atlas", admin_overview)


__all__ = ["register_atlas_web_routes"]
