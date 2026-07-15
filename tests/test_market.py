import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import discord
from discord.ext import commands

import storage
from modules.market import (
    MarketAlertModal,
    MarketAlertsView,
    MarketCatalogService,
    MarketHomeView,
    MarketItemView,
    market_alert_dm_embed,
    market_alerts_embed,
    market_home_embed,
    market_item_embed,
    normalize_market_text,
    setup_market,
)
from modules.majestic_api import MajesticApiResponseError, MajesticMarketplaceItem, MajesticMarketplaceSummary


def item(
    item_id: int,
    name: str,
    average_price: int,
    *,
    total_count: int = 100,
    sold_count: int = 50,
) -> dict:
    return {
        "item_id": item_id,
        "item_name": name,
        "normalized_name": normalize_market_text(name),
        "total_count": total_count,
        "sold_count": sold_count,
        "average_price": average_price,
        "min_price": max(1, average_price // 2),
        "max_price": average_price * 2,
    }


class MarketStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "market-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def save(self, source_updated_at: str, items: list[dict]) -> dict:
        return storage.market_replace_snapshot(
            server_id="RU15",
            category="items",
            server_name="Phoenix",
            source_updated_at=source_updated_at,
            period_days=30,
            items=items,
            history_retention_days=400,
        )

    def test_snapshot_is_atomic_and_history_is_idempotent(self) -> None:
        first_source = "2026-07-15T02:12:11.960Z"
        second_source = "2026-07-16T02:12:11.960Z"
        self.save(first_source, [item(1, "Железная руда", 100), item(2, "Аптечка", 500)])
        self.save(first_source, [item(1, "Железная руда", 110), item(2, "Аптечка", 500)])

        self.assertEqual(len(storage.market_item_history("RU15", 1)), 1)
        self.assertEqual(storage.market_get_item("RU15", 1)["average_price"], 110)
        self.assertEqual(storage.market_item_history("RU15", 1)[0]["average_price"], 110)

        status = self.save(second_source, [item(1, "Железная руда", 125)])
        self.assertEqual(status["record_count"], 1)
        self.assertEqual(len(storage.market_item_history("RU15", 1)), 2)
        self.assertIsNone(storage.market_get_item("RU15", 2))
        self.assertEqual([row["item_id"] for row in storage.market_list_items("RU15")], [1])

    def test_russian_search_handles_fragments_typos_and_ids(self) -> None:
        self.save(
            "2026-07-15T02:12:11.960Z",
            [
                item(39, "Железная руда", 750, sold_count=200),
                item(40, "Промышленные металлы", 1500, sold_count=100),
                item(41, "Улучшенная аптечка", 5000, sold_count=50),
            ],
        )
        catalog = MarketCatalogService("RU15")

        self.assertEqual(catalog.search("железная руда")[0].item["item_id"], 39)
        self.assertEqual(catalog.search("железна руда")[0].item["item_id"], 39)
        self.assertEqual(catalog.search("пром метал")[0].item["item_id"], 40)
        self.assertEqual(catalog.search("#39")[0].item["item_id"], 39)
        self.assertEqual(catalog.search("аптеч")[0].item["item_id"], 41)

    def test_sync_errors_do_not_remove_last_good_catalog(self) -> None:
        self.save("2026-07-15T02:12:11.960Z", [item(39, "Железная руда", 750)])
        state = storage.market_record_sync_error("RU15", "items", "network unavailable")
        self.assertEqual(state["consecutive_failures"], 1)
        self.assertEqual(storage.market_get_item("RU15", 39)["item_name"], "Железная руда")

    def test_suspiciously_partial_api_response_cannot_replace_catalog(self) -> None:
        self.save(
            "2026-07-15T02:12:11.960Z",
            [item(index, f"Предмет {index}", 100 + index) for index in range(1, 102)],
        )
        summary = MajesticMarketplaceSummary(
            category="items",
            server_id="RU15",
            server_name="Phoenix",
            record_count=10,
            total_count=10,
            total_sold=5,
            overall_average_price=100,
            last_updated="2026-07-16T02:12:11.960Z",
            period_days=30,
        )
        snapshot = SimpleNamespace(
            summary=summary,
            items=tuple(
                MajesticMarketplaceItem(
                    item_id=index,
                    item_name=f"Предмет {index}",
                    total_count=10,
                    sold_count=5,
                    average_price=100,
                    min_price=50,
                    max_price=150,
                )
                for index in range(1, 11)
            ),
        )

        class FakeClient:
            async def marketplace_items_snapshot_async(self, *_args, **_kwargs):
                return snapshot

        catalog = MarketCatalogService("RU15")
        with patch("modules.market.get_majestic_api_client", return_value=FakeClient()):
            with self.assertRaises(MajesticApiResponseError):
                asyncio.run(catalog.sync())
        self.assertEqual(storage.market_catalog_status("RU15")["record_count"], 101)

    def test_menu_and_item_card_are_private_and_compact(self) -> None:
        status = self.save("2026-07-15T02:12:11.960Z", [item(39, "Железная руда", 750)])
        current = storage.market_get_item("RU15", 39)
        history = storage.market_item_history("RU15", 39)
        home = market_home_embed(status)
        detail = market_item_embed(current, status, history)

        self.assertLessEqual(len(home), 6000)
        self.assertLessEqual(len(detail), 6000)
        self.assertIn("750 $", "\n".join(str(field.value) for field in detail.fields))

        async def inspect_view() -> None:
            view = MarketHomeView(100)
            self.assertEqual(view.timeout, 900)
            self.assertTrue(any(item.label == "Найти предмет" for item in view.children))

        asyncio.run(inspect_view())

    def test_market_command_is_registered_with_optional_query(self) -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        setup_market(bot)
        command = bot.tree.get_command("market")
        self.assertIsNotNone(command)
        self.assertEqual(command.name, "market")

    def test_personal_alert_waits_for_new_snapshot_and_is_one_shot(self) -> None:
        first_source = "2026-07-15T02:12:11.960Z"
        second_source = "2026-07-16T02:12:11.960Z"
        self.save(first_source, [item(39, "Железная руда", 100, total_count=20)])
        alert = storage.market_upsert_alert(
            discord_user_id=100,
            user_display="Tester",
            guild_id=200,
            server_id="RU15",
            category="items",
            item_id=39,
            target_price=60,
            min_quantity=10,
            current_source_updated_at=first_source,
        )

        self.assertEqual(alert["status"], "active")
        self.assertEqual(storage.market_evaluate_alerts("RU15", first_source), 0)

        self.save(second_source, [item(39, "Железная руда", 100, total_count=20)])
        self.assertEqual(storage.market_evaluate_alerts("RU15", second_source), 1)
        self.assertEqual(storage.market_evaluate_alerts("RU15", second_source), 0)
        pending = storage.market_pending_alert_notifications()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["observed_price"], 50)
        self.assertEqual(pending[0]["observed_quantity"], 20)

        storage.market_mark_alert_delivery(pending[0]["id"], delivered=True, dm_message_id=555)
        saved = storage.market_get_alert(100, "RU15", 39)
        self.assertEqual(saved["status"], "triggered")
        self.assertEqual(storage.market_pending_alert_notifications(), [])

    def test_alert_can_pause_resume_update_and_delete_without_stale_delivery(self) -> None:
        first_source = "2026-07-15T02:12:11.960Z"
        second_source = "2026-07-16T02:12:11.960Z"
        self.save(first_source, [item(39, "Железная руда", 100, total_count=20)])
        alert = storage.market_upsert_alert(
            discord_user_id=100,
            user_display="Tester",
            guild_id=200,
            server_id="RU15",
            category="items",
            item_id=39,
            target_price=60,
            min_quantity=10,
            current_source_updated_at=first_source,
        )
        self.save(second_source, [item(39, "Железная руда", 100, total_count=20)])
        self.assertEqual(storage.market_evaluate_alerts("RU15", second_source), 1)

        storage.market_set_alert_status(100, alert["id"], "paused")
        self.assertEqual(storage.market_pending_alert_notifications(), [])
        storage.market_set_alert_status(
            100,
            alert["id"],
            "active",
            current_source_updated_at=second_source,
        )
        updated = storage.market_upsert_alert(
            discord_user_id=100,
            user_display="Tester",
            guild_id=200,
            server_id="RU15",
            category="items",
            item_id=39,
            target_price=45,
            min_quantity=30,
            current_source_updated_at=second_source,
        )
        self.assertEqual(updated["target_price"], 45)
        self.assertEqual(updated["min_quantity"], 30)
        self.assertTrue(storage.market_delete_alert(100, alert["id"]))
        self.assertIsNone(storage.market_get_alert(100, "RU15", 39))

    def test_alert_ui_and_dm_fit_discord_limits(self) -> None:
        source = "2026-07-15T02:12:11.960Z"
        status = self.save(source, [item(39, "Железная руда", 100, total_count=20)])
        alert = storage.market_upsert_alert(
            discord_user_id=100,
            user_display="Tester",
            guild_id=200,
            server_id="RU15",
            category="items",
            item_id=39,
            target_price=60,
            min_quantity=10,
            current_source_updated_at=source,
        )
        alerts = storage.market_list_user_alerts(100)
        listing = market_alerts_embed(alerts, status)
        notification = {
            **alert,
            "item_name": "Железная руда",
            "observed_price": 50,
            "observed_quantity": 20,
            "source_updated_at": source,
        }
        dm = market_alert_dm_embed(notification)
        self.assertLessEqual(len(listing), 6000)
        self.assertLessEqual(len(dm), 6000)

        async def inspect_ui() -> None:
            item_row = storage.market_get_item("RU15", 39)
            modal = MarketAlertModal(100, item_row, query=None, back_to_tvrs=False, alert=alert)
            self.assertEqual(len(modal.children), 2)
            self.assertTrue(all(len(str(field.label)) <= 45 for field in modal.children))
            alerts_view = MarketAlertsView(100, alerts)
            item_view = MarketItemView(100, 39, query=None, alert=alert)
            self.assertLessEqual(len(alerts_view.children), 25)
            self.assertLessEqual(len(item_view.children), 25)
            self.assertTrue(any(getattr(child, "label", None) == "Изменить сигнал" for child in item_view.children))

        asyncio.run(inspect_ui())

    def test_failed_dm_delivery_retries_then_pauses_alert(self) -> None:
        first_source = "2026-07-15T02:12:11.960Z"
        second_source = "2026-07-16T02:12:11.960Z"
        self.save(first_source, [item(39, "Железная руда", 100, total_count=20)])
        storage.market_upsert_alert(
            discord_user_id=100,
            user_display="Tester",
            guild_id=200,
            server_id="RU15",
            category="items",
            item_id=39,
            target_price=60,
            min_quantity=10,
            current_source_updated_at=first_source,
        )
        self.save(second_source, [item(39, "Железная руда", 100, total_count=20)])
        storage.market_evaluate_alerts("RU15", second_source)
        notification = storage.market_pending_alert_notifications()[0]

        first = storage.market_mark_alert_delivery(
            notification["id"], delivered=False, error="Forbidden", max_attempts=3
        )
        second = storage.market_mark_alert_delivery(
            notification["id"], delivered=False, error="Forbidden", max_attempts=3
        )
        third = storage.market_mark_alert_delivery(
            notification["id"], delivered=False, error="Forbidden", max_attempts=3
        )

        self.assertEqual(first["status"], "retry")
        self.assertEqual(second["status"], "retry")
        self.assertEqual(third["status"], "failed")
        self.assertEqual(storage.market_get_alert(100, "RU15", 39)["status"], "paused")


if __name__ == "__main__":
    unittest.main()
