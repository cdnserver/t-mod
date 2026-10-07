"""Durable admission pipeline from a T-Mod account to a consensus decision."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso
from persistence.outbox_repository import delivery_outbox_enqueue_in_connection


ADMISSION_DELIVERY_TOPIC = "tmod.admission.delivery.v1"
ADMISSION_KINDS = frozenset({"community", "senate"})
ADMISSION_OPEN_STATUSES = frozenset(
    {"chair_review", "ovr_review", "ovr_approved", "consensus_queued"}
)
ADMISSION_TERMINAL_STATUSES = frozenset(
    {"ovr_denied", "membership_approved", "membership_denied"}
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: Any, fallback: Any) -> Any:
    try:
        decoded = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return decoded if isinstance(decoded, type(fallback)) else fallback


def _clean_text(
    value: Any,
    *,
    minimum: int = 0,
    maximum: int,
    code: str,
) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) < minimum or len(text) > maximum:
        raise ValueError(code)
    return text


def _forum_url(value: Any, *, required: bool = True) -> str:
    url = str(value or "").strip()
    if not url and not required:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(url) > 1000:
        raise ValueError("admission_forum_url_invalid")
    return url


def _characters(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise ValueError("admission_characters_invalid")
    normalized: list[dict[str, str]] = []
    seen_statics: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("admission_character_invalid")
        nickname = _clean_text(
            str(raw.get("nickname") or "").replace("_", " "),
            minimum=3,
            maximum=80,
            code="admission_character_nickname_invalid",
        )
        if len(nickname.split()) < 2:
            raise ValueError("admission_character_nickname_invalid")
        static_id = str(raw.get("static_id") or "").strip().lstrip("#")
        if (
            not static_id.isascii()
            or not static_id.isdigit()
            or not 1 <= len(static_id) <= 12
        ):
            raise ValueError("admission_character_static_invalid")
        static_id = str(int(static_id))
        if static_id in seen_statics:
            raise ValueError("admission_character_static_duplicate")
        seen_statics.add(static_id)
        normalized.append({"nickname": nickname, "static_id": static_id})
    return normalized


def _row_payload(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["characters"] = _decode(item.pop("characters_json", "[]"), [])
    item["answers"] = _decode(item.pop("answers_json", "{}"), {})
    item["traits"] = _decode(item.pop("traits_json", "{}"), {})
    return item


def _event(
    con: Any,
    *,
    guild_id: int,
    application_id: int,
    actor_id: int,
    actor_display: str,
    action: str,
    from_status: str | None,
    to_status: str,
    note: str | None,
    now: str,
) -> None:
    con.execute(
        """
        INSERT INTO membership_application_events(
            guild_id, application_id, actor_id, actor_display, action,
            from_status, to_status, note, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(guild_id),
            int(application_id),
            int(actor_id),
            str(actor_display)[:200],
            str(action)[:80],
            str(from_status)[:40] if from_status else None,
            str(to_status)[:40],
            str(note)[:5000] if note else None,
            now,
        ),
    )


def _enqueue_projection(
    con: Any,
    *,
    application: dict[str, Any],
    event: str,
    title: str,
    body: str,
    now: str,
    case_number: int | None = None,
    bill_number: int | None = None,
) -> None:
    base = {
        "payload_version": 1,
        "guild_id": int(application["guild_id"]),
        "application_id": int(application["id"]),
        "user_id": int(application["user_id"]),
        "user_display": str(application.get("user_display") or "Кандидат"),
        "event": str(event),
        "status": str(application.get("status") or "ovr_review"),
        "application_kind": str(application.get("application_kind") or "senate"),
        "title": str(title)[:200],
        "body": str(body)[:3500],
        "case_number": int(case_number) if case_number else None,
        "bill_number": int(bill_number) if bill_number else None,
        "route": "https://phx.tvr.lat/admission",
    }
    generation = int(application.get("revision") or 1)
    for kind, priority in (("dm", 80), ("log", 60)):
        delivery_outbox_enqueue_in_connection(
            con,
            topic=ADMISSION_DELIVERY_TOPIC,
            dedupe_key=(
                f"admission:{int(application['id'])}:{generation}:{event}:{kind}"
            ),
            payload={**base, "kind": kind},
            max_attempts=12,
            priority=priority,
            now=now,
        )


