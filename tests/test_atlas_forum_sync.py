import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import storage
from modules.atlas_forum_sync import (
    AtlasForumBrowser,
    AtlasForumManualActionRequired,
    AtlasForumScrapeBatch,
    AtlasForumSnapshot,
    AtlasForumSyncConfig,
    AtlasForumSyncError,
    AtlasForumSyncRunner,
    forum_interstitial_kind,
    parse_forum_listing,
    parse_forum_thread,
)
from persistence import atlas_repository


ROOT_URL = "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.1213/"


def sync_config() -> AtlasForumSyncConfig:
    return AtlasForumSyncConfig(
        enabled=True,
        selenium_url="http://browser:4444/wd/hub",
        root_url=ROOT_URL,
        cookie_file="",
        feed_key="majestic-phoenix-laws-test",
        server_code="phoenix-15",
        faction_code="lspd",
        visibility_scope="server",
        interval_seconds=43_200,
        initial_delay_seconds=5,
        page_delay_seconds=1,
        challenge_wait_seconds=10,
        max_listing_pages=20,
        max_threads=200,
    )


class AtlasForumParserTests(unittest.TestCase):
    def test_hidden_login_overlay_does_not_mask_authenticated_listing(self) -> None:
        page = """
        <html><body>
          <form action="/login"><input type="password" /></form>
          <div class="structItem-title">
            <a href="/threads/ugolovnyi-kodeks.123/">Уголовный кодекс</a>
          </div>
        </body></html>
        """

        self.assertIsNone(forum_interstitial_kind(page))

    def test_hidden_login_overlay_does_not_mask_authenticated_thread(self) -> None:
        page = """
        <html><body>
          <form action="/login"><input type="password" /></form>
          <div class="message-userContent">
            <article class="message-body"><div class="bbWrapper">Глава 16</div></article>
          </div>
        </body></html>
        """

        self.assertIsNone(forum_interstitial_kind(page))

    def test_listing_collects_canonical_threads_and_next_page(self) -> None:
        page = """
        <html><body>
          <div class="structItem-title"><a href="/threads/zakon-1.100/">Закон 1</a></div>
          <div class="structItem-title"><a href="https://forum.majestic-rp.ru/threads/zakon-2.101/?ref=x">Закон 2</a></div>
          <a href="https://example.com/threads/foreign.1/">Чужая ссылка</a>
          <a class="pageNav-jump pageNav-jump--next" href="/forums/zakonodatel-naya-baza.1213/page-2">Далее</a>
        </body></html>
        """

        links, next_url = parse_forum_listing(page, ROOT_URL)

        self.assertEqual(
            links,
            [
                "https://forum.majestic-rp.ru/threads/zakon-1.100/",
                "https://forum.majestic-rp.ru/threads/zakon-2.101/",
            ],
        )
        self.assertEqual(
            next_url,
            "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.1213/page-2",
        )

    def test_listing_normalizes_unread_thread_suffix(self) -> None:
        page = """
        <div class="structItem-title">
          <a href="/threads/zakon.100/unread?new=1">Закон</a>
        </div>
        """

        links, _next_url = parse_forum_listing(page, ROOT_URL)

        self.assertEqual(
            links,
            ["https://forum.majestic-rp.ru/threads/zakon.100/"],
        )

    def test_thread_extracts_only_first_post_without_quote(self) -> None:
        page = """
        <html><head><meta property="og:title" content="Уголовный кодекс" /></head><body>
          <h1 class="p-title-value">Уголовный кодекс</h1>
          <article class="message message--post">
            <a class="message-name"><span class="username">Robert</span></a>
            <time class="u-dt" datetime="2026-08-08T12:00:00+03:00"></time>
            <div class="message-userContent"><article class="message-body js-selectToQuote"><div class="bbWrapper">
              <p>Раздел первый. Общие положения закона.</p>
              <blockquote class="bbCodeBlock bbCodeBlock--quote">Старая цитата</blockquote>
              <ol><li>Положение номер один.</li><li>Положение номер два.</li></ol>
            </div></article></div>
          </article>
          <article class="message message--post"><div class="message-body"><p>Чужой ответ</p></div></article>
        </body></html>
        """

        snapshot = parse_forum_thread(
            page,
            "https://forum.majestic-rp.ru/threads/kodeks.100/",
        )

        self.assertEqual(snapshot.title, "Уголовный кодекс")
        self.assertEqual(snapshot.author, "Robert")
        self.assertIn("Положение номер два", snapshot.content)
        self.assertNotIn("Старая цитата", snapshot.content)
        self.assertNotIn("Чужой ответ", snapshot.content)

    def test_thread_preserves_text_from_rich_divs_and_tables(self) -> None:
        page = """
        <h1 class="p-title-value">Уголовный кодекс</h1>
        <article class="message message--post">
          <div class="message-body"><div class="bbWrapper">
            <p>Глава 1. Общие положения</p>
            <div class="law-section"><span>Статья 1. Основные понятия.</span></div>
            <table><tr><td>Глава 16.</td><td>Преступления против правосудия.</td></tr></table>
          </div></div>
        </article>
        """

        snapshot = parse_forum_thread(page, "https://forum.majestic-rp.ru/threads/uk.1/")

        self.assertIn("Статья 1. Основные понятия.", snapshot.content)
        self.assertIn("Глава 16.", snapshot.content)
        self.assertIn("Преступления против правосудия.", snapshot.content)

    def test_thread_supports_theme_without_standard_message_wrappers(self) -> None:
        page = """
        <h1 class="p-title-value">Общие правила сервера</h1>
        <div class="message custom-theme-message">
          <div class="bbWrapper"><p>Проверенный текст правил сервера в новой теме форума.</p></div>
        </div>
        """

        snapshot = parse_forum_thread(
            page,
            "https://forum.majestic-rp.ru/threads/general-rules.2/",
        )

        self.assertIn("Проверенный текст правил", snapshot.content)

    def test_interstitial_detection_distinguishes_js_and_manual_checks(self) -> None:
        self.assertEqual(
            forum_interstitial_kind("<p>Please turn JavaScript on</p><script src='vddosw3data.js'></script>"),
            "javascript",
        )
        self.assertEqual(forum_interstitial_kind("<div class='g-recaptcha'></div>"), "manual")
        self.assertEqual(
            forum_interstitial_kind(
                '<form action="/login/login"><input type="password" name="password"></form>'
            ),
            "login",
        )
        self.assertIsNone(forum_interstitial_kind("<html><body>Обычная страница</body></html>"))
        self.assertEqual(
            forum_interstitial_kind("<main>You must be logged in to view this page</main>"),
            "login",
        )
        self.assertEqual(
            forum_interstitial_kind("<main>You do not have permission to view this page</main>"),
            "access",
        )

    def test_browser_marks_capped_inventory_as_incomplete(self) -> None:
        config = replace(sync_config(), max_threads=1)
        browser = AtlasForumBrowser(config)
        listing = """
        <div class="structItem-title"><a href="/threads/one.1/">One</a></div>
        <div class="structItem-title"><a href="/threads/two.2/">Two</a></div>
        """
        thread = """
        <h1 class="p-title-value">Первый закон</h1>
        <article class="message message--post"><div class="message-body"><div class="bbWrapper">
        <p>Достаточно длинный проверенный текст первой редакции закона.</p>
        </div></div></article>
        """
        browser._load = lambda url: listing if "/forums/" in url else thread

        batch = browser.scrape()

        self.assertEqual(len(batch.snapshots), 1)
        self.assertFalse(batch.inventory_complete)

    def test_browser_indexes_other_threads_when_one_thread_is_unreadable(self) -> None:
        browser = AtlasForumBrowser(sync_config())
        listing = """
        <div class="structItem-title"><a href="/threads/broken.1/">Broken</a></div>
        <div class="structItem-title"><a href="/threads/working.2/">Working</a></div>
        """
        working = """
        <h1 class="p-title-value">Уголовный кодекс</h1>
        <article class="message message--post"><div class="message-body"><div class="bbWrapper">
        <p>Полная редакция уголовного кодекса с достаточным объёмом текста.</p>
        </div></div></article>
        """

        def load(url: str) -> str:
            if "/forums/" in url:
                return listing
            if "broken" in url:
                raise AtlasForumSyncError("atlas_forum_thread_body_missing")
            return working

        browser._load = load
        batch = browser.scrape()

        self.assertEqual([item.title for item in batch.snapshots], ["Уголовный кодекс"])
        self.assertEqual(len(batch.skipped_threads), 1)
        self.assertFalse(batch.inventory_complete)

    def test_single_thread_import_uses_saved_browser_and_rejects_foreign_host(self) -> None:
        browser = AtlasForumBrowser(sync_config())
        page = """
        <h1 class="p-title-value">Устав LSPD</h1>
        <article class="message message--post"><div class="message-body"><div class="bbWrapper">
        <p>Настоящий устав определяет полномочия сотрудников и порядок службы.</p>
        </div></div></article>
        """
        browser._load = lambda _url: page

        snapshot = browser.scrape_thread(
            "https://forum.majestic-rp.ru/threads/ustav-lspd.500/?ref=atlas"
        )

        self.assertEqual(snapshot.title, "Устав LSPD")
        self.assertEqual(
            snapshot.url,
            "https://forum.majestic-rp.ru/threads/ustav-lspd.500/",
        )
        with self.assertRaisesRegex(AtlasForumSyncError, "thread_url_invalid"):
            browser.scrape_thread("https://example.org/threads/secret.1/")

    @patch("modules.atlas_forum_sync.requests.delete")
    @patch("modules.atlas_forum_sync.requests.post")
    def test_browser_releases_orphaned_grid_session(self, post, delete) -> None:
        post.return_value = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "data": {"sessionsInfo": {"sessions": [{"id": "old-atlas-session"}]}}
            },
        )
        delete.return_value = SimpleNamespace(status_code=200)
        browser = AtlasForumBrowser(sync_config())

        released = browser._release_orphaned_sessions()

        self.assertEqual(released, 1)
        delete.assert_called_once_with(
            "http://browser:4444/session/old-atlas-session",
            timeout=5,
        )

    @patch("modules.atlas_forum_sync.time.sleep")
    def test_browser_retries_session_creation_once(self, _sleep) -> None:
        class FakeOptions:
            arguments = []

            def add_argument(self, value):
                self.arguments.append(value)

            def set_capability(self, _name, _value):
                return None

        selenium = ModuleType("selenium")
        webdriver = ModuleType("selenium.webdriver")
        chrome = ModuleType("selenium.webdriver.chrome")
        options = ModuleType("selenium.webdriver.chrome.options")
        options.Options = FakeOptions
        browser = AtlasForumBrowser(sync_config())
        driver = SimpleNamespace(current_url=ROOT_URL)
        with patch.dict(
            "sys.modules",
            {
                "selenium": selenium,
                "selenium.webdriver": webdriver,
                "selenium.webdriver.chrome": chrome,
                "selenium.webdriver.chrome.options": options,
            },
        ), patch.object(
            browser, "_release_orphaned_sessions", return_value=1
        ) as release, patch.object(
            browser, "_open_driver", side_effect=[RuntimeError("session occupied"), driver]
        ) as open_driver:
            connected = browser._connect()

        self.assertIs(connected, driver)
        self.assertEqual(open_driver.call_count, 2)
        self.assertEqual(release.call_count, 2)
        self.assertFalse(
            any(value.startswith("--user-data-dir") for value in FakeOptions.arguments)
        )

    def test_browser_persists_and_restores_authenticated_cookies(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            cookie_file = Path(directory) / "forum-cookies.json"
            browser = AtlasForumBrowser(
                replace(sync_config(), cookie_file=str(cookie_file))
            )
            source = SimpleNamespace(
                get_cookies=lambda: [
                    {
                        "name": "xf_session",
                        "value": "signed-in",
                        "domain": "forum.majestic-rp.ru",
                        "path": "/",
                        "sameSite": "Lax",
                    }
                ],
                execute_script=lambda _script: {"forum-theme": "dark"},
            )

            self.assertEqual(browser._save_cookies(source), 1)
            target = SimpleNamespace(
                get=Mock(),
                add_cookie=Mock(),
                execute_script=Mock(),
                refresh=Mock(),
            )
            self.assertEqual(browser._restore_cookies(target), 1)

            target.get.assert_called_once_with("https://forum.majestic-rp.ru/")
            target.add_cookie.assert_called_once()
            self.assertEqual(
                target.add_cookie.call_args.args[0]["value"],
                "signed-in",
            )
            target.execute_script.assert_called_once()
            self.assertEqual(
                target.execute_script.call_args.args[1],
                {"forum-theme": "dark"},
            )
            target.refresh.assert_called_once_with()

    def test_browser_restores_legacy_cookie_list(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            cookie_file = Path(directory) / "legacy-cookies.json"
            cookie_file.write_text(
                json.dumps([{"name": "xf_user", "value": "legacy", "path": "/"}]),
                encoding="utf-8",
            )
            browser = AtlasForumBrowser(
                replace(sync_config(), cookie_file=str(cookie_file))
            )
            target = SimpleNamespace(
                get=Mock(),
                add_cookie=Mock(),
                refresh=Mock(),
            )

            self.assertEqual(browser._restore_cookies(target), 1)
            target.add_cookie.assert_called_once()
            target.refresh.assert_called_once_with()

    @patch("modules.atlas_forum_sync.time.sleep")
    def test_empty_listing_becomes_manual_action_after_retries(self, _sleep) -> None:
        browser = AtlasForumBrowser(sync_config())
        loads = 0

        def empty(_url):
            nonlocal loads
            loads += 1
            return "<html><body>Промежуточная страница проверки</body></html>"

        browser._load = empty
        with self.assertRaisesRegex(AtlasForumManualActionRequired, "список тем"):
            browser.scrape()
        self.assertEqual(loads, 3)


class AtlasForumRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-forum-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_synced_source_is_versioned_and_never_deleted_when_missing(self) -> None:
        feed = atlas_repository.atlas_ensure_forum_feed(
            77,
            feed_key="laws",
            root_url=ROOT_URL,
        )
        kwargs = {
            "organization_id": int(feed["organization_id"]),
            "title": "Закон",
            "content": "Первая полная редакция закона длиной больше двадцати символов.",
            "source_url": "https://forum.majestic-rp.ru/threads/zakon.100/",
            "server_code": "phoenix-15",
            "faction_code": "lspd",
            "visibility_scope": "server",
            "feed_key": "laws",
        }
        first = atlas_repository.atlas_upsert_synced_knowledge(**kwargs)
        atlas_repository.atlas_mark_knowledge_indexed(
            int(first["source"]["id"]),
            point_id="point-1",
        )
        unchanged = atlas_repository.atlas_upsert_synced_knowledge(**kwargs)
        changed = atlas_repository.atlas_upsert_synced_knowledge(
            **{
                **kwargs,
                "content": "Вторая полная редакция закона с проверенным изменением содержания.",
            }
        )

        self.assertTrue(first["created"])
        self.assertFalse(unchanged["changed"])
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["source"]["status"], "pending")
        revisions = atlas_repository.atlas_knowledge_revisions(int(first["source"]["id"]))
        self.assertEqual(len(revisions), 1)
        self.assertIn("Первая полная редакция", revisions[0]["content_text"])

        renamed = atlas_repository.atlas_upsert_synced_knowledge(
            **{**kwargs, "title": "Закон — новая редакция заголовка", "content": changed["source"]["content_text"]}
        )
        self.assertTrue(renamed["changed"])
        self.assertEqual(renamed["source"]["title"], "Закон — новая редакция заголовка")

        atlas_repository.atlas_mark_forum_sources_seen(
            int(feed["organization_id"]),
            feed_key="laws",
            seen_urls=[],
        )
        stored = atlas_repository.atlas_knowledge_sources(
            int(feed["organization_id"]),
            server_code="phoenix-15",
            faction_code="lspd",
        )[0]
        self.assertTrue(stored["metadata"]["missing_from_feed"])
        self.assertNotEqual(stored["status"], "archived")

        atlas_repository.atlas_forum_sync_started(int(feed["id"]))
        state = atlas_repository.atlas_forum_sync_finished(
            int(feed["id"]),
            stats={"pages": 1, "changed": 1},
        )
        self.assertEqual(state["last_stats"], {"pages": 1, "changed": 1})


class _FakeBrowser:
    def __init__(self, result):
        self.result = result
        self.closed = False

    def scrape(self):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def scrape_listing(self, url):
        self.listing_url = url
        return self.scrape()

    def close(self):
        self.closed = True


class _ManualBrowser(_FakeBrowser):
    def __init__(self):
        super().__init__(
            AtlasForumManualActionRequired("Форум запросил ручное подтверждение.")
        )
        self.active = True
        self.checkpoints = 0

    def checkpoint_authentication(self):
        self.checkpoints += 1
        self.active = False
        return 1


class _ActiveFailingBrowser(_ManualBrowser):
    def __init__(self):
        super().__init__()
        self.result = AtlasForumSyncError("atlas_forum_thread_body_missing")


class AtlasForumRunnerTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_runner_indexes_only_changes_and_preserves_incomplete_inventory(self) -> None:
        snapshot = AtlasForumSnapshot(
            url="https://forum.majestic-rp.ru/threads/zakon.100/",
            title="Закон",
            content="Полная редакция закона для безопасной проверки Atlas длиной больше двадцати символов.",
        )
        browser = _FakeBrowser(AtlasForumScrapeBatch((snapshot,), False))
        index = AsyncMock(return_value=["point-1"])
        runner = AtlasForumSyncRunner(
            SimpleNamespace(get_guild=lambda _guild_id: None),
            77,
            config=sync_config(),
            browser=browser,
            index_callback=index,
        )

        first = await runner.sync_once()
        second = await runner.sync_once()

        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["last_stats"]["inventory_complete"], False)
        self.assertEqual(second["last_stats"]["changed"], 0)
        index.assert_awaited_once()
        self.assertTrue(browser.closed)

    async def test_runner_reads_any_same_host_forum_listing(self) -> None:
        snapshot = AtlasForumSnapshot(
            url="https://forum.majestic-rp.ru/threads/general-rule.900/",
            title="Общие правила",
            content="Полный текст общих правил сервера для проверки импорта раздела.",
        )
        browser = _FakeBrowser(AtlasForumScrapeBatch((snapshot,), True))
        runner = AtlasForumSyncRunner(
            SimpleNamespace(get_guild=lambda _guild_id: None),
            77,
            config=sync_config(),
            browser=browser,
            index_callback=AsyncMock(),
        )

        batch = await runner.fetch_listing(
            "https://forum.majestic-rp.ru/forums/general-server-rules/"
        )

        self.assertEqual(batch.snapshots[0].title, "Общие правила")
        self.assertEqual(
            browser.listing_url,
            "https://forum.majestic-rp.ru/forums/general-server-rules/",
        )
        self.assertTrue(browser.closed)

    def test_runner_recognizes_only_configured_forum_listing(self) -> None:
        runner = AtlasForumSyncRunner(
            SimpleNamespace(get_guild=lambda _guild_id: None),
            77,
            config=sync_config(),
            browser=_FakeBrowser(None),
            index_callback=AsyncMock(),
        )

        self.assertTrue(runner.is_configured_listing_url(ROOT_URL))
        self.assertFalse(
            runner.is_configured_listing_url(
                "https://forum.majestic-rp.ru/forums/drugoy-razdel.10/"
            )
        )
        self.assertTrue(
            runner.is_forum_listing_url(
                "https://forum.majestic-rp.ru/forums/general-server-rules/"
            )
        )
        self.assertFalse(runner.is_configured_listing_url("https://example.org/forums/1/"))
        self.assertFalse(runner.is_forum_listing_url("https://example.org/forums/1/"))

    async def test_manual_check_sets_attention_without_changing_knowledge(self) -> None:
        browser = _FakeBrowser(
            AtlasForumManualActionRequired("Форум запросил ручное подтверждение.")
        )
        runner = AtlasForumSyncRunner(
            SimpleNamespace(get_guild=lambda _guild_id: None),
            77,
            config=sync_config(),
            browser=browser,
            index_callback=AsyncMock(),
        )

        state = await runner.sync_once()

        self.assertEqual(state["status"], "attention")
        self.assertIn("ручное подтверждение", state["last_error"])
        feed = atlas_repository.atlas_forum_sync_status(77)
        self.assertIsNone(feed["last_success_at"])
        self.assertEqual(atlas_repository.atlas_indexable_knowledge_sources(), [])

    async def test_manual_login_starts_immediate_auth_checkpoint(self) -> None:
        browser = _ManualBrowser()
        runner = AtlasForumSyncRunner(
            SimpleNamespace(get_guild=lambda _guild_id: None),
            77,
            config=sync_config(),
            browser=browser,
            index_callback=AsyncMock(return_value=[]),
        )

        await runner.sync_once()
        await asyncio.sleep(0.05)

        self.assertEqual(browser.checkpoints, 1)
        await runner.close()

    async def test_forum_read_failure_keeps_active_browser_open(self) -> None:
        browser = _ActiveFailingBrowser()
        runner = AtlasForumSyncRunner(
            SimpleNamespace(get_guild=lambda _guild_id: None),
            77,
            config=sync_config(),
            browser=browser,
            index_callback=AsyncMock(return_value=[]),
        )

        state = await runner.sync_once()
        await asyncio.sleep(0.05)

        self.assertEqual(state["status"], "attention")
        self.assertEqual(state["last_stats"]["phase"], "forum_read")
        self.assertFalse(browser.closed)
        self.assertEqual(browser.checkpoints, 1)
        await runner.close()

    async def test_failed_index_is_retried_even_when_forum_text_is_unchanged(self) -> None:
        snapshot = AtlasForumSnapshot(
            url="https://forum.majestic-rp.ru/threads/retry.101/",
            title="Повтор индексации",
            content="Проверенный материал должен повторно попасть в поиск после временного сбоя.",
        )
        browser = _FakeBrowser(AtlasForumScrapeBatch((snapshot,), True))
        index = AsyncMock(side_effect=[RuntimeError("qdrant unavailable"), ["point-2"]])
        runner = AtlasForumSyncRunner(
            SimpleNamespace(get_guild=lambda _guild_id: None),
            77,
            config=sync_config(),
            browser=browser,
            index_callback=index,
        )

        first = await runner.sync_once()
        second = await runner.sync_once()

        self.assertEqual(first["status"], "attention")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["last_stats"]["changed"], 0)
        self.assertEqual(second["last_stats"]["retried"], 1)
        self.assertEqual(index.await_count, 2)


if __name__ == "__main__":
    unittest.main()
