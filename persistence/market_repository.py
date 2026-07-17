from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from persistence.core import _db_lock, connect, utc_now_iso

def _market_optional_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _market_row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    try:
        metadata = json.loads(str(result.get("metadata_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    result["metadata"] = metadata if isinstance(metadata, dict) else {}
    result["external_id"] = str(result.get("external_id") or result.get("item_id") or "")
    return result


def market_replace_snapshot(
    *,
    server_id: str,
    category: str,
    server_name: str,
    source_updated_at: str,
    period_days: int | None,
    items: Iterable[dict[str, Any]],
    fetched_at: str | None = None,
    history_retention_days: int = 400,
) -> dict[str, Any]:
    """Atomically replace the current catalog and preserve one history row per source snapshot."""
    clean_server_id = str(server_id or "").strip().upper()
    clean_category = str(category or "").strip().lower()
    clean_source_updated_at = str(source_updated_at or "").strip()
    if not clean_server_id or not clean_category or not clean_source_updated_at:
        raise ValueError("market_snapshot_scope_required")
    now = str(fetched_at or utc_now_iso())
    prepared: list[tuple[Any, ...]] = []
    seen_ids: set[int] = set()
    seen_external_ids: set[str] = set()
    for raw in items:
        try:
            item_id = int(raw.get("item_id"))
        except (TypeError, ValueError):
            continue
        item_name = str(raw.get("item_name") or "").strip()
        external_id = str(raw.get("external_id") or item_id).strip()
        if item_id < 0 or not item_name or not external_id:
            continue
        if item_id in seen_ids or external_id in seen_external_ids:
            raise ValueError("market_snapshot_duplicate_key")
        seen_ids.add(item_id)
        seen_external_ids.add(external_id)
        normalized_name = str(raw.get("normalized_name") or item_name.casefold()).strip()
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        prepared.append(
            (
                clean_server_id,
                clean_category,
                item_id,
                external_id,
                item_name,
                normalized_name,
                metadata_json,
                _market_optional_non_negative_int(raw.get("total_count")) or 0,
                _market_optional_non_negative_int(raw.get("sold_count")) or 0,
                _market_optional_non_negative_int(raw.get("average_price")),
                _market_optional_non_negative_int(raw.get("min_price")),
                _market_optional_non_negative_int(raw.get("max_price")),
                clean_source_updated_at,
                now,
                now,
                now,
            )
        )
    if not prepared:
        raise ValueError("market_snapshot_empty")

    with _db_lock, connect() as con:
        con.execute(
            "UPDATE market_items SET active = 0, updated_at = ? WHERE server_id = ? AND category = ?",
            (now, clean_server_id, clean_category),
        )
        con.executemany(
            """
            INSERT INTO market_items(
                server_id, category, item_id, external_id, item_name, normalized_name, metadata_json,
                total_count, sold_count, average_price, min_price, max_price,
                source_updated_at, fetched_at, active, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(server_id, category, item_id) DO UPDATE SET
                external_id = excluded.external_id,
                item_name = excluded.item_name,
                normalized_name = excluded.normalized_name,
                metadata_json = excluded.metadata_json,
                total_count = excluded.total_count,
                sold_count = excluded.sold_count,
                average_price = excluded.average_price,
                min_price = excluded.min_price,
                max_price = excluded.max_price,
                source_updated_at = excluded.source_updated_at,
                fetched_at = excluded.fetched_at,
                active = 1,
                updated_at = excluded.updated_at
            """,
            prepared,
        )
        con.executemany(
            """
            INSERT INTO market_item_history(
                server_id, category, item_id, external_id, item_name, metadata_json, total_count, sold_count,
                average_price, min_price, max_price, source_updated_at, fetched_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, category, item_id, source_updated_at) DO UPDATE SET
                external_id = excluded.external_id,
                item_name = excluded.item_name,
                metadata_json = excluded.metadata_json,
                total_count = excluded.total_count,
                sold_count = excluded.sold_count,
                average_price = excluded.average_price,
                min_price = excluded.min_price,
                max_price = excluded.max_price,
                fetched_at = excluded.fetched_at
            """,
            [
                (
                    row[0], row[1], row[2], row[3], row[4], row[6], row[7], row[8],
                    row[9], row[10], row[11], row[12], row[13],
                )
                for row in prepared
            ],
        )
        con.execute(
            """
            INSERT INTO market_catalogs(
                server_id, category, server_name, record_count, source_updated_at,
                period_days, last_attempt_at, last_success_at, last_error,
                consecutive_failures, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)
            ON CONFLICT(server_id, category) DO UPDATE SET
                server_name = excluded.server_name,
                record_count = excluded.record_count,
                source_updated_at = excluded.source_updated_at,
                period_days = excluded.period_days,
                last_attempt_at = excluded.last_attempt_at,
                last_success_at = excluded.last_success_at,
                last_error = NULL,
                consecutive_failures = 0,
                updated_at = excluded.updated_at
            """,
            (
                clean_server_id,
                clean_category,
                str(server_name or "").strip(),
                len(prepared),
                clean_source_updated_at,
                _market_optional_non_negative_int(period_days),
                now,
                now,
                now,
                now,
            ),
        )
        if history_retention_days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=int(history_retention_days))).isoformat()
            con.execute("DELETE FROM market_item_history WHERE fetched_at < ?", (cutoff,))
            con.execute(
                """
                DELETE FROM market_alert_notifications
                WHERE status IN ('delivered', 'failed', 'cancelled') AND created_at < ?
                """,
                (cutoff,),
            )
    return market_catalog_status(clean_server_id, clean_category)


def market_record_sync_error(server_id: str, category: str, error: str) -> dict[str, Any]:
    clean_server_id = str(server_id or "").strip().upper()
    clean_category = str(category or "").strip().lower()
    if not clean_server_id or not clean_category:
        raise ValueError("market_sync_scope_required")
    now = utc_now_iso()
    clean_error = " ".join(str(error or "unknown").split())[:500]
    with _db_lock, connect() as con:
        con.execute(
            """
            INSERT INTO market_catalogs(
                server_id, category, server_name, record_count, last_attempt_at,
                last_error, consecutive_failures, created_at, updated_at
            ) VALUES(?, ?, '', 0, ?, ?, 1, ?, ?)
            ON CONFLICT(server_id, category) DO UPDATE SET
                last_attempt_at = excluded.last_attempt_at,
                last_error = excluded.last_error,
                consecutive_failures = market_catalogs.consecutive_failures + 1,
                updated_at = excluded.updated_at
            """,
            (clean_server_id, clean_category, now, clean_error, now, now),
        )
    return market_catalog_status(clean_server_id, clean_category)


def market_catalog_status(server_id: str, category: str = "items") -> dict[str, Any]:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM market_catalogs WHERE server_id = ? AND category = ?",
            (str(server_id).strip().upper(), str(category).strip().lower()),
        ).fetchone()
    return dict(row) if row is not None else {}