def submit_application(
    *,
    guild_id: int,
    user_id: int,
    user_display: str,
    forum_url: str,
    characters: Any,
    answers: Any,
    traits: Any,
    motivation: str,
    contribution: str,
    availability: str,
    application_kind: str = "senate",
) -> dict[str, Any]:
    clean_kind = str(application_kind or "").strip().lower()
    if clean_kind not in ADMISSION_KINDS:
        raise ValueError("admission_kind_invalid")
    clean_characters = _characters(characters)
    if clean_kind == "senate" and (not isinstance(answers, dict) or not answers):
        raise ValueError("admission_answers_invalid")
    if clean_kind == "senate" and (not isinstance(traits, dict) or not traits):
        raise ValueError("admission_traits_invalid")
    clean_answers = answers if isinstance(answers, dict) else {}
    clean_traits = traits if isinstance(traits, dict) else {}
    clean_forum = _forum_url(forum_url, required=clean_kind == "senate")
    clean_display = _clean_text(
        user_display,
        minimum=2,
        maximum=200,
        code="admission_user_display_invalid",
    )
    clean_motivation = _clean_text(
        motivation,
        minimum=10 if clean_kind == "community" else 20,
        maximum=2000,
        code="admission_motivation_invalid",
    )
    clean_contribution = _clean_text(
        contribution,
        minimum=10,
        maximum=1500,
        code="admission_contribution_invalid",
    )
    clean_availability = _clean_text(
        availability,
        minimum=3,
        maximum=500,
        code="admission_availability_invalid",
    )
    primary = clean_characters[0]
    name_parts = primary["nickname"].split()
    first_name, last_name = name_parts[0], " ".join(name_parts[1:])
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    due_at = (now_dt + timedelta(hours=48)).isoformat()
    character_lines = "\n".join(
        f"{index}. {item['nickname']} · #{item['static_id']}"
        for index, item in enumerate(clean_characters, start=1)
    )
    trait_line = ", ".join(
        f"{str(key)}: {int(value)}%"
        for key, value in clean_traits.items()
        if isinstance(value, (int, float))
    )
    additional = (
        "Заявка подана через единый портал Phoenix.\n\n"
        f"Персонажи:\n{character_lines}\n\n"
        f"Мотивация: {clean_motivation}\n"
        f"Вклад: {clean_contribution}\n"
        f"Доступность: {clean_availability}\n"
        f"Профиль ситуационного теста: {trait_line or 'сформирован'}"
    )
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT * FROM membership_applications WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        upgrading = bool(
            existing is not None
            and str(existing["application_kind"] or "senate") == "community"
            and str(existing["status"] or "") == "membership_approved"
            and clean_kind == "senate"
        )
        if existing is not None and not upgrading:
            con.commit()
            raise ValueError("admission_application_already_exists")
        initial_status = "chair_review" if clean_kind == "community" else "ovr_review"
        if upgrading:
            application_id = int(existing["id"])
            revision = int(existing["revision"] or 1) + 1
            con.execute(
                """
                UPDATE membership_applications
                SET application_kind = 'senate', user_display = ?, forum_url = ?,
                    characters_json = ?, answers_json = ?, traits_json = ?,
                    motivation = ?, contribution = ?, availability = ?,
                    status = 'ovr_review', ovr_case_id = NULL,
                    submitted_bill_id = NULL, submitted_bill_number = NULL,
                    decision_note = NULL, consensus_result = NULL,
                    ovr_decided_at = NULL, leadership_decided_at = NULL,
                    consensus_decided_at = NULL, revision = ?, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (
                    clean_display,
                    clean_forum,
                    _json(clean_characters),
                    _json(clean_answers),
                    _json(clean_traits),
                    clean_motivation,
                    clean_contribution,
                    clean_availability,
                    revision,
                    now,
                    application_id,
                    int(existing["revision"] or 1),
                ),
            )
        else:
            inserted = con.execute(
                """
                INSERT INTO membership_applications(
                    guild_id, user_id, user_display, application_kind,
                    forum_url, characters_json, answers_json, traits_json,
                    motivation, contribution, availability, status,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(guild_id),
                    int(user_id),
                    clean_display,
                    clean_kind,
                    clean_forum,
                    _json(clean_characters),
                    _json(clean_answers),
                    _json(clean_traits),
                    clean_motivation,
                    clean_contribution,
                    clean_availability,
                    initial_status,
                    now,
                    now,
                ),
            )
            application_id = int(inserted.lastrowid)

        if clean_kind == "community":
            _event(
                con,
                guild_id=guild_id,
                application_id=application_id,
                actor_id=user_id,
                actor_display=clean_display,
                action="community_submitted",
                from_status=None,
                to_status="chair_review",
                note="Заявка передана Совету председателей.",
                now=now,
            )
            row = con.execute(
                "SELECT * FROM membership_applications WHERE id = ?",
                (application_id,),
            ).fetchone()
            _enqueue_projection(
                con,
                application=dict(row),
                event="community_submitted",
                title="Заявка в Товарищество зарегистрирована",
                body=(
                    "Совет председателей получил вашу заявку. Решение будет "
                    "принято без пленарного консенсуса и появится на этой странице."
                ),
                now=now,
            )
            con.commit()
            return application_detail(guild_id, user_id)

        latest = con.execute(
            "SELECT MAX(case_number) AS n FROM ovr_cases WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchone()
        case_number = int(latest["n"] or 0) + 1
        case_cursor = con.execute(
            """
            INSERT INTO ovr_cases(
                guild_id, case_number, first_name, last_name, static_id,
                discord_text, discord_user_id, forum_url, additional_info,
                case_kind, priority, classification, objective,
                created_by_id, created_by_display, due_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'admission', 'important',
                     'restricted', ?, ?, ?, ?, ?, ?)
            """,
            (
                int(guild_id),
                case_number,
                first_name,
                last_name,
                primary["static_id"],
                f"{clean_display} · {int(user_id)}",
                int(user_id),
                clean_forum,
                additional,
                "Установить обстоятельства, риски и возможность допуска кандидата "
                "к рассмотрению на пленарном консенсусе Товарищества.",
                int(user_id),
                clean_display,
                due_at,
                now,
                now,
            ),
        )
        case_id = int(case_cursor.lastrowid)
        con.execute(
            "UPDATE membership_applications SET ovr_case_id = ? WHERE id = ?",
            (case_id, application_id),
        )
        con.execute(
            """
            INSERT INTO ovr_case_events(
                guild_id, case_id, actor_id, actor_display, action, note, created_at
            ) VALUES(?, ?, ?, ?, 'admission_submitted', ?, ?)
            """,
            (
                int(guild_id),
                case_id,
                int(user_id),
                clean_display,
                "Заявка Phoenix автоматически передана в ОВР. Контрольный срок — 48 часов.",
                now,
            ),
        )
        baseline_tasks = (
            (
                "Проверить форумный профиль",
                "Сверить историю, публикации и сведения кандидата.",
            ),
            (
                "Проверить связи и репутационные риски",
                "Зафиксировать подтверждённые связи и значимые обстоятельства.",
            ),
            (
                "Подготовить мотивированное решение",
                "Отделить факты от предположений и сформировать вывод ОВР.",
            ),
        )
        for title, description in baseline_tasks:
            con.execute(
                """
                INSERT INTO ovr_case_tasks(
                    guild_id, case_id, title, description, status, priority,
                    created_by_id, created_by_display, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'todo', 'important', ?, 'T-Mod · Phoenix', ?, ?)
                """,
                (
                    int(guild_id),
                    case_id,
                    title,
                    description,
                    int(user_id),
                    now,
                    now,
                ),
            )
        _event(
            con,
            guild_id=guild_id,
            application_id=application_id,
            actor_id=user_id,
            actor_display=clean_display,
            action="senate_upgrade" if upgrading else "submitted",
            from_status="membership_approved" if upgrading else None,
            to_status="ovr_review",
            note=(
                f"Открыта сенатская траектория и расследование ОВР-{case_number:03d}."
                if upgrading
                else f"Создано расследование ОВР-{case_number:03d}."
            ),
            now=now,
        )
        row = con.execute(
            "SELECT * FROM membership_applications WHERE id = ?",
            (application_id,),
        ).fetchone()
        application = dict(row)
        _enqueue_projection(
            con,
            application=application,
            event="submitted",
            title="Заявка принята в контур Phoenix",
            body=(
                f"Заявка зарегистрирована как проверка ОВР-{case_number:03d}. "
                "ОВР изучит сведения в срок до 48 часов. Все изменения будут "
                "приходить в ЛС и отображаться на странице заявки."
            ),
            case_number=case_number,
            now=now,
        )
        con.commit()
    return application_detail(guild_id, user_id)


def application_detail(guild_id: int, user_id: int) -> dict[str, Any] | None:
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT a.*, c.case_number AS ovr_case_number
            FROM membership_applications AS a
            LEFT JOIN ovr_cases AS c ON c.id = a.ovr_case_id
            WHERE a.guild_id = ? AND a.user_id = ?
            """,
            (int(guild_id), int(user_id)),
        ).fetchone()
        if row is None:
            return None
        events = con.execute(
            """
            SELECT * FROM membership_application_events
            WHERE application_id = ? ORDER BY id ASC
            """,
            (int(row["id"]),),
        ).fetchall()
    return {
        "application": _row_payload(row),
        "events": [dict(event) for event in events],
    }


def application_for_case(case_id: int, *, guild_id: int) -> dict[str, Any] | None:
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT a.*, c.case_number AS ovr_case_number
            FROM membership_applications AS a
            LEFT JOIN ovr_cases AS c ON c.id = a.ovr_case_id
            WHERE a.guild_id = ? AND a.ovr_case_id = ?
            """,
            (int(guild_id), int(case_id)),
        ).fetchone()
    return _row_payload(row) if row is not None else None


