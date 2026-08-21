"""Durable, leased background jobs for Atlas product workflows."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


ATLAS_JOB_OPEN_STATUSES = frozenset({"pending", "running", "retry"})
ATLAS_JOB_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_JOB_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decoded(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _job(row: Any) -> dict[str, Any]:
    item = dict(row)
    for name in ("payload_json", "progress_json", "result_json"):
        item[name.removesuffix("_json")] = _decoded(item.pop(name, "{}"))
    return item


def _utc(value: str | None = None) -> datetime:
    parsed = datetime.fromisoformat(str(value or utc_now_iso()).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_job_identity(job_type: str, dedupe_key: str) -> tuple[str, str]:
    clean_type = str(job_type or "").strip().lower()
    clean_key = str(dedupe_key or "").strip()[:240]
    if not _JOB_TYPE_RE.fullmatch(clean_type):
        raise ValueError("atlas_job_type_invalid")
    if not clean_key:
        raise ValueError("atlas_job_dedupe_key_required")
    return clean_type, clean_key


def atlas_job_enqueue_in_connection(
    con: sqlite3.Connection,
    *,
    organization_id: int,
    created_by_id: int,
    job_type: str,
    dedupe_key: str,
    payload: dict[str, Any] | None = None,
    subject_type: str | None = None,
    subject_id: str | int | None = None,
    max_attempts: int = 5,
    available_at: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    clean_type, clean_key = _validate_job_identity(job_type, dedupe_key)
    clean_subject_type = str(subject_type or "").strip().lower()[:80] or None
    clean_subject_id = str(subject_id or "").strip()[:160] or None
    if bool(clean_subject_type) != bool(clean_subject_id):
        raise ValueError("atlas_job_subject_incomplete")
    created_at = str(now or utc_now_iso())
    ready_at = str(available_at or created_at)
    existing = con.execute(
        """
        SELECT * FROM atlas_jobs
        WHERE organization_id = ? AND job_type = ? AND dedupe_key = ?
        """,
        (int(organization_id), clean_type, clean_key),
    ).fetchone()
    if existing is not None:
        return _job(existing)
    con.execute(
        """
        INSERT INTO atlas_jobs(
            organization_id, created_by_id, job_type, dedupe_key,
            subject_type, subject_id, payload_json, max_attempts,
            available_at, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(organization_id, job_type, dedupe_key) DO NOTHING
        """,
        (
            int(organization_id), int(created_by_id), clean_type, clean_key,
            clean_subject_type, clean_subject_id, _json(dict(payload or {})),
            max(1, min(20, int(max_attempts))), ready_at, created_at, created_at,
        ),
    )
    row = con.execute(
        """
        SELECT * FROM atlas_jobs
        WHERE organization_id = ? AND job_type = ? AND dedupe_key = ?
        """,
        (int(organization_id), clean_type, clean_key),
    ).fetchone()
    if row is None:  # pragma: no cover - protected by insert/select transaction
        raise RuntimeError("atlas_job_enqueue_failed")
    return _job(row)


def atlas_job_enqueue(
    organization_id: int,
    created_by_id: int,
    *,
    job_type: str,
    dedupe_key: str,
    payload: dict[str, Any] | None = None,
    subject_type: str | None = None,
    subject_id: str | int | None = None,
    max_attempts: int = 5,
    available_at: str | None = None,
) -> dict[str, Any]:
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        organization = con.execute(
            "SELECT 1 FROM atlas_organizations WHERE id = ? AND status = 'active'",
            (int(organization_id),),
        ).fetchone()
        if organization is None:
            raise ValueError("atlas_job_organization_missing")
        if int(created_by_id) > 0:
            membership = con.execute(
                """
                SELECT role FROM atlas_memberships
                WHERE organization_id = ? AND user_id = ? AND status = 'active'
                """,
                (int(organization_id), int(created_by_id)),
            ).fetchone()
            if membership is None or str(membership["role"]) == "viewer":
                raise ValueError("atlas_job_forbidden")
        row = atlas_job_enqueue_in_connection(
            con,
            organization_id=int(organization_id),
            created_by_id=int(created_by_id),
            job_type=job_type,
            dedupe_key=dedupe_key,
            payload=payload,
            subject_type=subject_type,
            subject_id=subject_id,
            max_attempts=max_attempts,
            available_at=available_at,
        )
        con.commit()
    return row


def atlas_job_claim(
    *,
    worker_id: str,
    job_types: tuple[str, ...] | list[str] | None = None,
    limit: int = 10,
    lease_seconds: int = 120,
    now: str | None = None,
) -> list[dict[str, Any]]:
    clean_worker = str(worker_id or "").strip()[:120]
    if not clean_worker:
        raise ValueError("atlas_job_worker_required")
    selected_types = tuple(
        _validate_job_identity(item, "claim")[0] for item in (job_types or ())
    )
    moment = _utc(now)
    now_iso = moment.isoformat()
    lease_until = (moment + timedelta(seconds=max(10, int(lease_seconds)))).isoformat()
    claimed: list[dict[str, Any]] = []
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            UPDATE atlas_jobs
            SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'retry' END,
                lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                last_error = COALESCE(last_error, 'atlas_job_lease_expired'),
                finished_at = CASE WHEN attempts >= max_attempts THEN ? ELSE NULL END,
                available_at = ?, updated_at = ?
            WHERE status = 'running' AND lease_until <= ?
            """,
            (now_iso, now_iso, now_iso, now_iso),
        )
        type_filter = ""
        params: list[Any] = [now_iso]
        if selected_types:
            type_filter = f"AND job_type IN ({','.join('?' for _ in selected_types)})"
            params.extend(selected_types)
        params.append(max(1, min(50, int(limit))))
        rows = con.execute(
            f"""
            SELECT id FROM atlas_jobs
            WHERE status IN ('pending', 'retry')
              AND available_at <= ? AND attempts < max_attempts
              {type_filter}
            ORDER BY available_at ASC, id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        for selected in rows:
            token = uuid.uuid4().hex
            cursor = con.execute(
                """
                UPDATE atlas_jobs
                SET status = 'running', attempts = attempts + 1,
                    lease_owner = ?, lease_token = ?, lease_until = ?,
                    started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE id = ? AND status IN ('pending', 'retry')
                """,
                (
                    clean_worker, token, lease_until, now_iso, now_iso,
                    int(selected["id"]),
                ),
            )
            if int(cursor.rowcount or 0) == 1:
                row = con.execute(
                    "SELECT * FROM atlas_jobs WHERE id = ?",
                    (int(selected["id"]),),
                ).fetchone()
                if row is not None:
                    claimed.append(_job(row))
        con.commit()
    return claimed


def atlas_job_renew(
    job_id: int,
    *,
    lease_token: str,
    lease_seconds: int = 120,
    now: str | None = None,
) -> bool:
    moment = _utc(now)
    now_iso = moment.isoformat()
    lease_until = (moment + timedelta(seconds=max(10, int(lease_seconds)))).isoformat()
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            UPDATE atlas_jobs SET lease_until = ?, updated_at = ?
            WHERE id = ? AND status = 'running' AND lease_token = ?
              AND lease_until > ?
            """,
            (lease_until, now_iso, int(job_id), str(lease_token), now_iso),
        )
        con.commit()
    return int(cursor.rowcount or 0) == 1


