"""Durable private workspaces for assisted bill drafting."""

from __future__ import annotations

from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


EDITABLE_WORKSPACE_STATUSES = ("draft", "review", "changes_requested")
OPEN_WORKSPACE_STATUSES = (*EDITABLE_WORKSPACE_STATUSES, "moderation")
WORKSPACE_CATEGORIES = frozenset({"ordinary", "heavy", "unanimous"})


def _row_dict(row: Any | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_bill_workspace(workspace_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM tvrs_bill_workspaces WHERE id = ?",
            (int(workspace_id),),
        ).fetchone()
    return _row_dict(row)


def get_open_bill_workspace(guild_id: int, author_id: int) -> dict[str, Any] | None:
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT * FROM tvrs_bill_workspaces
            WHERE guild_id = ? AND author_id = ?
              AND status IN ('draft', 'review', 'changes_requested')
            ORDER BY CASE status WHEN 'changes_requested' THEN 0 ELSE 1 END,
                     updated_at DESC, id DESC
            LIMIT 1
            """,
            (int(guild_id), int(author_id)),
        ).fetchone()
    return _row_dict(row)


def list_open_bill_workspaces(guild_id: int | None = None) -> list[dict[str, Any]]:
    where = "WHERE status IN ('draft', 'review', 'changes_requested', 'moderation')"
    values: tuple[Any, ...] = ()
    if guild_id is not None:
        where += " AND guild_id = ?"
        values = (int(guild_id),)
    with _db_lock, connect() as con:
        rows = con.execute(
            f"""
            SELECT * FROM tvrs_bill_workspaces
            {where}
            ORDER BY updated_at DESC, id DESC
            """,
            values,
        ).fetchall()
    return [dict(row) for row in rows]


def create_or_get_bill_workspace(
    *,
    guild_id: int,
    author_id: int,
    author_display: str | None,
    parent_channel_id: int,
) -> tuple[dict[str, Any], bool]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            """
            SELECT * FROM tvrs_bill_workspaces
            WHERE guild_id = ? AND author_id = ?
              AND status IN ('draft', 'review', 'changes_requested')
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (int(guild_id), int(author_id)),
        ).fetchone()
        if existing is not None:
            con.commit()
            return dict(existing), False
        cursor = con.execute(
            """
            INSERT INTO tvrs_bill_workspaces(
                guild_id, author_id, author_display, parent_channel_id,
                status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 'draft', ?, ?)
            """,
            (
                int(guild_id),
                int(author_id),
                str(author_display or "")[:200] or None,
                int(parent_channel_id),
                now,
                now,
            ),
        )
        row = con.execute(
            "SELECT * FROM tvrs_bill_workspaces WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
        con.commit()
    if row is None:  # pragma: no cover - one transaction
        raise RuntimeError("bill_workspace_create_failed")
    return dict(row), True


def attach_bill_workspace_thread(
    workspace_id: int,
    *,
    thread_id: int,
    panel_message_id: int | None = None,
) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        changed = con.execute(
            """
            UPDATE tvrs_bill_workspaces
            SET thread_id = ?, panel_message_id = ?,
                revision = revision + 1, updated_at = ?
            WHERE id = ? AND status IN ('draft', 'review')
            """,
            (
                int(thread_id),
                int(panel_message_id) if panel_message_id is not None else None,
                now,
                int(workspace_id),
            ),
        )
        if changed.rowcount != 1:
            con.rollback()
            raise ValueError("bill_workspace_not_editable")
        row = con.execute(
            "SELECT * FROM tvrs_bill_workspaces WHERE id = ?",
            (int(workspace_id),),
        ).fetchone()
        con.commit()
    return dict(row)


def attach_bill_workspace_panel(
    workspace_id: int,
    *,
    panel_message_id: int,
) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        changed = con.execute(
            """
            UPDATE tvrs_bill_workspaces
            SET panel_message_id = ?, revision = revision + 1, updated_at = ?
            WHERE id = ? AND status IN ('draft', 'review')
            """,
            (int(panel_message_id), now, int(workspace_id)),
        )
        if changed.rowcount != 1:
            con.rollback()
            raise ValueError("bill_workspace_not_editable")
        row = con.execute(
            "SELECT * FROM tvrs_bill_workspaces WHERE id = ?",
            (int(workspace_id),),
        ).fetchone()
        con.commit()
    return dict(row)


def update_bill_workspace(
    workspace_id: int,
    *,
    expected_revision: int,
    idea: str | None = None,
    desired_outcome: str | None = None,
    constraints_text: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    materials: str | None = None,
    decision_category: str | None = None,
    implementation_plan: str | None = None,
    leadership_actions: str | None = None,
    execution_blocks_json: str | None = None,
    ai_model: str | None = None,
    increment_ai_revision: bool = False,
    status: str | None = None,
) -> dict[str, Any]:
    assignments: list[str] = []
    values: list[Any] = []
    text_fields = {
        "idea": idea,
        "desired_outcome": desired_outcome,
        "constraints_text": constraints_text,
        "title": title,
        "summary": summary,
        "materials": materials,
        "implementation_plan": implementation_plan,
        "leadership_actions": leadership_actions,
        "ai_model": ai_model,
        "execution_blocks_json": execution_blocks_json,
    }
    for column, value in text_fields.items():
        if value is not None:
            assignments.append(f"{column} = ?")
            values.append(str(value).strip() or None)
    if decision_category is not None:
        category = str(decision_category).strip().lower()
        if category not in WORKSPACE_CATEGORIES:
            raise ValueError("bill_workspace_category_invalid")
        assignments.append("decision_category = ?")
        values.append(category)
    if status is not None:
        clean_status = str(status).strip().lower()
        if clean_status not in EDITABLE_WORKSPACE_STATUSES:
            raise ValueError("bill_workspace_status_invalid")
        assignments.append("status = ?")
        values.append(clean_status)
    if increment_ai_revision:
        assignments.append("ai_revision = ai_revision + 1")
    assignments.extend(("revision = revision + 1", "updated_at = ?"))
    values.append(utc_now_iso())
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        changed = con.execute(
            f"""
            UPDATE tvrs_bill_workspaces
            SET {", ".join(assignments)}
            WHERE id = ? AND revision = ?
              AND status IN ('draft', 'review', 'changes_requested')
            """,
            (*values, int(workspace_id), int(expected_revision)),
        )
        if changed.rowcount != 1:
            con.rollback()
            raise ValueError("bill_workspace_revision_conflict")
        row = con.execute(
            "SELECT * FROM tvrs_bill_workspaces WHERE id = ?",
            (int(workspace_id),),
        ).fetchone()
        con.commit()
    return dict(row)


def finish_bill_workspace(
    workspace_id: int,
    *,
    author_id: int,
    status: str,
    submitted_bill_id: int | None = None,
) -> dict[str, Any]:
    clean_status = str(status).strip().lower()
    if clean_status not in {"submitted", "cancelled"}:
        raise ValueError("bill_workspace_finish_status_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        changed = con.execute(
            """
            UPDATE tvrs_bill_workspaces
            SET status = ?, submitted_bill_id = COALESCE(?, submitted_bill_id),
                revision = revision + 1, updated_at = ?
            WHERE id = ? AND author_id = ?
              AND status IN ('draft', 'review', 'changes_requested')
            """,
            (
                clean_status,
                int(submitted_bill_id) if submitted_bill_id is not None else None,
                now,
                int(workspace_id),
                int(author_id),
            ),
        )
        if changed.rowcount != 1:
            current = con.execute(
                "SELECT * FROM tvrs_bill_workspaces WHERE id = ? AND author_id = ?",
                (int(workspace_id), int(author_id)),
            ).fetchone()
            if (
                current is None
                or str(current["status"]) != clean_status
                or (
                    submitted_bill_id is not None
                    and int(current["submitted_bill_id"] or 0) != int(submitted_bill_id)
                )
            ):
                con.rollback()
                raise ValueError("bill_workspace_not_editable")
            con.commit()
            return dict(current)
        row = con.execute(
            "SELECT * FROM tvrs_bill_workspaces WHERE id = ?",
            (int(workspace_id),),
        ).fetchone()
        con.commit()
    return dict(row)


__all__ = [
    "OPEN_WORKSPACE_STATUSES",
    "WORKSPACE_CATEGORIES",
    "attach_bill_workspace_thread",
    "attach_bill_workspace_panel",
    "create_or_get_bill_workspace",
    "finish_bill_workspace",
    "get_bill_workspace",
    "get_open_bill_workspace",
    "list_open_bill_workspaces",
    "update_bill_workspace",
]
