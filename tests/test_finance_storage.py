import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import storage


class FinanceStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        # SQLite WAL handles can be released a moment late by Windows worker threads.
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "finance-test.db"
        storage.LEGACY_ACTIVITY_FILE = storage.DATA_DIR / "activity.json"
        storage.init_db()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def snapshot(
        self,
        amount: int,
        *,
        kind: str = "interim",
        prompt_id: int | None = None,
        actor_id: int = 10,
    ):
        return storage.finance_record_snapshot(
            guild_id=1,
            event_kind=kind,
            amount=amount,
            actor_id=actor_id,
            actor_display="Tester",
            channel_id=100,
            message_id=200,
            log_channel_id=300,
            admin_user_id=400,
            report_date="2026-07-13",
            prompt_id=prompt_id,
        )

    def movement(self, kind: str, amount: int, *, actor_id: int = 10):
        return storage.finance_record_movement(
            guild_id=1,
            event_kind=kind,
            amount=amount,
            reason="Подробная тестовая причина операции",
            captcha_digest="digest",
            game_code="ABCD",
            actor_id=actor_id,
            actor_display="Tester",
            channel_id=100,
            message_id=None,
            log_channel_id=300,
            admin_user_id=400,
        )

    def undo(self, target_actor_id: int, *, undone_by_id: int = 10):
        return storage.finance_undo_last_action(
            guild_id=1,
            target_actor_id=target_actor_id,
            undone_by_id=undone_by_id,
            undone_by_display="Undo Tester",
            channel_id=100,
            message_id=None,
            log_channel_id=300,
            admin_user_id=400,
        )

    def test_daily_prompt_is_idempotent_and_first_submission_wins(self) -> None:
        prompt_a = storage.finance_get_or_create_daily_prompt(
            guild_id=1, report_date="2026-07-13", channel_id=300
        )
        prompt_b = storage.finance_get_or_create_daily_prompt(
            guild_id=1, report_date="2026-07-13", channel_id=300
        )
        self.assertEqual(prompt_a["id"], prompt_b["id"])

        first = self.snapshot(1_000_000, kind="daily", prompt_id=prompt_a["id"])
        second = self.snapshot(2_000_000, kind="daily", prompt_id=prompt_a["id"])
        self.assertFalse(first["already_recorded"])
        self.assertTrue(second["already_recorded"])
        self.assertEqual(second["amount"], 1_000_000)

    def test_movement_arithmetic_and_interim_reset(self) -> None:
        self.snapshot(1_000_000)
        deposit = self.movement("deposit", 250_000)
        withdrawal = self.movement("withdraw", 100_000)
        self.assertEqual(deposit["balance_after"], 1_250_000)
        self.assertEqual(withdrawal["balance_after"], 1_150_000)

        self.snapshot(900_000)
        state = storage.finance_get_latest_state(1)
        self.assertEqual(state["estimated_balance"], 900_000)
        self.assertEqual(state["movements_after_report"], 0)

    def test_movements_before_first_report_remain_auditable(self) -> None:
        event = self.movement("deposit", 50_000)
        self.assertIsNone(event["balance_before"])
        self.assertIsNone(event["balance_after"])
        state = storage.finance_get_latest_state(1)
        self.assertIsNone(state["estimated_balance"])

    def test_concurrent_movements_do_not_lose_updates(self) -> None:
        self.snapshot(1_000_000)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.movement("deposit", 1_000), range(20)))
        state = storage.finance_get_latest_state(1)
        self.assertEqual(state["estimated_balance"], 1_020_000)
        self.assertEqual(state["movements_after_report"], 20)

    def test_each_event_enqueues_channel_and_admin_notifications(self) -> None:
        event = self.snapshot(123_000)
        pending = storage.finance_pending_notifications(10)
        matching = [item for item in pending if item["event_id"] == event["id"]]
        self.assertEqual({item["destination_kind"] for item in matching}, {"log_channel", "admin_dm"})

    def test_public_event_log_can_be_disabled_without_disabling_admin_dm(self) -> None:
        event = storage.finance_record_snapshot(
            guild_id=1,
            event_kind="interim",
            amount=123_000,
            actor_id=10,
            actor_display="Tester",
            channel_id=100,
            message_id=200,
            log_channel_id=0,
            admin_user_id=400,
            report_date="2026-07-13",
        )
        pending = storage.finance_pending_notifications(10)
        matching = [item for item in pending if item["event_id"] == event["id"]]
        self.assertEqual({item["destination_kind"] for item in matching}, {"admin_dm"})

    def test_operations_count_only_recent_running_audio(self) -> None:
        record_id = storage.create_audio_generation(
            guild_id=1,
            channel_id=100,
            user_id=10,
            user_display="Tester",
            prompt="Тест",
            model="test-model",
        )
        self.assertEqual(storage.count_recent_running_audio_generations(1), 1)
        storage.update_audio_generation(record_id, status="completed", completed_at=storage.utc_now_iso())
        self.assertEqual(storage.count_recent_running_audio_generations(1), 0)

    def test_undo_reverses_latest_user_movement_without_deleting_history(self) -> None:
        self.snapshot(1_000_000, actor_id=99)
        movement = self.movement("deposit", 250_000, actor_id=10)
        result = self.undo(10)
        self.assertIsNotNone(result)
        self.assertEqual(result["reversed_event"]["id"], movement["id"])
        self.assertEqual(result["undo_event"]["event_kind"], "undo")
        self.assertEqual(result["undo_event"]["balance_after"], 1_000_000)
        self.assertIsNotNone(storage.finance_get_event(movement["id"]))
        self.assertEqual(storage.finance_get_latest_state(1)["estimated_balance"], 1_000_000)

    def test_undo_old_movement_does_not_change_a_later_exact_report(self) -> None:
        self.snapshot(1_000_000, actor_id=99)
        self.movement("deposit", 250_000, actor_id=10)
        self.snapshot(900_000, actor_id=99)
        result = self.undo(10, undone_by_id=99)
        self.assertIsNotNone(result)
        self.assertEqual(result["undo_event"]["balance_before"], 900_000)
        self.assertEqual(result["undo_event"]["balance_after"], 900_000)
        self.assertEqual(storage.finance_get_latest_state(1)["estimated_balance"], 900_000)

    def test_undo_daily_report_reopens_its_button_prompt(self) -> None:
        prompt = storage.finance_get_or_create_daily_prompt(
            guild_id=1, report_date="2026-07-13", channel_id=300
        )
        self.snapshot(700_000, kind="daily", prompt_id=prompt["id"], actor_id=10)
        result = self.undo(10)
        self.assertIsNotNone(result)
        reopened = storage.finance_get_daily_prompt(prompt["id"])
        self.assertEqual(reopened["status"], "open")
        self.assertIsNone(reopened["event_id"])
        self.assertIsNone(storage.finance_get_latest_state(1)["latest_report"])
        replacement = self.snapshot(710_000, kind="daily", prompt_id=prompt["id"], actor_id=11)
        self.assertFalse(replacement["already_recorded"])
        self.assertEqual(storage.finance_get_latest_state(1)["estimated_balance"], 710_000)


if __name__ == "__main__":
    unittest.main()
