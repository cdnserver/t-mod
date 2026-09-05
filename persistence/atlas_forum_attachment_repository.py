"""Review-gated forum attachment evidence for Atlas.

Forum pages are sources of record; a screenshot or scanned PDF is not.  This
repository keeps the original attachment link, exact source revision and OCR
state separately so a transcription cannot silently become a law, rule or
judicial act.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_MEDIA_KINDS = frozenset({"image", "file"})
_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
)
_NON_CONTENT_IMAGE_PATHS = ("/styles/", "/avatars/", "/smilies/", "/reactions/")
_TERMINAL_STATUSES = frozenset({"review_pending", "approved", "rejected", "unavailable", "archived"})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decoded(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row(row: Any) -> dict[str, Any]:
    return dict(row)


def _clean_filename(value: Any) -> str:
    filename = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(char for char in filename if char >= " " and char != "\x7f")
    return filename[:240] or "forum-attachment"


def _clean_attachment(
    item: Mapping[str, Any],
    *,
    source_url: str,
) -> dict[str, str | None]:
    raw_url = str(item.get("url") or "").strip()
    parsed_source = urlsplit(str(source_url or ""))
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.netloc.casefold() != parsed_source.netloc.casefold()
    ):
        raise ValueError("atlas_forum_attachment_url_invalid")
    path = str(parsed.path or "")
    lowered_path = path.casefold()
    is_attachment = "/attachments/" in lowered_path or "/data/attachments/" in lowered_path
    is_content_image = (
        str(item.get("media_kind") or "").strip().lower() == "image"
        and any(lowered_path.endswith(suffix) for suffix in _IMAGE_SUFFIXES)
        and not any(marker in lowered_path for marker in _NON_CONTENT_IMAGE_PATHS)
    )
    if not is_attachment and not is_content_image:
        raise ValueError("atlas_forum_attachment_url_invalid")
    url = urlunsplit(("https", parsed.netloc.casefold(), path, "", ""))
    media_kind = str(item.get("media_kind") or "file").strip().lower()
    if media_kind not in _MEDIA_KINDS:
        media_kind = "file"
    label = " ".join(str(item.get("label") or "").split())[:180] or None
    return {
        "url": url,
        "filename": _clean_filename(item.get("filename") or path),
        "media_kind": media_kind,
        "label": label,
    }


def _attachment_row(con: sqlite3.Connection, attachment_id: int) -> Any:
    return con.execute(
        "SELECT * FROM atlas_forum_attachments WHERE id = ?",
        (int(attachment_id),),
    ).fetchone()


def atlas_sync_forum_attachments(
    organization_id: int,
    source_id: int,
    attachments: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> list[dict[str, Any]]:
    """Persist an authoritative, bounded attachment inventory for one topic.

    The caller is expected to pass links extracted from the first forum post.
    We validate the host/path once more here because storage is the final trust
    boundary. Existing OCR is reset only when the parent source revision
    changes, never merely because a title/label changed.
    """

    raw_items = list(attachments or ())[:512]
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        source = con.execute(
            """
            SELECT id, organization_id, source_kind, source_url, checksum
            FROM atlas_knowledge_sources WHERE id = ?
            """,
            (int(source_id),),
        ).fetchone()
        if source is None or int(source["organization_id"]) != int(organization_id):
            raise ValueError("atlas_forum_attachment_source_missing")
        if str(source["source_kind"] or "") != "forum" or not str(source["source_url"] or ""):
            raise ValueError("atlas_forum_attachment_source_invalid")
        inventory: list[dict[str, str | None]] = []
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            clean = _clean_attachment(item, source_url=str(source["source_url"]))
            if str(clean["url"]) in seen:
                continue
            seen.add(str(clean["url"]))
            inventory.append(clean)

        stale_derivatives = con.execute(
            """
            SELECT knowledge_source_id FROM atlas_forum_attachments
            WHERE source_id = ? AND source_checksum != ?
              AND knowledge_source_id IS NOT NULL
            """,
            (int(source_id), str(source["checksum"] or "")),
        ).fetchall()
        for stale in stale_derivatives:
            con.execute(
                """
                UPDATE atlas_knowledge_sources
                SET status = 'archived', qdrant_point_id = NULL,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, int(stale["knowledge_source_id"])),
            )

        rows: list[dict[str, Any]] = []
        source_checksum = str(source["checksum"] or "")
        for item in inventory:
            con.execute(
                """
                INSERT INTO atlas_forum_attachments(
                    organization_id, source_id, attachment_url, filename,
                    media_kind, label, source_checksum, first_seen_at,
                    last_seen_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, attachment_url) DO UPDATE SET
                    filename = excluded.filename,
                    media_kind = excluded.media_kind,
                    label = excluded.label,
                    last_seen_at = excluded.last_seen_at,
                    status = CASE
                        WHEN atlas_forum_attachments.status = 'archived' THEN 'discovered'
                        WHEN atlas_forum_attachments.source_checksum != excluded.source_checksum
                            THEN 'discovered'
                        ELSE atlas_forum_attachments.status
                    END,
                    content_sha256 = CASE
                        WHEN atlas_forum_attachments.source_checksum != excluded.source_checksum
                            THEN NULL ELSE atlas_forum_attachments.content_sha256 END,
                    storage_key = CASE
                        WHEN atlas_forum_attachments.source_checksum != excluded.source_checksum
                            THEN NULL ELSE atlas_forum_attachments.storage_key END,
                    knowledge_source_id = CASE
                        WHEN atlas_forum_attachments.source_checksum != excluded.source_checksum
                            THEN NULL ELSE atlas_forum_attachments.knowledge_source_id END,
                    mime_type = CASE
                        WHEN atlas_forum_attachments.source_checksum != excluded.source_checksum
                            THEN NULL ELSE atlas_forum_attachments.mime_type END,
                    size_bytes = CASE
                        WHEN atlas_forum_attachments.source_checksum != excluded.source_checksum
                            THEN NULL ELSE atlas_forum_attachments.size_bytes END,
                    ocr_text = CASE
                        WHEN atlas_forum_attachments.source_checksum != excluded.source_checksum
                            THEN NULL ELSE atlas_forum_attachments.ocr_text END,
                    ocr_engine = CASE
                        WHEN atlas_forum_attachments.source_checksum != excluded.source_checksum
                            THEN NULL ELSE atlas_forum_attachments.ocr_engine END,
                    ocr_error = CASE
                        WHEN atlas_forum_attachments.source_checksum != excluded.source_checksum
                            THEN NULL ELSE atlas_forum_attachments.ocr_error END,
                    reviewed_by_id = CASE
                        WHEN atlas_forum_attachments.source_checksum != excluded.source_checksum
                            THEN NULL ELSE atlas_forum_attachments.reviewed_by_id END,
                    reviewed_at = CASE
                        WHEN atlas_forum_attachments.source_checksum != excluded.source_checksum
                            THEN NULL ELSE atlas_forum_attachments.reviewed_at END,
                    source_checksum = excluded.source_checksum,
                    updated_at = excluded.updated_at
                """,
                (
                    int(organization_id),
                    int(source_id),
                    str(item["url"]),
                    str(item["filename"]),
                    str(item["media_kind"]),
                    item["label"],
                    source_checksum,
                    now,
                    now,
                    now,
                    now,
                ),
            )
            row = con.execute(
                """
                SELECT * FROM atlas_forum_attachments
                WHERE source_id = ? AND attachment_url = ?
                """,
                (int(source_id), str(item["url"])),
            ).fetchone()
            if row is not None:
                rows.append(_row(row))
        con.commit()
    return rows


