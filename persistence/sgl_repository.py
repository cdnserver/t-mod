from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from persistence import core as _core
from persistence.core import (
    ClientProfile,
    LawyerProfile,
    SGLCase,
    SGLReceipt,
    _client_profile_from_row,
    _db_lock,
    _lawyer_profile_from_row,
    connect,
    utc_now_iso,
)
from persistence.activity_repository import _record_bot_action, get_meta, set_meta, upsert_member

def _case_from_row(row: sqlite3.Row) -> SGLCase:
    return SGLCase(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        case_number=int(row["case_number"]),
        channel_id=row["channel_id"],
        client_id=int(row["client_id"]),
        client_display=row["client_display"],
        lead_lawyer_id=int(row["lead_lawyer_id"]),
        lead_lawyer_display=row["lead_lawyer_display"],
        secretary_id=row["secretary_id"],
        secretary_display=row["secretary_display"],
        status=str(row["status"]),
        request_type=row["request_type"],
        client_nick=row["client_nick"],
        static_id=row["static_id"],
        bank_account=row["bank_account"],
        phone=row["phone"],
        passport_url=row["passport_url"],
        situation_text=row["situation_text"],
        situation_author_id=row["situation_author_id"],
        situation_author_display=row["situation_author_display"],
        claim_link=row["claim_link"],
        claim_message_id=row["claim_message_id"],
        portfolio_message_id=row["portfolio_message_id"],
        portfolio_description=row["portfolio_description"],
        created_by_id=row["created_by_id"],
        created_by_display=row["created_by_display"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        closed_at=row["closed_at"],
        archived_at=row["archived_at"] if "archived_at" in row.keys() else None,
        archive_category_id=row["archive_category_id"] if "archive_category_id" in row.keys() else None,
    )


def _record_case_event(
    con: sqlite3.Connection,
    *,
    case_id: int | None,
    guild_id: int,
    case_number: int,
    actor_id: int | None,
    actor_display: str | None,
    action: str,
    details: str | None = None,
) -> None:
    con.execute(
        """
        INSERT INTO sgl_case_events(case_id, guild_id, case_number, actor_id, actor_display, action, details, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (case_id, guild_id, case_number, actor_id, actor_display, action, details, utc_now_iso()),
    )


def set_sgl_case_seed(guild_id: int, first_case_number: int) -> str:
    """Set the initial case number only when no case exists yet."""
    first_case_number = max(1, int(first_case_number))
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT MAX(case_number) AS max_case FROM sgl_cases WHERE guild_id = ?", (guild_id,)).fetchone()
        has_cases = row is not None and row["max_case"] is not None
        if has_cases:
            set_meta(con, f"sgl_case_seed_ignored:{guild_id}:{utc_now_iso()}", str(first_case_number))
            con.commit()
            return "kept_existing_cases"
        set_meta(con, f"sgl_case_next_number:{guild_id}", str(first_case_number))
        con.commit()
        return "seed_set"


def _next_case_number(con: sqlite3.Connection, guild_id: int) -> int:
    row = con.execute("SELECT MAX(case_number) AS max_case FROM sgl_cases WHERE guild_id = ?", (guild_id,)).fetchone()
    if row is not None and row["max_case"] is not None:
        return int(row["max_case"]) + 1
    seed_row = con.execute("SELECT value FROM meta WHERE key = ?", (f"sgl_case_next_number:{guild_id}",)).fetchone()
    if seed_row:
        try:
            return max(1, int(seed_row["value"]))
        except (TypeError, ValueError):
            return 1
    return 1


def reserve_sgl_case(
    *,
    guild_id: int,
    client_id: int,
    client_display: str | None,
    lead_lawyer_id: int,
    lead_lawyer_display: str | None,
    secretary_id: int | None,
    secretary_display: str | None,
    created_by_id: int | None,
    created_by_display: str | None,
) -> SGLCase:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        case_number = _next_case_number(con, guild_id)
        cur = con.execute(
            """
            INSERT INTO sgl_cases(
                guild_id, case_number, channel_id, client_id, client_display,
                lead_lawyer_id, lead_lawyer_display, secretary_id, secretary_display,
                status, created_by_id, created_by_display, created_at, updated_at
            )
            VALUES(?, ?, NULL, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?)
            """,
            (
                guild_id,
                case_number,
                client_id,
                client_display,
                lead_lawyer_id,
                lead_lawyer_display,
                secretary_id,
                secretary_display,
                created_by_id,
                created_by_display,
                now,
                now,
            ),
        )
        case_id = int(cur.lastrowid)
        _record_case_event(con, case_id=case_id, guild_id=guild_id, case_number=case_number, actor_id=created_by_id, actor_display=created_by_display, action="reserved")
        row = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (case_id,)).fetchone()
        con.commit()
    return _case_from_row(row)


def attach_sgl_case_channel(case_id: int, channel_id: int) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute("UPDATE sgl_cases SET channel_id = ?, status = 'created', updated_at = ? WHERE id = ?", (channel_id, now, case_id))
        _record_case_event(con, case_id=case_id, guild_id=int(row["guild_id"]), case_number=int(row["case_number"]), actor_id=None, actor_display=None, action="channel_created", details=str(channel_id))
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (case_id,)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def mark_sgl_case_error(case_id: int, details: str) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (case_id,)).fetchone()
        if row is not None:
            con.execute("UPDATE sgl_cases SET status = 'error', updated_at = ? WHERE id = ?", (now, case_id))
            _record_case_event(con, case_id=case_id, guild_id=int(row["guild_id"]), case_number=int(row["case_number"]), actor_id=None, actor_display=None, action="error", details=details)
        con.commit()


def get_sgl_case_by_channel(guild_id: int, channel_id: int) -> SGLCase | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
    return _case_from_row(row) if row else None


def get_sgl_case_by_number(guild_id: int, case_number: int) -> SGLCase | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND case_number = ?", (guild_id, case_number)).fetchone()
    return _case_from_row(row) if row else None


def update_sgl_case_params(
    *,
    guild_id: int,
    channel_id: int,
    request_type: str,
    client_nick: str,
    static_id: str,
    bank_account: str,
    phone: str,
    passport_url: str,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_cases
            SET request_type = ?, client_nick = ?, static_id = ?, bank_account = ?, phone = ?, passport_url = ?,
                status = 'awaiting_situation', updated_at = ?
            WHERE id = ?
            """,
            (request_type, client_nick, static_id, bank_account, phone, passport_url, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="params_saved")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_case",
            target_id=int(row["id"]),
            summary=f"Изменены параметры дела №{int(row['case_number'])}",
            payload={"table": "sgl_cases", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def update_sgl_case_situation(
    guild_id: int,
    channel_id: int,
    situation_text: str,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_cases
            SET situation_text = ?, situation_author_id = ?, situation_author_display = ?, status = 'awaiting_link', updated_at = ?
            WHERE id = ?
            """,
            (situation_text, actor_id, actor_display, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="situation_saved")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_case",
            target_id=int(row["id"]),
            summary=f"Изменено описание ситуации в деле №{int(row['case_number'])}",
            payload={"table": "sgl_cases", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def set_sgl_case_link(
    guild_id: int,
    channel_id: int,
    claim_link: str,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute("UPDATE sgl_cases SET claim_link = ?, status = 'awaiting_close', updated_at = ? WHERE id = ?", (claim_link, now, int(row["id"])))
        _record_case_event(con, case_id=int(row["id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="claim_link_saved", details=claim_link)
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_case",
            target_id=int(row["id"]),
            summary=f"Изменена ссылка на иск в деле №{int(row['case_number'])}",
            payload={"table": "sgl_cases", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def set_sgl_case_link_message(guild_id: int, channel_id: int, message_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("UPDATE sgl_cases SET claim_message_id = ?, updated_at = ? WHERE guild_id = ? AND channel_id = ?", (message_id, now, guild_id, channel_id))
        con.commit()


def close_sgl_case(
    *,
    guild_id: int,
    channel_id: int,
    actor_id: int | None,
    actor_display: str | None,
    publish_portfolio: bool,
    portfolio_description: str | None,
    portfolio_message_id: int | None,
) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_cases
            SET status = 'closed', portfolio_description = ?, portfolio_message_id = ?, closed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (portfolio_description, portfolio_message_id, now, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="closed", details=f"publish_portfolio={publish_portfolio}")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_case",
            target_id=int(row["id"]),
            summary=f"Закрыто дело №{int(row['case_number'])}",
            payload={"table": "sgl_cases", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def list_sgl_cases_with_channels(guild_id: int) -> list[SGLCase]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_cases
            WHERE guild_id = ? AND channel_id IS NOT NULL
            ORDER BY case_number ASC
            """,
            (guild_id,),
        ).fetchall()
    return [_case_from_row(row) for row in rows]



def list_sgl_cases_for_client(guild_id: int, client_id: int, limit: int = 25) -> list[SGLCase]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_cases
            WHERE guild_id = ? AND client_id = ?
            ORDER BY case_number DESC
            LIMIT ?
            """,
            (guild_id, client_id, int(limit)),
        ).fetchall()
    return [_case_from_row(row) for row in rows]


def list_sgl_cases_for_participant(guild_id: int, user_id: int, limit: int = 25) -> list[SGLCase]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_cases
            WHERE guild_id = ?
              AND (client_id = ? OR lead_lawyer_id = ? OR secretary_id = ?)
            ORDER BY case_number DESC
            LIMIT ?
            """,
            (guild_id, user_id, user_id, user_id, int(limit)),
        ).fetchall()
    return [_case_from_row(row) for row in rows]


def get_sgl_case_events(case_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_case_events
            WHERE case_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (case_id, int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]

def list_sgl_closed_cases_pending_archive(guild_id: int | None = None) -> list[SGLCase]:
    params: list[Any] = []
    where = "status = 'closed' AND channel_id IS NOT NULL AND archived_at IS NULL"
    if guild_id is not None:
        where += " AND guild_id = ?"
        params.append(guild_id)
    with _db_lock, connect() as con:
        rows = con.execute(f"SELECT * FROM sgl_cases WHERE {where} ORDER BY closed_at ASC, case_number ASC", params).fetchall()
    return [_case_from_row(row) for row in rows]


def mark_sgl_case_archived(guild_id: int, channel_id: int, archive_category_id: int) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_cases
            SET archived_at = ?, archive_category_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, archive_category_id, now, int(row["id"])),
        )
        _record_case_event(
            con,
            case_id=int(row["id"]),
            guild_id=guild_id,
            case_number=int(row["case_number"]),
            actor_id=None,
            actor_display=None,
            action="archived",
            details=str(archive_category_id),
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def _receipt_from_row(row: sqlite3.Row) -> SGLReceipt:
    return SGLReceipt(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        case_id=int(row["case_id"]),
        case_number=int(row["case_number"]),
        channel_id=row["channel_id"],
        client_id=int(row["client_id"]),
        lead_lawyer_id=int(row["lead_lawyer_id"]),
        created_by_id=row["created_by_id"],
        created_by_display=row["created_by_display"],
        court_code=str(row["court_code"]),
        court_label=str(row["court_label"]),
        court_suffix=str(row["court_suffix"]),
        total_amount=int(row["total_amount"]),
        lawyer_amount=int(row["lawyer_amount"]),
        duty_amount=int(row["duty_amount"]),
        lawyer_bank=str(row["lawyer_bank"]),
        duty_bank=str(row["duty_bank"]),
        invoice_message_id=row["invoice_message_id"],
        proof_services_url=row["proof_services_url"],
        proof_duty_url=row["proof_duty_url"],
        proof_submitted_by_id=row["proof_submitted_by_id"],
        proof_submitted_by_display=row["proof_submitted_by_display"],
        proof_submitted_at=row["proof_submitted_at"],
        confirmation_message_id=row["confirmation_message_id"],
        confirmed_by_id=row["confirmed_by_id"],
        confirmed_by_display=row["confirmed_by_display"],
        confirmed_at=row["confirmed_at"],
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def create_sgl_receipt(
    *,
    guild_id: int,
    case: SGLCase,
    created_by_id: int | None,
    created_by_display: str | None,
    court_code: str,
    court_label: str,
    court_suffix: str,
    total_amount: int,
    lawyer_amount: int,
    duty_amount: int,
    lawyer_bank: str,
    duty_bank: str,
) -> SGLReceipt:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute(
            """
            INSERT INTO sgl_receipts(
                guild_id, case_id, case_number, channel_id, client_id, lead_lawyer_id,
                created_by_id, created_by_display, court_code, court_label, court_suffix,
                total_amount, lawyer_amount, duty_amount, lawyer_bank, duty_bank,
                status, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', ?, ?)
            """,
            (
                guild_id, case.id, case.case_number, case.channel_id, case.client_id, case.lead_lawyer_id,
                created_by_id, created_by_display, court_code, court_label, court_suffix,
                int(total_amount), int(lawyer_amount), int(duty_amount), lawyer_bank, duty_bank,
                now, now,
            ),
        )
        receipt_id = int(cur.lastrowid)
        _record_case_event(con, case_id=case.id, guild_id=guild_id, case_number=case.case_number, actor_id=created_by_id, actor_display=created_by_display, action="receipt_created", details=f"receipt_id={receipt_id};{court_suffix};total={total_amount}")
        row = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (receipt_id,)).fetchone()
        con.commit()
    return _receipt_from_row(row)


def set_sgl_receipt_invoice_message(receipt_id: int, message_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("UPDATE sgl_receipts SET invoice_message_id = ?, updated_at = ? WHERE id = ?", (message_id, now, receipt_id))
        con.commit()


def get_sgl_receipt_by_id(receipt_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (receipt_id,)).fetchone()
    return _receipt_from_row(row) if row else None


def get_sgl_receipt_by_invoice_message(guild_id: int, message_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE guild_id = ? AND invoice_message_id = ?", (guild_id, message_id)).fetchone()
    return _receipt_from_row(row) if row else None


def get_sgl_receipt_by_confirmation_message(guild_id: int, message_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE guild_id = ? AND confirmation_message_id = ?", (guild_id, message_id)).fetchone()
    return _receipt_from_row(row) if row else None


def get_sgl_receipt_by_confirmation_message_any(message_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE confirmation_message_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1", (message_id,)).fetchone()
    return _receipt_from_row(row) if row else None


def list_sgl_receipts_for_case(case_id: int, limit: int = 10) -> list[SGLReceipt]:
    with _db_lock, connect() as con:
        rows = con.execute("SELECT * FROM sgl_receipts WHERE case_id = ? ORDER BY created_at DESC, id DESC LIMIT ?", (case_id, int(limit))).fetchall()
    return [_receipt_from_row(row) for row in rows]


def get_latest_sgl_receipt_for_case(case_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE case_id = ? ORDER BY CASE WHEN status = 'confirmed' THEN 0 ELSE 1 END, created_at DESC, id DESC LIMIT 1", (case_id,)).fetchone()
    return _receipt_from_row(row) if row else None


def get_latest_confirmed_sgl_receipt_for_case(case_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE case_id = ? AND status = 'confirmed' ORDER BY confirmed_at DESC, id DESC LIMIT 1", (case_id,)).fetchone()
    return _receipt_from_row(row) if row else None


def submit_sgl_receipt_proofs_by_invoice_message(
    *,
    guild_id: int,
    invoice_message_id: int,
    services_url: str,
    duty_url: str,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLReceipt | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_receipts WHERE guild_id = ? AND invoice_message_id = ?", (guild_id, invoice_message_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_receipts
            SET proof_services_url = ?, proof_duty_url = ?, proof_submitted_by_id = ?, proof_submitted_by_display = ?, proof_submitted_at = ?, status = 'proofs_submitted', updated_at = ?
            WHERE id = ?
            """,
            (services_url, duty_url, actor_id, actor_display, now, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["case_id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="receipt_proofs_submitted", details=f"receipt_id={int(row['id'])}")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_receipt",
            target_id=int(row["id"]),
            summary=f"Добавлены доказательства оплаты по квитанции #{int(row['id'])}",
            payload={"table": "sgl_receipts", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _receipt_from_row(updated) if updated else None


def set_sgl_receipt_confirmation_message(receipt_id: int, message_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("UPDATE sgl_receipts SET confirmation_message_id = ?, updated_at = ? WHERE id = ?", (message_id, now, receipt_id))
        con.commit()


def confirm_sgl_receipt_by_confirmation_message(
    *,
    guild_id: int,
    confirmation_message_id: int,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLReceipt | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_receipts WHERE guild_id = ? AND confirmation_message_id = ?", (guild_id, confirmation_message_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_receipts
            SET confirmed_by_id = ?, confirmed_by_display = ?, confirmed_at = ?, status = 'confirmed', updated_at = ?
            WHERE id = ?
            """,
            (actor_id, actor_display, now, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["case_id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="receipt_confirmed", details=f"receipt_id={int(row['id'])}")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_receipt",
            target_id=int(row["id"]),
            summary=f"Подтверждена квитанция #{int(row['id'])}",
            payload={"table": "sgl_receipts", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _receipt_from_row(updated) if updated else None


def confirm_sgl_receipt_by_confirmation_message_any(
    *,
    confirmation_message_id: int,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLReceipt | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_receipts WHERE confirmation_message_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1", (confirmation_message_id,)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_receipts
            SET confirmed_by_id = ?, confirmed_by_display = ?, confirmed_at = ?, status = 'confirmed', updated_at = ?
            WHERE id = ?
            """,
            (actor_id, actor_display, now, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["case_id"]), guild_id=int(row["guild_id"]), case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="receipt_confirmed", details=f"receipt_id={int(row['id'])}")
        _record_bot_action(
            con,
            guild_id=int(row["guild_id"]),
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_receipt",
            target_id=int(row["id"]),
            summary=f"Подтверждена квитанция #{int(row['id'])}",
            payload={"table": "sgl_receipts", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _receipt_from_row(updated) if updated else None


def _read_legacy_activity_json() -> dict[str, Any] | None:
    if not _core.LEGACY_ACTIVITY_FILE.exists() or not _core.LEGACY_ACTIVITY_FILE.is_file():
        return None
    try:
        with _core.LEGACY_ACTIVITY_FILE.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def migrate_legacy_activity_json() -> dict[str, int | str]:
    result: dict[str, int | str] = {"users": 0, "events": 0, "counters": 0, "status": "skipped", "reason": "legacy file not found"}
    if not _core.LEGACY_ACTIVITY_FILE.exists():
        return result

    raw = _core.LEGACY_ACTIVITY_FILE.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    meta_key = f"legacy_activity_json_sha256:{sha}"
    if get_meta(meta_key) == "imported":
        result["reason"] = "same legacy file already imported"
        return result

    data = _read_legacy_activity_json()
    if not data:
        result["reason"] = "legacy file empty or invalid"
        return result

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        users = 0
        events = 0
        counters = 0
        for guild_key, guild_data in data.items():
            if not isinstance(guild_data, dict):
                continue
            try:
                guild_id = int(guild_key)
            except (TypeError, ValueError):
                continue
            for user_key, item in guild_data.items():
                if not isinstance(item, dict):
                    continue
                try:
                    user_id = int(item.get("user_id") or user_key)
                except (TypeError, ValueError):
                    continue
                upsert_member(
                    con,
                    guild_id,
                    user_id,
                    item.get("display_name"),
                    item.get("name"),
                    item.get("mention"),
                    False,
                )
                users += 1

                counters_data = item.get("counters") if isinstance(item.get("counters"), dict) else {}
                total_events = int(counters_data.get("total", 0) or 0)
                for event_type, count in counters_data.items():
                    if event_type == "total":
                        continue
                    try:
                        count_int = int(count)
                    except (TypeError, ValueError):
                        continue
                    con.execute(
                        """
                        INSERT INTO activity_counters(guild_id, user_id, event_type, count, updated_at)
                        VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(guild_id, user_id, event_type) DO UPDATE SET
                            count = MAX(activity_counters.count, excluded.count),
                            updated_at = excluded.updated_at
                        """,
                        (guild_id, user_id, str(event_type), count_int, now),
                    )
                    counters += 1

                recent_events = item.get("recent_events") if isinstance(item.get("recent_events"), list) else []
                for event in reversed(recent_events):
                    if not isinstance(event, dict):
                        continue
                    con.execute(
                        """
                        INSERT INTO activity_events(
                            guild_id, user_id, event_type, event_text, at,
                            channel_id, channel_name, category_id, category_name,
                            details, message_id, created_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, ?)
                        """,
                        (
                            guild_id,
                            user_id,
                            str(event.get("event") or "legacy_event"),
                            event.get("event_text"),
                            str(event.get("at") or now),
                            event.get("channel_id"),
                            event.get("channel_name"),
                            event.get("details"),
                            now,
                        ),
                    )
                    events += 1

                con.execute(
                    """
                    INSERT INTO activity_summary(
                        guild_id, user_id, last_activity_at, last_event, last_event_text,
                        last_channel_id, last_channel_name, last_category_id, last_category_name,
                        last_details, total_events, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                        last_activity_at = CASE
                            WHEN activity_summary.last_activity_at IS NULL THEN excluded.last_activity_at
                            WHEN excluded.last_activity_at IS NULL THEN activity_summary.last_activity_at
                            WHEN excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_activity_at
                            ELSE activity_summary.last_activity_at
                        END,
                        last_event = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_event
                            ELSE activity_summary.last_event
                        END,
                        last_event_text = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_event_text
                            ELSE activity_summary.last_event_text
                        END,
                        last_channel_id = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_channel_id
                            ELSE activity_summary.last_channel_id
                        END,
                        last_channel_name = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_channel_name
                            ELSE activity_summary.last_channel_name
                        END,
                        last_details = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_details
                            ELSE activity_summary.last_details
                        END,
                        total_events = MAX(activity_summary.total_events, excluded.total_events),
                        updated_at = excluded.updated_at
                    """,
                    (
                        guild_id,
                        user_id,
                        item.get("last_activity_at"),
                        item.get("last_event"),
                        item.get("last_event_text"),
                        item.get("last_channel_id"),
                        item.get("last_channel_name"),
                        item.get("last_details"),
                        total_events,
                        now,
                        now,
                    ),
                )

        set_meta(con, meta_key, "imported")
        set_meta(con, "legacy_activity_json_last_sha256", sha)
        con.commit()

    result.update({"users": users, "events": events, "counters": counters, "status": "imported", "reason": "ok"})
    return result


def create_audio_generation(
    *,
    guild_id: int,
    channel_id: int,
    user_id: int,
    user_display: str | None,
    prompt: str,
    model: str,
) -> int:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        cur = con.execute(
            """
            INSERT INTO audio_generations(
                guild_id, channel_id, user_id, user_display, prompt, model,
                status, seconds_elapsed, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, 'running', 0, ?, ?)
            """,
            (guild_id, channel_id, user_id, user_display, prompt, model, now, now),
        )
        con.commit()
        return int(cur.lastrowid)


def update_audio_generation(record_id: int, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "status",
        "seconds_elapsed",
        "output_filename",
        "output_mime",
        "output_size_bytes",
        "dm_message_id",
        "error",
        "completed_at",
        "updated_at",
    }
    clean = {key: value for key, value in fields.items() if key in allowed}
    if not clean:
        return
    clean["updated_at"] = clean.get("updated_at") or utc_now_iso()
    assignments = ", ".join(f"{key} = ?" for key in clean.keys())
    params = [*clean.values(), record_id]
    with _db_lock, connect() as con:
        con.execute(f"UPDATE audio_generations SET {assignments} WHERE id = ?", params)
        con.commit()


def count_recent_running_audio_generations(guild_id: int, within_minutes: int = 30) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(1, int(within_minutes)))).isoformat()
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT COUNT(*) AS count
            FROM audio_generations
            WHERE guild_id = ? AND status = 'running' AND updated_at >= ?
            """,
            (guild_id, cutoff),
        ).fetchone()
    return int(row["count"] or 0) if row else 0


def save_client_profile(
    *,
    guild_id: int,
    discord_user_id: int,
    client_nick: str,
    static_id: str,
    bank_account: str,
    phone: str,
    passport_url: str,
    notes: str | None = None,
    last_case_id: int | None = None,
) -> int:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        existing = con.execute(
            "SELECT id FROM client_profiles WHERE guild_id = ? AND discord_user_id = ? AND lower(client_nick) = lower(?) AND static_id = ?",
            (guild_id, discord_user_id, client_nick, static_id),
        ).fetchone()
        if existing:
            profile_id = int(existing["id"])
            con.execute(
                """
                UPDATE client_profiles
                SET bank_account = ?, phone = ?, passport_url = ?, notes = COALESCE(?, notes), last_case_id = ?, updated_at = ?, last_used_at = ?
                WHERE id = ?
                """,
                (bank_account, phone, passport_url, notes, last_case_id, now, now, profile_id),
            )
        else:
            cur = con.execute(
                """
                INSERT INTO client_profiles(
                    guild_id, discord_user_id, client_nick, static_id, bank_account, phone, passport_url, notes, last_case_id, created_at, updated_at, last_used_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, discord_user_id, client_nick, static_id, bank_account, phone, passport_url, notes, last_case_id, now, now, now),
            )
            profile_id = int(cur.lastrowid)
        con.commit()
        return profile_id


def list_client_profiles_for_user(guild_id: int, discord_user_id: int, limit: int = 20) -> list[ClientProfile]:
    with _db_lock, connect() as con:
        rows = con.execute(
            "SELECT * FROM client_profiles WHERE guild_id = ? AND discord_user_id = ? ORDER BY COALESCE(last_used_at, updated_at) DESC, id DESC LIMIT ?",
            (guild_id, discord_user_id, limit),
        ).fetchall()
    return [_client_profile_from_row(r) for r in rows if r is not None]


def get_client_profile(profile_id: int, guild_id: int | None = None) -> ClientProfile | None:
    with _db_lock, connect() as con:
        if guild_id is None:
            row = con.execute("SELECT * FROM client_profiles WHERE id = ?", (profile_id,)).fetchone()
        else:
            row = con.execute("SELECT * FROM client_profiles WHERE id = ? AND guild_id = ?", (profile_id, guild_id)).fetchone()
    return _client_profile_from_row(row)


def search_client_profiles(guild_id: int, query: str, limit: int = 25) -> list[ClientProfile]:
    like = f"%{query.strip().lower()}%"
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM client_profiles
            WHERE guild_id = ? AND (
                lower(client_nick) LIKE ? OR lower(static_id) LIKE ? OR lower(bank_account) LIKE ? OR lower(phone) LIKE ? OR CAST(discord_user_id AS TEXT) LIKE ?
            )
            ORDER BY COALESCE(last_used_at, updated_at) DESC, id DESC
            LIMIT ?
            """,
            (guild_id, like, like, like, like, like, limit),
        ).fetchall()
    return [_client_profile_from_row(r) for r in rows if r is not None]


def save_lawyer_profile(
    *,
    guild_id: int,
    lawyer_nick: str,
    discord_user_id: int | None = None,
    static_id: str | None = None,
    bank_account: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    passport_url: str | None = None,
    notes: str | None = None,
) -> int:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        existing = None
        if discord_user_id:
            existing = con.execute("SELECT id FROM lawyer_profiles WHERE guild_id = ? AND discord_user_id = ?", (guild_id, discord_user_id)).fetchone()
        if existing is None:
            existing = con.execute("SELECT id FROM lawyer_profiles WHERE guild_id = ? AND lower(lawyer_nick) = lower(?) AND COALESCE(static_id,'') = COALESCE(?, '')", (guild_id, lawyer_nick, static_id)).fetchone()
        if existing:
            profile_id = int(existing["id"])
            con.execute(
                """UPDATE lawyer_profiles SET lawyer_nick=?, discord_user_id=?, static_id=?, bank_account=?, phone=?, email=?, passport_url=?, notes=?, updated_at=? WHERE id=?""",
                (lawyer_nick, discord_user_id, static_id, bank_account, phone, email, passport_url, notes, now, profile_id),
            )
        else:
            cur = con.execute(
                """INSERT INTO lawyer_profiles(guild_id, discord_user_id, lawyer_nick, static_id, bank_account, phone, email, passport_url, notes, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (guild_id, discord_user_id, lawyer_nick, static_id, bank_account, phone, email, passport_url, notes, now, now),
            )
            profile_id = int(cur.lastrowid)
        con.commit()
        return profile_id



def get_lawyer_profile_for_user(guild_id: int, discord_user_id: int) -> LawyerProfile | None:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM lawyer_profiles WHERE guild_id = ? AND discord_user_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
            (guild_id, discord_user_id),
        ).fetchone()
    return _lawyer_profile_from_row(row) if row else None

def search_lawyer_profiles(guild_id: int, query: str | None = None, limit: int = 25) -> list[LawyerProfile]:
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            rows = con.execute(
                """SELECT * FROM lawyer_profiles WHERE guild_id = ? AND (lower(lawyer_nick) LIKE ? OR lower(COALESCE(static_id,'')) LIKE ? OR lower(COALESCE(phone,'')) LIKE ? OR CAST(COALESCE(discord_user_id,'') AS TEXT) LIKE ?) ORDER BY updated_at DESC, id DESC LIMIT ?""",
                (guild_id, like, like, like, like, limit),
            ).fetchall()
        else:
            rows = con.execute("SELECT * FROM lawyer_profiles WHERE guild_id = ? ORDER BY updated_at DESC, id DESC LIMIT ?", (guild_id, limit)).fetchall()
    return [_lawyer_profile_from_row(r) for r in rows if r is not None]


# Registry pagination helpers for /sg inline panels.
def count_client_profiles(guild_id: int, query: str | None = None) -> int:
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            row = con.execute(
                """
                SELECT COUNT(*) AS c FROM client_profiles
                WHERE guild_id = ? AND (
                    lower(client_nick) LIKE ? OR lower(static_id) LIKE ? OR lower(bank_account) LIKE ? OR lower(phone) LIKE ? OR CAST(discord_user_id AS TEXT) LIKE ?
                )
                """,
                (guild_id, like, like, like, like, like),
            ).fetchone()
        else:
            row = con.execute("SELECT COUNT(*) AS c FROM client_profiles WHERE guild_id = ?", (guild_id,)).fetchone()
    return int(row["c"] or 0) if row else 0


def list_client_profiles_page(guild_id: int, page: int = 0, per_page: int = 10, query: str | None = None) -> list[ClientProfile]:
    page = max(0, int(page))
    per_page = max(1, min(25, int(per_page)))
    offset = page * per_page
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            rows = con.execute(
                """
                SELECT * FROM client_profiles
                WHERE guild_id = ? AND (
                    lower(client_nick) LIKE ? OR lower(static_id) LIKE ? OR lower(bank_account) LIKE ? OR lower(phone) LIKE ? OR CAST(discord_user_id AS TEXT) LIKE ?
                )
                ORDER BY lower(client_nick) ASC, CAST(static_id AS INTEGER) ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (guild_id, like, like, like, like, like, per_page, offset),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT * FROM client_profiles
                WHERE guild_id = ?
                ORDER BY lower(client_nick) ASC, CAST(static_id AS INTEGER) ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (guild_id, per_page, offset),
            ).fetchall()
    return [_client_profile_from_row(r) for r in rows if r is not None]


def count_lawyer_profiles(guild_id: int, query: str | None = None) -> int:
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            row = con.execute(
                """
                SELECT COUNT(*) AS c FROM lawyer_profiles
                WHERE guild_id = ? AND (
                    lower(lawyer_nick) LIKE ? OR lower(COALESCE(static_id,'')) LIKE ? OR lower(COALESCE(phone,'')) LIKE ? OR lower(COALESCE(bank_account,'')) LIKE ? OR CAST(COALESCE(discord_user_id,'') AS TEXT) LIKE ?
                )
                """,
                (guild_id, like, like, like, like, like),
            ).fetchone()
        else:
            row = con.execute("SELECT COUNT(*) AS c FROM lawyer_profiles WHERE guild_id = ?", (guild_id,)).fetchone()
    return int(row["c"] or 0) if row else 0


def list_lawyer_profiles_page(guild_id: int, page: int = 0, per_page: int = 10, query: str | None = None) -> list[LawyerProfile]:
    page = max(0, int(page))
    per_page = max(1, min(25, int(per_page)))
    offset = page * per_page
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            rows = con.execute(
                """
                SELECT * FROM lawyer_profiles
                WHERE guild_id = ? AND (
                    lower(lawyer_nick) LIKE ? OR lower(COALESCE(static_id,'')) LIKE ? OR lower(COALESCE(phone,'')) LIKE ? OR lower(COALESCE(bank_account,'')) LIKE ? OR CAST(COALESCE(discord_user_id,'') AS TEXT) LIKE ?
                )
                ORDER BY lower(lawyer_nick) ASC, CAST(COALESCE(static_id,'0') AS INTEGER) ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (guild_id, like, like, like, like, like, per_page, offset),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT * FROM lawyer_profiles
                WHERE guild_id = ?
                ORDER BY lower(lawyer_nick) ASC, CAST(COALESCE(static_id,'0') AS INTEGER) ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (guild_id, per_page, offset),
            ).fetchall()
    return [_lawyer_profile_from_row(r) for r in rows if r is not None]


def count_open_sgl_cases(guild_id: int) -> int:
    with _db_lock, connect() as con:
        row = con.execute("SELECT COUNT(*) AS c FROM sgl_cases WHERE guild_id = ? AND status != 'closed'", (guild_id,)).fetchone()
    return int(row["c"] or 0) if row else 0


def count_closed_sgl_cases(guild_id: int) -> int:
    with _db_lock, connect() as con:
        row = con.execute("SELECT COUNT(*) AS c FROM sgl_cases WHERE guild_id = ? AND status = 'closed'", (guild_id,)).fetchone()
    return int(row["c"] or 0) if row else 0

# Manual SGL admin editor helpers.

__all__ = ['_case_from_row', '_record_case_event', 'set_sgl_case_seed', '_next_case_number', 'reserve_sgl_case', 'attach_sgl_case_channel', 'mark_sgl_case_error', 'get_sgl_case_by_channel', 'get_sgl_case_by_number', 'update_sgl_case_params', 'update_sgl_case_situation', 'set_sgl_case_link', 'set_sgl_case_link_message', 'close_sgl_case', 'list_sgl_cases_with_channels', 'list_sgl_cases_for_client', 'list_sgl_cases_for_participant', 'get_sgl_case_events', 'list_sgl_closed_cases_pending_archive', 'mark_sgl_case_archived', '_receipt_from_row', 'create_sgl_receipt', 'set_sgl_receipt_invoice_message', 'get_sgl_receipt_by_id', 'get_sgl_receipt_by_invoice_message', 'get_sgl_receipt_by_confirmation_message', 'get_sgl_receipt_by_confirmation_message_any', 'list_sgl_receipts_for_case', 'get_latest_sgl_receipt_for_case', 'get_latest_confirmed_sgl_receipt_for_case', 'submit_sgl_receipt_proofs_by_invoice_message', 'set_sgl_receipt_confirmation_message', 'confirm_sgl_receipt_by_confirmation_message', 'confirm_sgl_receipt_by_confirmation_message_any', '_read_legacy_activity_json', 'migrate_legacy_activity_json', 'create_audio_generation', 'update_audio_generation', 'count_recent_running_audio_generations', 'save_client_profile', 'list_client_profiles_for_user', 'get_client_profile', 'search_client_profiles', 'save_lawyer_profile', 'get_lawyer_profile_for_user', 'search_lawyer_profiles', 'count_client_profiles', 'list_client_profiles_page', 'count_lawyer_profiles', 'list_lawyer_profiles_page', 'count_open_sgl_cases', 'count_closed_sgl_cases']
