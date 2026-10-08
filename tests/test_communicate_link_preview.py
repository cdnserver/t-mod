import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import storage
from modules.communicate_link_preview import document_preview, resource_reference
from modules.consensus_web_auth import ConsensusWebPrincipal
from modules.reactor_web import register_reactor_web_routes
from persistence import ovr_repository, tvrs_repository, web_auth_repository


class LinkReferenceTests(unittest.TestCase):
    def test_exact_hosts_transport_and_identifiers(self):
        self.assertEqual(resource_reference("https://ovr.tvr.lat/ovr?case_id=42"), ("ovr", 42))
        self.assertEqual(resource_reference("https://reactor.tvr.lat/ovr/cases/42"), ("ovr", 42))
        self.assertEqual(resource_reference("https://consensus.tvr.lat/bills/5"), ("bill", 5))
        self.assertEqual(resource_reference("https://home.tvr.lat/reactor?bill_number=9"), ("bill_number", 9))
        for value in ("https://evil.example/?bill_id=5", "https://ovr.tvr.lat.evil.example/?case_id=42",
                      "http://ovr.tvr.lat/?case_id=42", "https://user:pass@ovr.tvr.lat/?case_id=42",
                      "https://ovr.tvr.lat:8443/?case_id=42", "https://ovr.tvr.lat/?case_id=-1",
                      "https://ovr.tvr.lat/?case_id=999999999999999999999", "https://reactor.tvr.lat/case/42"):
            self.assertIsNone(resource_reference(value), value)


class LinkPreviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_dir, self.old_file = storage.DATA_DIR, storage.DATABASE_FILE
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.directory.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "previews.db"
        storage.init_db()

    def tearDown(self):
        storage.DATA_DIR, storage.DATABASE_FILE = self.old_dir, self.old_file
        self.directory.cleanup()

    def create_bill(self, guild_id=77):
        return tvrs_repository.tvrs_create_bill(guild_id=guild_id, channel_id=123, author_id=100,
                                               author_display="Автор", title="Улучшение транспорта",
                                               summary="**План:** открыть [маршрут](https://example.com). " + "Подробности " * 80,
                                               materials=None)

    async def test_publication_owner_moderator_and_guild_boundaries(self):
        bill = self.create_bill()
        url = f"https://consensus.tvr.lat/bills/{bill.id}"
        self.assertIsNone(document_preview(77, 200, url))
        self.assertEqual(document_preview(77, 100, url)["title"], "Улучшение транспорта")
        self.assertEqual(document_preview(77, 100, url)["status"], "Черновик")
        self.assertIsNotNone(document_preview(77, 200, url, can_moderate_bills=True))
        tvrs_repository.tvrs_set_bill_message(bill.id, 456)
        preview = document_preview(77, 200, url)
        self.assertEqual(preview["title"], "Улучшение транспорта")
        self.assertEqual(preview["status"], "В повестке")
        self.assertIn("План: открыть маршрут.", preview["detail"])
        self.assertNotIn("https://", preview["detail"])
        self.assertLessEqual(len(preview["detail"]), 221)
        self.assertIsNone(document_preview(88, 100, url, can_moderate_bills=True))

    async def test_ovr_access_and_guild_boundaries(self):
        case = ovr_repository.create_case(guild_id=77, first_name="Robert", last_name="Example", static_id="263345",
                                          discord_text="robert", discord_user_id=100, forum_url=None, additional_info="",
                                          actor_id=100, actor_display="Автор", objective="Проверить обстоятельства обращения")
        url = f"https://ovr.tvr.lat/ovr?case_id={case['id']}"
        self.assertIsNone(document_preview(77, 100, url))
        self.assertEqual(document_preview(77, 100, url, can_read_ovr=True)["detail"], "Проверить обстоятельства обращения")
        self.assertIsNone(document_preview(88, 100, url, can_read_ovr=True))

    async def test_http_requires_login_and_does_not_expose_private_drafts(self):
        bill = self.create_bill()
        case = ovr_repository.create_case(guild_id=77, first_name="Robert", last_name="Example", static_id="263345",
                                          discord_text="robert", discord_user_id=100, forum_url=None, additional_info="",
                                          actor_id=100, actor_display="Автор", objective="Закрытые материалы обращения")

        async def authenticate(request):
            value = request.headers.get("X-Test-User")
            if value not in {"100", "200"}:
                return None, False
            member = SimpleNamespace(id=int(value), roles=[], guild_permissions=SimpleNamespace(administrator=False))
            return ConsensusWebPrincipal(int(value), 77, "Участник", "csrf", member), False

        app = web.Application()
        register_reactor_web_routes(app, SimpleNamespace(), guild_id=77,
                                    asset_dir=Path(__file__).resolve().parents[1] / "web" / "consensus", authenticate=authenticate)
        async with TestClient(TestServer(app)) as client:
            endpoint = "/api/blackbird/communicate/preview"
            query = {"url": f"https://consensus.tvr.lat/bills/{bill.id}"}
            self.assertEqual((await client.get(endpoint, params=query)).status, 401)
            hidden = await client.get(endpoint, params=query, headers={"X-Test-User": "200"})
            self.assertEqual(hidden.status, 200)
            self.assertIsNone((await hidden.json())["result"])
            self.assertIn("no-store", hidden.headers["Cache-Control"])
            own = await client.get(endpoint, params=query, headers={"X-Test-User": "100"})
            self.assertEqual((await own.json())["result"]["title"], "Улучшение транспорта")
            unsupported = await client.get(endpoint, params={"url": "http://127.0.0.1:5432/?bill_id=1"}, headers={"X-Test-User": "100"})
            self.assertIsNone((await unsupported.json())["result"])
            case_query = {"url": f"https://ovr.tvr.lat/ovr?case_id={case['id']}"}
            denied = await client.get(endpoint, params=case_query, headers={"X-Test-User": "200"})
            self.assertIsNone((await denied.json())["result"])
            web_auth_repository.web_set_section_grant(77, 200, "ovr", enabled=True, granted_by_id=100)
            granted = await client.get(endpoint, params=case_query, headers={"X-Test-User": "200"})
            self.assertEqual((await granted.json())["result"]["detail"], "Закрытые материалы обращения")
            web_auth_repository.web_set_section_grant(77, 200, "ovr", enabled=False, granted_by_id=100)
            revoked = await client.get(endpoint, params=case_query, headers={"X-Test-User": "200"})
            self.assertIsNone((await revoked.json())["result"])
