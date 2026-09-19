import os
import unittest
from unittest.mock import patch

from persistence import postgres_guard


class PostgresGuardExternalProtectionTests(unittest.TestCase):
    def test_startup_uses_localized_external_recovery_policy(self) -> None:
        integrity = {"ok": True, "result": "ok", "checked_at": "2026-09-19T18:00:00+00:00"}
        with (
            patch.dict(os.environ, {"TMOD_DB_EXTERNAL_PROTECTION": "true"}),
            patch.object(postgres_guard, "check_live_database", return_value=integrity),
            patch.object(
                postgres_guard,
                "postgres_safe_target",
                return_value="postgresql://localized/tmod",
            ),
            patch.object(postgres_guard, "create_database_backup") as create_backup,
        ):
            result = postgres_guard.ensure_startup_recovery_point()

        self.assertEqual(result["name"], "localized-data-node")
        self.assertTrue(result["reused"])
        self.assertEqual(result["integrity"], integrity)
        create_backup.assert_not_called()

    def test_scheduled_cycle_checks_health_without_exporting_remote_database(self) -> None:
        integrity = {"ok": True, "result": "ok"}
        snapshot = {"status": "ok", "external_protection": True}
        with (
            patch.dict(os.environ, {"TMOD_DB_EXTERNAL_PROTECTION": "true"}),
            patch.object(postgres_guard, "check_live_database", return_value=integrity),
            patch.object(postgres_guard, "database_protection_snapshot", return_value=snapshot),
            patch.object(postgres_guard, "create_database_backup") as create_backup,
        ):
            result = postgres_guard.run_scheduled_database_protection()

        self.assertEqual(result["created"], [])
        self.assertTrue(result["external"])
        self.assertEqual(result["integrity"], integrity)
        create_backup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
