import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import storage
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from modules.atlas_web import register_atlas_web_routes
from modules.consensus_web_auth import ConsensusWebPrincipal
from persistence import atlas_document_repository, atlas_repository
from persistence.core import connect


class AtlasDocumentWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-documents.db"
        storage.init_db()
        dashboard = atlas_repository.atlas_dashboard(77, 42, "Владелец")
        self.organization_id = int(dashboard["organization"]["id"])
        with connect() as con:
            con.execute(
                """
                INSERT INTO atlas_memberships(
                    organization_id, guild_id, user_id, display_name, role, status,
                    created_at, updated_at
                ) VALUES(?, 77, 84, 'Согласующий', 'member', 'active', ?, ?)
                """,
                (self.organization_id, "2026-08-22T00:00:00+00:00", "2026-08-22T00:00:00+00:00"),
            )
            con.commit()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_revision_comment_and_ordered_approval_lifecycle(self) -> None:
        document = atlas_repository.atlas_create_document(
            self.organization_id,
            42,
            title="Рапорт о происшествии",
            template_id=None,
            fields={"facts": "Исходные обстоятельства"},
            rendered_text="Исходные обстоятельства",
        )
        detail = atlas_document_repository.atlas_document_detail(
            self.organization_id, 42, int(document["id"])
        )
        self.assertEqual([item["revision"] for item in detail["revisions"]], [1])

        comment = atlas_document_repository.atlas_document_add_comment(
            self.organization_id,
            84,
            int(document["id"]),
            body="Нужно уточнить точное время.",
            revision=1,
        )
        approvals = atlas_document_repository.atlas_document_configure_approvals(
            self.organization_id,
            42,
            int(document["id"]),
            steps=[{"title": "Проверка ответственным", "assigned_user_id": 84}],
        )
        reviewed = atlas_document_repository.atlas_document_transition(
            self.organization_id,
            42,
            int(document["id"]),
            status="review",
            expected_revision=1,
        )
        with self.assertRaisesRegex(ValueError, "atlas_document_open_comments|atlas_document_approval_incomplete"):
            atlas_document_repository.atlas_document_transition(
                self.organization_id,
                42,
                int(document["id"]),
                status="approved",
                expected_revision=int(reviewed["revision"]),
            )
        atlas_document_repository.atlas_document_decide_approval(
            self.organization_id,
            84,
            int(document["id"]),
            int(approvals[0]["id"]),
            decision="approved",
            note="Факты сверены.",
        )
        with self.assertRaisesRegex(ValueError, "atlas_document_open_comments"):
            atlas_document_repository.atlas_document_transition(
                self.organization_id,
                42,
                int(document["id"]),
                status="approved",
                expected_revision=1,
            )
        atlas_document_repository.atlas_document_resolve_comment(
            self.organization_id, 84, int(document["id"]), int(comment["id"])
        )
        approved = atlas_document_repository.atlas_document_transition(
            self.organization_id,
            42,
            int(document["id"]),
            status="approved",
            expected_revision=1,
        )
        revised = atlas_document_repository.atlas_document_revise(
            self.organization_id,
            42,
            int(document["id"]),
            title=approved["title"],
            fields={"facts": "Обстоятельства и время уточнены"},
            rendered_text="Обстоятельства и время уточнены",
            change_summary="Уточнено время",
            expected_revision=1,
        )
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["status"], "review")
        after_revision = atlas_document_repository.atlas_document_detail(
            self.organization_id, 42, int(document["id"])
        )
        self.assertEqual([item["revision"] for item in after_revision["revisions"]], [2, 1])
        self.assertEqual(after_revision["approvals"][0]["status"], "pending")
        with self.assertRaisesRegex(ValueError, "atlas_document_revision_conflict"):
            atlas_document_repository.atlas_document_revise(
                self.organization_id,
                42,
                int(document["id"]),
                title=approved["title"],
                fields={},
                rendered_text="stale",
                change_summary="stale",
                expected_revision=1,
            )


class AtlasDocumentWebTests(unittest.IsolatedAsyncioTestCase):
    async def test_document_detail_and_comment_api(self) -> None:
        old_data_dir = storage.DATA_DIR
        old_database_file = storage.DATABASE_FILE
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-document-web.db"
        storage.init_db()
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Администратор",
            csrf_token="document-csrf",
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
        headers = {"X-CSRF-Token": "document-csrf"}
        try:
            async with TestClient(TestServer(app)) as client:
                created = await client.post(
                    "/api/atlas/documents",
                    json={"title": "Служебная записка", "fields": {"content": "Текст"}, "rendered_text": "Текст"},
                    headers=headers,
                )
                created_payload = await created.json()
                document_id = int(created_payload["document"]["id"])
                comment = await client.post(
                    f"/api/atlas/documents/{document_id}/comments",
                    json={"body": "Добавить основание.", "revision": 1},
                    headers=headers,
                )
                comment_payload = await comment.json()
                detail = await client.get(f"/api/atlas/documents/{document_id}")
                detail_payload = await detail.json()

            self.assertEqual(created.status, 201, created_payload)
            self.assertEqual(comment.status, 201, comment_payload)
            self.assertEqual(detail.status, 200, detail_payload)
            self.assertEqual(detail_payload["document"]["revision"], 1)
            self.assertEqual(detail_payload["open_comments"], 1)
            self.assertEqual(detail_payload["revisions"][0]["change_summary"], "Первый черновик")
        finally:
            storage.DATA_DIR = old_data_dir
            storage.DATABASE_FILE = old_database_file
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