def atlas_reconcile_forum_attachment_inventory(*, limit: int = 250) -> int:
    """Backfill durable inventories from pre-existing source metadata safely."""

    with connect_readonly() as con:
        source_rows = con.execute(
            """
            SELECT id, organization_id, metadata_json FROM atlas_knowledge_sources
            WHERE source_kind = 'forum' AND status != 'archived'
            ORDER BY updated_at DESC, id DESC LIMIT ?
            """,
            (max(1, min(2_000, int(limit))),),
        ).fetchall()
    saved = 0
    for source in source_rows:
        metadata = _decoded(source["metadata_json"])
        raw = metadata.get("forum_attachments")
        if not isinstance(raw, list) or not raw:
            continue
        rows = atlas_sync_forum_attachments(
            int(source["organization_id"]),
            int(source["id"]),
            tuple(item for item in raw if isinstance(item, Mapping)),
        )
        saved += len(rows)
    return saved


def atlas_forum_attachment_pending(*, limit: int = 40) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT a.*, s.source_url, s.title AS source_title
            FROM atlas_forum_attachments a
            JOIN atlas_knowledge_sources s ON s.id = a.source_id
            WHERE a.status = 'discovered' AND s.status != 'archived'
            ORDER BY a.updated_at ASC, a.id ASC LIMIT ?
            """,
            (max(1, min(200, int(limit))),),
        ).fetchall()
    return [_row(row) for row in rows]


def atlas_forum_attachment_mark_queued(attachment_id: int) -> dict[str, Any] | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            UPDATE atlas_forum_attachments
            SET status = 'queued', ocr_error = NULL, updated_at = ?
            WHERE id = ? AND status = 'discovered'
            """,
            (now, int(attachment_id)),
        )
        row = _attachment_row(con, int(attachment_id))
        con.commit()
    return _row(row) if row is not None else None


