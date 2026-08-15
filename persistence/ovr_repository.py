"""Durable OVR candidate screening cases and their append-only timeline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


OVR_STATUSES = frozenset({"new", "screening", "needs_info", "approved", "denied"})
OVR_RISKS = frozenset({"unrated", "low", "medium", "high", "critical"})


def _clean(value: Any, *, minimum: int = 0, maximum: int = 1000, code: str) -> str:
    text = str(value or "").strip()
    if len(text) < minimum or len(text) > maximum:
        raise ValueError(code)
    return text


def _forum_url(value: Any) -> str | None:
    url = str(value or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(url) > 500:
        raise ValueError("ovr_forum_url_invalid")
    return url


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
) -> dict[str, Any]:
    first = _clean(first_name, minimum=2, maximum=80, code="ovr_name_invalid")
    last = _clean(last_name, minimum=2, maximum=80, code="ovr_name_invalid")
    static = _clean(static_id, minimum=1, maximum=12, code="ovr_static_invalid")
    if not static.isdigit():
        raise ValueError("ovr_static_invalid")
    discord_value = _clean(
        discord_text,
        minimum=2,
        maximum=200,
        code="ovr_discord_invalid",
    )
    additional = _clean(
        additional_info,
        maximum=3000,
        code="ovr_additional_too_long",
    )
    clean_forum = _forum_url(forum_url)
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
                created_by_id, created_by_display, due_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(guild_id),
                number,
                first,
                last,
                static,
                discord_value,
                int(discord_user_id) if discord_user_id else None,
                clean_forum,
                additional or None,
                int(actor_id),
                str(actor_display)[:200],
                due_at,
                now,
                now,
            ),
        )
        case_id = int(cursor.lastrowid)
        con.execute(
            """
            INSERT INTO ovr_case_events(
                guild_id, case_id, actor_id, actor_display, action, note, created_at
            ) VALUES(?, ?, ?, ?, 'created', ?, ?)
            """,
            (
                int(guild_id),
                case_id,
                int(actor_id),
                str(actor_display)[:200],
                "Досье передано в ОВР. Срок проверки — 48 часов.",
                now,
            ),
        )
        row = con.execute("SELECT * FROM ovr_cases WHERE id = ?", (case_id,)).fetchone()
        con.commit()
    return dict(row)


def list_cases(
    guild_id: int,
    *,
    actor_id: int | None = None,
    full_access: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    where = "WHERE guild_id = ?"
    values: list[Any] = [int(guild_id)]
    if not full_access:
        where += " AND created_by_id = ?"
        values.append(int(actor_id or 0))
    values.append(max(1, min(int(limit), 400)))
    with connect_readonly() as con:
        rows = con.execute(
            f"""
            SELECT * FROM ovr_cases {where}
            ORDER BY
              CASE status WHEN 'screening' THEN 0 WHEN 'new' THEN 1
                WHEN 'needs_info' THEN 2 ELSE 3 END,
              due_at ASC, case_number DESC
            LIMIT ?
            """,
            values,
        ).fetchall()
    result = [dict(row) for row in rows]
    if not full_access:
        for item in result:
            for field in (
                "nowa_links",
                "findings",
                "risk_level",
                "decision_reason",
                "assigned_to_id",
            ):
                item.pop(field, None)
    return result


def case_events(case_id: int) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            "SELECT * FROM ovr_case_events WHERE case_id = ? ORDER BY id ASC",
            (int(case_id),),
        ).fetchall()
    return [dict(row) for row in rows]


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
) -> dict[str, Any]:
    clean_action = str(action).strip().lower()
    if clean_action not in {"claim", "note", "needs_info", "approve", "deny", "update"}:
        raise ValueError("ovr_action_invalid")
    clean_note = str(note or "").strip()
    if clean_action in {"note", "needs_info", "approve", "deny"} and len(clean_note) < 3:
        raise ValueError("ovr_note_required")
    if len(clean_note) > 3000:
        raise ValueError("ovr_note_too_long")
    clean_risk = str(risk_level or "").strip().lower()
    if clean_risk and clean_risk not in OVR_RISKS:
        raise ValueError("ovr_risk_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM ovr_cases WHERE id = ? AND guild_id = ?",
            (int(case_id), int(guild_id)),
        ).fetchone()
        if row is None:
            con.rollback()
            raise ValueError("ovr_case_not_found")
        if int(row["revision"] or 0) != int(expected_revision):
            con.rollback()
            raise ValueError("ovr_case_revision_conflict")
        if str(row["status"]) in {"approved", "denied"}:
            con.rollback()
            raise ValueError("ovr_case_closed")
        status = str(row["status"])
        decision = row["decision"]
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
        elif clean_action in {"approve", "deny"}:
            status = "approved" if clean_action == "approve" else "denied"
            decision = status
            decision_reason = clean_note
            decided_at = now
        elif clean_action in {"note", "update"} and status == "new":
            status = "screening"
        clean_findings = str(findings if findings is not None else row["findings"] or "").strip()
        clean_nowa = str(nowa_links if nowa_links is not None else row["nowa_links"] or "").strip()
        if len(clean_findings) > 5000 or len(clean_nowa) > 3000:
            con.rollback()
            raise ValueError("ovr_dossier_too_long")
        selected_risk = clean_risk or str(row["risk_level"] or "unrated")
        con.execute(
            """
            UPDATE ovr_cases
            SET status = ?, decision = ?, decision_reason = ?, decided_at = ?,
                assigned_to_id = ?, assigned_to_display = ?,
                findings = ?, nowa_links = ?, risk_level = ?,
                revision = revision + 1, updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (
                status,
                decision,
                decision_reason,
                decided_at,
                assigned_id,
                assigned_display,
                clean_findings or None,
                clean_nowa or None,
                selected_risk,
                now,
                int(case_id),
                int(expected_revision),
            ),
        )
        con.execute(
            """
            INSERT INTO ovr_case_events(
                guild_id, case_id, actor_id, actor_display, action, note, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(guild_id),
                int(case_id),
                int(actor_id),
                str(actor_display)[:200],
                clean_action,
                clean_note or None,
                now,
            ),
        )
        updated = con.execute("SELECT * FROM ovr_cases WHERE id = ?", (int(case_id),)).fetchone()
        con.commit()
    return dict(updated)


__all__ = [
    "OVR_RISKS",
    "OVR_STATUSES",
    "case_events",
    "create_case",
    "list_cases",
    "update_case",
]
