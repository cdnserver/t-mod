"""Permission-aware cross-module search for the Atlas workspace."""

from __future__ import annotations

import re
from typing import Any

from persistence import atlas_repository
from persistence.core import connect_readonly


_SPACE_RE = re.compile(r"\s+")


def _clean_query(value: str) -> str:
    return _SPACE_RE.sub(" ", str(value or "").strip())[:180]


def _snippet(text: Any, query: str, *, limit: int = 240) -> str:
    source = _SPACE_RE.sub(" ", str(text or "").strip())
    if not source:
        return ""
    position = source.casefold().find(query.casefold())
    start = max(0, position - 70) if position >= 0 else 0
    excerpt = source[start : start + limit].strip()
    return ("…" if start else "") + excerpt + ("…" if start + limit < len(source) else "")


def _score(title: str, body: str, query: str, updated_at: str) -> tuple[int, str]:
    clean_title = str(title or "").casefold()
    clean_body = str(body or "").casefold()
    target = query.casefold()
    value = 0
    if clean_title == target:
        value += 120
    elif clean_title.startswith(target):
        value += 85
    elif target in clean_title:
        value += 60
    if target in clean_body:
        value += 20
    for token in target.split():
        value += 8 if token in clean_title else 2 if token in clean_body else 0
    return value, str(updated_at or "")


