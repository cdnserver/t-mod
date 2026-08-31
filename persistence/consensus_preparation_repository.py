"""Private preparation sheets for upcoming consensus bills.

The preparation domain is intentionally separate from ``tvrs_votes``.  A
preliminary position is a personal working note; it is never a formal vote and
is not visible in public consensus projections.
"""

from __future__ import annotations

import json
from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


PRELIMINARY_VOTES = frozenset({"yes", "no", "abstain"})
REVIEW_FLAG_KEYS = frozenset({"read_text", "verify_sources", "need_discussion"})
MAX_NOTES_LENGTH = 16_000
MAX_REASON_LENGTH = 2_400
MAX_QUESTIONS = 12
MAX_QUESTION_LENGTH = 1_000
MAX_QUESTION_ID_LENGTH = 96
MAX_DISPLAY_LENGTH = 160


class PreparationRevisionConflict(ValueError):
    """Raised when another tab has saved a newer private sheet."""


def _empty_sheet() -> dict[str, Any]:
    return {
        "questions": [],
        "notes": "",
        "preliminary_vote": None,
        "preliminary_vote_reason": "",
        "review_flags": {
            "read_text": False,
            "verify_sources": False,
            "need_discussion": False,
        },
        "source_bill_updated_at": None,
        "revision": 0,
        "created_at": None,
        "updated_at": None,
    }


def _clean_text(value: Any, *, field: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"preparation_{field}_invalid")
    clean = value.strip()
    if len(clean) > limit:
        raise ValueError(f"preparation_{field}_too_long")
    return clean


def _normalise_questions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("preparation_questions_invalid")
    if len(value) > MAX_QUESTIONS:
        raise ValueError("preparation_questions_too_many")
    questions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError("preparation_question_invalid")
        question = _clean_text(
            item.get("text"),
            field="question",
            limit=MAX_QUESTION_LENGTH,
        )
        # Empty rows are harmless in the UI and should not turn into permanent
        # blank entries in a personal sheet.
        if not question:
            continue
        raw_id = item.get("id")
        question_id = str(raw_id or f"question-{index}").strip()
        if not question_id or len(question_id) > MAX_QUESTION_ID_LENGTH:
            raise ValueError("preparation_question_id_invalid")
        if question_id in seen_ids:
            raise ValueError("preparation_question_id_duplicate")
        seen_ids.add(question_id)
        questions.append(
            {
                "id": question_id,
                "text": question,
                "resolved": bool(item.get("resolved")),
            }
        )
    return questions


def _normalise_review_flags(value: Any) -> dict[str, bool]:
    if value is None:
        return dict(_empty_sheet()["review_flags"])
    if not isinstance(value, dict):
        raise ValueError("preparation_review_flags_invalid")
    unexpected = set(value) - REVIEW_FLAG_KEYS
    if unexpected:
        raise ValueError("preparation_review_flags_invalid")
    return {key: bool(value.get(key)) for key in sorted(REVIEW_FLAG_KEYS)}


