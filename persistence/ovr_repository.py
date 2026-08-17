"""Durable OVR investigations, dossiers and append-only operational history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


OVR_STATUSES = frozenset(
    {"new", "screening", "needs_info", "analysis", "decision", "approved", "denied", "archived"}
)
OVR_RISKS = frozenset({"unrated", "low", "medium", "high", "critical"})
OVR_CASE_KINDS = frozenset({"admission", "background", "incident", "internal", "other"})
OVR_PRIORITIES = frozenset({"normal", "important", "urgent", "critical"})
OVR_CLASSIFICATIONS = frozenset({"restricted", "secret", "top_secret"})
OVR_MATERIAL_KINDS = frozenset({"document", "link", "testimony", "observation", "media", "other"})
OVR_RELIABILITY = frozenset({"unrated", "low", "medium", "high", "confirmed"})
OVR_MATERIAL_STATUSES = frozenset({"new", "verified", "rejected"})
OVR_TASK_STATUSES = frozenset({"todo", "doing", "blocked", "done", "archived"})
OVR_ACTION_TRANSITIONS = {
    "claim": frozenset({"new"}),
    "needs_info": frozenset({"screening", "analysis", "decision"}),
    "analysis": frozenset({"screening", "needs_info", "decision"}),
    "decision": frozenset({"analysis"}),
    "approve": frozenset({"decision"}),
    "deny": frozenset({"decision"}),
    "reopen": frozenset({"approved", "denied", "archived"}),
    "archive": frozenset({"approved", "denied"}),
}


def _clean(value: Any, *, minimum: int = 0, maximum: int = 1000, code: str) -> str:
    text = str(value or "").strip()
    if len(text) < minimum or len(text) > maximum:
        raise ValueError(code)
    return text


def _choice(value: Any, allowed: frozenset[str], *, default: str, code: str) -> str:
    clean = str(value or default).strip().lower()
    if clean not in allowed:
        raise ValueError(code)
    return clean


def _url(value: Any, *, code: str = "ovr_source_url_invalid") -> str | None:
    url = str(value or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(url) > 1000:
        raise ValueError(code)
    return url


def _due_at(value: Any) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("ovr_due_at_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _progress(status: str, *, tasks_total: int = 0, tasks_done: int = 0) -> int:
    base = {
        "new": 8,
        "screening": 30,
        "needs_info": 34,
        "analysis": 62,
        "decision": 86,
        "approved": 100,
        "denied": 100,
        "archived": 100,
    }.get(str(status), 0)
    if tasks_total and base < 86:
        task_progress = round((tasks_done / tasks_total) * 20)
        base = min(84, base + task_progress)
    return base


def _decorate_case(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["progress"] = _progress(
        str(item.get("status") or "new"),
        tasks_total=int(item.get("task_count") or 0),
        tasks_done=int(item.get("task_done_count") or 0),
    )
    return item


def _event(
    con: Any,
    *,
    guild_id: int,
    case_id: int,
    actor_id: int,
    actor_display: str,
    action: str,
    note: str | None,
    now: str,
) -> None:
    con.execute(
        """
        INSERT INTO ovr_case_events(
            guild_id, case_id, actor_id, actor_display, action, note, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(guild_id), int(case_id), int(actor_id), str(actor_display)[:200],
            str(action)[:80], str(note)[:5000] if note else None, now,
        ),
    )


def _case_for_write(
    con: Any,
    case_id: int,
    guild_id: int,
    expected_revision: int,
    *,
    allow_closed: bool = False,
) -> Any:
    row = con.execute(
        "SELECT * FROM ovr_cases WHERE id = ? AND guild_id = ?",
        (int(case_id), int(guild_id)),
    ).fetchone()
    if row is None:
        raise ValueError("ovr_case_not_found")
    if int(row["revision"] or 0) != int(expected_revision):
        raise ValueError("ovr_case_revision_conflict")
    if not allow_closed and str(row["status"]) in {"approved", "denied", "archived"}:
        raise ValueError("ovr_case_closed")
    return row