def market_list_items(
    server_id: str,
    category: str = "items",
    *,
    active_only: bool = True,
    limit: int = 10000,
) -> list[dict[str, Any]]:
    clauses = ["server_id = ?", "category = ?"]
    params: list[Any] = [str(server_id).strip().upper(), str(category).strip().lower()]
    if active_only:
        clauses.append("active = 1")
    params.append(max(1, min(int(limit), 10000)))
    with _db_lock, connect() as con:
        rows = con.execute(
            f"SELECT * FROM market_items WHERE {' AND '.join(clauses)} ORDER BY item_name COLLATE NOCASE LIMIT ?",
            params,
        ).fetchall()
    return [parsed for row in rows if (parsed := _market_row_dict(row)) is not None]


def market_get_item(server_id: str, item_id: int, category: str = "items") -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT * FROM market_items
            WHERE server_id = ? AND category = ? AND item_id = ? AND active = 1
            """,
            (str(server_id).strip().upper(), str(category).strip().lower(), int(item_id)),
        ).fetchone()
    return _market_row_dict(row)


def market_popular_items(server_id: str, category: str = "items", limit: int = 10) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM market_items
            WHERE server_id = ? AND category = ? AND active = 1
            ORDER BY sold_count DESC, total_count DESC, item_name COLLATE NOCASE
            LIMIT ?
            """,
            (
                str(server_id).strip().upper(),
                str(category).strip().lower(),
                max(1, min(int(limit), 25)),
            ),
        ).fetchall()
    return [parsed for row in rows if (parsed := _market_row_dict(row)) is not None]


