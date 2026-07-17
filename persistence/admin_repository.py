from __future__ import annotations

import re
import sqlite3
from typing import Any

from persistence.core import _db_lock, _table_columns, connect, utc_now_iso
from persistence.activity_repository import _record_bot_action
from persistence.sgl_repository import _record_case_event

_ADMIN_TARGETS = {
    "case": {
        "table": "sgl_cases",
        "id_field": "case_number",
        "fields": {
            "case_number", "channel_id", "client_id", "client_display", "lead_lawyer_id", "lead_lawyer_display",
            "secretary_id", "secretary_display", "status", "request_type", "client_nick", "static_id",
            "bank_account", "phone", "passport_url", "situation_text", "situation_author_id", "situation_author_display",
            "claim_link", "claim_message_id", "portfolio_message_id", "portfolio_description", "created_by_id",
            "created_by_display", "closed_at", "archived_at", "archive_category_id"
        },
        "int_fields": {"case_number", "channel_id", "client_id", "lead_lawyer_id", "secretary_id", "situation_author_id", "claim_message_id", "portfolio_message_id", "created_by_id", "archive_category_id"},
    },
    "client": {
        "table": "client_profiles",
        "id_field": "id",
        "fields": {"discord_user_id", "client_nick", "static_id", "bank_account", "phone", "passport_url", "notes", "last_case_id", "last_used_at"},
        "int_fields": {"discord_user_id", "last_case_id"},
    },
    "lawyer": {
        "table": "lawyer_profiles",
        "id_field": "id",
        "fields": {"discord_user_id", "lawyer_nick", "static_id", "bank_account", "phone", "email", "passport_url", "notes"},
        "int_fields": {"discord_user_id"},
    },
    "receipt": {
        "table": "sgl_receipts",
        "id_field": "id",
        "fields": {
            "case_id", "case_number", "channel_id", "client_id", "lead_lawyer_id", "created_by_id",
            "created_by_display", "court_code", "court_label", "court_suffix", "total_amount", "lawyer_amount",
            "duty_amount", "lawyer_bank", "duty_bank", "invoice_message_id", "proof_services_url",
            "proof_duty_url", "proof_submitted_by_id", "proof_submitted_by_display", "proof_submitted_at",
            "confirmation_message_id", "confirmed_by_id", "confirmed_by_display", "confirmed_at", "status"
        },
        "int_fields": {"case_id", "case_number", "channel_id", "client_id", "lead_lawyer_id", "created_by_id", "total_amount", "lawyer_amount", "duty_amount", "invoice_message_id", "proof_submitted_by_id", "confirmation_message_id", "confirmed_by_id"},
    },
}


def normalize_admin_target(target: str) -> str | None:
    raw = str(target or "").strip().lower()
    aliases = {
        "case": "case", "cases": "case", "кейс": "case", "кейсы": "case",
        "client": "client", "clients": "client", "клиент": "client", "клиенты": "client",
        "lawyer": "lawyer", "lawyers": "lawyer", "адвокат": "lawyer", "адвокаты": "lawyer",
        "receipt": "receipt", "receipts": "receipt", "чек": "receipt", "чеки": "receipt",
    }
    return aliases.get(raw)


def admin_allowed_fields(target: str) -> list[str]:
    target = normalize_admin_target(target) or target
    cfg = _ADMIN_TARGETS.get(target)
    return sorted(cfg["fields"]) if cfg else []


def _admin_parse_identifier(target: str, identifier: str) -> int | None:
    value = str(identifier or "").strip()
    if target == "case":
        value = value.lstrip("#").replace("SGL-", "").replace("sgl-", "")
    value = value.lstrip("0") or "0"
    try:
        return int(value)
    except ValueError:
        return None


