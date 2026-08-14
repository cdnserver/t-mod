import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import storage
from modules.consensus_schedule import (
    parse_schedule_time,
    public_schedule_payload,
    sync_schedule_discord_event,
)
from persistence import consensus_schedule_repository as repository


class ConsensusScheduleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "schedule-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def _create(self, **overrides):
        values = {
            "guild_id": 77,
            "plenary_number": 9,
            "title": "Девятый пленарный консенсус",
            "description": "Рассмотрение очереди законопроектов.",
            "invitation_text": "Просим прибыть заранее.",
            "scheduled_for": datetime.now(timezone.utc) + timedelta(days=2),
            "duration_minutes": 90,
            "voice_channel_id": 88,
            "actor_id": 42,
            "actor_display": "Председатель",
        }
        values.update(overrides)
        return repository.save_consensus_schedule(**values)

    def test_schedule_is_revisioned_and_only_one_can_be_upcoming(self) -> None:
        created = self._create()
        self.assertEqual(created["status"], "scheduled")
        self.assertEqual(created["revision"], 1)
        with self.assertRaisesRegex(ValueError, "consensus_schedule_already_exists"):
            self._create(title="Другой план")

        updated = self._create(
            schedule_id=created["id"],
            expected_revision=created["revision"],
            title="Обновлённый девятый консенсус",
            duration_minutes=120,
        )
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["duration_minutes"], 120)
        with self.assertRaisesRegex(ValueError, "consensus_schedule_conflict"):
            self._create(
                schedule_id=created["id"],
                expected_revision=created["revision"],
            )

    def test_reschedule_keeps_original_time_and_exposes_the_shift(self) -> None:
        initial_time = datetime.now(timezone.utc) + timedelta(days=2)
        created = self._create(scheduled_for=initial_time)
        moved = self._create(
            schedule_id=created["id"],
            expected_revision=created["revision"],
            scheduled_for=initial_time + timedelta(minutes=45),
        )

        self.assertEqual(moved["initial_scheduled_for"], created["scheduled_for"])
        self.assertEqual(moved["time_shift_minutes"], 45)
        self.assertIsNotNone(moved["last_rescheduled_at"])
        payload = public_schedule_payload(moved)
        self.assertEqual(payload["time_shift_minutes"], 45)
        self.assertEqual(payload["initial_scheduled_for"], created["scheduled_for"])

    def test_cancel_does_not_delete_history_and_allows_next_plan(self) -> None:
        created = self._create()
        cancelled = repository.cancel_consensus_schedule(
            created["id"],
            guild_id=77,
            expected_revision=created["revision"],
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(repository.get_upcoming_consensus_schedule(77))

        replacement = self._create(title="Новый план после отмены")
        history = repository.list_consensus_schedules(77)
        self.assertEqual(replacement["status"], "scheduled")
        self.assertEqual({item["status"] for item in history}, {"scheduled", "cancelled"})

    def test_start_binds_the_exact_live_session(self) -> None:
        created = self._create()
        started = repository.start_consensus_schedule(
            77,
            session_key="guild:v3:session",
            schedule_id=created["id"],
        )

        self.assertIsNotNone(started)
        self.assertEqual(started["id"], created["id"])
        self.assertEqual(started["status"], "started")
        self.assertEqual(started["started_session_key"], "guild:v3:session")
        self.assertIsNone(repository.get_upcoming_consensus_schedule(77))

    def test_start_refuses_a_stale_or_replaced_schedule(self) -> None:
        created = self._create()
        with self.assertRaisesRegex(ValueError, "consensus_schedule_not_active"):
            repository.start_consensus_schedule(
                77,
                session_key="guild:v3:stale",
                schedule_id=int(created["id"]) + 999,
            )
        self.assertEqual(repository.get_upcoming_consensus_schedule(77)["id"], created["id"])

    def test_local_time_parser_rejects_past_and_far_future_values(self) -> None:
        now = datetime(2026, 8, 8, 10, 0, tzinfo=timezone.utc)
        parsed = parse_schedule_time("09.08.2026 14:30", now=now)
        self.assertGreater(parsed, now)
        with self.assertRaisesRegex(ValueError, "consensus_schedule_time_past"):
            parse_schedule_time("08.08.2026 10:00", now=now)
        with self.assertRaisesRegex(ValueError, "consensus_schedule_time_too_far"):
            parse_schedule_time("09.08.2028 10:00", now=now)

    async def test_discord_event_is_created_once_and_bound_to_plan(self) -> None:
        created = self._create()
        event = SimpleNamespace(id=1234)
        guild = SimpleNamespace(
            get_channel=Mock(return_value=object()),
            get_scheduled_event=Mock(return_value=None),
            create_scheduled_event=AsyncMock(return_value=event),
        )

        synced = await sync_schedule_discord_event(guild, created)

        self.assertEqual(synced["discord_event_id"], 1234)
        guild.create_scheduled_event.assert_awaited_once()
        payload = public_schedule_payload(synced)
        self.assertEqual(payload["event_url"], "https://discord.com/events/77/1234")

    async def test_discord_event_fetches_uncached_stage_channel(self) -> None:
        created = self._create()
        channel = SimpleNamespace(type=discord.ChannelType.stage_voice)
        event = SimpleNamespace(id=2234)
        guild = SimpleNamespace(
            me=None,
            get_channel=Mock(return_value=None),
            fetch_channel=AsyncMock(return_value=channel),
            get_scheduled_event=Mock(return_value=None),
            create_scheduled_event=AsyncMock(return_value=event),
        )

        await sync_schedule_discord_event(guild, created)

        guild.fetch_channel.assert_awaited_once_with(88)
        kwargs = guild.create_scheduled_event.await_args.kwargs
        self.assertEqual(kwargs["entity_type"], discord.EntityType.stage_instance)
        self.assertIs(kwargs["channel"], channel)

    async def test_discord_event_rejects_missing_event_permission(self) -> None:
        created = self._create()
        permissions = SimpleNamespace(
            administrator=False,
            create_events=False,
            manage_events=False,
        )
        channel = SimpleNamespace(
            type=discord.ChannelType.voice,
            permissions_for=Mock(return_value=permissions),
        )
        guild = SimpleNamespace(
            me=object(),
            get_channel=Mock(return_value=channel),
            get_scheduled_event=Mock(return_value=None),
            create_scheduled_event=AsyncMock(),
        )

        with self.assertRaisesRegex(
            ValueError,
            "consensus_schedule_event_permission_missing",
        ):
            await sync_schedule_discord_event(guild, created)

        guild.create_scheduled_event.assert_not_awaited()

    async def test_closed_discord_event_is_recreated(self) -> None:
        created = self._create()
        closed_event = SimpleNamespace(
            id=1234,
            status=discord.EventStatus.completed,
            edit=AsyncMock(),
        )
        replacement = SimpleNamespace(id=3234)
        guild = SimpleNamespace(
            me=None,
            get_channel=Mock(return_value=object()),
            get_scheduled_event=Mock(return_value=closed_event),
            create_scheduled_event=AsyncMock(return_value=replacement),
        )

        synced = await sync_schedule_discord_event(guild, created | {"discord_event_id": 1234})

        self.assertEqual(synced["discord_event_id"], 3234)
        closed_event.edit.assert_not_awaited()
        guild.create_scheduled_event.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