def atlas_global_search(
    organization_id: int,
    user_id: int,
    *,
    query: str,
    server_code: str = "phoenix-15",
    faction_code: str = "lspd",
    kinds: list[str] | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    clean_query = _clean_query(query)
    if len(clean_query) < 2:
        raise ValueError("atlas_search_query_too_short")
    allowed = set(kinds or {"case", "document", "timeline_event", "media_asset", "knowledge_source"})
    allowed &= {"case", "document", "timeline_event", "media_asset", "knowledge_source"}
    bounded_limit = max(1, min(80, int(limit)))
    results: list[dict[str, Any]] = []
    with connect_readonly() as con:
        membership = con.execute(
            """
            SELECT 1 FROM atlas_memberships
            WHERE organization_id = ? AND user_id = ? AND status = 'active'
            """,
            (int(organization_id), int(user_id)),
        ).fetchone()
        if membership is None:
            raise ValueError("atlas_search_forbidden")

        if "case" in allowed:
            rows = con.execute(
                """
                SELECT id, case_number, title, objective, executive_summary, status,
                       visibility_scope, updated_at
                FROM atlas_cases
                WHERE organization_id = ?
                  AND (visibility_scope = 'workspace' OR created_by_id = ? OR assigned_to_id = ?)
                ORDER BY updated_at DESC LIMIT 500
                """,
                (int(organization_id), int(user_id), int(user_id)),
            ).fetchall()
            for row in rows:
                body = f"{row['objective'] or ''} {row['executive_summary'] or ''}"
                if clean_query.casefold() not in f"{row['title']} {body}".casefold():
                    continue
                results.append({
                    "kind": "case", "id": int(row["id"]), "title": str(row["title"]),
                    "eyebrow": f"Дело {int(row['case_number']):03d}",
                    "snippet": _snippet(body, clean_query), "status": str(row["status"]),
                    "updated_at": str(row["updated_at"]), "screen": "cases",
                })

        if "document" in allowed:
            rows = con.execute(
                """
                SELECT id, title, rendered_text, status, revision, updated_at
                FROM atlas_documents WHERE organization_id = ?
                ORDER BY updated_at DESC LIMIT 500
                """,
                (int(organization_id),),
            ).fetchall()
            for row in rows:
                body = str(row["rendered_text"] or "")
                if clean_query.casefold() not in f"{row['title']} {body}".casefold():
                    continue
                results.append({
                    "kind": "document", "id": int(row["id"]), "title": str(row["title"]),
                    "eyebrow": f"Документ · редакция {int(row['revision'])}",
                    "snippet": _snippet(body, clean_query), "status": str(row["status"]),
                    "updated_at": str(row["updated_at"]), "screen": "documents",
                })

        if "timeline_event" in allowed:
            rows = con.execute(
                """
                SELECT e.id, e.title, e.summary, e.event_kind, e.status, e.updated_at
                FROM atlas_timeline_events e
                WHERE e.organization_id = ?
                  AND (
                    COALESCE(e.source_type, '') NOT IN ('case', 'media_asset')
                    OR (e.source_type = 'case' AND EXISTS(
                      SELECT 1 FROM atlas_cases c
                      WHERE c.id = CAST(e.source_id AS INTEGER)
                        AND c.organization_id = e.organization_id
                        AND (c.visibility_scope = 'workspace' OR c.created_by_id = ? OR c.assigned_to_id = ?)
                    ))
                    OR (e.source_type = 'media_asset' AND EXISTS(
                      SELECT 1 FROM atlas_media_assets a
                      WHERE a.id = CAST(e.source_id AS INTEGER)
                        AND a.organization_id = e.organization_id
                        AND (a.visibility_scope = 'workspace' OR a.owner_user_id = ?)
                    ))
                  )
                ORDER BY e.updated_at DESC LIMIT 500
                """,
                (int(organization_id), int(user_id), int(user_id), int(user_id)),
            ).fetchall()
            for row in rows:
                body = str(row["summary"] or "")
                if clean_query.casefold() not in f"{row['title']} {body}".casefold():
                    continue
                results.append({
                    "kind": "timeline_event", "id": int(row["id"]), "title": str(row["title"]),
                    "eyebrow": "Событие Atlas Memory", "snippet": _snippet(body, clean_query),
                    "status": str(row["status"]), "updated_at": str(row["updated_at"]), "screen": "memory",
                })

        if "media_asset" in allowed:
            rows = con.execute(
                """
                SELECT id, title, original_filename, media_kind, status, visibility_scope, updated_at
                FROM atlas_media_assets
                WHERE organization_id = ?
                  AND (visibility_scope = 'workspace' OR owner_user_id = ?)
                ORDER BY updated_at DESC LIMIT 500
                """,
                (int(organization_id), int(user_id)),
            ).fetchall()
            for row in rows:
                body = str(row["original_filename"] or "")
                if clean_query.casefold() not in f"{row['title']} {body}".casefold():
                    continue
                results.append({
                    "kind": "media_asset", "id": int(row["id"]), "title": str(row["title"]),
                    "eyebrow": f"Медиасеть · {row['media_kind']}", "snippet": _snippet(body, clean_query),
                    "status": str(row["status"]), "updated_at": str(row["updated_at"]), "screen": "media",
                })

    if "knowledge_source" in allowed:
        sources = atlas_repository.atlas_searchable_knowledge_sources(
            int(organization_id),
            server_code=server_code,
            faction_code=faction_code,
            limit=500,
        )
        for source in sources:
            body = str(source.get("content_text") or "")
            if clean_query.casefold() not in f"{source.get('title', '')} {body}".casefold():
                continue
            results.append({
                "kind": "knowledge_source", "id": int(source["id"]),
                "title": str(source.get("title") or "Источник Atlas"),
                "eyebrow": "База знаний", "snippet": _snippet(body, clean_query),
                "status": str(source.get("status") or ""), "updated_at": str(source.get("updated_at") or ""),
                "screen": "knowledge", "external_url": str(source.get("source_url") or "") or None,
            })

    for item in results:
        item["score"] = _score(item["title"], item["snippet"], clean_query, item["updated_at"])[0]
    results.sort(key=lambda item: (int(item["score"]), str(item["updated_at"])), reverse=True)
    selected = results[:bounded_limit]
    counts: dict[str, int] = {}
    for item in selected:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    return {"query": clean_query, "items": selected, "counts": counts, "total": len(results)}


__all__ = ["atlas_global_search"]