def atlas_forum_attachment_begin_processing(attachment_id: int) -> dict[str, Any] | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = _attachment_row(con, int(attachment_id))
        if row is None:
            con.commit()
            return None
        if str(row["status"]) not in _TERMINAL_STATUSES:
            con.execute(
                """
                UPDATE atlas_forum_attachments
                SET status = 'processing', ocr_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, int(attachment_id)),
            )
            row = _attachment_row(con, int(attachment_id))
        con.commit()
    return _row(row) if row is not None else None


def atlas_forum_attachment_complete_ocr(
    attachment_id: int,
    *,
    content_sha256: str,
    mime_type: str,
    size_bytes: int,
    storage_key: str,
    text: str,
    engine: str,
) -> dict[str, Any]:
    checksum = str(content_sha256 or "").strip().lower()
    clean_text = str(text or "").strip()[:120_000]
    clean_storage_key = str(storage_key or "").strip()[:500]
    if (
        not _SHA256_RE.fullmatch(checksum)
        or not clean_storage_key
        or len(clean_text) < 20
        or int(size_bytes) <= 0
    ):
        raise ValueError("atlas_forum_attachment_ocr_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE atlas_forum_attachments
            SET status = 'review_pending', content_sha256 = ?, storage_key = ?, mime_type = ?, size_bytes = ?,
                ocr_text = ?, ocr_engine = ?, ocr_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (
                checksum,
                clean_storage_key,
                str(mime_type or "")[:160],
                int(size_bytes),
                clean_text,
                str(engine or "")[:120],
                now,
                int(attachment_id),
            ),
        )
        row = _attachment_row(con, int(attachment_id))
        con.commit()
    if row is None:
        raise ValueError("atlas_forum_attachment_missing")
    return _row(row)


def atlas_forum_attachment_mark_unavailable(
    attachment_id: int,
    *,
    error: str,
    content_sha256: str | None = None,
    storage_key: str | None = None,
    mime_type: str | None = None,
    size_bytes: int | None = None,
) -> dict[str, Any] | None:
    checksum = str(content_sha256 or "").strip().lower()
    if checksum and not _SHA256_RE.fullmatch(checksum):
        raise ValueError("atlas_forum_attachment_checksum_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE atlas_forum_attachments
            SET status = 'unavailable', content_sha256 = COALESCE(?, content_sha256),
                storage_key = COALESCE(?, storage_key),
                mime_type = COALESCE(?, mime_type), size_bytes = COALESCE(?, size_bytes),
                ocr_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                checksum or None,
                str(storage_key or "").strip()[:500] or None,
                str(mime_type or "")[:160] or None,
                int(size_bytes) if size_bytes is not None else None,
                str(error or "")[:2000],
                now,
                int(attachment_id),
            ),
        )
        row = _attachment_row(con, int(attachment_id))
        con.commit()
    return _row(row) if row is not None else None


def atlas_forum_attachment_mark_error(attachment_id: int, *, error: str) -> dict[str, Any] | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE atlas_forum_attachments
            SET status = 'failed', ocr_error = ?, updated_at = ?
            WHERE id = ? AND status NOT IN ('approved', 'rejected', 'archived')
            """,
            (str(error or "")[:2000], now, int(attachment_id)),
        )
        row = _attachment_row(con, int(attachment_id))
        con.commit()
    return _row(row) if row is not None else None


