"""Isolated, append-only persistence for the T-Mod global audit log.

The log deliberately lives in a database separate from the operational T-Mod
schema.  Events form a SHA-256 chain, so an offline edit is detectable.  This
module never stores one-time login codes or browser session tokens verbatim.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from persistence.postgres_compat import _load_driver, postgres_enabled, postgres_settings


_pool: Any = None
_pool_lock = threading.Lock()
_schema_ready = False
_CHAIN_LOCK_ID = 845_103_901
_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")


def global_log_enabled() -> bool:
    return os.getenv("GLOBAL_LOG_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on",
    } and postgres_enabled()


def global_log_database_name() -> str:
    value = os.getenv("GLOBAL_LOG_DATABASE", "tmod_global_log").strip()
    if not _NAME_RE.fullmatch(value):
        raise RuntimeError("global_log_database_name_invalid")
    return value


def allowed_user_ids() -> tuple[int, ...]:
    values: list[int] = []
    for name, default in (
        ("GLOBAL_LOG_ALLOWED_USER_ID_1", "902235631952998410"),
        ("GLOBAL_LOG_ALLOWED_USER_ID_2", ""),
    ):
        raw = os.getenv(name, default).strip()
        if raw.isdigit() and int(raw) > 0 and int(raw) not in values:
            values.append(int(raw))
    return tuple(values)


def user_is_allowed(user_id: int | str) -> bool:
    try:
        selected = int(user_id)
    except (TypeError, ValueError):
        return False
    return selected in allowed_user_ids()


def _target_settings() -> tuple[str, dict[str, Any]]:
    psycopg, _ = _load_driver()
    settings = dict(postgres_settings())
    conninfo = str(settings.pop("conninfo", "") or "")
    if conninfo:
        parsed = psycopg.conninfo.conninfo_to_dict(conninfo)
        parsed["dbname"] = global_log_database_name()
        return psycopg.conninfo.make_conninfo(**parsed), {}
    settings["dbname"] = global_log_database_name()
    settings["application_name"] = "tmod-global-log"
    return "", settings


def _ensure_database() -> None:
    psycopg, _ = _load_driver()
    settings = dict(postgres_settings())
    conninfo = str(settings.pop("conninfo", "") or "")
    if conninfo:
        admin = psycopg.conninfo.conninfo_to_dict(conninfo)
        admin.setdefault("dbname", "postgres")
        connection = psycopg.connect(psycopg.conninfo.make_conninfo(**admin), autocommit=True)
    else:
        connection = psycopg.connect(autocommit=True, **settings)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (global_log_database_name(),),
            )
            if cursor.fetchone() is None:
                if os.getenv("GLOBAL_LOG_AUTO_CREATE_DATABASE", "true").lower() not in {
                    "1", "true", "yes", "on",
                }:
                    raise RuntimeError("global_log_database_missing")
                cursor.execute(
                    psycopg.sql.SQL("CREATE DATABASE {}").format(
                        psycopg.sql.Identifier(global_log_database_name())
                    )
                )
    finally:
        connection.close()


def _connection_pool() -> Any:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        _ensure_database()
        _, pool_type = _load_driver()
        conninfo, settings = _target_settings()
        _pool = pool_type(
            conninfo=conninfo,
            kwargs=settings,
            min_size=0,
            max_size=max(2, min(16, int(os.getenv("GLOBAL_LOG_POOL_MAX", "6") or 6))),
            timeout=10,
            open=True,
        )
        return _pool


def initialize_global_log() -> None:
    global _schema_ready
    if not global_log_enabled() or _schema_ready:
        return
    with _pool_lock:
        if _schema_ready:
            return
    pool = _connection_pool()
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS global_log_events (
                id BIGSERIAL PRIMARY KEY,
                event_uuid UUID NOT NULL UNIQUE,
                occurred_at TIMESTAMPTZ NOT NULL,
                ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                source_service TEXT NOT NULL,
                source_type TEXT NOT NULL,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                actor_user_id BIGINT,
                actor_display TEXT,
                guild_id BIGINT,
                channel_id BIGINT,
                message_id BIGINT,
                request_id TEXT,
                session_id TEXT,
                target_type TEXT,
                target_id TEXT,
                summary TEXT NOT NULL,
                content_text TEXT,
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                ip_hash TEXT,
                user_agent TEXT,
                status_code INTEGER,
                duration_ms DOUBLE PRECISION,
                previous_hash CHAR(64) NOT NULL,
                event_hash CHAR(64) NOT NULL UNIQUE
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS global_log_events_time_idx
            ON global_log_events (occurred_at DESC, id DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS global_log_events_source_idx
            ON global_log_events (source_service, source_type, event_type, id DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS global_log_events_actor_idx
            ON global_log_events (actor_user_id, id DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS global_log_events_channel_idx
            ON global_log_events (guild_id, channel_id, id DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS global_log_events_search_idx
            ON global_log_events USING GIN (
                to_tsvector('simple', coalesce(summary, '') || ' ' ||
                    coalesce(content_text, '') || ' ' || coalesce(actor_display, '') ||
                    ' ' || coalesce(target_id, ''))
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS global_log_login_codes (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                code_hash CHAR(64) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL,
                used_at TIMESTAMPTZ,
                attempts INTEGER NOT NULL DEFAULT 0,
                requested_from TEXT
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS global_log_codes_user_idx
            ON global_log_login_codes (user_id, id DESC)
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS global_log_sessions (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                token_hash CHAR(64) NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL,
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                revoked_at TIMESTAMPTZ,
                ip_hash TEXT,
                user_agent TEXT
            )
        """)
        connection.commit()
    _schema_ready = True