def market_item_history(
    server_id: str,
    item_id: int,
    category: str = "items",
    limit: int = 30,
) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM market_item_history
            WHERE server_id = ? AND category = ? AND item_id = ?
            ORDER BY source_updated_at DESC
            LIMIT ?
            """,
            (
                str(server_id).strip().upper(),
                str(category).strip().lower(),
                int(item_id),
                max(1, min(int(limit), 365)),
            ),
        ).fetchall()
    return [parsed for row in rows if (parsed := _market_row_dict(row)) is not None]


def market_upsert_alert(
    *,
    discord_user_id: int,
    user_display: str | None,
    guild_id: int | None,
    server_id: str,
    category: str,
    item_id: int,
    target_price: int,
    min_quantity: int,
    current_source_updated_at: str | None,
    max_alerts_per_user: int = 20,
) -> dict[str, Any]:
    """Create or replace one personal, one-shot alert for an item."""
    clean_user_id = int(discord_user_id)
    clean_item_id = int(item_id)
    clean_target_price = int(target_price)
    clean_min_quantity = int(min_quantity)
    clean_server_id = str(server_id or "").strip().upper()
    clean_category = str(category or "").strip().lower()
    if clean_user_id <= 0 or clean_item_id < 0 or not clean_server_id or not clean_category:
        raise ValueError("market_alert_scope_required")
    if clean_target_price <= 0 or clean_min_quantity <= 0:
        raise ValueError("market_alert_thresholds_must_be_positive")
    if clean_target_price > 10**15 or clean_min_quantity > 10**15:
        raise ValueError("market_alert_thresholds_too_large")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        item = con.execute(
            """
            SELECT item_id FROM market_items
            WHERE server_id = ? AND category = ? AND item_id = ? AND active = 1
            """,
            (clean_server_id, clean_category, clean_item_id),
        ).fetchone()
        if item is None:
            raise ValueError("market_alert_item_not_found")
        existing = con.execute(
            """
            SELECT id FROM market_alerts
            WHERE discord_user_id = ? AND server_id = ? AND category = ? AND item_id = ?
            """,
            (clean_user_id, clean_server_id, clean_category, clean_item_id),
        ).fetchone()
        if existing is None:
            count = con.execute(
                "SELECT COUNT(*) AS amount FROM market_alerts WHERE discord_user_id = ?",
                (clean_user_id,),
            ).fetchone()
            if int(count["amount"] or 0) >= max(1, int(max_alerts_per_user)):
                raise ValueError("market_alert_limit_reached")
        con.execute(
            """
            INSERT INTO market_alerts(
                discord_user_id, user_display, guild_id, server_id, category, item_id,
                target_price, min_quantity, status, last_evaluated_source_at,
                triggered_at, last_delivery_error, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, NULL, ?, ?)
            ON CONFLICT(discord_user_id, server_id, category, item_id) DO UPDATE SET
                user_display = excluded.user_display,
                guild_id = excluded.guild_id,
                target_price = excluded.target_price,
                min_quantity = excluded.min_quantity,
                status = 'active',
                last_evaluated_source_at = excluded.last_evaluated_source_at,
                triggered_at = NULL,
                last_delivery_error = NULL,
                updated_at = excluded.updated_at
            """,
            (
                clean_user_id,
                str(user_display or "").strip()[:200] or None,
                int(guild_id) if guild_id else None,
                clean_server_id,
                clean_category,
                clean_item_id,
                clean_target_price,
                clean_min_quantity,
                str(current_source_updated_at or "").strip() or None,
                now,
                now,
            ),
        )
        con.execute(
            """
            UPDATE market_alert_notifications
            SET status = 'cancelled', updated_at = ?
            WHERE alert_id = (
                SELECT id FROM market_alerts
                WHERE discord_user_id = ? AND server_id = ? AND category = ? AND item_id = ?
            ) AND status IN ('pending', 'retry')
            """,
            (now, clean_user_id, clean_server_id, clean_category, clean_item_id),
        )
        row = con.execute(
            """
            SELECT a.*, i.external_id, i.item_name, i.metadata_json, i.average_price,
                   i.min_price, i.total_count, i.sold_count,
                   i.source_updated_at AS item_source_updated_at
            FROM market_alerts a
            JOIN market_items i
              ON i.server_id = a.server_id AND i.category = a.category AND i.item_id = a.item_id
            WHERE a.discord_user_id = ? AND a.server_id = ? AND a.category = ? AND a.item_id = ?
            """,
            (clean_user_id, clean_server_id, clean_category, clean_item_id),
        ).fetchone()
    parsed = _market_row_dict(row)
    if parsed is None:
        raise RuntimeError("market_alert_not_saved")
    return parsed


def market_get_alert(
    discord_user_id: int,
    server_id: str,
    item_id: int,
    category: str = "items",
) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT a.*, i.external_id, i.item_name, i.metadata_json, i.average_price,
                   i.min_price, i.total_count, i.sold_count,
                   i.source_updated_at AS item_source_updated_at
            FROM market_alerts a
            LEFT JOIN market_items i
              ON i.server_id = a.server_id AND i.category = a.category AND i.item_id = a.item_id
            WHERE a.discord_user_id = ? AND a.server_id = ? AND a.category = ? AND a.item_id = ?
            """,
            (
                int(discord_user_id),
                str(server_id).strip().upper(),
                str(category).strip().lower(),
                int(item_id),
            ),
        ).fetchone()
    return _market_row_dict(row)


