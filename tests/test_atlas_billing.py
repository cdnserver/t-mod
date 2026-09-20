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

from modules.atlas_ai import (
    _ATLAS_USAGE_EVENTS,
    _capture_provider_usage,
    _current_usage_summary,
)
from modules.atlas_billing import (
    atlas_billing_catalog,
    atlas_tokens_for_cost,
)
from modules.atlas_billing_web import (
    register_atlas_billing_web_routes,
    require_atlas_billing_host,
)
from persistence import atlas_billing_repository, atlas_repository, web_auth_repository
from persistence.core import connect


class AtlasBillingRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-billing-test.db"
        storage.init_db()
        self.organization_id = int(
            atlas_repository.atlas_dashboard(77, 42, "Иван")["organization"]["id"]
        )

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_catalog_has_four_plans_and_manual_features_remain_free(self) -> None:
        catalog = atlas_billing_catalog()

        self.assertEqual([item["code"] for item in catalog["plans"]], [
            "free", "start", "pro", "sovereign",
        ])
        self.assertEqual(catalog["plans"][-1]["monthly_price_rub"], 29_990)
        self.assertTrue(all(item["manual_features"] == "unlimited" for item in catalog["plans"]))
        self.assertFalse(catalog["availability"]["commercial_sales"])
        self.assertEqual(catalog["service"]["software_price_rub"], 0)
        self.assertIn("T-Mod Desktop", catalog["service"]["required_software"])

    def test_admin_grant_is_idempotent_and_never_expires(self) -> None:
        first = atlas_billing_repository.atlas_admin_grant_tokens(
            825331775857360906,
            actor_user_id=902235631952998410,
            amount_tokens=750_000,
            reason="Компенсация после технического инцидента",
            request_key="admin-grant-test-1",
        )
        repeated = atlas_billing_repository.atlas_admin_grant_tokens(
            825331775857360906,
            actor_user_id=902235631952998410,
            amount_tokens=750_000,
            reason="Повтор не должен создать новое начисление",
            request_key="admin-grant-test-1",
        )

        self.assertEqual(first["entry"]["id"], repeated["entry"]["id"])
        self.assertEqual(first["entry"]["balance_bucket"], "payg")
        self.assertIsNone(first["entry"]["expires_at"])
        self.assertEqual(repeated["summary"]["payg_balance_tokens"], 750_000)
        with connect() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM atlas_token_ledger WHERE entry_kind = 'admin_grant'"
                ).fetchone()[0],
                1,
            )

    def test_provider_cost_is_converted_to_atlas_tokens_with_ceiling(self) -> None:
        self.assertEqual(atlas_tokens_for_cost("0"), 0)
        self.assertEqual(atlas_tokens_for_cost("0.00001"), 1)
        self.assertEqual(atlas_tokens_for_cost("0.012345"), 1235)

    def test_monthly_allowance_and_usage_are_idempotent(self) -> None:
        first = atlas_billing_repository.atlas_billing_summary(42)
        second = atlas_billing_repository.atlas_billing_summary(42)

        self.assertEqual(first["balance_tokens"], 50_000)
        self.assertEqual(second["balance_tokens"], 50_000)
        recorded = atlas_billing_repository.atlas_record_ai_usage(
            42,
            self.organization_id,
            request_key="web:101",
            source="desktop",
            model="openai/gpt-test",
            model_provider="openrouter",
            usage={
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "model_calls": 1,
                "provider_cost_microusd": 1_000,
            },
            message_id=None,
        )
        duplicate = atlas_billing_repository.atlas_record_ai_usage(
            42,
            self.organization_id,
            request_key="web:101",
            source="desktop",
            model="openai/gpt-test",
            model_provider="openrouter",
            usage={"provider_cost_microusd": 1_000},
        )
        summary = atlas_billing_repository.atlas_billing_summary(42)
        metrics = atlas_billing_repository.atlas_billing_admin_metrics()

        self.assertEqual(recorded["atlas_tokens"], 100)
        self.assertEqual(duplicate["id"], recorded["id"])
        self.assertEqual(summary["monthly_balance_tokens"], 49_900)
        self.assertEqual(summary["payg_balance_tokens"], 0)
        self.assertEqual(summary["period_usage"]["requests"], 1)
        self.assertEqual(metrics["requests"], 1)
        self.assertEqual(metrics["provider_cost_microusd"], 1_000)
        with connect() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM atlas_token_ledger WHERE entry_kind = 'ai_usage'"
                ).fetchone()[0],
                1,
            )

    def test_subscription_and_payg_settlement_are_idempotent(self) -> None:
        subscription = atlas_billing_repository.atlas_create_payment_order(
            42,
            product_kind="subscription",
            product_code="start",
            amount_kopecks=99_000,
            atlas_tokens=1_000_000,
            plan_code="start",
        )
        atlas_billing_repository.atlas_settle_payment_order(subscription["id"])
        atlas_billing_repository.atlas_settle_payment_order(subscription["id"])
        pack = atlas_billing_repository.atlas_create_payment_order(
            42,
            product_kind="token_pack",
            product_code="at-100k",
            amount_kopecks=9_900,
            atlas_tokens=100_000,
        )
        atlas_billing_repository.atlas_settle_payment_order(pack["id"])
        summary = atlas_billing_repository.atlas_billing_summary(42)

        self.assertEqual(summary["plan"]["code"], "start")
        self.assertEqual(summary["monthly_balance_tokens"], 1_000_000)
        self.assertEqual(summary["payg_balance_tokens"], 100_000)
        self.assertEqual(summary["balance_tokens"], 1_100_000)
        with connect() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM atlas_token_ledger WHERE entry_kind = 'payment'"
                ).fetchone()[0],
                2,
            )

    def test_active_subscription_can_be_renewed_or_upgraded_without_losing_balance(self) -> None:
        first = atlas_billing_repository.atlas_create_payment_order(
            42,
            product_kind="subscription",
            product_code="start",
            amount_kopecks=99_000,
            atlas_tokens=1_000_000,
            plan_code="start",
            checkout_key="renewal-first-00000001",
        )
        atlas_billing_repository.atlas_settle_payment_order(first["id"])
        first_summary = atlas_billing_repository.atlas_billing_summary(42)

        renewal = atlas_billing_repository.atlas_create_payment_order(
            42,
            product_kind="subscription",
            product_code="start",
            amount_kopecks=99_000,
            atlas_tokens=1_000_000,
            plan_code="start",
            checkout_key="renewal-second-0000002",
        )
        atlas_billing_repository.atlas_settle_payment_order(renewal["id"])
        renewed = atlas_billing_repository.atlas_billing_summary(42)
        self.assertEqual(renewed["monthly_balance_tokens"], 2_000_000)
        self.assertGreater(
            renewed["account"]["subscription_expires_at"],
            first_summary["account"]["subscription_expires_at"],
        )

        upgrade = atlas_billing_repository.atlas_create_payment_order(
            42,
            product_kind="subscription",
            product_code="pro",
            amount_kopecks=499_000,
            atlas_tokens=5_000_000,
            plan_code="pro",
            checkout_key="upgrade-pro-00000000001",
        )
        atlas_billing_repository.atlas_settle_payment_order(upgrade["id"])
        upgraded = atlas_billing_repository.atlas_billing_summary(42)
        self.assertEqual(upgraded["plan"]["code"], "pro")
        self.assertEqual(upgraded["monthly_balance_tokens"], 7_000_000)

    def test_existing_accounts_receive_transition_reserve_exactly_once(self) -> None:
        web_auth_repository.configure_web_credential(77, 73, "legacy-user", "12345678")
        with connect() as con:
            con.execute(
                """
                INSERT INTO members(
                    guild_id, user_id, display_name, name, mention, is_bot,
                    created_at, updated_at
                ) VALUES(77, 74, 'Участник', 'member', '<@74>', 0, '2026-09-15', '2026-09-15')
                """
            )
            con.execute(
                "DELETE FROM meta WHERE key = ?",
                ("migration:atlas-billing-launch:2026-09-15-v1",),
            )
            con.commit()

        storage.init_db()
        storage.init_db()
        summary = atlas_billing_repository.atlas_billing_summary(73)

        self.assertEqual(summary["payg_balance_tokens"], 10_000_000)
        self.assertEqual(summary["monthly_balance_tokens"], 50_000)
        self.assertEqual(
            atlas_billing_repository.atlas_billing_summary(74)["payg_balance_tokens"],
            10_000_000,
        )
        with connect() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM atlas_token_ledger WHERE reference_key = ?",
                    ("legacy-launch:73",),
                ).fetchone()[0],
                1,
            )