def atlas_job_progress(
    job_id: int,
    *,
    lease_token: str,
    progress: dict[str, Any],
    now: str | None = None,
) -> bool:
    now_iso = _utc(now).isoformat()
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            UPDATE atlas_jobs SET progress_json = ?, updated_at = ?
            WHERE id = ? AND status = 'running' AND lease_token = ?
              AND lease_until > ?
            """,
            (_json(dict(progress or {})), now_iso, int(job_id), str(lease_token), now_iso),
        )
        con.commit()
    return int(cursor.rowcount or 0) == 1


def atlas_job_succeed(
    job_id: int,
    *,
    lease_token: str,
    result: dict[str, Any] | None = None,
    now: str | None = None,
) -> bool:
    now_iso = _utc(now).isoformat()
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            UPDATE atlas_jobs
            SET status = 'succeeded', result_json = ?, progress_json = '{"percent":100}',
                lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                last_error = NULL, finished_at = ?, updated_at = ?
            WHERE id = ? AND status = 'running' AND lease_token = ?
              AND lease_until > ?
            """,
            (
                _json(dict(result or {})), now_iso, now_iso,
                int(job_id), str(lease_token), now_iso,
            ),
        )
        con.commit()
    return int(cursor.rowcount or 0) == 1


def atlas_job_fail(
    job_id: int,
    *,
    lease_token: str,
    error: str,
    retry_delay_seconds: int = 10,
    retryable: bool = True,
    now: str | None = None,
) -> str:
    moment = _utc(now)
    now_iso = moment.isoformat()
    available_at = (moment + timedelta(seconds=max(0, int(retry_delay_seconds)))).isoformat()
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT attempts, max_attempts FROM atlas_jobs
            WHERE id = ? AND status = 'running' AND lease_token = ?
            """,
            (int(job_id), str(lease_token)),
        ).fetchone()
        if row is None:
            return "stale"
        terminal = not retryable or int(row["attempts"]) >= int(row["max_attempts"])
        status = "failed" if terminal else "retry"
        con.execute(
            """
            UPDATE atlas_jobs
            SET status = ?, available_at = ?, lease_owner = NULL,
                lease_token = NULL, lease_until = NULL, last_error = ?,
                finished_at = CASE WHEN ? = 'failed' THEN ? ELSE NULL END,
                updated_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (
                status, available_at, str(error or "")[:4000], status,
                now_iso, now_iso, int(job_id),
            ),
        )
        con.commit()
    return status


def atlas_job_get(job_id: int) -> dict[str, Any] | None:
    with connect_readonly() as con:
        row = con.execute("SELECT * FROM atlas_jobs WHERE id = ?", (int(job_id),)).fetchone()
    return _job(row) if row is not None else None


def atlas_jobs(
    organization_id: int,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clean_status = str(status or "").strip().lower()
    if clean_status and clean_status not in ATLAS_JOB_OPEN_STATUSES | ATLAS_JOB_TERMINAL_STATUSES:
        raise ValueError("atlas_job_status_invalid")
    filter_sql = "AND status = ?" if clean_status else ""
    params: list[Any] = [int(organization_id)]
    if clean_status:
        params.append(clean_status)
    params.append(max(1, min(200, int(limit))))
    with connect_readonly() as con:
        rows = con.execute(
            f"""
            SELECT * FROM atlas_jobs
            WHERE organization_id = ? {filter_sql}
            ORDER BY id DESC LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_job(row) for row in rows]


__all__ = [name for name in globals() if name.startswith("atlas_") or name.startswith("ATLAS_JOB_")]
