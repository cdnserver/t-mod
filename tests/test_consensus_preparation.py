import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from pypdf import PdfReader

import storage
from modules.consensus_preparation_artifacts import generate_preparation_pdf
from modules.consensus_web import create_consensus_web_app
from modules.consensus_web_auth import ConsensusWebPrincipal


class _PreparationStorageCase(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "preparation-test.db"
        storage.init_db()
        self.bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=88,
            author_id=5,
            author_display="Автор",
            title="О подготовке к консенсусу",
            summary="Текст проекта для личного рабочего листа.",
            materials="https://example.com/material",
        )

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_private_sheet_is_revisioned_and_never_creates_an_official_vote(self) -> None:
        blank = storage.get_preparation_sheet(77, self.bill.id, 41)
        self.assertEqual(blank["revision"], 0)
        self.assertEqual(blank["questions"], [])

        saved = storage.save_preparation_sheet(
            77,
            self.bill.id,
            41,
            user_display="Сенатор Тестовый",
            expected_revision=0,
            questions=[
                {
                    "id": "question-1",
                    "text": "Какие ограничения предусмотрены?",
                    "resolved": False,
                }
            ],
            notes="Проверить механизм контроля и сроки.",
            preliminary_vote="yes",
            preliminary_vote_reason="Позиция положительная после ответа на вопрос.",
            review_flags={"read_text": True, "verify_sources": False, "need_discussion": True},
            source_bill_updated_at=self.bill.updated_at,
        )
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["preliminary_vote"], "yes")
        self.assertEqual(saved["questions"][0]["text"], "Какие ограничения предусмотрены?")
        self.assertEqual(storage.tvrs_votes_for_bill(77, self.bill.id), [])

        other_person = storage.get_preparation_sheet(77, self.bill.id, 42)
        self.assertEqual(other_person["revision"], 0)
        self.assertEqual(other_person["notes"], "")

        with self.assertRaises(storage.PreparationRevisionConflict):
            storage.save_preparation_sheet(
                77,
                self.bill.id,
                41,
                user_display="Сенатор Тестовый",
                expected_revision=0,
                questions=[],
                notes="Устаревшая запись",
                preliminary_vote=None,
                preliminary_vote_reason="",
                review_flags={},
                source_bill_updated_at=self.bill.updated_at,
            )

    def test_bill_deletion_cascades_private_sheets(self) -> None:
        storage.save_preparation_sheet(
            77,
            self.bill.id,
            41,
            user_display="Сенатор Тестовый",
            expected_revision=0,
            questions=[],
            notes="Личная заметка",
            preliminary_vote=None,
            preliminary_vote_reason="",
            review_flags={},
            source_bill_updated_at=self.bill.updated_at,
        )
        deleted = storage.tvrs_delete_bill_by_number(77, self.bill.bill_number)
        self.assertIsNotNone(deleted)
        self.assertEqual(
            storage.get_preparation_sheet(77, self.bill.id, 41)["revision"],
            0,
        )


class ConsensusPreparationArtifactTests(unittest.TestCase):
    def test_pdf_keeps_cyrillic_working_fields_and_native_controls(self) -> None:
        data = generate_preparation_pdf(
            {
                "id": 18,
                "bill_number": 118,
                "title": "О прозрачной подготовке законопроектов",
                "summary": "Полный русский текст проекта. " * 45,
                "materials": "https://example.com/материалы",
                "decision_category": "significant",
                "author": {"name": "Автор Тестовый"},
            },
            {
                "questions": [
                    {
                        "id": "q-1",
                        "text": "Какие гарантии предусмотрены для участников?",
                        "resolved": False,
                    }
                ],
                "notes": "Проверить публичные отчёты и срок исполнения.",
                "preliminary_vote": "yes",
                "preliminary_vote_reason": "Поддержать после ответа на вопрос.",
                "review_flags": {"read_text": True},
            },
            "Сенатор Тестовый",
        )
        self.assertTrue(data.startswith(b"%PDF"))
        reader = PdfReader(BytesIO(data))
        self.assertGreaterEqual(len(reader.pages), 3)
        fields = reader.get_fields() or {}
        self.assertTrue(
            {"prep_questions", "prep_notes", "prep_vote_reason", "prep_vote"}.issubset(fields)
        )
        self.assertEqual(fields["prep_notes"]["/V"], "Проверить публичные отчёты и срок исполнения.")
        self.assertIn("Какие гарантии", fields["prep_questions"]["/V"])
        widgets = [
            annotation.get_object()
            for page in reader.pages
            for annotation in (page.get("/Annots") or [])
            if str(annotation.get_object().get("/FT") or "") == "/Tx"
        ]
        self.assertEqual(len(widgets), 3)
        self.assertTrue(all(widget.get("/AP", {}).get("/N") for widget in widgets))


class ConsensusPreparationWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "preparation-web-test.db"
        storage.init_db()
        self.bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=88,
            author_id=5,
            author_display="Автор",
            title="Личный лист подготовки",
            summary="Текст законопроекта для веб-проверки.",
            materials="https://example.com/material",
        )
        self.bot = SimpleNamespace(
            get_guild=lambda guild_id: (
                SimpleNamespace(id=77, name="Товарищество") if guild_id == 77 else None
            )
        )

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    @staticmethod
    def _principal(user_id: int = 41) -> ConsensusWebPrincipal:
        member = SimpleNamespace(
            id=user_id,
            display_name=f"Сенатор {user_id}",
            guild_permissions=SimpleNamespace(administrator=False),
            roles=[],
        )
        return ConsensusWebPrincipal(
            user_id=user_id,
            guild_id=77,
            display_name=member.display_name,
            csrf_token="csrf-preparation-test",
            member=member,  # type: ignore[arg-type]
        )

    async def test_personal_sheet_requires_csrf_is_isolated_and_exports_pdf(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        owner = self._principal(41)
        try:
            with patch("modules.consensus_web.resolve_principal", AsyncMock(return_value=owner)):
                opened = await client.get(f"/api/bills/{self.bill.id}/preparation")
                self.assertEqual(opened.status, 200)
                self.assertEqual((await opened.json())["sheet"]["revision"], 0)

                csrf_denied = await client.post(
                    f"/api/bills/{self.bill.id}/preparation",
                    json={},
                )
                self.assertEqual(csrf_denied.status, 403)

                body = {
                    "expected_revision": 0,
                    "questions": [{"id": "q-1", "text": "Что изменится?", "resolved": False}],
                    "notes": "Личные заметки первого сенатора.",
                    "preliminary_vote": "yes",
                    "preliminary_vote_reason": "Есть основания поддержать.",
                    "review_flags": {"read_text": True},
                }
                saved = await client.post(
                    f"/api/bills/{self.bill.id}/preparation",
                    headers={"X-CSRF-Token": "csrf-preparation-test"},
                    json=body,
                )
                self.assertEqual(saved.status, 200)
                saved_payload = await saved.json()
                self.assertEqual(saved_payload["sheet"]["revision"], 1)
                self.assertEqual(storage.tvrs_votes_for_bill(77, self.bill.id), [])

                stale = await client.post(
                    f"/api/bills/{self.bill.id}/preparation",
                    headers={"X-CSRF-Token": "csrf-preparation-test"},
                    json=body,
                )
                self.assertEqual(stale.status, 409)

                pdf = await client.get(f"/api/bills/{self.bill.id}/preparation.pdf")
                self.assertEqual(pdf.status, 200)
                self.assertEqual(pdf.headers.get("Cache-Control"), "private, no-store")
                pdf_bytes = await pdf.read()
                self.assertTrue(pdf_bytes.startswith(b"%PDF"))
                fields = PdfReader(BytesIO(pdf_bytes)).get_fields() or {}
                self.assertEqual(fields["prep_notes"]["/V"], body["notes"])

            other = self._principal(42)
            with patch("modules.consensus_web.resolve_principal", AsyncMock(return_value=other)):
                isolated = await client.get(f"/api/bills/{self.bill.id}/preparation")
                self.assertEqual(isolated.status, 200)
                self.assertEqual((await isolated.json())["sheet"]["notes"], "")
        finally:
            await client.close()

    async def test_legacy_shared_key_cannot_open_personal_sheet(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with patch("modules.consensus_web._runtime_token", "test-access-token-123456"):
                denied = await client.get(
                    f"/api/bills/{self.bill.id}/preparation",
                    headers={"Authorization": "Bearer test-access-token-123456"},
                )
            self.assertEqual(denied.status, 403)
            self.assertEqual((await denied.json())["error"], "personal_login_required")
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
