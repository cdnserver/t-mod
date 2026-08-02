from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from persistence.core import _db_lock, connect, utc_now_iso


def _row_dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def record_runtime_error(
    *,
    fingerprint: str,
    title: str,
    component: str,
    level: str,
    exception_type: str | None,
    details: str,
    traceback_text: str | None,
    environment: str,
    release: str,
    now: str | None = None,
) -> dict[str, Any]:
    observed_at = str(now or utc_now_iso())
    with _db_lock, connect() as con:
        con.execute(
            """
            INSERT INTO runtime_error_inbox(
                fingerprint, title, component, level, exception_type,
                details, traceback_text, environment, release,
                first_seen_at, last_seen_at, occurrences, pending,
                attempt_count, next_attempt_at, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 0, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                title = excluded.title,
                component = excluded.component,
                level = excluded.level,
                exception_type = excluded.exception_type,
                details = excluded.details,
                traceback_text = excluded.traceback_text,
                environment = excluded.environment,
                release = excluded.release,
                last_seen_at = excluded.last_seen_at,
                occurrences = runtime_error_inbox.occurrences + 1,
                pending = 1,
                next_attempt_at = CASE
                    WHEN runtime_error_inbox.pending = 0 THEN excluded.next_attempt_at
                    ELSE runtime_error_inbox.next_attempt_at
                END,
                updated_at = excluded.updated_at
            """,
            (
                str(fingerprint),
                str(title),
                str(component),
                str(level),
                str(exception_type) if exception_type else None,
                str(details),
                str(traceback_text) if traceback_text else None,
                str(environment),
                str(release),
                observed_at,
                observed_at,
                observed_at,
                observed_at,
                observed_at,
            ),
        )
        row = con.execute(
            "SELECT * FROM runtime_error_inbox WHERE fingerprint = ?",
            (str(fingerprint),),
        ).fetchone()
        con.commit()
    result = _row_dict(row)
    if result is None:
        raise RuntimeError("runtime_error_record_missing")
    return result


def list_runtime_errors_ready(
    *,
    limit: int = 10,
    comment_cooldown_seconds: int = 1800,
    now: str | None = None,
) -> list[dict[str, Any]]:
    checked_at = str(now or utc_now_iso())
    checked_dt = datetime.fromisoformat(checked_at)
    if checked_dt.tzinfo is None:
        checked_dt = checked_dt.replace(tzinfo=timezone.utc)
    published_cutoff = (checked_dt - timedelta(seconds=max(0, int(comment_cooldown_seconds)))).isoformat()
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM runtime_error_inbox
            WHERE pending = 1
              AND next_attempt_at <= ?
              AND (last_published_at IS NULL OR last_published_at <= ?)
            ORDER BY
                CASE WHEN github_issue_number IS NULL THEN 0 ELSE 1 END,
                last_seen_at ASC
            LIMIT ?
            """,
            (checked_at, published_cutoff, max(1, min(int(limit), 100))),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_runtime_error_published(
    fingerprint: str,
    *,
    published_occurrences: int,
    github_issue_number: int,
    github_issue_url: str,
    now: str | None = None,
) -> dict[str, Any] | None:
    published_at = str(now or utc_now_iso())
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE runtime_error_inbox
            SET pending = CASE WHEN occurrences > ? THEN 1 ELSE 0 END,
                attempt_count = 0,
                next_attempt_at = ?,
                last_error = NULL,
                github_issue_number = ?,
                github_issue_url = ?,
                last_published_at = ?,
                updated_at = ?
            WHERE fingerprint = ?
            """,
            (
                int(published_occurrences),
                published_at,
                int(github_issue_number),
                str(github_issue_url),
                published_at,
                published_at,
                str(fingerprint),
            ),
        )
        row = con.execute(
            "SELECT * FROM runtime_error_inbox WHERE fingerprint = ?",
            (str(fingerprint),),
        ).fetchone()
        con.commit()
    return _row_dict(row)


def mark_runtime_error_failed(
    fingerprint: str,
    *,
    error: str,
    now: str | None = None,
) -> dict[str, Any] | None:
    failed_at = str(now or utc_now_iso())
    failed_dt = datetime.fromisoformat(failed_at)
    if failed_dt.tzinfo is None:
        failed_dt = failed_dt.replace(tzinfo=timezone.utc)
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT attempt_count FROM runtime_error_inbox WHERE fingerprint = ?",
            (str(fingerprint),),
        ).fetchone()
        if row is None:
            return None
        attempts = int(row["attempt_count"] or 0) + 1
        delay_seconds = min(3600, 5 * (2 ** min(attempts - 1, 10)))
        retry_at = (failed_dt + timedelta(seconds=delay_seconds)).isoformat()
        con.execute(
            """
            UPDATE runtime_error_inbox
            SET pending = 1,
                attempt_count = ?,
                next_attempt_at = ?,
                last_error = ?,
                updated_at = ?
            WHERE fingerprint = ?
            """,
            (attempts, retry_at, str(error)[:2000], failed_at, str(fingerprint)),
        )
        current = con.execute(
            "SELECT * FROM runtime_error_inbox WHERE fingerprint = ?",
            (str(fingerprint),),
        ).fetchone()
        con.commit()
    return _row_dict(current)


def runtime_error_inbox_summary() -> dict[str, int]:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN pending = 1 THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN github_issue_number IS NOT NULL THEN 1 ELSE 0 END) AS published
            FROM runtime_error_inbox
            """
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "pending": int(row["pending"] or 0),
        "published": int(row["published"] or 0),
    }


def prune_runtime_error_inbox(*, retention_days: int = 90, now: str | None = None) -> int:
    checked_at = str(now or utc_now_iso())
    checked_dt = datetime.fromisoformat(checked_at)
    if checked_dt.tzinfo is None:
        checked_dt = checked_dt.replace(tzinfo=timezone.utc)
    cutoff = (checked_dt - timedelta(days=max(7, int(retention_days)))).isoformat()
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            DELETE FROM runtime_error_inbox
            WHERE pending = 0
              AND last_seen_at < ?
            """,
            (cutoff,),
        )
        con.commit()
        return max(0, int(cursor.rowcount or 0))


__all__ = [
    "list_runtime_errors_ready",
    "mark_runtime_error_failed",
    "mark_runtime_error_published",
    "prune_runtime_error_inbox",
    "record_runtime_error",
    "runtime_error_inbox_summary",
]
