import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import discord

from modules.craft import (
    BatchQuantityModal,
    BatchTimeSkipModal,
    CraftPlanView,
    completion_embed,
    create_craft_plan,
    event_log_embed,
    FinalOutputModal,
    InventoryModal,
    ListingModal,
    PlanCreateModal,
    PriceModal,
    PurchaseModal,
    RecipeModal,
    RecipeSelectView,
    SaleModal,
    error_text,
    is_expected_craft_error,
    parse_material_lines,
    plan_embed,
    quiet_hours,
    recipe_embed,
    recipe_list_embed,
    send_interaction_error,
)


def sample_plan(stage: str, *, material_count: int = 4, active: bool = False) -> dict:
    materials = [
        {
            "id": index + 1,
            "material_name": f"Материал {index + 1}",
            "quantity_per_unit": index + 1,
            "required_total": (index + 1) * 100,
            "stock_quantity": 0 if stage == "procurement" else (index + 1) * 100,
            "purchased_quantity": 0,
            "spent_total": 0,
        }
        for index in range(material_count)
    ]
    active_batch = None
    if active:
        active_batch = {
            "quantity": 10,
            "started_by_id": 30,
            "due_at": "2026-07-13T12:00:00+00:00",
        }
    return {
        "id": 7,
        "guild_id": 1,
        "channel_id": 2,
        "message_id": 3,
        "responsible_id": 20,
        "created_by_id": 10,
        "stage": stage,
        "attempts_total": 100,
        "attempts_queued": 10 if active else (100 if stage not in {"procurement", "crafting"} else 0),
        "attempts_completed": 100 if stage not in {"procurement", "crafting"} else 0,
        "product_stock": 90 if stage in {"listing", "selling", "completed"} else 0,
        "final_product_qty": 90 if stage in {"listing", "selling", "completed"} else None,
        "estimated_unit_price": 125_000 if stage in {"selling", "completed"} else None,
        "market_listed_qty": 90 if stage in {"selling", "completed"} else 0,
        "sold_qty": 30 if stage == "selling" else (90 if stage == "completed" else 0),
        "total_revenue": 3_750_000 if stage == "selling" else (11_250_000 if stage == "completed" else 0),
        "purchase_count": 4,
        "purchase_cost_total": 100_000,
        "materials": materials,
        "active_batch": active_batch,
        "recipe": {
            "product_name": "Промышленные металлы",
            "duration_minutes_per_unit": 10,
            "max_batch_size": 10,
            "treasury_cost_per_unit": 1_000,
        },
    }


class CraftFormatTests(unittest.TestCase):
    def test_material_parser_accepts_colon_equals_and_grouping(self) -> None:
        self.assertEqual(
            parse_material_lines("Железная руда: 5 000\nСеребряная руда = 3,000"),
            [("Железная руда", 5_000), ("Серебряная руда", 3_000)],
        )

    def test_quiet_hours_are_two_until_nine(self) -> None:
        tz = ZoneInfo("Europe/Riga")
        self.assertTrue(quiet_hours(datetime(2026, 7, 13, 2, 0, tzinfo=tz)))
        self.assertTrue(quiet_hours(datetime(2026, 7, 13, 8, 59, tzinfo=tz)))
        self.assertFalse(quiet_hours(datetime(2026, 7, 13, 9, 0, tzinfo=tz)))

    def test_bad_purchase_is_a_friendly_validation_error(self) -> None:
        exc = ValueError("craft_bad_purchase")
        self.assertTrue(is_expected_craft_error(exc))
        self.assertIn("Количество должно быть больше нуля", error_text(exc))

    def test_unknown_failure_is_not_hidden_as_validation(self) -> None:
        self.assertFalse(is_expected_craft_error(RuntimeError("database unavailable")))


class CraftComponentTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_responsible_is_a_friendly_validation_error(self) -> None:
        unknown_user = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"),
            {"message": "Unknown User", "code": 10013},
        )
        guild = SimpleNamespace(
            get_member=lambda member_id: None,
            fetch_member=AsyncMock(side_effect=unknown_user),
        )

        with self.assertRaisesRegex(ValueError, "craft_bad_responsible"):
            await create_craft_plan(
                SimpleNamespace(),
                guild,
                recipe_id=1,
                attempts_total=1,
                responsible_id=999,
                created_by_id=1,
                created_by_display="Admin",
            )

    async def test_expected_validation_does_not_alert_technical_log(self) -> None:
        response = SimpleNamespace(is_done=lambda: True)
        followup = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            guild=object(),
            client=object(),
            channel_id=123,
            user=SimpleNamespace(id=456),
            response=response,
            followup=followup,
        )

        with patch("modules.craft.log_technical_event", new=AsyncMock()) as technical_log:
            await send_interaction_error(interaction, ValueError("craft_bad_purchase"))

        technical_log.assert_not_awaited()
        followup.send.assert_awaited_once()
        self.assertIn("Количество должно быть больше нуля", followup.send.await_args.args[0])
        self.assertTrue(followup.send.await_args.kwargs["ephemeral"])

    async def test_unexpected_failure_still_alerts_technical_log(self) -> None:
        response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
        interaction = SimpleNamespace(
            guild=object(),
            client=object(),
            channel_id=123,
            user=SimpleNamespace(id=456),
            response=response,
        )

        with (
            patch("modules.craft.log_technical_event", new=AsyncMock()) as technical_log,
            patch("modules.craft.traceback.print_exception"),
        ):
            await send_interaction_error(interaction, RuntimeError("database unavailable"))

        technical_log.assert_awaited_once()
        response.send_message.assert_awaited_once()

    async def test_all_modals_match_discord_component_limits(self) -> None:
        plan = sample_plan("procurement")
        material = plan["materials"][0]
        modals = [
            RecipeModal(),
            PlanCreateModal(1),
            PurchaseModal(7, material),
            InventoryModal(plan),
            BatchQuantityModal(7, 10),
            BatchTimeSkipModal(7, 100),
            FinalOutputModal(7, 100),
            PriceModal(7),
            ListingModal(7, 90),
            SaleModal(7, 90),
        ]
        for modal in modals:
            with self.subTest(modal=type(modal).__name__):
                self.assertLessEqual(len(modal.title), 45)
                self.assertLessEqual(len(modal.children), 5)
                for item in modal.children:
                    self.assertLessEqual(len(item._underlying.label), 45)
                    if item.placeholder is not None:
                        self.assertLessEqual(len(item.placeholder), 100)

    async def test_adaptive_plan_views_fit_discord_limits(self) -> None:
        procurement = CraftPlanView(sample_plan("procurement", material_count=20))
        self.assertEqual(len(procurement.children), 22)
        self.assertTrue(all((item.row or 0) <= 4 for item in procurement.children))
        self.assertEqual(procurement.children[-2].label, "Пока не хватает материалов")

        partial = sample_plan("procurement")
        for material in partial["materials"]:
            material["stock_quantity"] = material["quantity_per_unit"] * 3
        partial_labels = [item.label for item in CraftPlanView(partial).children]
        self.assertEqual(
            partial_labels[-4:],
            ["Поставил 1 шт.", "Поставил 3 шт.", "Другое количество", "Сверка склада"],
        )

        idle_crafting = CraftPlanView(sample_plan("crafting"))
        self.assertEqual(
            [item.label for item in idle_crafting.children],
            ["Поставил 1 шт.", "Поставил 10 шт.", "Другое количество", "Сверка склада"],
        )
        active_crafting = CraftPlanView(sample_plan("crafting", active=True))
        self.assertTrue(all(item.disabled for item in active_crafting.children[:3]))
        self.assertIn("Скип времени", [item.label for item in active_crafting.children])

        self.assertEqual([item.label for item in CraftPlanView(sample_plan("awaiting_output")).children], ["Указать результат", "Сверка склада"])
        self.assertEqual([item.label for item in CraftPlanView(sample_plan("listing")).children], ["Цена за 1 шт.", "Выставил на маркет", "Сверка склада"])
        self.assertEqual([item.label for item in CraftPlanView(sample_plan("selling")).children], ["Цена за 1 шт.", "Записать продажу", "Сверка склада"])
        self.assertEqual(len(CraftPlanView(sample_plan("completed")).children), 0)

    async def test_plan_embeds_fit_discord_limits(self) -> None:
        for stage in ("procurement", "crafting", "awaiting_output", "listing", "selling", "completed"):
            embed = plan_embed(sample_plan(stage, material_count=20))
            with self.subTest(stage=stage):
                self.assertLessEqual(len(embed), 6000)
                self.assertLessEqual(len(embed.fields), 25)
                self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))

    async def test_plan_embed_shows_material_surplus_as_stock(self) -> None:
        plan = sample_plan("procurement")
        plan["materials"][0]["stock_quantity"] = plan["materials"][0]["required_total"] + 15
        embed = plan_embed(plan)
        materials_field = next(field for field in embed.fields if field.name == "🧱 Материалы")
        self.assertIn("запас **+15**", materials_field.value)

    async def test_large_recipe_catalog_is_paginated_and_embeds_stay_small(self) -> None:
        recipes = [
            {
                "id": index + 1,
                "product_name": f"Продукт {index + 1}",
                "duration_minutes_per_unit": 10,
                "max_batch_size": 10,
                "treasury_cost_per_unit": 1_000,
                "materials": [
                    {"material_name": f"Материал {material + 1}", "quantity_per_unit": material + 1}
                    for material in range(20)
                ],
            }
            for index in range(60)
        ]
        first = RecipeSelectView(recipes)
        middle = RecipeSelectView(recipes, 1)
        last = RecipeSelectView(recipes, 2)
        self.assertEqual([len(first.children[0].options), len(middle.children[0].options), len(last.children[0].options)], [25, 25, 10])
        self.assertEqual([len(first.children), len(middle.children), len(last.children)], [2, 3, 2])
        embed = recipe_list_embed(recipes)
        self.assertLessEqual(len(embed), 6000)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))

    async def test_recipe_inventory_log_and_completion_embeds_fit_limits(self) -> None:
        plan = sample_plan("completed", material_count=20)
        recipe = dict(plan["recipe"])
        recipe.update({"id": 1, "materials": plan["materials"]})
        event = {
            "id": 1,
            "event_kind": "inventory_check",
            "created_at": "2026-07-13T12:00:00+00:00",
            "actor_id": 10,
            "details_json": json.dumps(
                {
                    "materials": {
                        ("Очень длинное название материала " + str(index)) * 2: index * 1_000
                        for index in range(20)
                    },
                    "product_quantity": 90,
                    "note": "Плановая сверка",
                },
                ensure_ascii=False,
            ),
        }
        for embed in (recipe_embed(recipe), event_log_embed(event), completion_embed(plan)):
            self.assertLessEqual(len(embed), 6000)
            self.assertLessEqual(len(embed.fields), 25)
            self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))


if __name__ == "__main__":
    unittest.main()
