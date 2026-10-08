import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import storage
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from modules.account_registration_web import API, register_account_registration_routes, registration_remote
from modules.global_log_runtime import redact_value, _request_payload, _response_payload
from persistence import account_registration_repository as registry
from persistence import web_auth_repository as credentials
from persistence import profile_repository as profiles


class RegistrationStorageTests(unittest.TestCase):
    def setUp(self):
        self.previous = storage.DATA_DIR, storage.DATABASE_FILE
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "registration.db"
        storage.init_db()

    def tearDown(self):
        storage.DATA_DIR, storage.DATABASE_FILE = self.previous
        self.temp.cleanup()

    def paired(self, user=501):
        started = registry.start_registration(77, now=1000)
        registry.claim_registration(77, user, started["pairing_code"], "Тестовый участник", now=1001)
        return started

    def finish(self, started, **overrides):
        fields = {"login": "robert.phoenix", "secret": "not-a-real-password", "kind": "password",
                  "preferred_name": "Роберт", "characters": [{"nickname": "Robert Bailey", "static_id": "263345"}], "now": 1002}
        return registry.complete_registration(77, started["browser_token"], **(fields | overrides))

    def test_pairing_is_hashed_expiring_and_browser_owned(self):
        started = registry.start_registration(77, now=1000)
        with storage.connect_readonly() as con:
            raw = dict(con.execute("SELECT * FROM account_registrations").fetchone())
        self.assertNotIn(started["browser_token"], json.dumps(raw))
        self.assertNotIn(started["pairing_code"], json.dumps(raw))
        for guild, token, now in [(88, started["browser_token"], 1001), (77, "another-browser", 1001), (77, started["browser_token"], 1900)]:
            with self.assertRaisesRegex(ValueError, "registration_expired"):
                registry.registration_status(guild, token, now=now)
        with self.assertRaisesRegex(ValueError, "registration_discord_required"):
            self.finish(started)

    def test_code_is_single_use_including_simultaneous_claims(self):
        started = registry.start_registration(77, now=1000)
        def claim(user):
            try:
                registry.claim_registration(77, user, started["pairing_code"], "Test", now=1001)
                return "ok"
            except ValueError as exc:
                return str(exc)
        with ThreadPoolExecutor(2) as executor:
            outcomes = list(executor.map(claim, [501, 502]))
        self.assertCountEqual(outcomes, ["ok", "registration_code_invalid"])

    def test_restart_revokes_previous_code(self):
        first = registry.start_registration(77, now=1000)
        registry.start_registration(77, previous_token=first["browser_token"], now=1001)
        with self.assertRaisesRegex(ValueError, "registration_code_invalid"):
            registry.claim_registration(77, 501, first["pairing_code"], "Test", now=1002)

    def test_atomic_password_account_and_private_character_are_usable(self):
        started = self.paired()
        self.assertTrue(self.finish(started)["ok"])
        self.assertEqual(credentials.authenticate_web_credential(77, "robert.phoenix", "not-a-real-password").status, "ok")
        self.assertEqual(profiles.get_member_profile(77, 501).preferred_name, "Роберт")
        character = profiles.list_profile_characters(77, 501)[0]
        self.assertEqual(character.static_id, "263345")
        self.assertFalse(character.is_public)
        with storage.connect_readonly() as con:
            row = con.execute("SELECT pin_hash FROM web_credentials").fetchone()
            self.assertTrue(row["pin_hash"].startswith("scrypt$"))
            self.assertNotIn("not-a-real-password", row["pin_hash"])
            self.assertEqual(con.execute("SELECT credential_kind FROM account_security").fetchone()["credential_kind"], "password")

    def test_pin_and_empty_character_step_are_supported(self):
        started = self.paired()
        self.finish(started, kind="pin", secret="12345678", characters=[])
        self.assertEqual(credentials.authenticate_web_credential(77, "robert.phoenix", "12345678").status, "ok")
        self.assertEqual(profiles.list_profile_characters(77, 501), [])

    def test_existing_account_cannot_be_taken_over_even_after_pairing(self):
        started = self.paired()
        credentials.configure_web_credential(77, 501, "existing", "12345678")
        with self.assertRaisesRegex(ValueError, "registration_account_exists"):
            self.finish(started)
        self.assertEqual(credentials.authenticate_web_credential(77, "existing", "12345678").status, "ok")
        self.assertEqual(profiles.list_profile_characters(77, 501), [])

    def test_conflict_rolls_back_everything_and_retry_does_not_reset_password(self):
        profiles.add_profile_character(77, 600, "Other Person", "263345")
        started = self.paired()
        with self.assertRaisesRegex(ValueError, "profile_static_taken"):
            self.finish(started)
        self.assertIsNone(credentials.get_web_credential(77, 501))
        self.assertIsNone(profiles.get_member_profile(77, 501))
        self.finish(started, characters=[])
        self.assertEqual(self.finish(started, secret="different-password", characters=[])["login"], "robert.phoenix")
        self.assertEqual(credentials.authenticate_web_credential(77, "robert.phoenix", "not-a-real-password").status, "ok")

    def test_existing_characters_are_preserved(self):
        profiles.add_profile_character(77, 501, "Existing Person", "123")
        self.finish(self.paired())
        self.assertEqual([item.static_id for item in profiles.list_profile_characters(77, 501)], ["123", "263345"])

    def test_ban_after_pairing_still_prevents_creation(self):
        started = self.paired()
        with storage.connect() as con:
            con.execute("INSERT INTO global_bans(guild_id,user_id,reason,issued_by_id,issued_by_display,issued_at,updated_at) VALUES(77,501,'test',1,'test','now','now')")
            con.commit()
        with self.assertRaisesRegex(ValueError, "registration_blocked"):
            self.finish(started)


