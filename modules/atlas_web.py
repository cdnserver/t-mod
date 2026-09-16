"""Independent Atlas product surface hosted by the T-Mod web runtime."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import sqlite3
import time
from collections import defaultdict, deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

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
    AtlasForumSyncConfig,
    AtlasForumSyncError,
    AtlasForumSyncRunner,
)
from modules.atlas_forum_engine import AtlasForumEngineRunner
from modules.atlas_knowledge import (
    ATLAS_KNOWLEDGE_MAX_FILE_BYTES,
    AtlasKnowledgeFileError,
    atlas_extract_knowledge_file,
)
from modules.atlas_ocr import (
    AtlasOcrError,
    AtlasOcrUnavailable,
    atlas_ocr_attachment,
)
from modules.atlas_jobs import AtlasJobWorker
from modules.atlas_media import (
    AtlasLocalBlobStore,
    AtlasMediaConfig,
    AtlasMediaError,
    atlas_media_detect_type,
    atlas_media_kind_for_mime,
    atlas_media_scan,
)
from modules.atlas_tts import AtlasTTSResult, AtlasTTSService
from modules.consensus_web_auth import (
    ConsensusWebPrincipal,
    csrf_matches,
    has_trusted_forwarded_host,
    request_public_host,
)
from modules.music_providers import MusicProviderError, OpenRouterTranscriber
from modules.technical_log import log_technical_event
from persistence import atlas_repository as storage
from persistence import atlas_billing_repository as billing_storage
from persistence import atlas_forum_engine_repository as forum_engine_storage
from persistence import atlas_forum_attachment_repository as attachment_storage
from persistence import atlas_job_repository as job_storage
from persistence import atlas_case_repository as case_storage
from persistence import atlas_document_repository as document_storage
from persistence import atlas_media_repository as media_storage
from persistence import atlas_search_repository as search_storage
from persistence import craft_repository as craft_storage
from persistence import web_auth_repository as web_auth_storage


AuthenticatedRequest = Callable[
    [web.Request],
    Awaitable[tuple[ConsensusWebPrincipal | None, bool]],
]


_ATLAS_OVERLAY_AUDIO_MAX_BYTES = 6 * 1024 * 1024
_ATLAS_OVERLAY_PCM_MAX_BYTES = 48_000 * 2 * 2 * 25
_ATLAS_OVERLAY_AUDIO_MAX_SECONDS = 25
_ATLAS_OVERLAY_SCREEN_MAX_BYTES = 2 * 1024 * 1024
_ATLAS_OVERLAY_CHAT_MAX_BYTES = 3 * 1024 * 1024
_ATLAS_OVERLAY_TTS_REQUEST_MAX_BYTES = 16 * 1024
_ATLAS_OVERLAY_AUDIO_TYPES = {
    "audio/webm",
    "audio/ogg",
    "audio/mp4",
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
    "application/octet-stream",
}


def _overlay_craft_snapshot(guild_id: int, user_id: int) -> dict[str, Any]:
    """Return a small, non-administrative projection for the field overlay."""

    now = datetime.now(timezone.utc)
    plans = []
    for plan in craft_storage.craft_active_plans(int(guild_id), limit=30):
        recipe = plan.get("recipe") if isinstance(plan.get("recipe"), dict) else {}
        batch = plan.get("active_batch") if isinstance(plan.get("active_batch"), dict) else None
        materials = []
        for item in plan.get("materials") or []:
            if not isinstance(item, dict):
                continue
            required = max(0, int(item.get("required_total") or 0))
            available = max(0, int(item.get("stock_quantity") or 0))
            materials.append(
                {
                    "name": str(item.get("material_name") or "Материал")[:80],
                    "required": required,
                    "available": available,
                    "ready": available >= required,
                }
            )
        attempts_total = max(0, int(plan.get("attempts_total") or 0))
        attempts_queued = max(0, int(plan.get("attempts_queued") or 0))
        attempts_completed = max(0, int(plan.get("attempts_completed") or 0))
        stage = str(plan.get("stage") or "procurement")
        needs_next_batch = bool(
            stage == "crafting"
            and batch is None
            and attempts_queued < attempts_total
        )
        plan_id = int(plan.get("id") or 0)
        responsible_id = int(plan.get("responsible_id") or 0)
        item = {
            "id": plan_id,
            "product_name": str(
                plan.get("product_name_snapshot")
                or recipe.get("product_name")
                or "Крафт"
            )[:100],
            "stage": stage,
            "responsible": str(plan.get("responsible_display") or "Не назначен")[:100],
            "mine": responsible_id == int(user_id),
            "attempts_total": attempts_total,
            "attempts_queued": attempts_queued,
            "attempts_completed": attempts_completed,
            "remaining_to_queue": max(0, attempts_total - attempts_queued),
            "product_stock": max(0, int(plan.get("product_stock") or 0)),
            "materials": materials,
            "active_batch": (
                {
                    "id": int(batch.get("id") or 0),
                    "quantity": max(0, int(batch.get("quantity") or 0)),
                    "started_at": batch.get("started_at"),
                    "due_at": batch.get("due_at"),
                }
                if batch else None
            ),
            "needs_next_batch": needs_next_batch,
            "alarm_key": (
                f"craft:{plan_id}:next:{attempts_completed}:{attempts_queued}"
                if needs_next_batch else None
            ),
            "updated_at": plan.get("updated_at"),
        }
        plans.append(item)
    plans.sort(
        key=lambda item: (
            not bool(item["needs_next_batch"]),
            not bool(item["mine"]),
            str((item.get("active_batch") or {}).get("due_at") or "9999"),
            -int(item["id"]),
        )
    )
    raw_revision = json.dumps(plans, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "server_time": now.isoformat(),
        "revision": hashlib.sha256(raw_revision.encode("utf-8")).hexdigest()[:20],
        "plans": plans,
        "attention_count": sum(1 for item in plans if item["needs_next_batch"]),
    }


def _forum_engine_status_view(value: dict[str, Any], *, administrator: bool) -> dict[str, Any]:
    """Keep operational diagnostics private while exposing useful monitor state."""

    if administrator:
        return value
    feeds = []
    for item in value.get("feeds", []) if isinstance(value, dict) else []:
        if not isinstance(item, dict):
            continue
        feeds.append(
            {
                "section_kind": str(item.get("section_kind") or ""),
                "status": str(item.get("status") or "pending"),
                "baseline_completed": bool(item.get("baseline_completed_at")),
                "last_success_at": item.get("last_success_at"),
                "next_scan_at": item.get("next_scan_at"),
                "backfill_pages_scanned": int(item.get("backfill_pages_scanned") or 0),
                "backfill_topics_seen": int(item.get("backfill_topics_seen") or 0),
                "backfill_completed": bool(item.get("backfill_completed_at")),
            }
        )
    return {
        "feeds": feeds,
        "complaints": int(value.get("complaints") or 0),
        "open": int(value.get("open") or 0),
        "pending_hydration": int(value.get("pending_hydration") or 0),
        "hydrated": int(value.get("hydrated") or 0),
        "posts": int(value.get("posts") or 0),
    }


def _forum_engine_items_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = (
        "id", "thread_url", "title", "author", "section_kind", "post_count",
        "last_changed_at", "latest_post_at", "latest_post_author", "locked",
        "static_id", "nickname", "event_count", "participant_role",
    )
    return [
        {key: item.get(key) for key in allowed}
        for item in items
        if isinstance(item, dict)
    ]


def _is_tmod_desktop_request(request: web.Request) -> bool:
    """Recognise the product shell without treating it as authentication.

    Account, grants and CSRF remain authoritative.  This marker only keeps the
    browser product surface closed while allowing the signed-in Desktop shell.
    """

    user_agent = str(request.headers.get("User-Agent") or "").lower()
    version = str(request.headers.get("X-TMod-Desktop-Version") or "").strip()
    return "tmoddesktop/" in user_agent or bool(version)


async def _decode_overlay_audio(audio: bytes) -> bytes:
    """Decode browser MediaRecorder output into Discord-compatible PCM.

    ffmpeg reads and writes pipes only: voice clips never touch persistent
    storage, which is important for a desktop push-to-talk feature.
    """

    if not audio or len(audio) > _ATLAS_OVERLAY_AUDIO_MAX_BYTES:
        raise ValueError("atlas_overlay_audio_size_invalid")
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-t",
            str(_ATLAS_OVERLAY_AUDIO_MAX_SECONDS),
            "-f",
            "s16le",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("atlas_overlay_ffmpeg_unavailable") from exc
    try:
        pcm, stderr = await asyncio.wait_for(process.communicate(audio), timeout=12)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise TimeoutError("atlas_overlay_audio_decode_timeout") from exc
    if process.returncode or not pcm:
        message = stderr.decode("utf-8", errors="replace").strip()[:300]
        raise ValueError(f"atlas_overlay_audio_invalid:{message}")
    if len(pcm) > _ATLAS_OVERLAY_PCM_MAX_BYTES:
        raise ValueError("atlas_overlay_audio_too_long")
    return pcm


def _validated_overlay_screen_context(value: Any) -> str:
    """Validate a small in-memory gameplay frame before multimodal forwarding."""

    selected = str(value or "").strip()
    prefix, separator, encoded = selected.partition(",")
    media = prefix.removeprefix("data:").split(";", 1)[0].lower()
    if not separator or ";base64" not in prefix.lower() or media not in {
        "image/png",
        "image/jpeg",
        "image/webp",
    }:
        raise ValueError("atlas_overlay_screen_format_invalid")
    encoded_limit = ((_ATLAS_OVERLAY_SCREEN_MAX_BYTES + 2) // 3) * 4 + 8
    if len(encoded) > encoded_limit:
        raise ValueError("atlas_overlay_screen_size_invalid")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("atlas_overlay_screen_invalid") from exc
    if not raw or len(raw) > _ATLAS_OVERLAY_SCREEN_MAX_BYTES:
        raise ValueError("atlas_overlay_screen_size_invalid")
    signature_ok = (
        media == "image/png" and raw.startswith(b"\x89PNG\r\n\x1a\n")
        or media == "image/jpeg" and raw.startswith(b"\xff\xd8\xff")
        or media == "image/webp" and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
    )
    if not signature_ok:
        raise ValueError("atlas_overlay_screen_content_invalid")
    return selected


def register_atlas_web_routes(
    app: web.Application,
    bot: discord.Client,
    *,
    guild_id: int,
    asset_dir: Path,
    authenticate: AuthenticatedRequest,
) -> None:
    rates: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=30))
    voice_rates: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=40))
    tts_rates: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=40))
    receipts: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
    indexing_tasks: set[asyncio.Task[None]] = set()
    index_lock = asyncio.Lock()
    rebuild_task: asyncio.Task[None] | None = None
    forum_sync_task: asyncio.Task[None] | None = None
    forum_engine_task: asyncio.Task[None] | None = None
    attachment_ocr_task: asyncio.Task[None] | None = None
    forum_sync_runner: AtlasForumSyncRunner | None = None
    forum_engine_browser: AtlasForumSyncRunner | None = None
    forum_engine_runner: AtlasForumEngineRunner | None = None
    job_worker = AtlasJobWorker(
        concurrency=2,
        lease_seconds=180,
        poll_seconds=0.5,
    )
    media_config = AtlasMediaConfig.from_env()
    media_blobs = AtlasLocalBlobStore(media_config.root)
    overlay_transcriber = OpenRouterTranscriber()
    overlay_tts = AtlasTTSService()
    # A small bounded pool keeps simultaneous field requests responsive while
    # preventing a voice burst from exhausting worker threads/OpenRouter.
    overlay_transcription_slots = asyncio.Semaphore(3)

    async def atlas_index(request: web.Request) -> web.FileResponse:
        page = "index.html" if _is_tmod_desktop_request(request) else "install.html"
        response = web.FileResponse(asset_dir / page)
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Vary"] = "User-Agent, X-TMod-Desktop-Version"
        return response

    async def atlas_asset(request: web.Request) -> web.FileResponse:
        name = str(request.match_info.get("name") or "")
        if name not in {"app.js", "style.css", "install.css", "favicon.svg"}:
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

    def require_desktop_client(request: web.Request) -> None:
        host = request_public_host(request).partition(":")[0].lower()
        # Local/internal calls remain available for diagnostics and automated
        # tests.  The product restriction is enforced on the public contour.
        # A request carrying the gateway marker is a public request even when
        # the upstream Host is an internal Docker name.  Unknown forwarded
        # hosts are denied as well, rather than accidentally bypassing the
        # desktop-only policy.
        public_request = has_trusted_forwarded_host(request)
        if public_request and host != "dash.tvr.lat":
            raise web.HTTPForbidden(
                text=json.dumps(
                    {
                        "error": "atlas_canonical_host_required",
                        "message": "Откройте Atlas на официальном домене.",
                    },
                    ensure_ascii=False,
                ),
                content_type="application/json",
            )
        if _is_tmod_desktop_request(request):
            return
        if host != "dash.tvr.lat":
            return
        raise web.HTTPForbidden(
            text=json.dumps(
                {
                    "error": "atlas_desktop_required",
                    "message": "Atlas AI доступен в приложении T-Mod Desktop.",
                    "desktop_url": "https://github.com/cdnserver/t-mod-releases/releases/latest",
                },
                ensure_ascii=False,
            ),
            content_type="application/json",
        )

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

    def reject_oversized_chat(request: web.Request) -> None:
        length = request.content_length
        if length is not None and length > _ATLAS_OVERLAY_CHAT_MAX_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=_ATLAS_OVERLAY_CHAT_MAX_BYTES,
                actual_size=length,
            )

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

    async def record_billing_usage(
        selected: ConsensusWebPrincipal,
        organization_id: int,
        *,
        request_key: str,
        source: str,
        answer: dict[str, Any],
        message_id: int,
    ) -> None:
        """Meter compute without ever turning telemetry into an answer outage."""

        try:
            await asyncio.to_thread(
                billing_storage.atlas_record_ai_usage,
                int(selected.user_id),
                int(organization_id),
                request_key=request_key,
                source=source,
                model=str(answer.get("model") or ""),
                model_provider=str(answer.get("model_provider") or ""),
                usage=answer.get("usage"),
                message_id=int(message_id),
            )
        except Exception as exc:  # noqa: BLE001 - metering is fail-open during rollout
            try:
                await atlas_log(
                    "не удалось записать расход Atlas Token",
                    (
                        f"Пользователь: `{selected.user_id}`\n"
                        f"Запрос: `{request_key}`\n"
                        f"Ошибка: `{type(exc).__name__}: {str(exc)[:700]}`"
                    ),
                    level="warning",
                    exception=exc,
                    dedupe_key=f"atlas-billing-meter:{type(exc).__name__}",
                )
            except Exception:
                # Diagnostics must be fail-open for the same reason as billing.
                pass

    async def require_ai_balance(selected: ConsensusWebPrincipal) -> dict[str, Any]:
        try:
            entitlement = await asyncio.to_thread(
                billing_storage.atlas_ai_entitlement,
                int(selected.user_id),
            )
        except Exception as exc:  # noqa: BLE001 - rollout remains fail-open on telemetry failure
            try:
                await atlas_log(
                    "не удалось проверить баланс Atlas Token",
                    f"Пользователь: `{selected.user_id}`\nОшибка: `{type(exc).__name__}: {str(exc)[:700]}`",
                    level="warning",
                    exception=exc,
                    dedupe_key=f"atlas-billing-entitlement:{type(exc).__name__}",
                )
            except Exception:
                pass
            return {"allowed": True, "enforcement_enabled": False, "balance_tokens": None}
        if not bool(entitlement.get("allowed")):
            raise web.HTTPPaymentRequired(
                text=json.dumps(
                    {
                        "error": "atlas_tokens_required",
                        "message": "Atlas Token закончились. Пополните баланс или выберите тариф.",
                        "billing_url": "https://atlas.tvr.lat/#plans",
                        "balance_tokens": int(entitlement.get("balance_tokens") or 0),
                    },
                    ensure_ascii=False,
                ),
                content_type="application/json",
            )
        return entitlement

    async def user_dashboard(
        request: web.Request,
        selected: ConsensusWebPrincipal,
        *,
        project_code: str | None = None,
    ) -> dict[str, Any]:
        try:
            requested_space = int(request.headers.get("X-Atlas-Space-ID") or 0) or None
        except (TypeError, ValueError):
            requested_space = None
        clean_project = str(project_code or "").strip().lower() or None
        dashboard = await asyncio.to_thread(
            storage.atlas_dashboard,
            int(guild_id),
            int(selected.user_id),
            str(selected.display_name),
            requested_space,
            project_code=clean_project,
        )
        if clean_project and str(dashboard["organization"].get("project_code") or "").strip().lower() != clean_project:
            raise ValueError("atlas_space_project_mismatch")
        return dashboard

    def explicit_platform_scope(payload: dict[str, Any]) -> str | None:
        """Require a deliberate administrator confirmation before cross-project sharing.

        The old UI calls project-wide material `global`.  Treating that label as
        platform-wide would expose legacy Majestic laws to every future project,
        so it continues to map to the current project unless this dedicated
        switch and confirmation are both supplied by an administrator-only
        endpoint.
        """

        requested = str(payload.get("federation_scope") or "").strip().lower()
        if not requested:
            return None
        if requested != "platform":
            raise ValueError("atlas_federation_scope_selection_invalid")
        confirmed = str(payload.get("confirm_platform_scope") or "").strip().lower()
        if confirmed not in {"1", "true", "yes", "on", "confirm"}:
            raise ValueError("atlas_platform_scope_confirmation_required")
        return "platform"

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
        dashboard, health, forum_sync, forum_engine, forum_complaints, forum_characters = await asyncio.gather(
            user_dashboard(request, selected),
            atlas_ai_health(),
            asyncio.to_thread(storage.atlas_forum_sync_status, int(guild_id)),
            asyncio.to_thread(forum_engine_storage.forum_monitor_status, int(guild_id)),
            asyncio.to_thread(
                forum_engine_storage.user_forum_complaints,
                int(guild_id),
                int(selected.user_id),
                limit=50,
            ),
            asyncio.to_thread(
                forum_engine_storage.list_monitored_characters,
                int(guild_id),
            ),
        )
        own_forum_characters = [
            {
                "id": int(item["character_id"]),
                "nickname": str(item.get("nickname") or ""),
                "static_id": str(item.get("static_id") or ""),
            }
            for item in forum_characters
            if int(item["user_id"]) == int(selected.user_id)
        ]
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
            "forum_engine": {
                **_forum_engine_status_view(
                    forum_engine,
                    administrator=bool(selected.administrator),
                ),
                "items": _forum_engine_items_view(forum_complaints),
                "characters": own_forum_characters,
                "enabled": bool(forum_engine_runner and forum_engine_runner.config.enabled),
            },
            "capabilities": [
                "chat",
                "documents.create",
                "knowledge.add" if selected.administrator else "knowledge.read",
            ],
        }

    async def bootstrap(request: web.Request) -> web.Response:
        require_desktop_client(request)
        selected = await principal(request)
        payload = await dashboard_for(request, selected)
        return web.json_response(payload)

    async def overlay_context_get(request: web.Request) -> web.Response:
        require_desktop_client(request)
        selected = await principal(request)
        await require_atlas(selected)
        context = await asyncio.to_thread(
            storage.atlas_overlay_context,
            int(guild_id),
            int(selected.user_id),
        )
        response = web.json_response(
            {
                **context,
                "allowed": True,
                "endpoints": {
                    "context": "/api/atlas/overlay/context",
                    "transcribe": "/api/atlas/overlay/transcribe",
                    "stream": "/api/atlas/chat/stream",
                    "tts_voices": "/api/atlas/overlay/tts/voices",
                    "tts_preview": "/api/atlas/overlay/tts/preview",
                    "tts_synthesize": "/api/atlas/overlay/tts/synthesize",
                },
                "capabilities": {
                    "push_to_talk": True,
                    "spoken_reply": True,
                    "ai_voice": "system_fallback",
                    # Capture is opt-in in the character binding. The transport
                    # intentionally remains disabled until the desktop main
                    # process can redact and validate a single captured frame.
                    "screen_context": "consent_gated",
                },
            }
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    async def overlay_crafts(request: web.Request) -> web.Response:
        require_desktop_client(request)
        selected = await principal(request)
        await require_atlas(selected)
        if not selected.guild_member:
            raise web.HTTPForbidden(
                text=json.dumps(
                    {
                        "error": "craft_membership_required",
                        "message": "Контур крафтов доступен участникам Товарищества.",
                    },
                    ensure_ascii=False,
                ),
                content_type="application/json",
            )
        snapshot = await asyncio.to_thread(
            _overlay_craft_snapshot,
            int(guild_id),
            int(selected.user_id),
        )
        etag = f'"{snapshot["revision"]}"'
        if request.headers.get("If-None-Match") == etag:
            response = web.Response(status=304)
        else:
            response = web.json_response(snapshot)
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "private, no-store"
        return response

    async def overlay_context_set(request: web.Request) -> web.Response:
        require_desktop_client(request)
        selected = await principal(request)
        await require_atlas(selected)
        payload = await body(request, selected)
        try:
            character_id = int(payload.get("character_id") or 0)
            context = await asyncio.to_thread(
                storage.atlas_set_overlay_character,
                int(guild_id),
                int(selected.user_id),
                character_id,
                server_code=str(payload.get("server_code") or "phoenix-15"),
                faction_code=str(payload.get("faction_code") or ""),
                rank=(
                    str(payload.get("rank") or "")
                    if "rank" in payload
                    else None
                ),
                voice_reply_enabled=bool(payload.get("voice_reply_enabled", True)),
                screen_context_enabled=bool(payload.get("screen_context_enabled", False)),
            )
        except (TypeError, ValueError) as exc:
            code = str(exc) or "atlas_overlay_context_invalid"
            return web.json_response(
                {"error": code, "message": "Проверьте персонажа, сервер и организацию."},
                status=400 if code != "atlas_overlay_character_not_owned" else 403,
            )
        return web.json_response({"ok": True, **context})

    async def overlay_transcribe(request: web.Request) -> web.Response:
        require_desktop_client(request)
        selected = await principal(request)
        await require_atlas(selected)
        if not csrf_matches(request, selected):
            return web.json_response(
                {"error": "csrf_failed", "message": "Защитная сессия устарела."},
                status=403,
            )
        check_voice_rate(selected.user_id)
        try:
            if request.content_type.startswith("multipart/"):
                if (
                    request.content_length is not None
                    and request.content_length > _ATLAS_OVERLAY_AUDIO_MAX_BYTES + 128 * 1024
                ):
                    raise ValueError("atlas_overlay_audio_size_invalid")
                reader = await request.multipart()
                audio = bytearray()
                while part := await reader.next():
                    if str(part.name or "") != "audio":
                        await part.release()
                        continue
                    while chunk := await part.read_chunk(size=64 * 1024):
                        audio.extend(chunk)
                        if len(audio) > _ATLAS_OVERLAY_AUDIO_MAX_BYTES:
                            raise ValueError("atlas_overlay_audio_size_invalid")
                    break
            elif request.content_type in _ATLAS_OVERLAY_AUDIO_TYPES:
                if (
                    request.content_length is not None
                    and request.content_length > _ATLAS_OVERLAY_AUDIO_MAX_BYTES
                ):
                    raise ValueError("atlas_overlay_audio_size_invalid")
                audio = bytearray()
                while chunk := await request.content.read(64 * 1024):
                    audio.extend(chunk)
                    if len(audio) > _ATLAS_OVERLAY_AUDIO_MAX_BYTES:
                        raise ValueError("atlas_overlay_audio_size_invalid")
            else:
                return web.json_response(
                    {"error": "atlas_overlay_audio_format_invalid"},
                    status=400,
                )
            pcm = await _decode_overlay_audio(bytes(audio))
            if not overlay_transcriber.configured:
                return web.json_response(
                    {
                        "error": "atlas_overlay_stt_not_configured",
                        "message": "Распознавание речи ещё не настроено.",
                    },
                    status=503,
                )
            async with overlay_transcription_slots:
                transcript = await asyncio.wait_for(
                    asyncio.to_thread(overlay_transcriber.transcribe_pcm, pcm),
                    timeout=30,
                )
        except MusicProviderError as exc:
            return web.json_response(
                {"error": "atlas_overlay_stt_failed", "message": str(exc)[:300]},
                status=503,
            )
        except (ValueError, RuntimeError, TimeoutError, asyncio.TimeoutError) as exc:
            return web.json_response(
                {
                    "error": str(exc).split(":", 1)[0] or "atlas_overlay_audio_invalid",
                    "message": "Не удалось распознать голосовую команду. Повторите короче и ближе к микрофону.",
                },
                status=400,
            )
        clean = " ".join(str(transcript or "").split())[:4000]
        if not clean:
            return web.json_response(
                {"error": "atlas_overlay_speech_not_detected", "message": "Речь не распознана."},
                status=422,
            )
        return web.json_response(
            {
                "text": clean,
                "transcript": clean,
                "duration_ms": round(len(pcm) / (48_000 * 2 * 2) * 1000),
            }
        )

    def check_tts_rate(user_id: int, *, preview: bool = False) -> None:
        now = time.monotonic()
        bucket = tts_rates[int(user_id)]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        limit = 8 if preview else 20
        if len(bucket) >= limit:
            raise web.HTTPTooManyRequests(
                text=json.dumps(
                    {
                        "error": "atlas_tts_rate_limited",
                        "message": (
                            "Слишком много запросов озвучивания. "
                            "Системный голос остаётся доступен."
                        ),
                        "fallback": "system",
                    },
                    ensure_ascii=False,
                ),
                content_type="application/json",
                headers={"Retry-After": "60"},
            )
        bucket.append(now)

    def tts_response(result: AtlasTTSResult) -> web.Response:
        common_headers = {
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Atlas-TTS-Provider": result.provider,
            "X-Atlas-TTS-Voice": result.voice,
        }
        if result.fallback or result.audio is None:
            return web.Response(
                status=204,
                headers={
                    **common_headers,
                    "X-Atlas-TTS-Fallback": "system",
                    "X-Atlas-TTS-Fallback-Reason": (
                        result.reason or "provider_unavailable"
                    ),
                },
            )
        common_headers["X-Atlas-TTS-Cache"] = (
            "hit" if result.cache_hit else "miss"
        )
        if result.generation_id:
            common_headers["X-Atlas-TTS-Generation"] = result.generation_id
        return web.Response(
            body=result.audio,
            content_type=result.content_type,
            headers=common_headers,
        )

    async def overlay_tts_voices(request: web.Request) -> web.Response:
        require_desktop_client(request)
        selected = await principal(request)
        await require_atlas(selected)
        response = web.json_response(overlay_tts.voices_payload())
        response.headers["Cache-Control"] = "private, max-age=60"
        return response

    async def overlay_tts_preview(request: web.Request) -> web.Response:
        require_desktop_client(request)
        selected = await principal(request)
        await require_atlas(selected)
        if (
            request.content_length is not None
            and request.content_length > _ATLAS_OVERLAY_TTS_REQUEST_MAX_BYTES
        ):
            raise web.HTTPRequestEntityTooLarge(
                max_size=_ATLAS_OVERLAY_TTS_REQUEST_MAX_BYTES,
                actual_size=request.content_length,
            )
        payload = await body(request, selected)
        check_tts_rate(selected.user_id, preview=True)
        try:
            result = await overlay_tts.preview(
                str(payload.get("voice") or "") or None
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return tts_response(result)

    async def overlay_tts_synthesize(request: web.Request) -> web.Response:
        require_desktop_client(request)
        selected = await principal(request)
        await require_atlas(selected)
        if (
            request.content_length is not None
            and request.content_length > _ATLAS_OVERLAY_TTS_REQUEST_MAX_BYTES
        ):
            raise web.HTTPRequestEntityTooLarge(
                max_size=_ATLAS_OVERLAY_TTS_REQUEST_MAX_BYTES,
                actual_size=request.content_length,
            )
        payload = await body(request, selected)
        check_tts_rate(selected.user_id)
        try:
            result = await overlay_tts.synthesize(
                payload.get("text"),
                voice=str(payload.get("voice") or "") or None,
                speed=payload.get("speed", 1.0),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return tts_response(result)

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

    def check_voice_rate(user_id: int) -> None:
        now = time.monotonic()
        bucket = voice_rates[int(user_id)]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= 24:
            raise web.HTTPTooManyRequests(
                text='{"error":"atlas_overlay_voice_rate_limited","message":"Слишком много голосовых запросов."}',
                content_type="application/json",
            )
        bucket.append(now)

    def screen_context_for(
        payload: dict[str, Any],
        character: dict[str, Any] | None,
    ) -> str | None:
        raw = payload.get("screen_context")
        if not raw or character is None:
            return None
        if not bool(character.get("screen_context_enabled")):
            # Explicit consent is stored with the selected character. A stale
            # desktop client may keep sending frames; ignore them safely.
            return None
        return _validated_overlay_screen_context(raw)

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
        require_desktop_client(request)
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
        require_desktop_client(request)
        selected = await principal(request)
        await require_atlas(selected)
        reject_oversized_chat(request)
        payload = await body(request, selected)
        receipt_key, cached = cached_receipt(selected.user_id, request)
        if cached is not None:
            return web.json_response(cached)
        await require_ai_balance(selected)
        check_rate(selected.user_id)
        question = str(payload.get("question") or "").strip()
        if not question:
            return web.json_response(
                {"error": "question_required", "message": "Введите вопрос для Atlas."},
                status=400,
            )
        latency_mode = (
            "overlay"
            if str(payload.get("latency_mode") or "").strip().lower() == "overlay"
            else "standard"
        )
        question = question[:4000] if latency_mode == "overlay" else question[:8000]
        overlay_character: dict[str, Any] | None = None
        if latency_mode == "overlay":
            try:
                requested_character_id = int(payload.get("character_id") or 0) or None
                overlay = await asyncio.to_thread(
                    storage.atlas_overlay_context,
                    int(guild_id),
                    int(selected.user_id),
                    character_id=requested_character_id,
                )
            except (TypeError, ValueError) as exc:
                return web.json_response(
                    {"error": str(exc) or "atlas_overlay_character_invalid"},
                    status=403,
                )
            candidate = overlay.get("selected_character")
            if not isinstance(candidate, dict) or not bool(candidate.get("bound")):
                return web.json_response(
                    {
                        "error": "atlas_overlay_character_required",
                        "message": "Сначала выберите персонажа и его организацию в Atlas Overlay.",
                    },
                    status=409,
                )
            overlay_character = candidate
        try:
            screen_context = screen_context_for(payload, overlay_character)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        try:
            server_code, faction_code = await asyncio.to_thread(
                storage.atlas_normalize_scope,
                str(
                    overlay_character.get("server_code")
                    if overlay_character is not None
                    else payload.get("server_code") or "phoenix-15"
                ),
                str(
                    overlay_character.get("faction_code")
                    if overlay_character is not None
                    else payload.get("faction_code") or "lspd"
                ),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc), "message": "Выберите доступный сервер и фракцию."}, status=400)
        try:
            dashboard = await user_dashboard(
                request,
                selected,
                project_code=(
                    str(overlay_character.get("project_code") or "")
                    if overlay_character is not None
                    else None
                ),
            )
        except ValueError as exc:
            return web.json_response(
                {"error": str(exc), "message": "Выбранное пространство Atlas относится к другому проекту."},
                status=409,
            )
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
        memory = (
            []
            if latency_mode == "overlay"
            else await asyncio.to_thread(
                storage.atlas_recent_chat_memory,
                organization_id,
                int(selected.user_id),
                exclude_thread_id=thread_id,
                agent_id=agent_id,
            )
        )
        user_profile = dict(dashboard["membership"].get("profile") or {})
        if overlay_character is not None:
            user_profile.update(
                {
                    "nickname": str(overlay_character.get("nickname") or ""),
                    "static_id": str(overlay_character.get("static_id") or ""),
                    "rank": str(overlay_character.get("rank") or ""),
                    "direction": str(overlay_character.get("faction_label") or ""),
                    "identity_verified": True,
                }
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
                user_profile=user_profile,
                latency_mode=latency_mode,
                screen_context=screen_context,
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
        await asyncio.to_thread(
            storage.atlas_add_message,
            thread_id,
            "user",
            question,
            project_code=answer["project_code"],
            server_code=answer["server_code"],
            faction_code=answer["faction_code"],
        )
        assistant_message_id = await asyncio.to_thread(
            storage.atlas_add_message,
            thread_id,
            "assistant",
            answer["answer"],
            citations=answer["citations"],
            model=answer["model"],
            model_provider=answer["model_provider"],
            model_release=answer["model_release"],
            project_code=answer["project_code"],
            server_code=answer["server_code"],
            faction_code=answer["faction_code"],
            latency_ms=answer["latency_ms"],
        )
        await record_billing_usage(
            selected,
            organization_id,
            request_key=f"web:{assistant_message_id}",
            source="desktop" if latency_mode == "standard" else "desktop-overlay",
            answer=answer,
            message_id=assistant_message_id,
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
                "model_provider": answer["model_provider"],
                "model_release": answer["model_release"],
                "project_code": answer["project_code"],
                "server_code": answer["server_code"],
                "faction_code": answer["faction_code"],
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
        require_desktop_client(request)
        selected = await principal(request)
        await require_atlas(selected)
        reject_oversized_chat(request)
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
        await require_ai_balance(selected)
        check_rate(selected.user_id)
        question = str(payload.get("question") or "").strip()
        if not question:
            return web.json_response(
                {"error": "question_required", "message": "Введите вопрос для Atlas."},
                status=400,
            )
        latency_mode = (
            "overlay"
            if str(payload.get("latency_mode") or "").strip().lower() == "overlay"
            else "standard"
        )
        question = question[:4000] if latency_mode == "overlay" else question[:8000]
        overlay_character: dict[str, Any] | None = None
        if latency_mode == "overlay":
            try:
                requested_character_id = int(payload.get("character_id") or 0) or None
                overlay = await asyncio.to_thread(
                    storage.atlas_overlay_context,
                    int(guild_id),
                    int(selected.user_id),
                    character_id=requested_character_id,
                )
            except (TypeError, ValueError) as exc:
                return web.json_response(
                    {"error": str(exc) or "atlas_overlay_character_invalid"},
                    status=403,
                )
            candidate = overlay.get("selected_character")
            if not isinstance(candidate, dict) or not bool(candidate.get("bound")):
                return web.json_response(
                    {
                        "error": "atlas_overlay_character_required",
                        "message": "Сначала выберите персонажа и его организацию в Atlas Overlay.",
                    },
                    status=409,
                )
            overlay_character = candidate
        try:
            screen_context = screen_context_for(payload, overlay_character)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        try:
            server_code, faction_code = await asyncio.to_thread(
                storage.atlas_normalize_scope,
                str(
                    overlay_character.get("server_code")
                    if overlay_character is not None
                    else payload.get("server_code") or "phoenix-15"
                ),
                str(
                    overlay_character.get("faction_code")
                    if overlay_character is not None
                    else payload.get("faction_code") or "lspd"
                ),
            )
        except ValueError as exc:
            return web.json_response(
                {"error": str(exc), "message": "Выберите доступный сервер и фракцию."},
                status=400,
            )
        try:
            dashboard = await user_dashboard(
                request,
                selected,
                project_code=(
                    str(overlay_character.get("project_code") or "")
                    if overlay_character is not None
                    else None
                ),
            )
        except ValueError as exc:
            return web.json_response(
                {"error": str(exc), "message": "Выбранное пространство Atlas относится к другому проекту."},
                status=409,
            )
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
        memory = (
            []
            if latency_mode == "overlay"
            else await asyncio.to_thread(
                storage.atlas_recent_chat_memory,
                organization_id,
                int(selected.user_id),
                exclude_thread_id=thread_id,
                agent_id=agent_id,
            )
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
            user_profile = dict(dashboard["membership"].get("profile") or {})
            if overlay_character is not None:
                user_profile.update(
                    {
                        "nickname": str(overlay_character.get("nickname") or ""),
                        "static_id": str(overlay_character.get("static_id") or ""),
                        "rank": str(overlay_character.get("rank") or ""),
                        "direction": str(overlay_character.get("faction_label") or ""),
                        "identity_verified": True,
                    }
                )
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
                user_profile=user_profile,
                latency_mode=latency_mode,
                screen_context=screen_context,
            )
            if thread_id is None:
                thread_id = await asyncio.to_thread(
                    storage.atlas_create_thread,
                    organization_id,
                    int(selected.user_id),
                    question[:100],
                    agent_id=agent_id,
                )
            await asyncio.to_thread(
                storage.atlas_add_message,
                thread_id,
                "user",
                question,
                project_code=answer["project_code"],
                server_code=answer["server_code"],
                faction_code=answer["faction_code"],
            )
            assistant_message_id = await asyncio.to_thread(
                storage.atlas_add_message,
                thread_id,
                "assistant",
                answer["answer"],
                citations=answer["citations"],
                model=answer["model"],
                model_provider=answer["model_provider"],
                model_release=answer["model_release"],
                project_code=answer["project_code"],
                server_code=answer["server_code"],
                faction_code=answer["faction_code"],
                latency_ms=answer["latency_ms"],
            )
            await record_billing_usage(
                selected,
                organization_id,
                request_key=f"web-stream:{assistant_message_id}",
                source="desktop-overlay" if latency_mode == "overlay" else "desktop-stream",
                answer=answer,
                message_id=assistant_message_id,
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
                    "source": "desktop-overlay" if latency_mode == "overlay" else "web-stream",
                    "model": answer["model"],
                    "model_provider": answer["model_provider"],
                    "model_release": answer["model_release"],
                    "project_code": answer["project_code"],
                    "server_code": answer["server_code"],
                    "faction_code": answer["faction_code"],
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

    async def document_detail(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        try:
            document_id = int(request.match_info.get("document_id") or 0)
            if request.method == "GET":
                detail = await asyncio.to_thread(
                    document_storage.atlas_document_detail,
                    organization_id,
                    int(selected.user_id),
                    document_id,
                )
                return web.json_response(detail)
            payload = await body(request, selected)
            operation = str(payload.get("operation") or "revise").strip().lower()
            if operation == "transition":
                item = await asyncio.to_thread(
                    document_storage.atlas_document_transition,
                    organization_id,
                    int(selected.user_id),
                    document_id,
                    status=str(payload.get("status") or ""),
                    expected_revision=int(payload.get("expected_revision") or 0),
                )
            else:
                item = await asyncio.to_thread(
                    document_storage.atlas_document_revise,
                    organization_id,
                    int(selected.user_id),
                    document_id,
                    title=str(payload.get("title") or ""),
                    fields=dict(payload.get("fields") or {}),
                    rendered_text=str(payload.get("rendered_text") or ""),
                    change_summary=str(payload.get("change_summary") or ""),
                    expected_revision=int(payload.get("expected_revision") or 0),
                )
        except (TypeError, ValueError) as exc:
            status = 409 if "conflict" in str(exc) else 400
            return web.json_response({"error": str(exc), "message": "Документ не изменён."}, status=status)
        return web.json_response({"document": item})

    async def document_comments(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        try:
            document_id = int(request.match_info.get("document_id") or 0)
            raw_comment_id = str(request.match_info.get("comment_id") or "").strip()
            payload = await body(request, selected)
            if raw_comment_id:
                item = await asyncio.to_thread(
                    document_storage.atlas_document_resolve_comment,
                    organization_id,
                    int(selected.user_id),
                    document_id,
                    int(raw_comment_id),
                )
                return web.json_response({"comment": item})
            item = await asyncio.to_thread(
                document_storage.atlas_document_add_comment,
                organization_id,
                int(selected.user_id),
                document_id,
                body=str(payload.get("body") or ""),
                revision=int(payload.get("revision") or 0),
                parent_comment_id=int(payload.get("parent_comment_id") or 0) or None,
            )
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc), "message": "Комментарий не сохранён."}, status=400)
        return web.json_response({"comment": item}, status=201)

    async def document_approvals(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        try:
            document_id = int(request.match_info.get("document_id") or 0)
            raw_approval_id = str(request.match_info.get("approval_id") or "").strip()
            payload = await body(request, selected)
            if raw_approval_id:
                item = await asyncio.to_thread(
                    document_storage.atlas_document_decide_approval,
                    organization_id,
                    int(selected.user_id),
                    document_id,
                    int(raw_approval_id),
                    decision=str(payload.get("decision") or ""),
                    note=str(payload.get("note") or ""),
                )
                return web.json_response({"approval": item})
            items = await asyncio.to_thread(
                document_storage.atlas_document_configure_approvals,
                organization_id,
                int(selected.user_id),
                document_id,
                steps=list(payload.get("steps") or []),
            )
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc), "message": "Маршрут согласования не сохранён."}, status=400)
        return web.json_response({"items": items}, status=201)

    async def timeline(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        if request.method == "GET":
            try:
                items = await asyncio.to_thread(
                    storage.atlas_timeline_page,
                    organization_id,
                    actor_user_id=int(selected.user_id),
                    status=str(request.query.get("status") or "") or None,
                    limit=int(request.query.get("limit") or 80),
                    cursor=str(request.query.get("cursor") or "") or None,
                )
            except (TypeError, ValueError) as exc:
                return web.json_response(
                    {"error": str(exc), "message": "Не удалось открыть историю Atlas."},
                    status=400,
                )
            summary = await asyncio.to_thread(storage.atlas_timeline_summary, organization_id)
            return web.json_response({**items, "summary": summary})

        payload = await body(request, selected)
        receipt_key, cached = cached_receipt(selected.user_id, request)
        if cached is not None:
            return web.json_response(cached, status=201)
        check_rate(selected.user_id)
        try:
            created = await asyncio.to_thread(
                storage.atlas_create_timeline_event,
                organization_id,
                int(selected.user_id),
                title=str(payload.get("title") or ""),
                summary=str(payload.get("summary") or ""),
                event_kind=str(payload.get("event_kind") or "activity"),
                status=str(payload.get("status") or "open"),
                importance=str(payload.get("importance") or "routine"),
                occurred_at=str(payload.get("occurred_at") or "") or None,
                source_type=str(payload.get("source_type") or "") or None,
                source_id=payload.get("source_id"),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
                dedupe_key=f"web:{int(selected.user_id)}:{receipt_key}",
            )
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {"error": str(exc), "message": "Событие не сохранено. Проверьте поля."},
                status=400,
            )
        await asyncio.to_thread(
            storage.atlas_record_event,
            organization_id,
            int(selected.user_id),
            "timeline_event_created",
            f"Зафиксировано событие «{created['title']}»",
            target_type="timeline_event",
            target_id=created["id"],
            details={"kind": created["event_kind"], "importance": created["importance"]},
        )
        response = {"event": created}
        receipts[(int(selected.user_id), receipt_key)] = (time.monotonic() + 300, response)
        return web.json_response(response, status=201)

    async def timeline_item(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        payload = await body(request, selected)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        try:
            event_id = int(request.match_info.get("event_id") or 0)
            changes = {
                key: payload[key]
                for key in ("title", "summary", "status", "importance")
                if key in payload
            }
            updated = await asyncio.to_thread(
                storage.atlas_update_timeline_event,
                organization_id,
                int(selected.user_id),
                event_id,
                expected_version=(
                    int(payload["expected_version"])
                    if payload.get("expected_version") is not None
                    else None
                ),
                **changes,
            )
        except (TypeError, ValueError) as exc:
            status_code = 409 if str(exc) == "atlas_timeline_version_conflict" else 400
            return web.json_response(
                {"error": str(exc), "message": "Событие не обновлено."},
                status=status_code,
            )
        await asyncio.to_thread(
            storage.atlas_record_event,
            organization_id,
            int(selected.user_id),
            "timeline_event_updated",
            f"Обновлено событие «{updated['title']}»",
            target_type="timeline_event",
            target_id=updated["id"],
            details={"status": updated["status"], "version": updated["version"]},
        )
        return web.json_response({"event": updated})

    async def entity_links(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        payload = await body(request, selected)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        try:
            link = await asyncio.to_thread(
                storage.atlas_link_entities,
                organization_id,
                int(selected.user_id),
                source_type=str(payload.get("source_type") or ""),
                source_id=payload.get("source_id") or "",
                relation=str(payload.get("relation") or "related_to"),
                target_type=str(payload.get("target_type") or ""),
                target_id=payload.get("target_id") or "",
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            )
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {"error": str(exc), "message": "Связь не создана."},
                status=400,
            )
        return web.json_response({"link": link}, status=201)

    async def message_feedback(request: web.Request) -> web.Response:
        require_desktop_client(request)
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

    async def index_source(source: dict[str, Any]) -> list[str]:
        point_ids = await atlas_index_source(source)
        await asyncio.to_thread(
            storage.atlas_mark_knowledge_indexed,
            int(source["id"]),
            point_id=point_ids[0] if point_ids else None,
        )
        return point_ids

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

    async def run_knowledge_index_job(
        job: dict[str, Any],
        report: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> dict[str, Any]:
        source_id = int(dict(job.get("payload") or {}).get("source_id") or 0)
        source = await asyncio.to_thread(storage.atlas_knowledge_source, source_id)
        if source is None or str(source.get("status") or "") == "archived":
            return {"source_id": source_id, "skipped": True}
        await report({"percent": 10, "stage": "preparing", "source_id": source_id})
        try:
            async with index_lock:
                point_ids = await index_source(source)
        except Exception as exc:
            await mark_index_error(source, exc)
            if isinstance(exc, AtlasAIError) and exc.code == "qdrant_index_corrupted":
                queue_index_reconciliation(force_reset=True)
            raise
        await report(
            {
                "percent": 90,
                "stage": "saving",
                "source_id": source_id,
                "points": len(point_ids),
            }
        )
        return {
            "source_id": source_id,
            "points": len(point_ids),
            "point_id": point_ids[0] if point_ids else None,
        }

    job_worker.register("atlas.knowledge.index.v1", run_knowledge_index_job)

    async def run_media_finalize_job(
        job: dict[str, Any],
        report: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> dict[str, Any]:
        upload_id = str(dict(job.get("payload") or {}).get("upload_id") or "").strip()
        upload = await asyncio.to_thread(media_storage.atlas_media_upload, upload_id)
        if upload is None:
            raise AtlasMediaError("atlas_media_upload_missing", "Сессия загрузки не найдена.")
        if str(upload.get("status")) == "completed":
            return {"upload_id": upload_id, "asset_id": int(upload["asset_id"]), "ready": True}
        await report({"percent": 10, "stage": "verifying", "asset_id": int(upload["asset_id"])})
        try:
            prepared_key = str(upload.get("final_storage_key") or "")
            source_key = (
                prepared_key
                if prepared_key and media_blobs.path(prepared_key).is_file()
                else str(upload["temp_storage_key"])
            )
            checksum, actual_size = await asyncio.to_thread(
                media_blobs.checksum_and_size,
                source_key,
            )
            if actual_size != int(upload["expected_size"]):
                raise AtlasMediaError(
                    "atlas_media_size_mismatch",
                    "Размер сохранённого файла не совпадает с заявленным.",
                )
            expected_hash = str(upload.get("expected_sha256") or "").lower()
            if expected_hash and checksum != expected_hash:
                raise AtlasMediaError(
                    "atlas_media_checksum_mismatch",
                    "Контрольная сумма файла не совпала.",
                )
            detected_mime = str(upload.get("detected_mime_type") or "") or atlas_media_detect_type(
                await asyncio.to_thread(media_blobs.head, source_key),
                str(upload.get("declared_mime_type") or "") or None,
            )
            final_key = prepared_key or media_blobs.final_key(checksum)
            await asyncio.to_thread(
                media_storage.atlas_media_prepare_finalize,
                upload_id,
                checksum_sha256=checksum,
                final_storage_key=final_key,
                detected_mime_type=detected_mime,
            )
            await report({"percent": 45, "stage": "safety_check", "asset_id": int(upload["asset_id"])})
            scan_status = await asyncio.to_thread(
                atlas_media_scan,
                media_blobs.path(source_key),
                media_config.scan_command,
            )
            await report({"percent": 70, "stage": "storing", "asset_id": int(upload["asset_id"])})
            await asyncio.to_thread(
                media_blobs.finalize,
                str(upload["temp_storage_key"]),
                final_key,
                expected_size=actual_size,
            )
            asset = await asyncio.to_thread(
                media_storage.atlas_media_complete_upload,
                upload_id,
                checksum_sha256=checksum,
                storage_key=final_key,
                mime_type=detected_mime,
                media_kind=atlas_media_kind_for_mime(detected_mime),
                scan_status=scan_status,
            )
            await report({"percent": 95, "stage": "ready", "asset_id": int(asset["id"])})
            return {
                "upload_id": upload_id,
                "asset_id": int(asset["id"]),
                "checksum_sha256": checksum,
                "mime_type": detected_mime,
                "ready": True,
            }
        except AtlasMediaError as exc:
            await asyncio.to_thread(
                media_storage.atlas_media_mark_upload_error,
                upload_id,
                error=f"{exc.code}: {exc}",
                terminal=not exc.retryable,
            )
            raise
        except ValueError as exc:
            wrapped = AtlasMediaError(str(exc), "Файл не прошёл проверку Atlas.")
            await asyncio.to_thread(
                media_storage.atlas_media_mark_upload_error,
                upload_id,
                error=f"{type(exc).__name__}: {exc}",
                terminal=True,
            )
            raise wrapped from exc
        except Exception as exc:
            await asyncio.to_thread(
                media_storage.atlas_media_mark_upload_error,
                upload_id,
                error=f"{type(exc).__name__}: {exc}",
                terminal=False,
            )
            raise

    job_worker.register("atlas.media.finalize.v1", run_media_finalize_job)

    async def run_forum_attachment_ocr_job(
        job: dict[str, Any],
        report: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> dict[str, Any]:
        """OCR one forum-owned file without treating its text as a legal source."""

        attachment_id = int(dict(job.get("payload") or {}).get("attachment_id") or 0)
        attachment = await asyncio.to_thread(
            attachment_storage.atlas_forum_attachment_begin_processing,
            attachment_id,
        )
        if attachment is None:
            return {"attachment_id": attachment_id, "skipped": True}
        if str(attachment.get("status") or "") in {
            "review_pending", "approved", "rejected", "unavailable", "archived"
        }:
            return {
                "attachment_id": attachment_id,
                "skipped": True,
                "status": attachment.get("status"),
            }
        if forum_sync_runner is None or not forum_sync_runner.config.enabled:
            error = "atlas_forum_sync_disabled"
            await asyncio.to_thread(
                attachment_storage.atlas_forum_attachment_mark_error,
                attachment_id,
                error=error,
            )
            raise AtlasForumSyncError(error)
        await report({"percent": 12, "stage": "fetching_original", "attachment_id": attachment_id})
        content_sha256: str | None = None
        storage_key: str | None = None
        detected_mime: str | None = None
        size_bytes: int | None = None
        try:
            raw, declared_mime = await forum_sync_runner.fetch_attachment(
                str(attachment["attachment_url"]),
            )
            size_bytes = len(raw)
            content_sha256 = hashlib.sha256(raw).hexdigest()
            detected_mime = atlas_media_detect_type(raw[:64 * 1024], declared_mime)
            storage_key = await asyncio.to_thread(
                media_blobs.put_bytes,
                raw,
                checksum_sha256=content_sha256,
            )
            await report({"percent": 45, "stage": "recognising", "attachment_id": attachment_id})
            result = await asyncio.to_thread(
                atlas_ocr_attachment,
                raw,
                mime_type=detected_mime,
                filename=str(attachment.get("filename") or "forum-attachment"),
            )
        except AtlasOcrUnavailable as exc:
            stored = await asyncio.to_thread(
                attachment_storage.atlas_forum_attachment_mark_unavailable,
                attachment_id,
                error=f"{exc.code}: {exc}",
                content_sha256=content_sha256,
                storage_key=storage_key,
                mime_type=detected_mime,
                size_bytes=size_bytes,
            )
            return {
                "attachment_id": attachment_id,
                "status": stored.get("status") if stored else "unavailable",
                "reason": exc.code,
            }
        except AtlasOcrError as exc:
            if exc.retryable:
                await asyncio.to_thread(
                    attachment_storage.atlas_forum_attachment_mark_error,
                    attachment_id,
                    error=f"{exc.code}: {exc}",
                )
                raise
            stored = await asyncio.to_thread(
                attachment_storage.atlas_forum_attachment_mark_unavailable,
                attachment_id,
                error=f"{exc.code}: {exc}",
                content_sha256=content_sha256,
                storage_key=storage_key,
                mime_type=detected_mime,
                size_bytes=size_bytes,
            )
            return {
                "attachment_id": attachment_id,
                "status": stored.get("status") if stored else "unavailable",
                "reason": exc.code,
            }
        except AtlasMediaError as exc:
            stored = await asyncio.to_thread(
                attachment_storage.atlas_forum_attachment_mark_unavailable,
                attachment_id,
                error=f"{exc.code}: {exc}",
                content_sha256=content_sha256,
                storage_key=storage_key,
                mime_type=detected_mime,
                size_bytes=size_bytes,
            )
            return {
                "attachment_id": attachment_id,
                "status": stored.get("status") if stored else "unavailable",
                "reason": exc.code,
            }
        except Exception as exc:
            await asyncio.to_thread(
                attachment_storage.atlas_forum_attachment_mark_error,
                attachment_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        await report({"percent": 80, "stage": "waiting_for_review", "attachment_id": attachment_id})
        stored = await asyncio.to_thread(
            attachment_storage.atlas_forum_attachment_complete_ocr,
            attachment_id,
            content_sha256=content_sha256,
            storage_key=str(storage_key or ""),
            mime_type=detected_mime,
            size_bytes=size_bytes,
            text=result.text,
            engine=result.engine,
        )
        await asyncio.to_thread(
            storage.atlas_record_event,
            int(stored["organization_id"]),
            0,
            "forum_attachment_ocr_ready",
            "Atlas подготовил распознавание вложения для проверки",
            target_type="forum_attachment",
            target_id=attachment_id,
            details={
                "source_id": int(stored["source_id"]),
                "engine": result.engine,
                "pages": result.pages,
                "mime_type": detected_mime,
                "size_bytes": size_bytes,
            },
        )
        return {
            "attachment_id": attachment_id,
            "status": "review_pending",
            "pages": result.pages,
            "engine": result.engine,
        }

    job_worker.register("atlas.forum.attachment-ocr.v1", run_forum_attachment_ocr_job)

    async def reconcile_knowledge_index(*, force_reset: bool = False) -> None:
        sources = await asyncio.to_thread(storage.atlas_indexable_knowledge_sources)
        if not sources:
            return
        async with index_lock:
            probe = await atlas_probe_collection()
            reset = force_reset or probe["status"] in {"corrupted", "stale"}
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
        generation = (
            f"rebuild:{time.time_ns()}"
            if rebuild_all
            else None
        )
        for source in targets:
            await queue_knowledge_index(source, generation=generation)

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

    async def queue_knowledge_index(
        source: dict[str, Any],
        *,
        generation: str | None = None,
    ) -> dict[str, Any]:
        fingerprint = hashlib.sha256(
            str(
                generation
                or f"{source.get('checksum') or ''}:{source.get('updated_at') or ''}"
            ).encode("utf-8")
        ).hexdigest()[:24]
        queued = await asyncio.to_thread(
            job_storage.atlas_job_enqueue,
            int(source["organization_id"]),
            int(source.get("created_by_id") or 0),
            job_type="atlas.knowledge.index.v1",
            dedupe_key=f"source:{int(source['id'])}:{fingerprint}",
            payload={"source_id": int(source["id"])},
            subject_type="knowledge_source",
            subject_id=int(source["id"]),
            max_attempts=6,
        )
        job_worker.wake()
        return queued

    async def queue_media_finalize(upload: dict[str, Any]) -> dict[str, Any]:
        queued = await asyncio.to_thread(
            job_storage.atlas_job_enqueue,
            int(upload["organization_id"]),
            int(upload["user_id"]),
            job_type="atlas.media.finalize.v1",
            dedupe_key=f"upload:{str(upload['upload_id'])}",
            payload={"upload_id": str(upload["upload_id"])},
            subject_type="media_asset",
            subject_id=int(upload["asset_id"]),
            max_attempts=5,
        )
        job_worker.wake()
        return queued

    async def queue_forum_attachment_ocr(attachment: dict[str, Any]) -> dict[str, Any] | None:
        """Make discovery durable first, then let the leased worker OCR it."""

        if str(attachment.get("status") or "") != "discovered":
            return None
        fingerprint = hashlib.sha256(
            str(attachment.get("source_checksum") or "").encode("utf-8")
        ).hexdigest()[:24]
        queued = await asyncio.to_thread(
            job_storage.atlas_job_enqueue,
            int(attachment["organization_id"]),
            0,
            job_type="atlas.forum.attachment-ocr.v1",
            dedupe_key=f"attachment:{int(attachment['id'])}:{fingerprint}",
            payload={"attachment_id": int(attachment["id"])},
            subject_type="forum_attachment",
            subject_id=int(attachment["id"]),
            max_attempts=4,
        )
        await asyncio.to_thread(
            attachment_storage.atlas_forum_attachment_mark_queued,
            int(attachment["id"]),
        )
        job_worker.wake()
        return queued

    async def reconcile_forum_attachment_ocr(*, limit: int = 40) -> int:
        """Backfill old forum topics and queue only explicitly discovered files."""

        if forum_sync_runner is None or not forum_sync_runner.config.enabled:
            return 0
        await asyncio.to_thread(
            attachment_storage.atlas_reconcile_forum_attachment_inventory,
            limit=max(1, min(2_000, int(limit) * 8)),
        )
        pending = await asyncio.to_thread(
            attachment_storage.atlas_forum_attachment_pending,
            limit=limit,
        )
        queued = 0
        for attachment in pending:
            if await queue_forum_attachment_ocr(attachment):
                queued += 1
        return queued

    async def persist_forum_attachment_inventory(
        source: dict[str, Any],
        attachments: tuple[Any, ...] | list[Any],
    ) -> tuple[int, int]:
        """Save the parser inventory and immediately schedule new safe OCR work."""

        rows = await asyncio.to_thread(
            attachment_storage.atlas_sync_forum_attachments,
            int(source["organization_id"]),
            int(source["id"]),
            tuple(
                item.public()
                for item in attachments
                if callable(getattr(item, "public", None))
            ),
        )
        queued = 0
        for row in rows:
            if await queue_forum_attachment_ocr(row):
                queued += 1
        return len(rows), queued

    async def run_forum_listing_job(
        job: dict[str, Any],
        report: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> dict[str, Any]:
        payload = dict(job.get("payload") or {})
        source_url = str(payload.get("source_url") or "").strip()
        organization_id = int(job["organization_id"])
        actor_user_id = int(job.get("created_by_id") or 0)
        requested_feed_key = str(payload.get("feed_key") or "")
        if forum_sync_runner is None:
            raise AtlasForumSyncError("atlas_forum_sync_disabled")
        try:
            # A manually imported section is not a one-off blob.  Persist its
            # exact scope and taxonomy as a feed before reading it, so Atlas
            # can refresh the same forum section later without an operator
            # having to submit the URL again.
            feed = await asyncio.to_thread(
                storage.atlas_ensure_forum_feed,
                int(guild_id),
                feed_key=requested_feed_key,
                root_url=source_url,
                server_code=str(payload.get("server_code") or "phoenix-15"),
                faction_code=str(payload.get("faction_code") or "lspd"),
                visibility_scope=str(payload.get("visibility_scope") or "server"),
                federation_scope=str(payload.get("federation_scope") or "") or None,
                knowledge_domain=str(payload.get("knowledge_domain") or "") or None,
                corpus_kind=str(payload.get("corpus_kind") or "") or None,
                organization_id=organization_id,
                interval_seconds=int(forum_sync_runner.config.interval_seconds),
            )
            feed_key = str(feed["feed_key"])
            await report({"percent": 5, "stage": "opening_forum"})
            batch = await forum_sync_runner.fetch_listing(source_url)
            total = max(1, len(batch.snapshots))
            created = 0
            changed = 0
            queued = 0
            attachments = 0
            attachment_jobs = 0
            for position, snapshot in enumerate(batch.snapshots, start=1):
                result = await asyncio.to_thread(
                    storage.atlas_upsert_synced_knowledge,
                    organization_id,
                    title=snapshot.title,
                    content=snapshot.content,
                    source_url=snapshot.url,
                    server_code=str(payload.get("server_code") or "phoenix-15"),
                    faction_code=str(payload.get("faction_code") or "lspd"),
                    visibility_scope=str(payload.get("visibility_scope") or "server"),
                    federation_scope=str(payload.get("federation_scope") or "") or None,
                    knowledge_domain=str(payload.get("knowledge_domain") or "") or None,
                    corpus_kind=str(payload.get("corpus_kind") or "") or None,
                    feed_key=feed_key,
                    metadata={
                        "author": snapshot.author,
                        "source_updated_at": snapshot.source_updated_at,
                        "import_mode": "authenticated_forum_listing",
                        "listing_url": source_url,
                        "requested_by_id": actor_user_id,
                        "ingestion_origin": "manual_forum_feed",
                        "forum_attachments": [
                            attachment.public() for attachment in snapshot.attachments
                        ],
                    },
                )
                created += int(bool(result["created"]))
                changed += int(bool(result["changed"]))
                source = result["source"]
                saved_attachments, queued_attachments = await persist_forum_attachment_inventory(
                    source,
                    snapshot.attachments,
                )
                attachments += saved_attachments
                attachment_jobs += queued_attachments
                if result["changed"] or source.get("status") != "indexed":
                    await queue_knowledge_index(source)
                    queued += 1
                await report(
                    {
                        "percent": min(90, 10 + round(position / total * 80)),
                        "stage": "reading_topics",
                        "processed": position,
                        "total": len(batch.snapshots),
                    }
                )
            details = {
                "created": created,
                "changed": changed,
                "queued": queued,
                "skipped": len(batch.skipped_threads),
                "inventory_complete": batch.inventory_complete,
                "attachments": attachments,
                "attachment_jobs": attachment_jobs,
                "feed_key": feed_key,
                "federation_scope": feed.get("federation_scope"),
                "knowledge_domain": feed.get("knowledge_domain"),
                "corpus_kind": feed.get("corpus_kind"),
            }
            await asyncio.to_thread(
                storage.atlas_record_event,
                organization_id,
                actor_user_id,
                "forum_listing_imported",
                f"Atlas прочитал раздел форума: {len(batch.snapshots)} тем",
                target_type="forum_listing",
                target_id=source_url,
                details=details,
            )
            await atlas_log(
                "раздел форума прочитан",
                (
                    f"Лента: `{feed_key}` · тем: **{len(batch.snapshots)}** · "
                    f"новых: **{created}** · обновлено: **{changed}** · "
                    f"пропущено: **{len(batch.skipped_threads)}**"
                ),
                level="info",
                dedupe_key=f"atlas-forum-listing-ok:{feed_key}",
            )
            return {"topics": len(batch.snapshots), **details}
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
                dedupe_key=f"atlas-forum-listing-error:{requested_feed_key}",
            )
            raise

    async def run_forum_thread_job(
        job: dict[str, Any],
        report: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> dict[str, Any]:
        """Read one forum topic without holding an HTTP request open."""

        payload = dict(job.get("payload") or {})
        source_url = str(payload.get("source_url") or "").strip()
        organization_id = int(job["organization_id"])
        actor_user_id = int(job.get("created_by_id") or 0)
        if forum_sync_runner is None:
            raise AtlasForumSyncError("atlas_forum_sync_disabled")
        try:
            await report({"percent": 8, "stage": "opening_forum"})
            snapshot = await forum_sync_runner.fetch_thread(source_url)
            await report({"percent": 62, "stage": "saving_topic"})
            source = await asyncio.to_thread(
                storage.atlas_add_knowledge,
                organization_id,
                actor_user_id,
                title=snapshot.title,
                content=snapshot.content,
                source_kind="forum",
                source_url=snapshot.url,
                server_code=str(payload.get("server_code") or "phoenix-15"),
                faction_code=str(payload.get("faction_code") or "lspd"),
                visibility_scope=str(payload.get("visibility_scope") or "server"),
                federation_scope=str(payload.get("federation_scope") or "") or None,
                knowledge_domain=str(payload.get("knowledge_domain") or "") or None,
                corpus_kind=str(payload.get("corpus_kind") or "") or None,
                metadata={
                    "author": snapshot.author,
                    "source_updated_at": snapshot.source_updated_at,
                    "import_mode": "authenticated_forum_thread",
                    "forum_attachments": [
                        attachment.public() for attachment in snapshot.attachments
                    ],
                },
            )
            attachment_count, attachment_jobs = await persist_forum_attachment_inventory(
                source,
                snapshot.attachments,
            )
            index_job = await queue_knowledge_index(source)
            taxonomy = dict(source.get("metadata", {})).get("taxonomy", {})
            await report({"percent": 92, "stage": "index_queued"})
            await asyncio.to_thread(
                storage.atlas_record_event,
                organization_id,
                actor_user_id,
                "forum_thread_imported",
                f"Atlas прочитал тему форума: {snapshot.title}",
                target_type="knowledge_source",
                target_id=int(source["id"]),
                details={"source_url": snapshot.url, "taxonomy": taxonomy},
            )
            return {
                "source_id": int(source["id"]),
                "title": str(source.get("title") or snapshot.title),
                "taxonomy": taxonomy,
                "index_job_id": int(index_job["id"]),
                "attachments": attachment_count,
                "attachment_jobs": attachment_jobs,
            }
        except Exception as exc:
            await atlas_log(
                "не удалось прочитать тему форума",
                (
                    f"Ссылка: `{source_url[:800]}`\n"
                    f"Ошибка: `{type(exc).__name__}: {str(exc)[:1000]}`\n"
                    "Живой Chromium: `http://127.0.0.1:7900/?autoconnect=1&resize=scale`"
                ),
                level="warning",
                exception=exc,
                dedupe_key=(
                    "atlas-forum-thread-error:"
                    + hashlib.sha256(source_url.casefold().encode("utf-8")).hexdigest()[:20]
                ),
            )
            raise

    job_worker.register("atlas.forum.listing.v1", run_forum_listing_job)
    job_worker.register("atlas.forum.thread.v1", run_forum_thread_job)

    async def queue_forum_listing_import(
        *,
        source_url: str,
        organization_id: int,
        actor_user_id: int,
        server_code: str,
        faction_code: str,
        visibility_scope: str,
        federation_scope: str | None,
        knowledge_domain: str | None,
        corpus_kind: str | None,
        request_key: str | None = None,
    ) -> dict[str, Any]:
        """Persist a full forum-section import so restarts cannot lose it."""
        feed_key = "manual-" + hashlib.sha256(
            source_url.strip().casefold().encode("utf-8")
        ).hexdigest()[:20]
        request_fingerprint = hashlib.sha256(
            str(request_key or f"{actor_user_id}:{time.time_ns()}").encode("utf-8")
        ).hexdigest()[:20]
        queued = await asyncio.to_thread(
            job_storage.atlas_job_enqueue,
            int(organization_id),
            int(actor_user_id),
            job_type="atlas.forum.listing.v1",
            dedupe_key=f"{feed_key}:{request_fingerprint}",
            payload={
                "source_url": source_url,
                "feed_key": feed_key,
                "server_code": server_code,
                "faction_code": faction_code,
                "visibility_scope": visibility_scope,
                "federation_scope": federation_scope,
                "knowledge_domain": knowledge_domain,
                "corpus_kind": corpus_kind,
            },
            subject_type="forum_listing",
            subject_id=source_url,
            max_attempts=4,
        )
        job_worker.wake()
        return queued

    async def queue_forum_thread_import(
        *,
        source_url: str,
        organization_id: int,
        actor_user_id: int,
        server_code: str,
        faction_code: str,
        visibility_scope: str,
        federation_scope: str | None,
        knowledge_domain: str | None,
        corpus_kind: str | None,
        request_key: str | None = None,
    ) -> dict[str, Any]:
        request_fingerprint = hashlib.sha256(
            str(request_key or f"{actor_user_id}:{time.time_ns()}").encode("utf-8")
        ).hexdigest()[:20]
        source_fingerprint = hashlib.sha256(
            source_url.strip().casefold().encode("utf-8")
        ).hexdigest()[:20]
        queued = await asyncio.to_thread(
            job_storage.atlas_job_enqueue,
            int(organization_id),
            int(actor_user_id),
            job_type="atlas.forum.thread.v1",
            dedupe_key=f"{source_fingerprint}:{request_fingerprint}",
            payload={
                "source_url": source_url,
                "server_code": server_code,
                "faction_code": faction_code,
                "visibility_scope": visibility_scope,
                "federation_scope": federation_scope,
                "knowledge_domain": knowledge_domain,
                "corpus_kind": corpus_kind,
            },
            subject_type="forum_thread",
            subject_id=source_url,
            max_attempts=4,
        )
        job_worker.wake()
        return queued

    forum_config = AtlasForumSyncConfig.from_env()
    forum_sync_runner = AtlasForumSyncRunner(
        bot,
        int(guild_id),
        config=forum_config,
        index_callback=index_synced_source,
    )
    forum_engine_browser = AtlasForumSyncRunner(
        bot,
        int(guild_id),
        config=replace(
            forum_config,
            selenium_url=str(
                os.getenv(
                    "ATLAS_FORUM_ENGINE_SELENIUM_URL",
                    forum_config.selenium_url,
                )
            ).strip(),
        ),
        index_callback=index_synced_source,
    )
    forum_engine_runner = AtlasForumEngineRunner(
        bot,
        int(guild_id),
        forum_engine_browser,
    )

    async def start_index_reconciliation(_: web.Application) -> None:
        queue_index_reconciliation()

    async def start_atlas_jobs(_: web.Application) -> None:
        nonlocal attachment_ocr_task
        # Let startup reconciliation acquire the index lock before old jobs resume.
        await asyncio.sleep(0)
        job_worker.start()
        job_worker.wake()

        async def attachment_runner() -> None:
            while True:
                try:
                    await reconcile_forum_attachment_ocr()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The queue remains durable; a transient forum/DB outage
                    # must not prevent the rest of Atlas from serving users.
                    pass
                await asyncio.sleep(45)

        attachment_ocr_task = asyncio.create_task(
            attachment_runner(),
            name="atlas-forum-attachment-ocr",
        )

    async def start_forum_sync(_: web.Application) -> None:
        nonlocal forum_sync_task
        if forum_sync_runner is None or not forum_sync_runner.config.enabled:
            return
        forum_sync_task = asyncio.create_task(
            forum_sync_runner.run(),
            name="atlas-forum-sync",
        )

    async def start_forum_engine(_: web.Application) -> None:
        nonlocal forum_engine_task
        if forum_engine_runner is None or not forum_engine_runner.config.enabled:
            return
        forum_engine_task = asyncio.create_task(
            forum_engine_runner.run(),
            name="atlas-forum-engine",
        )

    async def stop_index_reconciliation(_: web.Application) -> None:
        for task in tuple(indexing_tasks):
            task.cancel()
        if indexing_tasks:
            await asyncio.gather(*tuple(indexing_tasks), return_exceptions=True)

    async def stop_atlas_jobs(_: web.Application) -> None:
        await job_worker.close()

    async def stop_forum_attachment_ocr(_: web.Application) -> None:
        nonlocal attachment_ocr_task
        if attachment_ocr_task is None:
            return
        attachment_ocr_task.cancel()
        await asyncio.gather(attachment_ocr_task, return_exceptions=True)
        attachment_ocr_task = None

    async def stop_overlay_tts(_: web.Application) -> None:
        await overlay_tts.close()

    async def stop_forum_sync(_: web.Application) -> None:
        if forum_sync_runner is not None:
            await forum_sync_runner.close()
        if forum_sync_task is not None:
            forum_sync_task.cancel()
            await asyncio.gather(forum_sync_task, return_exceptions=True)

    async def stop_forum_engine(_: web.Application) -> None:
        if forum_engine_runner is not None:
            await forum_engine_runner.close()
        if forum_engine_task is not None:
            forum_engine_task.cancel()
            await asyncio.gather(forum_engine_task, return_exceptions=True)
        if forum_engine_browser is not None:
            await forum_engine_browser.close()

    app.on_startup.append(start_index_reconciliation)
    app.on_startup.append(start_atlas_jobs)
    app.on_startup.append(start_forum_sync)
    app.on_startup.append(start_forum_engine)
    app.on_cleanup.append(stop_forum_engine)
    app.on_cleanup.append(stop_forum_sync)
    app.on_cleanup.append(stop_index_reconciliation)
    app.on_cleanup.append(stop_forum_attachment_ocr)
    app.on_cleanup.append(stop_atlas_jobs)
    app.on_cleanup.append(stop_overlay_tts)

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
            federation_scope = explicit_platform_scope(payload)
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
                federation_scope=federation_scope,
                knowledge_domain=str(payload.get("knowledge_domain") or "") or None,
                corpus_kind=str(payload.get("corpus_kind") or "") or None,
            )
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {"error": str(exc), "message": "Источник не добавлен."},
                status=400,
            )

        queued_job = await queue_knowledge_index(source)
        return web.json_response(
            {
                "source": source,
                "job": queued_job,
                "queued": True,
                "message": "Материал принят. Atlas готовит его для поиска.",
            },
            status=202,
        )

    async def jobs(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        raw_job_id = str(request.match_info.get("job_id") or "").strip()
        if raw_job_id:
            try:
                item = await asyncio.to_thread(job_storage.atlas_job_get, int(raw_job_id))
            except (TypeError, ValueError):
                item = None
            if item is None or int(item["organization_id"]) != organization_id:
                raise web.HTTPNotFound(
                    text='{"error":"atlas_job_not_found"}',
                    content_type="application/json",
                )
            return web.json_response({"job": item})
        try:
            limit = max(1, min(100, int(request.query.get("limit") or 30)))
            items = await asyncio.to_thread(
                job_storage.atlas_jobs,
                organization_id,
                status=str(request.query.get("status") or "") or None,
                limit=limit,
            )
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"items": items})

    async def forum_attachments(request: web.Request) -> web.Response:
        """Review the machine transcription before it can become legal evidence."""

        selected = await principal(request)
        await require_atlas(selected)
        if not selected.administrator:
            raise web.HTTPForbidden(
                text='{"error":"atlas_knowledge_admin_required"}',
                content_type="application/json",
            )
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        raw_attachment_id = str(request.match_info.get("attachment_id") or "").strip()
        if request.method == "GET":
            try:
                limit = max(1, min(500, int(request.query.get("limit") or 100)))
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": "atlas_forum_attachment_limit_invalid"}, status=400
                )
            items = await asyncio.to_thread(
                attachment_storage.atlas_forum_attachments,
                organization_id,
                status=str(request.query.get("status") or "") or None,
                limit=limit,
            )
            return web.json_response(
                {
                    "items": [
                        {
                            key: value
                            for key, value in item.items()
                            if key != "storage_key"
                        }
                        for item in items
                    ]
                }
            )
        try:
            attachment_id = int(raw_attachment_id)
        except (TypeError, ValueError):
            raise web.HTTPNotFound() from None
        payload = await body(request, selected)
        action = str(payload.get("action") or "").strip().lower()
        if action not in {"approve", "reject"}:
            return web.json_response(
                {"error": "atlas_forum_attachment_action_invalid"}, status=400
            )
        try:
            reviewed = await asyncio.to_thread(
                attachment_storage.atlas_forum_attachment_review,
                organization_id,
                int(selected.user_id),
                attachment_id,
                approve=action == "approve",
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        queued = None
        if reviewed.get("knowledge_source_id"):
            source = await asyncio.to_thread(
                storage.atlas_knowledge_source,
                int(reviewed["knowledge_source_id"]),
            )
            if source is not None:
                queued = await queue_knowledge_index(source)
        return web.json_response(
            {
                "attachment": {
                    key: value
                    for key, value in reviewed.items()
                    if key != "storage_key"
                },
                "job": queued,
                "message": (
                    "Расшифровка подтверждена и поставлена на индексацию."
                    if action == "approve"
                    else "Расшифровка отклонена."
                ),
            }
        )

    async def global_search(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        raw_kinds = [item.strip() for item in str(request.query.get("kinds") or "").split(",") if item.strip()]
        try:
            result = await asyncio.to_thread(
                search_storage.atlas_global_search,
                int(dashboard["organization"]["id"]),
                int(selected.user_id),
                query=str(request.query.get("q") or ""),
                server_code=str(request.query.get("server_code") or "phoenix-15"),
                faction_code=str(request.query.get("faction_code") or "lspd"),
                kinds=raw_kinds or None,
                limit=int(request.query.get("limit") or 40),
            )
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc), "message": "Введите не менее двух символов."}, status=400)
        return web.json_response(result)

    def public_media_asset(item: dict[str, Any]) -> dict[str, Any]:
        hidden = {"storage_key", "last_error"}
        payload = {key: value for key, value in item.items() if key not in hidden}
        payload["content_url"] = (
            f"/api/atlas/media/{int(item['id'])}/content"
            if str(item.get("status")) == "ready"
            else None
        )
        if item.get("last_error"):
            payload["error"] = "Файл пока не готов. Atlas сохранит возможность повторить обработку."
        return payload

    def public_media_upload(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: item.get(key)
            for key in (
                "upload_id", "asset_id", "status", "asset_status", "expected_size",
                "received_size", "expires_at", "title", "original_filename",
            )
        }

    async def media_library(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        items, quota = await asyncio.gather(
            asyncio.to_thread(
                media_storage.atlas_media_assets,
                organization_id,
                int(selected.user_id),
                status=str(request.query.get("status") or "") or None,
                limit=max(1, min(120, int(request.query.get("limit") or 60))),
            ),
            asyncio.to_thread(media_storage.atlas_media_quota, organization_id),
        )
        return web.json_response(
            {
                "items": [public_media_asset(item) for item in items],
                "quota": {**quota, "limit_bytes": media_config.quota_bytes},
                "upload": {
                    "max_asset_bytes": media_config.max_asset_bytes,
                    "chunk_bytes": media_config.max_chunk_bytes,
                },
            }
        )

    async def media_upload_start(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        payload = await body(request, selected)
        dashboard = await user_dashboard(request, selected)
        try:
            upload = await asyncio.to_thread(
                media_storage.atlas_media_begin_upload,
                int(dashboard["organization"]["id"]),
                int(selected.user_id),
                title=str(payload.get("title") or ""),
                filename=str(payload.get("filename") or ""),
                size_bytes=int(payload.get("size_bytes") or 0),
                declared_mime_type=str(payload.get("mime_type") or "") or None,
                media_kind=str(payload.get("media_kind") or "file"),
                visibility_scope=str(payload.get("visibility_scope") or "private"),
                source_kind=str(payload.get("source_kind") or "upload"),
                source_device_id=str(payload.get("source_device_id") or "") or None,
                captured_at=str(payload.get("captured_at") or "") or None,
                retention_policy=str(payload.get("retention_policy") or "manual"),
                expected_sha256=str(payload.get("sha256") or "") or None,
                client_request_id=str(request.headers.get("X-Idempotency-Key") or ""),
                max_asset_bytes=media_config.max_asset_bytes,
                quota_bytes=media_config.quota_bytes,
            )
        except (TypeError, ValueError) as exc:
            messages = {
                "atlas_media_size_invalid": "Файл пустой или превышает допустимый размер.",
                "atlas_media_quota_exceeded": "В рабочем пространстве недостаточно места.",
                "atlas_media_upload_limit": "Сначала завершите уже начатые загрузки.",
            }
            return web.json_response(
                {"error": str(exc), "message": messages.get(str(exc), "Не удалось начать загрузку.")},
                status=400,
            )
        return web.json_response(
            {
                "upload": public_media_upload(upload),
                "upload_url": f"/api/atlas/media/uploads/{str(upload['upload_id'])}",
                "chunk_bytes": media_config.max_chunk_bytes,
            },
            status=201,
        )

    async def media_upload_chunk(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        if not csrf_matches(request, selected):
            return web.json_response({"error": "csrf_failed"}, status=403)
        dashboard = await user_dashboard(request, selected)
        upload = await asyncio.to_thread(
            media_storage.atlas_media_upload_for_user,
            str(request.match_info.get("upload_id") or ""),
            int(dashboard["organization"]["id"]),
            int(selected.user_id),
        )
        if upload is None:
            raise web.HTTPNotFound(
                text='{"error":"atlas_media_upload_missing"}',
                content_type="application/json",
            )
        if int(upload["received_size"]) == int(upload["expected_size"]):
            job = await queue_media_finalize(upload)
            return web.json_response(
                {"upload": public_media_upload(upload), "job": job, "queued": True},
                status=202,
                headers={"Upload-Offset": str(upload["received_size"])},
            )
        if str(upload["status"]) != "open":
            return web.json_response(
                {"error": "atlas_media_upload_closed", "message": "Эта загрузка уже закрыта."},
                status=409,
            )
        try:
            offset = int(request.headers.get("Upload-Offset") or -1)
        except (TypeError, ValueError):
            offset = -1
        if offset != int(upload["received_size"]):
            return web.json_response(
                {
                    "error": "atlas_media_upload_offset_conflict",
                    "offset": int(upload["received_size"]),
                },
                status=409,
                headers={"Upload-Offset": str(upload["received_size"])},
            )
        if request.content_length is not None and int(request.content_length) > media_config.max_chunk_bytes:
            return web.json_response({"error": "atlas_media_chunk_too_large"}, status=413)
        chunk = await request.read()
        if not chunk or len(chunk) > media_config.max_chunk_bytes:
            return web.json_response({"error": "atlas_media_chunk_invalid"}, status=413)
        if offset + len(chunk) > int(upload["expected_size"]):
            return web.json_response({"error": "atlas_media_upload_size_conflict"}, status=409)
        try:
            received = await asyncio.to_thread(
                media_blobs.append_chunk,
                str(upload["temp_storage_key"]),
                offset=offset,
                data=chunk,
            )
            upload = await asyncio.to_thread(
                media_storage.atlas_media_record_upload_progress,
                str(upload["upload_id"]),
                expected_offset=offset,
                received_size=received,
            )
        except (AtlasMediaError, ValueError) as exc:
            code = getattr(exc, "code", str(exc))
            return web.json_response(
                {"error": code, "message": str(exc), "offset": int(upload["received_size"])},
                status=409,
                headers={"Upload-Offset": str(upload["received_size"])},
            )
        response_payload: dict[str, Any] = {"upload": public_media_upload(upload)}
        status = 200
        if int(upload["received_size"]) == int(upload["expected_size"]):
            response_payload.update({"job": await queue_media_finalize(upload), "queued": True})
            status = 202
        return web.json_response(
            response_payload,
            status=status,
            headers={"Upload-Offset": str(upload["received_size"])},
        )

    async def media_content(request: web.Request) -> web.StreamResponse:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        try:
            asset_id = int(request.match_info.get("asset_id") or 0)
        except (TypeError, ValueError):
            asset_id = 0
        asset = await asyncio.to_thread(
            media_storage.atlas_media_asset,
            int(dashboard["organization"]["id"]),
            int(selected.user_id),
            asset_id,
        )
        if asset is None or str(asset.get("status")) != "ready" or not asset.get("storage_key"):
            raise web.HTTPNotFound()
        path = media_blobs.path(str(asset["storage_key"]))
        if not path.is_file():
            raise web.HTTPServiceUnavailable(
                text='{"error":"atlas_media_blob_unavailable"}',
                content_type="application/json",
            )
        response = web.FileResponse(path)
        response.content_type = str(asset.get("mime_type") or "application/octet-stream")
        disposition = "inline" if str(asset.get("media_kind")) in {"video", "audio", "image"} else "attachment"
        response.headers["Content-Disposition"] = (
            f"{disposition}; filename*=UTF-8''{quote(str(asset['original_filename']))}"
        )
        response.headers["Cache-Control"] = "private, max-age=60"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def media_segments(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        try:
            asset_id = int(request.match_info.get("asset_id") or 0)
            if request.method == "GET":
                items = await asyncio.to_thread(
                    media_storage.atlas_media_segments,
                    organization_id,
                    int(selected.user_id),
                    asset_id,
                )
                return web.json_response({"items": items})
            payload = await body(request, selected)
            item = await asyncio.to_thread(
                media_storage.atlas_media_create_segment,
                organization_id,
                int(selected.user_id),
                asset_id,
                title=str(payload.get("title") or ""),
                start_ms=int(payload.get("start_ms") or 0),
                end_ms=int(payload.get("end_ms") or 0),
                segment_kind=str(payload.get("segment_kind") or "clip"),
                metadata=dict(payload.get("metadata") or {}),
            )
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc), "message": "Фрагмент не создан."}, status=400)
        return web.json_response({"segment": item}, status=201)

    async def cases(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        if request.method == "GET":
            try:
                items = await asyncio.to_thread(
                    case_storage.atlas_cases,
                    organization_id,
                    int(selected.user_id),
                    status=str(request.query.get("status") or "") or None,
                    limit=max(1, min(120, int(request.query.get("limit") or 60))),
                )
            except (TypeError, ValueError) as exc:
                return web.json_response({"error": str(exc)}, status=400)
            return web.json_response({"items": items})
        payload = await body(request, selected)
        try:
            item = await asyncio.to_thread(
                case_storage.atlas_case_create,
                organization_id,
                int(selected.user_id),
                title=str(payload.get("title") or ""),
                case_kind=str(payload.get("case_kind") or "investigation"),
                priority=str(payload.get("priority") or "routine"),
                visibility_scope=str(payload.get("visibility_scope") or "workspace"),
                objective=str(payload.get("objective") or ""),
                executive_summary=str(payload.get("executive_summary") or ""),
                hypothesis=str(payload.get("hypothesis") or ""),
                assigned_to_id=int(payload.get("assigned_to_id") or 0) or None,
            )
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc), "message": "Дело не создано."}, status=400)
        return web.json_response({"case": item}, status=201)

    async def case_detail(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        try:
            case_id = int(request.match_info.get("case_id") or 0)
            if request.method == "GET":
                detail = await asyncio.to_thread(
                    case_storage.atlas_case_detail,
                    organization_id,
                    int(selected.user_id),
                    case_id,
                )
                return web.json_response(detail)
            payload = await body(request, selected)
            item = await asyncio.to_thread(
                case_storage.atlas_case_set_status,
                organization_id,
                int(selected.user_id),
                case_id,
                status=str(payload.get("status") or ""),
                expected_version=int(payload.get("expected_version") or 0),
            )
        except (TypeError, ValueError) as exc:
            status = 409 if "version_conflict" in str(exc) else 400
            return web.json_response({"error": str(exc), "message": "Состояние дела не изменено."}, status=status)
        return web.json_response({"case": item})

    async def case_claims(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        try:
            case_id = int(request.match_info.get("case_id") or 0)
            raw_claim_id = str(request.match_info.get("claim_id") or "").strip()
            payload = await body(request, selected)
            if raw_claim_id:
                item = await asyncio.to_thread(
                    case_storage.atlas_case_update_claim,
                    organization_id,
                    int(selected.user_id),
                    case_id,
                    int(raw_claim_id),
                    claim_status=str(payload.get("claim_status") or ""),
                    rationale=str(payload.get("rationale") or ""),
                    expected_version=int(payload.get("expected_version") or 0),
                )
                return web.json_response({"claim": item})
            item = await asyncio.to_thread(
                case_storage.atlas_case_add_claim,
                organization_id,
                int(selected.user_id),
                case_id,
                statement=str(payload.get("statement") or ""),
                importance=str(payload.get("importance") or "material"),
                rationale=str(payload.get("rationale") or ""),
            )
        except (TypeError, ValueError) as exc:
            status = 409 if "version_conflict" in str(exc) else 400
            return web.json_response({"error": str(exc), "message": "Утверждение не сохранено."}, status=status)
        return web.json_response({"claim": item}, status=201)

    async def case_evidence(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        organization_id = int(dashboard["organization"]["id"])
        try:
            case_id = int(request.match_info.get("case_id") or 0)
            raw_evidence_id = str(request.match_info.get("evidence_id") or "").strip()
            payload = await body(request, selected)
            if raw_evidence_id:
                item = await asyncio.to_thread(
                    case_storage.atlas_case_review_evidence,
                    organization_id,
                    int(selected.user_id),
                    case_id,
                    int(raw_evidence_id),
                    verification_status=str(payload.get("verification_status") or ""),
                    admissibility=str(payload.get("admissibility") or ""),
                    expected_version=int(payload.get("expected_version") or 0),
                )
                return web.json_response({"evidence": item})
            item = await asyncio.to_thread(
                case_storage.atlas_case_add_evidence,
                organization_id,
                int(selected.user_id),
                case_id,
                source_type=str(payload.get("source_type") or ""),
                source_id=payload.get("source_id"),
                title=str(payload.get("title") or ""),
                summary=str(payload.get("summary") or ""),
                relevance=str(payload.get("relevance") or ""),
                claim_id=int(payload.get("claim_id") or 0) or None,
                locator=dict(payload.get("locator") or {}),
                provenance=dict(payload.get("provenance") or {}),
            )
        except (TypeError, ValueError) as exc:
            status = 409 if "version_conflict" in str(exc) else 400
            return web.json_response({"error": str(exc), "message": "Доказательство не сохранено."}, status=status)
        return web.json_response({"evidence": item}, status=201)

    async def case_findings(request: web.Request) -> web.Response:
        selected = await principal(request)
        await require_atlas(selected)
        dashboard = await user_dashboard(request, selected)
        payload = await body(request, selected)
        try:
            item = await asyncio.to_thread(
                case_storage.atlas_case_record_finding,
                int(dashboard["organization"]["id"]),
                int(selected.user_id),
                int(request.match_info.get("case_id") or 0),
                analysis_type=str(payload.get("analysis_type") or "readiness"),
                title=str(payload.get("title") or ""),
                summary=str(payload.get("summary") or ""),
                result=dict(payload.get("result") or {}),
                status=str(payload.get("status") or "draft"),
            )
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc), "message": "Вывод не сохранён."}, status=400)
        return web.json_response({"finding": item}, status=201)

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
                    "federation_scope",
                    "confirm_platform_scope",
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
            federation_scope = explicit_platform_scope(values)
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
                federation_scope=federation_scope,
                original_filename=str(extracted["filename"]),
                knowledge_domain=values.get("knowledge_domain") or None,
                corpus_kind=values.get("corpus_kind") or None,
            )
        except (AtlasKnowledgeFileError, ValueError) as exc:
            return web.json_response(
                {"error": getattr(exc, "code", str(exc)), "message": str(exc)},
                status=400,
            )
        queued_job = await queue_knowledge_index(source)
        return web.json_response(
            {
                "source": source,
                "job": queued_job,
                "queued": True,
                "message": "Файл загружен. Atlas готовит его для поиска.",
            },
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
                federation_scope = explicit_platform_scope(payload)
                dashboard = await user_dashboard(request, selected)
            except (TypeError, ValueError) as exc:
                return web.json_response(
                    {"error": str(exc), "message": "Проверьте сервер, организацию и доступ материала."},
                    status=400,
                )
            queued_job = await queue_forum_listing_import(
                source_url=source_url,
                organization_id=int(dashboard["organization"]["id"]),
                actor_user_id=int(selected.user_id),
                server_code=server_code,
                faction_code=faction_code,
                visibility_scope=visibility_scope,
                federation_scope=federation_scope,
                knowledge_domain=str(payload.get("knowledge_domain") or "") or None,
                corpus_kind=str(payload.get("corpus_kind") or "") or None,
                request_key=str(request.headers.get("X-Idempotency-Key") or "") or None,
            )
            return web.json_response(
                {
                    "job": queued_job,
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
            federation_scope = explicit_platform_scope(payload)
            dashboard = await user_dashboard(request, selected)
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {
                    "error": str(exc),
                    "message": "Проверьте сервер, организацию и доступ материала.",
                },
                status=400,
            )
        queued_job = await queue_forum_thread_import(
            source_url=source_url,
            organization_id=int(dashboard["organization"]["id"]),
            actor_user_id=int(selected.user_id),
            server_code=server_code,
            faction_code=faction_code,
            visibility_scope=visibility_scope,
            federation_scope=federation_scope,
            knowledge_domain=str(payload.get("knowledge_domain") or "") or None,
            corpus_kind=str(payload.get("corpus_kind") or "") or None,
            request_key=str(request.headers.get("X-Idempotency-Key") or "") or None,
        )
        return web.json_response(
            {
                "job": queued_job,
                "queued": True,
                "browser_url": "http://127.0.0.1:7900/?autoconnect=1&resize=scale",
                "message": (
                    "Тема принята. Atlas прочитает её в фоне, классифицирует и добавит в поиск."
                ),
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
            if resource == "project":
                item = await asyncio.to_thread(
                    storage.atlas_upsert_project,
                    int(selected.user_id),
                    code=str(payload.get("code") or ""),
                    name=str(payload.get("name") or ""),
                    description=str(payload.get("description") or "") or None,
                    enabled=bool(payload.get("enabled", True)),
                )
            elif resource == "server":
                item = await asyncio.to_thread(
                    storage.atlas_upsert_server,
                    int(selected.user_id),
                    code=str(payload.get("code") or ""),
                    name=str(payload.get("name") or ""),
                    number=payload.get("number"),
                    project_code=str(payload.get("project_code") or "majestic-rp"),
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

    async def forum_engine_control(request: web.Request) -> web.Response:
        require_desktop_client(request)
        selected = await principal(request)
        await require_atlas(selected)
        if request.method == "POST":
            await require_atlas_admin(selected)
            await body(request, selected)
            if forum_engine_runner is None or not forum_engine_runner.trigger():
                return web.json_response(
                    {
                        "error": "atlas_forum_engine_disabled",
                        "message": "Atlas Forum Engine сейчас отключён.",
                    },
                    status=409,
                )
        status, items, characters = await asyncio.gather(
            asyncio.to_thread(
                forum_engine_storage.forum_monitor_status,
                int(guild_id),
            ),
            asyncio.to_thread(
                forum_engine_storage.user_forum_complaints,
                int(guild_id),
                int(selected.user_id),
                limit=100,
            ),
            asyncio.to_thread(
                forum_engine_storage.list_monitored_characters,
                int(guild_id),
            ),
        )
        own_characters = [
            {
                "id": int(item["character_id"]),
                "nickname": str(item.get("nickname") or ""),
                "static_id": str(item.get("static_id") or ""),
            }
            for item in characters
            if int(item["user_id"]) == int(selected.user_id)
        ]
        return web.json_response(
            {
                "accepted": request.method == "POST",
                "enabled": bool(forum_engine_runner and forum_engine_runner.config.enabled),
                "status": _forum_engine_status_view(
                    status,
                    administrator=bool(selected.administrator),
                ),
                "characters": own_characters,
                "items": _forum_engine_items_view(items),
            },
            status=202 if request.method == "POST" else 200,
        )

    async def forum_engine_profile(request: web.Request) -> web.Response:
        require_desktop_client(request)
        selected = await principal(request)
        await require_atlas(selected)
        try:
            profile = await asyncio.to_thread(
                forum_engine_storage.forum_static_profile,
                int(guild_id),
                str(request.query.get("static_id") or ""),
                limit=40,
            )
        except ValueError:
            return web.json_response(
                {
                    "error": "atlas_forum_static_invalid",
                    "message": "Укажите корректный числовой статик.",
                },
                status=400,
            )
        return web.json_response(
            {
                **profile,
                "items": _forum_engine_items_view(profile.get("items", [])),
            }
        )

    app.router.add_get("/atlas", atlas_index)
    app.router.add_get("/atlas/", atlas_index)
    app.router.add_get("/atlas/assets/{name}", atlas_asset)
    app.router.add_get("/api/atlas/bootstrap", bootstrap)
    app.router.add_get("/api/atlas/overlay/context", overlay_context_get)
    app.router.add_post("/api/atlas/overlay/context", overlay_context_set)
    app.router.add_get("/api/atlas/overlay/crafts", overlay_crafts)
    app.router.add_post("/api/atlas/overlay/transcribe", overlay_transcribe)
    app.router.add_get("/api/atlas/overlay/tts/voices", overlay_tts_voices)
    app.router.add_post("/api/atlas/overlay/tts/preview", overlay_tts_preview)
    app.router.add_post(
        "/api/atlas/overlay/tts/synthesize", overlay_tts_synthesize
    )
    app.router.add_post("/api/atlas/onboarding", onboarding)
    app.router.add_post("/api/atlas/chat", chat)
    app.router.add_post("/api/atlas/chat/stream", chat_stream)
    app.router.add_post("/api/atlas/messages/{message_id}/feedback", message_feedback)
    app.router.add_get("/api/atlas/threads", threads)
    app.router.add_get("/api/atlas/threads/{thread_id}", threads)
    app.router.add_get("/api/atlas/documents", documents)
    app.router.add_post("/api/atlas/documents", documents)
    app.router.add_get("/api/atlas/documents/{document_id}", document_detail)
    app.router.add_patch("/api/atlas/documents/{document_id}", document_detail)
    app.router.add_post("/api/atlas/documents/{document_id}/comments", document_comments)
    app.router.add_patch("/api/atlas/documents/{document_id}/comments/{comment_id}", document_comments)
    app.router.add_put("/api/atlas/documents/{document_id}/approvals", document_approvals)
    app.router.add_patch("/api/atlas/documents/{document_id}/approvals/{approval_id}", document_approvals)
    app.router.add_get("/api/atlas/timeline", timeline)
    app.router.add_post("/api/atlas/timeline", timeline)
    app.router.add_patch("/api/atlas/timeline/{event_id}", timeline_item)
    app.router.add_post("/api/atlas/entity-links", entity_links)
    app.router.add_get("/api/atlas/jobs", jobs)
    app.router.add_get("/api/atlas/jobs/{job_id}", jobs)
    app.router.add_get("/api/atlas/search", global_search)
    app.router.add_get("/api/atlas/media", media_library)
    app.router.add_post("/api/atlas/media/uploads", media_upload_start)
    app.router.add_put("/api/atlas/media/uploads/{upload_id}", media_upload_chunk)
    app.router.add_get("/api/atlas/media/{asset_id}/content", media_content)
    app.router.add_get("/api/atlas/media/{asset_id}/segments", media_segments)
    app.router.add_post("/api/atlas/media/{asset_id}/segments", media_segments)
    app.router.add_get("/api/atlas/cases", cases)
    app.router.add_post("/api/atlas/cases", cases)
    app.router.add_get("/api/atlas/cases/{case_id}", case_detail)
    app.router.add_patch("/api/atlas/cases/{case_id}", case_detail)
    app.router.add_post("/api/atlas/cases/{case_id}/claims", case_claims)
    app.router.add_patch("/api/atlas/cases/{case_id}/claims/{claim_id}", case_claims)
    app.router.add_post("/api/atlas/cases/{case_id}/evidence", case_evidence)
    app.router.add_patch("/api/atlas/cases/{case_id}/evidence/{evidence_id}", case_evidence)
    app.router.add_post("/api/atlas/cases/{case_id}/findings", case_findings)
    app.router.add_get("/api/atlas/knowledge", knowledge)
    app.router.add_post("/api/atlas/knowledge", knowledge)
    app.router.add_post("/api/atlas/knowledge/upload", knowledge_upload)
    app.router.add_post("/api/atlas/knowledge/import-forum", knowledge_import_forum)
    app.router.add_get("/api/atlas/forum-attachments", forum_attachments)
    app.router.add_post(
        "/api/atlas/forum-attachments/{attachment_id}/review",
        forum_attachments,
    )
    app.router.add_get("/api/atlas/forum-sync", forum_sync_control)
    app.router.add_post("/api/atlas/forum-sync", forum_sync_control)
    app.router.add_get("/api/atlas/forum-engine", forum_engine_control)
    app.router.add_post("/api/atlas/forum-engine", forum_engine_control)
    app.router.add_get("/api/atlas/forum-engine/profile", forum_engine_profile)
    app.router.add_get("/api/admin/atlas", admin_overview)
    app.router.add_post("/api/admin/atlas/catalog", admin_catalog_control)
    app.router.add_post("/api/admin/atlas/spaces", admin_space_control)


__all__ = ["register_atlas_web_routes"]
