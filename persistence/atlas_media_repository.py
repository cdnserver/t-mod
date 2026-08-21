"""Durable metadata and access rules for Atlas Files + Media."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


ATLAS_MEDIA_KINDS = frozenset({"file", "video", "audio", "image"})
ATLAS_MEDIA_VISIBILITY = frozenset({"private", "workspace"})
ATLAS_MEDIA_RETENTION = frozenset({"manual", "30d", "90d", "permanent"})
ATLAS_MEDIA_SOURCE_KINDS = frozenset({"upload", "desktop_capture", "rollback", "import"})
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decoded(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row(row: Any) -> dict[str, Any]:
    item = dict(row)
    if "metadata_json" in item:
        item["metadata"] = _decoded(item.pop("metadata_json", "{}"))
    return item


def _member_role(con: sqlite3.Connection, organization_id: int, user_id: int) -> str:
    row = con.execute(
        """
        SELECT role FROM atlas_memberships
        WHERE organization_id = ? AND user_id = ? AND status = 'active'
        """,
        (int(organization_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ValueError("atlas_media_forbidden")
    return str(row["role"])


def _clean_filename(value: str) -> str:
    name = str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(character for character in name if character >= " " and character != "\x7f")
    return name[:240] or "atlas-file"


def _retention_until(policy: str, now: datetime) -> str | None:
    if policy == "30d":
        return (now + timedelta(days=30)).isoformat()
    if policy == "90d":
        return (now + timedelta(days=90)).isoformat()
    return None


def atlas_media_begin_upload(
    organization_id: int,
    user_id: int,
    *,
    title: str,
    filename: str,
    size_bytes: int,
    declared_mime_type: str | None = None,
    media_kind: str = "file",
    visibility_scope: str = "private",
    source_kind: str = "upload",
    source_device_id: str | None = None,
    captured_at: str | None = None,
    retention_policy: str = "manual",
    expected_sha256: str | None = None,
    client_request_id: str,
    max_asset_bytes: int,
    quota_bytes: int,
    upload_ttl_seconds: int = 86_400,
) -> dict[str, Any]:
    clean_size = int(size_bytes)
    if clean_size <= 0 or clean_size > int(max_asset_bytes):
        raise ValueError("atlas_media_size_invalid")
    clean_title = str(title or "").strip()[:180] or _clean_filename(filename)
    clean_filename = _clean_filename(filename)
    clean_kind = str(media_kind or "file").strip().lower()
    clean_visibility = str(visibility_scope or "private").strip().lower()
    clean_source = str(source_kind or "upload").strip().lower()
    clean_retention = str(retention_policy or "manual").strip().lower()
    if clean_kind not in ATLAS_MEDIA_KINDS:
        raise ValueError("atlas_media_kind_invalid")
    if clean_visibility not in ATLAS_MEDIA_VISIBILITY:
        raise ValueError("atlas_media_visibility_invalid")
    if clean_source not in ATLAS_MEDIA_SOURCE_KINDS:
        raise ValueError("atlas_media_source_invalid")
    if clean_retention not in ATLAS_MEDIA_RETENTION:
        raise ValueError("atlas_media_retention_invalid")
    clean_hash = str(expected_sha256 or "").strip().lower() or None
    if clean_hash and not _SHA256_RE.fullmatch(clean_hash):
        raise ValueError("atlas_media_checksum_invalid")
    clean_request = str(client_request_id or "").strip()[:200]
    if not clean_request:
        raise ValueError("atlas_media_request_id_required")
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    upload_id = uuid.uuid4().hex
    temp_key = f"uploads/{int(organization_id)}/{upload_id}.part"
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        role = _member_role(con, int(organization_id), int(user_id))
        if role == "viewer":
            raise ValueError("atlas_media_forbidden")
        existing = con.execute(
            """
            SELECT u.*, a.title, a.original_filename, a.status AS asset_status
            FROM atlas_media_uploads u
            JOIN atlas_media_assets a ON a.id = u.asset_id
            WHERE u.organization_id = ? AND u.user_id = ? AND u.client_request_id = ?
            """,
            (int(organization_id), int(user_id), clean_request),
        ).fetchone()
        if existing is not None:
            con.commit()
            return _row(existing)
        active = con.execute(
            """
            SELECT COUNT(*) FROM atlas_media_uploads
            WHERE organization_id = ? AND user_id = ? AND status IN ('open', 'finalizing')
            """,
            (int(organization_id), int(user_id)),
        ).fetchone()[0]
        if int(active) >= 8:
            raise ValueError("atlas_media_upload_limit")
        usage = con.execute(
            """
            SELECT COALESCE(SUM(size_bytes), 0) FROM atlas_media_assets
            WHERE organization_id = ? AND status != 'deleted'
            """,
            (int(organization_id),),
        ).fetchone()[0]
        if int(usage or 0) + clean_size > int(quota_bytes):
            raise ValueError("atlas_media_quota_exceeded")
        con.execute(
            """
            INSERT INTO atlas_media_assets(
                organization_id, owner_user_id, title, media_kind, visibility_scope,
                source_kind, original_filename, declared_mime_type, size_bytes,
                captured_at, source_device_id, retention_policy, retain_until,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(organization_id), int(user_id), clean_title, clean_kind,
                clean_visibility, clean_source, clean_filename,
                str(declared_mime_type or "").strip().lower()[:160] or None,
                clean_size, str(captured_at or "").strip()[:64] or None,
                str(source_device_id or "").strip()[:160] or None,
                clean_retention, _retention_until(clean_retention, now_dt), now, now,
            ),
        )
        asset_id = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
        con.execute(
            """
            INSERT INTO atlas_media_uploads(
                upload_id, organization_id, asset_id, user_id, client_request_id,
                temp_storage_key, expected_size, expected_sha256, expires_at,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                upload_id, int(organization_id), asset_id, int(user_id), clean_request,
                temp_key, clean_size, clean_hash,
                (now_dt + timedelta(seconds=max(300, int(upload_ttl_seconds)))).isoformat(),
                now, now,
            ),
        )
        row = con.execute(
            """
            SELECT u.*, a.title, a.original_filename, a.status AS asset_status
            FROM atlas_media_uploads u JOIN atlas_media_assets a ON a.id = u.asset_id
            WHERE u.upload_id = ?
            """,
            (upload_id,),
        ).fetchone()
        con.commit()
    return _row(row)


def atlas_media_upload(upload_id: str) -> dict[str, Any] | None:
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT u.*, a.title, a.original_filename, a.declared_mime_type,
                   a.media_kind, a.visibility_scope, a.source_kind,
                   a.retention_policy, a.status AS asset_status
            FROM atlas_media_uploads u JOIN atlas_media_assets a ON a.id = u.asset_id
            WHERE u.upload_id = ?
            """,
            (str(upload_id),),
        ).fetchone()
    return _row(row) if row is not None else None


