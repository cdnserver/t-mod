"""Atlas Token metering and non-commercial access catalogue.

Atlas keeps measuring AI usage in AT, but no money checkout is exposed.  Paid
plan definitions stay in the data model only so historical accounts and ledger
entries remain readable after the payment subsystem was retired.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any


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
    {"code": "at-100k", "name": "Пакет ИИ-обработки · 100 000 AT", "atlas_tokens": 100_000, "price_rub": 99},
    {"code": "at-1m", "name": "Пакет ИИ-обработки · 1 000 000 AT", "atlas_tokens": 1_000_000, "price_rub": 990},
    {"code": "at-5m", "name": "Пакет ИИ-обработки · 5 000 000 AT", "atlas_tokens": 5_000_000, "price_rub": 4_990},
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
            "paid_subject": (
                "Дистанционная обработка запросов с применением искусственного интеллекта; "
                "объём услуги учитывается в Atlas Token"
            ),
            "required_software": "T-Mod Desktop или его официальная последующая версия",
            "software_price_rub": 0,
            "software_url": "https://github.com/cdnserver/t-mod-releases/releases/latest",
            "requirements": [
                "Учётная запись T-Mod",
                "подключение к сети Интернет",
                "поддерживаемая операционная система и актуальная версия приложения",
            ],
        },
        "availability": {
            "commercial_sales": False,
            "message": (
                "Платные подключения и пополнение Atlas Token временно "
                "недоступны. Бесплатный доступ и ранее начисленный резерв "
                "продолжают работать."
            ),
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


def atlas_billing_enforcement_enabled() -> bool:
    return _env_bool("ATLAS_BILLING_ENFORCEMENT_ENABLED", default=True)


__all__ = [
    "ATLAS_TOKEN_COST_USD",
    "atlas_billing_catalog",
    "atlas_billing_enforcement_enabled",
    "atlas_plan",
    "atlas_token_pack",
    "atlas_tokens_for_cost",
]
