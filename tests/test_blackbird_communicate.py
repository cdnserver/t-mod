import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

import storage
from modules.overlay_remote_discord import overlay_operator_ids, resolve_overlay_recipient, setup_overlay_remote_discord
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
        self.assertEqual(communicate.list_conversations(77, 200)[1]["partner_id"], 100)
        for _ in range(11):
            communicate.send_message(77, 200, 100, "another")
        with self.assertRaisesRegex(ValueError, "communicate_rate_limited"):
            communicate.send_message(77, 200, 100, "too many")
        communicate.set_discoverable(77, 100, False)
        with self.assertRaisesRegex(ValueError, "communicate_recipient_unavailable"):
            communicate.send_message(77, 200, 100, "opted out")

    def test_verified_service_welcome_is_not_a_sendable_identity(self):
        service = communicate.list_conversations(77, 200)[0]
        self.assertEqual(service["partner_id"], 0)
        self.assertTrue(service["verified"])
        self.assertTrue(service["system"])
        self.assertIn("Добро пожаловать", service["last_message"])
        self.assertEqual(communicate.conversation(77, 200, 0)[0]["body"], service["last_message"])
        with self.assertRaisesRegex(ValueError, "communicate_self_invalid"):
            communicate.send_message(77, 200, 0, "spoof")

    def test_overlay_operator_and_exact_account_resolution(self):
        web_auth_repository.configure_web_credential(77, 100, "robert", "12345678")
        self.assertEqual(resolve_overlay_recipient(77, "100"), 100)
        self.assertEqual(resolve_overlay_recipient(77, "RoBeRt"), 100)
        self.assertIsNone(resolve_overlay_recipient(77, "rober"))
        self.assertIsNone(resolve_overlay_recipient(77, "200"))
        with patch.dict("os.environ", {"BLACKBIRD_OVERLAY_OPERATOR_IDS": "902235631952998410,123"}):
            self.assertEqual(overlay_operator_ids(), {902235631952998410, 123})

    def test_screenban_is_operator_only_and_cosmetic(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        setup_overlay_remote_discord(bot)
        command = bot.tree.get_command("screenban")
        self.assertIsNotNone(command)
        interaction = SimpleNamespace(
            guild_id=77,
            user=SimpleNamespace(id=902235631952998410),
            id=456,
            guild=None,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("modules.overlay_remote_discord.resolve_overlay_recipient", return_value=100), \
             patch("modules.overlay_remote_discord.reactor_repository.reactor_put_notification", return_value=42) as notify:
            asyncio.run(command.callback(interaction, "robert"))
        self.assertEqual(notify.call_args.kwargs["kind"], "screenban")
        self.assertEqual(notify.call_args.kwargs["user_id"], 100)
        self.assertIn("визуальный показ", interaction.followup.send.call_args.args[0].lower())

        interaction.user.id = 999
        with patch("modules.overlay_remote_discord.reactor_repository.reactor_put_notification") as notify:
            asyncio.run(command.callback(interaction, "robert"))
        notify.assert_not_called()