def pending_leadership_applications(
    guild_id: int, *, limit: int = 100
) -> list[dict[str, Any]]:
    """Return the Fellowship applications awaiting a chair decision."""

    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT * FROM membership_applications
            WHERE guild_id = ? AND application_kind = 'community'
              AND status = 'chair_review'
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (int(guild_id), max(1, min(int(limit), 300))),
        ).fetchall()
    return [_row_payload(row) for row in rows]


def approved_community_applications(
    guild_id: int, *, limit: int = 300
) -> list[dict[str, Any]]:
    """Return approved Fellowship identities for idempotent role projection."""

    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT * FROM membership_applications
            WHERE guild_id = ? AND application_kind = 'community'
              AND status = 'membership_approved'
            ORDER BY updated_at ASC, id ASC
            LIMIT ?
            """,
            (int(guild_id), max(1, min(int(limit), 1000))),
        ).fetchall()
    return [_row_payload(row) for row in rows]


def record_leadership_decision(
    application_id: int,
    *,
    guild_id: int,
    approved: bool,
    actor_id: int,
    actor_display: str,
    note: str,
) -> tuple[dict[str, Any], bool]:
    clean_note = _clean_text(
        note,
        minimum=5,
        maximum=5000,
        code="admission_decision_note_required",
    )
    target = "membership_approved" if approved else "membership_denied"
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM membership_applications WHERE id = ? AND guild_id = ?",
            (int(application_id), int(guild_id)),
        ).fetchone()
        if row is None:
            raise ValueError("admission_application_not_found")
        if str(row["application_kind"] or "senate") != "community":
            raise ValueError("admission_leadership_kind_invalid")
        if str(row["status"] or "") == target:
            con.commit()
            return _row_payload(row), False
        if str(row["status"] or "") != "chair_review":
            raise ValueError("admission_leadership_transition_invalid")
        revision = int(row["revision"] or 1) + 1
        con.execute(
            """
            UPDATE membership_applications
            SET status = ?, decision_note = ?, leadership_decided_at = ?,
                revision = ?, updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (
                target,
                clean_note,
                now,
                revision,
                now,
                int(application_id),
                int(row["revision"] or 1),
            ),
        )
        _event(
            con,
            guild_id=guild_id,
            application_id=application_id,
            actor_id=actor_id,
            actor_display=actor_display,
            action="leadership_approved" if approved else "leadership_denied",
            from_status="chair_review",
            to_status=target,
            note=clean_note,
            now=now,
        )
        updated_row = con.execute(
            "SELECT * FROM membership_applications WHERE id = ?",
            (int(application_id),),
        ).fetchone()
        updated = dict(updated_row)
        _enqueue_projection(
            con,
            application=updated,
            event="leadership_approved" if approved else "leadership_denied",
            title=(
                "Совет председателей принял вас в Товарищество"
                if approved
                else "Совет председателей рассмотрел вашу заявку"
            ),
            body=(
                "Заявка одобрена. Доступ участника Товарищества будет активирован "
                "автоматически. Право голоса в Сенате и сенатские преимущества "
                "в этот статус не входят."
                if approved
                else "Заявка в Товарищество отклонена. Мотивировка решения "
                "сохранена в личной карточке."
            ),
            now=now,
        )
        con.commit()
    return _row_payload(updated_row), True


