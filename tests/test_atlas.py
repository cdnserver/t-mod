import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import storage
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from docx import Document
from modules.atlas_ai import AtlasAIError, _chunks, atlas_ensure_collection, atlas_search
from modules.atlas_knowledge import AtlasKnowledgeFileError, atlas_extract_knowledge_file
from modules.atlas_web import register_atlas_web_routes
from modules.consensus_web import create_consensus_web_app
from modules.consensus_web_auth import ConsensusWebPrincipal
from persistence import atlas_repository
from persistence.core import connect


class AtlasRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_dashboard_is_idempotent_and_seeds_one_template_version(self) -> None:
        first = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        second = atlas_repository.atlas_dashboard(77, 42, "Пользователь")

        self.assertEqual(first["organization"]["id"], second["organization"]["id"])
        self.assertEqual(first["membership"]["role"], "owner")
        self.assertEqual(len(second["templates"]), 3)
        with connect() as con:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM atlas_document_templates").fetchone()[0],
                3,
            )

    def test_onboarding_and_document_are_persisted(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        organization_id = int(dashboard["organization"]["id"])
        membership = atlas_repository.atlas_update_onboarding(
            organization_id,
            42,
            step=4,
            profile={"agency": "SGL", "position": "Адвокат"},
        )
        document = atlas_repository.atlas_create_document(
            organization_id,
            42,
            title="Рапорт №1",
            template_id=int(dashboard["templates"][0]["id"]),
            fields={"facts": "Проверено"},
        )

        self.assertEqual(membership["onboarding_step"], 4)
        self.assertEqual(membership["profile"]["agency"], "SGL")
        self.assertEqual(document["title"], "Рапорт №1")
        snapshot = atlas_repository.atlas_admin_snapshot(77)
        self.assertEqual(snapshot["totals"]["documents"], 1)
        self.assertEqual(snapshot["recent_events"][0]["event_type"], "document_created")
        with self.assertRaisesRegex(ValueError, "atlas_document_template_forbidden"):
            atlas_repository.atlas_create_document(
                organization_id,
                42,
                title="Документ с чужим шаблоном",
                template_id=999999,
                fields={},
            )

    def test_admin_snapshot_does_not_mix_guilds(self) -> None:
        own = atlas_repository.atlas_dashboard(77, 42, "Первый")
        foreign = atlas_repository.atlas_dashboard(88, 84, "Второй")
        atlas_repository.atlas_create_document(
            int(own["organization"]["id"]), 42, title="Свой", template_id=None, fields={}
        )
        atlas_repository.atlas_create_document(
            int(foreign["organization"]["id"]), 84, title="Чужой", template_id=None, fields={}
        )

        snapshot = atlas_repository.atlas_admin_snapshot(77)
        self.assertEqual(snapshot["totals"]["organizations"], 1)
        self.assertEqual(snapshot["totals"]["documents"], 1)
        self.assertEqual({item["summary"] for item in snapshot["recent_events"]}, {"Создан документ «Свой»"})

    def test_knowledge_requires_editor_membership_and_deduplicates(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        organization_id = int(dashboard["organization"]["id"])
        first = atlas_repository.atlas_add_knowledge(
            organization_id,
            42,
            title="Регламент",
            content="Проверенный текст внутреннего регламента длиной больше двадцати символов.",
            source_kind="regulation",
        )
        second = atlas_repository.atlas_add_knowledge(
            organization_id,
            42,
            title="Регламент — новая подпись",
            content="Проверенный текст внутреннего регламента длиной больше двадцати символов.",
            source_kind="regulation",
        )

        self.assertEqual(first["id"], second["id"])
        with self.assertRaisesRegex(ValueError, "atlas_knowledge_forbidden"):
            atlas_repository.atlas_add_knowledge(
                organization_id,
                999,
                title="Чужой источник",
                content="Этот пользователь не состоит в выбранной организации Atlas.",
            )

    def test_knowledge_is_separated_by_server_and_faction(self) -> None:
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Пользователь")
        organization_id = int(dashboard["organization"]["id"])
        text = "Один и тот же общий текст может действовать в разных государственных структурах."
        lspd = atlas_repository.atlas_add_knowledge(
            organization_id,
            42,
            title="LSPD",
            content=text,
            server_code="phoenix-15",
            faction_code="lspd",
        )
        gov = atlas_repository.atlas_add_knowledge(
            organization_id,
            42,
            title="GOV",
            content=text,
            server_code="phoenix-15",
            faction_code="gov",
        )

        self.assertNotEqual(lspd["id"], gov["id"])
        self.assertEqual(
            [item["id"] for item in atlas_repository.atlas_knowledge_sources(
                organization_id, server_code="phoenix-15", faction_code="gov"
            )],
            [gov["id"]],
        )
        with self.assertRaisesRegex(ValueError, "atlas_faction_invalid"):
            atlas_repository.atlas_normalize_scope("phoenix-15", "unknown")


class AtlasAITests(unittest.IsolatedAsyncioTestCase):
    def test_chunker_is_bounded_and_preserves_overlap(self) -> None:
        chunks = _chunks("слово " * 4000, size=1000, overlap=100)
        self.assertGreater(len(chunks), 2)
        self.assertLessEqual(len(chunks), 48)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))

    async def test_search_always_applies_organization_filter(self) -> None:
        response = {
            "result": {
                "points": [
                    {
                        "score": 0.91,
                        "payload": {"organization_id": 77, "source_id": 4, "title": "Регламент", "text": "Текст"},
                    }
                ]
            }
        }
        with patch("modules.atlas_ai.atlas_embed", AsyncMock(return_value=[[0.1, 0.2]])), patch(
            "modules.atlas_ai._json_request", AsyncMock(return_value=response)
        ) as request:
            result = await atlas_search(77, "полномочия")

        payload = request.await_args.kwargs["payload"]
        self.assertEqual(
            payload["filter"]["must"][0],
            {"key": "organization_id", "match": {"value": 77}},
        )
        self.assertEqual(result[0]["source_id"], 4)

    async def test_search_applies_server_and_faction_filters(self) -> None:
        with patch("modules.atlas_ai.atlas_embed", AsyncMock(return_value=[[0.1, 0.2]])), patch(
            "modules.atlas_ai._json_request", AsyncMock(return_value={"result": {"points": []}})
        ) as request:
            await atlas_search(
                77,
                "порядок задержания",
                server_code="phoenix-15",
                faction_code="lspd",
            )

        self.assertEqual(
            request.await_args.kwargs["payload"]["filter"]["must"],
            [
                {"key": "organization_id", "match": {"value": 77}},
                {"key": "server_code", "match": {"value": "phoenix-15"}},
                {"key": "faction_code", "match": {"value": "lspd"}},
            ],
        )

    async def test_collection_is_created_only_when_missing(self) -> None:
        missing = AtlasAIError("upstream_not_found", "missing")
        request = AsyncMock(side_effect=[missing, {"result": True}])
        with patch("modules.atlas_ai._json_request", request):
            await atlas_ensure_collection(1536)
        self.assertEqual(request.await_count, 2)
        self.assertEqual(request.await_args.kwargs["payload"]["vectors"]["size"], 1536)

        denied = AsyncMock(side_effect=AtlasAIError("upstream_error", "denied"))
        with patch("modules.atlas_ai._json_request", denied), self.assertRaises(AtlasAIError):
            await atlas_ensure_collection(1536)
        self.assertEqual(denied.await_count, 1)