def market_list_user_alerts(discord_user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT a.*, i.external_id, i.item_name, i.metadata_json, i.average_price,
                   i.min_price, i.total_count, i.sold_count,
                   i.source_updated_at AS item_source_updated_at
            FROM market_alerts a
            LEFT JOIN market_items i
              ON i.server_id = a.server_id AND i.category = a.category AND i.item_id = a.item_id
            WHERE a.discord_user_id = ?
            ORDER BY
                CASE a.status WHEN 'active' THEN 0 WHEN 'notifying' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END,
                a.updated_at DESC
            LIMIT ?
            """,
            (int(discord_user_id), max(1, min(int(limit), 25))),
        ).fetchall()
    return [parsed for row in rows if (parsed := _market_row_dict(row)) is not None]


def market_alert_stats(server_id: str, category: str = "items") -> dict[str, int]:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) AS active,
                COALESCE(SUM(CASE WHEN status = 'notifying' THEN 1 ELSE 0 END), 0) AS notifying,
                COALESCE(SUM(CASE WHEN status = 'triggered' THEN 1 ELSE 0 END), 0) AS triggered,
                COALESCE(SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END), 0) AS paused
            FROM market_alerts
            WHERE server_id = ? AND category = ?
            """,
            (str(server_id).strip().upper(), str(category).strip().lower()),
        ).fetchone()
        pending = con.execute(
            """
            SELECT COUNT(*) AS amount
            FROM market_alert_notifications n
            JOIN market_alerts a ON a.id = n.alert_id
            WHERE a.server_id = ? AND a.category = ? AND n.status IN ('pending', 'retry')
            """,
            (str(server_id).strip().upper(), str(category).strip().lower()),
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "active": int(row["active"] or 0),
        "notifying": int(row["notifying"] or 0),
        "triggered": int(row["triggered"] or 0),
        "paused": int(row["paused"] or 0),
        "pending_notifications": int(pending["amount"] or 0),
    }