def _pepper() -> bytes:
    explicit = os.getenv("GLOBAL_LOG_AUTH_PEPPER", "").strip()
    if explicit:
        return explicit.encode("utf-8")
    settings = postgres_settings()
    seed = str(settings.get("password") or settings.get("conninfo") or "")
    if not seed:
        raise RuntimeError("global_log_auth_pepper_unavailable")
    return hashlib.sha256(("tmod/global-log/v1/" + seed).encode("utf-8")).digest()


def _secret_hash(kind: str, value: str) -> str:
    return hmac.new(_pepper(), f"{kind}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def canonical_event_payload(event: Mapping[str, Any], previous_hash: str) -> str:
    protected = {
        "event_uuid": str(event["event_uuid"]),
        "occurred_at": str(event["occurred_at"]),
        "source_service": str(event.get("source_service") or "tmod"),
        "source_type": str(event.get("source_type") or "system"),
        "event_type": str(event.get("event_type") or "event"),
        "severity": str(event.get("severity") or "info"),
        "actor_user_id": event.get("actor_user_id"),
        "actor_display": event.get("actor_display"),
        "guild_id": event.get("guild_id"),
        "channel_id": event.get("channel_id"),
        "message_id": event.get("message_id"),
        "request_id": event.get("request_id"),
        "session_id": event.get("session_id"),
        "target_type": event.get("target_type"),
        "target_id": event.get("target_id"),
        "summary": str(event.get("summary") or ""),
        "content_text": event.get("content_text"),
        "details": event.get("details") or {},
        "ip_hash": event.get("ip_hash"),
        "user_agent": event.get("user_agent"),
        "status_code": event.get("status_code"),
        "duration_ms": event.get("duration_ms"),
        "previous_hash": previous_hash,
    }
    return json.dumps(protected, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def prepare_event(event: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(event)
    value.setdefault("event_uuid", str(uuid4()))
    value.setdefault("occurred_at", datetime.now(timezone.utc).isoformat())
    value.setdefault("source_service", "tmod")
    value.setdefault("source_type", "system")
    value.setdefault("event_type", "event")
    value.setdefault("severity", "info")
    value.setdefault("summary", value["event_type"])
    details = value.get("details")
    value["details"] = details if isinstance(details, (dict, list)) else {"value": details}
    encoded_details = json.dumps(value["details"], ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded_details) > 250_000:
        value["details"] = {
            "truncated": True,
            "original_length": len(encoded_details),
            "preview": encoded_details[:200_000],
        }
    value["summary"] = str(value.get("summary") or "")[:4000]
    value["content_text"] = (
        str(value["content_text"])[:100_000]
        if value.get("content_text") is not None
        else None
    )
    for key, maximum in (
        ("source_service", 120), ("source_type", 120), ("event_type", 180),
        ("severity", 32), ("actor_display", 1000), ("request_id", 200),
        ("session_id", 200), ("target_type", 120), ("target_id", 1000),
        ("ip_hash", 128), ("user_agent", 1000),
    ):
        if value.get(key) is not None:
            value[key] = str(value[key])[:maximum]
    for key in ("actor_user_id", "guild_id", "channel_id", "message_id", "status_code"):
        raw = value.get(key)
        try:
            value[key] = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            value[key] = None
    if value.get("duration_ms") is not None:
        try:
            value["duration_ms"] = round(float(value["duration_ms"]), 3)
        except (TypeError, ValueError):
            value["duration_ms"] = None
    return value


def append_event(event: Mapping[str, Any]) -> dict[str, Any] | None:
    if not global_log_enabled():
        return None
    initialize_global_log()
    value = prepare_event(event)
    pool = _connection_pool()
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_CHAIN_LOCK_ID,))
        cursor.execute("SELECT event_hash FROM global_log_events ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        previous_hash = str(row[0]) if row else "0" * 64
        canonical = canonical_event_payload(value, previous_hash)
        event_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        cursor.execute("""
            INSERT INTO global_log_events (
                event_uuid, occurred_at, source_service, source_type, event_type,
                severity, actor_user_id, actor_display, guild_id, channel_id,
                message_id, request_id, session_id, target_type, target_id,
                summary, content_text, details, ip_hash, user_agent, status_code,
                duration_ms, previous_hash, event_hash
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s
            ) RETURNING id, ingested_at
        """, (
            value["event_uuid"], value["occurred_at"], value["source_service"],
            value["source_type"], value["event_type"], value["severity"],
            value.get("actor_user_id"), value.get("actor_display"), value.get("guild_id"),
            value.get("channel_id"), value.get("message_id"), value.get("request_id"),
            value.get("session_id"), value.get("target_type"), value.get("target_id"),
            value.get("summary") or "",
            value.get("content_text"),
            json.dumps(value.get("details") or {}, ensure_ascii=False, default=str),
            value.get("ip_hash"), value.get("user_agent") or None,
            value.get("status_code"), value.get("duration_ms"), previous_hash, event_hash,
        ))
        inserted = cursor.fetchone()
        connection.commit()
    return {"id": int(inserted[0]), "event_hash": event_hash, "ingested_at": inserted[1].isoformat()}


def issue_login_code(user_id: int, *, requested_from: str = "discord") -> str:
    initialize_global_log()
    if not user_is_allowed(user_id):
        raise PermissionError("global_log_access_denied")
    digits = max(8, min(12, int(os.getenv("GLOBAL_LOG_CODE_DIGITS", "10") or 10)))
    code = "".join(secrets.choice("0123456789") for _ in range(digits))
    ttl = max(60, min(900, int(os.getenv("GLOBAL_LOG_CODE_TTL_SECONDS", "300") or 300)))
    pool = _connection_pool()
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE global_log_login_codes SET used_at = NOW() "
            "WHERE user_id = %s AND used_at IS NULL",
            (int(user_id),),
        )
        cursor.execute("""
            INSERT INTO global_log_login_codes
                (user_id, code_hash, expires_at, requested_from)
            VALUES (%s, %s, NOW() + (%s * INTERVAL '1 second'), %s)
        """, (int(user_id), _secret_hash("code", f"{int(user_id)}:{code}"), ttl, requested_from[:120]))
        connection.commit()
    return code


def consume_login_code(
    user_id: int,
    code: str,
    *,
    ip_hash: str | None = None,
    user_agent: str | None = None,
) -> str | None:
    initialize_global_log()
    if not user_is_allowed(user_id) or not str(code).isdigit():
        return None
    pool = _connection_pool()
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_CHAIN_LOCK_ID + int(user_id) % 10_000,))
        cursor.execute("""
            SELECT id, code_hash, expires_at, attempts
            FROM global_log_login_codes
            WHERE user_id = %s AND used_at IS NULL
            ORDER BY id DESC LIMIT 1
            FOR UPDATE
        """, (int(user_id),))
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            "UPDATE global_log_login_codes SET attempts = attempts + 1 WHERE id = %s",
            (int(row[0]),),
        )
        valid = (
            row[2] > datetime.now(timezone.utc)
            and int(row[3]) < 5
            and hmac.compare_digest(str(row[1]), _secret_hash("code", f"{int(user_id)}:{code}"))
        )
        if not valid:
            connection.commit()
            return None
        cursor.execute("UPDATE global_log_login_codes SET used_at = NOW() WHERE id = %s", (int(row[0]),))
        token = secrets.token_urlsafe(48)
        lifetime = max(900, min(43_200, int(os.getenv("GLOBAL_LOG_SESSION_TTL_SECONDS", "7200") or 7200)))
        cursor.execute("""
            INSERT INTO global_log_sessions
                (user_id, token_hash, expires_at, ip_hash, user_agent)
            VALUES (%s, %s, NOW() + (%s * INTERVAL '1 second'), %s, %s)
        """, (int(user_id), _secret_hash("session", token), lifetime, ip_hash, str(user_agent or "")[:1000] or None))
        connection.commit()
    return token


