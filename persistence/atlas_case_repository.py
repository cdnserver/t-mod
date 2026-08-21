"""Cases, claims and provenance-preserving evidence for Atlas."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlparse

from persistence.atlas_repository import _atlas_entity_exists
from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


ATLAS_CASE_STATUSES = frozenset({"intake", "investigation", "review", "ready", "closed", "archived"})
ATLAS_CASE_KINDS = frozenset({"incident", "investigation", "legal", "request"})
ATLAS_CASE_PRIORITIES = frozenset({"routine", "high", "critical"})
ATLAS_CASE_VISIBILITY = frozenset({"private", "workspace"})
ATLAS_EVIDENCE_ENTITY_TYPES = frozenset(
    {"media_asset", "media_segment", "document", "knowledge_source", "timeline_event"}
)


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
    for name in ("metadata_json", "locator_json", "provenance_json", "result_json"):
        if name in item:
            item[name.removesuffix("_json")] = _decoded(item.pop(name, "{}"))
    return item


def _member_role(con: Any, organization_id: int, user_id: int) -> str:
    row = con.execute(
        """
        SELECT role FROM atlas_memberships
        WHERE organization_id = ? AND user_id = ? AND status = 'active'
        """,
        (int(organization_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ValueError("atlas_case_forbidden")
    return str(row["role"])


def _case_for_actor(con: Any, organization_id: int, user_id: int, case_id: int) -> Any:
    row = con.execute(
        """
        SELECT * FROM atlas_cases
        WHERE id = ? AND organization_id = ?
          AND (visibility_scope = 'workspace' OR created_by_id = ? OR assigned_to_id = ?)
        """,
        (int(case_id), int(organization_id), int(user_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ValueError("atlas_case_not_found")
    return row


def atlas_case_create(
    organization_id: int,
    user_id: int,
    *,
    title: str,
    case_kind: str = "investigation",
    priority: str = "routine",
    visibility_scope: str = "workspace",
    objective: str = "",
    executive_summary: str = "",
    hypothesis: str = "",
    assigned_to_id: int | None = None,
) -> dict[str, Any]:
    clean_title = str(title or "").strip()[:180]
    clean_kind = str(case_kind or "investigation").strip().lower()
    clean_priority = str(priority or "routine").strip().lower()
    clean_visibility = str(visibility_scope or "workspace").strip().lower()
    if not clean_title:
        raise ValueError("atlas_case_title_required")
    if clean_kind not in ATLAS_CASE_KINDS:
        raise ValueError("atlas_case_kind_invalid")
    if clean_priority not in ATLAS_CASE_PRIORITIES:
        raise ValueError("atlas_case_priority_invalid")
    if clean_visibility not in ATLAS_CASE_VISIBILITY:
        raise ValueError("atlas_case_visibility_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        role = _member_role(con, int(organization_id), int(user_id))
        if role == "viewer":
            raise ValueError("atlas_case_forbidden")
        assignee = int(assigned_to_id or 0) or None
        if assignee is not None:
            assigned = con.execute(
                """
                SELECT 1 FROM atlas_memberships
                WHERE organization_id = ? AND user_id = ? AND status = 'active'
                """,
                (int(organization_id), assignee),
            ).fetchone()
            if assigned is None:
                raise ValueError("atlas_case_assignee_invalid")
        case_number = int(
            con.execute(
                "SELECT COALESCE(MAX(case_number), 0) + 1 FROM atlas_cases WHERE organization_id = ?",
                (int(organization_id),),
            ).fetchone()[0]
        )
        con.execute(
            """
            INSERT INTO atlas_cases(
                organization_id, case_number, created_by_id, assigned_to_id,
                title, case_kind, priority, visibility_scope, objective,
                executive_summary, hypothesis, opened_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(organization_id), case_number, int(user_id), assignee,
                clean_title, clean_kind, clean_priority, clean_visibility,
                str(objective or "").strip()[:12000],
                str(executive_summary or "").strip()[:20000],
                str(hypothesis or "").strip()[:12000], now, now, now,
            ),
        )
        case_id = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
        con.execute(
            """
            INSERT INTO atlas_audit_events(
                organization_id, actor_user_id, event_type, target_type,
                target_id, summary, details_json, created_at
            ) VALUES(?, ?, 'case_created', 'case', ?, ?, ?, ?)
            """,
            (
                int(organization_id), int(user_id), str(case_id),
                f"Создано дело #{case_number}: {clean_title}",
                _json({"case_kind": clean_kind, "priority": clean_priority}), now,
            ),
        )
        con.execute(
            """
            INSERT INTO atlas_timeline_events(
                organization_id, actor_user_id, event_kind, title, summary,
                status, importance, occurred_at, source_type, source_id,
                dedupe_key, created_at, updated_at
            ) VALUES(?, ?, 'incident', ?, ?, 'active', ?, ?, 'case', ?, ?, ?, ?)
            """,
            (
                int(organization_id), int(user_id), f"Открыто дело #{case_number}: {clean_title}",
                str(executive_summary or objective or "Контекст дела зафиксирован в Atlas.")[:12000],
                "critical" if clean_priority == "critical" else "important" if clean_priority == "high" else "routine",
                now, str(case_id), f"case-created:{case_id}", now, now,
            ),
        )
        row = con.execute("SELECT * FROM atlas_cases WHERE id = ?", (case_id,)).fetchone()
        con.commit()
    return _row(row)


