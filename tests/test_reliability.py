import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage
from modules import reliability
from persistence.database_guard import create_database_backup


class ReliabilityContourTests(unittest.IsolatedAsyncioTestCase):
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
                "TMOD_UPDATE_STATUS_FILE": str(self.root / "updates" / "status.json"),
                "TMOD_RELEASE": "abc123",
            },
        )
        self.environment.start()
        storage.init_db()
        create_database_backup("manual")
        reliability._domain_cache = None
        reliability._domain_lock = None

    def tearDown(self) -> None:
        reliability._domain_cache = None
        reliability._domain_lock = None
        self.environment.stop()
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    async def test_snapshot_combines_database_domains_release_and_update(self) -> None:
        status_path = self.root / "updates" / "status.json"
        status_path.parent.mkdir(parents=True)
        status_path.write_text(
            json.dumps(
                {
                    "state": "success",
                    "release": "abc123",
                    "message": "deployment healthy",
                }
            ),
            encoding="utf-8",
        )
        report = {
            "status": "ok",
            "url": "https://example.test",
            "http_status": 200,
            "latency_ms": 12.0,
            "certificate_days_remaining": 80,
        }
        with patch("modules.reliability._probe_https", return_value=report):
            snapshot = await reliability.reliability_snapshot(force=True)
        self.assertEqual(snapshot["overall"], "ok")
        self.assertEqual(snapshot["release"], "abc123")
        self.assertEqual(snapshot["update"]["state"], "success")
        self.assertEqual(snapshot["database"]["status"], "ok")
        self.assertEqual(len(snapshot["domains"]), len(reliability.PUBLIC_SURFACES))

    async def test_tls_failure_is_visible_as_critical(self) -> None:
        with patch(
            "modules.reliability._probe_https",
            return_value={
                "status": "critical",
                "url": "https://atlas.tvr.lat",
                "error": "SSLError: certificate unavailable",
            },
        ):
            snapshot = await reliability.reliability_snapshot(force=True)
        self.assertEqual(snapshot["overall"], "critical")
        self.assertTrue(all(item["status"] == "critical" for item in snapshot["domains"]))

    def test_windows_powershell_bom_status_is_readable(self) -> None:
        status_path = self.root / "updates" / "status.json"
        status_path.parent.mkdir(parents=True)
        status_path.write_bytes(
            b"\xef\xbb\xbf" + json.dumps({"state": "rolled_back"}).encode("utf-8")
        )
        self.assertEqual(reliability.read_update_status()["state"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
