from __future__ import annotations

import hashlib
import json

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
