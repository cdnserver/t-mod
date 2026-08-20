import gc
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

import storage
from modules.consensus_web import create_consensus_web_app
from modules.consensus_web_auth import ConsensusWebPrincipal
from modules.sgl_forum_publish import SGLForumPublishError
from persistence import sgl_repository


class SGLWebWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "sgl-web-workspace.db"
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
    def _principal() -> ConsensusWebPrincipal:
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
    def _participant_principal() -> ConsensusWebPrincipal:
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

    async def test_detail_exposes_live_workspace_collections(self) -> None:
        sgl_repository.record_sgl_case_message(
            case=self.case,
            origin="discord",
            discord_message_id=800,
            author_id=101,
            author_display="Client",
            author_avatar_url=None,
            author_is_bot=False,
            content="Сообщение из Discord",
        )
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch("modules.consensus_web.resolve_principal", AsyncMock(return_value=self._principal())):
                response = await client.get(
                    f"/api/sgl/cases/{self.case.case_number}",
                    headers={"Host": "sgl.tvr.lat"},
                )
                payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["case"]["case_number"], self.case.case_number)
        self.assertEqual(payload["messages"][0]["content"], "Сообщение из Discord")
        self.assertEqual(payload["permissions"], {"view": True, "write": True, "manage": True})
        self.assertEqual(payload["forum_publications"], [])

    async def test_manager_task_queue_returns_context_and_respects_filters(self) -> None:
        owned = sgl_repository.create_sgl_case_task(
            case=self.case,
            title="Проверить материалы",
            priority="high",
            owner_id=42,
            owner_display="Lawyer",
            created_by_id=42,
            created_by_display="Lawyer",
        )
        sgl_repository.create_sgl_case_task(
            case=self.case,
            title="Задача другого сотрудника",
            priority="normal",
            owner_id=77,
            owner_display="Other",
            created_by_id=42,
            created_by_display="Lawyer",
        )
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch("modules.consensus_web.resolve_principal", AsyncMock(return_value=self._principal())):
                response = await client.get(
                    "/api/sgl/tasks?status=open&owner_id=42",
                    headers={"Host": "sgl.tvr.lat"},
                )
                payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["filters"], {"status": "open", "owner_id": 42})
        self.assertEqual(payload["tasks"][0]["id"], owned["id"])
        self.assertEqual(payload["tasks"][0]["case_number"], self.case.case_number)
        self.assertEqual(payload["tasks"][0]["case_title"], "Client")
        self.assertEqual(payload["tasks"][0]["case_status"], self.case.status)

    async def test_task_queue_is_not_available_to_case_participants(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=self._participant_principal()),
            ):
                response = await client.get(
                    "/api/sgl/tasks",
                    headers={"Host": "sgl.tvr.lat"},
                )
                payload = await response.json()

        self.assertEqual(response.status, 403)
        self.assertEqual(payload["error"], "sgl_management_required")

    async def test_manager_notification_history_filters_are_scoped_and_auditable(self) -> None:
        matched = sgl_repository.create_sgl_case_notification(
            guild_id=77,
            case=self.case,
            kind="forum_changed",
            severity="warning",
            title="Иск изменился",
            source="forum_watch",
            external_status="sent",
        )
        sgl_repository.create_sgl_case_notification(
            guild_id=77,
            case=self.case,
            kind="case_updated",
            severity="info",
            title="Карточка обновлена",
            source="web",
            external_status="not_applicable",
        )
        sgl_repository.acknowledge_sgl_case_notification(
            matched["id"], guild_id=77, actor_id=42, actor_display="Lawyer",
        )
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch("modules.consensus_web.resolve_principal", AsyncMock(return_value=self._principal())):
                response = await client.get(
                    "/api/sgl/notifications?status=acknowledged&severity=warning"
                    f"&case_number={self.case.case_number}&delivery=sent&source=forum_watch",
                    headers={"Host": "sgl.tvr.lat"},
                )
                payload = await response.json()
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=self._participant_principal()),
            ):
                forbidden_response = await client.get(
                    "/api/sgl/notifications?status=all",
                    headers={"Host": "sgl.tvr.lat"},
                )
                forbidden = await forbidden_response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["notifications"][0]["id"], matched["id"])
        self.assertEqual(
            payload["filters"],
            {
                "case_number": self.case.case_number,
                "severity": "warning",
                "status": "acknowledged",
                "delivery": "sent",
                "source": "forum_watch",
            },
        )
        self.assertEqual(forbidden_response.status, 403)
        self.assertEqual(forbidden["error"], "sgl_management_required")

    async def test_forum_operations_exposes_safe_watch_state_and_alert_history(self) -> None:
        alert = sgl_repository.create_sgl_case_notification(
            guild_id=77,
            case=self.case,
            kind="forum_watch_error",
            severity="critical",
            title="Forum Watch требует ручного действия",
            source="forum_watch",
            external_status="failed",
        )
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch("modules.consensus_web.resolve_principal", AsyncMock(return_value=self._principal())):
                response = await client.get(
                    "/api/sgl/forum/operations",
                    headers={"Host": "sgl.tvr.lat"},
                )
                payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["alerts"][0]["id"], alert["id"])
        self.assertEqual(payload["watch"]["unacknowledged_alerts"], 1)
        self.assertIn(payload["watch"]["runner"]["state"], {"disabled", "idle", "checking", "closed"})
        self.assertNotIn("selenium_url", payload["watch"]["runner"])
        self.assertNotIn("cookie_file", payload["watch"]["runner"])

    async def test_forum_draft_is_saved_and_failed_publish_returns_to_retryable_state(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        publisher = Mock()
        publisher.publish.side_effect = SGLForumPublishError("sgl_forum_publish_disabled")
        async with TestClient(TestServer(app)) as client:
            with patch("modules.consensus_web.resolve_principal", AsyncMock(return_value=self._principal())):
                draft_response = await client.post(
                    f"/api/sgl/cases/{self.case.case_number}/forum-publications",
                    headers={"Host": "sgl.tvr.lat", "X-CSRF-Token": "csrf-test-token"},
                    json={"target_url": "https://forum.majestic-rp.ru/forums/court.42/"},
                )
                draft = (await draft_response.json())["publication"]
                with patch("modules.sgl_web.SGLForumPublisher", return_value=publisher):
                    publish_response = await client.post(
                        f"/api/sgl/cases/{self.case.case_number}/forum-publications/{draft['id']}/publish",
                        headers={"Host": "sgl.tvr.lat", "X-CSRF-Token": "csrf-test-token"},
                        json={"expected_updated_at": draft["updated_at"]},
                    )
                    failure = await publish_response.json()

        self.assertEqual(draft_response.status, 201)
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(publish_response.status, 502)
        self.assertEqual(failure["error"], "sgl_forum_publish_failed")
        saved = sgl_repository.get_sgl_case_forum_publication(
            guild_id=77, case_number=self.case.case_number, publication_id=draft["id"]
        )
        assert saved is not None
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
