import hashlib
import os
import tempfile
import unittest
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
    robokassa_payment_fields,
    robokassa_result_is_valid,
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
        self.assertEqual(catalog["seller"]["name"], "ИП Саниев Муртазали Бухариевич")
        self.assertEqual(catalog["seller"]["inn"], "370266611106")

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


class RobokassaSignatureTests(unittest.TestCase):
    @patch.dict(
        os.environ,
        {
            "ATLAS_BILLING_PAYMENTS_ENABLED": "1",
            "ROBOKASSA_MERCHANT_LOGIN": "tvr.lat",
            "ROBOKASSA_PASSWORD1": "password-one",
            "ROBOKASSA_PASSWORD2": "password-two",
            "ROBOKASSA_TEST_MODE": "1",
        },
        clear=False,
    )
    def test_checkout_and_result_signatures_follow_robokassa_order(self) -> None:
        fields = robokassa_payment_fields(
            invoice_id=17,
            amount_kopecks=99_000,
            description="Atlas Start",
            user_id=42,
        )
        expected = hashlib.md5(
            b"tvr.lat:990.00:17:password-one:Shp_user=42"
        ).hexdigest().upper()
        self.assertEqual(fields["SignatureValue"], expected)
        self.assertEqual(fields["IsTest"], "1")

        result = {
            "OutSum": "990.00",
            "InvId": "17",
            "Shp_user": "42",
            "SignatureValue": hashlib.md5(
                b"990.00:17:password-two:Shp_user=42"
            ).hexdigest().upper(),
        }
        self.assertTrue(robokassa_result_is_valid(result))
        self.assertFalse(robokassa_result_is_valid({**result, "OutSum": "991.00"}))


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
        with patch.dict(os.environ, {"ATLAS_BILLING_PAYMENTS_ENABLED": "0"}, clear=False):
            async with TestClient(TestServer(app)) as client:
                page = await client.get("/atlas-billing")
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
                self.assertEqual(catalog.status, 200)
                self.assertFalse((await catalog.json())["payments"]["enabled"])
                self.assertEqual(summary.status, 200)
                self.assertEqual(metrics.status, 200)
                self.assertEqual((await metrics.json())["window_days"], 30)
                self.assertEqual((await summary.json())["balance_tokens"], 50_000)
                self.assertEqual(checkout.status, 503)

                require_atlas_billing_host(
                    SimpleNamespace(
                        headers={}, remote="203.0.113.10", host="dash.tvr.lat", secure=True
                    )
                )
                with self.assertRaises(web.HTTPNotFound):
                    require_atlas_billing_host(
                        SimpleNamespace(
                            headers={}, remote="203.0.113.10", host="tvr.lat", secure=True
                        )
                    )

    @patch.dict(
        os.environ,
        {
            "ATLAS_BILLING_PAYMENTS_ENABLED": "1",
            "ROBOKASSA_MERCHANT_LOGIN": "tvr.lat",
            "ROBOKASSA_PASSWORD1": "password-one",
            "ROBOKASSA_PASSWORD2": "password-two",
            "ROBOKASSA_TEST_MODE": "1",
        },
        clear=False,
    )
    async def test_checkout_and_result_callback_are_idempotent(self) -> None:
        principal = SimpleNamespace(user_id=42, display_name="Иван", csrf_token="csrf")

        async def authenticate(_request):
            return principal, False

        app = web.Application()
        register_atlas_billing_web_routes(
            app,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas-billing",
            authenticate=authenticate,
        )
        payload = {
            "product_kind": "token_pack",
            "product_code": "at-100k",
            "checkout_key": "checkout-test-00000001",
        }
        async with TestClient(TestServer(app)) as client:
            first = await client.post(
                "/api/atlas/billing/checkout",
                headers={"X-CSRF-Token": "csrf"},
                json=payload,
            )
            second = await client.post(
                "/api/atlas/billing/checkout",
                headers={"X-CSRF-Token": "csrf"},
                json=payload,
            )
            first_body = await first.json()
            second_body = await second.json()
            order_id = int(first_body["order"]["id"])
            self.assertEqual(first.status, 201)
            self.assertEqual(second.status, 201)
            self.assertEqual(order_id, int(second_body["order"]["id"]))
            self.assertEqual(
                first_body["payment"]["url"],
                "https://auth.robokassa.ru/Merchant/Payment/Index",
            )
            signature = hashlib.md5(
                f"99.00:{order_id}:password-two:Shp_user=42".encode()
            ).hexdigest().upper()
            result_params = {
                "OutSum": "99.00",
                "InvId": str(order_id),
                "Shp_user": "42",
                "SignatureValue": signature,
            }
            result = await client.post(
                "/api/atlas/billing/robokassa/result",
                data=result_params,
            )
            duplicate = await client.post(
                "/api/atlas/billing/robokassa/result",
                data=result_params,
            )
            summary = await client.get("/api/atlas/billing/summary")

            self.assertEqual(await result.text(), f"OK{order_id}")
            self.assertEqual(await duplicate.text(), f"OK{order_id}")
            self.assertEqual((await summary.json())["payg_balance_tokens"], 100_000)
            with connect() as con:
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM atlas_payment_orders").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM atlas_token_ledger WHERE reference_key = ?",
                        (f"payment:{order_id}",),
                    ).fetchone()[0],
                    1,
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
    def test_dash_is_a_direct_caddy_site_and_examples_keep_secrets_empty(self) -> None:
        root = Path(__file__).resolve().parents[1]
        caddy = (root / "Caddyfile").read_text(encoding="utf-8")
        persistent = (root / ".env.persistent.example").read_text(encoding="utf-8")

        self.assertIn("dash.tvr.lat {", caddy)
        self.assertIn("rewrite * /atlas-billing", caddy)
        self.assertIn("ROBOKASSA_PASSWORD1=\n", persistent)
        self.assertIn("ROBOKASSA_PASSWORD2=\n", persistent)
