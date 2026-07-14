import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import storage
from modules.craft import CRAFT_CHANNEL_ID
from modules.finance import FINANCE_COMMAND_CHANNEL_ID, FINANCE_DAILY_CHANNEL_ID
from modules.operations import ACTIVE_TASKS_CHANNEL_ID, build_operations_embed


class OperationsCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.old_activity_file = storage.LEGACY_ACTIVITY_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "operations-test.db"
        storage.LEGACY_ACTIVITY_FILE = storage.DATA_DIR / "activity.json"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        storage.LEGACY_ACTIVITY_FILE = self.old_activity_file
        self.temp_dir.cleanup()

    def test_dashboard_separates_attention_and_running_work(self) -> None:
        guild_id = 77
        report_date = "2026-07-15"
        storage.finance_get_or_create_daily_prompt(
            guild_id=guild_id,
            report_date=report_date,
            channel_id=ACTIVE_TASKS_CHANNEL_ID,
        )
        recipe = storage.craft_create_recipe(
            guild_id=guild_id,
            product_name="Промышленные металлы",
            treasury_cost_per_unit=1_000,
            duration_minutes_per_unit=10,
            max_batch_size=10,
            materials=[("Железная руда", 50)],
            created_by_id=10,
            created_by_display="Автор",
        )
        storage.craft_create_plan(
            guild_id=guild_id,
            recipe_id=recipe["id"],
            channel_id=ACTIVE_TASKS_CHANNEL_ID,
            attempts_total=10,
            responsible_id=20,
            responsible_display="Ответственный",
            created_by_id=10,
            created_by_display="Автор",
        )
        storage.create_audio_generation(
            guild_id=guild_id,
            channel_id=100,
            user_id=10,
            user_display="Автор",
            prompt="Тест",
            model="test-model",
        )

        embed = build_operations_embed(
            SimpleNamespace(id=guild_id),
            local_now=datetime(2026, 7, 15, 18, 5, tzinfo=ZoneInfo("Europe/Riga")),
            consensus=None,
        )
        rendered = "\n".join(str(field.value) for field in embed.fields)
        self.assertIn("Ежедневная сверка казны", rendered)
        self.assertIn("Крафт #", rendered)
        self.assertIn("AI-аудио", rendered)
        self.assertIn("Требует действия", embed.fields[0].name)
        self.assertLessEqual(len(embed), 6000)
        self.assertLessEqual(len(embed.fields), 25)

    def test_all_public_work_routes_to_active_tasks(self) -> None:
        self.assertEqual(FINANCE_DAILY_CHANNEL_ID, ACTIVE_TASKS_CHANNEL_ID)
        self.assertEqual(FINANCE_COMMAND_CHANNEL_ID, ACTIVE_TASKS_CHANNEL_ID)
        self.assertEqual(CRAFT_CHANNEL_ID, ACTIVE_TASKS_CHANNEL_ID)

    def test_paused_consensus_is_attention_not_history(self) -> None:
        embed = build_operations_embed(
            SimpleNamespace(id=77),
            local_now=datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("Europe/Riga")),
            consensus={
                "stage": "paused",
                "stage_label": "пауза",
                "plenary_number": 4,
                "leader_id": 10,
                "current_bill": {"bill_number": 12, "title": "Тестовый проект"},
            },
        )
        self.assertIn("4-й консенсус", str(embed.fields[0].value))
        self.assertIn("пауза", str(embed.fields[0].value))


if __name__ == "__main__":
    unittest.main()
