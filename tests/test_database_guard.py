import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import storage
from persistence import database_guard
from persistence.core import connect


class DatabaseGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        storage.DATA_DIR = self.root / "data"
        storage.DATABASE_FILE = storage.DATA_DIR / "tmod.db"
        self.environment = patch.dict(
            os.environ,
            {
                "TMOD_DB_BACKUP_DIR": str(self.root / "backups"),
                "TMOD_DB_HOURLY_RETENTION": "2",
                "TMOD_DB_DAILY_RETENTION": "2",
                "TMOD_DB_MANUAL_RETENTION": "2",
                "TMOD_DB_UPDATE_RETENTION": "2",
                "TMOD_DB_MIN_FREE_BYTES": str(64 * 1024 * 1024),
            },
        )
        self.environment.start()
        storage.init_db()

    def tearDown(self) -> None:
        self.environment.stop()
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_online_backup_contains_committed_wal_data_and_is_validated(self) -> None:
        with connect() as con:
            con.execute(
                "INSERT INTO meta(key, value, updated_at) VALUES('guard-test', 'preserved', '2026-08-08T00:00:00+00:00')"
            )
            con.commit()
        result = database_guard.create_database_backup("manual", note="unit test")
        backup = Path(result["path"])
        self.assertTrue(backup.exists())
        self.assertTrue(result["integrity"]["ok"])
        with sqlite3.connect(backup) as con:
            self.assertEqual(
                con.execute(
                    "SELECT value FROM meta WHERE key = 'guard-test'"
                ).fetchone()[0],
                "preserved",
            )
        snapshot = database_guard.database_protection_snapshot()
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["latest"]["kind"], "manual")

    def test_scheduled_policy_creates_hourly_daily_and_integrity_state(self) -> None:
        now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        first = database_guard.run_scheduled_database_protection(now=now)
        self.assertEqual({item["kind"] for item in first["created"]}, {"hourly", "daily"})
        self.assertTrue(first["integrity"]["ok"])
        second = database_guard.run_scheduled_database_protection(
            now=now + timedelta(minutes=30)
        )
        self.assertEqual(second["created"], [])
        third = database_guard.run_scheduled_database_protection(
            now=now + timedelta(days=1, minutes=1)
        )
        self.assertEqual({item["kind"] for item in third["created"]}, {"hourly", "daily"})

    def test_restore_requires_offline_confirmation_and_replaces_database(self) -> None:
        with connect() as con:
            con.execute(
                "INSERT INTO meta(key, value, updated_at) VALUES('restore-test', 'before', '2026-08-08T00:00:00+00:00')"
            )
            con.commit()
        backup = database_guard.create_database_backup("pre-update")
        with connect() as con:
            con.execute("UPDATE meta SET value = 'after' WHERE key = 'restore-test'")
            con.commit()
        with self.assertRaises(PermissionError):
            database_guard.restore_database_backup(backup["path"])
        restored = database_guard.restore_database_backup(
            backup["path"], offline_confirmed=True
        )
        self.assertTrue(restored["integrity"]["ok"])
        with sqlite3.connect(storage.DATABASE_FILE) as con:
            self.assertEqual(
                con.execute(
                    "SELECT value FROM meta WHERE key = 'restore-test'"
                ).fetchone()[0],
                "before",
            )

    def test_fresh_startup_recovery_point_prevents_duplicate_boot_snapshots(self) -> None:
        now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        database_guard.create_database_backup("startup", now=now)
        report = database_guard.run_scheduled_database_protection(
            now=now + timedelta(minutes=1)
        )
        self.assertEqual(report["created"], [])

    def test_retention_prunes_oldest_snapshots_per_kind(self) -> None:
        base = datetime(2026, 8, 8, 10, 0, tzinfo=timezone.utc)
        for offset in range(4):
            database_guard.create_database_backup(
                "manual", now=base + timedelta(seconds=offset)
            )
        items = [
            item
            for item in database_guard.list_database_backups(limit=20)
            if item["kind"] == "manual"
        ]
        self.assertEqual(len(items), 2)
        self.assertGreater(items[0]["created_at"], items[1]["created_at"])


if __name__ == "__main__":
    unittest.main()
