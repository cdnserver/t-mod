from __future__ import annotations

import json
import sqlite3
from typing import Any

from persistence.core import _db_lock, _table_columns, connect, utc_now_iso
from persistence.activity_repository import _bot_action_dict, bot_get_action
from persistence.craft_repository import _craft_add_event, _craft_recipe_from_con
from persistence.finance_repository import (
    _finance_enqueue_notifications,
    _finance_replay_balance,
    finance_undo_last_action,
)
from persistence.tvrs_repository import (
    _tvrs_assert_bill_admin_mutable,
    _tvrs_assert_result_admin_mutable,
    _tvrs_bill_referenced_by_active_consensus,
)

def _finance_reverse_event_in_con(
    con: sqlite3.Connection,
    *,
    guild_id: int,
    event_id: int,
    undone_by_id: int,
    undone_by_display: str | None,
    reason: str,
    channel_id: int | None,
    log_channel_id: int,
    admin_user_id: int,
    now: str,
) -> int | None:
    target = con.execute(
        """
        SELECT e.* FROM finance_events AS e
        WHERE e.guild_id = ? AND e.id = ?
          AND e.event_kind IN ('daily', 'interim', 'deposit', 'withdraw')
          AND NOT EXISTS (
              SELECT 1 FROM finance_events AS u WHERE u.reversed_event_id = e.id
          )
        """,
        (guild_id, event_id),
    ).fetchone()
    if target is None:
        return None
    active_rows = con.execute(
        """
        SELECT e.* FROM finance_events AS e
        WHERE e.guild_id = ?
          AND e.event_kind IN ('daily', 'interim', 'deposit', 'withdraw')
          AND NOT EXISTS (
              SELECT 1 FROM finance_events AS u WHERE u.reversed_event_id = e.id
          )
        ORDER BY e.id ASC
        """,
        (guild_id,),
    ).fetchall()
    balance_before = _finance_replay_balance(active_rows)
    balance_after = _finance_replay_balance(active_rows, omit_event_id=event_id)
    undo_delta = (
        balance_after - balance_before
        if balance_before is not None and balance_after is not None
        else None
    )
    cur = con.execute(
        """
        INSERT INTO finance_events(
            guild_id, event_kind, report_date, prompt_id, reversed_event_id,
            amount, delta, balance_before, balance_after, reason,
            captcha_digest, game_code, actor_id, actor_display,
            channel_id, message_id, created_at
        )
        VALUES(?, 'undo', NULL, NULL, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL, ?)
        """,
        (
            guild_id,
            event_id,
            int(target["amount"]),
            undo_delta,
            balance_before,
            balance_after,
            reason,
            undone_by_id,
            undone_by_display,
            channel_id,
            now,
        ),
    )
    undo_id = int(cur.lastrowid)
    _finance_enqueue_notifications(
        con,
        event_id=undo_id,
        log_channel_id=log_channel_id,
        admin_user_id=admin_user_id,
        now=now,
    )
    return undo_id


def _assert_tvrs_generic_restore_mutable(
    con: sqlite3.Connection,
    payload: dict[str, Any],
) -> None:
    """Fence universal undo from an unfinished consensus lifecycle."""

    table = str(payload.get("table") or "")
    if table not in {"tvrs_bills", "tvrs_votes", "tvrs_live_results"}:
        return
    row_id = int(payload.get("row_id"))
    current = con.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    before_raw = payload.get("before")
    before = dict(before_raw) if isinstance(before_raw, dict) else None
    candidate: sqlite3.Row | dict[str, Any] | None = current or before
    try:
        if table == "tvrs_bills":
            if candidate is not None:
                _tvrs_assert_bill_admin_mutable(con, candidate)  # type: ignore[arg-type]
            elif _tvrs_bill_referenced_by_active_consensus(con, row_id):
                raise ValueError("bill_locked_by_active_consensus")
            return
        if table == "tvrs_live_results":
            if candidate is not None:
                _tvrs_assert_result_admin_mutable(con, candidate)  # type: ignore[arg-type]
            return
        if candidate is not None and _tvrs_bill_referenced_by_active_consensus(
            con,
            int(candidate["bill_id"]),
            int(candidate["guild_id"]),
        ):
            raise ValueError("vote_locked_by_active_consensus")
    except ValueError as exc:
        if str(exc) in {
            "bill_locked_by_active_consensus",
            "result_locked_by_active_consensus",
            "vote_locked_by_active_consensus",
        }:
            raise ValueError("bot_action_locked_by_active_consensus") from exc
        raise


