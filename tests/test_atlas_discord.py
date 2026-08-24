from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules import atlas_discord


class _Tree:
    def command(self, **_kwargs):
        return lambda function: function


class _Bot:
    def __init__(self) -> None:
        self.tree = _Tree()
        self.listeners = {}

    def listen(self, name: str):
        def decorator(function):
            self.listeners[name] = function
            return function

        return decorator


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class AtlasDiscordTests(unittest.IsolatedAsyncioTestCase):
    def test_channel_call_parser_accepts_atlas_2(self) -> None:
        matched = atlas_discord._ATLAS_CALL_RE.match("Атлас 2, скажи прямо")
        self.assertIsNotNone(matched)
        assert matched is not None
        self.assertTrue(matched.group("variant"))
        self.assertEqual(matched.group("question"), "скажи прямо")

    async def test_new_conversation_uses_created_thread_without_refetching_starter(self) -> None:
        bot = _Bot()
        atlas_discord.setup_atlas_discord(bot)
        listener = bot.listeners["on_message"]

        parent = SimpleNamespace(id=atlas_discord.ATLAS_CHANNEL_ID)
        sent = AsyncMock()
        thread = SimpleNamespace(id=991, typing=lambda: _Typing(), send=sent)
        author = SimpleNamespace(bot=False, id=42, name="Tester", display_name="Tester")
        message = SimpleNamespace(
            id=777,
            guild=SimpleNamespace(id=15),
            author=author,
            channel=parent,
            content="Атлас, как оформить задержание?",
            create_thread=AsyncMock(return_value=thread),
            reply=AsyncMock(),
        )
        mapping = {
            "organization_id": 3,
            "owner_user_id": 42,
            "atlas_thread_id": 12,
        }
        dashboard = {
            "organization": {"id": 3},
            "membership": {"profile": {"server_code": "phoenix-15", "faction_code": "lspd"}},
        }
        answer = {
            "answer": "Готовый ответ",
            "citations": [],
            "model": "atlas-tvr-a",
            "latency_ms": 20,
        }

        with (
            patch.object(atlas_discord.global_ban_storage, "is_globally_banned", return_value=False),
            patch.object(atlas_discord.auth_storage, "web_section_grants", return_value=[{"section": "atlas_ai"}]),
            patch.object(atlas_discord.profile_storage, "list_profile_characters", return_value=[{"id": 1}]),
            patch.object(atlas_discord.atlas_storage, "atlas_dashboard", return_value=dashboard),
            patch.object(atlas_discord.atlas_storage, "atlas_create_thread", return_value=12),
            patch.object(atlas_discord.atlas_storage, "atlas_bind_discord_thread", return_value=mapping),
            patch.object(atlas_discord.atlas_storage, "atlas_thread_messages", return_value={"messages": []}),
            patch.object(atlas_discord.atlas_storage, "atlas_recent_chat_memory", return_value=[]),
            patch.object(atlas_discord.atlas_storage, "atlas_add_message"),
            patch.object(atlas_discord.atlas_storage, "atlas_record_event"),
            patch.object(atlas_discord, "atlas_answer", AsyncMock(return_value=answer)),
        ):
            await listener(message)

        message.create_thread.assert_awaited_once()
        sent.assert_awaited_once()
        self.assertEqual(sent.await_args.kwargs["embed"].description, "Готовый ответ")


if __name__ == "__main__":
    unittest.main()
