import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

import storage
from modules.consensus_admin_web import (
    _apply_global_discord_unban,
    _reconcile_global_bans_once,
)
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

    def test_desktop_installation_links_known_accounts_and_enforces_ban(self) -> None:
        token = "A" * 43
        fingerprint = "1" * 64
        first = bans.bind_desktop_installation(
            10,
            20,
            token,
            platform="win32",
            app_version="1.3.6",
            device_fingerprint=fingerprint,
        )
        bans.bind_desktop_installation(
            10,
            21,
            "D" * 43,
            platform="win32",
            app_version="1.3.6",
            device_fingerprint=fingerprint,
        )
        self.assertTrue(first["trusted"])
        self.assertTrue(first["hardware_bound"])
        self.assertEqual(bans.linked_desktop_accounts(10, 20), [21])
        bans.issue_global_ban(
            10,
            20,
            reason="Подтверждённое решение администратора",
            actor_id=99,
            actor_display="Администратор",
        )
        # A newly installed client on the same OS remains linked even after
        # its local installation credential changes.
        decision = bans.get_desktop_installation_ban(
            10, "E" * 43, fingerprint
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["user_id"], 20)
        summary = bans.desktop_installation_summaries(10)["20"]
        self.assertEqual(summary["installation_count"], 1)

    def test_global_ban_applies_across_tmod_guilds(self) -> None:
        bans.issue_global_ban(
            10,
            20,
            reason="Подтверждённое решение администратора",
            actor_id=99,
            actor_display="Администратор",
        )
        self.assertTrue(bans.is_globally_banned(999, 20))


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

    async def test_global_unban_preserves_independent_discord_decision(self) -> None:
        guild = SimpleNamespace(
            id=10,
            fetch_ban=AsyncMock(return_value=SimpleNamespace(reason="Other moderation case")),
            unban=AsyncMock(),
        )
        bot = SimpleNamespace(get_guild=lambda _guild_id: guild)
        await _apply_global_discord_unban(
            bot, 10, 123456789012345678, reason="T-Mod appeal accepted"
        )
        guild.unban.assert_not_awaited()

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
                protected_preview = await client.get("/api/blackbird/communicate/preview?url=https://consensus.tvr.lat/bills/1")
                social_statuses = [
                    (await client.get(path)).status for path in (
                        "/api/blackbird/communicate", "/api/blackbird/media",
                        "/api/blackbird/media/assets/20/avatar",
                        "/api/blackbird/communicate/attachments/test-attachment",
                        "/api/reactor/notifications",
                    )
                ]
                page = await client.get("/banned")
                state = await client.get("/api/banned")
                logout = await client.get("/auth/logout", allow_redirects=False)
                payload = await state.json()
        self.assertEqual(protected.status, 423)
        self.assertEqual(protected_preview.status, 423)
        self.assertEqual(social_statuses, [423] * 5)
        self.assertEqual(page.status, 200)
        self.assertEqual(logout.status, 303)
        self.assertEqual(logout.headers["Location"], "/banned")
        self.assertTrue(payload["active"])
        self.assertEqual(payload["reason"], "Критическое нарушение правил")

    async def test_known_banned_desktop_installation_blocks_alt_login(self) -> None:
        token = "B" * 43
        bans.bind_desktop_installation(10, 20, token, platform="win32")
        bans.issue_global_ban(
            10,
            20,
            reason="Критическое нарушение правил",
            actor_id=99,
            actor_display="Администратор",
        )
        app = create_consensus_web_app(self.bot, guild_id=10)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/auth/login?client=desktop",
                headers={
                    "X-TMod-Install-Token": token,
                    "X-TMod-Device-Fingerprint": "2" * 64,
                },
                data={"login": "alt.user", "pin": "12345678"},
                allow_redirects=False,
            )
            payload = await response.json()
            state_response = await client.get(
                "/api/banned",
                headers={"X-TMod-Install-Token": token},
            )
            state = await state_response.json()
        self.assertEqual(response.status, 423)
        self.assertEqual(payload["error"], "globally_banned")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertTrue(state["active"])
        self.assertEqual(state["user_id"], "")
        self.assertIn("no-store", state_response.headers["Cache-Control"])
        self.assertEqual(payload["reason"], "Доступ с этой установки T-Mod Desktop ограничен.")

    async def test_known_banned_device_blocks_alt_login_without_install_token(self) -> None:
        fingerprint = "a" * 64
        bans.bind_desktop_installation(
            10, 20, "B" * 43, platform="win32", device_fingerprint=fingerprint
        )
        bans.issue_global_ban(
            10, 20,
            reason="Критическое нарушение правил",
            actor_id=99,
            actor_display="Администратор",
        )
        app = create_consensus_web_app(self.bot, guild_id=10)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/auth/login?client=desktop",
                headers={"X-TMod-Device-Fingerprint": fingerprint},
                data={"login": "alt.user", "pin": "12345678"},
                allow_redirects=False,
            )
            payload = await response.json()
        self.assertEqual(response.status, 423)
        self.assertEqual(payload["error"], "globally_banned")

    async def test_hardware_binding_survives_new_installation_and_os_identifier(self) -> None:
        hardware = "c" * 64
        bans.bind_desktop_installation(
            10, 20, "B" * 43, platform="win32",
            device_fingerprint="a" * 64, hardware_fingerprint=hardware,
        )
        bans.issue_global_ban(
            10, 20,
            reason="Критическое нарушение правил",
            actor_id=99,
            actor_display="Администратор",
        )
        app = create_consensus_web_app(self.bot, guild_id=10)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/auth/login?client=desktop",
                headers={
                    "X-TMod-Install-Token": "Z" * 43,
                    "X-TMod-Device-Fingerprint": "d" * 64,
                    "X-TMod-Hardware-Fingerprint": hardware,
                },
                data={"login": "alt.user", "pin": "12345678"},
                allow_redirects=False,
            )
            payload = await response.json()
        self.assertEqual(response.status, 423)
        self.assertEqual(payload["error"], "globally_banned")

    async def test_existing_reactor_event_stream_stops_after_global_ban(self) -> None:
        admin = ConsensusWebPrincipal(
            user_id=20,
            guild_id=10,
            display_name="Администратор",
            csrf_token="csrf-token",
            member=SimpleNamespace(
                id=20,
                display_name="Администратор",
                guild_permissions=SimpleNamespace(administrator=True),
                roles=[],
            ),  # type: ignore[arg-type]
        )
        app = create_consensus_web_app(self.bot, guild_id=10)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch("modules.consensus_web.resolve_principal", AsyncMock(return_value=admin)):
                response = await client.get("/api/admin/reactor/events/stream")
                self.assertEqual(response.status, 200)
                await response.content.readuntil(b"\n\n")
                bans.issue_global_ban(
                    10, 20,
                    reason="Критическое нарушение правил",
                    actor_id=99,
                    actor_display="Администратор",
                )
                await asyncio.wait_for(response.content.read(), timeout=6)
                self.assertTrue(response.content.at_eof())

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
            fetch_ban=AsyncMock(return_value=SimpleNamespace(reason="T-Mod global ban · test")),
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
        self.assertEqual(
            issued_payload["record"]["user_id_text"],
            "123456789012345678",
        )
        guild.ban.assert_awaited_once()
        self.assertEqual(revoked.status, 200)
        self.assertFalse(revoked_payload["record"]["active"])
        guild.unban.assert_awaited_once()

    async def test_global_ban_cascades_to_confirmed_accounts_and_all_bot_guilds(self) -> None:
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
        primary = SimpleNamespace(
            id=10,
            owner_id=1,
            get_member=lambda _user_id: None,
            ban=AsyncMock(),
            fetch_ban=AsyncMock(return_value=SimpleNamespace(reason="T-Mod global ban · test")),
            unban=AsyncMock(),
        )
        second = SimpleNamespace(
            id=11,
            owner_id=2,
            get_member=lambda _user_id: None,
            ban=AsyncMock(),
            fetch_ban=AsyncMock(return_value=SimpleNamespace(reason="T-Mod global ban · test")),
            unban=AsyncMock(),
        )
        bot = SimpleNamespace(
            user=SimpleNamespace(id=500),
            guilds=[primary, second],
            get_guild=lambda selected: primary if selected == 10 else None,
            get_user=lambda _user_id: None,
            is_ready=lambda: True,
        )
        token = "F" * 43
        fingerprint = "4" * 64
        primary_id = 123456789012345678
        linked_id = 123456789012345679
        bans.bind_desktop_installation(
            10, primary_id, token, device_fingerprint=fingerprint
        )
        bans.bind_desktop_installation(
            10, linked_id, "G" * 43, device_fingerprint=fingerprint
        )
        app = create_consensus_web_app(bot, guild_id=10)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=administrator),
            ):
                preview = await client.get(
                    f"/api/admin/security/bans?lookup={primary_id}"
                )
                self.assertEqual(preview.status, 200)
                self.assertEqual(
                    (await preview.json())["linked_user_ids"], [str(linked_id)]
                )
                issued = await client.post(
                    "/api/admin/security/bans",
                    headers={"X-CSRF-Token": "admin-csrf"},
                    json={
                        "action": "issue",
                        "user_id": str(primary_id),
                        "reason": "Подтверждённое нарушение правил",
                        "confirmed": True,
                    },
                )
                issued_payload = await issued.json()
                self.assertEqual(issued.status, 200, issued_payload)
                self.assertTrue(bans.is_globally_banned(10, linked_id))
                self.assertEqual(
                    bans.get_global_ban(10, linked_id)["source_user_id"], primary_id
                )
                self.assertEqual(primary.ban.await_count, 2)
                self.assertEqual(second.ban.await_count, 2)
                revoked = await client.post(
                    "/api/admin/security/bans",
                    headers={"X-CSRF-Token": "admin-csrf"},
                    json={
                        "action": "revoke",
                        "user_id": str(primary_id),
                        "reason": "Решение пересмотрено",
                        "confirmed": True,
                    },
                )
                revoked_payload = await revoked.json()
        self.assertEqual(revoked.status, 200, revoked_payload)
        self.assertFalse(bans.is_globally_banned(10, primary_id))
        self.assertFalse(bans.is_globally_banned(10, linked_id))
        self.assertEqual(primary.unban.await_count, 2)
        self.assertEqual(second.unban.await_count, 2)

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

    async def test_web_ban_commits_when_discord_is_temporarily_unavailable(self) -> None:
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
        bot = SimpleNamespace(
            user=SimpleNamespace(id=500),
            get_guild=lambda _guild_id: None,
            get_user=lambda _user_id: None,
            is_ready=lambda: True,
        )
        app = create_consensus_web_app(bot, guild_id=10)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=administrator),
            ):
                access = await client.get("/api/admin/access/self")
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
                access_payload = await access.json()
                issued_payload = await issued.json()

        self.assertEqual(access.status, 200)
        self.assertIn("security", access_payload["sections"])
        self.assertEqual(issued.status, 200)
        self.assertTrue(issued_payload["record"]["active"])
        self.assertEqual(issued_payload["record"]["discord_state"], "failed")
        self.assertIn("веб-блокировка включена", issued_payload["message"].lower())
        self.assertTrue(bans.is_globally_banned(10, 123456789012345678))

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

    async def test_active_discord_ban_is_continuously_reasserted(self) -> None:
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
            state="banned",
            error=None,
            actor_id=99,
            actor_display="Администратор",
        )
        guild = SimpleNamespace(ban=AsyncMock())
        bot = SimpleNamespace(
            user=SimpleNamespace(id=500),
            get_guild=lambda guild_id: guild if guild_id == 10 else None,
        )

        changed = await _reconcile_global_bans_once(bot, 10)  # type: ignore[arg-type]

        self.assertEqual(changed, 0)
        guild.ban.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
