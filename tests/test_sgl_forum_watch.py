import gc
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import storage
from modules.sgl_forum_watch import SGLForumWatchConfig, SGLForumWatchRunner
from persistence import sgl_repository


class _Browser:
    def __init__(self) -> None:
        self.version = 1
        self.closed = 0

    def scrape_thread(self, _url: str) -> SimpleNamespace:
        return SimpleNamespace(title="Иск SGL", content=f"Версия темы {self.version}. Достаточно длинный текст.")

    def close(self) -> None:
        self.closed += 1


class SGLForumWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_data_dir, self.old_database_file = storage.DATA_DIR, storage.DATABASE_FILE
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "sgl-forum-watch.db"
        storage.init_db()
        reserved = sgl_repository.reserve_sgl_case(
            guild_id=55, client_id=1, client_display="Client",
            lead_lawyer_id=2, lead_lawyer_display="Lawyer",
            secretary_id=None, secretary_display=None,
            created_by_id=2, created_by_display="Lawyer",
        )
        self.case = sgl_repository.attach_sgl_case_channel(reserved.id, 333)
        assert self.case is not None
        draft = sgl_repository.create_sgl_case_forum_publication(
            case=self.case, target_url="https://forum.majestic-rp.ru/forums/court.42/",
            title="Иск", body="Подробный текст иска.",
            created_by_id=2, created_by_display="Lawyer",
        )
        claimed = sgl_repository.claim_sgl_case_forum_publication(
            guild_id=55, case_number=self.case.case_number, publication_id=draft["id"],
            actor_id=2, actor_display="Lawyer",
        )
        assert claimed is not None
        sgl_repository.mark_sgl_case_forum_publication_published(
            publication_id=draft["id"], forum_url="https://forum.majestic-rp.ru/threads/claim.1/"
        )

    def tearDown(self) -> None:
        storage.DATA_DIR, storage.DATABASE_FILE = self.old_data_dir, self.old_database_file
        gc.collect()
        self.temp_dir.cleanup()

    async def test_first_snapshot_is_quiet_and_later_change_is_detected(self) -> None:
        browser = _Browser()
        config = SGLForumWatchConfig(
            enabled=True, interval_seconds=120, selenium_url="http://browser:4444/wd/hub",
            root_url="https://forum.majestic-rp.ru/forums/", cookie_file="", challenge_wait_seconds=10,
        )
        bot = SimpleNamespace(get_channel=lambda _channel_id: None)
        runner = SGLForumWatchRunner(bot, 55, config=config, browser=browser)  # type: ignore[arg-type]
        self.assertEqual(runner.status_snapshot()["state"], "idle")
        first = await runner.check_once()
        browser.version = 2
        second = await runner.check_once()
        observations = sgl_repository.list_sgl_forum_observations(55)
        state = runner.status_snapshot()

        self.assertEqual(first["changed"], 0)
        self.assertEqual(second["changed"], 1)
        self.assertEqual(second["alerts"], 0)  # no Discord channel in this isolated test
        self.assertEqual(observations[0]["status"], "changed")
        self.assertGreaterEqual(browser.closed, 2)
        self.assertEqual(state["state"], "idle")
        self.assertEqual(state["last_summary"], second)
        self.assertIsNotNone(state["last_started_at"])
        self.assertIsNotNone(state["last_finished_at"])
        self.assertIsNone(state["last_error"])


if __name__ == "__main__":
    unittest.main()
