import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage
from modules.overlay_remote_discord import overlay_operator_ids, resolve_overlay_recipient
from persistence import atlas_repository, blackbird_communicate_repository as communicate
from persistence import profile_repository, web_auth_repository


class BlackbirdCommunicateTests(unittest.TestCase):
    def setUp(self):
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "communicate-test.db"
        storage.init_db()

    def tearDown(self):
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_character_search_is_exact_and_opt_in(self):
        character = profile_repository.add_profile_character(77, 100, "Robert Example", "263345")
        atlas_repository.atlas_set_overlay_character(77, 100, character.id, server_code="phoenix-15", faction_code="lspd")
        self.assertIsNone(communicate.search_character(77, 200, "phoenix-15", "263345"))
        communicate.set_discoverable(77, 100, True)
        found = communicate.search_character(77, 200, "phoenix-15", "263345")
        self.assertEqual(found["user_id"], 100)
        self.assertEqual(found["nickname"], "Robert Example")
        self.assertIsNone(communicate.search_character(77, 200, "phoenix-14", "263345"))
        self.assertIsNone(communicate.search_character(77, 100, "phoenix-15", "263345"))
        profile_repository.set_profile_character_visibility(77, 100, character.id, is_public=False)
        self.assertIsNone(communicate.search_character(77, 200, "phoenix-15", "263345"))

    def test_private_thread_and_rate_limit(self):
        with self.assertRaisesRegex(ValueError, "communicate_recipient_unavailable"):
            communicate.send_message(77, 200, 100, "hello")
        communicate.set_discoverable(77, 100, True)
        result = communicate.send_message(77, 200, 100, "hello")
        self.assertEqual(communicate.conversation(77, 100, 200)[0]["id"], result["id"])
        self.assertEqual(communicate.conversation(77, 300, 200), [])
        self.assertEqual(communicate.list_conversations(77, 200)[0]["partner_id"], 100)
        for _ in range(11):
            communicate.send_message(77, 200, 100, "another")
        with self.assertRaisesRegex(ValueError, "communicate_rate_limited"):
            communicate.send_message(77, 200, 100, "too many")
        communicate.set_discoverable(77, 100, False)
        with self.assertRaisesRegex(ValueError, "communicate_recipient_unavailable"):
            communicate.send_message(77, 200, 100, "opted out")

    def test_overlay_operator_and_exact_account_resolution(self):
        web_auth_repository.configure_web_credential(77, 100, "robert", "12345678")
        self.assertEqual(resolve_overlay_recipient(77, "100"), 100)
        self.assertEqual(resolve_overlay_recipient(77, "RoBeRt"), 100)
        self.assertIsNone(resolve_overlay_recipient(77, "rober"))
        self.assertIsNone(resolve_overlay_recipient(77, "200"))
        with patch.dict("os.environ", {"BLACKBIRD_OVERLAY_OPERATOR_IDS": "902235631952998410,123"}):
            self.assertEqual(overlay_operator_ids(), {902235631952998410, 123})
