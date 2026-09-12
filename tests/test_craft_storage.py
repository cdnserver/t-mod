import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import storage
from modules.atlas_web import _overlay_craft_snapshot


class CraftStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "craft-test.db"
        storage.LEGACY_ACTIVITY_FILE = storage.DATA_DIR / "activity.json"
        storage.init_db()
        self.recipe = storage.craft_create_recipe(
            guild_id=1,
            product_name="Промышленные металлы",
            treasury_cost_per_unit=1_000,
            duration_minutes_per_unit=10,
            max_batch_size=10,
            materials=[
                ("Железная руда", 50),
                ("Серебряная руда", 30),
                ("Медная руда", 15),
                ("Оловянная руда", 5),
            ],
            created_by_id=10,
            created_by_display="Recipe Author",
        )
        self.plan = storage.craft_create_plan(
            guild_id=1,
            recipe_id=self.recipe["id"],
            channel_id=300,
            attempts_total=100,
            responsible_id=20,
            responsible_display="Responsible",
            created_by_id=10,
            created_by_display="Planner",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def exact_finance_balance(self, amount: int = 1_000_000) -> None:
        storage.finance_record_snapshot(
            guild_id=1,
            event_kind="interim",
            amount=amount,
            actor_id=99,
            actor_display="Treasurer",
            channel_id=300,
            message_id=None,
            log_channel_id=300,
            admin_user_id=400,
            report_date="2026-07-13",
        )

    def procure_all(self) -> dict:
        plan = storage.craft_get_plan(self.plan["id"])
        for material in plan["materials"]:
            result = storage.craft_add_purchase(
                guild_id=1,
                plan_id=plan["id"],
                plan_material_id=material["id"],
                quantity=material["required_total"],
                total_cost=material["required_total"] * 10,
                finance_code=None,
                actor_id=30,
                actor_display="Buyer",
            )
        return result["plan"]

    def start_and_complete_batch(self, quantity: int, index: int) -> dict:
        result = storage.craft_start_batch(
            guild_id=1,
            plan_id=self.plan["id"],
            quantity=quantity,
            actor_id=40,
            actor_display="Crafter",
            log_channel_id=300,
            admin_user_id=400,
        )
        due = datetime.fromisoformat(result["batch"]["due_at"]) + timedelta(seconds=1)
        changed = storage.craft_complete_due_batches(due.isoformat())
        self.assertIn(self.plan["id"], changed)
        return storage.craft_get_plan(self.plan["id"])

    def test_recipe_and_plan_expand_materials_for_all_attempts(self) -> None:
        plan = storage.craft_get_plan(self.plan["id"])
        required = {item["material_name"]: item["required_total"] for item in plan["materials"]}
        self.assertEqual(
            required,
            {
                "Железная руда": 5_000,
                "Серебряная руда": 3_000,
                "Медная руда": 1_500,
                "Оловянная руда": 500,
            },
        )
        self.assertEqual(plan["stage"], "procurement")

    def test_atlas_overlay_projection_tracks_live_cycle_without_finance_data(self) -> None:
        self.procure_all()
        self.exact_finance_balance()
        started = storage.craft_start_batch(
            guild_id=1,
            plan_id=self.plan["id"],
            quantity=10,
            actor_id=20,
            actor_display="Responsible",
            log_channel_id=300,
            admin_user_id=400,
        )
        active = _overlay_craft_snapshot(1, 20)
        projected = active["plans"][0]
        self.assertTrue(projected["mine"])
        self.assertEqual(projected["active_batch"]["id"], started["batch"]["id"])
        self.assertNotIn("purchase_cost_total", projected)

        due = datetime.fromisoformat(started["batch"]["due_at"]) + timedelta(seconds=1)
        storage.craft_complete_due_batches(due.isoformat())
        waiting = _overlay_craft_snapshot(1, 20)["plans"][0]
        self.assertTrue(waiting["needs_next_batch"])
        self.assertTrue(str(waiting["alarm_key"]).startswith(f"craft:{self.plan['id']}:next:"))
    def test_unbound_plan_can_be_cleaned_up_after_discord_failure(self) -> None:
        self.assertTrue(storage.craft_delete_unbound_plan(self.plan["id"]))
        self.assertIsNone(storage.craft_get_plan(self.plan["id"]))
        self.assertFalse(storage.craft_delete_unbound_plan(self.plan["id"]))

    def test_plan_rejects_material_totals_outside_sqlite_range(self) -> None:
        huge_recipe = storage.craft_create_recipe(
            guild_id=1,
            product_name="Очень большой рецепт",
            treasury_cost_per_unit=0,
            duration_minutes_per_unit=1,
            max_batch_size=1,
            materials=[("Сверхтяжёлый материал", 1_000_000_000_000_000)],
            created_by_id=10,
            created_by_display="Planner",
        )
        with self.assertRaisesRegex(ValueError, "craft_plan_too_large"):
            storage.craft_create_plan(
                guild_id=1,
                recipe_id=huge_recipe["id"],
                channel_id=300,
                attempts_total=1_000_000,
                responsible_id=20,
                responsible_display="Responsible",
                created_by_id=10,
                created_by_display="Planner",
            )

    def test_procurement_tracks_costs_and_opens_crafting(self) -> None:
        plan = self.procure_all()
        self.assertEqual(plan["stage"], "crafting")
        self.assertEqual(plan["purchase_count"], 4)
        self.assertEqual(plan["purchase_cost_total"], 100_000)
        self.assertTrue(all(item["stock_quantity"] == item["required_total"] for item in plan["materials"]))

    def test_partial_materials_can_start_and_restock_multiple_cycles(self) -> None:
        self.exact_finance_balance()
        plan = storage.craft_get_plan(self.plan["id"])
        for material in plan["materials"]:
            result = storage.craft_add_purchase(
                guild_id=1,
                plan_id=plan["id"],
                plan_material_id=material["id"],
                quantity=material["quantity_per_unit"] * 3,
                total_cost=0,
                finance_code=None,
                actor_id=30,
                actor_display="Buyer",
            )
        self.assertEqual(result["plan"]["stage"], "procurement")

        result = storage.craft_start_batch(
            guild_id=1,
            plan_id=plan["id"],
            quantity=3,
            actor_id=40,
            actor_display="Crafter",
            log_channel_id=300,
            admin_user_id=400,
        )
        self.assertEqual(result["plan"]["stage"], "crafting")
        self.assertEqual(result["plan"]["attempts_queued"], 3)
        self.assertTrue(all(material["stock_quantity"] == 0 for material in result["plan"]["materials"]))
        due = datetime.fromisoformat(result["batch"]["due_at"]) + timedelta(seconds=1)
        storage.craft_complete_due_batches(due.isoformat())

        plan = storage.craft_get_plan(plan["id"])
        for material in plan["materials"]:
            storage.craft_add_purchase(
                guild_id=1,
                plan_id=plan["id"],
                plan_material_id=material["id"],
                quantity=material["quantity_per_unit"] * 2,
                total_cost=0,
                finance_code=None,
                actor_id=30,
                actor_display="Buyer",
            )
        with self.assertRaisesRegex(ValueError, "craft_batch_materials_missing"):
            storage.craft_start_batch(
                guild_id=1,
                plan_id=plan["id"],
                quantity=3,
                actor_id=40,
                actor_display="Crafter",
                log_channel_id=300,
                admin_user_id=400,
            )
        result = storage.craft_start_batch(
            guild_id=1,
            plan_id=plan["id"],
            quantity=2,
            actor_id=40,
            actor_display="Crafter",
            log_channel_id=300,
            admin_user_id=400,
        )
        self.assertEqual(result["plan"]["attempts_queued"], 5)
        self.assertTrue(all(material["stock_quantity"] == 0 for material in result["plan"]["materials"]))

    def test_purchase_can_link_exact_finance_withdrawal_code(self) -> None:
        storage.finance_record_snapshot(
            guild_id=1,
            event_kind="interim",
            amount=1_000_000,
            actor_id=99,
            actor_display="Treasurer",
            channel_id=300,
            message_id=None,
            log_channel_id=300,
            admin_user_id=400,
            report_date="2026-07-13",
        )
        withdrawal = storage.finance_record_movement(
            guild_id=1,
            event_kind="withdraw",
            amount=50_000,
            reason="Закупка железной руды для крафта",
            captcha_digest="digest",
            game_code="ABCD",
            actor_id=30,
            actor_display="Buyer",
            channel_id=300,
            message_id=None,
            log_channel_id=300,
            admin_user_id=400,
        )
        material = storage.craft_get_plan(self.plan["id"])["materials"][0]
        storage.craft_add_purchase(
            guild_id=1,
            plan_id=self.plan["id"],
            plan_material_id=material["id"],
            quantity=1_000,
            total_cost=50_000,
            finance_code="ABCD",
            actor_id=30,
            actor_display="Buyer",
        )
        with storage.connect() as con:
            row = con.execute("SELECT * FROM craft_purchases WHERE finance_event_id = ?", (withdrawal["id"],)).fetchone()
        self.assertIsNotNone(row)
        with self.assertRaisesRegex(ValueError, "finance_action_locked_by_craft"):
            storage.finance_undo_last_action(
                guild_id=1,
                target_actor_id=30,
                undone_by_id=30,
                undone_by_display="Buyer",
                channel_id=300,
                message_id=None,
                log_channel_id=300,
                admin_user_id=400,
            )

    def test_batch_uses_materials_finance_and_one_hundred_minutes(self) -> None:
        self.exact_finance_balance()
        self.procure_all()
        result = storage.craft_start_batch(
            guild_id=1,
            plan_id=self.plan["id"],
            quantity=10,
            actor_id=40,
            actor_display="Crafter",
            log_channel_id=300,
            admin_user_id=400,
        )
        started = datetime.fromisoformat(result["batch"]["started_at"])
        due = datetime.fromisoformat(result["batch"]["due_at"])
        self.assertEqual(int((due - started).total_seconds()), 100 * 60)
        self.assertEqual(storage.finance_get_latest_state(1)["estimated_balance"], 990_000)
        with self.assertRaisesRegex(ValueError, "finance_action_locked_by_craft"):
            storage.finance_undo_last_action(
                guild_id=1,
                target_actor_id=40,
                undone_by_id=40,
                undone_by_display="Crafter",
                channel_id=300,
                message_id=None,
                log_channel_id=300,
                admin_user_id=400,
            )
        plan = result["plan"]
        iron = next(item for item in plan["materials"] if item["material_name"] == "Железная руда")
        self.assertEqual(iron["stock_quantity"], 4_500)

    def test_active_ten_item_batch_can_skip_time_with_audit_event(self) -> None:
        self.exact_finance_balance()
        self.procure_all()
        started = storage.craft_start_batch(
            guild_id=1,
            plan_id=self.plan["id"],
            quantity=10,
            actor_id=40,
            actor_display="Crafter",
            log_channel_id=300,
            admin_user_id=400,
        )
        before = datetime.fromisoformat(started["batch"]["due_at"])

        result = storage.craft_skip_active_batch_time(
            guild_id=1,
            plan_id=self.plan["id"],
            minutes=25,
            actor_id=40,
            actor_display="Crafter",
        )

        after = datetime.fromisoformat(result["batch"]["due_at"])
        self.assertEqual(int((before - after).total_seconds()), 25 * 60)
        self.assertFalse(result["completes_now"])
        with storage.connect() as con:
            event = con.execute(
                "SELECT * FROM craft_events WHERE id = ?", (result["event_id"],)
            ).fetchone()
        self.assertEqual(event["event_kind"], "batch_time_skipped")

    def test_time_skip_rejects_missing_active_batch(self) -> None:
        with self.assertRaisesRegex(ValueError, "craft_batch_not_active"):
            storage.craft_skip_active_batch_time(
                guild_id=1,
                plan_id=self.plan["id"],
                minutes=10,
                actor_id=40,
                actor_display="Crafter",
            )

    def test_only_one_concurrent_batch_can_start(self) -> None:
        self.exact_finance_balance()
        self.procure_all()

        def attempt(_: int):
            try:
                return storage.craft_start_batch(
                    guild_id=1,
                    plan_id=self.plan["id"],
                    quantity=10,
                    actor_id=40,
                    actor_display="Crafter",
                    log_channel_id=300,
                    admin_user_id=400,
                )
            except ValueError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [1, 2]))
        self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
        self.assertEqual(sum(item == "craft_batch_active" for item in results), 1)
        self.assertEqual(storage.finance_get_latest_state(1)["estimated_balance"], 990_000)

    def test_full_cycle_to_partial_sales_and_completion(self) -> None:
        self.exact_finance_balance()
        self.procure_all()
        for index in range(10):
            plan = self.start_and_complete_batch(10, index)
        self.assertEqual(plan["stage"], "awaiting_output")
        self.assertEqual(plan["attempts_completed"], 100)
        self.assertEqual(storage.finance_get_latest_state(1)["estimated_balance"], 900_000)

        plan = storage.craft_set_final_output(
            guild_id=1,
            plan_id=plan["id"],
            product_quantity=90,
            actor_id=40,
            actor_display="Crafter",
        )
        self.assertEqual(plan["stage"], "listing")
        storage.craft_set_estimated_price(
            guild_id=1,
            plan_id=plan["id"],
            unit_price=125_000,
            actor_id=20,
            actor_display="Responsible",
        )
        plan = storage.craft_add_market_listing(
            guild_id=1,
            plan_id=plan["id"],
            quantity=40,
            actor_id=20,
            actor_display="Responsible",
        )
        self.assertEqual(plan["stage"], "listing")
        plan = storage.craft_add_market_listing(
            guild_id=1,
            plan_id=plan["id"],
            quantity=50,
            actor_id=20,
            actor_display="Responsible",
        )
        self.assertEqual(plan["stage"], "selling")
        plan = storage.craft_add_sale(
            guild_id=1,
            plan_id=plan["id"],
            quantity=30,
            total_amount=3_750_000,
            actor_id=20,
            actor_display="Responsible",
        )
        self.assertEqual(plan["sold_qty"], 30)
        self.assertEqual(plan["stage"], "selling")
        plan = storage.craft_add_sale(
            guild_id=1,
            plan_id=plan["id"],
            quantity=60,
            total_amount=7_200_000,
            actor_id=20,
            actor_display="Responsible",
        )
        self.assertEqual(plan["stage"], "completed")
        self.assertEqual(plan["sold_qty"], 90)
        self.assertEqual(plan["total_revenue"], 10_950_000)
        candidates = storage.craft_completion_candidates(1)
        self.assertEqual([item["id"] for item in candidates], [plan["id"]])

    def test_reminders_are_unique_per_batch(self) -> None:
        self.exact_finance_balance()
        self.procure_all()
        plan = self.start_and_complete_batch(10, 1)
        candidates = storage.craft_reminder_candidates(1)
        self.assertEqual(len(candidates), 1)
        key = f"b{candidates[0]['last_batch_id']}:m10"
        self.assertTrue(storage.craft_claim_reminder(plan["id"], key))
        self.assertFalse(storage.craft_claim_reminder(plan["id"], key))
        storage.craft_set_reminder_message(plan["id"], key, 123)
        self.assertEqual(storage.craft_open_reminder_messages(plan["id"]), [123])
        storage.craft_mark_reminder_deleted(plan["id"], 123)
        self.assertEqual(storage.craft_open_reminder_messages(plan["id"]), [])


if __name__ == "__main__":
    unittest.main()