class AtlasKnowledgeFileTests(unittest.TestCase):
    def test_plain_text_and_docx_are_extracted(self) -> None:
        plain = atlas_extract_knowledge_file(
            "Правила.md",
            "Проверенный материал для базы знаний Atlas длиной больше двадцати символов.".encode(),
        )
        document = Document()
        document.add_heading("Устав GOV", level=1)
        document.add_paragraph("Проверенный порядок работы государственного органа.")
        stream = io.BytesIO()
        document.save(stream)
        docx = atlas_extract_knowledge_file("Устав.docx", stream.getvalue())

        self.assertEqual(plain["title"], "Правила")
        self.assertIn("Устав GOV", docx["content"])
        self.assertIn("государственного органа", docx["content"])

    def test_unsupported_and_empty_files_are_rejected(self) -> None:
        with self.assertRaisesRegex(AtlasKnowledgeFileError, "Поддерживаются"):
            atlas_extract_knowledge_file("archive.zip", b"content")
        with self.assertRaisesRegex(AtlasKnowledgeFileError, "пуст"):
            atlas_extract_knowledge_file("empty.txt", b"")


class AtlasWebSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_atlas_surface_is_registered_and_api_requires_login(self) -> None:
        bot = SimpleNamespace(get_guild=lambda guild_id: None)
        app = create_consensus_web_app(bot, guild_id=77)
        async with TestClient(TestServer(app)) as client:
            page = await client.get("/atlas")
            bootstrap = await client.get("/api/atlas/bootstrap")

            self.assertEqual(page.status, 200)
            self.assertIn("T-Mod Atlas", await page.text())
            self.assertEqual(bootstrap.status, 401)
            self.assertIn((await bootstrap.json())["error"], {"unauthorized", "atlas_login_required"})

    async def test_non_admin_receives_only_closed_preview(self) -> None:
        member = SimpleNamespace(
            id=42,
            display_name="Участник",
            guild_permissions=SimpleNamespace(administrator=False),
            roles=[],
        )
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Участник",
            csrf_token="preview-csrf",
            member=member,
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
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/atlas/bootstrap")
            payload = await response.json()
            forbidden = await client.post(
                "/api/atlas/documents",
                json={"title": "Скрытый документ"},
                headers={"X-CSRF-Token": "preview-csrf"},
            )

        self.assertTrue(payload["preview"])
        self.assertEqual(payload["catalog"]["servers"][0]["label"], "Phoenix (15)")
        self.assertEqual(
            {item["code"] for item in payload["catalog"]["factions"]},
            {"lspd", "gov"},
        )
        self.assertNotIn("documents", payload)
        self.assertEqual(forbidden.status, 403)

    async def test_admin_can_upload_scoped_knowledge_file(self) -> None:
        old_data_dir = storage.DATA_DIR
        old_database_file = storage.DATABASE_FILE
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-web-test.db"
        storage.init_db()
        member = SimpleNamespace(
            id=42,
            display_name="Администратор",
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[],
        )
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Администратор",
            csrf_token="admin-csrf",
            member=member,
        )

        async def authenticate(_request):
            return selected, False

        app = web.Application(client_max_size=10 * 1024 * 1024)
        register_atlas_web_routes(
            app,
            SimpleNamespace(get_guild=lambda guild_id: None),
            guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
            authenticate=authenticate,
        )
        form = FormData()
        form.add_field("server_code", "phoenix-15")
        form.add_field("faction_code", "gov")
        form.add_field("source_kind", "regulation")
        form.add_field(
            "file",
            "Проверенный регламент Government для Atlas AI длиной больше двадцати символов.".encode(),
            filename="Регламент GOV.txt",
            content_type="text/plain",
        )
        try:
            with patch("modules.atlas_web.atlas_index_source", AsyncMock(return_value=["point-1"])):
                async with TestClient(TestServer(app)) as client:
                    uploaded = await client.post(
                        "/api/atlas/knowledge/upload",
                        data=form,
                        headers={"X-CSRF-Token": "admin-csrf", "X-Idempotency-Key": "upload-1"},
                    )
                    self.assertEqual(uploaded.status, 202, await uploaded.text())
                    await asyncio.sleep(0.05)
                    listed = await client.get(
                        "/api/atlas/knowledge?server_code=phoenix-15&faction_code=gov"
                    )
                    payload = await listed.json()
            self.assertEqual(payload["items"][0]["faction_code"], "gov")
            self.assertEqual(payload["items"][0]["original_filename"], "Регламент GOV.txt")
            self.assertEqual(payload["items"][0]["status"], "indexed")
        finally:
            storage.DATA_DIR = old_data_dir
            storage.DATABASE_FILE = old_database_file
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
