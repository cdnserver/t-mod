import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import storage
from modules.admin_broadcast import (
    ADMIN_BROADCAST_TOPIC,
    broadcast_message_embed,
    deliver_admin_broadcast,
)
from modules.delivery_outbox import OutboxMessage
from modules.tvrs_config import TVRS_SENATOR_ROLE_ID


class AdminBroadcastTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "broadcast-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def _queue(self) -> tuple[dict, dict]:
        draft = storage.create_broadcast_draft(
            guild_id=77,
            author_id=5,
            author_display="Администратор",
            kind="global",
            title="Общее собрание",
            body="Пожалуйста, ознакомьтесь с объявлением.",
        )
        broadcast, created = storage.activate_broadcast(
            draft["id"],
            author_id=5,
            recipients=[(10, "Сенатор")],
            delivery_topic=ADMIN_BROADCAST_TOPIC,
        )
        self.assertTrue(created)
        recipient = storage.get_broadcast_recipient(broadcast["id"], 10)
        assert recipient is not None
        return broadcast, recipient

    def test_activation_is_idempotent_and_report_is_durable(self) -> None:
        broadcast, _ = self._queue()
        repeated, created = storage.activate_broadcast(
            broadcast["id"],
            author_id=5,
            recipients=[(10, "Сенатор")],
            delivery_topic=ADMIN_BROADCAST_TOPIC,
        )

        self.assertFalse(created)
        self.assertEqual(repeated["id"], broadcast["id"])
        report = storage.broadcast_report(broadcast_id=broadcast["id"])
        assert report is not None
        self.assertEqual(report["recipient_count"], 1)
        self.assertEqual(report["counts"]["queued"], 1)

    async def test_delivery_marks_recipient_and_reuses_receipt(self) -> None:
        broadcast, recipient = self._queue()
        sent = SimpleNamespace(id=901)
        member = SimpleNamespace(
            id=10,
            roles=[SimpleNamespace(id=TVRS_SENATOR_ROLE_ID)],
            send=AsyncMock(return_value=sent),
        )
        guild = SimpleNamespace(id=77, get_member=lambda _: member)
        bot = SimpleNamespace(get_guild=lambda _: guild)
        payload = {
            "broadcast_id": broadcast["id"],
            "guild_id": 77,
            "user_id": 10,
            "kind": "global",
            "title": broadcast["title"],
            "body": broadcast["body"],
            "author_display": broadcast["author_display"],
        }
        message = OutboxMessage(
            id=int(recipient["outbox_id"]),
            topic=ADMIN_BROADCAST_TOPIC,
            dedupe_key="test",
            payload=payload,
            attempts=1,
            max_attempts=12,
            lease_token="lease",
        )

        first = await deliver_admin_broadcast(message, bot)
        second = await deliver_admin_broadcast(message, bot)

        self.assertEqual(first.message_id, 901)
        self.assertEqual(second.message_id, 901)
        member.send.assert_awaited_once()
        report = storage.broadcast_report(broadcast_id=broadcast["id"])
        assert report is not None
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["counts"]["delivered"], 1)

    def test_message_contains_no_mass_mentions(self) -> None:
        embed = broadcast_message_embed(
            {
                "kind": "consensus",
                "title": "Новый консенсус",
                "body": "Откройте панель и подтвердите участие.",
            }
        )
        self.assertIn("Новый консенсус", str(embed.title))
        self.assertLessEqual(len(embed), 6000)


if __name__ == "__main__":
    unittest.main()
