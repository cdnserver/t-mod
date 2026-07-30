import tempfile
import unittest
from pathlib import Path

import storage
from persistence import web_portal_repository as portal


class WebPortalRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.old_activity_file = storage.LEGACY_ACTIVITY_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "web-portal-test.db"
        storage.LEGACY_ACTIVITY_FILE = storage.DATA_DIR / "activity.json"
        storage.init_db()

        storage.remember_activity(
            guild_id=77,
            user_id=101,
            display_name="Маркетолог",
            name="market-user",
            mention="<@101>",
            is_bot=False,
            event_type="message",
            event_text="Проверяет цены на сплав",
            channel_id=201,
            channel_name="рынок",
            force=True,
        )
        storage.set_member_profile_status(
            77,
            101,
            "active",
            note="Работает с рынком",
        )
        storage.add_profile_character(77, 101, "Marketov", "10101")
        storage.tvrs_create_bill(
            guild_id=77,
            channel_id=202,
            author_id=101,
            author_display="Маркетолог",
            title="Проект о рынке",
            summary="Создать единый справочник цен.",
            materials=None,
        )
        storage.reserve_sgl_case(
            guild_id=77,
            client_id=102,
            client_display="Клиент",
            lead_lawyer_id=103,
            lead_lawyer_display="Юрист",
            secretary_id=104,
            secretary_display="Секретарь",
            created_by_id=103,
            created_by_display="Юрист",
        )
        storage.create_broadcast_draft(
            guild_id=77,
            author_id=1,
            author_display="Администратор",
            kind="global",
            title="Проверочное уведомление",
            body="Это безопасный тест веб-портала.",
        )
        storage.market_replace_snapshot(
            server_id="RU15",
            category="items",
            server_name="RU15",
            source_updated_at="2026-07-30T10:00:00+00:00",
            period_days=1,
            items=[
                {
                    "item_id": 10,
                    "external_id": "steel",
                    "item_name": "Промышленный сплав",
                    "total_count": 42,
                    "sold_count": 9,
                    "average_price": 125_000,
                    "min_price": 110_000,
                    "max_price": 150_000,
                    "metadata": {"group": "materials"},
                }
            ],
        )

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        storage.LEGACY_ACTIVITY_FILE = self.old_activity_file
        self.temp_dir.cleanup()

    def test_registry_counts_each_major_portal_domain(self) -> None:
        registry = portal.portal_registry(77)

        self.assertEqual(registry["members"], 1)
        self.assertEqual(registry["profiles"], 1)
        self.assertEqual(registry["characters"], 1)
        self.assertEqual(registry["bills"], 1)
        self.assertEqual(registry["sgl_cases"], 1)
        self.assertEqual(registry["broadcasts"], 1)
        self.assertEqual(registry["market_catalogs"][0]["record_count"], 1)

    def test_market_search_is_scoped_filtered_and_paged(self) -> None:
        result = portal.portal_market_search(
            server_id="ru15",
            category="items",
            query="сплав",
            sort="price_asc",
            limit=10,
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["item_name"], "Промышленный сплав")
        self.assertEqual(result["items"][0]["metadata"]["group"], "materials")
        with self.assertRaisesRegex(ValueError, "market_category_invalid"):
            portal.portal_market_search(category="houses")
        with self.assertRaisesRegex(ValueError, "market_sort_invalid"):
            portal.portal_market_search(sort="DROP TABLE")

    def test_case_member_broadcast_and_system_views_are_guild_scoped(self) -> None:
        cases = portal.portal_sgl_cases(77, query="Секретарь")
        members = portal.portal_members(77, query="маркетолог")
        broadcasts = portal.portal_broadcasts(77)
        system = portal.portal_system(77)

        self.assertEqual(cases["total"], 1)
        self.assertEqual(cases["items"][0]["secretary_display"], "Секретарь")
        self.assertEqual(members["total"], 1)
        self.assertEqual(members["items"][0]["character_count"], 1)
        self.assertEqual(broadcasts["total"], 1)
        self.assertEqual(broadcasts["items"][0]["title"], "Проверочное уведомление")
        self.assertIn("outbox_status", system)
        self.assertIn("consensus_sessions", system)

    def test_stable_link_records_are_exact_and_guild_scoped(self) -> None:
        case_id = portal.portal_sgl_cases(77)["items"][0]["id"]
        broadcast_id = portal.portal_broadcasts(77)["items"][0]["id"]

        market = portal.portal_linked_record(77, "market", "RU15.items.10")
        member = portal.portal_linked_record(77, "member", "101")
        case = portal.portal_linked_record(77, "case", str(case_id))
        broadcast = portal.portal_linked_record(
            77,
            "broadcast",
            str(broadcast_id),
        )

        self.assertEqual(market["item_name"], "Промышленный сплав")
        self.assertEqual(market["metadata"]["group"], "materials")
        self.assertEqual(member["display_name"], "Маркетолог")
        self.assertEqual(case["secretary_display"], "Секретарь")
        self.assertEqual(broadcast["title"], "Проверочное уведомление")
        self.assertIsNone(portal.portal_linked_record(78, "member", "101"))
        self.assertIsNone(portal.portal_linked_record(77, "market", "RU15.houses.10"))
        self.assertIsNone(portal.portal_linked_record(77, "unknown", "1"))


if __name__ == "__main__":
    unittest.main()
