"""Non-blocking, secret-aware global event collection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from aiohttp import web

from persistence import global_log_repository as repository


_SECRET_KEY = re.compile(
    r"(?:pass(?:word)?|pin|secret|token|authorization|cookie|api[_-]?key|private[_-]?key|rcon|csrf)",
    re.IGNORECASE,
)
_TOKEN_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|bot)\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:token|password|secret|api[_-]?key|pin)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\b[MN][A-Za-z\d_-]{20,}\.[A-Za-z\d_-]{5,}\.[A-Za-z\d_-]{20,}\b"),
)
_MAX_DEPTH = 8
_MAX_STRING = 100_000
_queue: asyncio.Queue[dict[str, Any]] | None = None
_worker: asyncio.Task[Any] | None = None
_loop: asyncio.AbstractEventLoop | None = None
_dropped = 0
_written = 0
_failed = 0
_last_error: str | None = None


def scrub_text(value: Any, *, limit: int = _MAX_STRING) -> str:
    text = str(value or "")[:limit]
    for pattern in _TOKEN_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda match: match.group(1) + "[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


def redact_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if _SECRET_KEY.search(str(key or "")):
        if value in (None, ""):
            return None
        return {"redacted": True, "length": len(str(value)), "type": type(value).__name__}
    if depth >= _MAX_DEPTH:
        return "[DEPTH_LIMIT]"
    if isinstance(value, Mapping):
        return {
            str(item_key)[:200]: redact_value(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in list(value.items())[:500]
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_value(item, depth=depth + 1) for item in list(value)[:500]]
    if isinstance(value, bytes):
        return {"binary": True, "length": len(value)}
    if isinstance(value, str):
        return scrub_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub_text(value, limit=10_000)


def hash_remote(value: str | None) -> str | None:
    remote = str(value or "").strip()
    if not remote:
        return None
    salt = os.getenv("GLOBAL_LOG_IP_HASH_SALT", "tmod-global-log-ip-v1").encode("utf-8")
    return hashlib.sha256(salt + b":" + remote.encode("utf-8")).hexdigest()


def _normalized_event(event: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(redact_value(dict(event)))
    value.setdefault("event_uuid", str(uuid4()))
    value.setdefault("occurred_at", datetime.now(timezone.utc).isoformat())
    value["summary"] = scrub_text(value.get("summary") or value.get("event_type") or "event", limit=4000)
    if value.get("content_text") is not None:
        value["content_text"] = scrub_text(value["content_text"])
    return value


def emit_global_event(event: Mapping[str, Any]) -> bool:
    """Queue an event without blocking Discord or an HTTP response."""
    global _dropped
    if not repository.global_log_enabled():
        return False
    value = _normalized_event(event)
    queue = _queue
    loop = _loop
    if queue is None or loop is None:
        # Startup events are rare; use a daemon thread through asyncio's caller
        # later rather than silently losing them.  A process without a runtime
        # can still write synchronously (tests and maintenance scripts).
        try:
            repository.append_event(value)
            return True
        except Exception as exc:
            print(f"Global log startup write failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return False
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        try:
            queue.put_nowait(value)
            return True
        except asyncio.QueueFull:
            _dropped += 1
            return False

    def enqueue() -> None:
        global _dropped
        try:
            queue.put_nowait(value)
        except asyncio.QueueFull:
            _dropped += 1

    loop.call_soon_threadsafe(enqueue)
    return True


async def _writer() -> None:
    global _written, _failed, _last_error
    assert _queue is not None
    while True:
        event = await _queue.get()
        try:
            await asyncio.to_thread(repository.append_event, event)
            _written += 1
            _last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _failed += 1
            _last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            traceback.print_exc()
        finally:
            _queue.task_done()


async def start_global_log_runtime(loop: asyncio.AbstractEventLoop | None = None) -> dict[str, Any]:
    global _queue, _worker, _loop
    if not repository.global_log_enabled():
        return runtime_health()
    selected_loop = loop or asyncio.get_running_loop()
    if _queue is None:
        _queue = asyncio.Queue(
            maxsize=max(1000, min(200_000, int(os.getenv("GLOBAL_LOG_QUEUE_MAXSIZE", "50000") or 50000)))
        )
    _loop = selected_loop
    await asyncio.to_thread(repository.initialize_global_log)
    if _worker is None or _worker.done():
        _worker = selected_loop.create_task(_writer(), name="tmod-global-log-writer")
    emit_global_event({
        "source_service": "tmod",
        "source_type": "system",
        "event_type": "global_log_started",
        "summary": "Контур глобального журнала запущен",
        "details": {"queue_max": _queue.maxsize, "database": repository.global_log_database_name()},
    })
    return runtime_health()


def runtime_health() -> dict[str, Any]:
    return {
        "enabled": repository.global_log_enabled(),
        "running": bool(_worker is not None and not _worker.done()),
        "queued": _queue.qsize() if _queue is not None else 0,
        "written": _written,
        "failed": _failed,
        "dropped": _dropped,
        "last_error": _last_error,
    }


def activity_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_service": "discord",
        "source_type": "discord_activity",
        "event_type": str(payload.get("event_type") or "activity"),
        "actor_user_id": payload.get("user_id"),
        "actor_display": payload.get("display_name") or payload.get("name"),
        "guild_id": payload.get("guild_id"),
        "channel_id": payload.get("channel_id"),
        "message_id": payload.get("message_id"),
        "summary": str(payload.get("event_text") or payload.get("event_type") or "Discord activity"),
        "details": dict(payload),
    }


async def _request_payload(request: web.Request) -> Any:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    length = int(request.content_length or 0)
    maximum = max(4096, min(262_144, int(os.getenv("GLOBAL_LOG_HTTP_BODY_LIMIT", "65536") or 65536)))
    if length > maximum:
        return {"omitted": "body_too_large", "content_length": length}
    content_type = str(request.content_type or "").lower()
    if "multipart/" in content_type:
        return {"omitted": "multipart", "content_length": length}
    try:
        raw = await request.read()
    except Exception as exc:
        return {"unreadable": type(exc).__name__}
    if not raw:
        return None
    if "json" in content_type:
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    if content_type.startswith("text/") or "form-urlencoded" in content_type or not content_type:
        return scrub_text(raw.decode("utf-8", errors="replace"), limit=maximum)
    return {"binary": True, "content_type": content_type, "length": len(raw)}


def _response_payload(response: web.StreamResponse, path: str) -> Any:
    if path.startswith("/api/global-log/events") or path.startswith("/api/global-log/export"):
        return {"omitted": "global_log_result"}
    if not isinstance(response, web.Response) or response.body is None:
        return None
    body = response.body
    if len(body) > 65_536:
        return {"omitted": "response_too_large", "length": len(body)}
    content_type = str(response.content_type or "")
    try:
        text = body.decode(response.charset or "utf-8", errors="replace")
    except (AttributeError, LookupError):
        return {"binary": True, "length": len(body)}
    if "json" in content_type:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return scrub_text(text, limit=65_536) if content_type.startswith("text/") else {"length": len(body), "content_type": content_type}


@web.middleware
async def global_log_web_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    if not repository.global_log_enabled():
        return await handler(request)
    started = time.perf_counter()
    request_id = request.headers.get("X-Request-ID", "").strip()[:120] or str(uuid4())
    request_payload = await _request_payload(request)
    actor_user_id: int | None = None
    try:
        from modules.consensus_web_auth import signed_session_identity

        guild_raw = os.getenv("CONSENSUS_WEB_GUILD_ID") or os.getenv("DISCORD_GUILD_ID") or "0"
        identity = signed_session_identity(
            request,
            expected_guild_id=int(guild_raw) if str(guild_raw).isdigit() else 0,
        )
        if identity is not None:
            actor_user_id = int(identity[1])
    except Exception:
        # Authentication projection is enrichment only; it must not affect the
        # request being observed.
        actor_user_id = None
    status = 500
    response: web.StreamResponse | None = None
    failure: BaseException | None = None
    try:
        response = await handler(request)
        status = int(response.status)
        return response
    except web.HTTPException as exc:
        status = int(exc.status)
        response = exc
        raise
    except BaseException as exc:
        failure = exc
        raise
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        remote = forwarded or request.remote
        severity = "error" if failure is not None or status >= 500 else "warning" if status >= 400 else "info"
        emit_global_event({
            "source_service": "web",
            "source_type": "http",
            "event_type": "http_request",
            "severity": severity,
            "request_id": request_id,
            "actor_user_id": actor_user_id,
            "summary": f"{request.method} {request.path} → {status}",
            "target_type": "route",
            "target_id": request.path,
            "content_text": json.dumps(redact_value(request_payload), ensure_ascii=False, default=str) if request_payload is not None else None,
            "details": {
                "method": request.method,
                "path": request.path,
                "query": redact_value(dict(request.query)),
                "request": request_payload,
                "response": _response_payload(response, request.path) if response is not None else None,
                "exception": f"{type(failure).__name__}: {failure}" if failure is not None else None,
                "host": request.host,
                "referer": request.headers.get("Referer"),
            },
            "ip_hash": hash_remote(remote),
            "user_agent": request.headers.get("User-Agent", "")[:1000],
            "status_code": status,
            "duration_ms": elapsed,
        })


__all__ = [
    "activity_event", "emit_global_event", "global_log_web_middleware", "hash_remote",
    "redact_value", "runtime_health", "scrub_text", "start_global_log_runtime",
]