def atlas_media_upload_for_user(
    upload_id: str,
    organization_id: int,
    user_id: int,
) -> dict[str, Any] | None:
    item = atlas_media_upload(upload_id)
    if item is None:
        return None
    if int(item["organization_id"]) != int(organization_id) or int(item["user_id"]) != int(user_id):
        return None
    return item


def atlas_media_record_upload_progress(
    upload_id: str,
    *,
    expected_offset: int,
    received_size: int,
) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM atlas_media_uploads WHERE upload_id = ?",
            (str(upload_id),),
        ).fetchone()
        if row is None:
            raise ValueError("atlas_media_upload_missing")
        current = int(row["received_size"])
        proposed = int(received_size)
        if current == proposed:
            con.commit()
            return _row(row)
        if str(row["status"]) != "open" or current != int(expected_offset):
            raise ValueError("atlas_media_upload_offset_conflict")
        if proposed <= current or proposed > int(row["expected_size"]):
            raise ValueError("atlas_media_upload_size_conflict")
        con.execute(
            "UPDATE atlas_media_uploads SET received_size = ?, updated_at = ? WHERE upload_id = ?",
            (proposed, now, str(upload_id)),
        )
        updated = con.execute(
            "SELECT * FROM atlas_media_uploads WHERE upload_id = ?",
            (str(upload_id),),
        ).fetchone()
        con.commit()
    return _row(updated)