def record_ovr_decision(
    *,
    guild_id: int,
    case_id: int,
    approved: bool,
    actor_id: int,
    actor_display: str,
    note: str,
) -> tuple[dict[str, Any] | None, bool]:
    target = "ovr_approved" if approved else "ovr_denied"
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM membership_applications WHERE guild_id = ? AND ovr_case_id = ?",
            (int(guild_id), int(case_id)),
        ).fetchone()
        if row is None:
            con.commit()
            return None, False
        current = str(row["status"])
        if current == target or current in {
            "consensus_queued",
            "membership_approved",
            "membership_denied",
        }:
            con.commit()
            return _row_payload(row), False
        if current != "ovr_review":
            con.rollback()
            raise ValueError("admission_ovr_transition_invalid")
        revision = int(row["revision"] or 1) + 1
        con.execute(
            """
            UPDATE membership_applications
            SET status = ?, decision_note = ?, ovr_decided_at = ?,
                revision = ?, updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (
                target,
                str(note)[:5000] or None,
                now,
                revision,
                now,
                int(row["id"]),
                int(row["revision"] or 1),
            ),
        )
        _event(
            con,
            guild_id=guild_id,
            application_id=int(row["id"]),
            actor_id=actor_id,
            actor_display=actor_display,
            action="ovr_approved" if approved else "ovr_denied",
            from_status=current,
            to_status=target,
            note=note,
            now=now,
        )
        updated_row = con.execute(
            "SELECT * FROM membership_applications WHERE id = ?",
            (int(row["id"]),),
        ).fetchone()
        updated = dict(updated_row)
        case = con.execute(
            "SELECT case_number FROM ovr_cases WHERE id = ?",
            (int(case_id),),
        ).fetchone()
        case_number = int(case["case_number"]) if case else None
        updated["ovr_case_number"] = case_number
        _enqueue_projection(
            con,
            application=updated,
            event="ovr_approved" if approved else "ovr_denied",
            title="ОВР допустил заявку до консенсуса"
            if approved
            else "ОВР отказал в допуске в Сенат Phoenix",
            body=(
                "Проверка завершена положительно. T-Mod подготовит инициативу о "
                "вступлении и передаст её на пленарный консенсус."
                if approved
                else "ОВР не допустил заявку к рассмотрению консенсусом. "
                "Решение окончательно: кандидат не вступит в Сенат Majestic RP · "
                "Phoenix ни при каких обстоятельствах. Мотивировка сохранена в "
                "закрытом досье."
            ),
            case_number=case_number,
            now=now,
        )
        con.commit()
    return _row_payload(updated), True


def unreconciled_ovr_decisions(
    guild_id: int,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return final OVR decisions not yet projected into admission state.

    OVR writes and Phoenix orchestration intentionally use separate commits.
    This query closes the small crash window between those commits and makes a
    restart equivalent to a successful synchronous hand-off.
    """

    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT a.id AS application_id, a.ovr_case_id,
                   COALESCE(NULLIF(c.decision, ''), c.status) AS status,
                   c.decision_reason, c.assigned_to_id,
                   c.assigned_to_display, c.case_number
            FROM membership_applications AS a
            JOIN ovr_cases AS c ON c.id = a.ovr_case_id
            WHERE a.guild_id = ? AND a.status = 'ovr_review'
              AND (
                    c.status IN ('approved', 'denied')
                    OR (c.status = 'archived' AND c.decision IN ('approved', 'denied'))
                  )
            ORDER BY c.updated_at ASC, c.id ASC
            LIMIT ?
            """,
            (int(guild_id), max(1, min(int(limit), 300))),
        ).fetchall()
    return [dict(row) for row in rows]


def approved_without_bill(guild_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT a.*, c.case_number AS ovr_case_number
            FROM membership_applications AS a
            LEFT JOIN ovr_cases AS c ON c.id = a.ovr_case_id
            WHERE a.guild_id = ? AND a.status = 'ovr_approved'
              AND a.submitted_bill_id IS NULL
            ORDER BY a.updated_at ASC, a.id ASC LIMIT ?
            """,
            (int(guild_id), max(1, min(int(limit), 200))),
        ).fetchall()
    return [_row_payload(row) for row in rows]