def _admin_record_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def admin_get_record(guild_id: int, target: str, identifier: str) -> dict[str, Any] | None:
    target = normalize_admin_target(target) or ""
    cfg = _ADMIN_TARGETS.get(target)
    if not cfg:
        return None
    parsed_id = _admin_parse_identifier(target, identifier)
    if parsed_id is None:
        return None
    table = cfg["table"]
    id_field = cfg["id_field"]
    with _db_lock, connect() as con:
        row = con.execute(f"SELECT * FROM {table} WHERE guild_id = ? AND {id_field} = ?", (guild_id, parsed_id)).fetchone()
    return _admin_record_to_dict(row)


def admin_format_record(record: dict[str, Any], *, max_value_length: int = 300) -> str:
    lines = []
    for key in sorted(record.keys()):
        value = record.get(key)
        if value is None:
            value_text = "NULL"
        else:
            value_text = str(value).replace("\n", "\\n")
            if len(value_text) > max_value_length:
                value_text = value_text[:max_value_length] + "..."
        lines.append(f"{key}: {value_text}")
    return "\n".join(lines)


def _coerce_admin_value(target: str, field: str, value: str) -> Any:
    if value is None:
        return None
    raw = str(value)
    if raw.strip().lower() in {"null", "none", "пусто", "empty", "__null__"}:
        return None
    cfg = _ADMIN_TARGETS[target]
    if field in cfg["int_fields"]:
        cleaned = re.sub(r"[^0-9-]", "", raw)
        if cleaned in {"", "-"}:
            return None
        return int(cleaned)
    return raw


def admin_update_record(
    *,
    guild_id: int,
    target: str,
    identifier: str,
    field: str,
    value: str,
    actor_id: int | None = None,
    actor_display: str | None = None,
) -> dict[str, Any] | None:
    target = normalize_admin_target(target) or ""
    cfg = _ADMIN_TARGETS.get(target)
    if not cfg:
        raise ValueError("unknown_target")
    field = str(field or "").strip()
    if field not in cfg["fields"]:
        raise ValueError("field_not_allowed")
    parsed_id = _admin_parse_identifier(target, identifier)
    if parsed_id is None:
        raise ValueError("bad_identifier")
    table = cfg["table"]
    id_field = cfg["id_field"]
    new_value = _coerce_admin_value(target, field, value)
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        before = con.execute(f"SELECT * FROM {table} WHERE guild_id = ? AND {id_field} = ?", (guild_id, parsed_id)).fetchone()
        if before is None:
            con.commit()
            return None
        updates = f"{field} = ?"
        params = [new_value]
        if "updated_at" in _table_columns(con, table):
            updates += ", updated_at = ?"
            params.append(now)
        params.extend([guild_id, parsed_id])
        con.execute(f"UPDATE {table} SET {updates} WHERE guild_id = ? AND {id_field} = ?", params)
        after = con.execute(f"SELECT * FROM {table} WHERE guild_id = ? AND {id_field} = ?", (guild_id, parsed_id)).fetchone()
        if target == "case":
            _record_case_event(
                con,
                case_id=int(before["id"]),
                guild_id=guild_id,
                case_number=int(before["case_number"]),
                actor_id=actor_id,
                actor_display=actor_display,
                action="manual_edit",
                details=f"{field}: {before[field] if field in before.keys() else None} -> {new_value}",
            )
        target_types = {
            "case": "sgl_case",
            "client": "client_profile",
            "lawyer": "lawyer_profile",
            "receipt": "sgl_receipt",
        }
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="bureau",
            action_kind="generic_row_restore",
            target_type=target_types[target],
            target_id=int(before["id"]),
            summary=f"Административное изменение {target} #{parsed_id}: {field}",
            payload={"table": table, "row_id": int(before["id"]), "before": dict(before)},
            now=now,
        )
        con.commit()
    return _admin_record_to_dict(after)


