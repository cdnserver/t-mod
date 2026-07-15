import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import storage
from modules.craft import CraftMenuView, PlanCreateModal, RecipeSelectView
from modules.finance import AuditCenterView, FinanceAuditSearchModal, FinancePanelView, UndoActionModal
from modules.tvrs import (
    TVRSLinksView,
    TVRSPublicPanelView,
    TVRSUniversalityView,
    build_public_universality_embed,
    build_universality_embed,
)


class TVRSUniversalityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.old_activity_file = storage.LEGACY_ACTIVITY_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "tvrs-universality-test.db"
        storage.LEGACY_ACTIVITY_FILE = storage.DATA_DIR / "activity.json"
        storage.init_db()

    async def asyncTearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        storage.LEGACY_ACTIVITY_FILE = self.old_activity_file
        self.temp_dir.cleanup()

    async def test_hub_and_submenus_fit_discord_component_limits(self) -> None:
        views = [
            TVRSUniversalityView(10),
            FinancePanelView(allow_any_channel=True, requester_id=10, back_to_tvrs=True),
            CraftMenuView(allow_any_channel=True, requester_id=10, back_to_tvrs=True),
            AuditCenterView(10, back_to_tvrs=True),
            TVRSLinksView(10),
        ]
        self.assertEqual(
            [item.label for item in views[0].children],
            ["Казна", "Крафты", "Аудит", "Консенсус", "Законопроекты", "Справка", "Ссылки", "Обновить"],
        )
        self.assertEqual([len(view.children) for view in views], [8, 4, 4, 7, 3])
        self.assertEqual(
            [item.label for item in views[-1].children],
            ["Назад", "Доступ к Бюро", "Заявка в Товарищество"],
        )
        for view in views:
            with self.subTest(view=type(view).__name__):
                self.assertLessEqual(len(view.children), 25)
                self.assertTrue(all((item.row or 0) <= 4 for item in view.children))
                self.assertTrue(all(len(item.label or "") <= 80 for item in view.children))

    async def test_universal_mode_propagates_to_craft_forms(self) -> None:
        recipe = {
            "id": 1,
            "product_name": "Промышленные металлы",
            "duration_minutes_per_unit": 10,
            "max_batch_size": 10,
            "treasury_cost_per_unit": 1_000,
            "materials": [{"material_name": "Железная руда", "quantity_per_unit": 50}],
        }
        selector = RecipeSelectView([recipe], allow_any_channel=True)
        self.assertTrue(selector.allow_any_channel)
        self.assertTrue(selector.children[0].allow_any_channel)
        self.assertTrue(PlanCreateModal(1, allow_any_channel=True).allow_any_channel)

    async def test_public_panel_is_persistent_and_uses_private_entry_buttons(self) -> None:
        view = TVRSPublicPanelView()
        self.assertIsNone(view.timeout)
        self.assertEqual(
            [item.label for item in view.children],
            ["Казна", "Крафты", "Аудит", "Консенсус", "Законопроекты", "Справка", "Ссылки", "Обновить"],
        )
        self.assertTrue(all(item.custom_id for item in view.children))
        embed = build_public_universality_embed(SimpleNamespace(id=77))
        self.assertIn("видят все", embed.description)
        self.assertIn("взаимодействия личные", embed.footer.text)

    async def test_hub_overview_uses_current_finance_and_craft_state(self) -> None:
        storage.finance_record_snapshot(
            guild_id=77,
            event_kind="interim",
            amount=1_250_000,
            actor_id=10,
            actor_display="Казначей",
            channel_id=100,
            message_id=None,
            log_channel_id=200,
            admin_user_id=300,
            report_date="2026-07-13",
        )
        recipe = storage.craft_create_recipe(
            guild_id=77,
            product_name="Промышленные металлы",
            treasury_cost_per_unit=1_000,
            duration_minutes_per_unit=10,
            max_batch_size=10,
            materials=[("Железная руда", 50)],
            created_by_id=10,
            created_by_display="Автор",
        )
        storage.craft_create_plan(
            guild_id=77,
            recipe_id=recipe["id"],
            channel_id=200,
            attempts_total=10,
            responsible_id=10,
            responsible_display="Ответственный",
            created_by_id=10,
            created_by_display="Автор",
        )
        embed = build_universality_embed(SimpleNamespace(id=77), requester_id=10)
        rendered = "\n".join(str(field.value) for field in embed.fields)
        self.assertIn("1 250 000 $", rendered)
        self.assertIn("Активных планов: **1**", rendered)
        self.assertIn("Рецептов: **1**", rendered)
        self.assertLessEqual(len(embed), 6000)
        self.assertLessEqual(len(embed.fields), 25)

    async def test_audit_modals_fit_discord_limits(self) -> None:
        for modal in (FinanceAuditSearchModal(), UndoActionModal()):
            with self.subTest(modal=type(modal).__name__):
                self.assertLessEqual(len(modal.title), 45)
                self.assertLessEqual(len(modal.children), 5)
                self.assertTrue(all(len(item.label) <= 45 for item in modal.children))
                self.assertTrue(
                    all(item.placeholder is None or len(item.placeholder) <= 100 for item in modal.children)
                )


if __name__ == "__main__":
    unittest.main()
