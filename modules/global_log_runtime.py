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
from pathlib import Path
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Mapping
from uuid import uuid4

from aiohttp import web

from persistence import global_log_repository as repository


_SECRET_KEY = re.compile(
    r"(?:pass(?:word)?|pin|secret|token|authorization|cookie|api[_-]?key|private[_-]?key|rcon|csrf)",
    re.IGNORECASE,
)
_TOKEN_PATTERNS = (
    re.compile(r"(?i)((?:одноразовый код|код подтверждения)\s*[:：]\s*`?)[0-9A-Z-]{6,32}"),
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
_spool_overflow_dropped = 0
_written = 0
_failed = 0
_last_error: str | None = None
_spool_lock = threading.Lock()
_last_spool_replay = 0.0
_trace_context: ContextVar[dict[str, Any]] = ContextVar("tmod_global_log_trace", default={})
_sql_rollup_lock = threading.Lock()
_sql_rollups: dict[tuple[str, str, str], dict[str, Any]] = {}


@contextmanager
def global_log_context(**values: Any):
    """Attach causal identifiers to every event emitted in this operation."""
    merged = dict(_trace_context.get())
    merged.update({key: value for key, value in values.items() if value not in (None, "")})
    token = _trace_context.set(merged)
    try:
        yield
    finally:
        _trace_context.reset(token)


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
    context = _trace_context.get()
    for key in ("request_id", "session_id", "actor_user_id", "guild_id", "channel_id"):
        if value.get(key) in (None, "") and context.get(key) not in (None, ""):
            value[key] = context[key]
    if not value.get("details"):
        value["details"] = {}
    if isinstance(value.get("details"), dict) and context.get("trace_id"):
        value["details"].setdefault("trace_id", context["trace_id"])
    if not value.get("event_uuid"):
        value["event_uuid"] = str(uuid4())
    if not value.get("occurred_at"):
        value["occurred_at"] = datetime.now(timezone.utc).isoformat()
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
            try:
                _spool_events([value])
                return True
            except Exception:
                pass
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
            try:
                _spool_events([value])
                return True
            except Exception:
                _dropped += 1
                return False

    def enqueue() -> None:
        global _dropped
        try:
            queue.put_nowait(value)
        except asyncio.QueueFull:
            try:
                _spool_events([value])
            except Exception:
                _dropped += 1

    try:
        loop.call_soon_threadsafe(enqueue)
        return True
    except RuntimeError:
        try:
            _spool_events([value])
            return True
        except Exception:
            _dropped += 1
            return False


def _spool_path() -> Path:
    explicit = os.getenv("GLOBAL_LOG_SPOOL_FILE", "").strip()
    if explicit:
        return Path(explicit)
    process_name = re.sub(
        r"[^a-z0-9_-]+",
        "-",
        os.getenv("POSTGRES_APPLICATION_NAME", "tmod-discord").strip().lower(),
    ).strip("-") or "tmod"
    return Path(f"/app/persistent/data/global-log-spool-{process_name}.jsonl")


def _spool_max_bytes() -> int:
    try:
        value = int(os.getenv("GLOBAL_LOG_SPOOL_MAX_BYTES", str(64 * 1024 * 1024)) or 0)
    except (TypeError, ValueError):
        value = 64 * 1024 * 1024
    return max(1 * 1024 * 1024, min(value, 2 * 1024 * 1024 * 1024))


def _spool_events(events: list[dict[str, Any]]) -> None:
    """Persist events with a hard byte ceiling.

    The spool is a safety net for database outages, not an unbounded second
    database.  Once full, the oldest records are discarded to keep the newest
    operational context and the loss is exposed in runtime health.
    """
    global _spool_overflow_dropped
    if not events:
        return
    path = _spool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = [
        (json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        for event in events
    ]
    max_bytes = _spool_max_bytes()
    incoming_size = sum(len(line) for line in serialized)
    with _spool_lock:
        current_size = 0
        try:
            current_size = path.stat().st_size
        except OSError:
            pass
        if current_size + incoming_size <= max_bytes:
            with path.open("ab") as stream:
                for line in serialized:
                    stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            return

        try:
            existing = path.read_bytes().splitlines(keepends=True)
        except OSError:
            existing = []
        lines = existing + serialized
        total = sum(len(line) for line in lines)
        removed = 0
        while lines and total > max_bytes:
            total -= len(lines.pop(0))
            removed += 1
        if removed:
            _spool_overflow_dropped += removed
        if not lines:
            path.unlink(missing_ok=True)
            return
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(b"".join(lines))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)


def _replay_spool() -> int:
    path = _spool_path()
    if not path.exists() or path.stat().st_size <= 0:
        return 0
    with _spool_lock:
        lines = path.read_text(encoding="utf-8").splitlines()
        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
        if not events:
            path.unlink(missing_ok=True)
            return 0
        # Keep transactions bounded while preserving the original queue order.
        for offset in range(0, len(events), 250):
            repository.append_events(events[offset: offset + 250])
        path.unlink(missing_ok=True)
        return len(events)


def _record_sql_rollup(payload: Mapping[str, Any]) -> None:
    """Aggregate internal SQL telemetry instead of duplicating every action.

    Business, HTTP, Discord and security events remain immutable event rows.
    Successful internal SQL calls are implementation detail and used to make
    up almost the entire audit database, so they are preserved as per-minute
    counters by query fingerprint. SQL errors and slow statements are still
    emitted individually by ``database_audit`` below.
    """
    occurred = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    fingerprint = str(payload.get("fingerprint") or "")[:64]
    outcome = str(payload.get("outcome") or "success")[:16]
    operation = str(payload.get("operation") or "SQL")[:32]
    key = (occurred.isoformat(), fingerprint, outcome)
    duration = max(0.0, float(payload.get("duration_ms") or 0.0))
    with _sql_rollup_lock:
        value = _sql_rollups.get(key)
        if value is None:
            value = {
                "bucket_at": occurred.isoformat(),
                "fingerprint": fingerprint,
                "operation": operation,
                "outcome": outcome,
                "tables": list(payload.get("tables") or [])[:20],
                "statement_sample": str(payload.get("statement") or "")[:4000],
                "count": 0,
                "duration_total_ms": 0.0,
                "duration_max_ms": 0.0,
                "rowcount_total": 0,
                "last_error": None,
            }
            _sql_rollups[key] = value
        value["count"] += 1
        value["duration_total_ms"] += duration
        value["duration_max_ms"] = max(value["duration_max_ms"], duration)
        rowcount = payload.get("rowcount")
        if isinstance(rowcount, int) and rowcount > 0:
            value["rowcount_total"] += rowcount
        if payload.get("error"):
            value["last_error"] = str(payload["error"])[:1000]


def _drain_sql_rollups() -> list[dict[str, Any]]:
    with _sql_rollup_lock:
        values = list(_sql_rollups.values())
        _sql_rollups.clear()
    return values


def _flush_sql_rollups() -> int:
    values = _drain_sql_rollups()
    if not values:
        return 0
    try:
        repository.upsert_sql_rollups(values)
    except Exception:
        # Merge the counters back so a temporary database outage does not turn
        # telemetry compression into telemetry loss.
        with _sql_rollup_lock:
            for value in values:
                key = (
                    str(value["bucket_at"]),
                    str(value["fingerprint"]),
                    str(value["outcome"]),
                )
                current = _sql_rollups.get(key)
                if current is None:
                    _sql_rollups[key] = value
                    continue
                current["count"] += int(value.get("count") or 0)
                current["duration_total_ms"] += float(value.get("duration_total_ms") or 0)
                current["duration_max_ms"] = max(
                    float(current.get("duration_max_ms") or 0),
                    float(value.get("duration_max_ms") or 0),
                )
                current["rowcount_total"] += int(value.get("rowcount_total") or 0)
                if value.get("last_error"):
                    current["last_error"] = value["last_error"]
        raise
    return len(values)


async def _writer() -> None:
    global _written, _failed, _last_error, _last_spool_replay
    assert _queue is not None
    while True:
        first = await _queue.get()
        batch = [first]
        deadline = asyncio.get_running_loop().time() + 0.075
        while len(batch) < 250:
            try:
                batch.append(_queue.get_nowait())
                continue
            except asyncio.QueueEmpty:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(_queue.get(), timeout=remaining))
                except TimeoutError:
                    break
        try:
            await asyncio.to_thread(repository.append_events, batch)
            _written += len(batch)
            await asyncio.to_thread(_flush_sql_rollups)
            _last_error = None
            now = time.monotonic()
            if now - _last_spool_replay >= 60:
                _last_spool_replay = now
                _written += await asyncio.to_thread(_replay_spool)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(asyncio.to_thread(_spool_events, batch))
            except Exception:
                traceback.print_exc()
            raise
        except Exception as exc:
            _failed += len(batch)
            _last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            traceback.print_exc()
            try:
                await asyncio.to_thread(_spool_events, batch)
            except Exception:
                traceback.print_exc()
        finally:
            for _ in batch:
                _queue.task_done()