def atlas_media_prepare_finalize(
    upload_id: str,
    *,
    checksum_sha256: str,
    final_storage_key: str,
    detected_mime_type: str,
) -> dict[str, Any]:
    clean_hash = str(checksum_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(clean_hash):
        raise ValueError("atlas_media_checksum_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM atlas_media_uploads WHERE upload_id = ?",
            (str(upload_id),),
        ).fetchone()
        if row is None:
            raise ValueError("atlas_media_upload_missing")
        if str(row["status"]) == "completed":
            con.commit()
            return _row(row)
        if int(row["received_size"]) != int(row["expected_size"]):
            raise ValueError("atlas_media_upload_incomplete")
        con.execute(
            """
            UPDATE atlas_media_uploads
            SET status = 'finalizing', computed_sha256 = ?, final_storage_key = ?,
                detected_mime_type = ?, last_error = NULL, updated_at = ?
            WHERE upload_id = ?
            """,
            (
                clean_hash, str(final_storage_key), str(detected_mime_type)[:160],
                now, str(upload_id),
            ),
        )
        con.execute(
            "UPDATE atlas_media_assets SET status = 'processing', last_error = NULL, updated_at = ? WHERE id = ?",
            (now, int(row["asset_id"])),
        )
        updated = con.execute(
            "SELECT * FROM atlas_media_uploads WHERE upload_id = ?",
            (str(upload_id),),
        ).fetchone()
        con.commit()
    return _row(updated)


def atlas_media_complete_upload(
    upload_id: str,
    *,
    checksum_sha256: str,
    storage_key: str,
    mime_type: str,
    media_kind: str,
    scan_status: str,
) -> dict[str, Any]:
    if scan_status not in {"not_configured", "clean"}:
        raise ValueError("atlas_media_scan_rejected")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        upload = con.execute(
            "SELECT * FROM atlas_media_uploads WHERE upload_id = ?",
            (str(upload_id),),
        ).fetchone()
        if upload is None:
            raise ValueError("atlas_media_upload_missing")
        asset_id = int(upload["asset_id"])
        if str(upload["status"]) != "completed":
            con.execute(
                """
                INSERT INTO atlas_media_blobs(
                    checksum_sha256, storage_key, size_bytes, mime_type, scan_status, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(checksum_sha256) DO NOTHING
                """,
                (
                    str(checksum_sha256), str(storage_key), int(upload["expected_size"]),
                    str(mime_type), scan_status, now,
                ),
            )
            con.execute(
                """
                UPDATE atlas_media_assets
                SET status = 'ready', blob_checksum = ?, mime_type = ?, media_kind = ?,
                    last_error = NULL, version = version + 1,
                    ready_at = COALESCE(ready_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (str(checksum_sha256), str(mime_type), str(media_kind), now, now, asset_id),
            )
            con.execute(
                """
                UPDATE atlas_media_uploads
                SET status = 'completed', computed_sha256 = ?, final_storage_key = ?,
                    detected_mime_type = ?, last_error = NULL,
                    completed_at = COALESCE(completed_at, ?), updated_at = ?
                WHERE upload_id = ?
                """,
                (str(checksum_sha256), str(storage_key), str(mime_type), now, now, str(upload_id)),
            )
            asset = con.execute(
                "SELECT * FROM atlas_media_assets WHERE id = ?",
                (asset_id,),
            ).fetchone()
            con.execute(
                """
                INSERT INTO atlas_audit_events(
                    organization_id, actor_user_id, event_type, target_type,
                    target_id, summary, details_json, created_at
                ) VALUES(?, ?, 'media_ready', 'media_asset', ?, ?, ?, ?)
                """,
                (
                    int(asset["organization_id"]), int(asset["owner_user_id"]), str(asset_id),
                    f"Материал «{str(asset['title'])}» готов",
                    _json({"mime_type": mime_type, "size_bytes": int(asset["size_bytes"])}), now,
                ),
            )
            con.execute(
                """
                INSERT INTO atlas_timeline_events(
                    organization_id, actor_user_id, event_kind, title, summary,
                    status, importance, occurred_at, source_type, source_id,
                    dedupe_key, created_at, updated_at
                ) VALUES(?, ?, 'activity', ?, ?, 'resolved', 'routine', ?,
                         'media_asset', ?, ?, ?, ?)
                ON CONFLICT(organization_id, dedupe_key) DO NOTHING
                """,
                (
                    int(asset["organization_id"]), int(asset["owner_user_id"]),
                    f"Сохранён материал «{str(asset['title'])}»",
                    "Файл проверен, сохранён и доступен для связи с событиями Atlas.",
                    now, str(asset_id), f"media-ready:{asset_id}", now, now,
                ),
            )
        asset = con.execute(
            "SELECT * FROM atlas_media_assets WHERE id = ?",
            (asset_id,),
        ).fetchone()
        con.commit()
    return _row(asset)


def atlas_media_mark_upload_error(
    upload_id: str,
    *,
    error: str,
    terminal: bool,
) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT asset_id FROM atlas_media_uploads WHERE upload_id = ?",
            (str(upload_id),),
        ).fetchone()
        if row is None:
            return
        con.execute(
            """
            UPDATE atlas_media_uploads
            SET status = ?, last_error = ?, updated_at = ?
            WHERE upload_id = ? AND status != 'completed'
            """,
            ("failed" if terminal else "finalizing", str(error)[:2000], now, str(upload_id)),
        )
        con.execute(
            """
            UPDATE atlas_media_assets
            SET status = ?, last_error = ?, updated_at = ?
            WHERE id = ? AND status != 'ready'
            """,
            ("failed" if terminal else "processing", str(error)[:2000], now, int(row["asset_id"])),
        )
        con.commit()


def atlas_media_assets(
    organization_id: int,
    user_id: int,
    *,
    status: str | None = None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    clean_status = str(status or "").strip().lower()
    params: list[Any] = [int(organization_id), int(user_id)]
    status_filter = ""
    if clean_status:
        status_filter = "AND a.status = ?"
        params.append(clean_status)
    params.append(max(1, min(200, int(limit))))
    with connect_readonly() as con:
        _member_role(con, int(organization_id), int(user_id))
        rows = con.execute(
            f"""
            SELECT a.*, b.storage_key, b.scan_status
            FROM atlas_media_assets a
            LEFT JOIN atlas_media_blobs b ON b.checksum_sha256 = a.blob_checksum
            WHERE a.organization_id = ? AND a.status != 'deleted'
              AND (a.visibility_scope = 'workspace' OR a.owner_user_id = ?)
              {status_filter}
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_row(row) for row in rows]


def atlas_media_asset(
    organization_id: int,
    user_id: int,
    asset_id: int,
) -> dict[str, Any] | None:
    with connect_readonly() as con:
        _member_role(con, int(organization_id), int(user_id))
        row = con.execute(
            """
            SELECT a.*, b.storage_key, b.scan_status
            FROM atlas_media_assets a
            LEFT JOIN atlas_media_blobs b ON b.checksum_sha256 = a.blob_checksum
            WHERE a.id = ? AND a.organization_id = ? AND a.status != 'deleted'
              AND (a.visibility_scope = 'workspace' OR a.owner_user_id = ?)
            """,
            (int(asset_id), int(organization_id), int(user_id)),
        ).fetchone()
    return _row(row) if row is not None else None


def atlas_media_quota(organization_id: int) -> dict[str, int]:
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT COUNT(*) AS assets, COALESCE(SUM(size_bytes), 0) AS used_bytes
            FROM atlas_media_assets
            WHERE organization_id = ? AND status != 'deleted'
            """,
            (int(organization_id),),
        ).fetchone()
    return {"assets": int(row["assets"]), "used_bytes": int(row["used_bytes"])}


def atlas_media_create_segment(
    organization_id: int,
    user_id: int,
    asset_id: int,
    *,
    title: str,
    start_ms: int,
    end_ms: int,
    segment_kind: str = "clip",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_title = str(title or "").strip()[:180]
    clean_kind = str(segment_kind or "clip").strip().lower()
    if not clean_title or clean_kind not in {"clip", "evidence", "highlight"}:
        raise ValueError("atlas_media_segment_invalid")
    if int(start_ms) < 0 or int(end_ms) <= int(start_ms):
        raise ValueError("atlas_media_segment_range_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        _member_role(con, int(organization_id), int(user_id))
        asset = con.execute(
            """
            SELECT * FROM atlas_media_assets
            WHERE id = ? AND organization_id = ? AND status = 'ready'
              AND (visibility_scope = 'workspace' OR owner_user_id = ?)
            """,
            (int(asset_id), int(organization_id), int(user_id)),
        ).fetchone()
        if asset is None:
            raise ValueError("atlas_media_asset_not_found")
        if asset["duration_ms"] is not None and int(end_ms) > int(asset["duration_ms"]):
            raise ValueError("atlas_media_segment_range_invalid")
        con.execute(
            """
            INSERT INTO atlas_media_segments(
                organization_id, asset_id, created_by_id, title,
                start_ms, end_ms, segment_kind, metadata_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id, start_ms, end_ms, segment_kind) DO UPDATE SET
                title = excluded.title, metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                int(organization_id), int(asset_id), int(user_id), clean_title,
                int(start_ms), int(end_ms), clean_kind, _json(dict(metadata or {})), now, now,
            ),
        )
        row = con.execute(
            """
            SELECT * FROM atlas_media_segments
            WHERE asset_id = ? AND start_ms = ? AND end_ms = ? AND segment_kind = ?
            """,
            (int(asset_id), int(start_ms), int(end_ms), clean_kind),
        ).fetchone()
        con.commit()
    return _row(row)


def atlas_media_segments(organization_id: int, user_id: int, asset_id: int) -> list[dict[str, Any]]:
    if atlas_media_asset(organization_id, user_id, asset_id) is None:
        raise ValueError("atlas_media_asset_not_found")
    with connect_readonly() as con:
        rows = con.execute(
            "SELECT * FROM atlas_media_segments WHERE asset_id = ? ORDER BY start_ms, id",
            (int(asset_id),),
        ).fetchall()
    return [_row(row) for row in rows]


__all__ = [name for name in globals() if name.startswith("atlas_media_") or name.startswith("ATLAS_MEDIA_")]
