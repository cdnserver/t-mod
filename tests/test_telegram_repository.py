from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import storage
from persistence import atlas_repository
from persistence import telegram_repository as telegram


class TelegramRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "telegram-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_challenge_is_one_time_and_binds_identity(self) -> None:
        challenge = telegram.create_link_challenge(77, 42)
        linked = telegram.consume_link_challenge(
            challenge["code"],
            guild_id=77,
            telegram_user_id=900,
            telegram_chat_id=901,
            telegram_username="atlas_user",
            telegram_display_name="Atlas User",
        )
        self.assertTrue(linked["ok"])
        self.assertEqual(telegram.get_link_by_discord(77, 42)["telegram_user_id"], 900)
        replay = telegram.consume_link_challenge(
            challenge["code"],
            guild_id=77,
            telegram_user_id=900,
            telegram_chat_id=901,
        )
        self.assertEqual(replay["error"], "invalid_or_expired_code")

    def test_telegram_identity_cannot_be_claimed_by_another_account(self) -> None:
        first = telegram.create_link_challenge(77, 42)
        self.assertTrue(
            telegram.consume_link_challenge(
                first["code"], guild_id=77, telegram_user_id=900, telegram_chat_id=901
            )["ok"]
        )
        second = telegram.create_link_challenge(77, 43)
        result = telegram.consume_link_challenge(
            second["code"], guild_id=77, telegram_user_id=900, telegram_chat_id=901
        )
        self.assertEqual(result["error"], "telegram_already_linked")

    def _link_telegram(self, discord_user_id: int = 42, telegram_user_id: int = 900) -> None:
        challenge = telegram.create_link_challenge(77, discord_user_id)
        result = telegram.consume_link_challenge(
            challenge["code"],
            guild_id=77,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=901,
            telegram_username="atlas_user",
            telegram_display_name="Atlas User",
        )
        self.assertTrue(result["ok"])

    def test_telegram_login_code_is_one_time_and_only_for_linked_identity(self) -> None:
        self._link_telegram()
        with self.assertRaisesRegex(ValueError, "telegram_account_not_linked"):
            telegram.create_telegram_login_code(77, 42, 901)

        challenge = telegram.create_telegram_login_code(77, 42, 900)
        self.assertEqual(len(challenge["code"]), 8)
        self.assertTrue(challenge["code"].isascii() and challenge["code"].isdigit())
        authenticated = telegram.consume_telegram_login_code(challenge["code"], guild_id=77)
        self.assertTrue(authenticated["ok"])
        self.assertEqual(authenticated["discord_user_id"], 42)
        self.assertEqual(authenticated["telegram_user_id"], 900)
        replay = telegram.consume_telegram_login_code(challenge["code"], guild_id=77)
        self.assertEqual(replay["error"], "invalid_or_expired_code")

    def test_telegram_login_code_cannot_be_reissued_in_a_short_burst(self) -> None:
        self._link_telegram()
        first = telegram.create_telegram_login_code(77, 42, 900)
        with self.assertRaisesRegex(ValueError, "telegram_login_code_rate_limited"):
            telegram.create_telegram_login_code(77, 42, 900)
        # A rate-limited request must not invalidate the code already delivered.
        self.assertTrue(telegram.consume_telegram_login_code(first["code"], guild_id=77)["ok"])

    def test_thread_mapping_round_trips(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Test")
        organization_id = int(dashboard["organization"]["id"])
        thread_id = atlas_repository.atlas_create_thread(organization_id, 42, "Telegram")
        saved = telegram.save_atlas_thread(77, 42, 901, organization_id, thread_id)
        stored = telegram.get_atlas_thread(77, 42, 901)
        self.assertEqual(
            {key: stored[key] for key in saved},
            saved,
        )
        self.assertTrue(telegram.reset_atlas_thread(77, 42, 901))
        self.assertIsNone(telegram.get_atlas_thread(77, 42, 901))
        self.assertFalse(telegram.reset_atlas_thread(77, 42, 901))


if __name__ == "__main__":
    unittest.main()
