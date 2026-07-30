import tempfile
import unittest
from pathlib import Path

import storage
from persistence import admin_dashboard_repository as dashboard


class AdminDashboardRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.old_activity_file = storage.LEGACY_ACTIVITY_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "admin-dashboard-test.db"
        storage.LEGACY_ACTIVITY_FILE = storage.DATA_DIR / "activity.json"
        storage.init_db()

        storage.finance_record_snapshot(
            guild_id=77,
            event_kind="interim",
            amount=1_000_000,
            actor_id=10,
            actor_display="Казначей",
            channel_id=20,
            message_id=30,
            log_channel_id=40,
            admin_user_id=50,
            report_date="2026-07-30",
        )
        storage.finance_record_movement(
            guild_id=77,
            event_kind="deposit",
            amount=125_000,
            reason="Доход от продажи тестовой партии",
            captcha_digest="test",
            game_code="ABCD",
            actor_id=11,
            actor_display="Оператор",
            channel_id=20,
            message_id=31,
            log_channel_id=40,
            admin_user_id=50,
        )
        recipe = storage.craft_create_recipe(
            guild_id=77,
            product_name="Промышленный сплав",
            treasury_cost_per_unit=1_000,
            duration_minutes_per_unit=10,
            max_batch_size=10,
            materials=[("Руда", 2)],
            created_by_id=12,
            created_by_display="Технолог",
        )
        self.plan = storage.craft_create_plan(
            guild_id=77,
            recipe_id=recipe["id"],
            channel_id=21,
            attempts_total=5,
            responsible_id=13,
            responsible_display="Крафтер",
            created_by_id=12,
            created_by_display="Технолог",
        )
        storage.remember_activity(
            guild_id=77,
            user_id=14,
            display_name="Участник",
            name="member",
            mention="<@14>",
            is_bot=False,
            event_type="message",
            event_text="Проверочное сообщение",
            channel_id=22,
            channel_name="сенат",
            category_id=23,
            category_name="Товарищество",
            message_id=32,
            force=True,
        )

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        storage.LEGACY_ACTIVITY_FILE = self.old_activity_file
        self.temp_dir.cleanup()

    def test_overview_counts_all_operational_sources(self) -> None:
        counts = dashboard.admin_dashboard_counts(77, days=30)
        self.assertGreaterEqual(counts["actions"], 4)
        self.assertEqual(counts["finance_events"], 2)
        self.assertEqual(counts["active_crafts"], 1)
        self.assertEqual(counts["discord_events"], 1)
        self.assertEqual(counts["discord_users"], 1)

    def test_filters_return_paged_audit_finance_craft_and_discord_rows(self) -> None:
        actions = dashboard.admin_audit_actions(
            77,
            module="craft",
            query="сплав",
            limit=1,
        )
        self.assertGreaterEqual(actions["total"], 1)
        self.assertEqual(len(actions["items"]), 1)
        self.assertIn("craft", actions["modules"])

        finance = dashboard.admin_finance_events(
            77,
            event_kind="deposit",
            code="abcd",
            query="продажи",
        )
        self.assertEqual(finance["total"], 1)
        self.assertEqual(finance["items"][0]["game_code"], "ABCD")

        craft = dashboard.admin_craft_events(77, query="сплав")
        self.assertGreaterEqual(craft["total"], 1)
        self.assertEqual(craft["items"][0]["plan_id"], self.plan["id"])
        self.assertIsInstance(craft["items"][0]["details"], dict)

        discord = dashboard.admin_discord_events(
            77,
            event_type="message",
            query="сенат",
        )
        self.assertEqual(discord["total"], 1)
        self.assertEqual(discord["items"][0]["display_name"], "Участник")
        stats = dashboard.admin_discord_stats(77)
        self.assertEqual(stats["events"], 1)
        self.assertEqual(stats["top_channels"][0]["channel_name"], "сенат")

    def test_invalid_finance_filters_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "finance_bad_game_code"):
            dashboard.admin_finance_events(77, code="bad")
        with self.assertRaisesRegex(ValueError, "finance_bad_audit_kind"):
            dashboard.admin_finance_events(77, event_kind="magic")


if __name__ == "__main__":
    unittest.main()