def _bump_case(con: Any, case_id: int, expected_revision: int, now: str) -> None:
    cursor = con.execute(
        """
        UPDATE ovr_cases SET revision = revision + 1, updated_at = ?
        WHERE id = ? AND revision = ?
        """,
        (now, int(case_id), int(expected_revision)),
    )
    if cursor.rowcount != 1:
        raise ValueError("ovr_case_revision_conflict")


def create_case(
    *,
    guild_id: int,
    first_name: str,
    last_name: str,
    static_id: str,
    discord_text: str,
    discord_user_id: int | None,
    forum_url: str | None,
    additional_info: str,
    actor_id: int,
    actor_display: str,
    case_kind: str = "admission",
    priority: str = "normal",
    classification: str = "restricted",
    objective: str = "",
) -> dict[str, Any]:
    first = _clean(first_name, minimum=2, maximum=80, code="ovr_name_invalid")
    last = _clean(last_name, minimum=2, maximum=80, code="ovr_name_invalid")
    static = _clean(static_id, minimum=1, maximum=12, code="ovr_static_invalid")
    if not static.isdigit():
        raise ValueError("ovr_static_invalid")
    discord_value = _clean(discord_text, minimum=2, maximum=200, code="ovr_discord_invalid")
    additional = _clean(additional_info, maximum=5000, code="ovr_additional_too_long")
    clean_kind = _choice(case_kind, OVR_CASE_KINDS, default="admission", code="ovr_case_kind_invalid")
    clean_priority = _choice(priority, OVR_PRIORITIES, default="normal", code="ovr_priority_invalid")
    clean_classification = _choice(
        classification, OVR_CLASSIFICATIONS, default="restricted", code="ovr_classification_invalid"
    )
    clean_objective = _clean(objective, maximum=3000, code="ovr_objective_too_long")
    clean_forum = _url(forum_url, code="ovr_forum_url_invalid")
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    due_at = (now_dt + timedelta(hours=48)).isoformat()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        latest = con.execute(
            "SELECT MAX(case_number) AS n FROM ovr_cases WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchone()
        number = int(latest["n"] or 0) + 1
        cursor = con.execute(
            """
            INSERT INTO ovr_cases(
                guild_id, case_number, first_name, last_name, static_id,
                discord_text, discord_user_id, forum_url, additional_info,
                case_kind, priority, classification, objective,
                created_by_id, created_by_display, due_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(guild_id), number, first, last, static, discord_value,
                int(discord_user_id) if discord_user_id else None, clean_forum,
                additional or None, clean_kind, clean_priority, clean_classification,
                clean_objective or None, int(actor_id), str(actor_display)[:200],
                due_at, now, now,
            ),
        )
        case_id = int(cursor.lastrowid)
        _event(
            con, guild_id=guild_id, case_id=case_id, actor_id=actor_id,
            actor_display=actor_display, action="created",
            note="Расследование зарегистрировано. Контрольный срок — 48 часов.", now=now,
        )
        row = con.execute("SELECT * FROM ovr_cases WHERE id = ?", (case_id,)).fetchone()
        con.commit()
    return _decorate_case(row)


def list_cases(
    guild_id: int,
    *,
    actor_id: int | None = None,
    full_access: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    where = "WHERE c.guild_id = ?"
    values: list[Any] = [int(guild_id)]
    if not full_access:
        where += " AND c.created_by_id = ?"
        values.append(int(actor_id or 0))
    values.append(max(1, min(int(limit), 400)))
    with connect_readonly() as con:
        rows = con.execute(
            f"""
            SELECT c.*,
              (SELECT COUNT(*) FROM ovr_case_materials m WHERE m.case_id = c.id AND m.active = 1) AS material_count,
              (SELECT COUNT(*) FROM ovr_case_relations r WHERE r.case_id = c.id AND r.active = 1) AS relation_count,
              (SELECT COUNT(*) FROM ovr_case_tasks t WHERE t.case_id = c.id AND t.status != 'archived') AS task_count,
              (SELECT COUNT(*) FROM ovr_case_tasks t WHERE t.case_id = c.id AND t.status = 'done') AS task_done_count
            FROM ovr_cases c {where}
            ORDER BY
              CASE c.status WHEN 'screening' THEN 0 WHEN 'analysis' THEN 1
                WHEN 'decision' THEN 2 WHEN 'new' THEN 3 WHEN 'needs_info' THEN 4 ELSE 5 END,
              CASE c.priority WHEN 'critical' THEN 0 WHEN 'urgent' THEN 1 WHEN 'important' THEN 2 ELSE 3 END,
              c.due_at ASC, c.case_number DESC
            LIMIT ?
            """,
            values,
        ).fetchall()
    result = [_decorate_case(row) for row in rows]
    if not full_access:
        for item in result:
            for field in (
                "nowa_links", "findings", "hypothesis", "executive_summary",
                "risk_level", "decision_reason", "assigned_to_id",
            ):
                item.pop(field, None)
    return result


def case_events(case_id: int, *, guild_id: int | None = None) -> list[dict[str, Any]]:
    where = "case_id = ?"
    values: list[Any] = [int(case_id)]
    if guild_id is not None:
        where += " AND guild_id = ?"
        values.append(int(guild_id))
    with connect_readonly() as con:
        rows = con.execute(
            f"SELECT * FROM ovr_case_events WHERE {where} ORDER BY id ASC", values
        ).fetchall()
    return [dict(row) for row in rows]


def case_detail(case_id: int, *, guild_id: int) -> dict[str, Any]:
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT c.*,
              (SELECT COUNT(*) FROM ovr_case_materials m WHERE m.case_id = c.id AND m.active = 1) AS material_count,
              (SELECT COUNT(*) FROM ovr_case_relations r WHERE r.case_id = c.id AND r.active = 1) AS relation_count,
              (SELECT COUNT(*) FROM ovr_case_tasks t WHERE t.case_id = c.id AND t.status != 'archived') AS task_count,
              (SELECT COUNT(*) FROM ovr_case_tasks t WHERE t.case_id = c.id AND t.status = 'done') AS task_done_count
            FROM ovr_cases c WHERE c.id = ? AND c.guild_id = ?
            """,
            (int(case_id), int(guild_id)),
        ).fetchone()
        if row is None:
            raise ValueError("ovr_case_not_found")
        materials = con.execute(
            "SELECT * FROM ovr_case_materials WHERE case_id = ? AND active = 1 ORDER BY id DESC",
            (int(case_id),),
        ).fetchall()
        relations = con.execute(
            "SELECT * FROM ovr_case_relations WHERE case_id = ? AND active = 1 ORDER BY id DESC",
            (int(case_id),),
        ).fetchall()
        tasks = con.execute(
            "SELECT * FROM ovr_case_tasks WHERE case_id = ? AND status != 'archived' ORDER BY CASE status WHEN 'doing' THEN 0 WHEN 'blocked' THEN 1 WHEN 'todo' THEN 2 ELSE 3 END, id DESC",
            (int(case_id),),
        ).fetchall()
        events = con.execute(
            "SELECT * FROM ovr_case_events WHERE case_id = ? ORDER BY id ASC",
            (int(case_id),),
        ).fetchall()
    return {
        "case": _decorate_case(row),
        "materials": [dict(item) for item in materials],
        "relations": [dict(item) for item in relations],
        "tasks": [dict(item) for item in tasks],
        "events": [dict(item) for item in events],
    }


def update_case(
    case_id: int,
    *,
    guild_id: int,
    expected_revision: int,
    action: str,
    actor_id: int,
    actor_display: str,
    note: str = "",
    findings: str | None = None,
    nowa_links: str | None = None,
    risk_level: str | None = None,
    objective: str | None = None,
    executive_summary: str | None = None,
    hypothesis: str | None = None,
    aliases: str | None = None,
    affiliations: str | None = None,
    priority: str | None = None,
    classification: str | None = None,
) -> dict[str, Any]:
    clean_action = str(action).strip().lower()
    allowed_actions = {
        "claim", "note", "needs_info", "analysis", "decision", "approve",
        "deny", "update", "reopen", "archive",
    }
    if clean_action not in allowed_actions:
        raise ValueError("ovr_action_invalid")
    clean_note = _clean(note, maximum=5000, code="ovr_note_too_long")
    if clean_action in {"note", "needs_info", "approve", "deny", "reopen"} and len(clean_note) < 3:
        raise ValueError("ovr_note_required")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = _case_for_write(
            con, case_id, guild_id, expected_revision,
            allow_closed=clean_action in {"note", "reopen", "archive"},
        )
        old_status = str(row["status"])
        allowed_from = OVR_ACTION_TRANSITIONS.get(clean_action)
        if allowed_from is not None and old_status not in allowed_from:
            raise ValueError("ovr_case_transition_invalid")
        status = old_status
        decision_value = row["decision"]
        decision_reason = row["decision_reason"]
        decided_at = row["decided_at"]
        assigned_id = row["assigned_to_id"]
        assigned_display = row["assigned_to_display"]
        if clean_action == "claim":
            status = "screening"
            assigned_id = int(actor_id)
            assigned_display = str(actor_display)[:200]
        elif clean_action == "needs_info":
            status = "needs_info"
        elif clean_action == "analysis":
            status = "analysis"
        elif clean_action == "decision":
            status = "decision"
        elif clean_action in {"approve", "deny"}:
            status = "approved" if clean_action == "approve" else "denied"
            decision_value = status
            decision_reason = clean_note
            decided_at = now
        elif clean_action == "reopen":
            if old_status not in {"approved", "denied", "archived"}:
                raise ValueError("ovr_case_not_closed")
            status = "screening"
            decision_value = None
            decision_reason = None
            decided_at = None
        elif clean_action == "archive":
            if old_status not in {"approved", "denied"}:
                raise ValueError("ovr_case_not_decided")
            status = "archived"

        values = {
            "findings": _clean(findings if findings is not None else row["findings"], maximum=12000, code="ovr_dossier_too_long"),
            "nowa_links": _clean(nowa_links if nowa_links is not None else row["nowa_links"], maximum=5000, code="ovr_dossier_too_long"),
            "objective": _clean(objective if objective is not None else row["objective"], maximum=3000, code="ovr_objective_too_long"),
            "executive_summary": _clean(executive_summary if executive_summary is not None else row["executive_summary"], maximum=5000, code="ovr_summary_too_long"),
            "hypothesis": _clean(hypothesis if hypothesis is not None else row["hypothesis"], maximum=5000, code="ovr_hypothesis_too_long"),
            "aliases": _clean(aliases if aliases is not None else row["aliases"], maximum=1000, code="ovr_aliases_too_long"),
            "affiliations": _clean(affiliations if affiliations is not None else row["affiliations"], maximum=3000, code="ovr_affiliations_too_long"),
        }
        selected_risk = _choice(
            risk_level if risk_level is not None else row["risk_level"],
            OVR_RISKS, default="unrated", code="ovr_risk_invalid",
        )
        selected_priority = _choice(
            priority if priority is not None else row["priority"],
            OVR_PRIORITIES, default="normal", code="ovr_priority_invalid",
        )
        selected_classification = _choice(
            classification if classification is not None else row["classification"],
            OVR_CLASSIFICATIONS, default="restricted", code="ovr_classification_invalid",
        )
        cursor = con.execute(
            """
            UPDATE ovr_cases
            SET status = ?, decision = ?, decision_reason = ?, decided_at = ?,
                assigned_to_id = ?, assigned_to_display = ?, findings = ?, nowa_links = ?,
                objective = ?, executive_summary = ?, hypothesis = ?, aliases = ?, affiliations = ?,
                risk_level = ?, priority = ?, classification = ?,
                revision = revision + 1, updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (
                status, decision_value, decision_reason, decided_at, assigned_id,
                assigned_display, values["findings"] or None, values["nowa_links"] or None,
                values["objective"] or None, values["executive_summary"] or None,
                values["hypothesis"] or None, values["aliases"] or None,
                values["affiliations"] or None, selected_risk, selected_priority,
                selected_classification, now, int(case_id), int(expected_revision),
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("ovr_case_revision_conflict")
        default_notes = {
            "claim": "Расследование принято в работу.",
            "update": "Аналитическая карточка обновлена.",
            "analysis": "Материалы переданы на аналитическую оценку.",
            "decision": "Расследование подготовлено к решению.",
            "archive": "Завершённое расследование перенесено в архив.",
        }
        _event(
            con, guild_id=guild_id, case_id=case_id, actor_id=actor_id,
            actor_display=actor_display, action=clean_action,
            note=clean_note or default_notes.get(clean_action), now=now,
        )
        updated = con.execute("SELECT * FROM ovr_cases WHERE id = ?", (int(case_id),)).fetchone()
        con.commit()
    return _decorate_case(updated)


def add_material(
    case_id: int, *, guild_id: int, expected_revision: int, actor_id: int,
    actor_display: str, kind: str, title: str, content: str = "",
    source_url: str = "", reliability: str = "unrated",
) -> dict[str, Any]:
    clean_kind = _choice(kind, OVR_MATERIAL_KINDS, default="document", code="ovr_material_kind_invalid")
    clean_title = _clean(title, minimum=2, maximum=200, code="ovr_material_title_invalid")
    clean_content = _clean(content, maximum=8000, code="ovr_material_content_too_long")
    clean_url = _url(source_url)
    if not clean_content and not clean_url:
        raise ValueError("ovr_material_empty")
    clean_reliability = _choice(
        reliability, OVR_RELIABILITY, default="unrated", code="ovr_reliability_invalid"
    )
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        _case_for_write(con, case_id, guild_id, expected_revision)
        cursor = con.execute(
            """
            INSERT INTO ovr_case_materials(
                guild_id, case_id, kind, title, content, source_url, reliability,
                created_by_id, created_by_display, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(guild_id), int(case_id), clean_kind, clean_title,
                clean_content or None, clean_url, clean_reliability, int(actor_id),
                str(actor_display)[:200], now, now,
            ),
        )
        _bump_case(con, case_id, expected_revision, now)
        _event(
            con, guild_id=guild_id, case_id=case_id, actor_id=actor_id,
            actor_display=actor_display, action="material_added",
            note=f"Добавлен материал: {clean_title}", now=now,
        )
        material_id = int(cursor.lastrowid)
        con.commit()
    return case_detail(case_id, guild_id=guild_id) | {"changed_id": material_id}


def set_material_status(
    case_id: int, *, guild_id: int, expected_revision: int, material_id: int,
    status: str, actor_id: int, actor_display: str,
) -> dict[str, Any]:
    clean_status = _choice(
        status, OVR_MATERIAL_STATUSES, default="new", code="ovr_material_status_invalid"
    )
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        _case_for_write(con, case_id, guild_id, expected_revision)
        material = con.execute(
            "SELECT * FROM ovr_case_materials WHERE id = ? AND case_id = ? AND active = 1",
            (int(material_id), int(case_id)),
        ).fetchone()
        if material is None:
            raise ValueError("ovr_material_not_found")
        con.execute(
            "UPDATE ovr_case_materials SET status = ?, updated_at = ? WHERE id = ?",
            (clean_status, now, int(material_id)),
        )
        _bump_case(con, case_id, expected_revision, now)
        _event(
            con, guild_id=guild_id, case_id=case_id, actor_id=actor_id,
            actor_display=actor_display, action="material_status",
            note=f"Материал «{material['title']}»: {clean_status}.", now=now,
        )
        con.commit()
    return case_detail(case_id, guild_id=guild_id)


def add_relation(
    case_id: int, *, guild_id: int, expected_revision: int, actor_id: int,
    actor_display: str, person_name: str, relation_type: str,
    static_id: str = "", discord_text: str = "", details: str = "",
    confidence: str = "unrated",
) -> dict[str, Any]:
    name = _clean(person_name, minimum=2, maximum=160, code="ovr_relation_name_invalid")
    relation = _clean(relation_type, minimum=2, maximum=120, code="ovr_relation_type_invalid")
    static = _clean(static_id, maximum=12, code="ovr_relation_static_invalid")
    if static and not static.isdigit():
        raise ValueError("ovr_relation_static_invalid")
    discord_value = _clean(discord_text, maximum=200, code="ovr_relation_discord_invalid")
    clean_details = _clean(details, maximum=3000, code="ovr_relation_details_too_long")
    clean_confidence = _choice(
        confidence, OVR_RELIABILITY, default="unrated", code="ovr_confidence_invalid"
    )
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        _case_for_write(con, case_id, guild_id, expected_revision)
        cursor = con.execute(
            """
            INSERT INTO ovr_case_relations(
                guild_id, case_id, person_name, relation_type, static_id,
                discord_text, details, confidence, created_by_id,
                created_by_display, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(guild_id), int(case_id), name, relation, static or None,
                discord_value or None, clean_details or None, clean_confidence,
                int(actor_id), str(actor_display)[:200], now, now,
            ),
        )
        _bump_case(con, case_id, expected_revision, now)
        _event(
            con, guild_id=guild_id, case_id=case_id, actor_id=actor_id,
            actor_display=actor_display, action="relation_added",
            note=f"Добавлена связь: {name} — {relation}.", now=now,
        )
        relation_id = int(cursor.lastrowid)
        con.commit()
    return case_detail(case_id, guild_id=guild_id) | {"changed_id": relation_id}


def add_task(
    case_id: int, *, guild_id: int, expected_revision: int, actor_id: int,
    actor_display: str, title: str, description: str = "", priority: str = "normal",
    assignee_id: int | None = None, assignee_display: str = "", due_at: str = "",
) -> dict[str, Any]:
    clean_title = _clean(title, minimum=2, maximum=240, code="ovr_task_title_invalid")
    clean_description = _clean(description, maximum=4000, code="ovr_task_description_too_long")
    clean_priority = _choice(priority, OVR_PRIORITIES, default="normal", code="ovr_priority_invalid")
    clean_assignee = _clean(assignee_display, maximum=200, code="ovr_task_assignee_invalid")
    clean_due = _due_at(due_at)
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        _case_for_write(con, case_id, guild_id, expected_revision)
        cursor = con.execute(
            """
            INSERT INTO ovr_case_tasks(
                guild_id, case_id, title, description, priority, assignee_id,
                assignee_display, due_at, created_by_id, created_by_display,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(guild_id), int(case_id), clean_title, clean_description or None,
                clean_priority, int(assignee_id) if assignee_id else None,
                clean_assignee or None, clean_due, int(actor_id),
                str(actor_display)[:200], now, now,
            ),
        )
        _bump_case(con, case_id, expected_revision, now)
        _event(
            con, guild_id=guild_id, case_id=case_id, actor_id=actor_id,
            actor_display=actor_display, action="task_added",
            note=f"Поставлена задача: {clean_title}", now=now,
        )
        task_id = int(cursor.lastrowid)
        con.commit()
    return case_detail(case_id, guild_id=guild_id) | {"changed_id": task_id}


def set_task_status(
    case_id: int, *, guild_id: int, expected_revision: int, task_id: int,
    status: str, actor_id: int, actor_display: str,
) -> dict[str, Any]:
    clean_status = _choice(status, OVR_TASK_STATUSES, default="todo", code="ovr_task_status_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        _case_for_write(con, case_id, guild_id, expected_revision)
        task = con.execute(
            "SELECT * FROM ovr_case_tasks WHERE id = ? AND case_id = ? AND status != 'archived'",
            (int(task_id), int(case_id)),
        ).fetchone()
        if task is None:
            raise ValueError("ovr_task_not_found")
        completed_at = now if clean_status == "done" else None
        con.execute(
            "UPDATE ovr_case_tasks SET status = ?, completed_at = ?, updated_at = ? WHERE id = ?",
            (clean_status, completed_at, now, int(task_id)),
        )
        _bump_case(con, case_id, expected_revision, now)
        _event(
            con, guild_id=guild_id, case_id=case_id, actor_id=actor_id,
            actor_display=actor_display, action="task_status",
            note=f"Задача «{task['title']}»: {clean_status}.", now=now,
        )
        con.commit()
    return case_detail(case_id, guild_id=guild_id)


__all__ = [
    "OVR_CASE_KINDS", "OVR_CLASSIFICATIONS", "OVR_PRIORITIES", "OVR_RISKS",
    "OVR_STATUSES", "add_material", "add_relation", "add_task", "case_detail",
    "case_events", "create_case", "list_cases", "set_material_status",
    "set_task_status", "update_case",
]
