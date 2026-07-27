import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import discord

import storage
from modules.craft import CRAFT_CHANNEL_ID
from modules.control_center import (
    CHANNEL_SPECS,
    FINANCE_LOG_CHANNEL_ID,
    WORKSHOP_CHANNEL_ID,
    BotTestView,
    SettingsPanelView,
    edit_message_with_retry,
    is_transient_discord_error,
    log_technical_event,
    majestic_test_embed,
    message_payload_matches,
)
from modules.majestic_api import MajesticMarketplaceSummary
from modules.finance import FINANCE_COMMAND_CHANNEL_ID, FINANCE_DAILY_CHANNEL_ID
from modules.operations import ACTIVE_TASKS_CHANNEL_ID, build_operations_embed
from modules.tvrs import TVRSPublicPanelView


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
        prompt = storage.finance_get_or_create_daily_prompt(
            guild_id=guild_id,
            report_date=report_date,
            channel_id=FINANCE_DAILY_CHANNEL_ID,
        )
        storage.finance_bind_daily_prompt_message(
            prompt_id=int(prompt["id"]),
            channel_id=FINANCE_DAILY_CHANNEL_ID,
            message_id=900,
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
        plan = storage.craft_create_plan(
            guild_id=guild_id,
            recipe_id=recipe["id"],
            channel_id=CRAFT_CHANNEL_ID,
            attempts_total=10,
            responsible_id=20,
            responsible_display="Ответственный",
            created_by_id=10,
            created_by_display="Автор",
        )
        storage.craft_bind_plan_message(
            plan_id=int(plan["id"]),
            channel_id=CRAFT_CHANNEL_ID,
            message_id=901,
            thread_id=902,
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
        self.assertNotIn("AI-аудио", rendered)
        self.assertIn("https://discord.com/channels/77", rendered)
        self.assertIn("Требует действия", embed.fields[0].name)
        self.assertLessEqual(len(embed), 6000)
        self.assertLessEqual(len(embed.fields), 25)

    def test_source_messages_stay_in_their_sections(self) -> None:
        self.assertEqual(FINANCE_DAILY_CHANNEL_ID, FINANCE_LOG_CHANNEL_ID)
        self.assertEqual(FINANCE_COMMAND_CHANNEL_ID, 0)
        self.assertEqual(CRAFT_CHANNEL_ID, WORKSHOP_CHANNEL_ID)
        self.assertNotEqual(FINANCE_DAILY_CHANNEL_ID, ACTIVE_TASKS_CHANNEL_ID)
        self.assertNotEqual(CRAFT_CHANNEL_ID, ACTIVE_TASKS_CHANNEL_ID)

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

    def test_service_channel_scaffolds_are_persistent(self) -> None:
        self.assertEqual(
            [spec.key for spec in CHANNEL_SPECS],
            [
                "control_panel",
                "music",
                "active_tasks",
                "workshop",
                "finance_log",
                "tech_log",
                "reports",
                "settings",
                "test",
            ],
        )

        async def inspect_views() -> None:
            for view in (SettingsPanelView(), BotTestView()):
                with self.subTest(view=type(view).__name__):
                    self.assertIsNone(view.timeout)
                    self.assertTrue(view.children)
                    self.assertTrue(all(item.custom_id for item in view.children))
            test_ids = {item.custom_id for item in BotTestView().children}
            self.assertIn("tmod_consensus_simulation", test_ids)
            settings_ids = {item.custom_id for item in SettingsPanelView().children}
            self.assertIn("tmod_settings_broadcasts", settings_ids)

        asyncio.run(inspect_views())

    def test_public_tvrs_panel_contains_private_market_entry(self) -> None:
        async def inspect_view() -> None:
            view = TVRSPublicPanelView()
            market_buttons = [
                item
                for item in view.children
                if item.custom_id == "tmod_public_tvrs_market"
            ]
            self.assertEqual(len(market_buttons), 1)
            self.assertEqual(market_buttons[0].label, "Рынок")

        asyncio.run(inspect_view())

    def test_majestic_test_embed_is_safe_and_compact(self) -> None:
        summary = MajesticMarketplaceSummary(
            category="items",
            server_id="RU15",
            server_name="Phoenix",
            record_count=1315,
            total_count=412243656,
            total_sold=497066243,
            overall_average_price=56984373,
            last_updated="2026-07-15T02:12:11.960Z",
            period_days=30,
        )
        embed = majestic_test_embed(
            summary,
            {"remaining_process_budget": 4, "requests_per_window": 5},
        )
        rendered = "\n".join(str(field.value) for field in embed.fields)
        self.assertIn("RU15", str(embed.description))
        self.assertIn("1 315", rendered)
        self.assertIn("4/5", rendered)
        self.assertLessEqual(len(embed), 6000)

    def test_technical_event_can_deliberately_ping_everyone(self) -> None:
        channel = SimpleNamespace(send=AsyncMock())
        guild = SimpleNamespace(id=987)

        async def send_notice() -> None:
            with patch(
                "modules.control_center.resolve_control_channel",
                new=AsyncMock(return_value=channel),
            ):
                await log_technical_event(
                    SimpleNamespace(),
                    guild,
                    title="Новый срез Majestic · RU15",
                    details="Каталог обновлён.",
                    level="info",
                    dedupe_key="market-snapshot:test",
                    cooldown_seconds=0,
                    mention_everyone=True,
                )

        asyncio.run(send_notice())
        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        self.assertEqual(kwargs["content"], "@everyone")
        self.assertTrue(kwargs["allowed_mentions"].everyone)
        self.assertFalse(kwargs["allowed_mentions"].users)
        self.assertFalse(kwargs["allowed_mentions"].roles)

    def test_unchanged_panel_ignores_timestamp_and_skips_edit(self) -> None:
        first = discord.Embed(
            title="Панель",
            description="Состояние не изменилось",
            timestamp=datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc),
        )
        second = discord.Embed(
            title="Панель",
            description="Состояние не изменилось",
            timestamp=datetime(2026, 7, 15, 18, 1, tzinfo=timezone.utc),
        )
        message = SimpleNamespace(content="", embeds=[first], components=[])

        self.assertTrue(message_payload_matches(message, embed=second))

        changed = discord.Embed(
            title="Панель",
            description="Теперь есть новая задача",
            timestamp=datetime(2026, 7, 15, 18, 1, tzinfo=timezone.utc),
        )
        self.assertFalse(message_payload_matches(message, embed=changed))

    def test_message_edit_retries_temporary_discord_503(self) -> None:
        response = SimpleNamespace(status=503, reason="Service Unavailable")
        temporary_error = discord.HTTPException(
            response,
            {"code": 0, "message": "upstream connect error"},
        )
        edited = SimpleNamespace(id=123)
        message = SimpleNamespace(edit=AsyncMock(side_effect=[temporary_error, edited]))

        async def retry_edit() -> object:
            with patch(
                "modules.control_center.asyncio.sleep", new=AsyncMock()
            ) as sleep:
                result = await edit_message_with_retry(
                    message, embed=discord.Embed(title="Панель")
                )
                sleep.assert_awaited_once_with(0.75)
                return result

        self.assertTrue(is_transient_discord_error(temporary_error))
        self.assertIs(asyncio.run(retry_edit()), edited)
        self.assertEqual(message.edit.await_count, 2)


if __name__ == "__main__":
    unittest.main()
