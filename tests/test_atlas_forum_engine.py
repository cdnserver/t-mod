import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import storage
from modules.atlas_forum_engine import (
    AtlasForumEngineConfig,
    AtlasForumEngineRunner,
    _complaint_participant_statics,
    _match_complaint_characters,
    _matches_character,
)
from modules.atlas_forum_sync import AtlasForumInventory, AtlasForumListingEntry
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

    def test_structured_complaint_extracts_reporter_and_target_profiles(self) -> None:
        participants = _complaint_participant_statics(
            "Жалоба на сотрудника",
            "Ваш статический ID # 316622 Статический #ID нарушителя 270160",
        )

        self.assertEqual(participants["reporter"], {"316622"})
        self.assertEqual(participants["target"], {"270160"})

    def test_short_static_is_not_guessed_from_legacy_timecode(self) -> None:
        matched = _match_complaint_characters(
            "Sheldon-0029",
            "Видео нарушения начинается на таймкоде 0:12.",
            [{"user_id": 10, "character_id": 1, "static_id": "12"}],
        )

        self.assertEqual(matched, [])

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

    def test_subject_reconciliation_removes_stale_false_match(self) -> None:
        complaint = self._listing(notify=False)["complaint"]
        forum_engine.reconcile_complaint_subjects(
            int(complaint["id"]), self.characters
        )
        self.assertEqual(len(forum_engine.user_forum_complaints(77, 42)), 1)

        changed = forum_engine.reconcile_complaint_subjects(int(complaint["id"]), [])

        self.assertGreaterEqual(changed, 1)
        self.assertEqual(forum_engine.user_forum_complaints(77, 42), [])

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
        self.assertEqual({int(item["interval_seconds"]) for item in feeds}, {30})
        self.assertEqual({int(item["hot_pages"]) for item in feeds}, {1})
        self.assertTrue(all(item["backfill_cursor_url"] for item in feeds))

        claimed = forum_engine.claim_monitor_feed(int(feeds[0]["id"]))
        self.assertIsNotNone(claimed)
        finished = forum_engine.finish_monitor_feed(
            int(feeds[0]["id"]),
            stats={"mode": "hot"},
            baseline_completed=True,
        )
        delay = datetime.fromisoformat(str(finished["next_scan_at"])) - datetime.now(timezone.utc)
        self.assertLessEqual(delay.total_seconds(), 31)
        self.assertTrue(finished["baseline_completed_at"])

    def test_repeated_forum_failure_backs_off_then_success_restores_hot_lane(self) -> None:
        feed = forum_engine.ensure_default_monitor_feeds(77)[0]
        delays = []
        for _ in range(6):
            finished = forum_engine.finish_monitor_feed(
                int(feed["id"]),
                stats={"phase": "forum_read"},
                error="atlas_forum_page_failed:WebDriverException",
                attention=True,
            )
            delays.append(
                (
                    datetime.fromisoformat(str(finished["next_scan_at"]))
                    - datetime.now(timezone.utc)
                ).total_seconds()
            )

        self.assertGreater(delays[-1], delays[0])
        self.assertLessEqual(delays[-1], 901)
        recovered = forum_engine.finish_monitor_feed(
            int(feed["id"]), stats={"mode": "hot"}
        )
        recovered_delay = (
            datetime.fromisoformat(str(recovered["next_scan_at"]))
            - datetime.now(timezone.utc)
        ).total_seconds()
        self.assertLessEqual(recovered_delay, 31)
        self.assertEqual(recovered["failure_count"], 0)

    def test_archive_cursor_progress_is_durable_and_bounded(self) -> None:
        feed = forum_engine.ensure_default_monitor_feeds(77)[0]
        first = forum_engine.advance_monitor_backfill(
            int(feed["id"]),
            next_url=f"{feed['root_url']}page-9",
            pages_scanned=8,
            topics_seen=160,
        )
        complete = forum_engine.advance_monitor_backfill(
            int(feed["id"]),
            next_url=None,
            pages_scanned=3,
            topics_seen=41,
        )

        self.assertEqual(first["backfill_pages_scanned"], 8)
        self.assertEqual(complete["backfill_pages_scanned"], 11)
        self.assertEqual(complete["backfill_topics_seen"], 201)
        self.assertIsNotNone(complete["backfill_completed_at"])

    def test_snapshot_preserves_every_forum_post(self) -> None:
        complaint = self._listing(notify=False)["complaint"]
        forum_engine.record_complaint_snapshot(
            int(complaint["id"]),
            content_fingerprint="full-thread",
            first_post_excerpt="Жалоба на игрока 228392.",
            latest_post_excerpt="Вердикт администратора.",
            latest_post_author="Administrator",
            latest_post_role="Администратор",
            latest_post_at="2026-09-09T08:00:00+00:00",
            latest_post_is_staff=True,
            post_count=2,
            matched_characters=self.characters,
            notify=False,
            posts=(
                {"index": 1, "author": "Reporter", "content": "Полная жалоба."},
                {
                    "index": 2,
                    "author": "Administrator",
                    "author_role": "Администратор",
                    "content": "Полный текст вердикта.",
                    "is_staff": True,
                },
            ),
        )

        with storage.connect_readonly() as con:
            rows = con.execute(
                "SELECT post_index, content, is_staff FROM atlas_forum_complaint_posts ORDER BY post_index"
            ).fetchall()
        self.assertEqual([row["content"] for row in rows], ["Полная жалоба.", "Полный текст вердикта."])
        self.assertEqual(int(rows[-1]["is_staff"]), 1)

    def test_static_profile_counts_filed_and_received_complaints(self) -> None:
        received = self._listing(notify=False)["complaint"]
        forum_engine.record_complaint_snapshot(
            int(received["id"]),
            content_fingerprint="participants-1",
            first_post_excerpt="Жалоба",
            latest_post_excerpt="Жалоба",
            latest_post_author="Reporter",
            latest_post_role=None,
            latest_post_at=None,
            latest_post_is_staff=False,
            post_count=1,
            matched_characters=[],
            notify=False,
            participant_statics={"reporter": {"111111"}, "target": {"228392"}},
        )
        filed = forum_engine.record_complaint_listing(
            guild_id=77,
            project_code="majestic-rp",
            server_code="phoenix-15",
            section_kind="accepted",
            thread_url="https://forum.majestic-rp.ru/threads/second.909/",
            title="Жалоба",
            author="Hero",
            listing_fingerprint="second",
            locked=True,
            metadata={},
            notify=False,
        )["complaint"]
        forum_engine.record_complaint_snapshot(
            int(filed["id"]),
            content_fingerprint="participants-2",
            first_post_excerpt="Жалоба",
            latest_post_excerpt="Рассмотрено",
            latest_post_author="Administrator",
            latest_post_role="Администратор",
            latest_post_at=None,
            latest_post_is_staff=True,
            post_count=2,
            matched_characters=[],
            notify=False,
            participant_statics={"reporter": {"228392"}, "target": {"999999"}},
        )

        profile = forum_engine.forum_static_profile(77, "228392")

        self.assertEqual(profile["filed"], 1)
        self.assertEqual(profile["received"], 1)
        self.assertEqual(len(profile["items"]), 2)

    def test_unreadable_topic_is_backed_off_without_blocking_archive(self) -> None:
        complaint = self._listing(notify=False)["complaint"]
        changed = forum_engine.mark_complaint_hydration_failed(
            77,
            [str(complaint["thread_url"])],
            error="temporary_read_failure",
        )

        self.assertEqual(changed, 1)
        self.assertNotIn(
            str(complaint["thread_url"]),
            forum_engine.complaints_needing_hydration(77),
        )


class AtlasForumEngineRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-forum-runner-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    async def test_archive_pass_reuses_hot_page_and_never_uses_full_page_limit(self) -> None:
        feeds = forum_engine.ensure_default_monitor_feeds(77)
        feed = feeds[0]
        now = datetime.now(timezone.utc).isoformat()
        with storage.connect() as con:
            con.execute(
                "UPDATE atlas_forum_monitor_feeds SET baseline_completed_at = ? WHERE id = ?",
                (now, int(feed["id"])),
            )
            con.commit()
        hot = AtlasForumListingEntry(
            url="https://forum.majestic-rp.ru/threads/fresh.901/",
            title="Жалоба на 228392",
        )
        old = AtlasForumListingEntry(
            url="https://forum.majestic-rp.ru/threads/old.801/",
            title="Архивная жалоба",
        )

        class FakeForum:
            def __init__(self) -> None:
                self.calls = []
                self.thread_urls = []

            async def fetch_inventory(self, url, *, max_pages):
                self.calls.append((url, max_pages))
                if url == str(feed["root_url"]):
                    return AtlasForumInventory(
                        entries=(hot,),
                        inventory_complete=False,
                        listing_pages=1,
                        next_url=f"{feed['root_url']}page-2",
                    )
                return AtlasForumInventory(
                    # XenForo repeats sticky topics on later pages. The hot
                    # notification lane must remain authoritative for them.
                    entries=(hot, old),
                    inventory_complete=False,
                    listing_pages=7,
                    next_url=f"{feed['root_url']}page-9",
                )

            async def fetch_threads(self, urls):
                self.thread_urls = list(urls)
                return (), ()

        fake = FakeForum()
        runner = AtlasForumEngineRunner(
            type("Bot", (), {"get_guild": lambda _self, _guild_id: None})(),
            77,
            fake,
            config=AtlasForumEngineConfig(
                archive_listing_batch_pages=8,
                archive_hydration_batch_size=8,
            ),
        )

        result = await runner._scan_feed(feed, archive=True, characters=[])

        self.assertEqual(fake.calls, [
            (str(feed["root_url"]), 1),
            (f"{feed['root_url']}page-2", 7),
        ])
        self.assertEqual(result["backfill_pages_scanned"], 8)
        complaint = forum_engine.complaint_by_thread(
            77, "majestic-rp", "phoenix-15", hot.url
        )
        self.assertTrue(complaint["notifications_armed"])


if __name__ == "__main__":
    unittest.main()