class RegistrationHttpTests(unittest.IsolatedAsyncioTestCase):
    def test_rate_limit_uses_client_not_shared_gateway_or_spoofed_prefix(self):
        headers = {"X-TMod-Forwarded-Host": "phx.tvr.lat", "X-Forwarded-For": "9.9.9.9, 8.8.8.8, 172.20.0.2"}
        request = SimpleNamespace(remote="172.20.0.3", headers=headers)
        self.assertEqual(registration_remote(request), "8.8.8.8")
        self.assertEqual(registration_remote(SimpleNamespace(remote="1.1.1.1", headers=headers)), "1.1.1.1")
        self.assertEqual(registration_remote(SimpleNamespace(remote="172.20.0.3", headers={"X-Forwarded-For": "8.8.8.8"})), "172.20.0.3")
        self.assertEqual(registration_remote(SimpleNamespace(remote="172.20.0.3", headers=headers | {"X-Forwarded-For": "8.8.8.8, invalid"})), "172.20.0.3")

    async def asyncSetUp(self):
        self.previous = storage.DATA_DIR, storage.DATABASE_FILE
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "registration-http.db"
        storage.init_db()
        self.fetch = AsyncMock(return_value=SimpleNamespace(id=501))
        self.bot = SimpleNamespace(get_guild=lambda guild: SimpleNamespace(fetch_member=self.fetch))
        app = web.Application()
        register_account_registration_routes(app, self.bot, guild_id=77, asset_dir=Path(__file__).resolve().parents[1] / "web/consensus")
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.origin = str(self.client.make_url("/")).rstrip("/")
        self.headers = {"Origin": self.origin}

    async def asyncTearDown(self):
        await self.client.close()
        storage.DATA_DIR, storage.DATABASE_FILE = self.previous
        self.temp.cleanup()

    async def start(self):
        response = await self.client.post(API + "/start", json={}, headers=self.headers)
        self.assertEqual(response.status, 201)
        return await response.json()

    async def test_actual_http_pairing_poll_completion_and_cookie_protection(self):
        start = await self.start()
        self.assertNotIn("browser_token", start)
        cookie = self.client.session.cookie_jar.filter_cookies(self.client.make_url(API))["tmod_registration"]
        self.assertTrue(cookie.value)
        waiting = await self.client.get(API + "/status")
        self.assertEqual((await waiting.json())["status"], "waiting")
        registry.claim_registration(77, 501, start["pairing_code"], "Роберт")
        claimed = await self.client.get(API + "/status")
        result = await claimed.json()
        self.assertEqual(result["user_id"], "501")
        self.assertEqual(result["discord_name"], "Роберт")
        payload = {"login": "http.robert", "password": "12345678", "kind": "pin", "preferred_name": "Роберт", "characters": [], "consent": True}
        denied = await self.client.post(API + "/complete", json=payload, headers=self.headers)
        self.assertEqual(denied.status, 403)
        done = await self.client.post(API + "/complete", json=payload, headers=self.headers | {"X-CSRF-Token": result["csrf_token"]})
        self.assertEqual(done.status, 200, await done.text())
        self.assertEqual(credentials.authenticate_web_credential(77, "http.robert", "12345678").status, "ok")
        self.fetch.assert_awaited_once_with(501)
        retry = await self.client.post(API + "/complete", json=payload, headers=self.headers | {"X-CSRF-Token": result["csrf_token"]})
        self.assertEqual(retry.status, 200)
        self.assertEqual(done.headers["Cache-Control"], "no-store")

    async def test_cross_origin_rate_limit_bad_payload_and_unowned_poll(self):
        denied = await self.client.post(API + "/start", json={}, headers={"Origin": "https://evil.example"})
        self.assertEqual(denied.status, 403)
        response = await self.client.get(API + "/status")
        self.assertEqual(response.status, 410)
        start = await self.start()
        invalid = await self.client.post(API + "/complete", json=[], headers=self.headers | {"X-CSRF-Token": start["csrf_token"]})
        self.assertEqual(invalid.status, 400)
        for _ in range(7):
            await self.start()
        limited = await self.client.post(API + "/start", json={}, headers=self.headers)
        self.assertEqual(limited.status, 429)

    async def test_membership_is_rechecked_before_saving(self):
        start = await self.start()
        registry.claim_registration(77, 501, start["pairing_code"], "Участник")
        self.fetch.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")
        denied = await self.client.post(API + "/complete", json={"login": "left.guild", "password": "12345678", "kind": "pin", "preferred_name": "Участник", "characters": [], "consent": True}, headers=self.headers | {"X-CSRF-Token": start["csrf_token"]})
        self.assertEqual(denied.status, 400)
        self.assertEqual((await denied.json())["error"], "registration_membership_required")
        self.assertIsNone(credentials.get_web_credential(77, 501))

    async def test_secure_cookie_uses_public_proxy_origin_and_clients_have_separate_limits(self):
        proxy = {"Origin": "https://phx.tvr.lat", "X-TMod-Forwarded-Host": "phx.tvr.lat", "X-TMod-Forwarded-Proto": "https", "X-Forwarded-For": "8.8.8.8, 172.20.0.2"}
        for _ in range(8):
            response = await self.client.post(API + "/start", json={}, headers=proxy)
            self.assertEqual(response.status, 201)
            self.assertIn("Secure", response.headers["Set-Cookie"])
            self.assertIn("HttpOnly", response.headers["Set-Cookie"])
            self.assertIn("SameSite=Strict", response.headers["Set-Cookie"])
        limited = await self.client.post(API + "/start", json={}, headers=proxy)
        self.assertEqual(limited.status, 429)
        other = await self.client.post(API + "/start", json={}, headers=proxy | {"X-Forwarded-For": "1.1.1.1, 172.20.0.2"})
        self.assertEqual(other.status, 201)

    async def test_secrets_and_discord_code_are_omitted_from_logs(self):
        result = redact_value({"options": [{"name": "code", "value": "2345ABCDEF"}], "pairing_code": "ABCD234567"})
        self.assertNotIn("2345ABCDEF", json.dumps(result))
        self.assertNotIn("ABCD234567", json.dumps(result))
        self.assertEqual(await _request_payload(SimpleNamespace(path=API + "/complete")), {"omitted": "authentication_secrets"})
        self.assertEqual(_response_payload(web.json_response({"pairing_code": "secret"}), API + "/start"), {"omitted": "authentication_secrets"})