def link_consensus_bill(
    application_id: int,
    *,
    guild_id: int,
    bill_id: int,
    bill_number: int,
    actor_id: int,
    actor_display: str,
) -> tuple[dict[str, Any], bool]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM membership_applications WHERE id = ? AND guild_id = ?",
            (int(application_id), int(guild_id)),
        ).fetchone()
        if row is None:
            raise ValueError("admission_application_not_found")
        if int(row["submitted_bill_id"] or 0) == int(bill_id):
            con.commit()
            return _row_payload(row), False
        if str(row["status"]) != "ovr_approved" or row["submitted_bill_id"] is not None:
            con.rollback()
            raise ValueError("admission_bill_transition_invalid")
        revision = int(row["revision"] or 1) + 1
        con.execute(
            """
            UPDATE membership_applications
            SET status = 'consensus_queued', submitted_bill_id = ?,
                submitted_bill_number = ?, revision = ?, updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (
                int(bill_id),
                int(bill_number),
                revision,
                now,
                int(application_id),
                int(row["revision"] or 1),
            ),
        )
        _event(
            con,
            guild_id=guild_id,
            application_id=application_id,
            actor_id=actor_id,
            actor_display=actor_display,
            action="consensus_queued",
            from_status="ovr_approved",
            to_status="consensus_queued",
            note=f"Создан законопроект №{int(bill_number):03d}.",
            now=now,
        )
        updated_row = con.execute(
            "SELECT * FROM membership_applications WHERE id = ?",
            (int(application_id),),
        ).fetchone()
        updated = dict(updated_row)
        case = con.execute(
            "SELECT case_number FROM ovr_cases WHERE id = ?",
            (int(row["ovr_case_id"] or 0),),
        ).fetchone()
        _enqueue_projection(
            con,
            application=updated,
            event="consensus_queued",
            title="Инициатива о вступлении передана на консенсус",
            body=(
                f"T-Mod создал законопроект №{int(bill_number):03d}. "
                "Решение примут участники Товарищества на пленарном консенсусе."
            ),
            case_number=int(case["case_number"]) if case else None,
            bill_number=bill_number,
            now=now,
        )
        con.commit()
    return _row_payload(updated_row), True


def pending_consensus_results(
    guild_id: int, *, limit: int = 100
) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT a.*, c.case_number AS ovr_case_number
            FROM membership_applications AS a
            LEFT JOIN ovr_cases AS c ON c.id = a.ovr_case_id
            WHERE a.guild_id = ? AND a.status = 'consensus_queued'
              AND a.submitted_bill_id IS NOT NULL
            ORDER BY a.updated_at ASC, a.id ASC LIMIT ?
            """,
            (int(guild_id), max(1, min(int(limit), 300))),
        ).fetchall()
    return [_row_payload(row) for row in rows]


