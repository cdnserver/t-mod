import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import storage
from modules.error_inbox import (
    ErrorInboxConfig,
    GitHubIssuePublisher,
    capture_runtime_event,
    sanitize_error_text,
)
from persistence import error_repository


def inbox_config(**overrides) -> ErrorInboxConfig:
    values = {
        "capture_enabled": True,
        "publish_enabled": True,
        "repository": "cdnserver/t-mod",
        "token": "test-token",
        "environment": "test",
        "release": "abc123",
        "label": "tmod-runtime",
        "interval_seconds": 30,
        "batch_size": 10,
        "comment_cooldown_seconds": 1800,
        "retention_days": 90,
        "request_timeout_seconds": 10,
    }
    values.update(overrides)
    return ErrorInboxConfig(**values)


class ErrorInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "error-inbox-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_sanitizer_removes_tokens_passwords_and_private_keys(self) -> None:
        fake_discord_token = f"{'A' * 24}.{'B' * 6}.{'C' * 32}"
        raw = (
            "Authorization: Bearer super-secret-value\n"
            "password=do-not-publish\n"
            f"{fake_discord_token}\n"
            "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
        )
        clean = sanitize_error_text(raw)
        self.assertNotIn("super-secret-value", clean)
        self.assertNotIn("do-not-publish", clean)
        self.assertNotIn(fake_discord_token, clean)
        self.assertNotIn("BEGIN PRIVATE KEY", clean)
        self.assertGreaterEqual(clean.count("[REDACTED]"), 4)

    def test_capture_is_durable_and_deduplicated(self) -> None:
        async def capture_twice() -> None:
            for details in ("first", "second"):
                captured = await capture_runtime_event(
                    title="Database unavailable",
                    details=details,
                    component="craft",
                    fingerprint_hint="craft:database-unavailable",
                    config=inbox_config(publish_enabled=False, token=""),
                )
                self.assertTrue(captured)

        asyncio.run(capture_twice())
        summary = error_repository.runtime_error_inbox_summary()
        self.assertEqual(summary, {"total": 1, "pending": 1, "published": 0})
        ready = error_repository.list_runtime_errors_ready(
            now="2999-08-02T12:00:00+00:00",
            comment_cooldown_seconds=0,
        )
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["occurrences"], 2)
        self.assertEqual(ready[0]["details"], "second")

    def test_publish_ack_does_not_lose_a_concurrent_recurrence(self) -> None:
        first = error_repository.record_runtime_error(
            fingerprint="same",
            title="Failure",
            component="test",
            level="error",
            exception_type="RuntimeError",
            details="first",
            traceback_text=None,
            environment="test",
            release="one",
            now="2026-08-02T10:00:00+00:00",
        )
        error_repository.record_runtime_error(
            fingerprint="same",
            title="Failure",
            component="test",
            level="error",
            exception_type="RuntimeError",
            details="second",
            traceback_text=None,
            environment="test",
            release="one",
            now="2026-08-02T10:00:01+00:00",
        )
        current = error_repository.mark_runtime_error_published(
            "same",
            published_occurrences=int(first["occurrences"]),
            github_issue_number=42,
            github_issue_url="https://github.com/cdnserver/t-mod/issues/42",
            now="2026-08-02T10:00:02+00:00",
        )
        self.assertIsNotNone(current)
        self.assertEqual(current["pending"], 1)
        self.assertEqual(current["occurrences"], 2)

    def test_failed_delivery_uses_exponential_retry(self) -> None:
        error_repository.record_runtime_error(
            fingerprint="retry",
            title="Failure",
            component="test",
            level="error",
            exception_type=None,
            details="network",
            traceback_text=None,
            environment="test",
            release="one",
            now="2026-08-02T10:00:00+00:00",
        )
        first = error_repository.mark_runtime_error_failed(
            "retry",
            error="github_transport_error",
            now="2026-08-02T10:00:00+00:00",
        )
        second = error_repository.mark_runtime_error_failed(
            "retry",
            error="github_transport_error",
            now="2026-08-02T10:00:05+00:00",
        )
        self.assertEqual(first["attempt_count"], 1)
        self.assertEqual(first["next_attempt_at"], "2026-08-02T10:00:05+00:00")
        self.assertEqual(second["attempt_count"], 2)
        self.assertEqual(second["next_attempt_at"], "2026-08-02T10:00:15+00:00")

    def test_publisher_creates_then_reopens_and_comments_on_same_issue(self) -> None:
        publisher = GitHubIssuePublisher(inbox_config())
        publisher._request = MagicMock(
            return_value={
                "number": 17,
                "html_url": "https://github.com/cdnserver/t-mod/issues/17",
            }
        )
        record = {
            "fingerprint": "abc",
            "title": "Craft failed",
            "component": "craft",
            "level": "error",
            "exception_type": "RuntimeError",
            "details": "database unavailable",
            "traceback_text": "Traceback...",
            "environment": "production",
            "release": "abc123",
            "first_seen_at": "2026-08-02T10:00:00+00:00",
            "last_seen_at": "2026-08-02T10:00:00+00:00",
            "occurrences": 1,
            "github_issue_number": None,
            "github_issue_url": None,
        }
        number, url = publisher.publish(record)
        self.assertEqual(number, 17)
        self.assertTrue(url.endswith("/17"))
        create_call = publisher._request.call_args
        self.assertEqual(create_call.args[:2], ("POST", "/repos/cdnserver/t-mod/issues"))
        self.assertIn("tmod-runtime:abc", create_call.args[2]["body"])

        publisher._request.reset_mock()
        publisher._request.return_value = {}
        record["github_issue_number"] = 17
        record["github_issue_url"] = url
        publisher.publish(record)
        calls = publisher._request.call_args_list
        self.assertEqual(calls[0].args[:2], ("PATCH", "/repos/cdnserver/t-mod/issues/17"))
        self.assertEqual(calls[1].args[:2], ("POST", "/repos/cdnserver/t-mod/issues/17/comments"))


if __name__ == "__main__":
    unittest.main()
