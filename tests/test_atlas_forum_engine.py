import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import storage
from modules.atlas_forum_engine import _match_complaint_characters, _matches_character
from persistence import atlas_forum_engine_repository as forum_engine


THREAD = "https://forum.majestic-rp.ru/threads/report.908/"


class AtlasForumEngineRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-forum-engine-test.db"
        storage.init_db()
        self.character = storage.add_profile_character(77, 42, "Phoenix Hero", "228392")
        self.characters = forum_engine.list_monitored_characters(77)

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def _listing(self, *, notify: bool, section: str = "open", fingerprint: str = "list-1"):
        return forum_engine.record_complaint_listing(
            guild_id=77,
            project_code="majestic-rp",
            server_code="phoenix-15",
            section_kind=section,
            thread_url=THREAD,
            title="Жалоба на 228392",
            author="Reporter",
            listing_fingerprint=fingerprint,
            locked=False,
            metadata={"reply_count": 0},
            notify=notify,
        )

    def _snapshot(self, complaint_id: int, *, fingerprint: str, posts: int, staff: bool):
        return forum_engine.record_complaint_snapshot(
            complaint_id,
            content_fingerprint=fingerprint,
            first_post_excerpt="Жалоба на игрока 228392 и описание нарушения.",
            latest_post_excerpt="Ответ по существу жалобы.",
            latest_post_author="Administrator" if staff else "Reporter",
            latest_post_role="Администратор" if staff else None,
            latest_post_at="2026-09-09T08:00:00+00:00",
            latest_post_is_staff=staff,
            post_count=posts,
            matched_characters=self.characters,
            notify=True,
        )

    def test_static_matching_uses_exact_numeric_boundaries(self) -> None:
        self.assertTrue(_matches_character("Игрок 228392 нарушил правило", "228392"))
        self.assertFalse(_matches_character("Игрок 12283920 нарушил правило", "228392"))

    def test_structured_complaint_matches_accused_not_reporter(self) -> None:
        reporter = {
            "character_id": 1,
            "user_id": 10,
            "nickname": "Reporter",
            "static_id": "316622",
        }
        accused = {
            "character_id": 2,
            "user_id": 20,
            "nickname": "Accused",
            "static_id": "270160",
        }

        matched = _match_complaint_characters(
            "Жалоба на сотрудника",
            "Ваш статический ID # 316622 Статический #ID нарушителя 270160",
            [reporter, accused],
        )

        self.assertEqual([item["user_id"] for item in matched], [20])

    def test_baseline_hydration_arms_future_alerts_without_old_discovery_dm(self) -> None:
        complaint = self._listing(notify=False)["complaint"]
        first = self._snapshot(int(complaint["id"]), fingerprint="body-1", posts=1, staff=False)

        self.assertTrue(first["first_hydration"])
        self.assertEqual(forum_engine.pending_complaint_deliveries(), [])

        changed = self._snapshot(int(complaint["id"]), fingerprint="body-2", posts=2, staff=True)
        pending = forum_engine.pending_complaint_deliveries()
        self.assertEqual(changed["event_kind"], "staff_reply")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["user_id"], 42)

    def test_new_post_baseline_complaint_notifies_on_first_hydration(self) -> None:
        complaint = self._listing(notify=True)["complaint"]
        result = self._snapshot(int(complaint["id"]), fingerprint="body-new", posts=1, staff=False)

        self.assertEqual(result["event_kind"], "discovered")
        self.assertEqual(len(forum_engine.pending_complaint_deliveries()), 1)

    def test_status_move_creates_one_deduplicated_delivery(self) -> None:
        complaint = self._listing(notify=True)["complaint"]
        self._snapshot(int(complaint["id"]), fingerprint="body-1", posts=1, staff=False)
        first = self._listing(notify=True, section="accepted", fingerprint="list-2")
        repeated = self._listing(notify=True, section="accepted", fingerprint="list-2")

        pending = forum_engine.pending_complaint_deliveries()
        self.assertTrue(first["section_changed"])
        self.assertFalse(repeated["section_changed"])
        self.assertEqual(sum(item["event_kind"] == "status_changed" for item in pending), 1)

    def test_new_character_is_linked_to_cached_complaint_without_fake_event(self) -> None:
        complaint = self._listing(notify=False)["complaint"]
        forum_engine.record_complaint_snapshot(
            int(complaint["id"]),
            content_fingerprint="body-1",
            first_post_excerpt="Описание жалобы на 999001.",
            latest_post_excerpt="Описание жалобы на 999001.",
            latest_post_author="Reporter",
            latest_post_role=None,
            latest_post_at=None,
            latest_post_is_staff=False,
            post_count=1,
            matched_characters=[],
            notify=False,
        )
        new_character = storage.add_profile_character(77, 99, "Late Hero", "999001")
        linked = forum_engine.reconcile_complaint_subjects(
            int(complaint["id"]),
            [{
                "character_id": new_character.id,
                "user_id": 99,
                "nickname": new_character.nickname,
                "static_id": new_character.static_id,
            }],
        )

        self.assertGreaterEqual(linked, 1)
        self.assertEqual(len(forum_engine.user_forum_complaints(77, 99)), 1)
        self.assertEqual(forum_engine.pending_complaint_deliveries(), [])

    def test_stale_running_feed_is_reclaimed_after_interrupted_process(self) -> None:
        feed = forum_engine.ensure_default_monitor_feeds(77)[0]
        stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with storage.connect() as con:
            con.execute(
                "UPDATE atlas_forum_monitor_feeds SET status = 'running', last_started_at = ?, next_scan_at = NULL WHERE id = ?",
                (stale, int(feed["id"])),
            )
            con.commit()

        due = forum_engine.due_monitor_feeds(77)
        claimed = forum_engine.claim_monitor_feed(int(feed["id"]))
        duplicate = forum_engine.claim_monitor_feed(int(feed["id"]))

        self.assertIn(int(feed["id"]), [int(item["id"]) for item in due])
        self.assertIsNotNone(claimed)
        self.assertIsNone(duplicate)

    def test_startup_releases_running_leases_from_previous_process(self) -> None:
        feeds = forum_engine.ensure_default_monitor_feeds(77)
        for feed in feeds[:2]:
            self.assertIsNotNone(forum_engine.claim_monitor_feed(int(feed["id"])))

        recovered = forum_engine.recover_interrupted_monitor_feeds(77)
        status = forum_engine.forum_monitor_status(77)

        self.assertEqual(recovered, 2)
        self.assertNotIn("running", {item["status"] for item in status["feeds"]})
        self.assertEqual(
            {int(item["id"]) for item in forum_engine.due_monitor_feeds(77)},
            {int(item["id"]) for item in feeds},
        )

    def test_default_feeds_keep_hot_monitoring_inside_one_minute(self) -> None:
        feeds = forum_engine.ensure_default_monitor_feeds(77)
        self.assertEqual({int(item["interval_seconds"]) for item in feeds}, {45})
        self.assertEqual({int(item["hot_pages"]) for item in feeds}, {1})

        claimed = forum_engine.claim_monitor_feed(int(feeds[0]["id"]))
        self.assertIsNotNone(claimed)
        finished = forum_engine.finish_monitor_feed(
            int(feeds[0]["id"]),
            stats={"mode": "hot"},
            baseline_completed=True,
        )
        delay = datetime.fromisoformat(str(finished["next_scan_at"])) - datetime.now(timezone.utc)
        self.assertLessEqual(delay.total_seconds(), 46)
        self.assertTrue(finished["baseline_completed_at"])


if __name__ == "__main__":
    unittest.main()