def market_set_alert_status(
    discord_user_id: int,
    alert_id: int,
    status: str,
    *,
    current_source_updated_at: str | None = None,
) -> dict[str, Any] | None:
    clean_status = str(status or "").strip().lower()
    if clean_status not in {"active", "paused"}:
        raise ValueError("market_alert_status_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        if clean_status == "active":
            con.execute(
                """
                UPDATE market_alerts
                SET status = 'active', last_evaluated_source_at = ?, triggered_at = NULL,
                    last_delivery_error = NULL, updated_at = ?
                WHERE id = ? AND discord_user_id = ?
                """,
                (
                    str(current_source_updated_at or "").strip() or None,
                    now,
                    int(alert_id),
                    int(discord_user_id),
                ),
            )
        else:
            con.execute(
                """
                UPDATE market_alerts SET status = 'paused', updated_at = ?
                WHERE id = ? AND discord_user_id = ?
                """,
                (now, int(alert_id), int(discord_user_id)),
            )
            con.execute(
                """
                UPDATE market_alert_notifications
                SET status = 'cancelled', updated_at = ?
                WHERE alert_id = ? AND status IN ('pending', 'retry')
                """,
                (now, int(alert_id)),
            )
        row = con.execute(
            "SELECT * FROM market_alerts WHERE id = ? AND discord_user_id = ?",
            (int(alert_id), int(discord_user_id)),
        ).fetchone()
    return dict(row) if row is not None else None


def market_delete_alert(discord_user_id: int, alert_id: int) -> bool:
    with _db_lock, connect() as con:
        cursor = con.execute(
            "DELETE FROM market_alerts WHERE id = ? AND discord_user_id = ?",
            (int(alert_id), int(discord_user_id)),
        )
    return cursor.rowcount > 0


def market_evaluate_alerts(
    server_id: str,
    source_updated_at: str,
    category: str = "items",
) -> int:
    """Evaluate active alerts once per source snapshot and enqueue matching DMs."""
    clean_server_id = str(server_id or "").strip().upper()
    clean_category = str(category or "").strip().lower()
    clean_source = str(source_updated_at or "").strip()
    if not clean_server_id or not clean_category or not clean_source:
        return 0
    now = utc_now_iso()
    enqueued = 0
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT a.*, i.item_name, i.total_count, i.average_price, i.min_price
            FROM market_alerts a
            JOIN market_items i
              ON i.server_id = a.server_id AND i.category = a.category AND i.item_id = a.item_id
            WHERE a.server_id = ? AND a.category = ? AND a.status = 'active'
              AND i.active = 1 AND i.source_updated_at = ?
              AND COALESCE(a.last_evaluated_source_at, '') <> ?
            """,
            (clean_server_id, clean_category, clean_source, clean_source),
        ).fetchall()
        for row in rows:
            minimum = _market_optional_non_negative_int(row["min_price"])
            average = _market_optional_non_negative_int(row["average_price"])
            observed_price = minimum if minimum and minimum > 0 else average if average and average > 0 else None
            observed_quantity = _market_optional_non_negative_int(row["total_count"]) or 0
            matches = (
                observed_price is not None
                and observed_price <= int(row["target_price"])
                and observed_quantity >= int(row["min_quantity"])
            )
            con.execute(
                """
                UPDATE market_alerts
                SET last_evaluated_source_at = ?, last_observed_price = ?,
                    last_observed_quantity = ?, updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (clean_source, observed_price, observed_quantity, now, int(row["id"])),
            )
            if not matches:
                continue
            cursor = con.execute(
                """
                INSERT OR IGNORE INTO market_alert_notifications(
                    alert_id, source_updated_at, item_name, observed_price, observed_quantity,
                    status, attempt_count, next_attempt_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    int(row["id"]),
                    clean_source,
                    str(row["item_name"] or f"Предмет #{int(row['item_id'])}"),
                    int(observed_price),
                    observed_quantity,
                    now,
                    now,
                    now,
                ),
            )
            if cursor.rowcount > 0:
                con.execute(
                    "UPDATE market_alerts SET status = 'notifying', updated_at = ? WHERE id = ?",
                    (now, int(row["id"])),
                )
                enqueued += 1
    return enqueued


def market_pending_alert_notifications(limit: int = 25) -> list[dict[str, Any]]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT n.*, a.discord_user_id, a.server_id, a.category, a.item_id,
                   a.target_price, a.min_quantity, i.external_id, i.metadata_json
            FROM market_alert_notifications n
            JOIN market_alerts a ON a.id = n.alert_id
            LEFT JOIN market_items i
              ON i.server_id = a.server_id AND i.category = a.category AND i.item_id = a.item_id
            WHERE n.status IN ('pending', 'retry') AND n.next_attempt_at <= ?
              AND a.status = 'notifying'
            ORDER BY n.next_attempt_at, n.id
            LIMIT ?
            """,
            (now, max(1, min(int(limit), 100))),
        ).fetchall()
    return [parsed for row in rows if (parsed := _market_row_dict(row)) is not None]


