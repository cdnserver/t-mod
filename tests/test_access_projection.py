import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import storage
from modules import consensus_web_auth
from modules.consensus_web_auth import (
    SESSION_COOKIE,
    create_session_token,
    resolve_projected_principal,
)
from api_main import create_app as create_api_app


class AccessProjectionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "projection.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_snapshot_updates_roles_and_revokes_missing_members(self) -> None:
        first = storage.replace_web_access_projections(
            77,
            [
                {
                    "user_id": 10,
                    "display_name": "Admin",
                    "administrator": True,
                    "role_ids": [9, 3, 9],
                },
                {
                    "user_id": 11,
                    "display_name": "Member",
                    "administrator": False,
                    "role_ids": [4],
                },
            ],
        )
        self.assertEqual(first, {"active": 2, "revoked": 0})
        admin = storage.get_web_access_projection(77, 10)
        self.assertIsNotNone(admin)
        assert admin is not None
        self.assertTrue(admin.administrator)
        self.assertEqual(admin.role_ids, (3, 9))

        second = storage.replace_web_access_projections(
            77,
            [
                {
                    "user_id": 11,
                    "display_name": "Renamed",
                    "administrator": True,
                    "role_ids": [7],
                }
            ],
        )
        self.assertEqual(second, {"active": 1, "revoked": 1})
        departed = storage.get_web_access_projection(77, 10)
        active = storage.get_web_access_projection(77, 11)
        assert departed is not None and active is not None
        self.assertFalse(departed.guild_member)
        self.assertFalse(departed.administrator)
        self.assertEqual(departed.role_ids, ())
        self.assertEqual(active.display_name, "Renamed")
        self.assertTrue(active.administrator)

    def test_partial_snapshot_never_revokes_unobserved_members(self) -> None:
        storage.upsert_web_access_projection(
            77,
            10,
            "Cached member",
            administrator=True,
            role_ids=[3],
        )
        result = storage.replace_web_access_projections(
            77,
            [],
            revoke_missing=False,
        )
        cached = storage.get_web_access_projection(77, 10)
        self.assertEqual(result, {"active": 0, "revoked": 0})
        assert cached is not None
        self.assertTrue(cached.guild_member)
        self.assertTrue(cached.administrator)


class ProjectedPrincipalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.old_secret = consensus_web_auth._runtime_secret
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "projected-auth.db"
        consensus_web_auth._runtime_secret = None
        storage.init_db()

    def tearDown(self) -> None:
        consensus_web_auth._runtime_secret = self.old_secret
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    async def _resolve(self, token: str):
        async def handler(request: web.Request) -> web.Response:
            principal = await resolve_projected_principal(request, guild_id=77)
            if principal is None:
                return web.json_response({"principal": None})
            return web.json_response(
                {
                    "id": principal.user_id,
                    "name": principal.display_name,
                    "member": principal.guild_member,
                    "admin": principal.administrator,
                    "roles": [role.id for role in principal.member.roles],
                }
            )

        app = web.Application()
        app.router.add_get("/", handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            client.session.cookie_jar.update_cookies({SESSION_COOKIE: token})
            response = await client.get("/")
            return await response.json()
        finally:
            await client.close()

    async def test_projected_admin_does_not_need_discord_client(self) -> None:
        storage.upsert_web_access_projection(
            77,
            42,
            "Soul",
            administrator=True,
            role_ids=[100, 200],
        )
        token, _ = create_session_token(guild_id=77, user_id=42)
        payload = await self._resolve(token)
        self.assertEqual(
            payload,
            {
                "id": 42,
                "name": "Soul",
                "member": True,
                "admin": True,
                "roles": [100, 200],
            },
        )

    async def test_departed_ticket_session_is_not_promoted_to_zero_account(self) -> None:
        storage.upsert_web_access_projection(
            77,
            42,
            "Soul",
            administrator=False,
        )
        storage.mark_web_access_projection_departed(77, 42)
        token, _ = create_session_token(guild_id=77, user_id=42)
        self.assertEqual(await self._resolve(token), {"principal": None})

    async def test_versioned_account_session_survives_departure_as_zero_account(self) -> None:
        credential = storage.configure_web_credential(77, 42, "soul", "12345678")
        storage.upsert_web_access_projection(
            77,
            42,
            "Soul",
            administrator=False,
        )
        storage.mark_web_access_projection_departed(77, 42)
        token, _ = create_session_token(
            guild_id=77,
            user_id=42,
            session_version=credential.session_version,
        )
        payload = await self._resolve(token)
        self.assertFalse(payload["member"])
        self.assertFalse(payload["admin"])
        self.assertEqual(payload["name"], "soul")

    async def test_shadow_api_serves_admin_bootstrap_without_discord(self) -> None:
        credential = storage.configure_web_credential(77, 42, "soul", "12345678")
        storage.upsert_web_access_projection(
            77,
            42,
            "Soul",
            administrator=True,
            role_ids=[100],
        )
        token, _ = create_session_token(
            guild_id=77,
            user_id=42,
            session_version=credential.session_version,
        )
        app = await create_api_app(guild_id=77)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            ready = await client.get("/ready")
            self.assertEqual(ready.status, 200)
            response = await client.get(
                "/internal/desktop/v1/bootstrap",
                headers={
                    "Cookie": f"{SESSION_COOKIE}={token}",
                    "X-TMod-Desktop-Version": "1.3.6-dev.2",
                },
            )
            payload = await response.json()
            self.assertEqual(response.status, 200)
            self.assertTrue(payload["viewer"]["administrator"])
            self.assertTrue(payload["viewer"]["guild_member"])
            self.assertTrue(all(item["enabled"] for item in payload["services"]))
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        finally:
            await client.close()

    async def test_shadow_api_serves_member_notifications_without_discord(self) -> None:
        credential = storage.configure_web_credential(77, 42, "soul", "12345678")
        storage.upsert_web_access_projection(
            77,
            42,
            "Soul",
            administrator=True,
            role_ids=[100],
        )
        storage.reactor_put_notification(
            guild_id=77,
            user_id=42,
            severity="warning",
            kind="attention",
            title="Нужна проверка",
            body="Очередь ожидает администратора.",
            route="#/system",
            dedupe_key="projection-test",
        )
        token, _ = create_session_token(
            guild_id=77,
            user_id=42,
            session_version=credential.session_version,
        )
        app = await create_api_app(guild_id=77)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get(
                "/internal/reactor/notifications?unread=1",
                headers={"Cookie": f"{SESSION_COOKIE}={token}"},
            )
            payload = await response.json()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["unread"], 1)
            self.assertEqual(payload["items"][0]["title"], "Нужна проверка")
            self.assertTrue(payload["viewer"]["administrator"])
            self.assertEqual(payload["viewer"]["account_tier"], "administrator")
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        finally:
            await client.close()

    async def test_shadow_notification_api_rejects_zero_account(self) -> None:
        credential = storage.configure_web_credential(77, 42, "soul", "12345678")
        token, _ = create_session_token(
            guild_id=77,
            user_id=42,
            session_version=credential.session_version,
        )
        app = await create_api_app(guild_id=77)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get(
                "/internal/reactor/notifications",
                headers={"Cookie": f"{SESSION_COOKIE}={token}"},
            )
            self.assertEqual(response.status, 403)
            self.assertEqual(
                (await response.json())["error"],
                "zero_account_reactor_forbidden",
            )
        finally:
            await client.close()

    async def test_shadow_api_serves_private_preparation_projection(self) -> None:
        credential = storage.configure_web_credential(77, 42, "soul", "12345678")
        storage.upsert_web_access_projection(
            77,
            42,
            "Soul",
            administrator=True,
            role_ids=[100],
        )
        bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=88,
            author_id=5,
            author_display="Автор",
            title="О подготовке API",
            summary="Проверка независимого контура.",
            materials="https://example.com/material",
        )
        storage.save_preparation_sheet(
            77,
            bill.id,
            42,
            user_display="Soul",
            expected_revision=0,
            questions=[],
            notes="Секретная личная заметка.",
            preliminary_vote="yes",
            preliminary_vote_reason="Секретная причина.",
            review_flags={"read_text": True},
            source_bill_updated_at=bill.updated_at,
        )
        token, _ = create_session_token(
            guild_id=77,
            user_id=42,
            session_version=credential.session_version,
        )
        app = await create_api_app(guild_id=77)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get(
                "/internal/reactor/preparation",
                headers={"Cookie": f"{SESSION_COOKIE}={token}"},
            )
            payload = await response.json()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["prepared"], 1)
            self.assertEqual(
                payload["items"][0]["preparation"]["preliminary_vote"],
                "yes",
            )
            self.assertNotIn("notes", payload["items"][0])
            self.assertNotIn("preliminary_vote_reason", payload["items"][0])
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        finally:
            await client.close()

    async def test_shadow_api_serves_durable_admin_section_access(self) -> None:
        credential = storage.configure_web_credential(77, 42, "soul", "12345678")
        storage.upsert_web_access_projection(
            77,
            42,
            "Soul",
            administrator=True,
            role_ids=[100],
        )
        token, _ = create_session_token(
            guild_id=77,
            user_id=42,
            session_version=credential.session_version,
        )
        app = await create_api_app(guild_id=77)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get(
                "/internal/admin/access/self",
                headers={"Cookie": f"{SESSION_COOKIE}={token}"},
            )
            payload = await response.json()
            self.assertEqual(response.status, 200)
            self.assertTrue(payload["administrator"])
            self.assertIn("security", payload["sections"])
            self.assertIn("minecraft", payload["sections"])
            self.assertNotIn("roles", payload["viewer"])
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        finally:
            await client.close()

    async def test_shadow_api_rejects_account_without_admin_sections(self) -> None:
        credential = storage.configure_web_credential(77, 43, "member", "12345678")
        storage.upsert_web_access_projection(
            77,
            43,
            "Member",
            administrator=False,
            role_ids=[200],
        )
        token, _ = create_session_token(
            guild_id=77,
            user_id=43,
            session_version=credential.session_version,
        )
        app = await create_api_app(guild_id=77)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get(
                "/internal/admin/access/self",
                headers={"Cookie": f"{SESSION_COOKIE}={token}"},
            )
            self.assertEqual(response.status, 403)
            self.assertEqual(
                (await response.json())["error"],
                "administrator_required",
            )
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