async def start_global_log_runtime(loop: asyncio.AbstractEventLoop | None = None) -> dict[str, Any]:
    global _queue, _worker, _loop, _written, _last_error
    if not repository.global_log_enabled():
        return runtime_health()
    selected_loop = loop or asyncio.get_running_loop()
    if _queue is None:
        _queue = asyncio.Queue(
            maxsize=max(1000, min(200_000, int(os.getenv("GLOBAL_LOG_QUEUE_MAXSIZE", "50000") or 50000)))
        )
    _loop = selected_loop
    from persistence.postgres_compat import set_postgres_audit_hook

    def database_audit(payload: Mapping[str, Any]) -> None:
        duration = float(payload.get("duration_ms") or 0)
        outcome = str(payload.get("outcome") or "success")
        _record_sql_rollup(payload)
        # Errors and slow calls deserve an individual immutable event. Normal
        # successful calls remain fully countable in compact SQL rollups.
        if outcome != "error" and duration < max(
            250.0,
            float(os.getenv("GLOBAL_LOG_SQL_SLOW_MS", "1000") or 1000),
        ):
            return
        emit_global_event({
            "source_service": "postgresql",
            "source_type": "database",
            "event_type": "sql_error" if outcome == "error" else "sql_slow_statement",
            "severity": "error" if outcome == "error" else "warning" if duration >= 500 else "info",
            "summary": f"SQL {payload.get('operation') or 'operation'} · {duration:.1f} ms",
            "target_type": "database_table",
            "target_id": ",".join(payload.get("tables") or []) or None,
            "content_text": payload.get("statement"),
            "duration_ms": duration,
            "details": dict(payload),
        })

    set_postgres_audit_hook(database_audit)
    try:
        await asyncio.to_thread(repository.initialize_global_log)
        _written += await asyncio.to_thread(_replay_spool)
    except Exception as exc:
        # Start the queue anyway. Until PostgreSQL recovers, the writer persists
        # every redacted event into the durable local spool.
        _last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
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