def _restore_generic_row(con: sqlite3.Connection, payload: dict[str, Any]) -> None:
    allowed_tables = {
        "bureau_announcements",
        "sgl_cases",
        "sgl_case_events",
        "sgl_receipts",
        "client_profiles",
        "lawyer_profiles",
        "tvrs_bills",
        "tvrs_votes",
        "tvrs_live_results",
    }
    table = str(payload.get("table") or "")
    if table not in allowed_tables:
        raise ValueError("bot_action_unsupported")
    _assert_tvrs_generic_restore_mutable(con, payload)
    before = payload.get("before")
    primary_key = str(payload.get("primary_key") or "id")
    if primary_key != "id":
        raise ValueError("bot_action_unsupported")
    row_id = int(payload.get("row_id"))
    columns = _table_columns(con, table)
    if before is None:
        con.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        return
    clean = {key: value for key, value in dict(before).items() if key in columns}
    existing = con.execute(f"SELECT id FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if existing is None:
        names = list(clean.keys())
        placeholders = ", ".join("?" for _ in names)
        con.execute(
            f"INSERT INTO {table}({', '.join(names)}) VALUES({placeholders})",
            [clean[name] for name in names],
        )
    else:
        names = [name for name in clean.keys() if name != "id"]
        if not names:
            return
        assignments = ", ".join(f"{name} = ?" for name in names)
        con.execute(
            f"UPDATE {table} SET {assignments} WHERE id = ?",
            [*[clean[name] for name in names], row_id],
        )


def _restore_generic_rows(con: sqlite3.Connection, payload: dict[str, Any]) -> None:
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("bot_action_unsupported")
    # Parent records are intentionally restored first. SQLite installations with
    # foreign-key enforcement enabled can then safely accept their child rows.
    priority = {"sgl_cases": 0, "tvrs_bills": 0, "client_profiles": 0, "lawyer_profiles": 0}
    ordered = sorted(rows, key=lambda item: priority.get(str(dict(item).get("table") or ""), 1))
    # Validate the entire batch before its first write. The surrounding undo
    # transaction is still the final safety net, but preflight keeps protected
    # consensus state untouched even temporarily.
    for row_payload in ordered:
        if not isinstance(row_payload, dict):
            raise ValueError("bot_action_unsupported")
        _assert_tvrs_generic_restore_mutable(con, row_payload)
    for row_payload in ordered:
        if not isinstance(row_payload, dict):
            raise ValueError("bot_action_unsupported")
        _restore_generic_row(con, row_payload)


def bot_undo_action(
    *,
    guild_id: int,
    target_actor_id: int,
    undone_by_id: int,
    undone_by_display: str | None,
    reason: str | None,
    log_channel_id: int,
    admin_user_id: int,
    channel_id: int | None,
    action_id: int | None = None,
) -> dict[str, Any] | None:
    """Undo a recorded bot action without deleting its audit trail.

    Craft actions are compensated in-place. Finance actions use the immutable
    finance ledger. Other modules may register a safe generic row snapshot.
    """
    clean_reason = str(reason or "").strip() or "Отмена действия пользователем"
    with _db_lock:
        with connect() as lookup:
            if action_id is None:
                action_row = lookup.execute(
                    """
                    SELECT * FROM bot_actions
                    WHERE guild_id = ? AND actor_id = ? AND status = 'active'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (guild_id, target_actor_id),
                ).fetchone()
            else:
                action_row = lookup.execute(
                    "SELECT * FROM bot_actions WHERE guild_id = ? AND id = ?",
                    (guild_id, int(action_id)),
                ).fetchone()
        action = _bot_action_dict(action_row)
        if action is None:
            return None
        if str(action["status"]) != "active":
            raise ValueError("bot_action_already_undone")
        if not int(action["reversible"]):
            raise ValueError("bot_action_not_reversible")

        if str(action["module"]) == "finance":
            result = finance_undo_last_action(
                guild_id=guild_id,
                target_actor_id=int(action["actor_id"]),
                undone_by_id=undone_by_id,
                undone_by_display=undone_by_display,
                channel_id=channel_id,
                message_id=None,
                log_channel_id=log_channel_id,
                admin_user_id=admin_user_id,
                target_event_id=int(action["payload"]["finance_event_id"]),
            )
            return {
                "action": bot_get_action(int(action["id"]), guild_id),
                "module": "finance",
                "finance": result,
                "refresh_plan_id": None,
                "completion_message_id": None,
            }

        now = utc_now_iso()
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current_row = con.execute(
                "SELECT * FROM bot_actions WHERE id = ? AND guild_id = ?",
                (int(action["id"]), guild_id),
            ).fetchone()
            if current_row is None or str(current_row["status"]) != "active":
                con.rollback()
                raise ValueError("bot_action_already_undone")
            if action.get("target_id") is not None:
                dependent = con.execute(
                    """
                    SELECT id, summary FROM bot_actions
                    WHERE guild_id = ? AND target_type = ? AND target_id = ?
                      AND status = 'active' AND id > ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (
                        guild_id,
                        str(action["target_type"]),
                        action["target_id"],
                        int(action["id"]),
                    ),
                ).fetchone()
                if dependent is not None:
                    con.rollback()
                    raise ValueError(f"bot_action_has_dependents:{int(dependent['id'])}")

            payload = dict(action.get("payload") or {})
            kind = str(action["action_kind"])
            refresh_plan_id: int | None = None
            completion_message_id: int | None = None
            finance_undo_event_id: int | None = None

            if kind == "recipe_created":
                recipe_id = int(payload["recipe_id"])
                recipe = con.execute("SELECT * FROM craft_recipes WHERE id = ?", (recipe_id,)).fetchone()
                if recipe is None:
                    con.rollback()
                    raise ValueError("craft_recipe_not_found")
                new_version = int(recipe["version"] or 1) + 1
                con.execute(
                    "UPDATE craft_recipes SET active = 0, version = ?, updated_at = ? WHERE id = ?",
                    (new_version, now, recipe_id),
                )
            elif kind == "recipe_updated":
                recipe_id = int(payload["recipe_id"])
                previous = dict(payload["previous"])
                recipe = con.execute("SELECT * FROM craft_recipes WHERE id = ?", (recipe_id,)).fetchone()
                if recipe is None:
                    con.rollback()
                    raise ValueError("craft_recipe_not_found")
                new_version = int(recipe["version"] or 1) + 1
                con.execute(
                    """
                    UPDATE craft_recipes
                    SET product_name = ?, treasury_cost_per_unit = ?, duration_minutes_per_unit = ?,
                        max_batch_size = ?, active = ?, version = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        previous["product_name"],
                        int(previous["treasury_cost_per_unit"]),
                        int(previous["duration_minutes_per_unit"]),
                        int(previous["max_batch_size"]),
                        int(previous["active"]),
                        new_version,
                        now,
                        recipe_id,
                    ),
                )
                con.execute("DELETE FROM craft_recipe_materials WHERE recipe_id = ?", (recipe_id,))
                con.executemany(
                    "INSERT INTO craft_recipe_materials(recipe_id, material_name, quantity_per_unit, created_at) VALUES(?, ?, ?, ?)",
                    [(recipe_id, str(item[0]), int(item[1]), now) for item in previous["materials"]],
                )
            elif kind in {"recipe_enabled", "recipe_disabled"}:
                recipe_id = int(payload["recipe_id"])
                recipe = con.execute("SELECT * FROM craft_recipes WHERE id = ?", (recipe_id,)).fetchone()
                if recipe is None:
                    con.rollback()
                    raise ValueError("craft_recipe_not_found")
                con.execute(
                    "UPDATE craft_recipes SET active = ?, version = ?, updated_at = ? WHERE id = ?",
                    (int(payload["previous_active"]), int(recipe["version"] or 1) + 1, now, recipe_id),
                )
            elif kind == "plan_created":
                plan_id = int(payload["plan_id"])
                con.execute(
                    "UPDATE craft_plans SET stage = 'cancelled', completed_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, plan_id),
                )
                refresh_plan_id = plan_id
            elif kind == "craft_purchase":
                plan_id = int(payload["plan_id"])
                purchase = con.execute(
                    "SELECT * FROM craft_purchases WHERE id = ? AND undone_at IS NULL",
                    (int(payload["purchase_id"]),),
                ).fetchone()
                if purchase is None:
                    con.rollback()
                    raise ValueError("bot_action_already_undone")
                con.execute(
                    """
                    UPDATE craft_plan_materials
                    SET stock_quantity = MAX(0, stock_quantity - ?),
                        purchased_quantity = MAX(0, purchased_quantity - ?),
                        spent_total = MAX(0, spent_total - ?), updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        int(purchase["quantity"]),
                        int(purchase["quantity"]),
                        int(purchase["total_cost"]),
                        now,
                        int(purchase["plan_material_id"]),
                    ),
                )
                con.execute(
                    "UPDATE craft_purchases SET undone_at = ?, undone_by_id = ?, undo_reason = ? WHERE id = ?",
                    (now, undone_by_id, clean_reason, int(purchase["id"])),
                )
                con.execute(
                    "UPDATE craft_plans SET stage = ?, updated_at = ? WHERE id = ?",
                    (str(payload.get("stage_before") or "procurement"), now, plan_id),
                )
                refresh_plan_id = plan_id
            elif kind == "craft_inventory_check":
                plan_id = int(payload["plan_id"])
                check = con.execute(
                    "SELECT * FROM craft_inventory_checks WHERE id = ? AND undone_at IS NULL",
                    (int(payload["inventory_check_id"]),),
                ).fetchone()
                if check is None:
                    con.rollback()
                    raise ValueError("bot_action_already_undone")
                previous_materials = json.loads(str(check["previous_materials_json"] or "{}"))
                for material_id, quantity in previous_materials.items():
                    con.execute(
                        "UPDATE craft_plan_materials SET stock_quantity = ?, updated_at = ? WHERE id = ? AND plan_id = ?",
                        (int(quantity), now, int(material_id), plan_id),
                    )
                con.execute(
                    """
                    UPDATE craft_plans SET product_stock = ?, stage = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        int(check["previous_product_quantity"] or 0),
                        str(check["previous_stage"] or "procurement"),
                        now,
                        plan_id,
                    ),
                )
                con.execute(
                    "UPDATE craft_inventory_checks SET undone_at = ?, undone_by_id = ?, undo_reason = ? WHERE id = ?",
                    (now, undone_by_id, clean_reason, int(check["id"])),
                )
                refresh_plan_id = plan_id
            elif kind == "craft_batch_started":
                plan_id = int(payload["plan_id"])
                batch = con.execute(
                    "SELECT * FROM craft_batches WHERE id = ? AND undone_at IS NULL",
                    (int(payload["batch_id"]),),
                ).fetchone()
                if batch is None:
                    con.rollback()
                    raise ValueError("bot_action_already_undone")
                quantity = int(batch["quantity"])
                materials = con.execute(
                    "SELECT * FROM craft_plan_materials WHERE plan_id = ?",
                    (plan_id,),
                ).fetchall()
                for material in materials:
                    con.execute(
                        "UPDATE craft_plan_materials SET stock_quantity = stock_quantity + ?, updated_at = ? WHERE id = ?",
                        (int(material["quantity_per_unit"]) * quantity, now, int(material["id"])),
                    )
                was_completed = str(batch["status"]) == "completed"
                plan_row = con.execute("SELECT completion_message_id FROM craft_plans WHERE id = ?", (plan_id,)).fetchone()
                completion_message_id = plan_row["completion_message_id"] if plan_row else None
                con.execute(
                    """
                    UPDATE craft_plans
                    SET attempts_queued = MAX(0, attempts_queued - ?),
                        attempts_completed = MAX(0, attempts_completed - ?),
                        product_stock = MAX(0, product_stock - ?),
                        stage = ?, completed_at = NULL,
                        completion_message_id = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        quantity,
                        quantity if was_completed else 0,
                        quantity if was_completed else 0,
                        str(payload.get("stage_before") or "crafting"),
                        now,
                        plan_id,
                    ),
                )
                con.execute(
                    "UPDATE craft_batches SET status = 'cancelled', undone_at = ?, undone_by_id = ?, undo_reason = ? WHERE id = ?",
                    (now, undone_by_id, clean_reason, int(batch["id"])),
                )
                if batch["finance_event_id"] is not None:
                    finance_undo_event_id = _finance_reverse_event_in_con(
                        con,
                        guild_id=guild_id,
                        event_id=int(batch["finance_event_id"]),
                        undone_by_id=undone_by_id,
                        undone_by_display=undone_by_display,
                        reason=f"Отмена цикла крафта #{plan_id}, партия #{batch['id']}: {clean_reason}",
                        channel_id=channel_id,
                        log_channel_id=int(payload.get("log_channel_id") or log_channel_id),
                        admin_user_id=int(payload.get("admin_user_id") or admin_user_id),
                        now=now,
                    )
                refresh_plan_id = plan_id
            elif kind == "craft_final_output":
                plan_id = int(payload["plan_id"])
                plan = con.execute("SELECT * FROM craft_plans WHERE id = ?", (plan_id,)).fetchone()
                completion_message_id = plan["completion_message_id"] if plan else None
                con.execute(
                    """
                    UPDATE craft_plans
                    SET product_stock = ?, final_product_qty = ?, stage = ?, completed_at = ?,
                        completion_message_id = NULL, updated_at = ? WHERE id = ?
                    """,
                    (
                        int(payload.get("previous_product_stock") or 0),
                        payload.get("previous_final_product_qty"),
                        str(payload.get("previous_stage") or "awaiting_output"),
                        payload.get("previous_completed_at"),
                        now,
                        plan_id,
                    ),
                )
                refresh_plan_id = plan_id
            elif kind == "craft_estimated_price":
                plan_id = int(payload["plan_id"])
                con.execute(
                    "UPDATE craft_plans SET estimated_unit_price = ?, updated_at = ? WHERE id = ?",
                    (payload.get("previous_price"), now, plan_id),
                )
                refresh_plan_id = plan_id
            elif kind == "craft_market_listing":
                plan_id = int(payload["plan_id"])
                listing = con.execute(
                    "SELECT * FROM craft_market_listings WHERE id = ? AND undone_at IS NULL",
                    (int(payload["listing_id"]),),
                ).fetchone()
                if listing is None:
                    con.rollback()
                    raise ValueError("bot_action_already_undone")
                con.execute(
                    "UPDATE craft_market_listings SET undone_at = ?, undone_by_id = ?, undo_reason = ? WHERE id = ?",
                    (now, undone_by_id, clean_reason, int(listing["id"])),
                )
                con.execute(
                    """
                    UPDATE craft_plans SET market_listed_qty = MAX(0, market_listed_qty - ?),
                        stage = ?, updated_at = ? WHERE id = ?
                    """,
                    (int(listing["quantity"]), str(payload.get("previous_stage") or "listing"), now, plan_id),
                )
                refresh_plan_id = plan_id
            elif kind == "craft_sale":
                plan_id = int(payload["plan_id"])
                sale = con.execute(
                    "SELECT * FROM craft_sales WHERE id = ? AND undone_at IS NULL",
                    (int(payload["sale_id"]),),
                ).fetchone()
                if sale is None:
                    con.rollback()
                    raise ValueError("bot_action_already_undone")
                plan = con.execute("SELECT * FROM craft_plans WHERE id = ?", (plan_id,)).fetchone()
                completion_message_id = plan["completion_message_id"] if plan else None
                con.execute(
                    "UPDATE craft_sales SET undone_at = ?, undone_by_id = ?, undo_reason = ? WHERE id = ?",
                    (now, undone_by_id, clean_reason, int(sale["id"])),
                )
                con.execute(
                    """
                    UPDATE craft_plans
                    SET sold_qty = MAX(0, sold_qty - ?), total_revenue = MAX(0, total_revenue - ?),
                        product_stock = product_stock + ?, stage = ?, completed_at = ?,
                        completion_message_id = NULL, updated_at = ? WHERE id = ?
                    """,
                    (
                        int(sale["quantity"]),
                        int(sale["total_amount"]),
                        int(sale["quantity"]),
                        str(payload.get("previous_stage") or "selling"),
                        payload.get("previous_completed_at"),
                        now,
                        plan_id,
                    ),
                )
                refresh_plan_id = plan_id
            elif kind == "generic_row_restore":
                _restore_generic_row(con, payload)
            elif kind == "generic_rows_restore":
                _restore_generic_rows(con, payload)
            else:
                con.rollback()
                raise ValueError("bot_action_unsupported")

            if kind in {"recipe_created", "recipe_updated", "recipe_enabled", "recipe_disabled"}:
                recipe_id = int(payload["recipe_id"])
                restored = _craft_recipe_from_con(con, recipe_id)
                if restored is not None:
                    restored_materials = [
                        [item["material_name"], int(item["quantity_per_unit"])]
                        for item in restored["materials"]
                    ]
                    con.execute(
                        """
                        INSERT OR IGNORE INTO craft_recipe_versions(
                            recipe_id, version, product_name, treasury_cost_per_unit,
                            duration_minutes_per_unit, max_batch_size, materials_json,
                            active, changed_by_id, changed_by_display, change_kind, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'undo_restore', ?)
                        """,
                        (
                            recipe_id,
                            int(restored["version"]),
                            restored["product_name"],
                            int(restored["treasury_cost_per_unit"]),
                            int(restored["duration_minutes_per_unit"]),
                            int(restored["max_batch_size"]),
                            json.dumps(restored_materials, ensure_ascii=False),
                            int(restored["active"]),
                            undone_by_id,
                            undone_by_display,
                            now,
                        ),
                    )

            if refresh_plan_id is not None:
                _craft_add_event(
                    con,
                    plan_id=refresh_plan_id,
                    guild_id=guild_id,
                    event_kind="action_undone",
                    actor_id=undone_by_id,
                    actor_display=undone_by_display,
                    details={
                        "action_id": int(action["id"]),
                        "original_action": kind,
                        "reason": clean_reason,
                    },
                    now=now,
                )
            con.execute(
                """
                UPDATE bot_actions
                SET status = 'undone', undone_by_id = ?, undone_by_display = ?,
                    undone_at = ?, undo_reason = ?
                WHERE id = ? AND status = 'active'
                """,
                (undone_by_id, undone_by_display, now, clean_reason, int(action["id"])),
            )
            con.commit()
        return {
            "action": bot_get_action(int(action["id"]), guild_id),
            "module": str(action["module"]),
            "refresh_plan_id": refresh_plan_id,
            "completion_message_id": completion_message_id,
            "finance_undo_event_id": finance_undo_event_id,
        }

__all__ = ['_finance_reverse_event_in_con', '_assert_tvrs_generic_restore_mutable', '_restore_generic_row', '_restore_generic_rows', 'bot_undo_action']
