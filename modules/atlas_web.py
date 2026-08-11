"""Independent Atlas product surface hosted by the T-Mod web runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
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
    atlas_answer_stream,
    atlas_index_source,
    atlas_probe_collection,
    atlas_reset_collection,
)
from modules.atlas_catalog import (
    atlas_normalize_knowledge_scope,
)
from modules.atlas_forum_sync import (
    AtlasForumManualActionRequired,
    AtlasForumSyncError,
    AtlasForumSyncRunner,
)
from modules.atlas_knowledge import (
    ATLAS_KNOWLEDGE_MAX_FILE_BYTES,
    AtlasKnowledgeFileError,
    atlas_extract_knowledge_file,
)
from modules.consensus_web_auth import ConsensusWebPrincipal, csrf_matches
from modules.technical_log import log_technical_event
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
    forum_sync_task: asyncio.Task[None] | None = None
    forum_sync_runner: AtlasForumSyncRunner | None = None

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

    async def atlas_allowed(selected: ConsensusWebPrincipal) -> bool:
        if selected.administrator:
            return True
        try:
            grants = await asyncio.to_thread(
                web_auth_storage.web_section_grants,
                int(guild_id),
                int(selected.user_id),
            )
        except (OSError, sqlite3.Error):
            return False
        return any(str(item.get("section")) == "atlas_ai" for item in grants)

    async def require_atlas(selected: ConsensusWebPrincipal) -> None:
        if not await atlas_allowed(selected):
            raise web.HTTPForbidden(
                text=json.dumps(
                    {
                        "error": "atlas_access_required",
                        "message": "Доступ к Atlas AI выдаёт администратор T-Mod.",
                    },
                    ensure_ascii=False,
                ),
                content_type="application/json",
            )

    async def atlas_log(
        title: str,
        details: str,
        *,
        level: str = "error",
        exception: BaseException | None = None,
        dedupe_key: str,
    ) -> None:
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return
        await log_technical_event(
            bot,
            guild,
            title=f"Atlas · {title}",
            details=details,
            level=level,
            exception=exception,
            dedupe_key=dedupe_key,
            cooldown_seconds=120,
            component="atlas",
        )

    async def user_dashboard(
        request: web.Request,
        selected: ConsensusWebPrincipal,
    ) -> dict[str, Any]:
        try:
            requested_space = int(request.headers.get("X-Atlas-Space-ID") or 0) or None
        except (TypeError, ValueError):
            requested_space = None
        return await asyncio.to_thread(
            storage.atlas_dashboard,
            int(guild_id),
            int(selected.user_id),
            str(selected.display_name),
            requested_space,
        )

    async def dashboard_for(
        request: web.Request,
        selected: ConsensusWebPrincipal,
    ) -> dict[str, Any]:
        allowed = await atlas_allowed(selected)
        if not allowed:
            return {
                "preview": True,
                "viewer": {
                    "id": int(selected.user_id),
                    "name": str(selected.display_name),
                    "administrator": False,
                    "account_tier": str(selected.account_tier),
                    "atlas_access": False,
                    "csrf_token": str(selected.csrf_token),
                },
                "release": {
                    "status": "closed_preview",
                    "modules": ["Atlas AI", "Документы", "База знаний", "Forum Desk"],
                },
                "catalog": await asyncio.to_thread(storage.atlas_catalog),
            }
        dashboard, health, forum_sync = await asyncio.gather(
            user_dashboard(request, selected),
            atlas_ai_health(),
            asyncio.to_thread(storage.atlas_forum_sync_status, int(guild_id)),
        )
        return {
            **dashboard,
            "viewer": {
                "id": int(selected.user_id),
                "name": str(selected.display_name),
                "administrator": bool(selected.administrator),
                "account_tier": str(selected.account_tier),
                "atlas_access": True,
                "csrf_token": str(selected.csrf_token),
            },
            "ai": health,
            "forum_sync": forum_sync or {
                "status": "waiting" if forum_sync_runner and forum_sync_runner.config.enabled else "disabled",
                "last_stats": {},
            },
            "capabilities": [
                "chat",
                "documents.create",
                "knowledge.add" if selected.administrator else "knowledge.read",
            ],
        }

    async def bootstrap(request: web.Request) -> web.Response:
        selected = await principal(request)
        payload = await dashboard_for(request, selected)
        return web.json_response(payload)

    async def onboarding(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        payload = await body(request, selected)
        profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
        try:
            server_code, faction_code = await asyncio.to_thread(
                storage.atlas_normalize_scope,
                str(profile.get("server_code") or "phoenix-15"),
                str(profile.get("faction_code") or "lspd"),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc), "message": "Выберите доступный сервер и фракцию."}, status=400)
        nickname = " ".join(str(profile.get("nickname") or "").split())[:80]
        rank = " ".join(str(profile.get("rank") or "").split())[:100]
        requested_step = int(payload.get("step") or 0)
        if requested_step >= 4 and (len(nickname) < 2 or len(rank) < 1):
            return web.json_response(
                {
                    "error": "atlas_onboarding_profile_required",
                    "message": "Укажите игровой ник и ранг.",
                },
                status=400,
            )
        profile = {
            **profile,
            "server_code": server_code,
            "faction_code": faction_code,
            "nickname": nickname,
            "rank": rank,
        }
        dashboard = await user_dashboard(request, selected)
        result = await asyncio.to_thread(
            storage.atlas_update_onboarding,
            int(dashboard["organization"]["id"]),
            int(selected.user_id),
            step=requested_step,
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

    async def threads(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        raw_thread_id = request.match_info.get("thread_id")
        if raw_thread_id is None:
            items = await asyncio.to_thread(
                storage.atlas_threads,
                organization_id,
                int(selected.user_id),
            )
            return web.json_response({"items": items})
        try:
            thread_id = int(raw_thread_id)
            result = await asyncio.to_thread(
                storage.atlas_thread_messages,
                organization_id,
                int(selected.user_id),
                thread_id,
            )
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "atlas_thread_not_found", "message": "Диалог не найден."},
                status=404,
            )
        return web.json_response(result)

    async def chat(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        payload = await body(request, selected)
        receipt_key, cached = cached_receipt(selected.user_id, request)
        if cached is not None:
            return web.json_response(cached)
        check_rate(selected.user_id)
        question = str(payload.get("question") or "").strip()
        try:
            server_code, faction_code = await asyncio.to_thread(
                storage.atlas_normalize_scope,
                str(payload.get("server_code") or "phoenix-15"),
                str(payload.get("faction_code") or "lspd"),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc), "message": "Выберите доступный сервер и фракцию."}, status=400)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        agent_id = str(payload.get("model") or "atlas-tvr-a").strip().lower()
        thread_id: int | None = None
        history: list[dict[str, Any]] = []
        raw_thread_id = payload.get("thread_id")
        if raw_thread_id is not None and raw_thread_id != "":
            try:
                thread_id = int(raw_thread_id)
                thread = await asyncio.to_thread(
                    storage.atlas_thread_messages,
                    organization_id,
                    int(selected.user_id),
                    thread_id,
                    limit=80,
                )
                history = list(thread["messages"])
                agent_id = str(thread["thread"].get("agent_id") or "atlas-tvr-a")
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": "atlas_thread_not_found", "message": "Выбранный диалог недоступен."},
                    status=404,
                )
        memory = await asyncio.to_thread(
            storage.atlas_recent_chat_memory,
            organization_id,
            int(selected.user_id),
            exclude_thread_id=thread_id,
            agent_id=agent_id,
        )
        try:
            answer = await atlas_answer(
                organization_id,
                question,
                server_code=server_code,
                faction_code=faction_code,
                history=history,
                memory=memory,
                response_mode=str(payload.get("response_mode") or "balanced"),
                model_id=agent_id,
                user_profile=dict(dashboard["membership"].get("profile") or {}),
            )
        except AtlasAIError as exc:
            if exc.code in {
                "atlas_index_missing",
                "atlas_index_recovery_required",
            }:
                queue_index_reconciliation(
                    force_reset=exc.code == "atlas_index_recovery_required"
                )
            await atlas_log(
                "ответ временно недоступен",
                f"Пользователь: `{selected.user_id}`\nКод: `{exc.code}`\nОшибка: `{str(exc)[:1000]}`",
                level="warning" if exc.retryable else "error",
                exception=exc,
                dedupe_key=f"atlas-chat:{exc.code}",
            )
            return web.json_response(
                {"error": exc.code, "message": str(exc), "retryable": exc.retryable},
                status=503 if exc.retryable or exc.code.endswith("not_configured") else 400,
            )
        except Exception as exc:  # noqa: BLE001 - keep the web runtime alive
            await atlas_log(
                "непредвиденная ошибка ответа",
                f"Пользователь: `{selected.user_id}`\nОшибка: `{type(exc).__name__}: {str(exc)[:1000]}`",
                exception=exc,
                dedupe_key=f"atlas-chat-unexpected:{type(exc).__name__}",
            )
            return web.json_response(
                {
                    "error": "atlas_internal_error",
                    "message": "Atlas временно не смог обработать запрос. Ошибка уже записана.",
                    "retryable": True,
                },
                status=503,
            )
        if thread_id is None:
            thread_id = await asyncio.to_thread(
                storage.atlas_create_thread,
                organization_id,
                int(selected.user_id),
                question[:100],
                agent_id=agent_id,
            )
        await asyncio.to_thread(storage.atlas_add_message, thread_id, "user", question)
        assistant_message_id = await asyncio.to_thread(
            storage.atlas_add_message,
            thread_id,
            "assistant",
            answer["answer"],
            citations=answer["citations"],
            model=answer["model"],
            latency_ms=answer["latency_ms"],
        )
        await asyncio.to_thread(
            storage.atlas_record_event,
            organization_id,
            int(selected.user_id),
            "ai_answer_created",
            "Atlas ответил на запрос",
            target_type="ai_thread",
            target_id=thread_id,
            details={
                "source": "web",
                "model": answer["model"],
                "latency_ms": answer["latency_ms"],
            },
        )
        stored_thread = await asyncio.to_thread(
            storage.atlas_thread_messages,
            organization_id,
            int(selected.user_id),
            thread_id,
            limit=1,
        )
        response = {
            **answer,
            "thread_id": thread_id,
            "message_id": assistant_message_id,
            "thread": stored_thread["thread"],
        }
        receipts[(int(selected.user_id), receipt_key)] = (time.monotonic() + 300, response)
        return web.json_response(response)

    async def chat_stream(request: web.Request) -> web.StreamResponse | web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        payload = await body(request, selected)
        receipt_key, cached = cached_receipt(selected.user_id, request)
        if cached is not None:
            response = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "text/event-stream; charset=utf-8",
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "X-Accel-Buffering": "no",
                },
            )
            await response.prepare(request)
            for event in (
                {"type": "start", "thread_id": cached.get("thread_id")},
                {"type": "done", **cached},
            ):
                await response.write(
                    ("data:" + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")
                )
            await response.write_eof()
            return response
        check_rate(selected.user_id)
        question = str(payload.get("question") or "").strip()
        try:
            server_code, faction_code = await asyncio.to_thread(
                storage.atlas_normalize_scope,
                str(payload.get("server_code") or "phoenix-15"),
                str(payload.get("faction_code") or "lspd"),
            )
        except ValueError as exc:
            return web.json_response(
                {"error": str(exc), "message": "Выберите доступный сервер и фракцию."},
                status=400,
            )
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        agent_id = str(payload.get("model") or "atlas-tvr-a").strip().lower()
        thread_id: int | None = None
        history: list[dict[str, Any]] = []
        raw_thread_id = payload.get("thread_id")
        if raw_thread_id is not None and raw_thread_id != "":
            try:
                thread_id = int(raw_thread_id)
                thread = await asyncio.to_thread(
                    storage.atlas_thread_messages,
                    organization_id,
                    int(selected.user_id),
                    thread_id,
                    limit=80,
                )
                history = list(thread["messages"])
                agent_id = str(thread["thread"].get("agent_id") or "atlas-tvr-a")
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": "atlas_thread_not_found", "message": "Выбранный диалог недоступен."},
                    status=404,
                )
        memory = await asyncio.to_thread(
            storage.atlas_recent_chat_memory,
            organization_id,
            int(selected.user_id),
            exclude_thread_id=thread_id,
            agent_id=agent_id,
        )
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        connected = True

        async def emit(event: dict[str, Any]) -> None:
            nonlocal connected
            if not connected:
                return
            try:
                data = "data:" + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n"
                await response.write(data.encode("utf-8"))
            except (ConnectionError, RuntimeError):
                connected = False

        await emit({"type": "start", "thread_id": thread_id})
        try:
            answer = await atlas_answer_stream(
                organization_id,
                question,
                on_delta=lambda text: emit({"type": "delta", "text": text}),
                on_progress=lambda event: emit({"type": "progress", **event}),
                server_code=server_code,
                faction_code=faction_code,
                history=history,
                memory=memory,
                response_mode=str(payload.get("response_mode") or "balanced"),
                model_id=agent_id,
                user_profile=dict(dashboard["membership"].get("profile") or {}),
            )
            if thread_id is None:
                thread_id = await asyncio.to_thread(
                    storage.atlas_create_thread,
                    organization_id,
                    int(selected.user_id),
                    question[:100],
                    agent_id=agent_id,
                )
            await asyncio.to_thread(storage.atlas_add_message, thread_id, "user", question)
            assistant_message_id = await asyncio.to_thread(
                storage.atlas_add_message,
                thread_id,
                "assistant",
                answer["answer"],
                citations=answer["citations"],
                model=answer["model"],
                latency_ms=answer["latency_ms"],
            )
            await asyncio.to_thread(
                storage.atlas_record_event,
                organization_id,
                int(selected.user_id),
                "ai_answer_created",
                "Atlas ответил на запрос",
                target_type="ai_thread",
                target_id=thread_id,
                details={
                    "source": "web-stream",
                    "model": answer["model"],
                    "latency_ms": answer["latency_ms"],
                },
            )
            stored_thread = await asyncio.to_thread(
                storage.atlas_thread_messages,
                organization_id,
                int(selected.user_id),
                thread_id,
                limit=1,
            )
            result = {
                **answer,
                "thread_id": thread_id,
                "message_id": assistant_message_id,
                "thread": stored_thread["thread"],
            }
            receipts[(int(selected.user_id), receipt_key)] = (time.monotonic() + 300, result)
            await emit({"type": "done", **result})
        except AtlasAIError as exc:
            if exc.code in {"atlas_index_missing", "atlas_index_recovery_required"}:
                queue_index_reconciliation(force_reset=exc.code == "atlas_index_recovery_required")
            await atlas_log(
                "потоковый ответ временно недоступен",
                f"Пользователь: `{selected.user_id}`\nКод: `{exc.code}`\nОшибка: `{str(exc)[:1000]}`",
                level="warning" if exc.retryable else "error",
                exception=exc,
                dedupe_key=f"atlas-chat-stream:{exc.code}",
            )
            await emit(
                {
                    "type": "error",
                    "error": exc.code,
                    "message": str(exc),
                    "retryable": exc.retryable,
                }
            )
        except Exception as exc:  # noqa: BLE001 - preserve the long-lived web runtime
            await atlas_log(
                "непредвиденная ошибка потокового ответа",
                f"Пользователь: `{selected.user_id}`\nОшибка: `{type(exc).__name__}: {str(exc)[:1000]}`",
                exception=exc,
                dedupe_key=f"atlas-chat-stream-unexpected:{type(exc).__name__}",
            )
            await emit(
                {
                    "type": "error",
                    "error": "atlas_internal_error",
                    "message": "Atlas временно не смог обработать запрос. Ошибка уже записана.",
                    "retryable": True,
                }
            )
        if connected:
            try:
                await response.write_eof()
            except (ConnectionError, RuntimeError):
                pass
        return response

    async def documents(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
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

    async def message_feedback(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        payload = await body(request, selected)
        dashboard = await user_dashboard(request, selected)
        try:
            message_id = int(request.match_info.get("message_id") or 0)
            feedback = await asyncio.to_thread(
                storage.atlas_set_message_feedback,
                int(dashboard["organization"]["id"]),
                int(selected.user_id),
                message_id,
                str(payload.get("rating") or ""),
                comment=str(payload.get("comment") or "") or None,
            )
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {"error": str(exc), "message": "Не удалось сохранить оценку ответа."},
                status=400,
            )
        await asyncio.to_thread(
            storage.atlas_record_event,
            int(dashboard["organization"]["id"]),
            int(selected.user_id),
            "ai_answer_feedback",
            "Пользователь оценил ответ Atlas",
            target_type="ai_message",
            target_id=message_id,
            details={"rating": feedback["rating"]},
        )
        return web.json_response({"feedback": feedback})

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

    async def index_synced_source(source: dict[str, Any]) -> list[str]:
        async with index_lock:
            return await atlas_index_source(source)

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

    def queue_forum_listing_import(
        *,
        source_url: str,
        organization_id: int,
        actor_user_id: int,
        server_code: str,
        faction_code: str,
        visibility_scope: str,
        knowledge_domain: str | None,
        corpus_kind: str | None,
    ) -> None:
        """Read large forum sections asynchronously and index every topic."""

        feed_key = "manual-" + hashlib.sha256(
            source_url.strip().casefold().encode("utf-8")
        ).hexdigest()[:20]

        async def import_in_background() -> None:
            try:
                if forum_sync_runner is None:
                    raise AtlasForumSyncError("atlas_forum_sync_disabled")
                batch = await forum_sync_runner.fetch_listing(source_url)
                created = 0
                changed = 0
                for snapshot in batch.snapshots:
                    result = await asyncio.to_thread(
                        storage.atlas_upsert_synced_knowledge,
                        int(organization_id),
                        title=snapshot.title,
                        content=snapshot.content,
                        source_url=snapshot.url,
                        server_code=server_code,
                        faction_code=faction_code,
                        visibility_scope=visibility_scope,
                        feed_key=feed_key,
                        metadata={
                            "author": snapshot.author,
                            "source_updated_at": snapshot.source_updated_at,
                            "import_mode": "authenticated_forum_listing",
                            "listing_url": source_url,
                            "requested_by_id": int(actor_user_id),
                            "knowledge_domain": knowledge_domain,
                            "corpus_kind": corpus_kind,
                        },
                    )
                    created += int(bool(result["created"]))
                    changed += int(bool(result["changed"]))
                    source = result["source"]
                    if result["changed"] or source.get("status") != "indexed":
                        queue_knowledge_index(source)
                await asyncio.to_thread(
                    storage.atlas_record_event,
                    int(organization_id),
                    int(actor_user_id),
                    "forum_listing_imported",
                    f"Atlas прочитал раздел форума: {len(batch.snapshots)} тем",
                    target_type="forum_listing",
                    target_id=source_url,
                    details={
                        "created": created,
                        "changed": changed,
                        "skipped": len(batch.skipped_threads),
                        "inventory_complete": batch.inventory_complete,
                    },
                )
                await atlas_log(
                    "раздел форума прочитан",
                    (
                        f"Тем: **{len(batch.snapshots)}** · новых: **{created}** · "
                        f"обновлено: **{changed}** · пропущено: **{len(batch.skipped_threads)}**"
                    ),
                    level="info",
                    dedupe_key=f"atlas-forum-listing-ok:{feed_key}",
                )
            except Exception as exc:
                await atlas_log(
                    "не удалось прочитать раздел форума",
                    (
                        f"Ссылка: `{source_url[:800]}`\n"
                        f"Ошибка: `{type(exc).__name__}: {str(exc)[:1000]}`\n"
                        "Живой Chromium: `http://127.0.0.1:7900/?autoconnect=1&resize=scale`"
                    ),
                    level="warning",
                    exception=exc,
                    dedupe_key=f"atlas-forum-listing-error:{feed_key}",
                )

        task = asyncio.create_task(
            import_in_background(),
            name=f"atlas-forum-listing-{feed_key}",
        )
        indexing_tasks.add(task)
        task.add_done_callback(indexing_tasks.discard)

    forum_sync_runner = AtlasForumSyncRunner(
        bot,
        int(guild_id),
        index_callback=index_synced_source,
    )

    async def start_index_reconciliation(_: web.Application) -> None:
        queue_index_reconciliation()

    async def start_forum_sync(_: web.Application) -> None:
        nonlocal forum_sync_task
        if forum_sync_runner is None or not forum_sync_runner.config.enabled:
            return
        forum_sync_task = asyncio.create_task(
            forum_sync_runner.run(),
            name="atlas-forum-sync",
        )

    async def stop_index_reconciliation(_: web.Application) -> None:
        for task in tuple(indexing_tasks):
            task.cancel()
        if indexing_tasks:
            await asyncio.gather(*tuple(indexing_tasks), return_exceptions=True)

    async def stop_forum_sync(_: web.Application) -> None:
        if forum_sync_runner is not None:
            await forum_sync_runner.close()
        if forum_sync_task is not None:
            forum_sync_task.cancel()
            await asyncio.gather(forum_sync_task, return_exceptions=True)

    app.on_startup.append(start_index_reconciliation)
    app.on_startup.append(start_forum_sync)
    app.on_cleanup.append(stop_forum_sync)
    app.on_cleanup.append(stop_index_reconciliation)

    async def knowledge(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        if request.method != "GET" and not selected.administrator:
            raise web.HTTPForbidden(
                text='{"error":"atlas_knowledge_admin_required"}',
                content_type="application/json",
            )
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        if request.method == "GET":
            try:
                server_code, faction_code = await asyncio.to_thread(
                    storage.atlas_normalize_scope,
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
            server_code, faction_code = await asyncio.to_thread(
                storage.atlas_normalize_scope,
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
                knowledge_domain=str(payload.get("knowledge_domain") or "") or None,
                corpus_kind=str(payload.get("corpus_kind") or "") or None,
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
                    "knowledge_domain",
                    "corpus_kind",
                }:
                    values[str(field.name)] = (await field.text()).strip()
            extracted = await asyncio.to_thread(atlas_extract_knowledge_file, filename, bytes(file_data))
            server_code, faction_code = await asyncio.to_thread(
                storage.atlas_normalize_scope,
                values.get("server_code", "phoenix-15"),
                values.get("faction_code", "lspd"),
            )
            visibility_scope = atlas_normalize_knowledge_scope(
                values.get("visibility_scope", "server")
            )
            dashboard = await user_dashboard(request, selected)
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
                knowledge_domain=values.get("knowledge_domain") or None,
                corpus_kind=values.get("corpus_kind") or None,
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

    async def knowledge_import_forum(request: web.Request) -> web.Response:
        selected = await principal(request)
        if not selected.administrator:
            raise web.HTTPForbidden(
                text='{"error":"atlas_knowledge_admin_required"}',
                content_type="application/json",
            )
        payload = await body(request, selected)
        if forum_sync_runner is None:
            return web.json_response(
                {
                    "error": "atlas_forum_sync_disabled",
                    "message": "Браузер Atlas ещё запускается. Повторите через несколько секунд.",
                },
                status=503,
            )
        source_url = str(payload.get("source_url") or "").strip()
        if forum_sync_runner.is_configured_listing_url(source_url):
            if not forum_sync_runner.trigger():
                return web.json_response(
                    {
                        "error": "atlas_forum_sync_disabled",
                        "message": "Автоматическое обновление форума отключено.",
                    },
                    status=409,
                )
            return web.json_response(
                {
                    "queued": True,
                    "bulk": True,
                    "message": (
                        "Раздел форума принят. Atlas обойдёт все страницы и темы, "
                        "после чего обновит библиотеку и поиск."
                    ),
                },
                status=202,
            )
        if forum_sync_runner.is_forum_listing_url(source_url):
            try:
                server_code, faction_code = await asyncio.to_thread(
                    storage.atlas_normalize_scope,
                    str(payload.get("server_code") or "phoenix-15"),
                    str(payload.get("faction_code") or "lspd"),
                )
                visibility_scope = atlas_normalize_knowledge_scope(
                    str(payload.get("visibility_scope") or "server")
                )
                dashboard = await user_dashboard(request, selected)
            except (TypeError, ValueError) as exc:
                return web.json_response(
                    {"error": str(exc), "message": "Проверьте сервер, организацию и доступ материала."},
                    status=400,
                )
            queue_forum_listing_import(
                source_url=source_url,
                organization_id=int(dashboard["organization"]["id"]),
                actor_user_id=int(selected.user_id),
                server_code=server_code,
                faction_code=faction_code,
                visibility_scope=visibility_scope,
                knowledge_domain=str(payload.get("knowledge_domain") or "") or None,
                corpus_kind=str(payload.get("corpus_kind") or "") or None,
            )
            return web.json_response(
                {
                    "queued": True,
                    "bulk": True,
                    "browser_url": "http://127.0.0.1:7900/?autoconnect=1&resize=scale",
                    "message": (
                        "Раздел принят. Atlas в фоне обойдёт все страницы и добавит каждую тему "
                        "отдельным материалом."
                    ),
                },
                status=202,
            )
        try:
            server_code, faction_code = await asyncio.to_thread(
                storage.atlas_normalize_scope,
                str(payload.get("server_code") or "phoenix-15"),
                str(payload.get("faction_code") or "lspd"),
            )
            visibility_scope = atlas_normalize_knowledge_scope(
                str(payload.get("visibility_scope") or "server")
            )
            snapshot = await forum_sync_runner.fetch_thread(source_url)
            dashboard = await user_dashboard(request, selected)
            source = await asyncio.to_thread(
                storage.atlas_add_knowledge,
                int(dashboard["organization"]["id"]),
                int(selected.user_id),
                title=snapshot.title,
                content=snapshot.content,
                source_kind="forum",
                source_url=snapshot.url,
                server_code=server_code,
                faction_code=faction_code,
                visibility_scope=visibility_scope,
                knowledge_domain=str(payload.get("knowledge_domain") or "") or None,
                corpus_kind=str(payload.get("corpus_kind") or "") or None,
                metadata={
                    "author": snapshot.author,
                    "source_updated_at": snapshot.source_updated_at,
                    "import_mode": "authenticated_forum_thread",
                },
            )
        except (AtlasForumSyncError, TypeError, ValueError) as exc:
            code = str(exc)
            if isinstance(exc, AtlasForumManualActionRequired):
                message = (
                    f"{code} Откройте живой Chromium на домашнем сервере, завершите вход "
                    "и повторите импорт."
                )
            elif "thread_body_missing" in code or "content_too_short" in code:
                message = (
                    "Страница открылась, но Atlas не нашёл в ней текст первого сообщения. "
                    "Проверьте, что это ссылка на тему и аккаунт видит её содержимое."
                )
            elif "browser_unavailable" in code or "page_failed" in code:
                message = "Chromium Atlas не смог открыть страницу. Повторите через несколько секунд."
            elif "url_invalid" in code:
                message = "Нужна ссылка Majestic Forum на тему /threads/... или раздел /forums/... ."
            else:
                message = f"Не удалось прочитать тему: {code[:300]}"
            return web.json_response(
                {
                    "error": code,
                    "message": message,
                    "browser_url": "http://127.0.0.1:7900/?autoconnect=1&resize=scale",
                },
                status=400,
            )
        queue_knowledge_index(source)
        taxonomy = dict(source.get("metadata", {})).get("taxonomy", {})
        return web.json_response(
            {
                "source": source,
                "taxonomy": taxonomy,
                "queued": True,
                "message": "Тема прочитана, классифицирована и добавлена в библиотеку.",
            },
            status=202,
        )

    async def require_atlas_admin(selected: ConsensusWebPrincipal) -> None:
        if not selected.administrator:
            grants = await asyncio.to_thread(
                web_auth_storage.web_section_grants,
                int(guild_id),
                int(selected.user_id),
            )
            if not any(str(item["section"]) == "atlas" for item in grants):
                raise web.HTTPForbidden(text='{"error":"atlas_admin_required"}', content_type="application/json")

    async def admin_overview(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas_admin(selected)
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
                    "account_tier": str(selected.account_tier),
                    "csrf_token": str(selected.csrf_token),
                },
                "guild": {
                    "id": int(guild_id),
                    "name": str(getattr(guild, "name", "T-Mod")),
                },
            }
        )

    async def admin_catalog_control(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas_admin(selected)
        payload = await body(request, selected)
        resource = str(payload.get("resource") or "").strip().lower()
        try:
            if resource == "server":
                item = await asyncio.to_thread(
                    storage.atlas_upsert_server,
                    int(selected.user_id),
                    code=str(payload.get("code") or ""),
                    name=str(payload.get("name") or ""),
                    number=payload.get("number"),
                    enabled=bool(payload.get("enabled", True)),
                )
            elif resource == "faction":
                item = await asyncio.to_thread(
                    storage.atlas_upsert_faction,
                    int(selected.user_id),
                    code=str(payload.get("code") or ""),
                    name=str(payload.get("name") or ""),
                    short_name=str(payload.get("short_name") or ""),
                    enabled=bool(payload.get("enabled", True)),
                )
            else:
                raise ValueError("atlas_catalog_resource_invalid")
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {"error": str(exc), "message": "Проверьте код и название элемента Atlas."},
                status=400,
            )
        return web.json_response(
            {"item": item, "catalog": await asyncio.to_thread(storage.atlas_catalog)},
            status=201,
        )

    async def admin_space_control(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas_admin(selected)
        payload = await body(request, selected)
        try:
            item = await asyncio.to_thread(
                storage.atlas_create_organization,
                int(guild_id),
                int(selected.user_id),
                name=str(payload.get("name") or ""),
                slug=str(payload.get("slug") or "") or None,
                owner_user_id=int(payload.get("owner_user_id") or 0),
                kind=str(payload.get("kind") or "government"),
                description=str(payload.get("description") or "") or None,
                server_code=str(payload.get("server_code") or "phoenix-15"),
                faction_code=str(payload.get("faction_code") or "lspd"),
            )
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {"error": str(exc), "message": "Не удалось создать пространство Atlas."},
                status=400,
            )
        return web.json_response({"organization": item}, status=201)

    async def forum_sync_control(request: web.Request) -> web.Response:
        selected = await principal(request)
        if not selected.administrator:
            raise web.HTTPForbidden(
                text='{"error":"atlas_forum_admin_required"}',
                content_type="application/json",
            )
        if request.method == "POST":
            await body(request, selected)
            if forum_sync_runner is None or not forum_sync_runner.trigger():
                return web.json_response(
                    {
                        "error": "atlas_forum_sync_disabled",
                        "message": "Автоматическое обновление форума отключено.",
                    },
                    status=409,
                )
        status = await asyncio.to_thread(
            storage.atlas_forum_sync_status,
            int(guild_id),
        )
        return web.json_response(
            {
                "accepted": request.method == "POST",
                "status": status or {
                    "status": (
                        "waiting"
                        if forum_sync_runner and forum_sync_runner.config.enabled
                        else "disabled"
                    ),
                    "last_stats": {},
                },
            },
            status=202 if request.method == "POST" else 200,
        )

    app.router.add_get("/atlas", atlas_index)
    app.router.add_get("/atlas/", atlas_index)
    app.router.add_get("/atlas/assets/{name}", atlas_asset)
    app.router.add_get("/api/atlas/bootstrap", bootstrap)
    app.router.add_post("/api/atlas/onboarding", onboarding)
    app.router.add_post("/api/atlas/chat", chat)
    app.router.add_post("/api/atlas/chat/stream", chat_stream)
    app.router.add_post("/api/atlas/messages/{message_id}/feedback", message_feedback)
    app.router.add_get("/api/atlas/threads", threads)
    app.router.add_get("/api/atlas/threads/{thread_id}", threads)
    app.router.add_get("/api/atlas/documents", documents)
    app.router.add_post("/api/atlas/documents", documents)
    app.router.add_get("/api/atlas/knowledge", knowledge)
    app.router.add_post("/api/atlas/knowledge", knowledge)
    app.router.add_post("/api/atlas/knowledge/upload", knowledge_upload)
    app.router.add_post("/api/atlas/knowledge/import-forum", knowledge_import_forum)
    app.router.add_get("/api/atlas/forum-sync", forum_sync_control)
    app.router.add_post("/api/atlas/forum-sync", forum_sync_control)
    app.router.add_get("/api/admin/atlas", admin_overview)
    app.router.add_post("/api/admin/atlas/catalog", admin_catalog_control)
    app.router.add_post("/api/admin/atlas/spaces", admin_space_control)


__all__ = ["register_atlas_web_routes"]
