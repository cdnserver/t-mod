import gc
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

import storage
from modules.consensus_web import create_consensus_web_app
from modules.consensus_web_auth import ConsensusWebPrincipal
from persistence import core, schema, sgl_repository


class SGLCaseDecisionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_data_dir, self.old_database_file = core.DATA_DIR, core.DATABASE_FILE
        core.DATA_DIR = Path(self.temp_dir.name)
        core.DATABASE_FILE = core.DATA_DIR / "sgl-decisions.db"
        schema.init_db()
        reserved = sgl_repository.reserve_sgl_case(
            guild_id=88,
            client_id=11,
            client_display="Client",
            lead_lawyer_id=22,
            lead_lawyer_display="Lawyer",
            secretary_id=None,
            secretary_display=None,
            created_by_id=22,
            created_by_display="Lawyer",
        )
        self.case = sgl_repository.attach_sgl_case_channel(reserved.id, 404)
        assert self.case is not None

    def tearDown(self) -> None:
        core.DATA_DIR, core.DATABASE_FILE = self.old_data_dir, self.old_database_file
        gc.collect()
        self.temp_dir.cleanup()

    def test_private_handoff_has_auditable_read_and_acknowledgement_lifecycle(self) -> None:
        decision = sgl_repository.create_sgl_case_decision(
            case=self.case,
            kind="handoff",
            title="Передать проверку срока",
            body="Проверьте дату решения и вернитесь с подтверждённым дедлайном.",
            target_user_id=33,
            target_display="Second lawyer",
            created_by_id=22,
            created_by_display="Lawyer",
        )

        self.assertEqual(decision["status"], "open")
        self.assertEqual(decision["target_user_id"], 33)
        self.assertEqual(decision["created_by_display"], "Lawyer")
        self.assertIsNotNone(decision["created_at"])
        self.assertEqual(
            sgl_repository.list_sgl_case_decisions(self.case.id)[0]["id"],
            decision["id"],
        )

        read = sgl_repository.update_sgl_case_decision_status(
            decision_id=decision["id"],
            case_id=self.case.id,
            guild_id=88,
            status="read",
            actor_id=33,
            actor_display="Second lawyer",
        )
        assert read is not None
        self.assertEqual(read["status"], "read")
        self.assertIsNotNone(read["read_at"])
        self.assertEqual(read["read_by_id"], 33)
        self.assertEqual(read["updated_by_id"], 33)

        acknowledged = sgl_repository.update_sgl_case_decision_status(
            decision_id=decision["id"],
            case_id=self.case.id,
            guild_id=88,
            status="acknowledged",
            actor_id=33,
            actor_display="Second lawyer",
        )
        assert acknowledged is not None
        self.assertEqual(acknowledged["status"], "acknowledged")
        self.assertIsNotNone(acknowledged["acknowledged_at"])
        self.assertEqual(acknowledged["acknowledged_by_display"], "Second lawyer")
        self.assertEqual(
            sgl_repository.list_sgl_case_decisions(self.case.id, include_resolved=False),
            [],
        )

        events = sgl_repository.get_sgl_case_events(self.case.id, 20)
        actions = [event["action"] for event in events]
        self.assertIn("internal_decision_logged", actions)
        self.assertIn("internal_decision_status_updated", actions)
        self.assertNotIn(
            "Проверьте дату решения",
            " ".join(str(event.get("details") or "") for event in events),
        )

    def test_decision_rejects_invalid_kind_status_and_case_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "sgl_case_decision_kind_invalid"):
            sgl_repository.create_sgl_case_decision(
                case=self.case,
                kind="external",
                title="Title",
                body="Body",
                created_by_id=22,
                created_by_display="Lawyer",
            )
        decision = sgl_repository.create_sgl_case_decision(
            case=self.case,
            kind="decision",
            title="Title",
            body="Body",
            created_by_id=22,
            created_by_display="Lawyer",
        )
        with self.assertRaisesRegex(ValueError, "sgl_case_decision_status_invalid"):
            sgl_repository.update_sgl_case_decision_status(
                decision_id=decision["id"],
                case_id=self.case.id,
                guild_id=88,
                status="deleted",
                actor_id=22,
                actor_display="Lawyer",
            )
        self.assertIsNone(
            sgl_repository.update_sgl_case_decision_status(
                decision_id=decision["id"],
                case_id=self.case.id + 1,
                guild_id=88,
                status="read",
                actor_id=22,
                actor_display="Lawyer",
            )
        )


class SGLCaseDecisionWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "sgl-decisions-web.db"
        storage.init_db()
        reserved = sgl_repository.reserve_sgl_case(
            guild_id=77,
            client_id=101,
            client_display="Client",
            lead_lawyer_id=42,
            lead_lawyer_display="Lawyer",
            secretary_id=None,
            secretary_display=None,
            created_by_id=42,
            created_by_display="Lawyer",
        )
        self.case = sgl_repository.attach_sgl_case_channel(reserved.id, 404)
        assert self.case is not None
        self.bot = SimpleNamespace(get_guild=lambda _guild_id: None)

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        gc.collect()
        self.temp_dir.cleanup()

    @staticmethod
    def _manager() -> ConsensusWebPrincipal:
        member = SimpleNamespace(
            id=42,
            display_name="Lawyer",
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[],
        )
        return ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Lawyer",
            csrf_token="csrf-test-token",
            member=member,  # type: ignore[arg-type]
        )

    @staticmethod
    def _participant() -> ConsensusWebPrincipal:
        member = SimpleNamespace(
            id=101,
            display_name="Client",
            guild_permissions=SimpleNamespace(administrator=False),
            roles=[],
        )
        return ConsensusWebPrincipal(
            user_id=101,
            guild_id=77,
            display_name="Client",
            csrf_token="csrf-participant-token",
            member=member,  # type: ignore[arg-type]
        )

    async def test_manager_only_decision_endpoints_and_private_event_filter(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        case_url = f"/api/sgl/cases/{self.case.case_number}"
        async with TestClient(TestServer(app)) as client:
            with patch("modules.consensus_web.resolve_principal", AsyncMock(return_value=self._manager())):
                created_response = await client.post(
                    f"{case_url}/decisions",
                    headers={"Host": "sgl.tvr.lat", "X-CSRF-Token": "csrf-test-token"},
                    json={
                        "kind": "risk",
                        "title": "Проверить срок",
                        "body": "Не публиковать проект до проверки процессуального срока.",
                    },
                )
                created = await created_response.json()
                listed_response = await client.get(
                    f"{case_url}/decisions?resolved=0",
                    headers={"Host": "sgl.tvr.lat"},
                )
                listed = await listed_response.json()
                updated_response = await client.patch(
                    f"{case_url}/decisions/{created['decision']['id']}",
                    headers={"Host": "sgl.tvr.lat", "X-CSRF-Token": "csrf-test-token"},
                    json={"status": "acknowledged"},
                )
                updated = await updated_response.json()

            with patch("modules.consensus_web.resolve_principal", AsyncMock(return_value=self._participant())):
                denied_response = await client.get(
                    f"{case_url}/decisions",
                    headers={"Host": "sgl.tvr.lat"},
                )
                denied = await denied_response.json()
                detail_response = await client.get(case_url, headers={"Host": "sgl.tvr.lat"})
                detail = await detail_response.json()

        self.assertEqual(created_response.status, 201)
        self.assertEqual(created["decision"]["kind"], "risk")
        self.assertEqual(listed_response.status, 200)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(updated_response.status, 200)
        self.assertEqual(updated["decision"]["status"], "acknowledged")
        self.assertIsNotNone(updated["decision"]["read_at"])
        self.assertIsNotNone(updated["decision"]["acknowledged_at"])
        self.assertEqual(denied_response.status, 403)
        self.assertEqual(denied["error"], "sgl_management_required")
        self.assertEqual(detail_response.status, 200)
        self.assertFalse(
            any(str(event["action"]).startswith("internal_") for event in detail["events"])
        )


if __name__ == "__main__":
    unittest.main()
