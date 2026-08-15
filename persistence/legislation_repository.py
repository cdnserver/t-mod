"""Moderation, execution blocks and the shared legislation task board."""

from __future__ import annotations

import json
import re
from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


BLOCK_TYPES = frozenset(
    {"rule_change", "task", "communication", "appointment", "integration", "review"}
)
TASK_STATUSES = frozenset({"planned", "todo", "in_progress", "blocked", "done"})
TASK_PRIORITIES = frozenset({"low", "normal", "high", "critical"})
_BLOCK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,50}$")


def normalize_execution_blocks(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("bill_execution_blocks_invalid") from exc
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 24:
        raise ValueError("bill_execution_blocks_invalid")
    result: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise ValueError("bill_execution_blocks_invalid")
        kind = str(raw.get("type") or "task").strip().lower()
        if kind not in BLOCK_TYPES:
            raise ValueError("bill_execution_block_type_invalid")
        block_id = str(raw.get("id") or f"block-{index}").strip()
        if not _BLOCK_ID_RE.fullmatch(block_id) or block_id in used:
            block_id = f"block-{index}"
            while block_id in used:
                block_id += "x"
        title = str(raw.get("title") or "").strip()
        description = str(raw.get("description") or "").strip()
        owner = str(raw.get("owner") or "").strip()
        deadline = str(raw.get("deadline") or "").strip()
        if not 2 <= len(title) <= 180 or len(description) > 1500:
            raise ValueError("bill_execution_block_content_invalid")
        if len(owner) > 120 or len(deadline) > 80:
            raise ValueError("bill_execution_block_content_invalid")
        used.add(block_id)
        result.append(
            {
                "id": block_id,
                "type": kind,
                "title": title,
                "description": description,
                "owner": owner,
                "deadline": deadline,
                "position": index,
            }
        )
    return result


def execution_blocks_json(value: Any) -> str:
    return json.dumps(
        normalize_execution_blocks(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_execution_blocks(value: Any) -> list[dict[str, Any]]:
    try:
        return normalize_execution_blocks(value)
    except ValueError:
        return []


def list_author_workspaces(
    guild_id: int,
    author_id: int,
    *,
    limit: int = 80,
) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT * FROM tvrs_bill_workspaces
            WHERE guild_id = ? AND author_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (int(guild_id), int(author_id), max(1, min(int(limit), 200))),
        ).fetchall()
    return [dict(row) for row in rows]


def moderation_events(workspace_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    ids = sorted({int(value) for value in workspace_ids if int(value) > 0})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with connect_readonly() as con:
        rows = con.execute(
            f"""
            SELECT * FROM tvrs_bill_moderation_events
            WHERE workspace_id IN ({placeholders})
            ORDER BY id ASC
            """,
            ids,
        ).fetchall()
    result: dict[int, list[dict[str, Any]]] = {value: [] for value in ids}
    for row in rows:
        result.setdefault(int(row["workspace_id"]), []).append(dict(row))
    return result


def list_moderation_queue(guild_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT * FROM tvrs_bill_workspaces
            WHERE guild_id = ? AND moderation_status = 'pending'
            ORDER BY submitted_at ASC, id ASC
            LIMIT ?
            """,
            (int(guild_id), max(1, min(int(limit), 250))),
        ).fetchall()
    return [dict(row) for row in rows]


def submit_for_moderation(
    workspace_id: int,
    *,
    guild_id: int,
    author_id: int,
    author_display: str,
    expected_revision: int,
) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM tvrs_bill_workspaces WHERE id = ? AND guild_id = ? AND author_id = ?",
            (int(workspace_id), int(guild_id), int(author_id)),
        ).fetchone()
        if row is None:
            con.rollback()
            raise ValueError("bill_workspace_not_found")
        if str(row["status"]) not in {"draft", "review", "changes_requested"}:
            con.rollback()
            raise ValueError("bill_workspace_not_editable")
        if int(row["revision"] or 0) != int(expected_revision):
            con.rollback()
            raise ValueError("bill_workspace_revision_conflict")
        new_round = int(row["moderation_round"] or 0) + 1
        new_revision = int(expected_revision) + 1
        changed = con.execute(
            """
            UPDATE tvrs_bill_workspaces
            SET status = 'moderation', moderation_status = 'pending',
                moderation_round = ?, moderation_note = NULL,
                moderator_id = NULL, moderator_display = NULL,
                submitted_at = ?, reviewed_at = NULL,
                revision = ?, updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (new_round, now, new_revision, now, int(workspace_id), int(expected_revision)),
        )
        if changed.rowcount != 1:
            con.rollback()
            raise ValueError("bill_workspace_revision_conflict")
        con.execute(
            """
            INSERT INTO tvrs_bill_moderation_events(
                guild_id, workspace_id, round, action, actor_id,
                actor_display, note, workspace_revision, created_at
            ) VALUES(?, ?, ?, 'submitted', ?, ?, NULL, ?, ?)
            """,
            (
                int(guild_id),
                int(workspace_id),
                new_round,
                int(author_id),
                str(author_display)[:200],
                new_revision,
                now,
            ),
        )
        updated = con.execute(
            "SELECT * FROM tvrs_bill_workspaces WHERE id = ?",
            (int(workspace_id),),
        ).fetchone()
        con.commit()
    return dict(updated)


def record_moderation_decision(
    workspace_id: int,
    *,
    guild_id: int,
    moderator_id: int,
    moderator_display: str,
    expected_revision: int,
    decision: str,
    note: str,
    submitted_bill_id: int | None = None,
) -> dict[str, Any]:
    clean_decision = str(decision).strip().lower()
    if clean_decision not in {"approved", "changes_requested", "rejected"}:
        raise ValueError("bill_moderation_decision_invalid")
    clean_note = str(note or "").strip()
    if clean_decision != "approved" and not 3 <= len(clean_note) <= 2000:
        raise ValueError("bill_moderation_note_required")
    if len(clean_note) > 2000:
        raise ValueError("bill_moderation_note_too_long")
    if clean_decision == "approved" and not submitted_bill_id:
        raise ValueError("bill_moderation_bill_required")
    target_status = {
        "approved": "submitted",
        "changes_requested": "changes_requested",
        "rejected": "rejected",
    }[clean_decision]
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM tvrs_bill_workspaces WHERE id = ? AND guild_id = ?",
            (int(workspace_id), int(guild_id)),
        ).fetchone()
        if row is None:
            con.rollback()
            raise ValueError("bill_workspace_not_found")
        if str(row["moderation_status"]) != "pending" or str(row["status"]) != "moderation":
            if (
                str(row["moderation_status"]) == clean_decision
                and (
                    submitted_bill_id is None
                    or int(row["submitted_bill_id"] or 0) == int(submitted_bill_id)
                )
            ):
                con.commit()
                return dict(row)
            con.rollback()
            raise ValueError("bill_moderation_not_pending")
        if int(row["revision"] or 0) != int(expected_revision):
            con.rollback()
            raise ValueError("bill_workspace_revision_conflict")
        new_revision = int(expected_revision) + 1
        con.execute(
            """
            UPDATE tvrs_bill_workspaces
            SET status = ?, moderation_status = ?, moderation_note = ?,
                moderator_id = ?, moderator_display = ?, reviewed_at = ?,
                submitted_bill_id = COALESCE(?, submitted_bill_id),
                revision = ?, updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (
                target_status,
                clean_decision,
                clean_note or None,
                int(moderator_id),
                str(moderator_display)[:200],
                now,
                int(submitted_bill_id) if submitted_bill_id else None,
                new_revision,
                now,
                int(workspace_id),
                int(expected_revision),
            ),
        )
        con.execute(
            """
            INSERT INTO tvrs_bill_moderation_events(
                guild_id, workspace_id, round, action, actor_id,
                actor_display, note, workspace_revision, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(guild_id),
                int(workspace_id),
                int(row["moderation_round"] or 1),
                clean_decision,
                int(moderator_id),
                str(moderator_display)[:200],
                clean_note or None,
                new_revision,
                now,
            ),
        )
        updated = con.execute(
            "SELECT * FROM tvrs_bill_workspaces WHERE id = ?",
            (int(workspace_id),),
        ).fetchone()
        con.commit()
    return dict(updated)


def ensure_execution_tasks(
    *,
    guild_id: int,
    bill_id: int,
    workspace_id: int,
    blocks: Any,
    actor_id: int,
    actor_display: str,
) -> int:
    normalized = normalize_execution_blocks(blocks)
    if not normalized:
        return 0
    now = utc_now_iso()
    created = 0
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        for block in normalized:
            cursor = con.execute(
                """
                INSERT OR IGNORE INTO tvrs_legislation_tasks(
                    guild_id, bill_id, workspace_id, source_block_id,
                    title, description, status, priority, assignee_display,
                    due_at, created_by_id, created_by_display,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'planned', 'normal', ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(guild_id),
                    int(bill_id),
                    int(workspace_id),
                    str(block["id"]),
                    str(block["title"]),
                    str(block.get("description") or "") or None,
                    str(block.get("owner") or "") or None,
                    str(block.get("deadline") or "") or None,
                    int(actor_id),
                    str(actor_display)[:200],
                    now,
                    now,
                ),
            )
            created += max(0, int(cursor.rowcount))
        con.commit()
    return created


def sync_accepted_tasks(guild_id: int) -> int:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        changed = con.execute(
            """
            UPDATE tvrs_legislation_tasks
            SET status = 'todo', revision = revision + 1, updated_at = ?
            WHERE guild_id = ? AND status = 'planned'
              AND bill_id IN (
                  SELECT id FROM tvrs_bills
                  WHERE guild_id = ? AND status = 'accepted'
              )
            """,
            (now, int(guild_id), int(guild_id)),
        )
        con.commit()
    return max(0, int(changed.rowcount))


def task_board(guild_id: int, *, limit: int = 300) -> list[dict[str, Any]]:
    sync_accepted_tasks(int(guild_id))
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT task.*, bill.bill_number, bill.title AS bill_title
            FROM tvrs_legislation_tasks AS task
            LEFT JOIN tvrs_bills AS bill ON bill.id = task.bill_id
            WHERE task.guild_id = ?
            ORDER BY
              CASE task.status
                WHEN 'in_progress' THEN 0 WHEN 'blocked' THEN 1
                WHEN 'todo' THEN 2 WHEN 'planned' THEN 3 ELSE 4 END,
              CASE task.priority
                WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                WHEN 'normal' THEN 2 ELSE 3 END,
              COALESCE(task.due_at, '9999-12-31'), task.id DESC
            LIMIT ?
            """,
            (int(guild_id), max(1, min(int(limit), 500))),
        ).fetchall()
    return [dict(row) for row in rows]


def create_task(
    *,
    guild_id: int,
    title: str,
    description: str,
    priority: str,
    assignee_id: int | None,
    assignee_display: str | None,
    due_at: str | None,
    actor_id: int,
    actor_display: str,
) -> dict[str, Any]:
    clean_title = str(title or "").strip()
    clean_description = str(description or "").strip()
    clean_priority = str(priority or "normal").strip().lower()
    if not 2 <= len(clean_title) <= 180 or len(clean_description) > 2000:
        raise ValueError("legislation_task_content_invalid")
    if clean_priority not in TASK_PRIORITIES:
        raise ValueError("legislation_task_priority_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            INSERT INTO tvrs_legislation_tasks(
                guild_id, title, description, status, priority,
                assignee_id, assignee_display, due_at,
                created_by_id, created_by_display, created_at, updated_at
            ) VALUES(?, ?, ?, 'todo', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(guild_id),
                clean_title,
                clean_description or None,
                clean_priority,
                int(assignee_id) if assignee_id else None,
                str(assignee_display or "").strip()[:200] or None,
                str(due_at or "").strip()[:80] or None,
                int(actor_id),
                str(actor_display)[:200],
                now,
                now,
            ),
        )
        row = con.execute(
            "SELECT * FROM tvrs_legislation_tasks WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
        con.commit()
    return dict(row)


def update_task(
    task_id: int,
    *,
    guild_id: int,
    expected_revision: int,
    status: str,
    assignee_id: int | None = None,
    assignee_display: str | None = None,
) -> dict[str, Any]:
    clean_status = str(status).strip().lower()
    if clean_status not in TASK_STATUSES - {"planned"}:
        raise ValueError("legislation_task_status_invalid")
    now = utc_now_iso()
    completed_at = now if clean_status == "done" else None
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        changed = con.execute(
            """
            UPDATE tvrs_legislation_tasks
            SET status = ?, assignee_id = COALESCE(?, assignee_id),
                assignee_display = COALESCE(?, assignee_display),
                completed_at = ?, revision = revision + 1, updated_at = ?
            WHERE id = ? AND guild_id = ? AND revision = ?
            """,
            (
                clean_status,
                int(assignee_id) if assignee_id else None,
                str(assignee_display or "").strip()[:200] or None,
                completed_at,
                now,
                int(task_id),
                int(guild_id),
                int(expected_revision),
            ),
        )
        if changed.rowcount != 1:
            con.rollback()
            raise ValueError("legislation_task_revision_conflict")
        row = con.execute(
            "SELECT * FROM tvrs_legislation_tasks WHERE id = ?",
            (int(task_id),),
        ).fetchone()
        con.commit()
    return dict(row)


__all__ = [
    "BLOCK_TYPES",
    "TASK_PRIORITIES",
    "TASK_STATUSES",
    "create_task",
    "ensure_execution_tasks",
    "execution_blocks_json",
    "list_author_workspaces",
    "list_moderation_queue",
    "moderation_events",
    "normalize_execution_blocks",
    "parse_execution_blocks",
    "record_moderation_decision",
    "submit_for_moderation",
    "sync_accepted_tasks",
    "task_board",
    "update_task",
]
