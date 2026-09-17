"""Commercial configuration and payment primitives for Atlas.

The module deliberately contains no database or HTTP side effects.  Prices are
real catalogue values, while checkout remains disabled until the merchant has
provided both Robokassa passwords and explicitly enabled payments.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Mapping
from urllib.parse import quote


ATLAS_TOKEN_COST_USD = Decimal("0.00001")


@dataclass(frozen=True, slots=True)
class AtlasPlan:
    code: str
    name: str
    monthly_price_rub: int
    monthly_tokens: int
    model_lane: str
    description: str

    def public(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "monthly_price_rub": self.monthly_price_rub,
            "monthly_tokens": self.monthly_tokens,
            "model_lane": self.model_lane,
            "description": self.description,
            "manual_features": "unlimited",
        }


PLANS = (
    AtlasPlan("free", "Atlas Free", 0, 50_000, "economy", "Все ручные функции и стартовый запас ИИ."),
    AtlasPlan("start", "Atlas Start", 990, 1_000_000, "balanced", "Для регулярной личной работы с Atlas."),
    AtlasPlan("pro", "Atlas Pro", 4_990, 5_000_000, "premium", "Для документов, дел и интенсивных исследований."),
    AtlasPlan("sovereign", "Atlas Sovereign", 29_990, 30_000_000, "frontier", "Максимальный вычислительный резерв и самые дорогие модели."),
)

TOKEN_PACKS = (
    {"code": "at-100k", "name": "100 000 Atlas Token", "atlas_tokens": 100_000, "price_rub": 99},
    {"code": "at-1m", "name": "1 000 000 Atlas Token", "atlas_tokens": 1_000_000, "price_rub": 990},
    {"code": "at-5m", "name": "5 000 000 Atlas Token", "atlas_tokens": 5_000_000, "price_rub": 4_990},
)


def atlas_billing_catalog() -> dict[str, Any]:
    return {
        "currency": "RUB",
        "token_unit": {
            "name": "Atlas Token",
            "symbol": "AT",
            "provider_cost_usd": str(ATLAS_TOKEN_COST_USD),
            "explanation": "AT списываются по фактической стоимости вычислений, которую вернул ИИ-провайдер.",
        },
        "plans": [plan.public() for plan in PLANS],
        "token_packs": [dict(item) for item in TOKEN_PACKS],
        "service": {
            "manual_features_price_rub": 0,
            "subscription_duration_days": 30,
            "subscription_auto_renewal": False,
            "monthly_tokens_expire": True,
            "purchased_tokens_expire": False,
            "paid_subject": "Информационно-вычислительные услуги, объём которых учитывается в Atlas Token",
        },
        "seller": {
            "name": "ИП Саниев Муртазали Бухариевич",
            "inn": "370266611106",
            "ogrnip": "322370000004857",
            "email": "tvr@ultra---industries.com",
        },
    }


def atlas_plan(code: str) -> AtlasPlan:
    selected = str(code or "").strip().lower()
    for plan in PLANS:
        if plan.code == selected:
            return plan
    raise ValueError("atlas_billing_plan_invalid")


def atlas_token_pack(code: str) -> dict[str, Any]:
    selected = str(code or "").strip().lower()
    for item in TOKEN_PACKS:
        if item["code"] == selected:
            return dict(item)
    raise ValueError("atlas_billing_pack_invalid")


def atlas_tokens_for_cost(cost_usd: object) -> int:
    try:
        cost = Decimal(str(cost_usd or "0"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("atlas_billing_cost_invalid") from exc
    if not cost.is_finite() or cost < 0:
        raise ValueError("atlas_billing_cost_invalid")
    return int((cost / ATLAS_TOKEN_COST_USD).to_integral_value(rounding=ROUND_CEILING))


def _env_bool(name: str, *, default: bool = False) -> bool:
    """Parse human-friendly boolean environment values consistently."""

    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return bool(default)
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name.lower()}_invalid")


def robokassa_config() -> dict[str, Any]:
    test_mode = _env_bool("ROBOKASSA_TEST_MODE", default=True)
    hash_algorithm = os.getenv("ROBOKASSA_HASH_ALGORITHM", "md5").strip().lower()
    if hash_algorithm not in {"md5", "sha256", "sha512"}:
        raise RuntimeError("robokassa_hash_algorithm_invalid")
    receipt_tax = os.getenv("ROBOKASSA_RECEIPT_TAX", "none").strip().lower()
    if receipt_tax not in {"none", "vat0", "vat5", "vat7", "vat10", "vat20", "vat105", "vat107", "vat110", "vat120"}:
        raise RuntimeError("robokassa_receipt_tax_invalid")
    receipt_sno = os.getenv("ROBOKASSA_RECEIPT_SNO", "").strip().lower()
    if receipt_sno and receipt_sno not in {
        "osn", "usn_income", "usn_income_outcome", "esn", "patent",
    }:
        raise RuntimeError("robokassa_receipt_sno_invalid")
    return {
        "merchant_login": os.getenv("ROBOKASSA_MERCHANT_LOGIN", "tvr.lat").strip(),
        "password1": os.getenv("ROBOKASSA_PASSWORD1", "").strip(),
        "password2": os.getenv("ROBOKASSA_PASSWORD2", "").strip(),
        "test_mode": test_mode,
        "enabled": _env_bool("ATLAS_BILLING_PAYMENTS_ENABLED", default=False),
        # Robokassa's current public payment form.  The similarly named
        # /Merchant/Payment/Index endpoint returns a branded 404 in browsers.
        "payment_url": "https://auth.robokassa.ru/Merchant/Index.aspx",
        "hash_algorithm": hash_algorithm,
        "receipt_tax": receipt_tax,
        "receipt_sno": receipt_sno,
        "personal_data_localization_ready": _env_bool(
            "ATLAS_PD_LOCALIZATION_READY", default=False
        ),
        "personal_data_primary_region": os.getenv(
            "ATLAS_PD_PRIMARY_REGION", ""
        ).strip().upper(),
    }


def atlas_billing_enforcement_enabled() -> bool:
    return _env_bool("ATLAS_BILLING_ENFORCEMENT_ENABLED", default=True)


def _signature(parts: list[object], *, algorithm: str | None = None) -> str:
    value = ":".join(str(item) for item in parts)
    selected = str(algorithm or robokassa_config()["hash_algorithm"])
    return hashlib.new(selected, value.encode("utf-8"), usedforsecurity=False).hexdigest().upper()


def _robokassa_receipt(*, amount: str, description: str) -> str:
    config = robokassa_config()
    receipt: dict[str, Any] = {
        "items": [
            {
                "name": str(description).strip()[:128] or "Цифровая услуга Atlas",
                "quantity": 1,
                "sum": float(amount),
                "payment_method": "full_payment",
                "payment_object": "service",
                "tax": str(config["receipt_tax"]),
            }
        ]
    }
    if config["receipt_sno"]:
        receipt["sno"] = str(config["receipt_sno"])
    compact = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    # Robokassa requires the URL-encoded Receipt value both in the request and
    # in the signature base.  The surrounding POST form encodes '%' once more
    # on the wire and Robokassa receives the intended encoded JSON value.
    return quote(compact, safe="")


def robokassa_payment_fields(
    *,
    invoice_id: int,
    amount_kopecks: int,
    description: str,
    user_id: int,
    receipt_email: str = "",
) -> dict[str, str]:
    config = robokassa_config()
    if not config["enabled"] or not config["password1"] or not config["password2"]:
        raise RuntimeError("atlas_billing_payments_not_configured")
    amount = f"{max(0, int(amount_kopecks)) / 100:.2f}"
    if amount == "0.00":
        raise ValueError("atlas_billing_amount_invalid")
    custom = {"Shp_user": str(int(user_id))}
    receipt = _robokassa_receipt(amount=amount, description=description)
    signature = _signature([
        config["merchant_login"], amount, int(invoice_id), receipt, config["password1"],
        f"Shp_user={custom['Shp_user']}",
    ], algorithm=str(config["hash_algorithm"]))
    fields = {
        "MerchantLogin": str(config["merchant_login"]),
        "OutSum": amount,
        "InvId": str(int(invoice_id)),
        "Description": str(description)[:100],
        "SignatureValue": signature,
        "Receipt": receipt,
        "IsTest": "1" if config["test_mode"] else "0",
        "Culture": "ru",
        "Encoding": "utf-8",
        **custom,
    }
    clean_email = str(receipt_email or "").strip().lower()
    if clean_email:
        fields["Email"] = clean_email[:254]
    return fields


def robokassa_result_is_valid(values: Mapping[str, object]) -> bool:
    config = robokassa_config()
    supplied = str(values.get("SignatureValue") or "").strip().upper()
    amount = str(values.get("OutSum") or "").strip()
    invoice = str(values.get("InvId") or values.get("InvoiceID") or "").strip()
    user_id = str(values.get("Shp_user") or "").strip()
    if not supplied or not amount or not invoice or not user_id or not config["password2"]:
        return False
    expected = _signature(
        [amount, invoice, config["password2"], f"Shp_user={user_id}"],
        algorithm=str(config["hash_algorithm"]),
    )
    return hmac.compare_digest(supplied, expected)


__all__ = [
    "ATLAS_TOKEN_COST_USD",
    "atlas_billing_catalog",
    "atlas_billing_enforcement_enabled",
    "atlas_plan",
    "atlas_token_pack",
    "atlas_tokens_for_cost",
    "robokassa_config",
    "robokassa_payment_fields",
    "robokassa_result_is_valid",
]
