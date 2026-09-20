import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import storage
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from modules.atlas_finance_admin_web import register_atlas_finance_admin_routes
from modules.atlas_legal_pdf import render_legal_pdf
from persistence import atlas_billing_repository, atlas_finance_repository


class AtlasFinanceRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "finance.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_subscription_and_content_publication_are_idempotent_and_versioned(self) -> None:
        first = atlas_finance_repository.grant_subscription(
            825331775857360906,
            plan_code="pro",
            days=45,
            actor_user_id=902235631952998410,
            reason="Партнёрская программа Atlas",
            request_key="subscription-test-1",
        )
        repeated = atlas_finance_repository.grant_subscription(
            825331775857360906,
            plan_code="pro",
            days=45,
            actor_user_id=902235631952998410,
            reason="Повтор запроса",
            request_key="subscription-test-1",
        )
        self.assertTrue(first["created"])
        self.assertFalse(repeated["created"])
        self.assertEqual(first["summary"]["account"]["plan_code"], "pro")

        draft = atlas_finance_repository.save_content(
            "footer_note", content_text="Первая редакция", publish=False,
            actor_user_id=1, request_key="content-draft-1",
        )
        published = atlas_finance_repository.save_content(
            "footer_note", content_text="Действующая редакция", publish=True,
            actor_user_id=1, request_key="content-publish-1",
        )
        public = atlas_finance_repository.content_manifest()
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(published["status"], "published")
        self.assertEqual(public["slots"]["footer_note"]["content_text"], "Действующая редакция")

    def test_profit_uses_provider_and_manual_costs(self) -> None:
        order = atlas_billing_repository.atlas_create_payment_order(
            42, product_kind="token_pack", product_code="at-100k",
            amount_kopecks=9900, atlas_tokens=100_000, checkout_key="finance-order-1",
        )
        atlas_billing_repository.atlas_settle_payment_order(order["id"])
        atlas_finance_repository.add_cost(
            category="infrastructure", title="VDS", amount_kopecks=1200,
            occurred_at="2026-09-19T12:00:00+00:00", note="",
            actor_user_id=1, request_key="finance-cost-1",
        )
        result = atlas_finance_repository.finance_dashboard(days=365, usd_rub_rate=Decimal("100"))
        self.assertEqual(result["revenue_kopecks"], 9900)
        self.assertEqual(result["manual_cost_kopecks"], 1200)
        self.assertEqual(result["profit_kopecks"], 8700)

    def test_legal_revision_builds_one_immutable_pdf(self) -> None:
        revision = atlas_finance_repository.create_legal_revision(
            "offer", version_label="Редакция от 19.09.2026", title="Публичная оферта Atlas",
            summary="Условия оказания услуги", effective_from="2026-09-19T00:00:00+00:00",
            blocks=[
                {"type": "section", "title": "Предмет", "text": ""},
                {"type": "paragraph", "text": "Исполнитель оказывает информационно-вычислительную услугу."},
                {"type": "list", "title": "Пользователь", "items": ["Выбирает пакет", "Принимает условия"]},
            ], actor_user_id=1, request_key="legal-offer-1",
        )
        with patch.dict(os.environ, {"ATLAS_LEGAL_REVISION_DIR": self.temp_dir.name}):
            path, digest = render_legal_pdf(revision)
        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 1000)
        self.assertEqual(len(digest), 64)


class AtlasFinanceWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "finance-web.db"
        storage.init_db()

    async def asyncTearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    async def _client(self, administrator: bool) -> TestClient:
        selected = SimpleNamespace(
            user_id=902235631952998410, display_name="Администратор",
            csrf_token="csrf", administrator=administrator,
        )

        async def authenticate(_request):
            return selected, False

        app = web.Application()
        register_atlas_finance_admin_routes(
            app, guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas-finance-admin",
            authenticate=authenticate,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        return client

    async def test_non_administrator_cannot_open_plane_or_api(self) -> None:
        client = await self._client(False)
        try:
            page = await client.get("/atlas-finance-admin", headers={"Host": "ap.finance.tvr.lat"})
            api = await client.get("/api/admin/atlas-finance/bootstrap", headers={"Host": "ap.finance.tvr.lat"})
            self.assertEqual(page.status, 403)
            self.assertEqual(api.status, 403)
        finally:
            await client.close()

    async def test_administrator_can_load_dashboard_and_mutation_requires_csrf(self) -> None:
        client = await self._client(True)
        try:
            page = await client.get("/atlas-finance-admin", headers={"Host": "ap.finance.tvr.lat"})
            overview = await client.get("/api/admin/atlas-finance/bootstrap", headers={"Host": "ap.finance.tvr.lat"})
            rejected = await client.post(
                "/api/admin/atlas-finance/costs", headers={"Host": "ap.finance.tvr.lat"}, json={},
            )
            self.assertEqual(page.status, 200)
            self.assertIn("ADMINISTRATION PLANE", await page.text())
            self.assertEqual(overview.status, 200)
            self.assertIn("dashboard", await overview.json())
            self.assertEqual(rejected.status, 403)
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