def atlas_cases(
    organization_id: int,
    user_id: int,
    *,
    status: str | None = None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    clean_status = str(status or "").strip().lower()
    if clean_status and clean_status not in ATLAS_CASE_STATUSES:
        raise ValueError("atlas_case_status_invalid")
    params: list[Any] = [int(organization_id), int(user_id), int(user_id)]
    status_filter = ""
    if clean_status:
        status_filter = "AND c.status = ?"
        params.append(clean_status)
    params.append(max(1, min(200, int(limit))))
    with connect_readonly() as con:
        _member_role(con, int(organization_id), int(user_id))
        rows = con.execute(
            f"""
            SELECT c.*,
                   (SELECT COUNT(*) FROM atlas_case_claims cl WHERE cl.case_id = c.id) AS claim_count,
                   (SELECT COUNT(*) FROM atlas_case_evidence e WHERE e.case_id = c.id) AS evidence_count
            FROM atlas_cases c
            WHERE c.organization_id = ?
              AND (c.visibility_scope = 'workspace' OR c.created_by_id = ? OR c.assigned_to_id = ?)
              {status_filter}
            ORDER BY CASE c.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                     c.updated_at DESC, c.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        items = []
        for row in rows:
            item = _row(row)
            item["readiness"] = _readiness(con, int(item["id"]))
            items.append(item)
    return items


def _readiness(con: Any, case_id: int) -> dict[str, Any]:
    claims = con.execute(
        """
        SELECT cl.*,
               SUM(CASE WHEN e.verification_status = 'verified'
                          AND e.admissibility IN ('pending', 'admissible') THEN 1 ELSE 0 END) AS support_count,
               SUM(CASE WHEN e.verification_status = 'rejected'
                          OR e.admissibility IN ('questioned', 'excluded') THEN 1 ELSE 0 END) AS weak_count
        FROM atlas_case_claims cl
        LEFT JOIN atlas_case_evidence e ON e.claim_id = cl.id
        WHERE cl.case_id = ? GROUP BY cl.id ORDER BY cl.id
        """,
        (int(case_id),),
    ).fetchall()
    evidence = con.execute(
        "SELECT * FROM atlas_case_evidence WHERE case_id = ? ORDER BY id",
        (int(case_id),),
    ).fetchall()
    unsupported = [
        int(row["id"])
        for row in claims
        if str(row["claim_status"]) != "rejected"
        and (
            int(row["support_count"] or 0) == 0
            or str(row["claim_status"]) not in {"supported", "accepted"}
        )
    ]
    critical_gaps = [
        int(row["id"])
        for row in claims
        if str(row["importance"]) == "critical"
        and str(row["claim_status"]) != "rejected"
        and (
            int(row["support_count"] or 0) == 0
            or str(row["claim_status"]) not in {"supported", "accepted"}
        )
    ]
    contradicted = [int(row["id"]) for row in claims if str(row["claim_status"]) == "contradicted"]
    pending_evidence = [
        int(row["id"])
        for row in evidence
        if str(row["verification_status"]) == "pending" or str(row["admissibility"]) == "pending"
    ]
    score = 0 if not claims else max(
        0,
        100
        - min(50, len(critical_gaps) * 25)
        - min(30, len(set(unsupported) - set(critical_gaps)) * 10)
        - min(20, len(contradicted) * 10)
        - min(20, len(pending_evidence) * 4),
    )
    return {
        "score": score,
        "state": "ready" if claims and evidence and score >= 80 and not critical_gaps else "attention" if score >= 50 else "not_ready",
        "claims": len(claims),
        "evidence": len(evidence),
        "unsupported_claim_ids": unsupported,
        "critical_gap_ids": critical_gaps,
        "contradicted_claim_ids": contradicted,
        "pending_evidence_ids": pending_evidence,
    }


def atlas_case_detail(organization_id: int, user_id: int, case_id: int) -> dict[str, Any]:
    with connect_readonly() as con:
        case = _case_for_actor(con, int(organization_id), int(user_id), int(case_id))
        participants = con.execute(
            "SELECT * FROM atlas_case_participants WHERE case_id = ? ORDER BY id",
            (int(case_id),),
        ).fetchall()
        claims = con.execute(
            "SELECT * FROM atlas_case_claims WHERE case_id = ? ORDER BY id",
            (int(case_id),),
        ).fetchall()
        evidence = con.execute(
            "SELECT * FROM atlas_case_evidence WHERE case_id = ? ORDER BY id",
            (int(case_id),),
        ).fetchall()
        findings = con.execute(
            "SELECT * FROM atlas_case_findings WHERE case_id = ? ORDER BY id DESC",
            (int(case_id),),
        ).fetchall()
        readiness = _readiness(con, int(case_id))
    return {
        "case": _row(case),
        "participants": [_row(row) for row in participants],
        "claims": [_row(row) for row in claims],
        "evidence": [_row(row) for row in evidence],
        "findings": [_row(row) for row in findings],
        "readiness": readiness,
    }


def atlas_case_add_claim(
    organization_id: int,
    user_id: int,
    case_id: int,
    *,
    statement: str,
    importance: str = "material",
    rationale: str = "",
) -> dict[str, Any]:
    clean_statement = str(statement or "").strip()[:12000]
    clean_importance = str(importance or "material").strip().lower()
    if not clean_statement or clean_importance not in {"context", "material", "critical"}:
        raise ValueError("atlas_case_claim_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        role = _member_role(con, int(organization_id), int(user_id))
        if role == "viewer":
            raise ValueError("atlas_case_forbidden")
        _case_for_actor(con, int(organization_id), int(user_id), int(case_id))
        con.execute(
            """
            INSERT INTO atlas_case_claims(
                organization_id, case_id, created_by_id, statement,
                importance, rationale, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(organization_id), int(case_id), int(user_id), clean_statement,
                clean_importance, str(rationale or "").strip()[:12000], now, now,
            ),
        )
        claim_id = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
        con.execute(
            "UPDATE atlas_cases SET version = version + 1, updated_at = ? WHERE id = ?",
            (now, int(case_id)),
        )
        row = con.execute("SELECT * FROM atlas_case_claims WHERE id = ?", (claim_id,)).fetchone()
        con.commit()
    return _row(row)


def atlas_case_update_claim(
    organization_id: int,
    user_id: int,
    case_id: int,
    claim_id: int,
    *,
    claim_status: str,
    rationale: str = "",
    expected_version: int,
) -> dict[str, Any]:
    clean_status = str(claim_status or "").strip().lower()
    if clean_status not in {"unverified", "supported", "contradicted", "accepted", "rejected"}:
        raise ValueError("atlas_case_claim_status_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        role = _member_role(con, int(organization_id), int(user_id))
        if role == "viewer":
            raise ValueError("atlas_case_forbidden")
        _case_for_actor(con, int(organization_id), int(user_id), int(case_id))
        cursor = con.execute(
            """
            UPDATE atlas_case_claims
            SET claim_status = ?, rationale = ?, version = version + 1, updated_at = ?
            WHERE id = ? AND case_id = ? AND version = ?
            """,
            (
                clean_status, str(rationale or "").strip()[:12000], now,
                int(claim_id), int(case_id), int(expected_version),
            ),
        )
        if int(cursor.rowcount or 0) != 1:
            raise ValueError("atlas_case_claim_version_conflict")
        con.execute(
            "UPDATE atlas_cases SET version = version + 1, updated_at = ? WHERE id = ?",
            (now, int(case_id)),
        )
        row = con.execute("SELECT * FROM atlas_case_claims WHERE id = ?", (int(claim_id),)).fetchone()
        con.commit()
    return _row(row)


def _evidence_source(
    con: Any,
    organization_id: int,
    user_id: int,
    source_type: str,
    source_id: str | None,
) -> tuple[str | None, dict[str, Any]]:
    clean_type = str(source_type or "").strip().lower()
    clean_id = str(source_id or "").strip() or None
    provenance: dict[str, Any] = {"source_type": clean_type}
    if clean_type in ATLAS_EVIDENCE_ENTITY_TYPES:
        if not clean_id or not clean_id.isdigit() or not _atlas_entity_exists(
            con,
            int(organization_id),
            clean_type,
            clean_id,
            actor_user_id=int(user_id),
        ):
            raise ValueError("atlas_case_evidence_source_not_found")
        provenance["source_id"] = clean_id
        if clean_type == "media_asset":
            media = con.execute(
                "SELECT blob_checksum, size_bytes, mime_type FROM atlas_media_assets WHERE id = ?",
                (int(clean_id),),
            ).fetchone()
            provenance.update(
                {
                    "checksum_sha256": media["blob_checksum"],
                    "size_bytes": int(media["size_bytes"]),
                    "mime_type": media["mime_type"],
                }
            )
    elif clean_type == "url":
        parsed = urlparse(str(clean_id or ""))
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("atlas_case_evidence_url_invalid")
        clean_id = str(clean_id)[:2000]
        provenance["url"] = clean_id
    elif clean_type == "note":
        clean_id = None
    else:
        raise ValueError("atlas_case_evidence_type_invalid")
    return clean_id, provenance


def atlas_case_add_evidence(
    organization_id: int,
    user_id: int,
    case_id: int,
    *,
    source_type: str,
    source_id: str | int | None,
    title: str,
    summary: str = "",
    relevance: str = "",
    claim_id: int | None = None,
    locator: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_title = str(title or "").strip()[:180]
    clean_locator = dict(locator or {})
    if not clean_title:
        raise ValueError("atlas_case_evidence_title_required")
    if "start_ms" in clean_locator or "end_ms" in clean_locator:
        try:
            start_ms = int(clean_locator.get("start_ms") or 0)
            end_ms = int(clean_locator.get("end_ms") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("atlas_case_evidence_locator_invalid") from exc
        if start_ms < 0 or end_ms <= start_ms:
            raise ValueError("atlas_case_evidence_locator_invalid")
        clean_locator.update({"start_ms": start_ms, "end_ms": end_ms})
    now = utc_now_iso()
    with _db_lock, connect() as con:
        role = _member_role(con, int(organization_id), int(user_id))
        if role == "viewer":
            raise ValueError("atlas_case_forbidden")
        _case_for_actor(con, int(organization_id), int(user_id), int(case_id))
        selected_claim = int(claim_id or 0) or None
        if selected_claim is not None:
            claim = con.execute(
                "SELECT 1 FROM atlas_case_claims WHERE id = ? AND case_id = ?",
                (selected_claim, int(case_id)),
            ).fetchone()
            if claim is None:
                raise ValueError("atlas_case_claim_not_found")
        clean_source_id, automatic_provenance = _evidence_source(
            con,
            int(organization_id),
            int(user_id),
            str(source_type),
            str(source_id) if source_id is not None else None,
        )
        # Caller metadata may enrich provenance, but can never replace the
        # immutable source identity/checksum captured by Atlas itself.
        merged_provenance = {**dict(provenance or {}), **automatic_provenance, "captured_at": now}
        dedupe_key = hashlib.sha256(
            _json(
                {
                    "source_type": str(source_type).lower(),
                    "source_id": clean_source_id,
                    "locator": clean_locator,
                    "claim_id": selected_claim,
                }
            ).encode("utf-8")
        ).hexdigest()
        inserted = con.execute(
            """
            INSERT INTO atlas_case_evidence(
                organization_id, case_id, claim_id, added_by_id,
                source_type, source_id, title, summary, relevance,
                locator_json, provenance_json, dedupe_key, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_id, dedupe_key) DO NOTHING
            """,
            (
                int(organization_id), int(case_id), selected_claim, int(user_id),
                str(source_type).strip().lower(), clean_source_id, clean_title,
                str(summary or "").strip()[:20000], str(relevance or "").strip()[:12000],
                _json(clean_locator), _json(merged_provenance), dedupe_key, now, now,
            ),
        )
        row = con.execute(
            "SELECT * FROM atlas_case_evidence WHERE case_id = ? AND dedupe_key = ?",
            (int(case_id), dedupe_key),
        ).fetchone()
        evidence_id = int(row["id"])
        if int(inserted.rowcount or 0) == 1:
            con.execute(
                "UPDATE atlas_cases SET version = version + 1, updated_at = ? WHERE id = ?",
                (now, int(case_id)),
            )
        link_target_type = "case_claim" if selected_claim is not None else "case"
        link_target_id = selected_claim if selected_claim is not None else int(case_id)
        con.execute(
            """
            INSERT INTO atlas_entity_links(
                organization_id, source_type, source_id, relation,
                target_type, target_id, created_by_id, metadata_json, created_at
            ) VALUES(?, 'case_evidence', ?, 'supports', ?, ?, ?, '{}', ?)
            ON CONFLICT DO NOTHING
            """,
            (
                int(organization_id), str(evidence_id), link_target_type,
                str(link_target_id), int(user_id), now,
            ),
        )
        if str(source_type).lower() in ATLAS_EVIDENCE_ENTITY_TYPES:
            con.execute(
                """
                INSERT INTO atlas_entity_links(
                    organization_id, source_type, source_id, relation,
                    target_type, target_id, created_by_id, metadata_json, created_at
                ) VALUES(?, 'case_evidence', ?, 'related_to', ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    int(organization_id), str(evidence_id), str(source_type).lower(),
                    str(clean_source_id), int(user_id), _json({"locator": clean_locator}), now,
                ),
            )
        con.commit()
    return _row(row)


def atlas_case_review_evidence(
    organization_id: int,
    user_id: int,
    case_id: int,
    evidence_id: int,
    *,
    verification_status: str,
    admissibility: str,
    expected_version: int,
) -> dict[str, Any]:
    clean_verification = str(verification_status or "").strip().lower()
    clean_admissibility = str(admissibility or "").strip().lower()
    if clean_verification not in {"pending", "verified", "rejected"}:
        raise ValueError("atlas_case_evidence_verification_invalid")
    if clean_admissibility not in {"pending", "admissible", "questioned", "excluded"}:
        raise ValueError("atlas_case_evidence_admissibility_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        role = _member_role(con, int(organization_id), int(user_id))
        if role == "viewer":
            raise ValueError("atlas_case_forbidden")
        _case_for_actor(con, int(organization_id), int(user_id), int(case_id))
        cursor = con.execute(
            """
            UPDATE atlas_case_evidence
            SET verification_status = ?, admissibility = ?,
                version = version + 1, updated_at = ?
            WHERE id = ? AND case_id = ? AND version = ?
            """,
            (
                clean_verification, clean_admissibility, now,
                int(evidence_id), int(case_id), int(expected_version),
            ),
        )
        if int(cursor.rowcount or 0) != 1:
            raise ValueError("atlas_case_evidence_version_conflict")
        con.execute(
            "UPDATE atlas_cases SET version = version + 1, updated_at = ? WHERE id = ?",
            (now, int(case_id)),
        )
        row = con.execute(
            "SELECT * FROM atlas_case_evidence WHERE id = ?",
            (int(evidence_id),),
        ).fetchone()
        con.commit()
    return _row(row)


def atlas_case_set_status(
    organization_id: int,
    user_id: int,
    case_id: int,
    *,
    status: str,
    expected_version: int,
) -> dict[str, Any]:
    clean_status = str(status or "").strip().lower()
    if clean_status not in ATLAS_CASE_STATUSES:
        raise ValueError("atlas_case_status_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        role = _member_role(con, int(organization_id), int(user_id))
        if role == "viewer":
            raise ValueError("atlas_case_forbidden")
        selected = _case_for_actor(con, int(organization_id), int(user_id), int(case_id))
        if int(selected["version"]) != int(expected_version):
            raise ValueError("atlas_case_version_conflict")
        readiness = _readiness(con, int(case_id))
        if clean_status == "ready" and readiness["state"] != "ready":
            raise ValueError("atlas_case_not_ready")
        cursor = con.execute(
            """
            UPDATE atlas_cases
            SET status = ?, version = version + 1,
                closed_at = CASE WHEN ? = 'closed' THEN ? ELSE closed_at END,
                updated_at = ?
            WHERE id = ? AND version = ?
            """,
            (clean_status, clean_status, now, now, int(case_id), int(expected_version)),
        )
        if int(cursor.rowcount or 0) != 1:
            raise ValueError("atlas_case_version_conflict")
        row = con.execute("SELECT * FROM atlas_cases WHERE id = ?", (int(case_id),)).fetchone()
        con.commit()
    return _row(row)


def atlas_case_record_finding(
    organization_id: int,
    user_id: int,
    case_id: int,
    *,
    analysis_type: str,
    title: str,
    summary: str,
    result: dict[str, Any],
    status: str = "draft",
) -> dict[str, Any]:
    clean_type = str(analysis_type or "").strip().lower()
    clean_status = str(status or "draft").strip().lower()
    if clean_type not in {"investigator", "contradiction", "readiness", "brief"}:
        raise ValueError("atlas_case_finding_type_invalid")
    if clean_status not in {"draft", "final"} or not str(title or "").strip():
        raise ValueError("atlas_case_finding_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        role = _member_role(con, int(organization_id), int(user_id))
        if role == "viewer":
            raise ValueError("atlas_case_forbidden")
        case = _case_for_actor(con, int(organization_id), int(user_id), int(case_id))
        if clean_status == "final":
            con.execute(
                """
                UPDATE atlas_case_findings SET status = 'superseded', updated_at = ?
                WHERE case_id = ? AND analysis_type = ? AND status = 'final'
                """,
                (now, int(case_id), clean_type),
            )
        con.execute(
            """
            INSERT INTO atlas_case_findings(
                organization_id, case_id, created_by_id, analysis_type,
                status, title, summary, result_json, source_version, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(organization_id), int(case_id), int(user_id), clean_type, clean_status,
                str(title).strip()[:180], str(summary or "").strip()[:20000],
                _json(dict(result or {})), int(case["version"]), now, now,
            ),
        )
        finding_id = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
        row = con.execute("SELECT * FROM atlas_case_findings WHERE id = ?", (finding_id,)).fetchone()
        con.commit()
    return _row(row)


__all__ = [name for name in globals() if name.startswith("atlas_case") or name.startswith("ATLAS_CASE") or name.startswith("ATLAS_EVIDENCE")]