async def stop_global_log_runtime(*, timeout: float = 8.0) -> None:
    """Flush queued telemetry and preserve the remainder during shutdown."""
    global _worker, _loop
    queue = _queue
    worker = _worker
    if queue is not None and worker is not None and not worker.done():
        try:
            await asyncio.wait_for(queue.join(), timeout=max(0.1, float(timeout)))
        except TimeoutError:
            pending: list[dict[str, Any]] = []
            while True:
                try:
                    pending.append(queue.get_nowait())
                    queue.task_done()
                except asyncio.QueueEmpty:
                    break
            if pending:
                await asyncio.to_thread(_spool_events, pending)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    _worker = None
    _loop = None
    if repository.global_log_enabled():
        try:
            await asyncio.to_thread(_flush_sql_rollups)
        except Exception:
            traceback.print_exc()


def runtime_health() -> dict[str, Any]:
    try:
        spool_bytes = _spool_path().stat().st_size
    except OSError:
        spool_bytes = 0
    return {
        "enabled": repository.global_log_enabled(),
        "running": bool(_worker is not None and not _worker.done()),
        "queued": _queue.qsize() if _queue is not None else 0,
        "written": _written,
        "failed": _failed,
        "dropped": _dropped,
        "spool_overflow_dropped": _spool_overflow_dropped,
        "spool_bytes": spool_bytes,
        "spool_max_bytes": _spool_max_bytes(),
        "sql_rollups_pending": len(_sql_rollups),
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
    if request.path.startswith("/api/account/security") or request.path == "/auth/login":
        return {"omitted": "authentication_secrets"}
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
    if path.startswith("/api/account/security") or path == "/auth/login":
        return {"omitted": "authentication_secrets"}
    if path.startswith("/api/global-log/events") or path.startswith("/api/global-log/export"):
        return {"omitted": "global_log_result"}
    if not isinstance(response, web.Response) or response.body is None:
        return None
    body = response.body
    if not isinstance(body, (bytes, bytearray)):
        return {"payload_type": type(body).__name__}
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
    if request.path == "/api/global-log/internal":
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
    trace_token = _trace_context.set({
        "trace_id": request_id,
        "request_id": request_id,
        "actor_user_id": actor_user_id,
    })
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
        # Observability is strictly fail-open. A malformed third-party payload,
        # a closed event loop or an unavailable audit database may be recorded
        # as a logging failure, but can never replace the real HTTP response.
        try:
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
        except Exception as log_error:
            print(
                f"Global HTTP observation failed open: {type(log_error).__name__}: {log_error}",
                file=sys.stderr,
            )
        finally:
            _trace_context.reset(trace_token)


__all__ = [
    "activity_event", "emit_global_event", "global_log_context", "global_log_web_middleware", "hash_remote",
    "redact_value", "runtime_health", "scrub_text", "start_global_log_runtime",
    "stop_global_log_runtime",
]