def _normalise_vote(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("preparation_vote_invalid")
    vote = value.strip().lower()
    if vote not in PRELIMINARY_VOTES:
        raise ValueError("preparation_vote_invalid")
    return vote


def _decode_json(value: Any, fallback: Any) -> Any:
    try:
        decoded = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return decoded


def _sheet_from_row(row: Any | None) -> dict[str, Any]:
    if row is None:
        return _empty_sheet()
    payload = _empty_sheet()
    questions = _decode_json(row["questions_json"], [])
    flags = _decode_json(row["review_flags_json"], {})
    try:
        payload["questions"] = _normalise_questions(questions)
    except ValueError:
        payload["questions"] = []
    try:
        payload["review_flags"] = _normalise_review_flags(flags)
    except ValueError:
        payload["review_flags"] = dict(_empty_sheet()["review_flags"])
    payload.update(
        {
            "notes": str(row["notes"] or ""),
            "preliminary_vote": (
                str(row["preliminary_vote"])
                if str(row["preliminary_vote"] or "") in PRELIMINARY_VOTES
                else None
            ),
            "preliminary_vote_reason": str(row["preliminary_vote_reason"] or ""),
            "source_bill_updated_at": str(row["source_bill_updated_at"] or "") or None,
            "revision": int(row["revision"] or 0),
            "created_at": str(row["created_at"] or "") or None,
            "updated_at": str(row["updated_at"] or "") or None,
        }
    )
    return payload


def get_preparation_sheet(
    guild_id: int,
    bill_id: int,
    user_id: int,
) -> dict[str, Any]:
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT * FROM tvrs_bill_preparation_sheets
            WHERE guild_id = ? AND bill_id = ? AND user_id = ?
            """,
            (int(guild_id), int(bill_id), int(user_id)),
        ).fetchone()
    return _sheet_from_row(row)


def save_preparation_sheet(
    guild_id: int,
    bill_id: int,
    user_id: int,
    *,
    user_display: str | None,
    expected_revision: int,
    questions: Any,
    notes: Any,
    preliminary_vote: Any,
    preliminary_vote_reason: Any,
    review_flags: Any,
    source_bill_updated_at: str | None,
) -> dict[str, Any]:
    """Save one whole personal sheet using optimistic revision control."""

    if isinstance(expected_revision, bool):
        raise ValueError("preparation_revision_invalid")
    try:
        revision = int(expected_revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("preparation_revision_invalid") from exc
    if revision < 0:
        raise ValueError("preparation_revision_invalid")

    clean_questions = _normalise_questions(questions)
    clean_notes = _clean_text(notes, field="notes", limit=MAX_NOTES_LENGTH)
    clean_vote = _normalise_vote(preliminary_vote)
    clean_reason = _clean_text(
        preliminary_vote_reason,
        field="vote_reason",
        limit=MAX_REASON_LENGTH,
    )
    clean_flags = _normalise_review_flags(review_flags)
    clean_display = _clean_text(
        user_display,
        field="display",
        limit=MAX_DISPLAY_LENGTH,
    ) or None
    clean_source_updated_at = _clean_text(
        source_bill_updated_at,
        field="source_bill_updated_at",
        limit=80,
    ) or None
    questions_json = json.dumps(clean_questions, ensure_ascii=False, separators=(",", ":"))
    flags_json = json.dumps(clean_flags, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    now = utc_now_iso()

    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            """
            SELECT revision FROM tvrs_bill_preparation_sheets
            WHERE guild_id = ? AND bill_id = ? AND user_id = ?
            """,
            (int(guild_id), int(bill_id), int(user_id)),
        ).fetchone()
        if existing is None:
            if revision != 0:
                con.rollback()
                raise PreparationRevisionConflict("preparation_revision_conflict")
            con.execute(
                """
                INSERT INTO tvrs_bill_preparation_sheets(
                    guild_id, bill_id, user_id, user_display, questions_json,
                    notes, preliminary_vote, preliminary_vote_reason,
                    review_flags_json, source_bill_updated_at, revision,
                    created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    int(guild_id),
                    int(bill_id),
                    int(user_id),
                    clean_display,
                    questions_json,
                    clean_notes,
                    clean_vote,
                    clean_reason,
                    flags_json,
                    clean_source_updated_at,
                    now,
                    now,
                ),
            )
        else:
            changed = con.execute(
                """
                UPDATE tvrs_bill_preparation_sheets
                SET user_display = ?, questions_json = ?, notes = ?,
                    preliminary_vote = ?, preliminary_vote_reason = ?,
                    review_flags_json = ?, source_bill_updated_at = ?,
                    revision = revision + 1, updated_at = ?
                WHERE guild_id = ? AND bill_id = ? AND user_id = ?
                  AND revision = ?
                """,
                (
                    clean_display,
                    questions_json,
                    clean_notes,
                    clean_vote,
                    clean_reason,
                    flags_json,
                    clean_source_updated_at,
                    now,
                    int(guild_id),
                    int(bill_id),
                    int(user_id),
                    revision,
                ),
            )
            if changed.rowcount != 1:
                con.rollback()
                raise PreparationRevisionConflict("preparation_revision_conflict")
        row = con.execute(
            """
            SELECT * FROM tvrs_bill_preparation_sheets
            WHERE guild_id = ? AND bill_id = ? AND user_id = ?
            """,
            (int(guild_id), int(bill_id), int(user_id)),
        ).fetchone()
        con.commit()
    return _sheet_from_row(row)


def preparation_statuses(
    guild_id: int,
    user_id: int,
    bill_ids: list[int] | tuple[int, ...],
) -> dict[int, dict[str, Any]]:
    """Return personal, content-free markers for a library projection."""

    unique_ids = sorted({int(item) for item in bill_ids if int(item) > 0})
    if not unique_ids:
        return {}
    placeholders = ", ".join("?" for _ in unique_ids)
    with connect_readonly() as con:
        rows = con.execute(
            f"""
            SELECT bill_id, preliminary_vote, updated_at
            FROM tvrs_bill_preparation_sheets
            WHERE guild_id = ? AND user_id = ? AND bill_id IN ({placeholders})
            """,
            (int(guild_id), int(user_id), *unique_ids),
        ).fetchall()
    return {
        int(row["bill_id"]): {
            "prepared": True,
            "preliminary_vote": (
                str(row["preliminary_vote"])
                if str(row["preliminary_vote"] or "") in PRELIMINARY_VOTES
                else None
            ),
            "updated_at": str(row["updated_at"] or "") or None,
        }
        for row in rows
    }


__all__ = [
    "MAX_NOTES_LENGTH",
    "MAX_QUESTIONS",
    "MAX_QUESTION_LENGTH",
    "MAX_REASON_LENGTH",
    "PRELIMINARY_VOTES",
    "PreparationRevisionConflict",
    "get_preparation_sheet",
    "preparation_statuses",
    "save_preparation_sheet",
]
