import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from modules.discord_interactions import (
    interaction_error_cause,
    is_expired_interaction_error,
    safe_interaction_error_message,
)


class DiscordInteractionSafetyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def unknown_interaction() -> discord.NotFound:
        return discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found", headers={}),
            {"message": "Unknown interaction", "code": 10062},
        )

    async def test_wrapped_unknown_interaction_is_detected(self) -> None:
        original = self.unknown_interaction()
        wrapper = RuntimeError("command failed")
        wrapper.__cause__ = original
        self.assertTrue(is_expired_interaction_error(wrapper))
        self.assertIs(interaction_error_cause(wrapper), original)

    async def test_safe_error_message_absorbs_expired_response(self) -> None:
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                is_done=lambda: False,
                send_message=AsyncMock(side_effect=self.unknown_interaction()),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        delivered = await safe_interaction_error_message(interaction, "Повторите")
        self.assertFalse(delivered)
        interaction.followup.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
