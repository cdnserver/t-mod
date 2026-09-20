"""Atlas finance, controlled content and immutable legal revision storage."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from modules.atlas_billing import atlas_plan
from persistence.atlas_billing_repository import (
    _ensure_account_in_connection,
    atlas_billing_summary,
)
from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


CONTENT_LIMITS = {
    "store_notice": 600,
    "legal_center_intro": 1200,
    "footer_note": 400,
    "support_email": 254,
}
LEGAL_DOCUMENTS = {"offer", "privacy", "terms", "refunds", "contacts"}
LEGAL_BLOCK_TYPES = {"section", "paragraph", "list", "notice"}


def _clean_request_key(value: str) -> str:
    result = str(value or "").strip()[:140]
    if len(result) < 8:
        raise ValueError("atlas_finance_request_key_required")
    return result


def finance_dashboard(*, days: int = 30, usd_rub_rate: Decimal = Decimal("95")) -> dict[str, Any]:
    selected_days = max(1, min(366, int(days or 30)))
    rate = Decimal(str(usd_rub_rate or "0"))
    if not rate.is_finite() or rate <= 0:
        raise ValueError("atlas_finance_usd_rate_invalid")
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=selected_days)).isoformat()
    with connect_readonly() as con:
        order_totals = con.execute(
            """
            SELECT COUNT(*) AS orders,
                   COALESCE(SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END), 0) AS paid_orders,
                   COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_orders,
                   COALESCE(SUM(CASE WHEN status = 'refunded' THEN 1 ELSE 0 END), 0) AS refunded_orders,
                   COALESCE(SUM(CASE WHEN status = 'paid' THEN amount_kopecks ELSE 0 END), 0) AS revenue_kopecks,
                   COUNT(DISTINCT CASE WHEN status = 'paid' THEN user_id END) AS paying_users
            FROM atlas_payment_orders WHERE created_at >= ?
            """,
            (since,),
        ).fetchone()
        usage = con.execute(
            """
            SELECT COUNT(*) AS requests, COUNT(DISTINCT user_id) AS active_users,
                   COALESCE(SUM(provider_cost_microusd), 0) AS provider_cost_microusd,
                   COALESCE(SUM(atlas_tokens), 0) AS atlas_tokens,
                   COALESCE(SUM(total_tokens), 0) AS model_tokens
            FROM atlas_ai_usage WHERE created_at >= ?
            """,
            (since,),
        ).fetchone()
        manual_costs = con.execute(
            "SELECT COALESCE(SUM(amount_kopecks), 0) AS value FROM atlas_finance_costs WHERE occurred_at >= ?",
            (since,),
        ).fetchone()
        daily = con.execute(
            """
            SELECT substr(paid_at, 1, 10) AS day, COUNT(*) AS orders,
                   COALESCE(SUM(amount_kopecks), 0) AS revenue_kopecks
            FROM atlas_payment_orders
            WHERE status = 'paid' AND paid_at >= ?
            GROUP BY substr(paid_at, 1, 10) ORDER BY day
            """,
            (since,),
        ).fetchall()
        models = con.execute(
            """
            SELECT model, model_provider, COUNT(*) AS requests,
                   COALESCE(SUM(provider_cost_microusd), 0) AS provider_cost_microusd,
                   COALESCE(SUM(total_tokens), 0) AS model_tokens
            FROM atlas_ai_usage WHERE created_at >= ?
            GROUP BY model, model_provider ORDER BY provider_cost_microusd DESC LIMIT 20
            """,
            (since,),
        ).fetchall()
        recent_orders = con.execute(
            "SELECT * FROM atlas_payment_orders ORDER BY id DESC LIMIT 20"
        ).fetchall()
        costs = con.execute(
            "SELECT * FROM atlas_finance_costs ORDER BY occurred_at DESC, id DESC LIMIT 20"
        ).fetchall()
        plans = con.execute(
            "SELECT plan_code, COUNT(*) AS accounts FROM atlas_billing_accounts GROUP BY plan_code ORDER BY accounts DESC"
        ).fetchall()
    order_data = dict(order_totals or {})
    usage_data = dict(usage or {})
    provider_usd = Decimal(int(usage_data.get("provider_cost_microusd") or 0)) / Decimal(1_000_000)
    provider_cost_kopecks = int((provider_usd * rate * 100).quantize(Decimal("1")))
    manual_cost_kopecks = int((manual_costs or {"value": 0})["value"] or 0)
    revenue_kopecks = int(order_data.get("revenue_kopecks") or 0)
    gross_profit = revenue_kopecks - provider_cost_kopecks - manual_cost_kopecks
    margin = (Decimal(gross_profit) / Decimal(revenue_kopecks) * 100) if revenue_kopecks else Decimal(0)
    return {
        "window_days": selected_days,
        "since": since,
        "usd_rub_rate": format(rate, "f"),
        "orders": int(order_data.get("orders") or 0),
        "paid_orders": int(order_data.get("paid_orders") or 0),
        "pending_orders": int(order_data.get("pending_orders") or 0),
        "refunded_orders": int(order_data.get("refunded_orders") or 0),
        "paying_users": int(order_data.get("paying_users") or 0),
        "revenue_kopecks": revenue_kopecks,
        "provider_cost_microusd": int(usage_data.get("provider_cost_microusd") or 0),
        "provider_cost_kopecks": provider_cost_kopecks,
        "manual_cost_kopecks": manual_cost_kopecks,
        "profit_kopecks": gross_profit,
        "margin_percent": format(margin.quantize(Decimal("0.01")), "f"),
        "requests": int(usage_data.get("requests") or 0),
        "active_users": int(usage_data.get("active_users") or 0),
        "atlas_tokens": int(usage_data.get("atlas_tokens") or 0),
        "model_tokens": int(usage_data.get("model_tokens") or 0),
        "daily": [dict(row) for row in daily],
        "models": [dict(row) for row in models],
        "recent_orders": [dict(row) for row in recent_orders],
        "recent_costs": [dict(row) for row in costs],
        "accounts_by_plan": [dict(row) for row in plans],
    }


def list_orders(*, status: str = "", query: str = "", limit: int = 100) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    clean_status = str(status or "").strip().lower()
    if clean_status:
        if clean_status not in {"pending", "paid", "failed", "cancelled", "refunded"}:
            raise ValueError("atlas_finance_order_status_invalid")
        clauses.append("status = ?")
        params.append(clean_status)
    clean_query = str(query or "").strip()
    if clean_query:
        if clean_query.isdigit():
            clauses.append("(id = ? OR user_id = ?)")
            params.extend((int(clean_query), int(clean_query)))
        else:
            clauses.append("(product_code LIKE ? OR provider_operation_id LIKE ?)")
            params.extend((f"%{clean_query[:80]}%", f"%{clean_query[:120]}%"))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(500, int(limit or 100))))
    with connect_readonly() as con:
        rows = con.execute(
            f"SELECT * FROM atlas_payment_orders {where} ORDER BY id DESC LIMIT ?", params
        ).fetchall()
    return [dict(row) for row in rows]


def add_cost(*, category: str, title: str, amount_kopecks: int, occurred_at: str,
             note: str, actor_user_id: int, request_key: str) -> dict[str, Any]:
    clean_category = str(category or "other").strip().lower()[:40]
    clean_title = " ".join(str(title or "").split())[:160]
    clean_note = str(note or "").strip()[:1000]
    key = _clean_request_key(request_key)
    if len(clean_title) < 3 or int(amount_kopecks) <= 0:
        raise ValueError("atlas_finance_cost_invalid")
    try:
        datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("atlas_finance_cost_date_invalid") from exc
    with _db_lock, connect() as con:
        existing = con.execute("SELECT * FROM atlas_finance_costs WHERE request_key = ?", (key,)).fetchone()
        if existing is None:
            cursor = con.execute(
                """INSERT INTO atlas_finance_costs(category, title, amount_kopecks, occurred_at,
                   note, actor_user_id, request_key, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (clean_category, clean_title, int(amount_kopecks), str(occurred_at), clean_note,
                 int(actor_user_id), key, utc_now_iso()),
            )
            existing = con.execute("SELECT * FROM atlas_finance_costs WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
        con.commit()
    return dict(existing)


def grant_subscription(user_id: int, *, plan_code: str, days: int, actor_user_id: int,
                       reason: str, request_key: str) -> dict[str, Any]:
    plan = atlas_plan(plan_code)
    if plan.code == "free":
        raise ValueError("atlas_finance_paid_plan_required")
    duration = max(1, min(366, int(days or 30)))
    clean_reason = " ".join(str(reason or "").split())[:240]
    key = _clean_request_key(request_key)
    if len(clean_reason) < 5:
        raise ValueError("atlas_finance_reason_required")
    reference = f"admin-subscription:{key}"
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    expires = (now + timedelta(days=duration)).isoformat()
    with _db_lock, connect() as con:
        _ensure_account_in_connection(con, int(user_id))
        existing = con.execute("SELECT * FROM atlas_token_ledger WHERE reference_key = ?", (reference,)).fetchone()
        if existing is None:
            con.execute(
                """UPDATE atlas_billing_accounts SET plan_code = ?, subscription_status = 'active',
                   period_key = ?, period_started_at = ?, period_ends_at = ?, subscription_expires_at = ?,
                   updated_at = ? WHERE user_id = ?""",
                (plan.code, f"admin:{key}", now_iso, expires, expires, now_iso, int(user_id)),
            )
            cursor = con.execute(
                """INSERT INTO atlas_token_ledger(user_id, amount_tokens, entry_kind, balance_bucket,
                   reference_key, description, expires_at, created_at)
                   VALUES(?, ?, 'admin_subscription', 'monthly', ?, ?, ?, ?)""",
                (int(user_id), int(plan.monthly_tokens), reference,
                 f"Подписка {plan.name} на {duration} дн. · {clean_reason}", expires, now_iso),
            )
            existing = con.execute("SELECT * FROM atlas_token_ledger WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
            created = True
        else:
            created = False
        con.commit()
    return {"created": created, "entry": dict(existing), "summary": atlas_billing_summary(int(user_id))}


def save_content(slot_key: str, *, content_text: str, publish: bool, actor_user_id: int,
                 request_key: str) -> dict[str, Any]:
    slot = str(slot_key or "").strip().lower()
    if slot not in CONTENT_LIMITS:
        raise ValueError("atlas_content_slot_invalid")
    text = str(content_text or "").strip()
    if not text or len(text) > CONTENT_LIMITS[slot]:
        raise ValueError("atlas_content_value_invalid")
    key = _clean_request_key(request_key)
    now = utc_now_iso()
    with _db_lock, connect() as con:
        existing = con.execute("SELECT * FROM atlas_content_revisions WHERE request_key = ?", (key,)).fetchone()
        if existing is None:
            if publish:
                con.execute("UPDATE atlas_content_revisions SET status = 'archived' WHERE slot_key = ? AND status = 'published'", (slot,))
            cursor = con.execute(
                """INSERT INTO atlas_content_revisions(slot_key, content_text, status, actor_user_id,
                   request_key, created_at, published_at) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (slot, text, "published" if publish else "draft", int(actor_user_id), key, now, now if publish else None),
            )
            existing = con.execute("SELECT * FROM atlas_content_revisions WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
        con.commit()
    return dict(existing)


def content_manifest(*, include_drafts: bool = False) -> dict[str, Any]:
    with connect_readonly() as con:
        rows = con.execute(
            "SELECT * FROM atlas_content_revisions ORDER BY slot_key, id DESC"
        ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        if not include_drafts and item["status"] != "published":
            continue
        grouped.setdefault(str(item["slot_key"]), []).append(item)
    return {
        "slots": {slot: items[0] for slot, items in grouped.items() if items},
        "history": grouped if include_drafts else {},
        "limits": CONTENT_LIMITS if include_drafts else {},
    }


def _normalize_blocks(blocks: Any) -> list[dict[str, Any]]:
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 120:
        raise ValueError("atlas_legal_blocks_invalid")
    result: list[dict[str, Any]] = []
    total = 0
    for raw in blocks:
        if not isinstance(raw, dict):
            raise ValueError("atlas_legal_block_invalid")
        kind = str(raw.get("type") or "paragraph").strip().lower()
        if kind not in LEGAL_BLOCK_TYPES:
            raise ValueError("atlas_legal_block_type_invalid")
        title = " ".join(str(raw.get("title") or "").split())[:220]
        if kind == "list":
            items = [str(item).strip()[:4000] for item in raw.get("items", []) if str(item).strip()][:50]
            if not items:
                raise ValueError("atlas_legal_list_empty")
            block = {"type": kind, "title": title, "items": items}
            total += sum(len(item) for item in items)
        else:
            text = str(raw.get("text") or "").strip()[:12000]
            if not text and kind != "section":
                raise ValueError("atlas_legal_block_text_required")
            if kind == "section" and not title:
                raise ValueError("atlas_legal_section_title_required")
            block = {"type": kind, "title": title, "text": text}
            total += len(text)
        result.append(block)
    if total > 180_000:
        raise ValueError("atlas_legal_document_too_large")
    return result


def create_legal_revision(document_key: str, *, version_label: str, title: str, summary: str,
                          effective_from: str, blocks: Any, actor_user_id: int,
                          request_key: str) -> dict[str, Any]:
    document = str(document_key or "").strip().lower()
    if document not in LEGAL_DOCUMENTS:
        raise ValueError("atlas_legal_document_invalid")
    label = " ".join(str(version_label or "").split())[:80]
    clean_title = " ".join(str(title or "").split())[:220]
    if not label or not clean_title:
        raise ValueError("atlas_legal_metadata_required")
    try:
        datetime.fromisoformat(str(effective_from).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("atlas_legal_effective_date_invalid") from exc
    normalized = _normalize_blocks(blocks)
    key = _clean_request_key(request_key)
    now = utc_now_iso()
    with _db_lock, connect() as con:
        existing = con.execute("SELECT * FROM atlas_legal_revisions WHERE request_key = ?", (key,)).fetchone()
        if existing is None:
            latest = con.execute(
                "SELECT COALESCE(MAX(version_number), 0) AS value FROM atlas_legal_revisions WHERE document_key = ?",
                (document,),
            ).fetchone()
            version = int(latest["value"] or 0) + 1
            cursor = con.execute(
                """INSERT INTO atlas_legal_revisions(document_key, version_number, version_label,
                   title, summary, effective_from, blocks_json, status, actor_user_id, request_key, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)""",
                (document, version, label, clean_title, str(summary or "").strip()[:2000],
                 str(effective_from), json.dumps(normalized, ensure_ascii=False), int(actor_user_id), key, now),
            )
            existing = con.execute("SELECT * FROM atlas_legal_revisions WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
        con.commit()
    return _legal_dict(existing)


def publish_legal_revision(revision_id: int, *, actor_user_id: int) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM atlas_legal_revisions WHERE id = ?", (int(revision_id),)).fetchone()
        if row is None:
            raise ValueError("atlas_legal_revision_not_found")
        con.execute(
            "UPDATE atlas_legal_revisions SET status = 'archived' WHERE document_key = ? AND status = 'published' AND id <> ?",
            (str(row["document_key"]), int(revision_id)),
        )
        con.execute(
            "UPDATE atlas_legal_revisions SET status = 'published', published_at = ?, actor_user_id = ? WHERE id = ?",
            (now, int(actor_user_id), int(revision_id)),
        )
        updated = con.execute("SELECT * FROM atlas_legal_revisions WHERE id = ?", (int(revision_id),)).fetchone()
        con.commit()
    return _legal_dict(updated)


def set_legal_pdf_hash(revision_id: int, sha256: str) -> None:
    with _db_lock, connect() as con:
        con.execute("UPDATE atlas_legal_revisions SET pdf_sha256 = ? WHERE id = ?", (str(sha256), int(revision_id)))
        con.commit()


def _legal_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    try:
        item["blocks"] = json.loads(str(item.pop("blocks_json") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        item["blocks"] = []
    return item


def legal_revisions(document_key: str = "", *, include_drafts: bool = False) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    document = str(document_key or "").strip().lower()
    if document:
        if document not in LEGAL_DOCUMENTS:
            raise ValueError("atlas_legal_document_invalid")
        clauses.append("document_key = ?")
        params.append(document)
    if not include_drafts:
        clauses.append("status IN ('published', 'archived')")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect_readonly() as con:
        rows = con.execute(
            f"SELECT * FROM atlas_legal_revisions {where} ORDER BY document_key, effective_from DESC, version_number DESC",
            params,
        ).fetchall()
    return [_legal_dict(row) for row in rows]


def legal_revision(revision_id: int) -> dict[str, Any] | None:
    with connect_readonly() as con:
        row = con.execute("SELECT * FROM atlas_legal_revisions WHERE id = ?", (int(revision_id),)).fetchone()
    return _legal_dict(row) if row is not None else None


def current_legal_revision(document_key: str) -> dict[str, Any] | None:
    document = str(document_key or "").strip().lower()
    if document not in LEGAL_DOCUMENTS:
        raise ValueError("atlas_legal_document_invalid")
    now = utc_now_iso()
    with connect_readonly() as con:
        row = con.execute(
            """SELECT * FROM atlas_legal_revisions
               WHERE document_key = ? AND status IN ('published', 'archived') AND effective_from <= ?
               ORDER BY effective_from DESC, version_number DESC LIMIT 1""",
            (document, now),
        ).fetchone()
    return _legal_dict(row) if row is not None else None


def save_external_snapshot(provider: str, *, payload: dict[str, Any], status: str,
                           error_text: str = "") -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """INSERT INTO atlas_external_snapshots(provider, payload_json, status, error_text, checked_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(provider) DO UPDATE SET payload_json = excluded.payload_json,
                   status = excluded.status, error_text = excluded.error_text,
                   checked_at = excluded.checked_at, updated_at = excluded.updated_at""",
            (str(provider), json.dumps(payload, ensure_ascii=False), str(status)[:30], str(error_text)[:1000], now, now),
        )
        con.commit()


__all__ = [
    "CONTENT_LIMITS", "LEGAL_DOCUMENTS", "add_cost", "content_manifest",
    "create_legal_revision", "current_legal_revision", "finance_dashboard",
    "grant_subscription", "legal_revision", "legal_revisions", "list_orders",
    "publish_legal_revision", "save_content", "save_external_snapshot", "set_legal_pdf_hash",
]
