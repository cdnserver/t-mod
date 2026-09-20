import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from persistence import postgres_guard


class PostgresGuardStartupTests(unittest.TestCase):
    def test_concurrent_worker_backup_does_not_restart_the_bot(self) -> None:
        integrity = {
            "ok": True,
            "result": "ok",
            "checked_at": "2026-09-20T08:00:00+00:00",
        }
        with (
            patch.object(postgres_guard, "list_database_backups", return_value=[]),
            patch.object(
                postgres_guard,
                "postgres_safe_target",
                return_value="postgresql://tmod-postgres:5432/tmod",
            ),
            patch.object(
                postgres_guard,
                "create_database_backup",
                side_effect=RuntimeError("database_backup_already_running"),
            ),
            patch.object(
                postgres_guard,
                "check_live_database",
                return_value=integrity,
            ),
        ):
            result = postgres_guard.ensure_startup_recovery_point(
                now=datetime(2026, 9, 20, 8, tzinfo=timezone.utc)
            )

        self.assertTrue(result["reused"])
        self.assertEqual(result["name"], "concurrent-postgresql-backup")
        self.assertEqual(result["reuse_reason"], "validated_concurrent_backup")
        self.assertEqual(result["integrity"], integrity)

    def test_concurrent_backup_still_fails_closed_on_bad_database(self) -> None:
        with (
            patch.object(postgres_guard, "list_database_backups", return_value=[]),
            patch.object(
                postgres_guard,
                "postgres_safe_target",
                return_value="postgresql://tmod-postgres:5432/tmod",
            ),
            patch.object(
                postgres_guard,
                "create_database_backup",
                side_effect=RuntimeError("database_backup_already_running"),
            ),
            patch.object(
                postgres_guard,
                "check_live_database",
                return_value={"ok": False, "result": "broken"},
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "concurrent_database_backup_integrity_failed"
            ):
                postgres_guard.ensure_startup_recovery_point()


if __name__ == "__main__":
    unittest.main()
