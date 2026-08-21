"""Versioned documents, comments and approval decisions for Atlas."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


ATLAS_DOCUMENT_TRANSITIONS = {
    "draft": frozenset({"review", "archived"}),
    "review": frozenset({"draft", "approved", "archived"}),
    "approved": frozenset({"review", "published", "archived"}),
    "published": frozenset({"archived"}),
    "archived": frozenset(),
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decoded(value: Any) -> Any:
    try:
        return json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _row(row: Any) -> dict[str, Any]:
    item = dict(row)
    for name in ("fields_json", "schema_json", "metadata_json"):
        if name in item:
            item[name.removesuffix("_json")] = _decoded(item.pop(name))
    return item


def _membership(con: Any, organization_id: int, user_id: int) -> str:
    row = con.execute(
        """
        SELECT role FROM atlas_memberships
        WHERE organization_id = ? AND user_id = ? AND status = 'active'
        """,
        (int(organization_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ValueError("atlas_document_forbidden")
    return str(row["role"])


def _document(con: Any, organization_id: int, document_id: int) -> Any:
    row = con.execute(
        "SELECT * FROM atlas_documents WHERE id = ? AND organization_id = ?",
        (int(document_id), int(organization_id)),
    ).fetchone()
    if row is None:
        raise ValueError("atlas_document_not_found")
    return row


def _checksum(title: str, fields: dict[str, Any], rendered_text: str) -> str:
    return hashlib.sha256(
        _json({"title": title, "fields": fields, "rendered_text": rendered_text}).encode("utf-8")
    ).hexdigest()


def atlas_document_detail(
    organization_id: int,
    user_id: int,
    document_id: int,
) -> dict[str, Any]:
    with connect_readonly() as con:
        _membership(con, int(organization_id), int(user_id))
        document = _document(con, int(organization_id), int(document_id))
        revisions = con.execute(
            """
            SELECT * FROM atlas_document_revisions
            WHERE document_id = ? ORDER BY revision DESC
            """,
            (int(document_id),),
        ).fetchall()
        comments = con.execute(
            """
            SELECT c.*, m.display_name AS author_name
            FROM atlas_document_comments c
            LEFT JOIN atlas_memberships m
              ON m.organization_id = c.organization_id AND m.user_id = c.author_user_id
            WHERE c.document_id = ? ORDER BY c.created_at, c.id
            """,
            (int(document_id),),
        ).fetchall()
        approvals = con.execute(
            """
            SELECT a.*, m.display_name AS assigned_name
            FROM atlas_document_approvals a
            LEFT JOIN atlas_memberships m
              ON m.organization_id = a.organization_id AND m.user_id = a.assigned_user_id
            WHERE a.document_id = ? ORDER BY a.step_order, a.id
            """,
            (int(document_id),),
        ).fetchall()
    approval_state = "complete" if approvals and all(str(row["status"]) in {"approved", "skipped"} for row in approvals) else "attention" if any(str(row["status"]) == "rejected" for row in approvals) else "pending"
    return {
        "document": _row(document),
        "revisions": [_row(row) for row in revisions],
        "comments": [_row(row) for row in comments],
        "approvals": [_row(row) for row in approvals],
        "approval_state": approval_state,
        "open_comments": sum(str(row["status"]) == "open" for row in comments),
    }


def atlas_document_revise(
    organization_id: int,
    user_id: int,
    document_id: int,
    *,
    title: str,
    fields: dict[str, Any],
    rendered_text: str,
    change_summary: str,
    expected_revision: int,
) -> dict[str, Any]:
    clean_title = str(title or "").strip()[:180]
    clean_fields = {str(key)[:80]: str(value).strip()[:12000] for key, value in dict(fields or {}).items()}
    clean_text = str(rendered_text or "")[:100000]
    if not clean_title:
        raise ValueError("atlas_document_title_required")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        role = _membership(con, int(organization_id), int(user_id))
        if role == "viewer":
            raise ValueError("atlas_document_forbidden")
        document = _document(con, int(organization_id), int(document_id))
        if int(document["revision"]) != int(expected_revision):
            raise ValueError("atlas_document_revision_conflict")
        if str(document["status"]) in {"published", "archived"}:
            raise ValueError("atlas_document_locked")
        next_revision = int(document["revision"]) + 1
        checksum = _checksum(clean_title, clean_fields, clean_text)
        con.execute(
            """
            INSERT INTO atlas_document_revisions(
                organization_id, document_id, revision, created_by_id,
                title, fields_json, rendered_text, change_summary,
                checksum_sha256, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(organization_id), int(document_id), next_revision, int(user_id),
                clean_title, _json(clean_fields), clean_text,
                str(change_summary or "Обновлён черновик").strip()[:1000], checksum, now,
            ),
        )
        con.execute(
            """
            UPDATE atlas_documents
            SET title = ?, fields_json = ?, rendered_text = ?, revision = ?,
                status = CASE WHEN status = 'approved' THEN 'review' ELSE status END,
                approved_at = CASE WHEN status = 'approved' THEN NULL ELSE approved_at END,
                updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (clean_title, _json(clean_fields), clean_text, next_revision, now, int(document_id), int(expected_revision)),
        )
        con.execute(
            """
            UPDATE atlas_document_approvals
            SET status = 'pending', decided_by_id = NULL, decision_note = '',
                decided_at = NULL, updated_at = ?
            WHERE document_id = ? AND status IN ('approved', 'rejected')
            """,
            (now, int(document_id)),
        )
        con.execute(
            """
            INSERT INTO atlas_audit_events(
                organization_id, actor_user_id, event_type, target_type,
                target_id, summary, details_json, created_at
            ) VALUES(?, ?, 'document_revised', 'document', ?, ?, ?, ?)
            """,
            (
                int(organization_id), int(user_id), str(document_id),
                f"Создана редакция {next_revision} документа «{clean_title}»",
                _json({"revision": next_revision, "checksum": checksum}), now,
            ),
        )
        row = con.execute("SELECT * FROM atlas_documents WHERE id = ?", (int(document_id),)).fetchone()
        con.commit()
    return _row(row)


def atlas_document_add_comment(
    organization_id: int,
    user_id: int,
    document_id: int,
    *,
    body: str,
    revision: int,
    parent_comment_id: int | None = None,
) -> dict[str, Any]:
    clean_body = str(body or "").strip()[:12000]
    if not clean_body:
        raise ValueError("atlas_document_comment_required")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        _membership(con, int(organization_id), int(user_id))
        document = _document(con, int(organization_id), int(document_id))
        if int(revision) < 1 or int(revision) > int(document["revision"]):
            raise ValueError("atlas_document_revision_not_found")
        parent = int(parent_comment_id or 0) or None
        if parent is not None and con.execute(
            "SELECT 1 FROM atlas_document_comments WHERE id = ? AND document_id = ?",
            (parent, int(document_id)),
        ).fetchone() is None:
            raise ValueError("atlas_document_comment_parent_not_found")
        cursor = con.execute(
            """
            INSERT INTO atlas_document_comments(
                organization_id, document_id, revision, author_user_id,
                parent_comment_id, body, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (int(organization_id), int(document_id), int(revision), int(user_id), parent, clean_body, now, now),
        )
        row = con.execute("SELECT * FROM atlas_document_comments WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
        con.commit()
    return _row(row)


def atlas_document_resolve_comment(
    organization_id: int,
    user_id: int,
    document_id: int,
    comment_id: int,
) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        role = _membership(con, int(organization_id), int(user_id))
        document = _document(con, int(organization_id), int(document_id))
        comment = con.execute(
            "SELECT * FROM atlas_document_comments WHERE id = ? AND document_id = ?",
            (int(comment_id), int(document_id)),
        ).fetchone()
        if comment is None:
            raise ValueError("atlas_document_comment_not_found")
        if int(comment["author_user_id"]) != int(user_id) and int(document["author_user_id"]) != int(user_id) and role not in {"owner", "administrator"}:
            raise ValueError("atlas_document_forbidden")
        con.execute(
            """
            UPDATE atlas_document_comments
            SET status = 'resolved', resolved_by_id = ?, resolved_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (int(user_id), now, now, int(comment_id)),
        )
        row = con.execute("SELECT * FROM atlas_document_comments WHERE id = ?", (int(comment_id),)).fetchone()
        con.commit()
    return _row(row)


def atlas_document_configure_approvals(
    organization_id: int,
    user_id: int,
    document_id: int,
    *,
    steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not steps or len(steps) > 12:
        raise ValueError("atlas_document_approval_steps_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        role = _membership(con, int(organization_id), int(user_id))
        document = _document(con, int(organization_id), int(document_id))
        if role not in {"owner", "administrator", "editor"} and int(document["author_user_id"]) != int(user_id):
            raise ValueError("atlas_document_forbidden")
        if str(document["status"]) in {"published", "archived"}:
            raise ValueError("atlas_document_locked")
        normalized = []
        for position, raw in enumerate(steps, start=1):
            title = str(raw.get("title") or "").strip()[:120]
            assigned_user_id = int(raw.get("assigned_user_id") or 0) or None
            required_role = str(raw.get("required_role") or "").strip().lower()[:40] or None
            if not title or (assigned_user_id is None and required_role is None):
                raise ValueError("atlas_document_approval_step_invalid")
            if assigned_user_id is not None and con.execute(
                """
                SELECT 1 FROM atlas_memberships
                WHERE organization_id = ? AND user_id = ? AND status = 'active'
                """,
                (int(organization_id), assigned_user_id),
            ).fetchone() is None:
                raise ValueError("atlas_document_approval_assignee_invalid")
            normalized.append((position, title, assigned_user_id, required_role, str(raw.get("due_at") or "").strip() or None))
        con.execute("DELETE FROM atlas_document_approvals WHERE document_id = ?", (int(document_id),))
        con.executemany(
            """
            INSERT INTO atlas_document_approvals(
                organization_id, document_id, step_order, title,
                assigned_user_id, required_role, due_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (int(organization_id), int(document_id), position, title, assigned, required, due, now, now)
                for position, title, assigned, required, due in normalized
            ],
        )
        rows = con.execute(
            "SELECT * FROM atlas_document_approvals WHERE document_id = ? ORDER BY step_order",
            (int(document_id),),
        ).fetchall()
        con.commit()
    return [_row(row) for row in rows]


def atlas_document_decide_approval(
    organization_id: int,
    user_id: int,
    document_id: int,
    approval_id: int,
    *,
    decision: str,
    note: str = "",
) -> dict[str, Any]:
    clean_decision = str(decision or "").strip().lower()
    if clean_decision not in {"approved", "rejected"}:
        raise ValueError("atlas_document_approval_decision_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        role = _membership(con, int(organization_id), int(user_id))
        _document(con, int(organization_id), int(document_id))
        approval = con.execute(
            "SELECT * FROM atlas_document_approvals WHERE id = ? AND document_id = ?",
            (int(approval_id), int(document_id)),
        ).fetchone()
        if approval is None:
            raise ValueError("atlas_document_approval_not_found")
        if str(approval["status"]) != "pending":
            raise ValueError("atlas_document_approval_already_decided")
        assigned = int(approval["assigned_user_id"] or 0)
        required_role = str(approval["required_role"] or "")
        if assigned and assigned != int(user_id) and role not in {"owner", "administrator"}:
            raise ValueError("atlas_document_forbidden")
        if not assigned and required_role and role != required_role and role not in {"owner", "administrator"}:
            raise ValueError("atlas_document_forbidden")
        previous_pending = con.execute(
            """
            SELECT 1 FROM atlas_document_approvals
            WHERE document_id = ? AND step_order < ? AND status NOT IN ('approved', 'skipped')
            """,
            (int(document_id), int(approval["step_order"])),
        ).fetchone()
        if previous_pending is not None:
            raise ValueError("atlas_document_approval_out_of_order")
        con.execute(
            """
            UPDATE atlas_document_approvals
            SET status = ?, decided_by_id = ?, decision_note = ?,
                decided_at = ?, updated_at = ? WHERE id = ?
            """,
            (clean_decision, int(user_id), str(note or "").strip()[:4000], now, now, int(approval_id)),
        )
        row = con.execute("SELECT * FROM atlas_document_approvals WHERE id = ?", (int(approval_id),)).fetchone()
        con.commit()
    return _row(row)


def atlas_document_transition(
    organization_id: int,
    user_id: int,
    document_id: int,
    *,
    status: str,
    expected_revision: int,
) -> dict[str, Any]:
    clean_status = str(status or "").strip().lower()
    now = utc_now_iso()
    with _db_lock, connect() as con:
        role = _membership(con, int(organization_id), int(user_id))
        document = _document(con, int(organization_id), int(document_id))
        current = str(document["status"])
        if int(document["revision"]) != int(expected_revision):
            raise ValueError("atlas_document_revision_conflict")
        if clean_status not in ATLAS_DOCUMENT_TRANSITIONS.get(current, frozenset()):
            raise ValueError("atlas_document_transition_invalid")
        if clean_status in {"approved", "published"}:
            open_comments = con.execute(
                "SELECT 1 FROM atlas_document_comments WHERE document_id = ? AND status = 'open' LIMIT 1",
                (int(document_id),),
            ).fetchone()
            rejected_or_pending = con.execute(
                """
                SELECT 1 FROM atlas_document_approvals
                WHERE document_id = ? AND status NOT IN ('approved', 'skipped') LIMIT 1
                """,
                (int(document_id),),
            ).fetchone()
            approval_count = int(con.execute(
                "SELECT COUNT(*) FROM atlas_document_approvals WHERE document_id = ?",
                (int(document_id),),
            ).fetchone()[0])
            if open_comments is not None:
                raise ValueError("atlas_document_open_comments")
            if approval_count and rejected_or_pending is not None:
                raise ValueError("atlas_document_approval_incomplete")
        if clean_status in {"approved", "published", "archived"} and role not in {"owner", "administrator", "editor"}:
            raise ValueError("atlas_document_forbidden")
        con.execute(
            """
            UPDATE atlas_documents
            SET status = ?, reviewed_by_id = CASE WHEN ? IN ('review','approved') THEN ? ELSE reviewed_by_id END,
                approved_at = CASE WHEN ? = 'approved' THEN ? ELSE approved_at END,
                updated_at = ? WHERE id = ? AND revision = ?
            """,
            (clean_status, clean_status, int(user_id), clean_status, now, now, int(document_id), int(expected_revision)),
        )
        con.execute(
            """
            INSERT INTO atlas_audit_events(
                organization_id, actor_user_id, event_type, target_type,
                target_id, summary, details_json, created_at
            ) VALUES(?, ?, 'document_transitioned', 'document', ?, ?, ?, ?)
            """,
            (
                int(organization_id), int(user_id), str(document_id),
                f"Документ переведён: {current} → {clean_status}",
                _json({"from": current, "to": clean_status, "revision": int(expected_revision)}), now,
            ),
        )
        row = con.execute("SELECT * FROM atlas_documents WHERE id = ?", (int(document_id),)).fetchone()
        con.commit()
    return _row(row)


__all__ = [name for name in globals() if name.startswith("atlas_document") or name == "ATLAS_DOCUMENT_TRANSITIONS"]
