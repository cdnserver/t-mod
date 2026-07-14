import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import storage


class UniversalAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "universal-audit-test.db"
        storage.LEGACY_ACTIVITY_FILE = storage.DATA_DIR / "activity.json"
        storage.init_db()
        self.recipe = storage.craft_create_recipe(
            guild_id=1,
            product_name="Тестовый сплав",
            treasury_cost_per_unit=1_000,
            duration_minutes_per_unit=10,
            max_batch_size=10,
            materials=[("Руда", 2), ("Уголь", 1)],
            created_by_id=10,
            created_by_display="Автор",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def snapshot(self, amount: int = 1_000_000) -> dict:
        return storage.finance_record_snapshot(
            guild_id=1,
            event_kind="interim",
            amount=amount,
            actor_id=99,
            actor_display="Казначей",
            channel_id=100,
            message_id=None,
            log_channel_id=200,
            admin_user_id=300,
            report_date="2026-07-13",
        )

    def create_plan(self, attempts: int = 10, actor_id: int = 20) -> dict:
        return storage.craft_create_plan(
            guild_id=1,
            recipe_id=self.recipe["id"],
            channel_id=100,
            attempts_total=attempts,
            responsible_id=21,
            responsible_display="Ответственный",
            created_by_id=actor_id,
            created_by_display="Планировщик",
        )

    def procure(self, plan_id: int, actor_id: int = 30) -> list[dict]:
        results = []
        for material in storage.craft_get_plan(plan_id)["materials"]:
            results.append(
                storage.craft_add_purchase(
                    guild_id=1,
                    plan_id=plan_id,
                    plan_material_id=material["id"],
                    quantity=material["required_total"],
                    total_cost=material["required_total"] * 5,
                    finance_code=None,
                    actor_id=actor_id,
                    actor_display="Закупщик",
                )
            )
        return results

    def undo(self, action_id: int, actor_id: int) -> dict:
        result = storage.bot_undo_action(
            guild_id=1,
            target_actor_id=actor_id,
            undone_by_id=actor_id,
            undone_by_display="Отменяющий",
            reason="Регрессионная проверка",
            log_channel_id=200,
            admin_user_id=300,
            channel_id=100,
            action_id=action_id,
        )
        self.assertIsNotNone(result)
        return result

    def complete_batch(self, batch: dict) -> None:
        due = datetime.fromisoformat(batch["due_at"]) + timedelta(seconds=1)
        storage.craft_complete_due_batches(due.isoformat())

    def test_finance_search_stats_and_exact_action_undo(self) -> None:
        self.snapshot()
        movement = storage.finance_record_movement(
            guild_id=1,
            event_kind="deposit",
            amount=75_000,
            reason="Доход от продажи тестовой партии",
            captcha_digest="digest",
            game_code="QWER",
            actor_id=40,
            actor_display="Продавец",
            channel_id=100,
            message_id=None,
            log_channel_id=200,
            admin_user_id=300,
        )
        found = storage.finance_search_events(1, code="qwer")
        self.assertEqual([row["id"] for row in found], [movement["id"]])
        self.assertEqual(found[0]["action_id"], movement["action_id"])
        self.assertEqual(storage.finance_stats(1, 30)["deposits"], 75_000)

        result = self.undo(movement["action_id"], 40)
        self.assertEqual(result["finance"]["undo_event"]["balance_after"], 1_000_000)
        self.assertEqual(storage.finance_stats(1, 30)["deposits"], 0)
        self.assertEqual(storage.finance_search_events(1, code="QWER")[0]["is_undone"], 1)

    def test_purchase_undo_restores_material_and_stage(self) -> None:
        plan = self.create_plan(attempts=2)
        material = storage.craft_get_plan(plan["id"])["materials"][0]
        purchase = storage.craft_add_purchase(
            guild_id=1,
            plan_id=plan["id"],
            plan_material_id=material["id"],
            quantity=3,
            total_cost=300,
            finance_code=None,
            actor_id=30,
            actor_display="Закупщик",
        )
        self.undo(purchase["action_id"], 30)
        restored = storage.craft_get_plan(plan["id"])
        restored_material = next(item for item in restored["materials"] if item["id"] == material["id"])
        self.assertEqual(restored_material["stock_quantity"], 0)
        self.assertEqual(restored["purchase_count"], 0)
        self.assertEqual(restored["stage"], "procurement")

    def test_stale_purchase_modal_cannot_overfill_material(self) -> None:
        plan = self.create_plan(attempts=2)
        material = storage.craft_get_plan(plan["id"])["materials"][0]
        with self.assertRaisesRegex(ValueError, "craft_purchase_too_large"):
            storage.craft_add_purchase(
                guild_id=1,
                plan_id=plan["id"],
                plan_material_id=material["id"],
                quantity=material["required_total"] + 1,
                total_cost=100,
                finance_code=None,
                actor_id=30,
                actor_display="Закупщик",
            )

    def test_completed_batch_undo_returns_materials_and_treasury(self) -> None:
        self.snapshot()
        plan = self.create_plan(attempts=10)
        self.procure(plan["id"])
        before = storage.craft_get_plan(plan["id"])
        batch_result = storage.craft_start_batch(
            guild_id=1,
            plan_id=plan["id"],
            quantity=10,
            actor_id=40,
            actor_display="Крафтер",
            log_channel_id=200,
            admin_user_id=300,
        )
        self.assertEqual(storage.finance_get_latest_state(1)["estimated_balance"], 990_000)
        self.complete_batch(batch_result["batch"])
        self.undo(batch_result["action_id"], 40)
        restored = storage.craft_get_plan(plan["id"])
        self.assertEqual(restored["attempts_queued"], 0)
        self.assertEqual(restored["attempts_completed"], 0)
        self.assertEqual(restored["stage"], "crafting")
        self.assertEqual(
            [item["stock_quantity"] for item in restored["materials"]],
            [item["stock_quantity"] for item in before["materials"]],
        )
        self.assertEqual(storage.finance_get_latest_state(1)["estimated_balance"], 1_000_000)
        self.assertEqual(storage.craft_stats(1, 30)["batch_count"], 0)

    def test_full_craft_chain_can_be_unwound_in_reverse_order(self) -> None:
        self.snapshot()
        plan = self.create_plan(attempts=1, actor_id=50)
        self.procure(plan["id"], actor_id=50)
        batch_result = storage.craft_start_batch(
            guild_id=1,
            plan_id=plan["id"],
            quantity=1,
            actor_id=50,
            actor_display="Участник",
            log_channel_id=200,
            admin_user_id=300,
        )
        self.complete_batch(batch_result["batch"])
        output = storage.craft_set_final_output(
            guild_id=1,
            plan_id=plan["id"],
            product_quantity=1,
            actor_id=50,
            actor_display="Участник",
        )
        price = storage.craft_set_estimated_price(
            guild_id=1,
            plan_id=plan["id"],
            unit_price=25_000,
            actor_id=50,
            actor_display="Участник",
        )
        listing = storage.craft_add_market_listing(
            guild_id=1,
            plan_id=plan["id"],
            quantity=1,
            actor_id=50,
            actor_display="Участник",
        )
        sale = storage.craft_add_sale(
            guild_id=1,
            plan_id=plan["id"],
            quantity=1,
            total_amount=25_000,
            actor_id=50,
            actor_display="Участник",
        )
        self.assertEqual(storage.craft_get_plan(plan["id"])["stage"], "completed")

        self.undo(sale["action_id"], 50)
        self.assertEqual(storage.craft_get_plan(plan["id"])["stage"], "selling")
        self.undo(listing["action_id"], 50)
        self.assertEqual(storage.craft_get_plan(plan["id"])["stage"], "listing")
        self.undo(price["action_id"], 50)
        self.assertIsNone(storage.craft_get_plan(plan["id"])["estimated_unit_price"])
        self.undo(output["action_id"], 50)
        restored = storage.craft_get_plan(plan["id"])
        self.assertEqual(restored["stage"], "awaiting_output")
        self.assertIsNone(restored["final_product_qty"])

    def test_recipe_edit_is_versioned_and_does_not_mutate_existing_plan(self) -> None:
        plan = self.create_plan(attempts=3)
        updated = storage.craft_update_recipe(
            guild_id=1,
            recipe_id=self.recipe["id"],
            product_name="Обновлённый сплав",
            treasury_cost_per_unit=9_000,
            duration_minutes_per_unit=60,
            max_batch_size=2,
            materials=[("Новая руда", 99)],
            actor_id=10,
            actor_display="Автор",
        )
        old_plan = storage.craft_get_plan(plan["id"])
        self.assertEqual(old_plan["recipe"]["product_name"], "Тестовый сплав")
        self.assertEqual(old_plan["recipe"]["treasury_cost_per_unit"], 1_000)
        self.assertEqual(updated["version"], 2)
        self.assertEqual(len(storage.craft_recipe_versions(self.recipe["id"], 1)), 2)

        self.undo(updated["action_id"], 10)
        restored = storage.craft_get_recipe(self.recipe["id"], 1)
        self.assertEqual(restored["product_name"], "Тестовый сплав")
        self.assertEqual(restored["version"], 3)

    def test_sgl_row_and_tvrs_vote_are_reversible(self) -> None:
        case = storage.reserve_sgl_case(
            guild_id=1,
            client_id=70,
            client_display="Клиент",
            lead_lawyer_id=71,
            lead_lawyer_display="Адвокат",
            secretary_id=None,
            secretary_display=None,
            created_by_id=72,
            created_by_display="Создатель",
        )
        storage.attach_sgl_case_channel(case.id, 777)
        storage.update_sgl_case_situation(1, 777, "Подробная ситуация", 70, "Клиент")
        action = storage.bot_list_actions(1, actor_id=70, module="sgl", limit=1)[0]
        self.undo(action["id"], 70)
        self.assertIsNone(storage.get_sgl_case_by_channel(1, 777).situation_text)

        bill = storage.tvrs_create_bill(
            guild_id=1,
            channel_id=888,
            author_id=80,
            author_display="Автор",
            title="Тестовый закон",
            summary="Описание",
            materials=None,
        )
        storage.tvrs_cast_vote(guild_id=1, bill_id=bill.id, voter_id=81, voter_display="Депутат", vote="approve")
        vote_action = storage.bot_list_actions(1, actor_id=81, module="tvrs", limit=1)[0]
        self.undo(vote_action["id"], 81)
        self.assertEqual(storage.tvrs_votes_for_bill(1, bill.id), [])


if __name__ == "__main__":
    unittest.main()
