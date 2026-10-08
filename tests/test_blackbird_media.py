import tempfile
import unittest
import sqlite3
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

import storage
from modules.consensus_web_auth import ConsensusWebPrincipal, TModAccountIdentity
from modules.reactor_web import register_reactor_web_routes
from persistence import blackbird_media_repository as media
from persistence import web_auth_repository


class BlackbirdMediaTests(unittest.TestCase):
    def setUp(self):
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "media-test.db"
        storage.init_db()
        for user_id in (100, 200):
            web_auth_repository.configure_web_credential(77, user_id, f"user{user_id}", "12345678")

    def tearDown(self):
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_private_by_default_search_posts_and_delete(self):
        self.assertFalse(media.own_profile(77, 100, "Иван")["is_public"])
        self.assertEqual(media.search_profiles(77, 200, "Иван"), [])
        with self.assertRaisesRegex(ValueError, "media_profile_not_public"):
            media.create_post(77, 100, "post", "Добрый день")
        saved = media.save_profile(77, 100, "Иван", "Рад знакомству", "night", True)
        self.assertTrue(saved["is_public"])
        self.assertEqual(media.search_profiles(77, 200, "ива")[0]["user_id"], "100")
        self.assertIsNone(media.public_profile(77, 100, 100))
        post = media.create_post(77, 100, "post", "Добрый день")
        rollback = media.create_post(77, 100, "rollback", "Откат инцидента", "https://example.com/video")
        self.assertEqual(media.feed(77, 200, kind="rollback")[0]["id"], rollback["id"])
        self.assertEqual([item["id"] for item in media.profile_posts(77, 200, 100)], [rollback["id"], post["id"]])
        self.assertFalse(media.delete_post(77, 200, post["id"]))
        self.assertTrue(media.delete_post(77, 100, post["id"]))
        self.assertEqual(len(media.feed(77, 200)), 1)
        media.save_profile(77, 100, "Иван", "Рад знакомству", "night", False)
        self.assertEqual(media.search_profiles(77, 200, "Иван"), [])
        self.assertEqual(media.feed(77, 200), [])
        self.assertEqual(media.profile_posts(77, 200, 100), [])
        self.assertEqual([item["id"] for item in media.profile_posts(77, 100, 100)], [rollback["id"]])

    def test_validation(self):
        with self.assertRaisesRegex(ValueError, "media_profile_invalid"):
            media.save_profile(77, 100, "x", "", "orbit", True)
        with self.assertRaisesRegex(ValueError, "media_profile_invalid"):
            media.save_profile(77, 100, "Иван", "", "fake", True)
        media.save_profile(77, 100, "Иван", "", "orbit", True)
        with self.assertRaisesRegex(ValueError, "media_source_required"):
            media.create_post(77, 100, "rollback", "Новый откат")
        with self.assertRaisesRegex(ValueError, "media_source_invalid"):
            media.create_post(77, 100, "post", "Файл", "file:///tmp/private")
        for source in ("https://example.com:wrong/video", "https://example.com:65536/video", "https://example.com:0/video", "https://exa mple.com/video", "https://example.com/line\nbreak"):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "media_source_invalid"):
                media.create_post(77, 100, "rollback", "Материал", source)

    def test_identifier_bounds_do_not_reach_database_bindings(self):
        for invalid in (0, -1, 2**63, 10**100, True, 1.5):
            for operation in (
                lambda: media.public_profile(77, 100, invalid),
                lambda: media.profile_posts(77, 100, invalid),
                lambda: media.feed(77, 100, before_id=invalid),
                lambda: media.profile_posts(77, 100, 200, before_id=invalid),
                lambda: media.delete_post(77, 100, invalid),
            ):
                with self.subTest(identifier=invalid), self.assertRaises(ValueError):
                    operation()
            self.assertIsNone(media.read_asset(77, 100, invalid, "avatar"))

    def test_post_retry_is_idempotent_without_spending_rate_limit(self):
        media.save_profile(77, 100, "Иван", "", "orbit", True)
        nonce = "a" * 32
        first = media.create_post(77, 100, "post", "На связи", client_nonce=nonce)
        for _ in range(8):
            retry = media.create_post(77, 100, "post", "  На связи  ", client_nonce=nonce)
            self.assertEqual(retry["id"], first["id"])
            self.assertNotIn("client_payload_hash", retry)
        self.assertEqual(len(media.feed(77, 200)), 1)
        with self.assertRaisesRegex(ValueError, "media_request_conflict"):
            media.create_post(77, 100, "post", "Другой текст", client_nonce=nonce)
        with self.assertRaisesRegex(ValueError, "media_request_invalid"):
            media.create_post(77, 100, "post", "На связи", client_nonce="wrong")
        storage.init_db()  # Upgrade is repeatable and retains the nonce ledger.
        self.assertEqual(media.create_post(77, 100, "post", "На связи", client_nonce=nonce)["id"], first["id"])
        media.save_profile(77, 100, "Иван", "", "orbit", False)
        with self.assertRaisesRegex(ValueError, "media_profile_not_public"):
            media.create_post(77, 100, "post", "На связи", client_nonce=nonce)
        media.save_profile(77, 100, "Иван", "", "orbit", True)
        media.delete_post(77, 100, first["id"])
        with self.assertRaisesRegex(ValueError, "media_post_deleted"):
            media.create_post(77, 100, "post", "На связи", client_nonce=nonce)
        self.assertEqual(media.feed(77, 200), [])

    def test_nonce_scope_rate_limit_and_page_cursor(self):
        for user_id in (100, 200):
            media.save_profile(77, user_id, f"User {user_id}", "", "orbit", True)
            for index in range(6):
                media.create_post(77, user_id, "post", f"Пост {index}", client_nonce=f"{index:032x}")
            with self.assertRaisesRegex(ValueError, "media_rate_limited"):
                media.create_post(77, user_id, "post", "Сверх лимита", client_nonce="f" * 32)
        page = media.feed(77, 100, limit=7)
        tail = media.feed(77, 100, before_id=page[-1]["id"])
        self.assertEqual(len(page) + len(tail), 12)
        self.assertFalse({post["id"] for post in page} & {post["id"] for post in tail})
        own_page = media.profile_posts(77, 100, 100, limit=3)
        own_tail = media.profile_posts(77, 100, 100, before_id=own_page[-1]["id"])
        self.assertEqual(len(own_page) + len(own_tail), 6)

    def test_avatar_and_cover_are_normalized_private_and_removable(self):
        image = BytesIO()
        Image.new("RGB", (640, 480), "#436072").save(image, format="PNG")
        revision = media.save_asset(77, 100, "avatar", image.getvalue())
        self.assertEqual(len(revision), 20)
        self.assertIsNone(media.read_asset(77, 200, 100, "avatar"))
        self.assertEqual(media.own_profile(77, 100, "Иван")["avatar_revision"], revision)
        media.save_profile(77, 100, "Иван", "", "orbit", True)
        mime, content, visible_revision = media.read_asset(77, 200, 100, "avatar")
        self.assertEqual((mime, visible_revision), ("image/webp", revision))
        with Image.open(BytesIO(content)) as normalized:
            self.assertEqual(normalized.size, (512, 512))
        media.save_asset(77, 100, "cover", image.getvalue())
        self.assertIsNotNone(media.public_profile(77, 200, 100)["cover_revision"])
        cover = media.read_asset(77, 200, 100, "cover")
        with Image.open(BytesIO(cover[1])) as normalized_cover:
            self.assertEqual(normalized_cover.size, (1600, 560))
        # Media lives in the guarded database, not in a loose file that a DB
        # recovery point would silently omit.
        with sqlite3.connect(storage.DATABASE_FILE) as source, sqlite3.connect(":memory:") as backup:
            source.backup(backup)
            backed_up = backup.execute(
                "SELECT content FROM blackbird_media_assets WHERE guild_id=? AND user_id=? AND kind=?",
                (77, 100, "cover"),
            ).fetchone()
        self.assertEqual(bytes(backed_up[0]), cover[1])
        self.assertTrue(media.delete_asset(77, 100, "avatar"))
        self.assertIsNone(media.read_asset(77, 200, 100, "avatar"))
        with self.assertRaisesRegex(ValueError, "media_asset_type_invalid"):
            media.save_asset(77, 100, "avatar", b"<svg><script>alert(1)</script></svg>")


class BlackbirdMediaHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "media-http-test.db"
        storage.init_db()
        for user_id in (100, 200):
            web_auth_repository.configure_web_credential(77, user_id, f"user{user_id}", "12345678")

    def tearDown(self):
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    async def test_non_guild_account_auth_csrf_publish_feed_and_asset_privacy(self):
        async def authenticate(request):
            user_id = int(request.headers.get("X-Test-User") or 0)
            if user_id not in (100, 200):
                return None, False
            member = TModAccountIdentity(user_id, "Аккаунт вне Сената")
            principal = ConsensusWebPrincipal(user_id, 77, member.display_name, "csrf-test", member)
            self.assertFalse(principal.guild_member)
            return principal, request.headers.get("X-Test-Legacy") == "1"

        app = web.Application()
        register_reactor_web_routes(
            app, SimpleNamespace(), guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "consensus",
            authenticate=authenticate,
        )
        async with TestClient(TestServer(app)) as client:
            url = "/api/blackbird/media"
            self.assertEqual((await client.get(url)).status, 401)
            self.assertEqual((await client.get(url, headers={"X-Test-User": "100", "X-Test-Legacy": "1"})).status, 401)
            own = await client.get(url, headers={"X-Test-User": "100"})
            self.assertEqual(own.status, 200)
            self.assertEqual((await own.json())["viewer"]["id"], "100")
            senate = await client.get("/api/reactor/home", headers={"X-Test-User": "100"})
            self.assertEqual(senate.status, 403)
            self.assertEqual((await senate.json())["error"], "zero_account_reactor_forbidden")
            self.assertEqual((await client.post(url, headers={"X-Test-User": "100"}, json={"action": "profile"})).status, 403)
            headers = {"X-Test-User": "100", "X-CSRF-Token": "csrf-test"}
            for identifier in ("-1", "9223372036854775808", "9" * 100):
                for payload in ({"action": "profile_view", "user_id": identifier},
                                {"action": "profile_posts", "user_id": identifier},
                                {"action": "profile_posts", "user_id": "200", "before_id": identifier},
                                {"action": "feed", "before_id": identifier},
                                {"action": "delete", "post_id": identifier}):
                    invalid = await client.post(url, headers=headers, json=payload)
                    self.assertEqual(invalid.status, 400, payload)
                    self.assertIn("media_", (await invalid.json())["error"])
                self.assertEqual((await client.get(f"{url}/assets/{identifier}/avatar", headers=headers)).status, 404)
            profile = await client.post(url, headers=headers, json={
                "action": "profile", "display_name": "Иван", "bio": "На связи", "cover_theme": "orbit", "is_public": "true",
            })
            self.assertEqual(profile.status, 200)
            post_data = {"action": "post", "kind": "post", "body": "Первый пост", "client_nonce": "b" * 32}
            post = await client.post(url, headers=headers, json=post_data)
            self.assertEqual(post.status, 200)
            retry = await client.post(url, headers=headers, json=post_data)
            self.assertEqual((await retry.json())["result"]["id"], (await post.json())["result"]["id"])
            conflict = await client.post(url, headers=headers, json={**post_data, "body": "Изменённый пост"})
            self.assertEqual(conflict.status, 400)
            found = await client.post(url, headers={"X-Test-User": "200", "X-CSRF-Token": "csrf-test"}, json={"action": "search", "query": "ива"})
            self.assertEqual((await found.json())["result"][0]["user_id"], "100")
            feed = await client.get(url, headers={"X-Test-User": "200"})
            self.assertEqual((await feed.json())["feed"][0]["body"], "Первый пост")
            posts = await client.post(url, headers={"X-Test-User": "200", "X-CSRF-Token": "csrf-test"},
                                      json={"action": "profile_posts", "user_id": "100"})
            self.assertEqual((await posts.json())["result"][0]["body"], "Первый пост")
            with sqlite3.connect(storage.DATABASE_FILE) as con:
                con.executemany(
                    "INSERT INTO blackbird_media_posts(guild_id,author_id,kind,body,created_at) VALUES(77,100,'post',?,'2026-01-01T00:00:00+00:00')",
                    [(f"Архивная запись {index}",) for index in range(30)],
                )
            first_page = await (await client.get(url, headers=headers)).json()
            self.assertTrue(first_page["has_more"])
            self.assertEqual(len(first_page["feed"]), 30)
            tail = await (await client.post(url, headers=headers, json={"action": "feed", "before_id": str(first_page["feed"][-1]["id"])})).json()
            self.assertFalse(tail["has_more"])
            self.assertEqual(len(tail["result"]), 1)
            profile_page = await (await client.post(url, headers=headers, json={"action": "profile_posts", "user_id": "100"})).json()
            self.assertTrue(profile_page["has_more"])
            profile_tail = await (await client.post(url, headers=headers, json={"action": "profile_posts", "user_id": "100", "before_id": str(profile_page["result"][-1]["id"])})).json()
            self.assertEqual(len(profile_tail["result"]), 1)
            self.assertFalse(profile_tail["has_more"])
            image = BytesIO()
            Image.new("RGB", (640, 480), "#436072").save(image, format="PNG")
            asset_url = "/api/blackbird/media/assets/avatar"
            upload = await client.post(asset_url, headers={**headers, "Content-Type": "application/octet-stream"}, data=image.getvalue())
            self.assertEqual(upload.status, 200)
            self.assertEqual((await client.get("/api/blackbird/media/assets/100/avatar")).status, 401)
            self.assertEqual((await client.get("/api/blackbird/media/assets/100/avatar", headers={"X-Test-User": "200"})).status, 200)
            media.save_profile(77, 100, "Иван", "", "orbit", False)
            self.assertEqual((await client.get("/api/blackbird/media/assets/100/avatar", headers={"X-Test-User": "200"})).status, 404)
            self.assertEqual((await client.delete(asset_url, headers={"X-Test-User": "100"})).status, 403)
            self.assertEqual((await client.delete(asset_url, headers=headers)).status, 200)