def atlas_forum_attachment_review(
    organization_id: int,
    user_id: int,
    attachment_id: int,
    *,
    approve: bool,
) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        membership = con.execute(
            """
            SELECT role FROM atlas_memberships
            WHERE organization_id = ? AND user_id = ? AND status = 'active'
            """,
            (int(organization_id), int(user_id)),
        ).fetchone()
        if membership is None or str(membership["role"]) not in {"owner", "administrator", "editor"}:
            raise ValueError("atlas_forum_attachment_forbidden")
        attachment = _attachment_row(con, int(attachment_id))
        if attachment is None or int(attachment["organization_id"]) != int(organization_id):
            raise ValueError("atlas_forum_attachment_missing")
        resume_approval = (
            bool(approve)
            and str(attachment["status"]) == "approved"
            and attachment["knowledge_source_id"] is None
        )
        if str(attachment["status"]) != "review_pending" and not resume_approval:
            raise ValueError("atlas_forum_attachment_review_invalid")
        status = "approved" if approve else "rejected"
        if not resume_approval:
            con.execute(
                """
                UPDATE atlas_forum_attachments
                SET status = ?, reviewed_by_id = ?, reviewed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, int(user_id), now, now, int(attachment_id)),
            )
            con.execute(
                """
                INSERT INTO atlas_audit_events(
                    organization_id, actor_user_id, event_type, target_type,
                    target_id, summary, details_json, created_at
                ) VALUES(?, ?, 'forum_attachment_reviewed', 'forum_attachment', ?, ?, ?, ?)
                """,
                (
                    int(organization_id),
                    int(user_id),
                    str(attachment_id),
                    "Подтверждён OCR вложения форума" if approve else "Отклонён OCR вложения форума",
                    _json({"approved": bool(approve), "source_id": int(attachment["source_id"])}),
                    now,
                ),
            )
        row = _attachment_row(con, int(attachment_id))
        con.commit()
    reviewed = _row(row)
    if approve:
        # A reviewed transcription becomes its own citable knowledge source.
        # The canonical parent post remains untouched and the derivative can
        # be archived independently when the forum revision changes.
        from persistence import atlas_repository as atlas_storage

        parent = atlas_storage.atlas_knowledge_source(int(reviewed["source_id"]))
        if parent is None:
            raise ValueError("atlas_forum_attachment_source_missing")
        metadata = parent.get("metadata") if isinstance(parent.get("metadata"), dict) else {}
        taxonomy = metadata.get("taxonomy") if isinstance(metadata.get("taxonomy"), dict) else {}
        label = str(reviewed.get("label") or reviewed.get("filename") or "изображение").strip()
        derivative = atlas_storage.atlas_upsert_synced_knowledge(
            int(organization_id),
            title=f"{str(parent.get('title') or 'Материал форума')} — {label}"[:180],
            content=(
                "Подтверждённая расшифровка изображения из материала форума.\n"
                f"Оригинал: {str(reviewed.get('attachment_url') or '')}\n\n"
                f"{str(reviewed.get('ocr_text') or '')}"
            ),
            source_url=str(reviewed.get("attachment_url") or ""),
            server_code=str(parent.get("server_code") or "phoenix-15"),
            faction_code=str(parent.get("faction_code") or "lspd"),
            visibility_scope=str(parent.get("visibility_scope") or "server"),
            federation_scope=str(parent.get("federation_scope") or "server"),
            knowledge_domain=str(taxonomy.get("domain") or "") or None,
            corpus_kind=str(taxonomy.get("corpus_kind") or "") or None,
            feed_key=f"forum-ocr:{int(parent['id'])}",
            metadata={
                "derived_from_source_id": int(parent["id"]),
                "forum_attachment_id": int(attachment_id),
                "ocr_verified": True,
                "ocr_engine": str(reviewed.get("ocr_engine") or ""),
                "content_sha256": str(reviewed.get("content_sha256") or ""),
                "original_storage_key": str(reviewed.get("storage_key") or ""),
                "reviewed_by_id": int(user_id),
                "reviewed_at": str(reviewed.get("reviewed_at") or now),
            },
        )["source"]
        with _db_lock, connect() as con:
            con.execute(
                "UPDATE atlas_forum_attachments SET knowledge_source_id = ?, updated_at = ? WHERE id = ?",
                (int(derivative["id"]), now, int(attachment_id)),
            )
            row = _attachment_row(con, int(attachment_id))
            con.commit()
        reviewed = _row(row)
    return reviewed


def atlas_forum_attachments(
    organization_id: int,
    *,
    source_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses = ["a.organization_id = ?"]
    params: list[Any] = [int(organization_id)]
    if source_id is not None:
        clauses.append("a.source_id = ?")
        params.append(int(source_id))
    if status:
        clauses.append("a.status = ?")
        params.append(str(status).strip().lower())
    params.append(max(1, min(500, int(limit))))
    with connect_readonly() as con:
        rows = con.execute(
            f"""
            SELECT a.*, s.title AS source_title, s.source_url
            FROM atlas_forum_attachments a
            JOIN atlas_knowledge_sources s ON s.id = a.source_id
            WHERE {' AND '.join(clauses)}
            ORDER BY a.updated_at DESC, a.id DESC LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_row(row) for row in rows]


__all__ = [name for name in globals() if name.startswith("atlas_forum_attachment") or name == "atlas_sync_forum_attachments" or name == "atlas_reconcile_forum_attachment_inventory"]
