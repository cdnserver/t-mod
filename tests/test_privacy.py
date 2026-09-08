from __future__ import annotations

import tempfile
import unittest
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import storage
from modules.legal_web import register_legal_web_routes
from modules.privacy_export import build_personal_data_archive
from persistence import privacy_repository


class PrivacyRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "privacy-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_request_is_durable_and_idempotent(self) -> None:
        payload = dict(
            receipt_key="privacy-receipt-123456789",
            request_type="erase",
            requester_email="Owner@Example.com",
            account_login="owner",
            discord_id="902235631952998410",
            account_user_id=None,
            scope="Аккаунт и история Atlas",
            details="Прошу удалить после проверки личности.",
            remote_hash="hashed-address",
            user_agent="test",
        )
        first, created = privacy_repository.create_privacy_request(**payload)
        retry, created_again = privacy_repository.create_privacy_request(**payload)

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["request_code"], retry["request_code"])
        self.assertEqual(first["requester_email"], "owner@example.com")
        self.assertEqual(
            privacy_repository.get_privacy_request(first["request_code"])["id"],
            first["id"],
        )

    def test_request_needs_a_searchable_account_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "privacy_identity_required"):
            privacy_repository.create_privacy_request(
                receipt_key="privacy-receipt-987654321",
                request_type="access",
                requester_email="owner@example.com",
                account_login=None,
                discord_id=None,
                account_user_id=None,
                scope="Все данные",
                details=None,
                remote_hash=None,
                user_agent=None,
            )

    def test_verified_access_request_builds_redacted_archive_and_can_be_completed(self) -> None:
        item, _ = privacy_repository.create_privacy_request(
            receipt_key="privacy-access-receipt-123456",
            request_type="access",
            requester_email="owner@example.com",
            account_login="owner",
            discord_id="697452945083727962",
            account_user_id=697452945083727962,
            scope="Все данные",
            details=None,
            remote_hash="hashed-address",
            user_agent="test",
        )
        pending = privacy_repository.list_verified_access_requests()
        self.assertEqual([row["id"] for row in pending], [item["id"]])
        claimed = privacy_repository.claim_verified_access_request(item["id"])
        self.assertEqual(claimed["status"], "processing")

        archive = build_personal_data_archive(claimed)
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            payload = json.loads(bundle.read("personal-data.json"))
        self.assertEqual(payload["subject"]["discord_user_id"], 697452945083727962)
        record = payload["main_database"]["privacy_requests"]["records"][0]
        self.assertEqual(record["receipt_key"], "[не раскрывается для безопасности аккаунта]")

        privacy_repository.complete_verified_access_request(item["id"])
        resolved = privacy_repository.get_privacy_request(item["request_code"])
        self.assertEqual(resolved["status"], "resolved")
        self.assertIsNotNone(resolved["response_sent_at"])


class PrivacyWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "privacy-web-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    async def test_public_form_creates_one_request_across_retry(self) -> None:
        app = web.Application()
        bot = SimpleNamespace(get_guild=lambda _guild_id: None)
        register_legal_web_routes(
            app,
            bot,  # type: ignore[arg-type]
            guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "consensus",
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        payload = {
            "receipt": "browser-receipt-123456789",
            "request_type": "withdraw",
            "email": "owner@example.com",
            "discord_id": "902235631952998410",
            "account_login": "",
            "scope": "Atlas и аккаунт",
            "details": "Отзываю необязательное согласие.",
            "website": "",
            "acknowledge": True,
        }

        first = await client.post("/api/privacy/requests", json=payload)
        first_body = await first.json()
        retry = await client.post("/api/privacy/requests", json=payload)
        retry_body = await retry.json()

        self.assertEqual(first.status, 201)
        self.assertEqual(retry.status, 200)
        self.assertEqual(first_body["request_code"], retry_body["request_code"])
        self.assertFalse(retry_body["created"])

    async def test_public_form_rejects_unverifiable_identity(self) -> None:
        app = web.Application()
        register_legal_web_routes(
            app,
            SimpleNamespace(get_guild=lambda _guild_id: None),  # type: ignore[arg-type]
            guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "consensus",
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        response = await client.post(
            "/api/privacy/requests",
            json={
                "receipt": "browser-receipt-987654321",
                "request_type": "erase",
                "email": "owner@example.com",
                "discord_id": "",
                "account_login": "",
                "scope": "Аккаунт",
                "details": "",
                "website": "",
                "acknowledge": True,
            },
        )

        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"], "privacy_identity_required")


if __name__ == "__main__":
    unittest.main()