class AtlasProviderUsageTests(unittest.TestCase):
    def test_openrouter_reported_cost_is_aggregated(self) -> None:
        _ATLAS_USAGE_EVENTS.set([])
        _capture_provider_usage(
            "https://openrouter.ai/api/v1/chat/completions",
            {"model": "openai/gpt-test"},
            {
                "model": "openai/gpt-test",
                "usage": {
                    "prompt_tokens": 250,
                    "completion_tokens": 50,
                    "total_tokens": 300,
                    "cost": 0.001234,
                },
            },
        )

        usage = _current_usage_summary()
        self.assertEqual(usage["prompt_tokens"], 250)
        self.assertEqual(usage["completion_tokens"], 50)
        self.assertEqual(usage["provider_cost_microusd"], 1234)
        self.assertEqual(usage["model_calls"], 1)
        self.assertEqual(usage["calls"][0]["provider"], "openrouter")

    def test_tiny_positive_provider_cost_is_not_rounded_to_free(self) -> None:
        _ATLAS_USAGE_EVENTS.set([])
        _capture_provider_usage(
            "https://openrouter.ai/api/v1/chat/completions",
            {"model": "openai/gpt-test"},
            {"usage": {"total_tokens": 1, "cost": 0.0000004}},
        )

        self.assertEqual(_current_usage_summary()["provider_cost_microusd"], 1)


class AtlasBillingWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-billing-web-test.db"
        storage.init_db()

    async def asyncTearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    async def test_catalog_page_and_authenticated_summary(self) -> None:
        principal = SimpleNamespace(
            user_id=42, display_name="Иван", csrf_token="csrf", administrator=True
        )

        async def authenticate(_request):
            return principal, False

        app = web.Application()
        register_atlas_billing_web_routes(
            app,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas-billing",
            authenticate=authenticate,
        )
        with patch.dict(os.environ, {"ATLAS_BILLING_ENFORCEMENT_ENABLED": "1"}, clear=False):
            async with TestClient(TestServer(app)) as client:
                page = await client.get("/atlas-billing")
                account = await client.get("/account")
                legal = await client.get("/legal")
                offer = await client.get("/offer")
                terms = await client.get("/terms")
                privacy = await client.get("/privacy")
                refunds = await client.get("/refunds")
                contacts = await client.get("/contacts")
                data_request = await client.get("/data-request")
                retired_document = await client.get("/documents/retired.pdf")
                catalog = await client.get("/api/atlas/billing/catalog")
                summary = await client.get("/api/atlas/billing/summary")
                metrics = await client.get("/api/atlas/billing/admin/metrics")
                checkout = await client.post(
                    "/api/atlas/billing/checkout",
                    headers={"X-CSRF-Token": "csrf"},
                    json={"product_kind": "subscription", "product_code": "start"},
                )

                self.assertEqual(page.status, 200)
                self.assertIn("Atlas Token", await page.text())
                self.assertIn("Понятные правила", await legal.text())
                self.assertIn("ЛИЧНЫЙ КАБИНЕТ", await account.text())
                self.assertIn("Платные услуги", await offer.text())
                offer_text = await (await client.get("/offer")).text()
                self.assertIn("не принимает денежную оплату", offer_text)
                self.assertIn("ПРАВИЛА ATLAS", await terms.text())
                self.assertIn("Политика обработки персональных данных", await privacy.text())
                self.assertIn("Новые заказы", await refunds.text())
                self.assertIn("ТЕХНИЧЕСКАЯ ПОДДЕРЖКА", await contacts.text())
                self.assertIn("privacy-request-form", await data_request.text())
                self.assertEqual(retired_document.status, 404)
                self.assertEqual(catalog.status, 200)
                self.assertFalse((await catalog.json())["payments"]["enabled"])
                self.assertEqual(summary.status, 200)
                self.assertEqual(metrics.status, 200)
                self.assertEqual((await metrics.json())["window_days"], 30)
                self.assertEqual((await summary.json())["balance_tokens"], 50_000)
                self.assertEqual(checkout.status, 410)
                self.assertEqual((await checkout.json())["error"], "atlas_payments_disabled")

                require_atlas_billing_host(
                    SimpleNamespace(
                        headers={}, remote="203.0.113.10", host="atlas.tvr.lat", secure=True
                    )
                )
                with self.assertRaises(web.HTTPNotFound):
                    require_atlas_billing_host(
                        SimpleNamespace(
                            headers={}, remote="203.0.113.10", host="tvr.lat", secure=True
                        )
                    )

    async def test_summary_serializes_postgres_decimal_aggregates(self) -> None:
        principal = SimpleNamespace(
            user_id=42, display_name="Иван", csrf_token="csrf", administrator=True
        )

        async def authenticate(_request):
            return principal, False

        app = web.Application()
        register_atlas_billing_web_routes(
            app,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas-billing",
            authenticate=authenticate,
        )
        postgres_result = {
            "balance_tokens": 50_000,
            "period_usage": {"atlas_tokens": Decimal("2581")},
            "usage_by_model": [
                {"model": "openai/gpt-5-mini", "atlas_tokens": Decimal("2581")}
            ],
            "diagnostic_ratio": Decimal("1.25"),
        }
        with patch.object(
            atlas_billing_repository,
            "atlas_billing_summary",
            return_value=postgres_result,
        ):
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/api/atlas/billing/summary")
                body = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(body["period_usage"]["atlas_tokens"], 2581)
        self.assertEqual(body["usage_by_model"][0]["atlas_tokens"], 2581)
        self.assertEqual(body["diagnostic_ratio"], "1.25")

    async def test_checkout_is_permanently_disabled(self) -> None:
        principal = SimpleNamespace(user_id=42, display_name="Иван", csrf_token="csrf")

        async def authenticate(_request):
            return principal, False

        app = web.Application()
        register_atlas_billing_web_routes(
            app,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas-billing",
            authenticate=authenticate,
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/atlas/billing/checkout",
                headers={"X-CSRF-Token": "csrf"},
                json={"product_kind": "token_pack", "product_code": "at-100k"},
            )
            body = await response.json()

        self.assertEqual(response.status, 410)
        self.assertEqual(body["error"], "atlas_payments_disabled")
        with connect() as con:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM atlas_payment_orders").fetchone()[0],
                0,
            )

    async def test_admin_metrics_are_forbidden_for_regular_account(self) -> None:
        principal = SimpleNamespace(
            user_id=42, display_name="Иван", csrf_token="csrf", administrator=False
        )

        async def authenticate(_request):
            return principal, False

        app = web.Application()
        register_atlas_billing_web_routes(
            app,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas-billing",
            authenticate=authenticate,
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/atlas/billing/admin/metrics")

        self.assertEqual(response.status, 403)

class AtlasBillingDeploymentTests(unittest.TestCase):
    def test_atlas_is_the_direct_store_and_examples_keep_secrets_empty(self) -> None:
        root = Path(__file__).resolve().parents[1]
        caddy = (root / "Caddyfile").read_text(encoding="utf-8")
        persistent = (root / ".env.persistent.example").read_text(encoding="utf-8")

        self.assertIn("atlas.tvr.lat {", caddy)
        self.assertIn("rewrite * /atlas-billing", caddy)
        self.assertIn("dash.tvr.lat {", caddy)
        self.assertIn("rewrite * /atlas", caddy)
        self.assertIn("ATLAS_BILLING_ENFORCEMENT_ENABLED=true", persistent)
        self.assertFalse((root / "web" / "atlas-billing" / "documents" / "atlas-public-offer.pdf").is_file())
        self.assertFalse((root / "web" / "atlas-billing" / "documents" / "atlas-privacy-policy.pdf").is_file())

    def test_public_legal_documents_are_complete_and_name_required_client(self) -> None:
        root = Path(__file__).resolve().parents[1]
        legal_dir = root / "web" / "atlas-billing"
        documents = [
            (legal_dir / name).read_text(encoding="utf-8")
            for name in ("offer.html", "privacy.html", "terms.html", "refunds.html", "legal.html", "contacts.html")
        ]
        combined = "\n".join(documents).casefold()

        self.assertNotIn("discord", combined)
        self.assertNotIn("дискорд", combined)
        self.assertIn("платные услуги", combined)
        self.assertIn("денежную оплату", combined)
        self.assertIn("технологии товарищества", combined)
        self.assertIn("управление персональными данными", combined)

    def test_store_distinguishes_promotional_copy_from_contract_terms(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "atlas-billing" / "index.html").read_text(encoding="utf-8")

        self.assertIn("Некоммерческий режим.", html)
        self.assertIn("Денежная оплата, подписки", html)
        self.assertIn("Скачать T‑Mod Desktop", html)
        self.assertNotIn("checkout-dialog", html)

    def test_login_returns_to_account_without_purchase_resume(self) -> None:
        root = Path(__file__).resolve().parents[1]
        login = (root / "web" / "consensus" / "login.js").read_text(encoding="utf-8")
        store = (root / "web" / "atlas-billing" / "app.js").read_text(encoding="utf-8")

        self.assertIn('"/account"', login)
        self.assertIn('form.action = `/auth/login?next=${encodeURIComponent(next)}`', login)
        self.assertIn("Подключение закрыто", store)
        self.assertNotIn("pending-purchase", store)
