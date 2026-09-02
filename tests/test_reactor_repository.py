import tempfile
import unittest
import sqlite3
import inspect
from pathlib import Path

import storage
from persistence import reactor_repository as reactor
from persistence.core import connect, connect_readonly, utc_now_iso


class ReactorRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "reactor-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_layout_is_validated_and_persisted_per_surface(self) -> None:
        self.assertEqual(
            reactor.reactor_get_layout(1, 2, "admin"),
            list(reactor.ADMIN_WIDGETS),
        )
        saved = reactor.reactor_set_layout(
            1,
            2,
            "admin",
            ["minecraft", "attention", "minecraft", "not-a-widget"],
        )
        self.assertEqual(saved, ["minecraft", "attention"])
        self.assertEqual(reactor.reactor_get_layout(1, 2, "admin"), saved)
        self.assertEqual(
            reactor.reactor_get_layout(1, 2, "member"),
            list(reactor.MEMBER_WIDGETS),
        )
        reactor.reactor_set_layout(1, 3, "member", ["identity", "consensus"])
        # Existing custom canvases automatically gain newly deployed personal
        # surfaces instead of hiding them forever.
        self.assertIn("preparation", reactor.reactor_get_layout(1, 3, "member"))

    def test_notifications_are_deduplicated_unread_and_markable(self) -> None:
        first = reactor.reactor_put_notification(
            guild_id=1,
            user_id=2,
            severity="warning",
            kind="attention",
            title="Нужна проверка",
            body="Очередь ожидает администратора.",
            route="#/system",
            dedupe_key="outbox",
        )
        same = reactor.reactor_put_notification(
            guild_id=1,
            user_id=2,
            severity="warning",
            kind="attention",
            title="Нужна проверка",
            body="Очередь ожидает администратора.",
            route="#/system",
            dedupe_key="outbox",
        )
        self.assertEqual(first, same)
        inbox = reactor.reactor_list_notifications(1, 2)
        self.assertEqual(inbox["unread"], 1)
        self.assertEqual(len(inbox["items"]), 1)
        self.assertEqual(
            reactor.reactor_mark_notifications_read(1, 2, ["bad", first]),
            1,
        )
        self.assertEqual(reactor.reactor_list_notifications(1, 2)["unread"], 0)

    def test_notification_projection_is_synchronized_in_one_batch(self) -> None:
        self.assertEqual(
            reactor.reactor_sync_notifications(
                1,
                2,
                "attention",
                [
                    {
                        "severity": "warning",
                        "title": "Первая задача",
                        "body": "Нужна проверка.",
                        "route": "#/attention/one",
                        "dedupe_key": "one",
                    },
                    {
                        "severity": "critical",
                        "title": "Вторая задача",
                        "body": "Нужно решение.",
                        "route": "#/attention/two",
                        "dedupe_key": "two",
                    },
                ],
            ),
            2,
        )
        self.assertEqual(reactor.reactor_list_notifications(1, 2)["unread"], 2)
        reactor.reactor_sync_notifications(
            1,
            2,
            "attention",
            [
                {
                    "severity": "critical",
                    "title": "Вторая задача",
                    "body": "Нужно решение.",
                    "dedupe_key": "two",
                }
            ],
        )
        inbox = reactor.reactor_list_notifications(1, 2)
        self.assertEqual(inbox["unread"], 1)
        self.assertEqual(inbox["items"][0]["title"], "Вторая задача")

    def test_read_only_connection_rejects_writes(self) -> None:
        with connect_readonly() as con:
            self.assertEqual(con.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("DELETE FROM members")

    def test_live_database_health_never_runs_full_integrity_scan(self) -> None:
        health = reactor.reactor_database_health()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["journal_mode"].lower(), "wal")
        self.assertNotIn("quick_check", inspect.getsource(reactor.reactor_database_health))

        reactor.reactor_put_notification(
            guild_id=1,
            user_id=2,
            severity="critical",
            kind="attention",
            title="Состояние ухудшилось",
            body="Очередь исчерпала попытки.",
            route="#/system",
            dedupe_key="outbox",
        )
        self.assertEqual(reactor.reactor_list_notifications(1, 2)["unread"], 1)
        self.assertEqual(
            reactor.reactor_resolve_notifications(1, 2, "attention", []),
            1,
        )
        self.assertEqual(reactor.reactor_list_notifications(1, 2)["unread"], 0)

    def test_event_feed_and_global_search_return_stable_routes(self) -> None:
        now = utc_now_iso()
        with connect() as con:
            con.execute(
                """
                INSERT INTO members(
                    guild_id, user_id, display_name, name, mention,
                    is_bot, created_at, updated_at
                ) VALUES(1, 42, 'Зигмунд Правосудов', 'zigmund', '<@42>', 0, ?, ?)
                """,
                (now, now),
            )
            con.execute(
                """
                INSERT INTO market_items(
                    server_id, category, item_id, external_id, item_name,
                    normalized_name, metadata_json, source_updated_at,
                    fetched_at, active, created_at, updated_at
                ) VALUES('RU15', 'items', 7, 'iron', 'Железо', 'железо', '{}', ?, ?, 1, ?, ?)
                """,
                (now, now, now, now),
            )
            con.commit()
        action_id = storage.bot_record_action(
            guild_id=1,
            actor_id=42,
            actor_display="Зигмунд Правосудов",
            module="reactor",
            action_kind="test_event",
            target_type="system",
            target_id="1",
            summary="Проверка живой ленты",
        )
        feed = reactor.reactor_event_feed(1)
        self.assertEqual(feed[-1]["id"], action_id)
        self.assertEqual(reactor.reactor_event_feed(1, after_id=action_id), [])

        member_results = reactor.reactor_global_search(1, "зигмунд")
        self.assertEqual(member_results[0]["route"], "#/members/member/42")
        market_results = reactor.reactor_global_search(1, "железо")
        self.assertTrue(
            any(item["route"] == "#/market/market/RU15.items.7" for item in market_results)
        )


if __name__ == "__main__":
    unittest.main()
