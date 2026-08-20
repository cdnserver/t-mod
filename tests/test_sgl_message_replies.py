import gc
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from aiohttp.test_utils import TestClient, TestServer

from modules.consensus_web import create_consensus_web_app
from modules.consensus_web_auth import ConsensusWebPrincipal
from modules.sgl_messages import send_web_case_message
from persistence import core, schema, sgl_repository


TARGET_ID = 987_654_321_012_345_678
SENT_ID = 987_654_321_012_345_679
ATTACHMENT_ID = 987_654_321_012_345_680


class _FakeTextChannel:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent_kwargs: dict[str, object] | None = None

    async def send(self, **kwargs: object) -> SimpleNamespace:
        self.sent_kwargs = kwargs
        if self.fail:
            raise _ReplyDeliveryFailure()
        return SimpleNamespace(
            id=SENT_ID,
            attachments=[
                SimpleNamespace(
                    id=ATTACHMENT_ID,
                    filename="proof.txt",
                    url="https://cdn.example/proof.txt",
                    content_type="text/plain",
                    size=12,
                )
            ],
            created_at=datetime.now(timezone.utc),
        )


class _ReplyDeliveryFailure(discord.HTTPException):
    """A minimal HTTPException subtype suitable for a send-failure test."""

    def __init__(self) -> None:
        pass


class SGLMessageReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.previous_data_dir = core.DATA_DIR
        self.previous_database_file = core.DATABASE_FILE
        core.DATA_DIR = Path(self.temp_dir.name)
        core.DATABASE_FILE = core.DATA_DIR / "sgl-message-replies.db"
        schema.init_db()

        reserved = sgl_repository.reserve_sgl_case(
            guild_id=77,
            client_id=101,
            client_display="Client",
            lead_lawyer_id=202,
            lead_lawyer_display="Lawyer",
            secretary_id=None,
            secretary_display=None,
            created_by_id=202,
            created_by_display="Lawyer",
        )
        self.case = sgl_repository.attach_sgl_case_channel(reserved.id, 404)
        assert self.case is not None

        other_reserved = sgl_repository.reserve_sgl_case(
            guild_id=77,
            client_id=111,
            client_display="Other client",
            lead_lawyer_id=202,
            lead_lawyer_display="Lawyer",
            secretary_id=None,
            secretary_display=None,
            created_by_id=202,
            created_by_display="Lawyer",
        )
        self.other_case = sgl_repository.attach_sgl_case_channel(other_reserved.id, 405)
        assert self.other_case is not None

    def tearDown(self) -> None:
        core.DATA_DIR = self.previous_data_dir
        core.DATABASE_FILE = self.previous_database_file
        gc.collect()
        self.temp_dir.cleanup()

    @staticmethod
    def _principal() -> ConsensusWebPrincipal:
        member = SimpleNamespace(
            id=202,
            display_name="Lawyer",
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[],
        )
        return ConsensusWebPrincipal(
            user_id=202,
            guild_id=77,
            display_name="Lawyer",
            csrf_token="csrf-reply-token",
            member=member,  # type: ignore[arg-type]
        )

    def _record_message(self, *, case=None, message_id: int = TARGET_ID) -> dict[str, object]:
        return sgl_repository.record_sgl_case_message(
            case=case or self.case,
            origin="discord",
            discord_message_id=message_id,
            author_id=101,
            author_display="Client",
            author_avatar_url=None,
            author_is_bot=False,
            content="Исходное сообщение клиента",
            attachments=[
                {
                    "id": ATTACHMENT_ID,
                    "filename": "screenshot.png",
                    "url": "https://cdn.example/screenshot.png",
                    "size": 12,
                }
            ],
        )

    async def test_delivery_uses_safe_discord_reference_and_serializes_snowflakes(self) -> None:
        target = self._record_message()
        self.assertEqual(target["discord_message_id"], str(TARGET_ID))
        self.assertEqual(target["attachments"][0]["id"], str(ATTACHMENT_ID))  # type: ignore[index]

        channel = _FakeTextChannel()
        bot = SimpleNamespace(get_channel=lambda channel_id: channel if channel_id == 404 else None)
        with patch("modules.sgl_messages.discord.TextChannel", _FakeTextChannel):
            delivered = await send_web_case_message(
                bot=bot,
                case=self.case,
                author_id=202,
                author_display="Lawyer",
                content="Принято, проверяю документы.",
                reply_to_discord_message_id=TARGET_ID,
            )

        self.assertEqual(delivered["discord_message_id"], str(SENT_ID))
        self.assertEqual(delivered["reply_to_discord_message_id"], str(TARGET_ID))
        self.assertEqual(delivered["attachments"][0]["id"], str(ATTACHMENT_ID))  # type: ignore[index]
        assert channel.sent_kwargs is not None
        reference = channel.sent_kwargs["reference"]
        self.assertIsInstance(reference, discord.MessageReference)
        self.assertEqual(reference.message_id, TARGET_ID)
        self.assertEqual(reference.channel_id, self.case.channel_id)
        self.assertEqual(reference.guild_id, self.case.guild_id)
        self.assertTrue(reference.fail_if_not_exists)
        self.assertFalse(channel.sent_kwargs["mention_author"])
        self.assertEqual(channel.sent_kwargs["allowed_mentions"].to_dict(), {"parse": []})  # type: ignore[union-attr]

    async def test_invalid_cross_case_and_deleted_targets_are_rejected_before_send(self) -> None:
        bot = SimpleNamespace(get_channel=lambda _channel_id: None)
        with self.assertRaisesRegex(ValueError, "sgl_case_reply_not_found"):
            await send_web_case_message(
                bot=bot,
                case=self.case,
                author_id=202,
                author_display="Lawyer",
                content="Ответ",
                reply_to_discord_message_id=TARGET_ID,
            )

        self._record_message(case=self.other_case)
        with self.assertRaisesRegex(ValueError, "sgl_case_reply_cross_case"):
            await send_web_case_message(
                bot=bot,
                case=self.case,
                author_id=202,
                author_display="Lawyer",
                content="Ответ",
                reply_to_discord_message_id=TARGET_ID,
            )

        deleted_id = TARGET_ID + 10
        self._record_message(message_id=deleted_id)
        sgl_repository.mark_sgl_case_message_deleted(
            guild_id=77,
            discord_message_id=deleted_id,
        )
        with self.assertRaisesRegex(ValueError, "sgl_case_reply_deleted"):
            await send_web_case_message(
                bot=bot,
                case=self.case,
                author_id=202,
                author_display="Lawyer",
                content="Ответ",
                reply_to_discord_message_id=deleted_id,
            )

    async def test_failed_discord_reply_is_never_persisted_as_an_ordinary_message(self) -> None:
        self._record_message()
        channel = _FakeTextChannel(fail=True)
        bot = SimpleNamespace(get_channel=lambda channel_id: channel if channel_id == 404 else None)
        with patch("modules.sgl_messages.discord.TextChannel", _FakeTextChannel), self.assertRaisesRegex(
            ValueError, "sgl_case_reply_unavailable"
        ):
            await send_web_case_message(
                bot=bot,
                case=self.case,
                author_id=202,
                author_display="Lawyer",
                content="Ответ",
                reply_to_discord_message_id=TARGET_ID,
            )

        messages = sgl_repository.list_sgl_case_messages(self.case.id)
        self.assertEqual([item["discord_message_id"] for item in messages], [str(TARGET_ID)])

    async def test_post_reports_reply_target_errors_and_member_ids_without_precision_loss(self) -> None:
        cross_case = self._record_message(case=self.other_case, message_id=TARGET_ID + 1)
        deleted_id = TARGET_ID + 20
        self._record_message(message_id=deleted_id)
        sgl_repository.mark_sgl_case_message_deleted(
            guild_id=77,
            discord_message_id=deleted_id,
        )
        guild = SimpleNamespace(
            members=[
                SimpleNamespace(
                    id=TARGET_ID,
                    display_name="Snowflake lawyer",
                    name="snowflake-lawyer",
                )
            ]
        )
        bot = SimpleNamespace(
            get_guild=lambda identifier: guild if identifier == 77 else None,
            get_channel=lambda _channel_id: None,
        )
        app = create_consensus_web_app(bot, guild_id=77)  # type: ignore[arg-type]
        headers = {
            "Host": "sgl.tvr.lat",
            "X-CSRF-Token": "csrf-reply-token",
        }
        async with TestClient(TestServer(app)) as client:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=self._principal()),
            ):
                invalid = await client.post(
                    f"/api/sgl/cases/{self.case.case_number}/messages",
                    headers=headers,
                    json={"content": "Ответ", "reply_to": "invalid"},
                )
                missing = await client.post(
                    f"/api/sgl/cases/{self.case.case_number}/messages",
                    headers=headers,
                    json={"content": "Ответ", "reply_to": str(TARGET_ID)},
                )
                cross = await client.post(
                    f"/api/sgl/cases/{self.case.case_number}/messages",
                    headers=headers,
                    json={"content": "Ответ", "reply_to": cross_case["discord_message_id"]},
                )
                deleted = await client.post(
                    f"/api/sgl/cases/{self.case.case_number}/messages",
                    headers=headers,
                    json={"content": "Ответ", "reply_to": str(deleted_id)},
                )
                members = await client.get("/api/sgl/members?q=snowflake", headers=headers)
                invalid_payload = await invalid.json()
                missing_payload = await missing.json()
                cross_payload = await cross.json()
                deleted_payload = await deleted.json()
                members_payload = await members.json()

        self.assertEqual((invalid.status, invalid_payload["error"]), (400, "sgl_case_reply_invalid"))
        self.assertEqual((missing.status, missing_payload["error"]), (404, "sgl_case_reply_not_found"))
        self.assertEqual((cross.status, cross_payload["error"]), (409, "sgl_case_reply_cross_case"))
        self.assertEqual((deleted.status, deleted_payload["error"]), (409, "sgl_case_reply_deleted"))
        self.assertEqual(members.status, 200)
        self.assertEqual(members_payload["members"][0]["id"], str(TARGET_ID))

    async def test_post_maps_ordinary_discord_delivery_failure_to_502(self) -> None:
        channel = _FakeTextChannel(fail=True)
        guild = SimpleNamespace(members=[])
        bot = SimpleNamespace(
            get_guild=lambda identifier: guild if identifier == 77 else None,
            get_channel=lambda channel_id: channel if channel_id == 404 else None,
        )
        app = create_consensus_web_app(bot, guild_id=77)  # type: ignore[arg-type]
        headers = {
            "Host": "sgl.tvr.lat",
            "X-CSRF-Token": "csrf-reply-token",
        }
        with patch("modules.sgl_messages.discord.TextChannel", _FakeTextChannel), patch(
            "modules.consensus_web.resolve_principal",
            AsyncMock(return_value=self._principal()),
        ):
            async with TestClient(TestServer(app)) as client:
                response = await client.post(
                    f"/api/sgl/cases/{self.case.case_number}/messages",
                    headers=headers,
                    json={"content": "Обычное сообщение"},
                )
                payload = await response.json()

        self.assertEqual(response.status, 502)
        self.assertEqual(payload["error"], "sgl_case_message_delivery_failed")
        self.assertEqual(sgl_repository.list_sgl_case_messages(self.case.id), [])


if __name__ == "__main__":
    unittest.main()
