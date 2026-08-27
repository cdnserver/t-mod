from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import storage

from modules.tvrs_directory import (
    directory_state_for,
    ensure_directory_message,
    update_directory_state,
)


class _FakeMessage:
    def __init__(
        self,
        message_id: int,
        content: str = "",
        *,
        author_id: int = 999,
        author_bot: bool = True,
    ) -> None:
        self.id = message_id
        self.content = content
        self.author = SimpleNamespace(id=author_id, bot=author_bot)
        self.jump_url = f"https://discord.com/channels/1/2/{message_id}"
        self.edit = AsyncMock(side_effect=self._edit)

    async def _edit(self, **kwargs):
        if "content" in kwargs and kwargs["content"] is not None:
            self.content = kwargs["content"]
        return self


class _FakeChannel:
    def __init__(self) -> None:
        self.messages: dict[int, _FakeMessage] = {}
        self.send = AsyncMock(side_effect=self._send)
        self.fetch_message = AsyncMock(side_effect=self._fetch_message)

    def add(self, message: _FakeMessage) -> _FakeMessage:
        self.messages[message.id] = message
        return message

    async def _send(self, **kwargs):
        message_id = 500 + len(self.messages)
        message = _FakeMessage(message_id, content=str(kwargs.get("content") or ""))
        self.messages[message_id] = message
        return message

    async def _fetch_message(self, message_id: int):
        return self.messages[message_id]

    async def history(self, limit: int = 80):
        for message in list(self.messages.values())[-limit:][::-1]:
            yield message


class TVRSDirectoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.old_activity_file = storage.LEGACY_ACTIVITY_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "tvrs-directory-test.db"
        storage.LEGACY_ACTIVITY_FILE = storage.DATA_DIR / "activity.json"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        storage.LEGACY_ACTIVITY_FILE = self.old_activity_file
        self.temp_dir.cleanup()

    def _make_guild(self):
        senator_role = SimpleNamespace(id=1500563715622174881)
        members = [
            SimpleNamespace(
                id=10,
                display_name="Альфа",
                mention="<@10>",
                roles=[senator_role],
                bot=False,
            ),
            SimpleNamespace(
                id=20,
                display_name="Бета",
                mention="<@20>",
                roles=[senator_role],
                bot=False,
            ),
            SimpleNamespace(
                id=30,
                display_name="Гамма",
                mention="<@30>",
                roles=[],
                bot=False,
            ),
        ]
        member_map = {member.id: member for member in members}
        return SimpleNamespace(
            id=77,
            members=members,
            get_member=lambda user_id: member_map.get(int(user_id)),
        )

    async def test_directory_message_is_created_once_and_reused(self) -> None:
        guild = self._make_guild()
        channel = _FakeChannel()
        bot = SimpleNamespace(user=SimpleNamespace(id=999))

        storage.tvrs_set_directory_state(
            guild.id,
            {
                "initialized": True,
                "responsibles": {
                    "technical_support": 30,
                    "bill_moderation": None,
                    "ovr_communications": None,
                    "bureau_secretariat": None,
                },
                "chairs": {
                    "chair_1": 10,
                    "chair_2": 20,
                    "chair_3": None,
                },
            },
        )

        with patch("modules.tvrs_directory.get_directory_channel", AsyncMock(return_value=channel)):
            first = await ensure_directory_message(bot, guild)
            second = await ensure_directory_message(bot, guild)

        self.assertIsNotNone(first)
        self.assertIs(first, second)
        self.assertEqual(channel.send.await_count, 1)
        self.assertEqual(channel.fetch_message.await_count, 1)
        self.assertIn("# 🏛️ Сенат сообщества Товарищества", first.content)
        self.assertIn("**I.** <@10> — **Первый постоянный председатель Сената**", first.content)
        self.assertIn("**II.** <@20> — **Второй председатель Сената**", first.content)
        self.assertIn("> <@10>", first.content)
        self.assertIn("Техническая поддержка", first.content)

    async def test_manual_post_is_imported_into_a_new_bot_owned_message(self) -> None:
        guild = self._make_guild()
        channel = _FakeChannel()
        manual = channel.add(
            _FakeMessage(
                111,
                (
                    "# Ответственные лица экосистемы T-Mod\n\n"
                    "### 🛠️ Техническая поддержка\n\n"
                    "**Ответственный:** <@30>\n\n---\n\n"
                    "# Ответственные лица сообщества Товарищества\n\n"
                    "### 📜 Модерация законопроектов\n\n"
                    "**Ответственный:** <@20>\n\n"
                    "### 🛰️ Коммуникации ОВР\n\n"
                    "**Ответственный:** <@10>\n\n"
                    "### 🗂️ Секретариат SGL Bureau\n\n"
                    "**Ответственный:** <@30>\n\n---\n\n"
                    "# 🏛️ Сенат сообщества Товарищества\n\n"
                    "### Совет председателей\n\n"
                    "**I.** <@10> — **Первый постоянный председатель Сената**\n"
                    "**II.** <@20> — **Второй председатель Сената**\n"
                    "**III.** <@30> — **Третий председатель Сената**"
                ),
                author_id=123,
                author_bot=False,
            )
        )
        bot = SimpleNamespace(user=SimpleNamespace(id=999))

        with patch("modules.tvrs_directory.get_directory_channel", AsyncMock(return_value=channel)):
            created = await ensure_directory_message(bot, guild)

        self.assertIsNotNone(created)
        self.assertNotEqual(created.id, manual.id)
        self.assertEqual(manual.edit.await_count, 0)
        self.assertEqual(channel.send.await_count, 1)
        self.assertIn("**III.** <@30> — **Третий председатель Сената**", created.content)
        state = directory_state_for(guild.id)
        self.assertEqual(state["responsibles"]["technical_support"], 30)
        self.assertEqual(state["responsibles"]["bill_moderation"], 20)
        self.assertEqual(state["chairs"]["chair_1"], 10)
        self.assertEqual(state["imported_from_message_id"], manual.id)

    async def test_foreign_message_id_is_never_edited(self) -> None:
        guild = self._make_guild()
        channel = _FakeChannel()
        foreign = channel.add(_FakeMessage(111, "ручной пост", author_id=123, author_bot=False))
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        storage.tvrs_set_directory_state(guild.id, {"initialized": True})
        storage.tvrs_set_directory_message_id(guild.id, foreign.id)

        with patch("modules.tvrs_directory.get_directory_channel", AsyncMock(return_value=channel)):
            created = await ensure_directory_message(bot, guild)

        self.assertIsNotNone(created)
        self.assertNotEqual(created.id, foreign.id)
        self.assertEqual(foreign.edit.await_count, 0)
        self.assertEqual(channel.send.await_count, 1)

    def test_directory_state_update_persists(self) -> None:
        updated = update_directory_state(
            77,
            slot="technical_support",
            member_id=30,
            actor_id=10,
        )
        loaded = directory_state_for(77)
        self.assertEqual(updated["responsibles"]["technical_support"], 30)
        self.assertEqual(loaded["responsibles"]["technical_support"], 30)
        self.assertEqual(loaded["updated_by"], 10)
        self.assertTrue(loaded["initialized"])
        self.assertGreaterEqual(int(loaded["revision"]), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
