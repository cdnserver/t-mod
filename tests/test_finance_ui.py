import unittest

from modules.finance import FinanceDailyPromptView, FinancePanelView, MovementModal, parse_money


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


if __name__ == "__main__":
    unittest.main()
