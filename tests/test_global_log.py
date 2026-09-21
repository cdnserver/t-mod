from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from modules import global_log_runtime as runtime
from modules.global_log_runtime import redact_value, scrub_text
from persistence import global_log_repository as repository


def test_redaction_removes_nested_credentials() -> None:
    result = redact_value(
        {
            "user": "902235631952998410",
            "password": "super-secret",
            "nested": {"api_key": "key-value", "message": "Authorization: Bearer abc.def.ghi"},
            "pin_code": 12345678,
        }
    )
    encoded = json.dumps(result, ensure_ascii=False)
    assert "super-secret" not in encoded
    assert "key-value" not in encoded
    assert "12345678" not in encoded
    assert "abc.def.ghi" not in encoded
    assert result["user"] == "902235631952998410"


def test_scrub_text_keeps_normal_text_and_removes_tokens() -> None:
    text = scrub_text("ОВР: принято. token=hello-world password:unsafe")
    assert text.startswith("ОВР: принято.")
    assert "hello-world" not in text
    assert "unsafe" not in text
    assert text.count("[REDACTED]") == 2


def test_prepare_event_bounds_chain_material() -> None:
    event = repository.prepare_event(
        {
            "event_uuid": "00000000-0000-0000-0000-000000000001",
            "occurred_at": "2026-08-30T12:00:00+00:00",
            "event_type": "test",
            "summary": "x" * 5000,
            "content_text": "y" * 120_000,
            "details": {"payload": "z" * 300_000},
        }
    )
    assert len(event["summary"]) == 4000
    assert len(event["content_text"]) == 100_000
    assert event["details"]["truncated"] is True
    canonical = repository.canonical_event_payload(event, "0" * 64)
    assert hashlib.sha256(canonical.encode()).hexdigest() == hashlib.sha256(canonical.encode()).hexdigest()


def test_allowlist_has_strict_first_slot(monkeypatch) -> None:
    monkeypatch.delenv("GLOBAL_LOG_ALLOWED_USER_ID_1", raising=False)
    monkeypatch.delenv("GLOBAL_LOG_ALLOWED_USER_ID_2", raising=False)
    assert repository.allowed_user_ids() == (902235631952998410,)
    assert repository.user_is_allowed(902235631952998410)
    assert not repository.user_is_allowed(811862068214890537)
    monkeypatch.setenv("GLOBAL_LOG_ALLOWED_USER_ID_2", "811862068214890537")
    assert repository.user_is_allowed(811862068214890537)


def test_database_name_rejects_sql_identifiers(monkeypatch) -> None:
    monkeypatch.setenv("GLOBAL_LOG_DATABASE", "tmod_global_log; DROP DATABASE tmod")
    try:
        repository.global_log_database_name()
    except RuntimeError as exc:
        assert str(exc) == "global_log_database_name_invalid"
    else:
        raise AssertionError("unsafe database identifier accepted")


def test_schema_initialization_is_serialized_and_non_destructive() -> None:
    source = Path(repository.__file__).read_text(encoding="utf-8")
    initializer = source.split("def initialize_global_log()", 1)[1].split(
        "\ndef _pepper()", 1
    )[0]
    assert "pg_advisory_xact_lock" in initializer
    assert "_SCHEMA_LOCK_ID" in initializer
    assert "CREATE INDEX IF NOT EXISTS global_log_events_search_v2_idx" in initializer
    assert "CREATE INDEX IF NOT EXISTS global_log_events_search_idx" not in initializer
    assert "DROP INDEX" not in initializer


def test_event_projection_is_json_serializable() -> None:
    names = ["id", "event_uuid", "occurred_at", "ingested_at", "details"]
    now = datetime.now(timezone.utc)
    projected = repository._event_dict(
        (1, UUID("00000000-0000-0000-0000-000000000001"), now, now, {"ok": True}),
        names,
    )
    assert projected["event_uuid"] == "00000000-0000-0000-0000-000000000001"
    json.dumps(projected)


def test_verify_chain_pages_through_complete_ledger(monkeypatch) -> None:
    names = [
        "event_uuid", "occurred_at", "source_service", "source_type", "event_type",
        "severity", "actor_user_id", "actor_display", "guild_id", "channel_id",
        "message_id", "request_id", "session_id", "target_type", "target_id",
        "summary", "content_text", "details", "ip_hash", "user_agent", "status_code",
        "duration_ms", "previous_hash", "event_hash", "id",
    ]
    rows = []
    previous = "0" * 64
    for identifier in range(1, 4):
        event = repository.prepare_event({
            "event_uuid": f"00000000-0000-0000-0000-{identifier:012d}",
            "occurred_at": f"2026-08-30T12:00:0{identifier}+00:00",
            "event_type": "test",
            "summary": f"event {identifier}",
            "details": {"identifier": identifier},
        })
        event["previous_hash"] = previous
        event["event_hash"] = hashlib.sha256(
            repository.canonical_event_payload(event, previous).encode()
        ).hexdigest()
        event["id"] = identifier
        previous = event["event_hash"]
        event["occurred_at"] = datetime.fromisoformat(event["occurred_at"])
        rows.append(tuple(event.get(name) for name in names))

    class Cursor:
        def __init__(self) -> None:
            self.current = []
            self.page_calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=()):
            if str(query).startswith("SET TRANSACTION"):
                return
            if "COUNT(*)" in str(query):
                self.current = [(len(rows),)]
                return
            self.page_calls += 1
            start = int(params[0]) if "WHERE id >" in str(query) else 0
            size = int(params[-1])
            self.current = [row for row in rows if int(row[-1]) > start][:size]

        def fetchone(self):
            return self.current[0] if self.current else None

        def fetchall(self):
            return self.current

    cursor = Cursor()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return cursor

    class Pool:
        def connection(self):
            return Connection()

    monkeypatch.setattr(repository, "initialize_global_log", lambda: None)
    monkeypatch.setattr(repository, "_connection_pool", lambda: Pool())
    result = repository.verify_chain(limit=2)
    assert result["ok"] is True
    assert result["checked"] == 3
    assert result["total"] == 3
    assert cursor.page_calls == 2


def test_global_log_spool_has_hard_ceiling_and_reports_overflow(monkeypatch, tmp_path: Path) -> None:
    spool = tmp_path / "events.jsonl"
    monkeypatch.setenv("GLOBAL_LOG_SPOOL_FILE", str(spool))
    monkeypatch.setattr(runtime, "_spool_max_bytes", lambda: 256)
    before = runtime._spool_overflow_dropped
    runtime._spool_events([{"event": "old", "payload": "x" * 120}])
    runtime._spool_events([{"event": "new", "payload": "y" * 120}])
    assert spool.stat().st_size <= 256
    assert runtime._spool_overflow_dropped > before
    assert runtime.runtime_health()["spool_max_bytes"] == 256


def test_sql_audit_rollups_compress_repeated_queries() -> None:
    with runtime._sql_rollup_lock:
        runtime._sql_rollups.clear()
    payload = {
        "fingerprint": "a" * 64,
        "operation": "SELECT",
        "outcome": "success",
        "tables": ["members"],
        "statement": "SELECT name FROM members WHERE user_id = ?",
        "duration_ms": 12.5,
        "rowcount": 1,
    }
    runtime._record_sql_rollup(payload)
    runtime._record_sql_rollup(payload)
    values = runtime._drain_sql_rollups()
    assert len(values) == 1
    assert values[0]["count"] == 2
    assert values[0]["duration_total_ms"] == 25.0
    assert values[0]["rowcount_total"] == 2
