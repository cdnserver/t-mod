"""Create a subject-only, security-redacted T-Mod personal-data archive."""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Any

from modules.global_log_runtime import redact_value
from persistence.core import connect_readonly, postgres_enabled
from persistence import global_log_repository


_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_SUBJECT_COLUMNS = frozenset(
    {
        "user_id",
        "discord_id",
        "discord_user_id",
        "account_user_id",
        "owner_user_id",
        "author_user_id",
        "actor_user_id",
        "assigned_user_id",
        "target_user_id",
        "source_user_id",
        "host_user_id",
        "guest_user_id",
        "author_id",
        "actor_id",
        "created_by_id",
        "requester_id",
        "recipient_id",
        "moderator_id",
        "member_id",
    }
)
_SECURITY_COLUMNS = re.compile(
    r"(?:password|pin_hash|secret|token|cookie|session_key|code_hash|receipt_key|device_hash|fingerprint)",
    re.IGNORECASE,
)
_MAX_GLOBAL_EVENTS = 50_000


def _table_columns() -> dict[str, list[str]]:
    with connect_readonly() as con:
        if postgres_enabled():
            rows = con.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
                """
            ).fetchall()
            result: dict[str, list[str]] = {}
            for row in rows:
                result.setdefault(str(row["table_name"]), []).append(
                    str(row["column_name"])
                )
            return result
        tables = con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        result = {}
        for row in tables:
            table = str(row["name"])
            if not _IDENTIFIER.fullmatch(table):
                continue
            result[table] = [
                str(column["name"])
                for column in con.execute(f'PRAGMA table_info("{table}")').fetchall()
            ]
        return result


def _sanitize(value: Any, *, key: str, subject_id: int) -> Any:
    if _SECURITY_COLUMNS.search(key):
        return "[не раскрывается для безопасности аккаунта]"
    if key in _SUBJECT_COLUMNS and value not in (None, ""):
        try:
            if int(value) != int(subject_id):
                return "[идентификатор другого лица скрыт]"
        except (TypeError, ValueError):
            return "[идентификатор другого лица скрыт]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize(item_value, key=str(item_key), subject_id=subject_id)
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, key=key, subject_id=subject_id) for item in value]
    if isinstance(value, bytes):
        return {"binary": True, "size_bytes": len(value)}
    if isinstance(value, str) and key.endswith("_json"):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return redact_value(value, key=key)
        return _sanitize(parsed, key=key, subject_id=subject_id)
    return redact_value(value, key=key)


def _sanitize_row(row: Any, subject_id: int) -> dict[str, Any]:
    return {
        str(key): _sanitize(value, key=str(key), subject_id=subject_id)
        for key, value in dict(row).items()
    }


def _main_records(subject_id: int) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    tables = _table_columns()
    with connect_readonly() as con:
        for table, columns in tables.items():
            if not _IDENTIFIER.fullmatch(table):
                continue
            identity_columns = [column for column in columns if column in _SUBJECT_COLUMNS]
            if not identity_columns:
                continue
            clauses = " OR ".join(f'"{column}" = ?' for column in identity_columns)
            rows = con.execute(
                f'SELECT * FROM "{table}" WHERE {clauses} ORDER BY 1',
                tuple(int(subject_id) for _ in identity_columns),
            ).fetchall()
            if rows:
                result[table] = {
                    "count": len(rows),
                    "records": [_sanitize_row(row, subject_id) for row in rows],
                }
    return result


def _global_events(subject_id: int) -> dict[str, Any]:
    if not global_log_repository.global_log_enabled():
        return {"count": 0, "included": 0, "records": []}
    count, rows = global_log_repository.personal_data_events(
        int(subject_id),
        limit=_MAX_GLOBAL_EVENTS,
    )
    return {
        "count": count,
        "included": len(rows),
        "truncated": count > len(rows),
        "records": [_sanitize(row, key="event", subject_id=subject_id) for row in rows],
    }


def build_personal_data_archive(request: dict[str, Any]) -> bytes:
    """Return a ZIP suitable for delivery only to the verified data subject."""

    subject_id = int(request.get("account_user_id") or 0)
    discord_id = int(request.get("discord_id") or 0)
    if subject_id <= 0 or subject_id != discord_id:
        raise ValueError("privacy_identity_not_verified")
    request_code = str(request.get("request_code") or "DSR")
    payload = {
        "format": "T-Mod personal data access package v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "request": {
            "code": request_code,
            "type": str(request.get("request_type") or "access"),
            "scope": str(request.get("scope") or ""),
            "created_at": request.get("created_at"),
        },
        "subject": {"discord_user_id": subject_id},
        "main_database": _main_records(subject_id),
        "activity_and_security_log": _global_events(subject_id),
        "notes": [
            "Секреты входа, хэши паролей, токены и идентификаторы других лиц скрыты.",
            "Записи представлены в том виде, в котором они хранились на момент выгрузки.",
        ],
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(
            "README.txt",
            (
                "T-Mod · копия персональных данных\n\n"
                f"Запрос: {request_code}\n"
                "Архив предназначен только владельцу Discord-аккаунта. "
                "Не пересылайте его третьим лицам.\n"
                "Основной файл: personal-data.json\n"
            ),
        )
        archive.writestr("personal-data.json", data)
    return output.getvalue()


__all__ = ["build_personal_data_archive"]
