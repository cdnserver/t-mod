import sqlite3
import tempfile
import unittest
from pathlib import Path

import storage
from persistence import web_auth_repository as web_auth


class WebAuthRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "web-auth-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_configure_authenticate_and_never_store_plain_pin(self) -> None:
        credential = web_auth.configure_web_credential(10, 20, "Robert.Admin", "12345678")

        self.assertEqual(credential.login, "robert.admin")
        result = web_auth.authenticate_web_credential(10, "ROBERT.ADMIN", "12345678")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.credential.user_id, 20)
        with sqlite3.connect(storage.DATABASE_FILE) as con:
            stored = con.execute(
                "SELECT pin_hash FROM web_credentials WHERE guild_id = 10 AND user_id = 20"
            ).fetchone()[0]
        self.assertNotIn("12345678", stored)
        self.assertTrue(stored.startswith("scrypt$"))

    def test_login_is_unique_inside_guild_and_pin_is_exactly_eight_digits(self) -> None:
        web_auth.configure_web_credential(10, 20, "senator", "12345678")
        with self.assertRaisesRegex(ValueError, "web_login_taken"):
            web_auth.configure_web_credential(10, 21, "SENATOR", "87654321")
        with self.assertRaisesRegex(ValueError, "web_pin_invalid"):
            web_auth.configure_web_credential(10, 22, "another", "1234abcd")
        web_auth.configure_web_credential(11, 21, "senator", "87654321")

    def test_three_bad_attempts_require_discord_reset(self) -> None:
        web_auth.configure_web_credential(10, 20, "senator", "12345678")
        for attempt in range(2):
            result = web_auth.authenticate_web_credential(
                10, "senator", "00000000", now_epoch=1000 + attempt
            )
            self.assertEqual(result.status, "invalid")
            self.assertTrue(result.notify_owner)
            self.assertEqual(result.failed_attempts, attempt + 1)
        result = web_auth.authenticate_web_credential(
            10, "senator", "00000000", now_epoch=1002
        )
        self.assertEqual(result.status, "reset_required")
        self.assertTrue(result.notify_owner)
        self.assertFalse(web_auth.web_session_version_matches(10, 20, 1))
        still_blocked = web_auth.authenticate_web_credential(
            10, "senator", "12345678", now_epoch=5000
        )
        self.assertEqual(still_blocked.status, "reset_required")
        self.assertFalse(still_blocked.notify_owner)

        reset = web_auth.configure_web_credential(10, 20, "senator", "87654321")
        self.assertFalse(reset.reset_required)
        self.assertEqual(reset.failed_attempts, 0)
        unlocked = web_auth.authenticate_web_credential(10, "senator", "87654321")
        self.assertEqual(unlocked.status, "ok")

    def test_change_and_delete_revoke_persistent_sessions(self) -> None:
        first = web_auth.configure_web_credential(10, 20, "senator", "12345678")
        self.assertTrue(web_auth.web_session_version_matches(10, 20, first.session_version))
        second = web_auth.configure_web_credential(10, 20, "senator", "87654321")
        self.assertGreater(second.session_version, first.session_version)
        self.assertFalse(web_auth.web_session_version_matches(10, 20, first.session_version))
        self.assertTrue(web_auth.delete_web_credential(10, 20))
        self.assertFalse(web_auth.web_session_version_matches(10, 20, second.session_version))


if __name__ == "__main__":
    unittest.main()
