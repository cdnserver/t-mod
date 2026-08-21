import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import storage
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from modules.atlas_web import register_atlas_web_routes
from modules.consensus_web_auth import ConsensusWebPrincipal
from persistence import atlas_case_repository, atlas_repository, atlas_search_repository
from persistence.core import connect


class AtlasSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-search.db"
        storage.init_db()
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Владелец")
        self.organization_id = int(dashboard["organization"]["id"])
        with connect() as con:
            con.execute(
                """
                INSERT INTO atlas_memberships(
                    organization_id, guild_id, user_id, display_name, role, status,
                    created_at, updated_at
                ) VALUES(?, 77, 84, 'Участник', 'member', 'active', ?, ?)
                """,
                (self.organization_id, "2026-08-22T00:00:00+00:00", "2026-08-22T00:00:00+00:00"),
            )
            con.commit()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_search_combines_modules_and_preserves_private_cases(self) -> None:
        atlas_case_repository.atlas_case_create(
            self.organization_id,
            42,
            title="Скрытая проверка штаба",
            visibility_scope="private",
        )
        atlas_repository.atlas_create_document(
            self.organization_id,
            42,
            title="Рапорт по штабу",
            template_id=None,
            fields={"content": "Проверка территории"},
            rendered_text="Проверка территории центрального штаба",
        )
        atlas_repository.atlas_create_timeline_event(
            self.organization_id,
            42,
            title="Выезд к штабу",
            summary="Патруль прибыл для проверки.",
        )
        atlas_repository.atlas_add_knowledge(
            self.organization_id,
            42,
            title="Регламент охраны штаба",
            content="Порядок доступа на территорию центрального штаба.",
        )
        owner = atlas_search_repository.atlas_global_search(
            self.organization_id, 42, query="штаб"
        )
        self.assertEqual(
            {item["kind"] for item in owner["items"]},
            {"case", "document", "timeline_event", "knowledge_source"},
        )
        member = atlas_search_repository.atlas_global_search(
            self.organization_id, 84, query="Скрытая проверка"
        )
        self.assertEqual(member["items"], [])
        exact = atlas_search_repository.atlas_global_search(
            self.organization_id, 42, query="Скрытая проверка штаба"
        )
        self.assertEqual(exact["items"][0]["kind"], "case")

    def test_short_query_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "atlas_search_query_too_short"):
            atlas_search_repository.atlas_global_search(
                self.organization_id, 42, query="а"
            )


class AtlasSearchWebTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_endpoint_returns_navigation_target(self) -> None:
        old_data_dir = storage.DATA_DIR
        old_database_file = storage.DATABASE_FILE
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-search-web.db"
        storage.init_db()
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Администратор")
        atlas_repository.atlas_create_document(
            int(dashboard["organization"]["id"]),
            42,
            title="План дежурства",
            template_id=None,
            fields={},
            rendered_text="Дежурство начинается в восемь часов.",
        )
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Администратор",
            csrf_token="search-csrf",
            member=SimpleNamespace(
                id=42,
                display_name="Администратор",
                guild_permissions=SimpleNamespace(administrator=True),
                roles=[],
            ),
        )

        async def authenticate(_request):
            return selected, False

        app = web.Application()
        register_atlas_web_routes(
            app,
            SimpleNamespace(get_guild=lambda guild_id: None),
            guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
            authenticate=authenticate,
        )
        try:
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/api/atlas/search?q=дежурство")
                payload = await response.json()
            self.assertEqual(response.status, 200, payload)
            self.assertEqual(payload["items"][0]["kind"], "document")
            self.assertEqual(payload["items"][0]["screen"], "documents")
        finally:
            storage.DATA_DIR = old_data_dir
            storage.DATABASE_FILE = old_database_file
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