def admin_delete_record(
    *,
    guild_id: int,
    target: str,
    identifier: str,
    actor_id: int | None = None,
    actor_display: str | None = None,
) -> dict[str, Any] | None:
    target = normalize_admin_target(target) or ""
    cfg = _ADMIN_TARGETS.get(target)
    if not cfg:
        raise ValueError("unknown_target")
    parsed_id = _admin_parse_identifier(target, identifier)
    if parsed_id is None:
        raise ValueError("bad_identifier")
    table = cfg["table"]
    id_field = cfg["id_field"]
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(f"SELECT * FROM {table} WHERE guild_id = ? AND {id_field} = ?", (guild_id, parsed_id)).fetchone()
        if row is None:
            con.commit()
            return None
        record = _admin_record_to_dict(row)
        if target == "case":
            case_id = int(row["id"])
            case_number = int(row["case_number"])
            receipt_rows = con.execute(
                "SELECT * FROM sgl_receipts WHERE guild_id = ? AND (case_id = ? OR case_number = ?)",
                (guild_id, case_id, case_number),
            ).fetchall()
            event_rows = con.execute(
                "SELECT * FROM sgl_case_events WHERE guild_id = ? AND (case_id = ? OR case_number = ?)",
                (guild_id, case_id, case_number),
            ).fetchall()
            _record_case_event(con, case_id=case_id, guild_id=guild_id, case_number=case_number, actor_id=actor_id, actor_display=actor_display, action="manual_delete", details="case deleted")
            latest_event = con.execute(
                "SELECT * FROM sgl_case_events WHERE guild_id = ? AND case_id = ? ORDER BY id DESC LIMIT 1",
                (guild_id, case_id),
            ).fetchone()
            snapshots = [{"table": "sgl_cases", "row_id": case_id, "before": dict(row)}]
            snapshots.extend(
                {"table": "sgl_receipts", "row_id": int(item["id"]), "before": dict(item)}
                for item in receipt_rows
            )
            snapshots.extend(
                {"table": "sgl_case_events", "row_id": int(item["id"]), "before": dict(item)}
                for item in event_rows
            )
            if latest_event is not None:
                snapshots.append(
                    {"table": "sgl_case_events", "row_id": int(latest_event["id"]), "before": dict(latest_event)}
                )
            _record_bot_action(
                con,
                guild_id=guild_id,
                actor_id=actor_id,
                actor_display=actor_display,
                module="bureau",
                action_kind="generic_rows_restore",
                target_type="sgl_case",
                target_id=case_id,
                summary=f"Удалено дело №{case_number}",
                payload={"rows": snapshots},
            )
            con.execute("DELETE FROM sgl_receipts WHERE guild_id = ? AND (case_id = ? OR case_number = ?)", (guild_id, case_id, case_number))
            con.execute("DELETE FROM sgl_case_events WHERE guild_id = ? AND (case_id = ? OR case_number = ?)", (guild_id, case_id, case_number))
            con.execute("DELETE FROM sgl_cases WHERE guild_id = ? AND id = ?", (guild_id, case_id))
        else:
            target_types = {"client": "client_profile", "lawyer": "lawyer_profile", "receipt": "sgl_receipt"}
            _record_bot_action(
                con,
                guild_id=guild_id,
                actor_id=actor_id,
                actor_display=actor_display,
                module="bureau",
                action_kind="generic_row_restore",
                target_type=target_types[target],
                target_id=int(row["id"]),
                summary=f"Удалена запись {target} #{parsed_id}",
                payload={"table": table, "row_id": int(row["id"]), "before": dict(row)},
            )
            con.execute(f"DELETE FROM {table} WHERE guild_id = ? AND {id_field} = ?", (guild_id, parsed_id))
        con.commit()
    return record

__all__ = ['_ADMIN_TARGETS', 'normalize_admin_target', 'admin_allowed_fields', '_admin_parse_identifier', '_admin_record_to_dict', 'admin_get_record', 'admin_format_record', '_coerce_admin_value', 'admin_update_record', 'admin_delete_record']
