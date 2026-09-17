"""Transactional Atlas Token balances, usage metering and payment orders."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from modules.atlas_billing import (
    atlas_billing_enforcement_enabled,
    atlas_plan,
    atlas_tokens_for_cost,
)
from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


def _month_window(now: datetime | None = None) -> tuple[str, str, str]:
    selected = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    started = selected.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if started.month == 12:
        ended = started.replace(year=started.year + 1, month=1)
    else:
        ended = started.replace(month=started.month + 1)
    return started.strftime("%Y-%m"), started.isoformat(), ended.isoformat()


def _ensure_account_in_connection(con: Any, user_id: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    period_key, period_start, period_end = _month_window(now)
    row = con.execute(
        "SELECT * FROM atlas_billing_accounts WHERE user_id = ?",
        (int(user_id),),
    ).fetchone()
    if row is None:
        con.execute(
            """
            INSERT INTO atlas_billing_accounts(
                user_id, plan_code, subscription_status, period_key,
                period_started_at, period_ends_at, created_at, updated_at
            ) VALUES(?, 'free', 'active', ?, ?, ?, ?, ?)
            """,
            (int(user_id), period_key, period_start, period_end, now_iso, now_iso),
        )
        plan_code = "free"
    else:
        plan_code = str(row["plan_code"] or "free")
        expires_at = str(row["subscription_expires_at"] or "")
        if plan_code != "free" and expires_at and expires_at <= now_iso:
            plan_code = "free"
            con.execute(
                """
                UPDATE atlas_billing_accounts
                SET plan_code = 'free', subscription_status = 'cancelled',
                    subscription_expires_at = NULL, updated_at = ?
                WHERE user_id = ?
                """,
                (now_iso, int(user_id)),
            )
        elif plan_code != "free" and expires_at:
            # Paid plans use their own exact 30-day service period. They must
            # not receive a second allowance merely because a calendar month
            # changed in the middle of a subscription.
            return {
                "user_id": int(user_id),
                "plan_code": plan_code,
                "period_key": str(row["period_key"]),
                "period_started_at": str(row["period_started_at"]),
                "period_ends_at": str(row["period_ends_at"]),
            }
        if plan_code == "free" and str(row["period_key"] or "") != period_key:
            con.execute(
                """
                UPDATE atlas_billing_accounts
                SET period_key = ?, period_started_at = ?, period_ends_at = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (period_key, period_start, period_end, now_iso, int(user_id)),
            )
    plan = atlas_plan(plan_code)
    con.execute(
        """
        INSERT OR IGNORE INTO atlas_token_ledger(
            user_id, amount_tokens, entry_kind, balance_bucket,
            reference_key, description, expires_at, created_at
        ) VALUES(?, ?, 'monthly_allowance', 'monthly', ?, ?, ?, ?)
        """,
        (
            int(user_id),
            int(plan.monthly_tokens),
            f"monthly:{int(user_id)}:{period_key}:{plan.code}",
            f"Ежемесячный пакет {plan.name}",
            period_end,
            now_iso,
        ),
    )
    return {
        "user_id": int(user_id),
        "plan_code": plan.code,
        "period_key": period_key,
        "period_started_at": period_start,
        "period_ends_at": period_end,
    }