def market_mark_alert_delivery(
    notification_id: int,
    *,
    delivered: bool,
    dm_message_id: int | None = None,
    error: str | None = None,
    max_attempts: int = 3,
    retry_seconds: int = 600,
) -> dict[str, Any] | None:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    clean_error = " ".join(str(error or "delivery_failed").split())[:500]
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM market_alert_notifications WHERE id = ?",
            (int(notification_id),),
        ).fetchone()
        if row is None:
            return None
        if str(row["status"]) not in {"pending", "retry"}:
            return dict(row)
        alert_id = int(row["alert_id"])
        if delivered:
            con.execute(
                """
                UPDATE market_alert_notifications
                SET status = 'delivered', attempt_count = attempt_count + 1,
                    dm_message_id = ?, last_error = NULL, delivered_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(dm_message_id) if dm_message_id else None, now, now, int(notification_id)),
            )
            con.execute(
                """
                UPDATE market_alerts
                SET status = 'triggered', triggered_at = ?, last_delivery_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, now, alert_id),
            )
        else:
            attempt_count = int(row["attempt_count"] or 0) + 1
            permanently_failed = attempt_count >= max(1, int(max_attempts))
            next_attempt = (now_dt + timedelta(seconds=max(60, int(retry_seconds)))).isoformat()
            con.execute(
                """
                UPDATE market_alert_notifications
                SET status = ?, attempt_count = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    "failed" if permanently_failed else "retry",
                    attempt_count,
                    next_attempt,
                    clean_error,
                    now,
                    int(notification_id),
                ),
            )
            con.execute(
                """
                UPDATE market_alerts
                SET status = ?, last_delivery_error = ?, updated_at = ?
                WHERE id = ?
                """,
                ("paused" if permanently_failed else "notifying", clean_error, now, alert_id),
            )
        result = con.execute(
            "SELECT * FROM market_alert_notifications WHERE id = ?",
            (int(notification_id),),
        ).fetchone()
    return dict(result) if result is not None else None

__all__ = ['_market_optional_non_negative_int', '_market_row_dict', 'market_replace_snapshot', 'market_record_sync_error', 'market_catalog_status', 'market_list_items', 'market_get_item', 'market_popular_items', 'market_item_history', 'market_upsert_alert', 'market_get_alert', 'market_list_user_alerts', 'market_alert_stats', 'market_set_alert_status', 'market_delete_alert', 'market_evaluate_alerts', 'market_pending_alert_notifications', 'market_mark_alert_delivery']
