import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

import storage
from modules.consensus_web import create_consensus_web_app
from modules.consensus_web_auth import (
    ConsensusWebPrincipal,
    TModAccountIdentity,
)
from persistence import atlas_repository


class DesktopBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "desktop-bootstrap.db"
        storage.init_db()
        self.guild = SimpleNamespace(
            id=77,
            name="Товарищество",
            get_member=lambda _user_id: None,
            fetch_member=AsyncMock(return_value=None),
        )
        self.bot = SimpleNamespace(
            get_guild=lambda guild_id: self.guild if guild_id == 77 else None,
        )

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def principal(self, *, administrator: bool = False) -> ConsensusWebPrincipal:
        member = SimpleNamespace(
            id=42,
            display_name="Участник 42",
            guild_permissions=SimpleNamespace(administrator=administrator),
            roles=[],
            joined_at=None,
        )
        return ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name=member.display_name,
            csrf_token="csrf-desktop",
            member=member,
        )

    async def test_requires_personal_account_session(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=77)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/api/desktop/v1/bootstrap")
            self.assertEqual(response.status, 401)
            self.assertEqual((await response.json())["error"], "desktop_login_required")
        finally:
            await client.close()

    async def test_member_receives_service_manifest_and_notifications(self) -> None:
        principal = self.principal(administrator=True)
        character = storage.add_profile_character(77, 42, "Saul Goodman", "263345")
        atlas_repository.atlas_set_overlay_character(
            77,
            42,
            character.id,
            server_code="phoenix-15",
            faction_code="gov",
        )
        app = create_consensus_web_app(self.bot, guild_id=77)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=principal),
            ):
                response = await client.get(
                    "/api/desktop/v1/bootstrap",
                    headers={
                        "X-TMod-Install-Token": "C" * 43,
                        "X-TMod-Desktop-Platform": "win32",
                        "X-TMod-Desktop-Version": "1.3.6",
                        "X-TMod-Device-Fingerprint": "3" * 64,
                    },
                )
            payload = await response.json()
            services = {item["id"]: item for item in payload["services"]}
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["protocol_version"], 1)
            self.assertTrue(payload["viewer"]["administrator"])
            self.assertTrue(services["reactor"]["enabled"])
            self.assertTrue(services["admin"]["enabled"])
            self.assertIn("unread", payload["notifications"])
            self.assertTrue(payload["atlas_overlay"]["allowed"])
            self.assertTrue(payload["device"]["trusted"])
            self.assertTrue(payload["device"]["hardware_bound"])
            self.assertTrue(payload["device"]["installation_ref"].startswith("TD-"))
            self.assertEqual(
                payload["atlas_overlay"]["selected_character"]["nickname"],
                "Saul Goodman",
            )
            self.assertNotIn("csrf_token", payload["atlas_overlay"])
            self.assertTrue(
                payload["atlas_overlay"]["endpoints"]["tts_synthesize"].endswith(
                    "/api/atlas/overlay/tts/synthesize"
                )
            )
            self.assertEqual(
                payload["atlas_overlay"]["capabilities"]["ai_voice"],
                "system_fallback",
            )
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        finally:
            await client.close()

    async def test_required_update_disables_services_and_describes_release(self) -> None:
        principal = self.principal(administrator=True)
        app = create_consensus_web_app(self.bot, guild_id=77)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with (
                patch("modules.consensus_web.resolve_principal", AsyncMock(return_value=principal)),
                patch.dict(
                    os.environ,
                    {
                        "TMOD_DESKTOP_MIN_VERSION": "1.3.5-p3",
                        "TMOD_DESKTOP_LATEST_VERSION": "1.3.5-p3",
                        "TMOD_DESKTOP_RELEASE_URL": "https://example.test/tmod",
                    },
                ),
            ):
                response = await client.get(
                    "/api/desktop/v1/bootstrap",
                    headers={
                        "X-TMod-Install-Token": "D" * 43,
                        "X-TMod-Desktop-Platform": "win32",
                        "X-TMod-Desktop-Version": "1.3.5-p2",
                        "X-TMod-Device-Fingerprint": "4" * 64,
                    },
                )
            payload = await response.json()
            self.assertTrue(payload["client_update"]["required"])
            self.assertEqual(payload["client_update"]["minimum_version"], "1.3.5-p3")
            self.assertEqual(payload["client_update"]["release_url"], "https://example.test/tmod")
            self.assertTrue(all(not service["enabled"] for service in payload["services"]))
            self.assertTrue(all("обновление" in service["reason"] for service in payload["services"]))
        finally:
            await client.close()

    async def test_zero_account_keeps_public_services_but_locks_reactor(self) -> None:
        identity = TModAccountIdentity(id=99, display_name="Внешний пользователь")
        principal = ConsensusWebPrincipal(
            user_id=99,
            guild_id=77,
            display_name=identity.display_name,
            csrf_token="csrf-zero",
            member=identity,
        )
        app = create_consensus_web_app(self.bot, guild_id=77)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=principal),
            ):
                response = await client.get("/api/desktop/v1/bootstrap")
            payload = await response.json()
            services = {item["id"]: item for item in payload["services"]}
            self.assertEqual(payload["viewer"]["account_tier"], "zero")
            self.assertFalse(services["reactor"]["enabled"])
            self.assertFalse(services["atlas"]["enabled"])
            self.assertTrue(services["sgl"]["enabled"])
            self.assertFalse(services["admin"]["enabled"])
        finally:
            await client.close()

    async def test_atlas_entitlement_does_not_unlock_nuclear_reactor(self) -> None:
        identity = TModAccountIdentity(id=99, display_name="Внешний пользователь")
        principal = ConsensusWebPrincipal(
            user_id=99,
            guild_id=77,
            display_name=identity.display_name,
            csrf_token="csrf-zero",
            member=identity,
        )
        storage.web_set_section_grant(
            77,
            99,
            "atlas_ai",
            enabled=True,
            granted_by_id=1,
        )
        app = create_consensus_web_app(self.bot, guild_id=77)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=principal),
            ):
                response = await client.get("/api/desktop/v1/bootstrap")
            services = {item["id"]: item for item in (await response.json())["services"]}
            self.assertTrue(services["atlas"]["enabled"])
            self.assertFalse(services["admin"]["enabled"])
        finally:
            await client.close()

    async def test_desktop_login_accepts_zero_account_without_character(self) -> None:
        credential = SimpleNamespace(user_id=99, session_version=1)
        result = SimpleNamespace(status="ok", credential=credential)
        app = create_consensus_web_app(self.bot, guild_id=77)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with (
                patch(
                    "modules.consensus_web.credential_storage.authenticate_web_credential",
                    return_value=result,
                ),
                patch(
                    "modules.consensus_web.global_ban_storage.is_globally_banned",
                    return_value=False,
                ),
                patch(
                    "modules.consensus_web.credential_storage.web_section_grants",
                    return_value=[],
                ),
            ):
                response = await client.post(
                    "/auth/login?client=desktop",
                    data={"login": "zero.user", "pin": "12345678"},
                    allow_redirects=False,
                )
            self.assertEqual(response.status, 303)
            self.assertEqual(response.headers["Location"], "/")
            self.assertIn("tmod_account_session", response.cookies)
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