def atlas_billing_summary(user_id: int) -> dict[str, Any]:
    with _db_lock, connect() as con:
        account = _ensure_account_in_connection(con, int(user_id))
        con.commit()
    with connect_readonly() as con:
        now_iso = utc_now_iso()
        stored = con.execute(
            "SELECT * FROM atlas_billing_accounts WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        balances = con.execute(
            """
            SELECT COALESCE(SUM(amount_tokens), 0) AS value,
                   COALESCE(SUM(CASE WHEN balance_bucket = 'monthly' THEN amount_tokens ELSE 0 END), 0)
                       AS monthly_value,
                   COALESCE(SUM(CASE WHEN balance_bucket = 'payg' THEN amount_tokens ELSE 0 END), 0)
                       AS payg_value
            FROM atlas_token_ledger
            WHERE user_id = ? AND (expires_at IS NULL OR expires_at > ?)
            """,
            (int(user_id), now_iso),
        ).fetchone()
        period_usage = con.execute(
            """
            SELECT COALESCE(SUM(atlas_tokens), 0) AS atlas_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(provider_cost_microusd), 0) AS provider_cost_microusd,
                   COUNT(*) AS requests
            FROM atlas_ai_usage
            WHERE user_id = ? AND created_at >= ? AND created_at < ?
            """,
            (int(user_id), account["period_started_at"], account["period_ends_at"]),
        ).fetchone()
        usage_by_model = con.execute(
            """
            SELECT model, model_provider, COUNT(*) AS requests,
                   COALESCE(SUM(atlas_tokens), 0) AS atlas_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens
            FROM atlas_ai_usage
            WHERE user_id = ? AND created_at >= ? AND created_at < ?
            GROUP BY model, model_provider
            ORDER BY atlas_tokens DESC, requests DESC LIMIT 12
            """,
            (int(user_id), account["period_started_at"], account["period_ends_at"]),
        ).fetchall()
        ledger = con.execute(
            """
            SELECT id, amount_tokens, entry_kind, balance_bucket, description, expires_at, created_at
            FROM atlas_token_ledger WHERE user_id = ?
            ORDER BY id DESC LIMIT 30
            """,
            (int(user_id),),
        ).fetchall()
        orders = con.execute(
            """
            SELECT id, product_kind, product_code, amount_kopecks, atlas_tokens,
                   plan_code, status, created_at, paid_at
            FROM atlas_payment_orders WHERE user_id = ?
            ORDER BY id DESC LIMIT 20
            """,
            (int(user_id),),
        ).fetchall()
    return {
        "account": dict(stored) if stored is not None else account,
        "plan": atlas_plan(str(account["plan_code"])).public(),
        "balance_tokens": int(balances["value"] if balances else 0),
        "monthly_balance_tokens": int(balances["monthly_value"] if balances else 0),
        "payg_balance_tokens": int(balances["payg_value"] if balances else 0),
        "period_usage": {
            key: int(value or 0)
            for key, value in (dict(period_usage) if period_usage is not None else {}).items()
        },
        "usage_by_model": [
            {
                **dict(row),
                "requests": int(row["requests"] or 0),
                "atlas_tokens": int(row["atlas_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
            }
            for row in usage_by_model
        ],
        "ledger": [dict(row) for row in ledger],
        "orders": [dict(row) for row in orders],
        "enforcement_enabled": atlas_billing_enforcement_enabled(),
    }


def atlas_ai_entitlement(user_id: int) -> dict[str, Any]:
    """Cheap preflight used only by AI endpoints; manual Atlas stays free."""

    with _db_lock, connect() as con:
        account = _ensure_account_in_connection(con, int(user_id))
        now = utc_now_iso()
        balance = con.execute(
            """
            SELECT COALESCE(SUM(amount_tokens), 0) AS value
            FROM atlas_token_ledger
            WHERE user_id = ? AND (expires_at IS NULL OR expires_at > ?)
            """,
            (int(user_id), now),
        ).fetchone()
        con.commit()
    available = int(balance["value"] if balance else 0)
    enforced = atlas_billing_enforcement_enabled()
    return {
        "allowed": bool(not enforced or available > 0),
        "enforcement_enabled": enforced,
        "balance_tokens": available,
        "plan": atlas_plan(str(account["plan_code"])).public(),
    }


def atlas_billing_admin_metrics(*, days: int = 30) -> dict[str, Any]:
    """Return aggregate economics without exposing prompts or payment secrets."""

    selected_days = max(1, min(366, int(days or 30)))
    since = (datetime.now(timezone.utc) - timedelta(days=selected_days)).isoformat()
    with connect_readonly() as con:
        usage = con.execute(
            """
            SELECT COUNT(*) AS requests,
                   COUNT(DISTINCT user_id) AS active_users,
                   COALESCE(SUM(atlas_tokens), 0) AS atlas_tokens,
                   COALESCE(SUM(total_tokens), 0) AS model_tokens,
                   COALESCE(SUM(provider_cost_microusd), 0) AS provider_cost_microusd
            FROM atlas_ai_usage WHERE created_at >= ?
            """,
            (since,),
        ).fetchone()
        revenue = con.execute(
            """
            SELECT COUNT(*) AS paid_orders,
                   COUNT(DISTINCT user_id) AS paying_users,
                   COALESCE(SUM(amount_kopecks), 0) AS revenue_kopecks
            FROM atlas_payment_orders
            WHERE status = 'paid' AND paid_at >= ?
            """,
            (since,),
        ).fetchone()
        accounts = con.execute(
            """
            SELECT plan_code, COUNT(*) AS accounts
            FROM atlas_billing_accounts GROUP BY plan_code ORDER BY accounts DESC
            """
        ).fetchall()
        models = con.execute(
            """
            SELECT model, model_provider, COUNT(*) AS requests,
                   COALESCE(SUM(atlas_tokens), 0) AS atlas_tokens,
                   COALESCE(SUM(total_tokens), 0) AS model_tokens,
                   COALESCE(SUM(provider_cost_microusd), 0) AS provider_cost_microusd
            FROM atlas_ai_usage WHERE created_at >= ?
            GROUP BY model, model_provider
            ORDER BY provider_cost_microusd DESC, requests DESC LIMIT 30
            """,
            (since,),
        ).fetchall()
    usage_data = dict(usage) if usage is not None else {}
    revenue_data = dict(revenue) if revenue is not None else {}
    cost_microusd = int(usage_data.get("provider_cost_microusd") or 0)
    revenue_kopecks = int(revenue_data.get("revenue_kopecks") or 0)
    return {
        "window_days": selected_days,
        "since": since,
        "requests": int(usage_data.get("requests") or 0),
        "active_users": int(usage_data.get("active_users") or 0),
        "atlas_tokens": int(usage_data.get("atlas_tokens") or 0),
        "model_tokens": int(usage_data.get("model_tokens") or 0),
        "provider_cost_microusd": cost_microusd,
        "provider_cost_usd": f"{cost_microusd / 1_000_000:.6f}",
        "paid_orders": int(revenue_data.get("paid_orders") or 0),
        "paying_users": int(revenue_data.get("paying_users") or 0),
        "revenue_kopecks": revenue_kopecks,
        "revenue_rub": f"{revenue_kopecks / 100:.2f}",
        "accounts_by_plan": [
            {**dict(row), "accounts": int(row["accounts"] or 0)}
            for row in accounts
        ],
        "models": [
            {
                **dict(row),
                "requests": int(row["requests"] or 0),
                "atlas_tokens": int(row["atlas_tokens"] or 0),
                "model_tokens": int(row["model_tokens"] or 0),
                "provider_cost_microusd": int(row["provider_cost_microusd"] or 0),
            }
            for row in models
        ],
    }


def atlas_admin_grant_tokens(
    user_id: int,
    *,
    actor_user_id: int,
    amount_tokens: int,
    reason: str,
    request_key: str,
) -> dict[str, Any]:
    """Grant a durable, idempotent pay-as-you-go reserve from the Reactor."""

    target_id = int(user_id)
    actor_id = int(actor_user_id)
    amount = int(amount_tokens)
    clean_reason = " ".join(str(reason or "").split())[:240]
    clean_key = str(request_key or "").strip()[:120]
    if target_id <= 0 or actor_id <= 0:
        raise ValueError("atlas_billing_admin_identity_invalid")
    if amount < 1 or amount > 100_000_000:
        raise ValueError("atlas_billing_admin_amount_invalid")
    if len(clean_reason) < 5:
        raise ValueError("atlas_billing_admin_reason_required")
    if len(clean_key) < 8:
        raise ValueError("atlas_billing_admin_request_key_required")
    reference_key = f"admin-grant:{clean_key}"
    now = utc_now_iso()
    description = f"Начисление администратором {actor_id}: {clean_reason}"
    with _db_lock, connect() as con:
        _ensure_account_in_connection(con, target_id)
        existing = con.execute(
            "SELECT * FROM atlas_token_ledger WHERE reference_key = ?",
            (reference_key,),
        ).fetchone()
        if existing is not None:
            if (
                int(existing["user_id"]) != target_id
                or int(existing["amount_tokens"]) != amount
                or str(existing["entry_kind"]) != "admin_grant"
            ):
                raise ValueError("atlas_billing_admin_request_key_conflict")
            con.commit()
            entry = dict(existing)
            created = False
        else:
            cursor = con.execute(
                """
                INSERT INTO atlas_token_ledger(
                    user_id, amount_tokens, entry_kind, balance_bucket,
                    reference_key, description, expires_at, created_at
                ) VALUES(?, ?, 'admin_grant', 'payg', ?, ?, NULL, ?)
                """,
                (target_id, amount, reference_key, description, now),
            )
            entry = dict(
                con.execute(
                    "SELECT * FROM atlas_token_ledger WHERE id = ?",
                    (int(cursor.lastrowid),),
                ).fetchone()
            )
            con.commit()
            created = True
    return {
        "created": created,
        "entry": entry,
        "summary": atlas_billing_summary(target_id),
    }


def atlas_record_ai_usage(
    user_id: int,
    organization_id: int,
    *,
    request_key: str,
    source: str,
    model: str,
    model_provider: str,
    usage: dict[str, Any] | None,
    message_id: int | None = None,
) -> dict[str, Any]:
    measured = dict(usage or {})
    cost_microusd = max(0, int(measured.get("provider_cost_microusd") or 0))
    atlas_tokens = atlas_tokens_for_cost(Decimal(cost_microusd) / Decimal(1_000_000))
    now = utc_now_iso()
    clean_key = str(request_key or "").strip()[:180]
    if not clean_key:
        raise ValueError("atlas_billing_request_key_required")
    with _db_lock, connect() as con:
        account = _ensure_account_in_connection(con, int(user_id))
        con.execute(
            """
            INSERT OR IGNORE INTO atlas_ai_usage(
                user_id, organization_id, message_id, request_key, source,
                model, model_provider, prompt_tokens, completion_tokens,
                total_tokens, model_calls, provider_cost_microusd,
                atlas_tokens, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id), int(organization_id), int(message_id) if message_id else None,
                clean_key, str(source or "unknown")[:40], str(model or "")[:160],
                str(model_provider or "")[:80], max(0, int(measured.get("prompt_tokens") or 0)),
                max(0, int(measured.get("completion_tokens") or 0)),
                max(0, int(measured.get("total_tokens") or 0)),
                max(0, int(measured.get("model_calls") or 0)), cost_microusd,
                atlas_tokens, now,
            ),
        )
        stored = con.execute(
            "SELECT * FROM atlas_ai_usage WHERE request_key = ?",
            (clean_key,),
        ).fetchone()
        if stored is not None and int(stored["user_id"]) != int(user_id):
            raise ValueError("atlas_billing_request_owner_mismatch")
        already_metered = con.execute(
            "SELECT 1 FROM atlas_token_ledger WHERE reference_key IN (?, ?) LIMIT 1",
            (f"usage:{clean_key}:monthly", f"usage:{clean_key}:payg"),
        ).fetchone()
        if stored is not None and int(stored["atlas_tokens"]) > 0 and already_metered is None:
            monthly_row = con.execute(
                """
                SELECT COALESCE(SUM(amount_tokens), 0) AS value
                FROM atlas_token_ledger
                WHERE user_id = ? AND balance_bucket = 'monthly'
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (int(user_id), now),
            ).fetchone()
            monthly_available = max(0, int(monthly_row["value"] if monthly_row else 0))
            monthly_debit = min(int(stored["atlas_tokens"]), monthly_available)
            payg_debit = int(stored["atlas_tokens"]) - monthly_debit
            if monthly_debit:
                con.execute(
                    """
                    INSERT OR IGNORE INTO atlas_token_ledger(
                        user_id, amount_tokens, entry_kind, balance_bucket,
                        reference_key, description, expires_at, created_at
                    ) VALUES(?, ?, 'ai_usage', 'monthly', ?, ?, ?, ?)
                    """,
                    (
                        int(user_id), -monthly_debit, f"usage:{clean_key}:monthly",
                        f"{str(model or 'Atlas')} · {str(source or 'request')}",
                        str(account["period_ends_at"]), now,
                    ),
                )
            if payg_debit:
                con.execute(
                    """
                    INSERT OR IGNORE INTO atlas_token_ledger(
                        user_id, amount_tokens, entry_kind, balance_bucket,
                        reference_key, description, created_at
                    ) VALUES(?, ?, 'ai_usage', 'payg', ?, ?, ?)
                    """,
                    (
                        int(user_id), -payg_debit, f"usage:{clean_key}:payg",
                        f"{str(model or 'Atlas')} · {str(source or 'request')}", now,
                    ),
                )
        con.commit()
    return dict(stored) if stored is not None else {}


def atlas_create_payment_order(
    user_id: int,
    *,
    product_kind: str,
    product_code: str,
    amount_kopecks: int,
    atlas_tokens: int,
    plan_code: str | None = None,
    checkout_key: str = "",
) -> dict[str, Any]:
    if product_kind not in {"subscription", "token_pack"}:
        raise ValueError("atlas_billing_product_invalid")
    now = utc_now_iso()
    clean_checkout_key = str(checkout_key or "").strip()[:100] or None
    with _db_lock, connect() as con:
        _ensure_account_in_connection(con, int(user_id))
        if clean_checkout_key:
            existing = con.execute(
                "SELECT * FROM atlas_payment_orders WHERE checkout_key = ?",
                (clean_checkout_key,),
            ).fetchone()
            if existing is not None:
                if (
                    int(existing["user_id"]) != int(user_id)
                    or str(existing["product_kind"]) != product_kind
                    or str(existing["product_code"]) != str(product_code)[:80]
                ):
                    raise ValueError("atlas_billing_checkout_key_conflict")
                con.commit()
                return dict(existing)
        # Reuse a recent unpaid invoice for the same product. This bounds
        # accidental double-click growth without preventing a user from
        # renewing or changing an active subscription.
        pending_since = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        pending = con.execute(
            """
            SELECT * FROM atlas_payment_orders
            WHERE user_id = ? AND product_kind = ? AND product_code = ?
              AND status = 'pending' AND created_at >= ?
            ORDER BY id DESC LIMIT 1
            """,
            (int(user_id), product_kind, str(product_code)[:80], pending_since),
        ).fetchone()
        if pending is not None:
            con.commit()
            return dict(pending)
        cursor = con.execute(
            """
            INSERT INTO atlas_payment_orders(
                user_id, checkout_key, product_kind, product_code, amount_kopecks,
                atlas_tokens, plan_code, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                int(user_id), clean_checkout_key, product_kind,
                str(product_code)[:80], int(amount_kopecks),
                int(atlas_tokens), str(plan_code or "")[:40] or None, now, now,
            ),
        )
        order_id = int(cursor.lastrowid)
        con.commit()
    return atlas_payment_order(order_id) or {}


def atlas_payment_order(order_id: int) -> dict[str, Any] | None:
    with connect_readonly() as con:
        row = con.execute(
            "SELECT * FROM atlas_payment_orders WHERE id = ?",
            (int(order_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def atlas_settle_payment_order(
    order_id: int,
    *,
    provider_operation_id: str = "",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM atlas_payment_orders WHERE id = ?",
            (int(order_id),),
        ).fetchone()
        if row is None:
            raise ValueError("atlas_billing_order_not_found")
        status = str(row["status"])
        if status not in {"pending", "paid"}:
            raise ValueError("atlas_billing_order_not_payable")
        if status == "pending":
            con.execute(
                """
                UPDATE atlas_payment_orders
                SET status = 'paid', provider_operation_id = ?, paid_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (str(provider_operation_id)[:160] or None, now_iso, now_iso, int(order_id)),
            )
            if str(row["product_kind"]) == "subscription":
                plan = atlas_plan(str(row["plan_code"] or row["product_code"]))
                current_account = con.execute(
                    "SELECT plan_code, subscription_expires_at FROM atlas_billing_accounts WHERE user_id = ?",
                    (int(row["user_id"]),),
                ).fetchone()
                current_plan = str(current_account["plan_code"] or "free") if current_account else "free"
                current_expiry_text = str(current_account["subscription_expires_at"] or "") if current_account else ""
                expiry_base = now
                if current_plan == plan.code and current_expiry_text:
                    try:
                        current_expiry = datetime.fromisoformat(current_expiry_text)
                        if current_expiry.tzinfo is None:
                            current_expiry = current_expiry.replace(tzinfo=timezone.utc)
                        if current_expiry > now:
                            expiry_base = current_expiry
                    except ValueError:
                        pass
                # The free monthly allowance is replaced by the first paid
                # subscription. Previously purchased subscription grants use
                # entry_kind='payment' and deliberately keep their own expiry
                # when a user renews or upgrades.
                con.execute(
                    """
                    UPDATE atlas_token_ledger SET expires_at = ?
                    WHERE user_id = ? AND balance_bucket = 'monthly'
                      AND entry_kind = 'monthly_allowance'
                      AND (expires_at IS NULL OR expires_at > ?)
                    """,
                    (now_iso, int(row["user_id"]), now_iso),
                )
                expires = (expiry_base + timedelta(days=30)).isoformat()
                period_key = f"subscription:{int(order_id)}"
                period_start = now_iso
                period_end = expires
                con.execute(
                    """
                    UPDATE atlas_billing_accounts
                    SET plan_code = ?, subscription_status = 'active', period_key = ?,
                        period_started_at = ?, period_ends_at = ?, subscription_expires_at = ?,
                        updated_at = ? WHERE user_id = ?
                    """,
                    (
                        plan.code, period_key, period_start, period_end, expires,
                        now_iso, int(row["user_id"]),
                    ),
                )
                grant = int(plan.monthly_tokens)
                balance_bucket = "monthly"
                grant_expires_at = expires
            else:
                grant = int(row["atlas_tokens"])
                balance_bucket = "payg"
                grant_expires_at = None
            con.execute(
                """
                INSERT OR IGNORE INTO atlas_token_ledger(
                    user_id, amount_tokens, entry_kind, balance_bucket,
                    reference_key, description, expires_at, created_at
                ) VALUES(?, ?, 'payment', ?, ?, ?, ?, ?)
                """,
                (
                    int(row["user_id"]), grant, balance_bucket, f"payment:{int(order_id)}",
                    f"Оплата заказа №{int(order_id)}", grant_expires_at, now_iso,
                ),
            )
        con.commit()
    return atlas_payment_order(int(order_id)) or {}


__all__ = [
    "atlas_ai_entitlement",
    "atlas_admin_grant_tokens",
    "atlas_billing_admin_metrics",
    "atlas_billing_summary",
    "atlas_record_ai_usage",
    "atlas_create_payment_order",
    "atlas_payment_order",
    "atlas_settle_payment_order",
]
