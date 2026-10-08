import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from discord.ext import commands

import storage
from modules.overlay_remote_discord import overlay_operator_ids, resolve_overlay_recipient, setup_overlay_remote_discord
from persistence import atlas_repository, blackbird_communicate_repository as communicate
from persistence import profile_repository, web_auth_repository
from modules.consensus_web_auth import ConsensusWebPrincipal, TModAccountIdentity
from modules.reactor_web import register_reactor_web_routes


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

    def test_character_search_is_exact_and_available_by_default(self):
        character = profile_repository.add_profile_character(77, 100, "Robert Example", "263345")
        atlas_repository.atlas_set_overlay_character(77, 100, character.id, server_code="phoenix-15", faction_code="lspd")
        web_auth_repository.configure_web_credential(77, 100, "robert", "12345678")
        found = communicate.search_character(77, 200, "phoenix-15", "263345")
        self.assertEqual(found["user_id"], 100)
        self.assertEqual(found["nickname"], "Robert Example")
        self.assertIsNone(communicate.search_character(77, 200, "phoenix-14", "263345"))
        self.assertIsNone(communicate.search_character(77, 100, "phoenix-15", "263345"))
        communicate.set_discoverable(77, 100, False)
        self.assertIsNone(communicate.search_character(77, 200, "phoenix-15", "263345"))
        communicate.set_discoverable(77, 100, True)
        profile_repository.set_profile_character_visibility(77, 100, character.id, is_public=False)
        self.assertIsNone(communicate.search_character(77, 200, "phoenix-15", "263345"))

    def test_exact_account_search_does_not_require_atlas_character(self):
        web_auth_repository.configure_web_credential(77, 100, "Robert.Account", "12345678")
        self.assertEqual(communicate.search_account(77, 200, "robert.account")["user_id"], 100)
        self.assertEqual(communicate.search_account(77, 200, "100")["user_id"], 100)
        self.assertIsNone(communicate.search_account(77, 200, "robert"))
        self.assertIsNone(communicate.search_account(77, 100, "100"))
        communicate.set_discoverable(77, 100, False)
        self.assertEqual(communicate.search_account(77, 200, "robert.account")["user_id"], 100)
        self.assertEqual(communicate.search_account(77, 200, "100")["user_id"], 100)

    def test_conversation_title_does_not_reveal_private_character(self):
        web_auth_repository.configure_web_credential(77, 100, "robert", "12345678")
        character = profile_repository.add_profile_character(77, 100, "Private Character", "263345")
        communicate.send_message(77, 200, 100, "hello")
        self.assertEqual(communicate.list_conversations(77, 200)[1]["partner_name"], "Private Character")
        profile_repository.set_profile_character_visibility(77, 100, character.id, is_public=False)
        self.assertEqual(communicate.list_conversations(77, 200)[1]["partner_name"], "robert")

    def test_busy_conversation_does_not_hide_older_dialogues(self):
        for user_id in (100, 300):
            web_auth_repository.configure_web_credential(77, user_id, f"user{user_id}", "12345678")
        communicate.send_message(77, 200, 100, "Старый диалог")
        with sqlite3.connect(storage.DATABASE_FILE) as con:
            con.executemany(
                "INSERT INTO blackbird_communicate_messages(guild_id,sender_id,recipient_id,body,created_at) VALUES(77,200,300,?,?)",
                [(f"Сообщение {index}", "2026-01-01") for index in range(220)],
            )
        dialogues = communicate.list_conversations(77, 200)
        self.assertEqual([item["partner_id"] for item in dialogues], [0, 300, 100])

    def test_private_thread_and_rate_limit(self):
        with self.assertRaisesRegex(ValueError, "communicate_recipient_unavailable"):
            communicate.send_message(77, 200, 100, "hello")
        web_auth_repository.configure_web_credential(77, 100, "robert", "12345678")
        result = communicate.send_message(77, 200, 100, "hello")
        self.assertEqual(communicate.conversation(77, 100, 200)[0]["id"], result["id"])
        self.assertEqual(communicate.conversation(77, 300, 200), [])
        self.assertEqual(communicate.list_conversations(77, 200)[1]["partner_id"], 100)
        for _ in range(11):
            communicate.send_message(77, 200, 100, "another")
        with self.assertRaisesRegex(ValueError, "communicate_rate_limited"):
            communicate.send_message(77, 200, 100, "too many")
        communicate.set_discoverable(77, 100, False)
        self.assertEqual(communicate.send_message(77, 300, 100, "still deliverable")["recipient_id"], 100)

    def test_history_cursor_covers_entire_thread_without_leaking_other_dialogues(self):
        with sqlite3.connect(storage.DATABASE_FILE) as con:
            con.executemany(
                "INSERT INTO blackbird_communicate_messages(guild_id,sender_id,recipient_id,body,created_at) VALUES(77,100,200,?,'2026-01-01')",
                [(f"Сообщение {index}",) for index in range(185)],
            )
            con.execute("INSERT INTO blackbird_communicate_messages(guild_id,sender_id,recipient_id,body,created_at) VALUES(77,100,300,'Чужая беседа','2026-01-01')")
            con.execute("INSERT INTO blackbird_communicate_messages(guild_id,sender_id,recipient_id,body,created_at) VALUES(88,100,200,'Другой сервер','2026-01-01')")
        latest = communicate.conversation(77, 200, 100)
        previous = communicate.conversation(77, 200, 100, before_id=latest[0]["id"])
        oldest = communicate.conversation(77, 200, 100, before_id=previous[0]["id"])
        self.assertEqual([len(latest), len(previous), len(oldest)], [80, 80, 25])
        self.assertEqual([row["body"] for row in oldest + previous + latest], [f"Сообщение {index}" for index in range(185)])
        self.assertEqual(communicate.conversation(77, 200, 100, before_id=oldest[0]["id"]), [])
        for cursor in (-1, 9223372036854775808):
            with self.assertRaisesRegex(ValueError, "communicate_cursor_invalid"):
                communicate.conversation(77, 200, 100, before_id=cursor)
            with self.assertRaisesRegex(ValueError, "communicate_cursor_invalid"):
                communicate.conversation(77, 200, 100, after_id=cursor)
        with self.assertRaisesRegex(ValueError, "communicate_cursor_invalid"):
            communicate.conversation(77, 200, 100, before_id=180, after_id=10)

        # Reconnecting after >80 new messages must start with the first missed
        # message, not jump to the newest page and silently leave a gap.
        cursor = oldest[0]["id"]
        caught_up = []
        while page := communicate.conversation(77, 200, 100, after_id=cursor):
            self.assertGreater(page[0]["id"], cursor)
            caught_up.extend(page)
            cursor = page[-1]["id"]
        self.assertEqual([row["body"] for row in caught_up], [f"Сообщение {index}" for index in range(1, 185)])
        self.assertEqual(communicate.conversation(77, 300, 200, after_id=oldest[0]["id"]), [])

    def test_out_of_range_recipients_fail_validation_before_database_queries(self):
        for partner_id in (-1, 9223372036854775808, 10**100):
            with self.assertRaisesRegex(ValueError, "communicate_recipient_invalid"):
                communicate.conversation(77, 100, partner_id)
            with self.assertRaisesRegex(ValueError, "communicate_recipient_invalid"):
                communicate.send_message(77, 100, partner_id, "Не должно попасть в БД")
            with self.assertRaisesRegex(ValueError, "communicate_recipient_invalid"):
                communicate.send_attachment_message(77, 100, partner_id, "file.txt", b"content")

    def test_verified_service_welcome_is_not_a_sendable_identity(self):
        service = communicate.list_conversations(77, 200)[0]
        self.assertEqual(service["partner_id"], 0)
        self.assertTrue(service["verified"])
        self.assertTrue(service["system"])
        self.assertIn("Добро пожаловать", service["last_message"])
        self.assertEqual(communicate.conversation(77, 200, 0)[0]["body"], service["last_message"])
        with self.assertRaisesRegex(ValueError, "communicate_self_invalid"):
            communicate.send_message(77, 200, 0, "spoof")

    def test_retried_text_and_file_deliver_once_and_reject_changed_payload(self):
        web_auth_repository.configure_web_credential(77, 100, "robert", "12345678")
        nonce = "a" * 32
        sent = communicate.send_message(77, 200, 100, "Привет", client_nonce=nonce)
        for _ in range(15):
            self.assertEqual(communicate.send_message(77, 200, 100, "Привет", client_nonce=nonce)["id"], sent["id"])
        with self.assertRaisesRegex(ValueError, "communicate_request_conflict"):
            communicate.send_message(77, 200, 100, "Другой текст", client_nonce=nonce)
        file_nonce = "b" * 32
        uploaded = communicate.send_attachment_message(77, 200, 100, "test.txt", b"content", "Файл", client_nonce=file_nonce)
        retried = communicate.send_attachment_message(77, 200, 100, "test.txt", b"content", "Файл", client_nonce=file_nonce)
        self.assertEqual(retried, uploaded)
        with self.assertRaisesRegex(ValueError, "communicate_request_conflict"):
            communicate.send_attachment_message(77, 200, 100, "test.txt", b"changed", "Файл", client_nonce=file_nonce)
        self.assertEqual(len(communicate.conversation(77, 100, 200)), 2)

    def test_legacy_message_table_migrates_without_losing_history(self):
        storage.DATABASE_FILE = storage.DATA_DIR / "legacy.db"
        with sqlite3.connect(storage.DATABASE_FILE) as con:
            con.execute("""CREATE TABLE blackbird_communicate_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL, recipient_id INTEGER NOT NULL,
                body TEXT NOT NULL, created_at TEXT NOT NULL, CHECK(sender_id != recipient_id))""")
            con.execute("INSERT INTO blackbird_communicate_messages(guild_id,sender_id,recipient_id,body,created_at) VALUES(77,200,100,'Старое сообщение','2026-01-01')")
        storage.init_db()
        web_auth_repository.configure_web_credential(77, 100, "robert", "12345678")
        sent = communicate.send_message(77, 200, 100, "Новое сообщение", client_nonce="c" * 32)
        self.assertEqual(communicate.send_message(77, 200, 100, "Новое сообщение", client_nonce="c" * 32)["id"], sent["id"])
        self.assertEqual([item["body"] for item in communicate.conversation(77, 100, 200)], ["Старое сообщение", "Новое сообщение"])

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


class BlackbirdCommunicateHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "communicate-http-test.db"
        storage.init_db()

    def tearDown(self):
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    async def test_non_guild_accounts_can_attach_but_require_login_csrf_and_conversation_membership(self):
        for user_id in (100, 200, 300):
            web_auth_repository.configure_web_credential(77, user_id, f"user{user_id}", "12345678")

        async def authenticate(request):
            user_id = int(request.headers.get("X-Test-User") or 0)
            if user_id not in (100, 200, 300):
                return None, False
            member = TModAccountIdentity(user_id, "Аккаунт вне Сената")
            principal = ConsensusWebPrincipal(user_id, 77, member.display_name, "csrf-test", member)
            self.assertFalse(principal.guild_member)
            return principal, request.headers.get("X-Test-Legacy") == "1"

        app = web.Application()
        register_reactor_web_routes(
            app, SimpleNamespace(), guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "consensus",
            authenticate=authenticate,
        )
        image = b"\x89PNG\r\n\x1a\n" + b"picture-data"
        async with TestClient(TestServer(app)) as client:
            url = "/api/blackbird/communicate/attachments"
            base = {
                "Content-Type": "application/octet-stream",
                "X-Blackbird-Partner": "200",
                "X-Blackbird-Filename": "image.png",
                "X-Blackbird-Nonce": "e" * 32,
            }
            self.assertEqual((await client.post(url, data=image, headers=base)).status, 401)
            self.assertEqual((await client.post(url, data=image, headers={**base, "X-Test-User": "100", "X-Test-Legacy": "1", "X-CSRF-Token": "csrf-test"})).status, 401)
            self.assertEqual((await client.post(url, data=image, headers={**base, "X-Test-User": "100"})).status, 403)
            senate = await client.get("/api/reactor/home", headers={"X-Test-User": "100"})
            self.assertEqual(senate.status, 403)
            self.assertEqual((await senate.json())["error"], "zero_account_reactor_forbidden")
            uploaded = await client.post(
                url, data=image,
                headers={**base, "X-Test-User": "100", "X-CSRF-Token": "csrf-test"},
            )
            self.assertEqual(uploaded.status, 200)
            message = (await uploaded.json())["result"]
            self.assertEqual(message["attachment"]["mime_type"], "image/png")
            retry = await client.post(url, data=image, headers={**base, "X-Test-User": "100", "X-CSRF-Token": "csrf-test"})
            self.assertEqual((await retry.json())["result"]["attachment"]["id"], message["attachment"]["id"])
            attachment_url = f"{url}/{message['attachment']['id']}"
            self.assertEqual((await client.get(attachment_url)).status, 401)
            self.assertEqual((await client.get(attachment_url, headers={"X-Test-User": "300"})).status, 404)
            retrieved = await client.get(attachment_url, headers={"X-Test-User": "200"})
            self.assertEqual(retrieved.status, 200)
            self.assertEqual(retrieved.headers["Content-Type"], "image/png")
            self.assertEqual(await retrieved.read(), image)
            thread = await client.post(
                "/api/blackbird/communicate",
                headers={"X-Test-User": "200", "X-CSRF-Token": "csrf-test"},
                json={"action": "thread", "partner_id": "100"},
            )
            self.assertEqual((await thread.json())["result"][0]["attachment"]["id"], message["attachment"]["id"])
            # A database-only recovery point must contain the bytes too; no
            # separate file tree may be required to restore the conversation.
            recovered_db = storage.DATA_DIR / "recovered.db"
            with sqlite3.connect(storage.DATABASE_FILE) as source, sqlite3.connect(recovered_db) as target:
                source.backup(target)
            storage.DATABASE_FILE = recovered_db
            recovered = communicate.read_attachment(77, 200, message["attachment"]["id"])
            self.assertEqual(recovered[1], image)

    async def test_exact_discord_ids_survive_search_send_and_thread(self):
        sender = 902235631952998410
        recipient = 825331775857360906
        web_auth_repository.configure_web_credential(77, sender, "sender", "12345678")
        web_auth_repository.configure_web_credential(77, recipient, "recipient", "12345678")

        async def authenticate(request):
            value = request.headers.get("X-Test-User")
            if value not in {str(sender), str(recipient)}:
                return None, False
            user_id = int(value)
            member = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False))
            return ConsensusWebPrincipal(user_id, 77, "Member", "csrf-test", member), False

        app = web.Application()
        register_reactor_web_routes(app, SimpleNamespace(), guild_id=77,
                                    asset_dir=Path(__file__).resolve().parents[1] / "web" / "consensus",
                                    authenticate=authenticate)
        async with TestClient(TestServer(app)) as client:
            url = "/api/blackbird/communicate"
            self.assertEqual((await client.get(url)).status, 401)
            headers = {"X-Test-User": str(sender)}
            response = await client.get(url, headers=headers)
            self.assertEqual(response.status, 200)
            snapshot = await response.json()
            self.assertEqual(snapshot["viewer"]["id"], str(sender))
            self.assertEqual(snapshot["conversations"][0]["partner_id"], "0")
            self.assertEqual(snapshot["viewer"]["csrf_token"], "csrf-test")

            self.assertEqual((await client.post(url, headers=headers, json={"action": "send", "partner_id": str(recipient), "message": "Привет"})).status, 403)
            headers["X-CSRF-Token"] = snapshot["viewer"]["csrf_token"]
            found = await client.post(url, headers=headers, json={"action": "search", "account_query": "recipient"})
            self.assertEqual(found.status, 200)
            self.assertEqual((await found.json())["result"]["user_id"], str(recipient))
            recipient_headers = {"X-Test-User": str(recipient), "X-CSRF-Token": "csrf-test"}
            hidden = await client.post(url, headers=recipient_headers,
                                       json={"action": "discoverability", "discoverable": False})
            self.assertEqual(hidden.status, 200)
            self.assertFalse((await hidden.json())["result"]["discoverable"])
            hidden_found = await client.post(url, headers=headers,
                                             json={"action": "search", "account_query": str(recipient)})
            self.assertEqual((await hidden_found.json())["result"]["user_id"], str(recipient))
            send_body = {"action": "send", "partner_id": str(recipient), "message": "Привет", "client_nonce": "d" * 32}
            sent = await client.post(url, headers=headers, json=send_body)
            self.assertEqual(sent.status, 200)
            sent_item = (await sent.json())["result"]
            self.assertEqual(sent_item["sender_id"], str(sender))
            self.assertEqual(sent_item["recipient_id"], str(recipient))
            retried = await client.post(url, headers=headers, json=send_body)
            self.assertEqual((await retried.json())["result"]["id"], sent_item["id"])

            thread = await client.post(url, headers=recipient_headers, json={"action": "thread", "partner_id": str(sender)})
            self.assertEqual(thread.status, 200)
            self.assertEqual((await thread.json())["result"][0]["sender_id"], str(sender))
            inbox = await client.get(url, headers=recipient_headers)
            self.assertEqual((await inbox.json())["conversations"][1]["partner_id"], str(sender))
            notices = await client.get("/api/reactor/notifications?unread=1", headers=recipient_headers)
            self.assertEqual(notices.status, 200)
            notification_data = await notices.json()
            self.assertEqual(notification_data["viewer"]["id_exact"], str(recipient))
            message_notice = next(item for item in notification_data["items"] if item["kind"] == "communicate")
            self.assertEqual(message_notice["title"], "Новое сообщение в Blackbird")
            read = await client.post("/api/reactor/notifications/read", headers=recipient_headers,
                                     json={"ids": [message_notice["id"]]})
            self.assertEqual(read.status, 200)
            self.assertEqual((await read.json())["updated"], 1)
            with sqlite3.connect(storage.DATABASE_FILE) as con:
                con.executemany(
                    "INSERT INTO blackbird_communicate_messages(guild_id,sender_id,recipient_id,body,created_at) VALUES(77,?,?,?,'2026-01-01')",
                    [(sender, recipient, f"История {index}") for index in range(90)],
                )
            latest_response = await client.post(url, headers=recipient_headers, json={"action": "thread", "partner_id": str(sender)})
            latest_page = await latest_response.json()
            self.assertTrue(latest_page["has_more"])
            self.assertEqual(len(latest_page["result"]), 80)
            previous_response = await client.post(url, headers=recipient_headers, json={
                "action": "thread", "partner_id": str(sender), "before_id": str(latest_page["result"][0]["id"]),
            })
            previous_page = await previous_response.json()
            self.assertFalse(previous_page["has_more"])
            self.assertEqual(len(previous_page["result"]), 11)
            self.assertEqual(previous_page["result"][0]["body"], "Привет")
            forward_response = await client.post(url, headers=recipient_headers, json={
                "action": "thread", "partner_id": str(sender), "after_id": str(sent_item["id"]),
            })
            forward_page = await forward_response.json()
            self.assertTrue(forward_page["has_newer"])
            self.assertFalse(forward_page["has_more"])
            self.assertEqual(len(forward_page["result"]), 80)
            self.assertEqual(forward_page["result"][0]["body"], "История 0")
            self.assertEqual(forward_page["result"][-1]["body"], "История 79")
            tail_response = await client.post(url, headers=recipient_headers, json={
                "action": "thread", "partner_id": str(sender), "after_id": str(forward_page["result"][-1]["id"]),
            })
            tail_page = await tail_response.json()
            self.assertFalse(tail_page["has_newer"])
            self.assertEqual([row["body"] for row in tail_page["result"]], [f"История {index}" for index in range(80, 90)])
            bad_cursors = await client.post(url, headers=recipient_headers, json={
                "action": "thread", "partner_id": str(sender), "after_id": "10", "before_id": "100",
            })
            self.assertEqual(bad_cursors.status, 400)
            for action in ("send", "thread"):
                invalid_recipient = await client.post(url, headers=recipient_headers, json={
                    "action": action, "partner_id": "9999999999999999999999", "message": "Не отправлять",
                })
                self.assertEqual(invalid_recipient.status, 400)
                self.assertEqual((await invalid_recipient.json())["error"], "communicate_recipient_invalid")
            invalid_cursor = await client.post(url, headers=recipient_headers, json={"action": "thread", "partner_id": str(sender), "before_id": "-1"})
            self.assertEqual(invalid_cursor.status, 400)
