import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from modules import finance
from modules.finance import (
    DailySnapshotModal,
    FinanceDailyPromptView,
    FinancePanelView,
    MovementModal,
    parse_money,
)


class FinanceFormatTests(unittest.TestCase):
    def test_money_parser_accepts_common_thousands_separators(self) -> None:
        for raw in ("1250000", "1 250 000", "1.250.000", "1,250,000"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_money(raw, allow_zero=False), 1_250_000)

    def test_money_parser_rejects_decimal_like_or_negative_values(self) -> None:
        for raw in ("1.5", "-500", "12,50", "сто тысяч"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_money(raw, allow_zero=False)


class FinanceComponentTests(unittest.IsolatedAsyncioTestCase):
    async def test_component_shapes_match_discord_contract(self) -> None:
        daily_view = FinanceDailyPromptView()
        panel = FinancePanelView()
        modal = MovementModal("deposit")
        self.assertEqual(daily_view.children[0].custom_id, "finance_daily_enter")
        self.assertEqual([item.label for item in panel.children], ["Снял", "Положил", "Межотчёт"])
        self.assertEqual(len(modal.children), 3)
        self.assertRegex(modal.captcha_value, r"^[0-9]{3}$")

    async def test_daily_prompt_opens_modal_without_database_round_trip(self) -> None:
        view = FinanceDailyPromptView()
        discord = __import__("discord")
        embed = discord.Embed()
        embed.set_footer(text="finance-prompt:42")
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            channel_id=finance.FINANCE_DAILY_CHANNEL_ID,
            message=SimpleNamespace(embeds=[embed]),
            response=SimpleNamespace(
                send_message=AsyncMock(),
                send_modal=AsyncMock(),
            ),
        )
        with patch.object(
            finance.storage,
            "finance_get_daily_prompt_by_message",
        ) as database_lookup:
            await view.children[0].callback(interaction)
        database_lookup.assert_not_called()
        interaction.response.send_modal.assert_awaited_once()
        opened = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(opened, DailySnapshotModal)
        self.assertEqual(opened.prompt_id, 42)

    async def test_filled_daily_prompt_is_not_republished_after_channel_migration(self) -> None:
        current = datetime(2026, 7, 15, 19, 0, tzinfo=ZoneInfo("Europe/Riga"))
        channel = SimpleNamespace(guild=SimpleNamespace(id=77))
        filled = {"id": 1, "report_date": "2026-07-15", "status": "filled"}
        create_prompt = Mock()
        with (
            patch.object(finance, "now_local", return_value=current),
            patch.object(finance, "_get_channel", AsyncMock(return_value=channel)),
            patch.object(finance.storage, "finance_recent_daily_prompts", return_value=[filled]),
            patch.object(finance.storage, "finance_get_or_create_daily_prompt", create_prompt),
        ):
            self.assertTrue(await finance.ensure_today_prompt(Mock()))
        create_prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
