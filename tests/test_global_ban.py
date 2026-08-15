import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

import storage
from modules.consensus_admin_web import _reconcile_global_bans_once
from modules.consensus_web import create_consensus_web_app
from modules.consensus_web_auth import ConsensusWebPrincipal
from persistence import global_ban_repository as bans
from persistence import web_auth_repository as credentials


class GlobalBanRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "global-ban-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_issue_invalidates_sessions_and_revoke_preserves_audit(self) -> None:
        credential = credentials.configure_web_credential(
            10, 20, "blocked.user", "12345678"
        )
        issued = bans.issue_global_ban(
            10,
            20,
            reason="Подтверждённое решение администратора",
            actor_id=99,
            actor_display="Администратор",
        )
        self.assertTrue(issued["active"])
        self.assertTrue(bans.is_globally_banned(10, 20))
        self.assertFalse(
            credentials.web_session_version_matches(
                10, 20, credential.session_version
            )
        )
        bans.set_global_ban_discord_state(
            10,
            20,
            state="banned",
            error=None,
            actor_id=99,
            actor_display="Администратор",
        )
        revoked = bans.revoke_global_ban(
            10,
            20,
            reason="Решение отменено",
            actor_id=99,
            actor_display="Администратор",
        )
        self.assertFalse(revoked["active"])
        self.assertFalse(bans.is_globally_banned(10, 20))
        self.assertEqual(len(bans.list_global_bans(10)), 1)


class GlobalBanWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "global-ban-web-test.db"
        storage.init_db()
        self.member = SimpleNamespace(
            id=20,
            display_name="Участник",
            guild_permissions=SimpleNamespace(administrator=False),
            roles=[],
        )
        self.principal = ConsensusWebPrincipal(
            user_id=20,
            guild_id=10,
            display_name="Участник",
            csrf_token="csrf-token",
            member=self.member,  # type: ignore[arg-type]
        )
        self.bot = SimpleNamespace(
            get_guild=lambda guild_id: SimpleNamespace(id=guild_id),
            is_ready=lambda: True,
        )

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    async def test_banned_identity_sees_lock_screen_and_api_is_closed(self) -> None:
        bans.issue_global_ban(
            10,
            20,
            reason="Критическое нарушение правил",
            actor_id=99,
            actor_display="Администратор",
        )
        app = create_consensus_web_app(self.bot, guild_id=10)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch(
                "modules.consensus_web.signed_session_identity",
                return_value=(10, 20),
            ):
                protected = await client.get("/api/state")
                page = await client.get("/banned")
                state = await client.get("/api/banned")
                payload = await state.json()
        self.assertEqual(protected.status, 423)
        self.assertEqual(page.status, 200)
        self.assertTrue(payload["active"])
        self.assertEqual(payload["reason"], "Критическое нарушение правил")

    async def test_consensus_task_board_requires_member_and_supports_updates(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=10)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            denied = await client.get("/api/tasks")
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=self.principal),
            ):
                created = await client.post(
                    "/api/tasks",
                    headers={"X-CSRF-Token": "csrf-token"},
                    json={
                        "action": "create",
                        "title": "Подготовить памятку",
                        "description": "Описать принятое решение для участников.",
                        "priority": "high",
                    },
                )
                created_payload = await created.json()
                board = await client.get("/api/tasks")
                board_payload = await board.json()
                task = created_payload["task"]
                updated = await client.post(
                    "/api/tasks",
                    headers={"X-CSRF-Token": "csrf-token"},
                    json={
                        "action": "update",
                        "task_id": task["id"],
                        "expected_revision": task["revision"],
                        "status": "in_progress",
                    },
                )
                updated_payload = await updated.json()
        self.assertEqual(denied.status, 401)
        self.assertEqual(created.status, 200)
        self.assertEqual(len(board_payload["tasks"]), 1)
        self.assertEqual(updated_payload["task"]["status"], "in_progress")

    async def test_administrator_can_issue_and_revoke_discord_global_ban(self) -> None:
        administrator = ConsensusWebPrincipal(
            user_id=99,
            guild_id=10,
            display_name="Администратор",
            csrf_token="admin-csrf",
            member=SimpleNamespace(
                id=99,
                display_name="Администратор",
                guild_permissions=SimpleNamespace(administrator=True),
                roles=[],
            ),  # type: ignore[arg-type]
        )
        guild = SimpleNamespace(
            id=10,
            owner_id=1,
            get_member=lambda user_id: None,
            ban=AsyncMock(),
            unban=AsyncMock(),
        )
        bot = SimpleNamespace(
            user=SimpleNamespace(id=500),
            get_guild=lambda guild_id: guild if guild_id == 10 else None,
            get_user=lambda user_id: None,
            is_ready=lambda: True,
        )
        app = create_consensus_web_app(bot, guild_id=10)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=administrator),
            ):
                issued = await client.post(
                    "/api/admin/security/bans",
                    headers={"X-CSRF-Token": "admin-csrf"},
                    json={
                        "action": "issue",
                        "user_id": "123456789012345678",
                        "reason": "Критическое нарушение правил",
                        "confirmed": True,
                    },
                )
                issued_payload = await issued.json()
                revoked = await client.post(
                    "/api/admin/security/bans",
                    headers={"X-CSRF-Token": "admin-csrf"},
                    json={
                        "action": "revoke",
                        "user_id": "123456789012345678",
                        "reason": "Решение пересмотрено",
                        "confirmed": True,
                    },
                )
                revoked_payload = await revoked.json()
        self.assertEqual(issued.status, 200)
        self.assertEqual(issued_payload["record"]["discord_state"], "banned")
        guild.ban.assert_awaited_once()
        self.assertEqual(revoked.status, 200)
        self.assertFalse(revoked_payload["record"]["active"])
        guild.unban.assert_awaited_once()

    async def test_web_lock_remains_authoritative_when_discord_or_audit_fails(self) -> None:
        administrator = ConsensusWebPrincipal(
            user_id=99,
            guild_id=10,
            display_name="Администратор",
            csrf_token="admin-csrf",
            member=SimpleNamespace(
                id=99,
                display_name="Администратор",
                guild_permissions=SimpleNamespace(administrator=True),
                roles=[],
            ),  # type: ignore[arg-type]
        )
        guild = SimpleNamespace(
            id=10,
            owner_id=1,
            get_member=lambda _user_id: None,
            ban=AsyncMock(side_effect=RuntimeError("discord transport lost")),
            unban=AsyncMock(),
        )
        bot = SimpleNamespace(
            user=SimpleNamespace(id=500),
            get_guild=lambda guild_id: guild if guild_id == 10 else None,
            get_user=lambda _user_id: None,
            is_ready=lambda: True,
        )
        app = create_consensus_web_app(bot, guild_id=10)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with (
                patch(
                    "modules.consensus_web.resolve_principal",
                    AsyncMock(return_value=administrator),
                ),
                patch(
                    "modules.consensus_admin_web.activity_storage.bot_record_action",
                    side_effect=RuntimeError("audit busy"),
                ),
            ):
                response = await client.post(
                    "/api/admin/security/bans",
                    headers={"X-CSRF-Token": "admin-csrf"},
                    json={
                        "action": "issue",
                        "user_id": "123456789012345678",
                        "reason": "Критическое нарушение правил",
                        "confirmed": True,
                    },
                )
                payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["record"]["active"])
        self.assertEqual(payload["record"]["discord_state"], "failed")
        self.assertIn("аудит", payload["warning"])
        self.assertTrue(bans.is_globally_banned(10, 123456789012345678))

    async def test_global_ban_admin_boundary_and_csrf_are_fail_closed(self) -> None:
        guild = SimpleNamespace(id=10, owner_id=1)
        bot = SimpleNamespace(
            user=SimpleNamespace(id=500),
            get_guild=lambda guild_id: guild if guild_id == 10 else None,
            get_user=lambda _user_id: None,
            is_ready=lambda: True,
        )
        app = create_consensus_web_app(bot, guild_id=10)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=self.principal),
            ):
                forbidden = await client.get("/api/admin/security/bans")
            administrator = ConsensusWebPrincipal(
                user_id=99,
                guild_id=10,
                display_name="Администратор",
                csrf_token="admin-csrf",
                member=SimpleNamespace(
                    id=99,
                    display_name="Администратор",
                    guild_permissions=SimpleNamespace(administrator=True),
                    roles=[],
                ),  # type: ignore[arg-type]
            )
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=administrator),
            ):
                no_csrf = await client.post(
                    "/api/admin/security/bans",
                    json={
                        "action": "issue",
                        "user_id": "123456789012345678",
                        "reason": "Критическое нарушение правил",
                        "confirmed": True,
                    },
                )
                protected = await client.post(
                    "/api/admin/security/bans",
                    headers={"X-CSRF-Token": "admin-csrf"},
                    json={
                        "action": "issue",
                        "user_id": "99",
                        "reason": "Нельзя заблокировать себя",
                        "confirmed": True,
                    },
                )
        self.assertEqual(forbidden.status, 403)
        self.assertEqual(no_csrf.status, 403)
        self.assertEqual(protected.status, 400)
        self.assertFalse(bans.is_globally_banned(10, 99))

    async def test_failed_discord_ban_is_reconciled_from_durable_state(self) -> None:
        bans.issue_global_ban(
            10,
            123456789012345678,
            reason="Критическое нарушение правил",
            actor_id=99,
            actor_display="Администратор",
        )
        bans.set_global_ban_discord_state(
            10,
            123456789012345678,
            state="failed",
            error="temporary transport failure",
            actor_id=99,
            actor_display="Администратор",
        )
        guild = SimpleNamespace(ban=AsyncMock())
        bot = SimpleNamespace(
            user=SimpleNamespace(id=500),
            get_guild=lambda guild_id: guild if guild_id == 10 else None,
        )

        changed = await _reconcile_global_bans_once(bot, 10)  # type: ignore[arg-type]

        self.assertEqual(changed, 1)
        guild.ban.assert_awaited_once()
        self.assertEqual(
            bans.get_global_ban(10, 123456789012345678)["discord_state"],
            "banned",
        )


if __name__ == "__main__":
    unittest.main()
