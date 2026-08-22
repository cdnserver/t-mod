import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

import storage
from modules.market import (
    MarketAlertModal,
    MarketAlertsView,
    MarketCatalogService,
    MarketHomeView,
    MarketItemView,
    MarketSearchHit,
    _market_internal_id,
    _market_snapshot_change,
    _market_snapshot_change_details,
    dispatch_market_alerts,
    market_alert_dm_embed,
    market_alerts_embed,
    market_home_embed,
    market_item_embed,
    market_results_embed,
    normalize_market_text,
    setup_market,
)
from modules.majestic_api import MajesticApiResponseError, MajesticMarketplaceEntry, MajesticMarketplaceSummary


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


def assert_discord_embed_limits(test_case: unittest.TestCase, embed: discord.Embed) -> None:
    test_case.assertLessEqual(len(embed), 6000)
    test_case.assertLessEqual(len(embed.title or ""), 256)
    test_case.assertLessEqual(len(embed.description or ""), 4096)
    test_case.assertLessEqual(len(embed.fields), 25)
    for field in embed.fields:
        test_case.assertLessEqual(len(field.name), 256)
        test_case.assertLessEqual(len(field.value), 1024)


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
        return self.save_category("items", source_updated_at, items)

    def save_category(self, category: str, source_updated_at: str, items: list[dict]) -> dict:
        return storage.market_replace_snapshot(
            server_id="RU15",
            category=category,
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
            entries=tuple(
                MajesticMarketplaceEntry(
                    external_id=str(index),
                    item_name=f"Предмет {index}",
                    total_count=10,
                    sold_count=5,
                    average_price=100,
                    min_price=50,
                    max_price=150,
                    metadata={"item_id": index, "quantity_metric": "total_count"},
                )
                for index in range(1, 11)
            ),
        )

        class FakeClient:
            async def marketplace_snapshot_async(self, *_args, **_kwargs):
                return snapshot

        catalog = MarketCatalogService("RU15")
        with patch("modules.market.get_majestic_api_client", return_value=FakeClient()):
            with self.assertRaises(MajesticApiResponseError):
                asyncio.run(catalog.sync())
        self.assertEqual(storage.market_catalog_status("RU15")["record_count"], 101)

    def test_vehicle_search_accepts_russian_transliteration_and_model(self) -> None:
        vehicle_id = _market_internal_id("vehicles", "faggio")
        self.save_category(
            "vehicles",
            "2026-07-15T02:12:11.960Z",
            [
                {
                    **item(vehicle_id, "Pegassi Faggio Sport", 150000, total_count=42, sold_count=9),
                    "external_id": "faggio",
                    "metadata": {"model": "faggio", "quantity_metric": "total_count"},
                }
            ],
        )
        catalog = MarketCatalogService("RU15", "vehicles")
        self.assertEqual(catalog.search("пегасси")[0].item["external_id"], "faggio")
        self.assertEqual(catalog.search("фаджио")[0].item["external_id"], "faggio")
        self.assertEqual(catalog.search("faggio")[0].item["item_id"], vehicle_id)

    def test_clothing_variants_with_same_name_remain_distinct(self) -> None:
        rows = []
        for texture in (2, 3):
            external_id = f"1:6:12:{texture}:0"
            internal_id = _market_internal_id("clothes", external_id)
            rows.append(
                {
                    **item(internal_id, "Чёрная сумка", 50000, total_count=17, sold_count=17),
                    "external_id": external_id,
                    "metadata": {
                        "gender": 1,
                        "component": 6,
                        "drawable": 12,
                        "texture": texture,
                        "isProp": 0,
                        "quantity_metric": "sold_count",
                    },
                }
            )
        self.save_category("clothes", "2026-07-15T02:12:11.960Z", rows)
        catalog = MarketCatalogService("RU15", "clothes")
        hits = catalog.search("черная сумка")
        self.assertEqual(len(hits), 2)
        self.assertEqual({hit.item["metadata"]["texture"] for hit in hits}, {2, 3})
        self.assertEqual(len({hit.item["item_id"] for hit in hits}), 2)

    def test_vehicle_alert_keeps_category_and_model_in_dm(self) -> None:
        vehicle_id = _market_internal_id("vehicles", "faggio")
        vehicle = {
            **item(vehicle_id, "Pegassi Faggio Sport", 150000, total_count=42, sold_count=9),
            "external_id": "faggio",
            "metadata": {"model": "faggio", "quantity_metric": "total_count"},
        }
        first_source = "2026-07-15T01:06:28.191Z"
        second_source = "2026-07-16T01:06:28.191Z"
        self.save_category("vehicles", first_source, [vehicle])
        storage.market_upsert_alert(
            discord_user_id=100,
            user_display="Tester",
            guild_id=200,
            server_id="RU15",
            category="vehicles",
            item_id=vehicle_id,
            target_price=100000,
            min_quantity=40,
            current_source_updated_at=first_source,
        )
        self.save_category("vehicles", second_source, [vehicle])
        self.assertEqual(storage.market_evaluate_alerts("RU15", second_source, "vehicles"), 1)
        notification = storage.market_pending_alert_notifications()[0]
        self.assertEqual(notification["category"], "vehicles")
        self.assertEqual(notification["external_id"], "faggio")
        self.assertEqual(notification["metadata"]["model"], "faggio")
        self.assertIn("model: faggio", market_alert_dm_embed(notification).fields[-1].value)

    def test_menu_and_item_card_are_private_and_compact(self) -> None:
        status = self.save("2026-07-15T02:12:11.960Z", [item(39, "Железная руда", 750)])
        current = storage.market_get_item("RU15", 39)
        history = storage.market_item_history("RU15", 39)
        home = market_home_embed(status)
        detail = market_item_embed(current, status, history)

        assert_discord_embed_limits(self, home)
        assert_discord_embed_limits(self, detail)
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
        self.assertEqual([parameter.name for parameter in command.parameters], ["category", "query"])
        category = command.parameters[0]
        self.assertFalse(category.required)
        self.assertEqual({choice.value for choice in category.choices}, {"items", "vehicles", "clothes"})

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
        assert_discord_embed_limits(self, listing)
        assert_discord_embed_limits(self, dm)

        async def inspect_ui() -> None:
            item_row = storage.market_get_item("RU15", 39)
            modal = MarketAlertModal(100, item_row, query=None, back_to_tvrs=False, alert=alert)
            self.assertEqual(len(modal.children), 2)
            self.assertTrue(all(len(str(field._underlying.label)) <= 45 for field in modal.children))
            alerts_view = MarketAlertsView(100, alerts)
            item_view = MarketItemView(100, 39, query=None, alert=alert)
            self.assertLessEqual(len(alerts_view.children), 25)
            self.assertLessEqual(len(item_view.children), 25)
            self.assertTrue(any(getattr(child, "label", None) == "Изменить сигнал" for child in item_view.children))

        asyncio.run(inspect_ui())

    def test_alert_modal_acknowledges_before_reading_busy_catalog(self) -> None:
        source = "2026-07-15T02:12:11.960Z"
        self.save(source, [item(39, "Железная руда", 100, total_count=20)])
        item_row = storage.market_get_item("RU15", 39)
        events: list[str] = []

        class FakeResponse:
            deferred = False

            async def defer(self) -> None:
                self.deferred = True
                events.append("acknowledged")

            async def send_message(self, *_args, **_kwargs) -> None:
                raise AssertionError("Valid modal must defer instead of sending an initial error")

        class FakeFollowup:
            async def send(self, *_args, **_kwargs) -> None:
                events.append("followup")

        response = FakeResponse()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=100, display_name="Tester"),
            guild_id=200,
            response=response,
            followup=FakeFollowup(),
        )

        def guarded_get_item(item_id: int) -> dict:
            self.assertTrue(response.deferred)
            events.append("catalog_read")
            self.assertEqual(item_id, 39)
            return item_row

        async def submit() -> None:
            modal = MarketAlertModal(100, item_row, query=None, back_to_tvrs=False, alert=None)
            catalog = SimpleNamespace(get_item=guarded_get_item)
            with (
                patch("modules.market.get_market_catalog", return_value=catalog),
                patch("modules.market._show_item", new_callable=AsyncMock) as show_item,
            ):
                await modal.on_submit(interaction)
            show_item.assert_awaited_once()

        asyncio.run(submit())
        self.assertEqual(events[:2], ["acknowledged", "catalog_read"])
        self.assertIsNotNone(storage.market_get_alert(100, "RU15", 39))

    def test_long_popular_and_alert_lists_fit_each_discord_field(self) -> None:
        status = {
            "record_count": 1360,
            "source_updated_at": "2026-07-15T01:06:28.191Z",
        }
        hits = [
            MarketSearchHit(
                item={
                    **item(index, f"Pegassi очень длинное название модели {index}" * 2, 10**12),
                    "category": "vehicles",
                    "external_id": f"extremely_long_vehicle_model_code_{index}",
                },
                score=0,
            )
            for index in range(25)
        ]
        popular = market_results_embed("Популярное по продажам", hits, status, "vehicles")
        assert_discord_embed_limits(self, popular)
        self.assertIn("в списке ниже", popular.fields[0].value)

        alerts = [
            {
                "item_id": index,
                "item_name": f"Очень длинное название варианта одежды {index}" * 2,
                "category": "clothes",
                "external_id": f"1:6:12:{index}:0",
                "metadata": {
                    "gender": 1,
                    "component": 6,
                    "drawable": 12,
                    "texture": index,
                    "isProp": 0,
                },
                "status": "active",
                "target_price": 10**12,
                "min_quantity": 1000,
            }
            for index in range(20)
        ]
        listing = market_alerts_embed(alerts, status)
        assert_discord_embed_limits(self, listing)
        self.assertIn("в списке ниже", listing.fields[0].value)

    def test_snapshot_notice_only_tracks_real_updates_after_initial_sync(self) -> None:
        initial = {
            "source_updated_at": "2026-07-15T02:12:11.960Z",
            "record_count": 1315,
        }
        updated = {
            "source_updated_at": "2026-07-16T02:12:11.960Z",
            "record_count": 1320,
        }

        self.assertIsNone(_market_snapshot_change("items", {}, initial))
        self.assertIsNone(_market_snapshot_change("items", initial, initial))

        change = _market_snapshot_change("items", initial, updated)
        self.assertIsNotNone(change)
        self.assertEqual(change.category, "items")
        self.assertEqual(change.source_updated_at, updated["source_updated_at"])
        self.assertEqual(change.record_count, 1320)

        details = _market_snapshot_change_details([change])
        self.assertIn("Majestic опубликовал новые данные", details)
        self.assertIn("**Предметы**", details)
        self.assertIn("1 320 позиций", details)

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

    def test_disabled_personal_market_dm_suppresses_delivery_without_retrying(self) -> None:
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
            target_price=100,
            min_quantity=10,
            current_source_updated_at=first_source,
        )
        self.save(second_source, [item(39, "Железная руда", 90, total_count=20)])
        storage.market_evaluate_alerts("RU15", second_source)
        storage.update_member_profile_preferences(200, 100, dm_market=False)

        class NoDmBot:
            def get_user(self, _user_id):
                raise AssertionError("DM must not be requested")

        asyncio.run(dispatch_market_alerts(NoDmBot()))
        self.assertEqual(storage.market_pending_alert_notifications(), [])
        self.assertEqual(storage.market_get_alert(100, "RU15", 39)["status"], "triggered")

    def test_global_ban_suppresses_personal_market_dm(self) -> None:
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
            target_price=100,
            min_quantity=10,
            current_source_updated_at=first_source,
        )
        self.save(second_source, [item(39, "Железная руда", 90, total_count=20)])
        storage.market_evaluate_alerts("RU15", second_source)

        class NoDmBot:
            def get_user(self, _user_id):
                raise AssertionError("DM must not be requested for a globally banned identity")

        with patch(
            "modules.market.global_ban_storage.is_globally_banned",
            return_value=True,
        ):
            asyncio.run(dispatch_market_alerts(NoDmBot()))

        self.assertEqual(storage.market_pending_alert_notifications(), [])
        self.assertEqual(storage.market_get_alert(100, "RU15", 39)["status"], "notifying")


if __name__ == "__main__":
    unittest.main()