def record_consensus_result(
    application_id: int,
    *,
    guild_id: int,
    result_status: str,
    actor_id: int,
    actor_display: str,
) -> tuple[dict[str, Any], bool]:
    clean_result = str(result_status).strip().lower()
    if clean_result not in {"accepted", "rejected", "vetoed"}:
        raise ValueError("admission_consensus_result_invalid")
    target = (
        "membership_approved" if clean_result == "accepted" else "membership_denied"
    )
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM membership_applications WHERE id = ? AND guild_id = ?",
            (int(application_id), int(guild_id)),
        ).fetchone()
        if row is None:
            raise ValueError("admission_application_not_found")
        if str(row["status"]) == target:
            con.commit()
            return _row_payload(row), False
        if str(row["status"]) != "consensus_queued":
            con.rollback()
            raise ValueError("admission_consensus_transition_invalid")
        revision = int(row["revision"] or 1) + 1
        con.execute(
            """
            UPDATE membership_applications
            SET status = ?, consensus_result = ?, consensus_decided_at = ?,
                revision = ?, updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (
                target,
                clean_result,
                now,
                revision,
                now,
                int(application_id),
                int(row["revision"] or 1),
            ),
        )
        _event(
            con,
            guild_id=guild_id,
            application_id=application_id,
            actor_id=actor_id,
            actor_display=actor_display,
            action="consensus_result",
            from_status="consensus_queued",
            to_status=target,
            note=f"Результат законопроекта: {clean_result}.",
            now=now,
        )
        updated_row = con.execute(
            "SELECT * FROM membership_applications WHERE id = ?",
            (int(application_id),),
        ).fetchone()
        updated = dict(updated_row)
        case = con.execute(
            "SELECT case_number FROM ovr_cases WHERE id = ?",
            (int(row["ovr_case_id"] or 0),),
        ).fetchone()
        _enqueue_projection(
            con,
            application=updated,
            event="membership_approved"
            if clean_result == "accepted"
            else "membership_denied",
            title=(
                "Товарищество приняло решение о вашем вступлении"
                if clean_result == "accepted"
                else "Товарищество завершило рассмотрение вашей кандидатуры"
            ),
            body=(
                "Инициатива принята. Один из сопредседателей свяжется с вами "
                "для завершения вступления и последующих шагов."
                if clean_result == "accepted"
                else "Инициатива не была принята на пленарном консенсусе. "
                "Решение и история рассмотрения сохранены в T-Mod."
            ),
            case_number=int(case["case_number"]) if case else None,
            bill_number=(
                int(row["submitted_bill_number"])
                if row["submitted_bill_number"]
                else None
            ),
            now=now,
        )
        con.commit()
    return _row_payload(updated_row), True


__all__ = [
    "ADMISSION_DELIVERY_TOPIC",
    "ADMISSION_KINDS",
    "ADMISSION_OPEN_STATUSES",
    "ADMISSION_TERMINAL_STATUSES",
    "application_detail",
    "application_for_case",
    "approved_community_applications",
    "approved_without_bill",
    "link_consensus_bill",
    "pending_consensus_results",
    "pending_leadership_applications",
    "record_consensus_result",
    "record_leadership_decision",
    "record_ovr_decision",
    "submit_application",
    "unreconciled_ovr_decisions",
]