def resolve_session(token: str) -> dict[str, Any] | None:
    if not token or not global_log_enabled():
        return None
    initialize_global_log()
    pool = _connection_pool()
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, user_id, created_at, expires_at, last_seen_at
            FROM global_log_sessions
            WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > NOW()
            LIMIT 1
        """, (_secret_hash("session", token),))
        row = cursor.fetchone()
        if row is None or not user_is_allowed(int(row[1])):
            return None
        cursor.execute(
            "UPDATE global_log_sessions SET last_seen_at = NOW() WHERE id = %s "
            "AND last_seen_at < NOW() - INTERVAL '60 seconds'",
            (int(row[0]),),
        )
        connection.commit()
    return {
        "session_id": int(row[0]), "user_id": int(row[1]),
        "created_at": row[2].isoformat(), "expires_at": row[3].isoformat(),
        "last_seen_at": row[4].isoformat(),
    }


def revoke_session(token: str) -> None:
    if not token or not global_log_enabled():
        return
    initialize_global_log()
    pool = _connection_pool()
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE global_log_sessions SET revoked_at = NOW() WHERE token_hash = %s",
            (_secret_hash("session", token),),
        )
        connection.commit()


def _event_dict(row: Any, names: list[str]) -> dict[str, Any]:
    value = {name: row[index] for index, name in enumerate(names)}
    for name in ("occurred_at", "ingested_at"):
        if value.get(name) is not None:
            value[name] = value[name].isoformat()
    return value


def search_events(filters: Mapping[str, Any]) -> dict[str, Any]:
    initialize_global_log()
    limit = max(1, min(200, int(filters.get("limit") or 50)))
    clauses: list[str] = []
    params: list[Any] = []
    cursor_id = str(filters.get("cursor") or "")
    if cursor_id.isdigit():
        clauses.append("id < %s")
        params.append(int(cursor_id))
    q = str(filters.get("q") or "").strip()[:300]
    if q:
        clauses.append("(to_tsvector('simple', coalesce(summary,'') || ' ' || coalesce(content_text,'') || ' ' || coalesce(actor_display,'') || ' ' || coalesce(target_id,'')) @@ plainto_tsquery('simple', %s) OR CAST(id AS TEXT) = %s OR CAST(actor_user_id AS TEXT) = %s OR CAST(message_id AS TEXT) = %s)")
        params.extend([q, q, q, q])
    for key, column in (
        ("source", "source_service"), ("source_type", "source_type"),
        ("event_type", "event_type"), ("severity", "severity"),
    ):
        value = str(filters.get(key) or "").strip()[:120]
        if value:
            clauses.append(f"{column} = %s")
            params.append(value)
    for key, column in (("actor", "actor_user_id"), ("channel", "channel_id"), ("status", "status_code")):
        value = str(filters.get(key) or "").strip()
        if value.lstrip("-").isdigit():
            clauses.append(f"{column} = %s")
            params.append(int(value))
    for key, operator in (("from", ">="), ("to", "<=")):
        value = str(filters.get(key) or "").strip()
        if value:
            clauses.append(f"occurred_at {operator} %s")
            params.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    names = [
        "id", "event_uuid", "occurred_at", "ingested_at", "source_service",
        "source_type", "event_type", "severity", "actor_user_id", "actor_display",
        "guild_id", "channel_id", "message_id", "request_id", "session_id",
        "target_type", "target_id", "summary", "content_text", "details",
        "ip_hash", "user_agent", "status_code", "duration_ms", "previous_hash", "event_hash",
    ]
    sql = "SELECT " + ", ".join(names) + " FROM global_log_events" + where + " ORDER BY id DESC LIMIT %s"
    params.append(limit + 1)
    pool = _connection_pool()
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
    has_more = len(rows) > limit
    selected = rows[:limit]
    return {
        "events": [_event_dict(row, names) for row in selected],
        "next_cursor": str(selected[-1][0]) if has_more and selected else None,
        "has_more": has_more,
    }


def facets() -> dict[str, Any]:
    initialize_global_log()
    pool = _connection_pool()
    result: dict[str, Any] = {}
    with pool.connection() as connection, connection.cursor() as cursor:
        for name, column in (
            ("sources", "source_service"), ("source_types", "source_type"),
            ("event_types", "event_type"), ("severities", "severity"),
        ):
            cursor.execute(
                f"SELECT {column}, COUNT(*) FROM global_log_events "
                f"GROUP BY {column} ORDER BY COUNT(*) DESC, {column} LIMIT 100"
            )
            result[name] = [{"value": row[0], "count": int(row[1])} for row in cursor.fetchall()]
        cursor.execute("SELECT COUNT(*), MIN(occurred_at), MAX(occurred_at) FROM global_log_events")
        row = cursor.fetchone()
        result["total"] = int(row[0])
        result["first_at"] = row[1].isoformat() if row[1] else None
        result["last_at"] = row[2].isoformat() if row[2] else None
    return result


def verify_chain(*, limit: int = 10_000) -> dict[str, Any]:
    initialize_global_log()
    pool = _connection_pool()
    names = [
        "event_uuid", "occurred_at", "source_service", "source_type", "event_type",
        "severity", "actor_user_id", "actor_display", "guild_id", "channel_id",
        "message_id", "request_id", "session_id", "target_type", "target_id",
        "summary", "content_text", "details", "ip_hash", "user_agent", "status_code",
        "duration_ms", "previous_hash", "event_hash", "id",
    ]
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT " + ", ".join(names) + " FROM global_log_events ORDER BY id ASC LIMIT %s",
            (max(1, min(100_000, int(limit))),),
        )
        rows = cursor.fetchall()
    previous = "0" * 64
    for row in rows:
        value = {name: row[index] for index, name in enumerate(names)}
        value["occurred_at"] = value["occurred_at"].isoformat()
        expected = hashlib.sha256(canonical_event_payload(value, previous).encode("utf-8")).hexdigest()
        if str(value["previous_hash"]) != previous or not hmac.compare_digest(str(value["event_hash"]), expected):
            return {"ok": False, "checked": int(value["id"]), "broken_at": int(value["id"])}
        previous = str(value["event_hash"])
    return {"ok": True, "checked": len(rows), "last_hash": previous if rows else None}


__all__ = [
    "allowed_user_ids", "append_event", "canonical_event_payload", "consume_login_code",
    "facets", "global_log_database_name", "global_log_enabled", "initialize_global_log",
    "issue_login_code", "prepare_event", "resolve_session", "revoke_session", "search_events",
    "user_is_allowed", "verify_chain",
]
