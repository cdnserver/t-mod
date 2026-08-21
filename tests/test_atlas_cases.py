import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import storage
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from modules.atlas_web import register_atlas_web_routes
from modules.consensus_web_auth import ConsensusWebPrincipal
from persistence import atlas_case_repository, atlas_repository
from persistence.core import connect


class AtlasCaseRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-cases.db"
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

    def test_case_readiness_requires_verified_evidence_and_is_versioned(self) -> None:
        case = atlas_case_repository.atlas_case_create(
            self.organization_id,
            42,
            title="Конфликт у штаба",
            case_kind="incident",
            priority="critical",
            visibility_scope="private",
            objective="Установить последовательность событий.",
        )
        claim = atlas_case_repository.atlas_case_add_claim(
            self.organization_id,
            42,
            int(case["id"]),
            statement="Сотрудник первым потребовал покинуть территорию.",
            importance="critical",
        )
        detail = atlas_case_repository.atlas_case_detail(
            self.organization_id, 42, int(case["id"])
        )
        self.assertEqual(detail["readiness"]["state"], "attention")
        with self.assertRaisesRegex(ValueError, "atlas_case_not_ready"):
            atlas_case_repository.atlas_case_set_status(
                self.organization_id,
                42,
                int(case["id"]),
                status="ready",
                expected_version=int(detail["case"]["version"]),
            )

        event = atlas_repository.atlas_create_timeline_event(
            self.organization_id,
            42,
            title="Зафиксировано требование",
            event_kind="incident",
        )
        evidence = atlas_case_repository.atlas_case_add_evidence(
            self.organization_id,
            42,
            int(case["id"]),
            claim_id=int(claim["id"]),
            source_type="timeline_event",
            source_id=event["id"],
            title="Событие в Atlas Memory",
            relevance="Подтверждает момент требования.",
            locator={"start_ms": 32000, "end_ms": 47000},
            provenance={"source_type": "forged", "operator_note": "original"},
        )
        case_version_before_repeat = atlas_case_repository.atlas_case_detail(
            self.organization_id, 42, int(case["id"])
        )["case"]["version"]
        repeated = atlas_case_repository.atlas_case_add_evidence(
            self.organization_id,
            42,
            int(case["id"]),
            claim_id=int(claim["id"]),
            source_type="timeline_event",
            source_id=event["id"],
            title="Уточнённое название",
            locator={"start_ms": 32000, "end_ms": 47000},
        )
        self.assertEqual(evidence["id"], repeated["id"])
        self.assertEqual(evidence["version"], repeated["version"])
        self.assertEqual(repeated["provenance"]["source_type"], "timeline_event")
        self.assertEqual(
            atlas_case_repository.atlas_case_detail(
                self.organization_id, 42, int(case["id"])
            )["case"]["version"],
            case_version_before_repeat,
        )
        reviewed = atlas_case_repository.atlas_case_review_evidence(
            self.organization_id,
            42,
            int(case["id"]),
            int(evidence["id"]),
            verification_status="verified",
            admissibility="admissible",
            expected_version=int(repeated["version"]),
        )
        self.assertEqual(reviewed["verification_status"], "verified")
        evidence_only = atlas_case_repository.atlas_case_detail(
            self.organization_id, 42, int(case["id"])
        )
        self.assertNotEqual(evidence_only["readiness"]["state"], "ready")
        atlas_case_repository.atlas_case_update_claim(
            self.organization_id,
            42,
            int(case["id"]),
            int(claim["id"]),
            claim_status="supported",
            rationale="Подтверждено проверенным материалом.",
            expected_version=int(claim["version"]),
        )
        detail = atlas_case_repository.atlas_case_detail(
            self.organization_id, 42, int(case["id"])
        )
        self.assertEqual(detail["readiness"]["score"], 100)
        ready = atlas_case_repository.atlas_case_set_status(
            self.organization_id,
            42,
            int(case["id"]),
            status="ready",
            expected_version=int(detail["case"]["version"]),
        )
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(atlas_case_repository.atlas_cases(self.organization_id, 84), [])
        self.assertTrue(
            any(link["source_type"] == "case_evidence" for link in atlas_repository.atlas_entity_links(
                self.organization_id, "timeline_event", event["id"]
            ))
        )

    def test_empty_case_cannot_be_marked_ready(self) -> None:
        case = atlas_case_repository.atlas_case_create(
            self.organization_id, 42, title="Пустая проверка"
        )
        detail = atlas_case_repository.atlas_case_detail(
            self.organization_id, 42, int(case["id"])
        )
        self.assertEqual(detail["readiness"]["score"], 0)
        self.assertEqual(detail["readiness"]["state"], "not_ready")
        with self.assertRaisesRegex(ValueError, "atlas_case_not_ready"):
            atlas_case_repository.atlas_case_set_status(
                self.organization_id,
                42,
                int(case["id"]),
                status="ready",
                expected_version=int(detail["case"]["version"]),
            )

    def test_findings_remain_bound_to_case_version(self) -> None:
        case = atlas_case_repository.atlas_case_create(
            self.organization_id, 42, title="Проверка противоречий"
        )
        finding = atlas_case_repository.atlas_case_record_finding(
            self.organization_id,
            42,
            int(case["id"]),
            analysis_type="contradiction",
            title="Первичная проверка",
            summary="Противоречий не найдено.",
            result={"contradictions": []},
            status="final",
        )
        self.assertEqual(finding["source_version"], case["version"])
        self.assertEqual(
            atlas_case_repository.atlas_case_detail(
                self.organization_id, 42, int(case["id"])
            )["findings"][0]["result"],
            {"contradictions": []},
        )


class AtlasCaseWebTests(unittest.IsolatedAsyncioTestCase):
    async def test_case_api_builds_claim_and_evidence_portal(self) -> None:
        old_data_dir = storage.DATA_DIR
        old_database_file = storage.DATABASE_FILE
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-case-web.db"
        storage.init_db()
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Администратор",
            csrf_token="case-csrf",
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
        headers = {"X-CSRF-Token": "case-csrf"}
        try:
            async with TestClient(TestServer(app)) as client:
                created = await client.post(
                    "/api/atlas/cases",
                    json={
                        "title": "Материал обращения",
                        "case_kind": "legal",
                        "objective": "Собрать проверяемую позицию.",
                    },
                    headers=headers,
                )
                created_payload = await created.json()
                case_id = int(created_payload["case"]["id"])
                claim = await client.post(
                    f"/api/atlas/cases/{case_id}/claims",
                    json={"statement": "Обращение подано своевременно.", "importance": "material"},
                    headers=headers,
                )
                note = await client.post(
                    f"/api/atlas/cases/{case_id}/evidence",
                    json={
                        "source_type": "note",
                        "title": "Пояснение заявителя",
                        "summary": "Исходная версия событий.",
                        "claim_id": int((await claim.json())["claim"]["id"]),
                    },
                    headers=headers,
                )
                note_payload = await note.json()
                detail = await client.get(f"/api/atlas/cases/{case_id}")
                detail_payload = await detail.json()
                listed = await client.get("/api/atlas/cases")
                listed_payload = await listed.json()

            self.assertEqual(created.status, 201, created_payload)
            self.assertEqual(claim.status, 201)
            self.assertEqual(note.status, 201, note_payload)
            self.assertEqual(len(detail_payload["claims"]), 1)
            self.assertEqual(len(detail_payload["evidence"]), 1)
            self.assertEqual(listed_payload["items"][0]["case_number"], 1)
        finally:
            storage.DATA_DIR = old_data_dir
            storage.DATABASE_FILE = old_database_file
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
