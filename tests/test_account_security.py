import base64
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet
import storage
from persistence import account_security_repository as security
from persistence import web_auth_repository as credentials
from modules.account_security_web import register_account_security_routes
from modules.consensus_web_auth import create_session_token, _verify


class AccountSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_dir, self.old_file = storage.DATA_DIR, storage.DATABASE_FILE
        self.temp = tempfile.TemporaryDirectory()
        storage.DATA_DIR = Path(self.temp.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "security.db"
        storage.init_db()
        credentials.configure_web_credential(77, 42, "operator", "12345678")
        self.env = patch.dict(os.environ, {"TMOD_ACCOUNT_SECURITY_KEY": Fernet.generate_key().decode()})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        storage.DATA_DIR, storage.DATABASE_FILE = self.old_dir, self.old_file
        self.temp.cleanup()

    def enroll(self, method="totp"):
        result = security.begin_enrollment(77, 42, method, "operator")
        nonce, message_code = security.challenge(77, 42, "enroll", method)
        code = security.totp(result["secret"], int(time.time()) // 30) if method == "totp" else message_code
        self.assertTrue(security.verify(77, 42, code, nonce, "enroll"))
        return security.enable(77, 42, nonce)

    def test_rfc6238_sha1_vector(self):
        seed = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(security.totp(seed, 59 // 30), "287082")

    def test_discord_password_configuration_preserves_second_factor(self):
        self.enroll()
        credentials.configure_web_credential(77, 42, "operator", "my-new-long-password", kind="password")
        self.assertEqual(security.state(77, 42)["credential_kind"], "password")
        self.assertEqual(security.state(77, 42)["mfa_method"], "totp")
        self.assertEqual(credentials.authenticate_web_credential(77, "operator", "my-new-long-password").status, "ok")

    def test_browser_factor_scope_does_not_bypass_blackbird(self):
        self.enroll()
        token, _ = create_session_token(guild_id=77, user_id=42)
        payload = _verify(token, purpose="session")
        self.assertTrue(security.session_allowed(77, 42, payload, require_mfa=False))
        self.assertFalse(security.session_allowed(77, 42, payload))

    def test_key_file_support_without_exposing_or_rotating_secret(self):
        keyfile = Path(self.temp.name) / "factor.key"
        keyfile.write_bytes(Fernet.generate_key())
        with patch.dict(os.environ, {"TMOD_ACCOUNT_SECURITY_KEY": "", "TMOD_ACCOUNT_SECURITY_KEY_FILE": str(keyfile)}):
            self.assertTrue(security.configured())
            encrypted = security.cipher().encrypt(b"test")
            self.assertEqual(security.cipher().decrypt(encrypted), b"test")
            self.assertEqual(len(security._hash("test")), 64)
            keyfile.unlink()
            self.assertFalse(security.configured())

    def test_key_provisioning_is_idempotent_and_preserves_configuration(self):
        from scripts.tmod_account_security_setup import configure
        root = Path(self.temp.name)
        envfile = root / ".env"
        envfile.write_text("APP_NAME=existing\n", encoding="utf-8")
        result = configure(root)
        keyfile = root / "secrets" / "account-security.key"
        before = keyfile.read_bytes()
        self.assertTrue(result["ok"])
        self.assertNotIn(before.decode().strip(), json.dumps(result))
        self.assertTrue(configure(root)["ok"])
        self.assertEqual(keyfile.read_bytes(), before)
        self.assertIn("APP_NAME=existing", envfile.read_text())
        self.assertEqual(envfile.read_text().count("TMOD_ACCOUNT_SECURITY_KEY_FILE="), 1)

    def test_provisioning_refuses_to_replace_lost_key_for_existing_factors(self):
        from scripts.tmod_account_security_setup import configure
        self.enroll()
        root = Path(self.temp.name)
        (root / ".env").write_text("APP_NAME=existing\n")
        with self.assertRaisesRegex(RuntimeError, "original_key"):
            configure(root)
        self.assertFalse((root / "secrets" / "account-security.key").exists())

    async def test_browser_login_does_not_verify_or_claim_blackbird_factor(self):
        self.enroll()
        from modules.consensus_web import create_consensus_web_app
        member = SimpleNamespace(id=42, display_name="Operator", guild_permissions=SimpleNamespace(administrator=True), roles=[])
        guild = SimpleNamespace(id=77, name="Test", get_member=lambda user: member)
        bot = SimpleNamespace(get_guild=lambda gid: guild)
        async with TestClient(TestServer(create_consensus_web_app(bot, guild_id=77))) as client:
            response = await client.post("/auth/login", data={"login":"operator", "pin":"12345678"}, allow_redirects=False)
            self.assertEqual(response.status, 303)
            payload = _verify(response.cookies["tmod_account_session"].value, purpose="session")
            self.assertNotIn("mfv", payload)
            self.assertFalse(security.session_allowed(77,42,payload))
            self.assertTrue(security.session_allowed(77,42,payload,require_mfa=False))

    def test_password_supports_unicode_and_revokes_sessions(self):
        before = credentials.get_web_credential(77, 42)
        security.change_credential(77, 42, "12345678", "Длинный уникальный пароль!", "password")
        self.assertEqual(credentials.authenticate_web_credential(77, "operator", "Длинный уникальный пароль!").status, "ok")
        self.assertFalse(credentials.web_session_version_matches(77, 42, before.session_version))
        self.assertEqual(security.state(77, 42)["credential_kind"], "password")
        security.change_credential(77, 42, "Длинный уникальный пароль!", "98765432", "pin")
        self.assertEqual(credentials.authenticate_web_credential(77, "operator", "98765432").status, "ok")

    def test_invalid_password_and_wrong_current_cannot_change_credentials(self):
        for current, value in [("12345678", "short"), ("87654321", "long-enough-password")]:
            with self.assertRaises(ValueError):
                security.change_credential(77, 42, current, value, "password")
        self.assertEqual(credentials.authenticate_web_credential(77, "operator", "12345678").status, "ok")

    def test_totp_is_encrypted_and_inactive_until_confirmed(self):
        result = security.begin_enrollment(77, 42, "totp", "operator")
        state = security.state(77, 42)
        self.assertEqual(state["mfa_method"], "")
        self.assertNotIn(result["secret"], state["pending_secret"])
        self.assertEqual(security.cipher().decrypt(state["pending_secret"].encode()).decode(), result["secret"])
        with self.assertRaises(ValueError):
            security.enable(77, 42, "no-proof")

    def test_totp_cannot_be_replayed_across_challenges(self):
        result = security.begin_enrollment(77, 42, "totp", "operator")
        nonce, _ = security.challenge(77, 42, "enroll", "totp")
        code = security.totp(result["secret"], int(time.time()) // 30)
        self.assertTrue(security.verify(77, 42, code, nonce, "enroll"))
        security.enable(77, 42, nonce)
        nonce, _ = security.challenge(77, 42, "login", "totp")
        self.assertFalse(security.verify(77, 42, code, nonce, "login"))

    def test_recovery_codes_single_use_and_not_plaintext(self):
        recovery = self.enroll()
        self.assertEqual(len(set(recovery)), 10)
        self.assertNotIn(recovery[0], security.state(77, 42)["recovery_hashes"])
        nonce, _ = security.challenge(77, 42, "login", "totp")
        self.assertTrue(security.verify(77, 42, recovery[0], nonce, "login"))
        self.assertFalse(security.verify(77, 42, recovery[0], nonce, "login"))
        self.assertEqual(len(json.loads(security.state(77, 42)["recovery_hashes"])), 9)

    def test_message_challenge_owner_purpose_expiry_and_attempt_budget(self):
        self.enroll("discord")
        nonce, code = security.challenge(77, 42, "login", "discord")
        self.assertFalse(security.verify(77, 43, code, nonce, "login"))
        self.assertFalse(security.verify(77, 42, code, nonce, "manage"))
        for _ in range(5):
            self.assertFalse(security.verify(77, 42, "bad", nonce, "login"))
        self.assertFalse(security.verify(77, 42, code, nonce, "login"))
        nonce, code = security.challenge(77, 42, "login", "discord")
        with patch("persistence.account_security_repository.time.time", return_value=time.time() + 301):
            self.assertFalse(security.verify(77, 42, code, nonce, "login"))

    def test_challenge_requests_are_rate_limited(self):
        self.enroll("discord")
        for _ in range(2):
            security.challenge(77, 42, "login", "discord")
        with self.assertRaisesRegex(ValueError, "challenge_rate_limited"):
            security.challenge(77, 42, "login", "discord")

    def test_sessions_need_current_factor_assurance_including_alternative_logins(self):
        self.enroll()
        token, _ = create_session_token(guild_id=77, user_id=42)
        self.assertFalse(security.session_allowed(77, 42, _verify(token, purpose="session")))
        token, _ = create_session_token(guild_id=77, user_id=42, mfa_verified=True)
        self.assertTrue(security.session_allowed(77, 42, _verify(token, purpose="session")))

    def test_missing_key_fails_closed(self):
        with patch.dict(os.environ, {"TMOD_ACCOUNT_SECURITY_KEY": ""}):
            self.assertFalse(security.configured())
            with self.assertRaisesRegex(ValueError, "security_key_unavailable"):
                security.begin_enrollment(77, 42, "totp", "operator")

    def test_password_change_revokes_even_legacy_sessions_without_credential_version(self):
        token, _ = create_session_token(guild_id=77, user_id=42)
        old_payload = _verify(token, purpose="session")
        security.change_credential(77, 42, "12345678", "long-and-unique-password", "password")
        self.assertFalse(security.session_allowed(77, 42, old_payload))

    def test_legacy_pin_reset_does_not_disable_second_factor(self):
        self.enroll()
        security.change_credential(77, 42, "12345678", "long-and-unique-password", "password")
        credentials.configure_web_credential(77, 42, "operator", "87654321")
        selected = security.state(77, 42)
        self.assertEqual(selected["credential_kind"], "pin")
        self.assertEqual(selected["mfa_method"], "totp")
        self.assertEqual(credentials.authenticate_web_credential(77, "operator", "87654321").status, "ok")

    def test_stale_confirmation_cannot_change_credentials_or_gain_new_assurance(self):
        self.enroll()
        old = security.state(77, 42)["security_version"]
        security.disable(77, 42, old)
        with self.assertRaisesRegex(ValueError, "verification_invalid"):
            security.change_credential(77, 42, "12345678", "unique-new-password", "password", old)
        token, _ = create_session_token(guild_id=77, user_id=42, mfa_verified=True, account_security_version=old)
        self.assertFalse(security.session_allowed(77, 42, _verify(token, purpose="session")))

    def test_rotated_key_is_a_controlled_failure_not_factor_downgrade(self):
        self.enroll()
        nonce, _ = security.challenge(77, 42, "login", "totp")
        with patch.dict(os.environ, {"TMOD_ACCOUNT_SECURITY_KEY": Fernet.generate_key().decode()}):
            with self.assertRaisesRegex(ValueError, "security_key_unavailable"):
                security.verify(77, 42, "123456", nonce, "login")
        self.assertEqual(security.state(77, 42)["mfa_method"], "totp")

    def test_security_secrets_are_omitted_from_global_log_and_privacy_export(self):
        from modules.global_log_runtime import _response_payload, scrub_text, redact_value
        from modules.privacy_export import _sanitize
        self.assertNotIn("123456", scrub_text("Blackbird · код подтверждения: 123456"))
        response = web.json_response({"secret": "TOTPKEY", "recovery_codes": ["PRIVATECODE"]})
        self.assertEqual(_response_payload(response, "/api/account/security"), {"omitted": "authentication_secrets"})
        self.assertNotIn("PRIVATECODE", _sanitize("PRIVATECODE", key="recovery_hashes", subject_id=42))
        modal = {"components": [{"type": 1, "components": [{"type": 4, "custom_id": "random-id", "value": "my-private-password"}]}]}
        self.assertNotIn("my-private-password", json.dumps(redact_value(modal)))
        self.assertNotIn("my-private-password", json.dumps(redact_value({"name":"password", "value":"my-private-password"})))

    async def test_delivery_failure_still_allows_recovery_code_login(self):
        recovery = self.enroll("discord")
        from modules.consensus_web import create_consensus_web_app
        member = SimpleNamespace(id=42, display_name="Operator", guild_permissions=SimpleNamespace(administrator=True), roles=[])
        guild = SimpleNamespace(id=77, name="Test", get_member=lambda user: member)
        bot = SimpleNamespace(get_guild=lambda gid: guild, get_user=lambda user: None, fetch_user=AsyncMock(side_effect=RuntimeError("offline")))
        async with TestClient(TestServer(create_consensus_web_app(bot, guild_id=77)), headers={"X-TMod-Desktop-Edition": "blackbird"}) as client:
            response = await client.post("/auth/login?client=desktop", data={"login": "operator", "pin": "12345678"}, allow_redirects=False)
            self.assertEqual(response.status, 202)
            data = await response.json()
            self.assertTrue(data["delivery_failed"])
            response = await client.post("/auth/login?client=desktop", data={"login": "operator", "pin": "12345678", "code": recovery[0], "challenge": data["challenge"]}, allow_redirects=False)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {"ok": True})
            self.assertIn("tmod_account_session", response.cookies)

    def test_restarted_enrollment_invalidates_old_confirmation(self):
        old = security.begin_enrollment(77, 42, "totp", "operator")
        nonce, _ = security.challenge(77, 42, "enroll", "totp")
        security.begin_enrollment(77, 42, "totp", "operator")
        self.assertFalse(security.verify(77, 42, security.totp(old["secret"], int(time.time()) // 30), nonce, "enroll"))

    async def test_http_settings_require_session_csrf_and_reauthentication(self):
        selected = SimpleNamespace(user_id=42, csrf_token="csrf-test")
        async def authenticate(request):
            return (selected if request.headers.get("X-Test-Session") else None), False
        app = web.Application()
        register_account_security_routes(app, SimpleNamespace(), guild_id=77, authenticate=authenticate)
        async with TestClient(TestServer(app)) as client:
            self.assertEqual((await client.get("/api/account/security")).status, 401)
            headers = {"X-Test-Session": "yes", "X-TMod-Desktop-Edition": "blackbird"}
            response = await client.get("/api/account/security", headers=headers)
            payload = await response.json()
            self.assertNotIn("mfa_secret", payload)
            self.assertEqual(payload["links"]["discord"], "42")
            request = {"action": "credential", "current": "12345678", "kind": "password", "value": "my-unique-password"}
            self.assertEqual((await client.post("/api/account/security", json=request, headers=headers)).status, 403)
            headers["X-CSRF-Token"] = "csrf-test"
            request["current"] = "incorrect"
            self.assertEqual((await client.post("/api/account/security", json=request, headers=headers)).status, 400)
            request["current"] = "12345678"
            response = await client.post("/api/account/security", json=request, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertIn("tmod_account_session", response.cookies)

    async def test_full_login_requires_factor_before_issuing_cookie(self):
        recovery = self.enroll()
        from modules.consensus_web import create_consensus_web_app
        member = SimpleNamespace(id=42, display_name="Operator", guild_permissions=SimpleNamespace(administrator=True), roles=[])
        guild = SimpleNamespace(id=77, name="Test", get_member=lambda user: member, fetch_member=AsyncMock(return_value=member))
        bot = SimpleNamespace(get_guild=lambda guild_id: guild)
        async with TestClient(TestServer(create_consensus_web_app(bot, guild_id=77)), headers={"X-TMod-Desktop-Edition": "blackbird"}) as client:
            legacy, _ = create_session_token(guild_id=77, user_id=42)
            denied = await client.get("/api/account/security", headers={"Cookie": f"tmod_account_session={legacy}"})
            self.assertEqual(denied.status, 401)
            response = await client.post("/auth/login?client=desktop", data={"login": "operator", "pin": "12345678"}, allow_redirects=False)
            self.assertEqual(response.status, 202)
            self.assertNotIn("tmod_account_session", response.cookies)
            challenge = (await response.json())["challenge"]
            response = await client.post("/auth/login?client=desktop", data={"login": "operator", "pin": "12345678", "challenge": challenge, "code": recovery[0]}, allow_redirects=False)
            self.assertEqual(response.status, 200)
            self.assertIn("tmod_account_session", response.cookies)
            payload = _verify(response.cookies["tmod_account_session"].value, purpose="session")
            self.assertTrue(security.session_allowed(77, 42, payload))

    async def test_desktop_json_login_preserves_cookie_and_browser_redirect(self):
        from modules.consensus_web import create_consensus_web_app
        member = SimpleNamespace(id=42, display_name="Operator", guild_permissions=SimpleNamespace(administrator=True), roles=[])
        guild = SimpleNamespace(id=77, name="Test", get_member=lambda user: member)
        bot = SimpleNamespace(get_guild=lambda gid: guild)
        async with TestClient(TestServer(create_consensus_web_app(bot, guild_id=77))) as client:
            bad = await client.post("/auth/login?client=desktop", data={"login": "operator", "pin": "00000000"}, allow_redirects=False)
            self.assertEqual(bad.status, 401)
            self.assertEqual((await bad.json())["error"], "invalid")
            self.assertNotIn("tmod_account_session", bad.cookies)
            accepted = await client.post("/auth/login?client=desktop", data={"login": "operator", "pin": "12345678"}, allow_redirects=False)
            self.assertEqual(accepted.status, 200)
            self.assertEqual(await accepted.json(), {"ok": True})
            self.assertIn("tmod_account_session", accepted.cookies)
            self.assertNotIn("Location", accepted.headers)
            browser = await client.post("/auth/login", data={"login": "operator", "pin": "12345678"}, allow_redirects=False)
            self.assertEqual(browser.status, 303)
