from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso
from persistence.activity_repository import _record_bot_action
from persistence.finance_repository import _finance_enqueue_notifications, _finance_row

CRAFT_ACTIVE_STAGES = {"procurement", "crafting", "awaiting_output", "listing", "selling"}


def _craft_add_event(
    con: sqlite3.Connection,
    *,
    plan_id: int,
    guild_id: int,
    event_kind: str,
    actor_id: int | None,
    actor_display: str | None,
    details: dict[str, Any],
    now: str,
) -> int:
    cur = con.execute(
        """
        INSERT INTO craft_events(
            plan_id, guild_id, event_kind, actor_id, actor_display,
            details_json, thread_message_id, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (plan_id, guild_id, event_kind, actor_id, actor_display, json.dumps(details, ensure_ascii=False), now),
    )
    return int(cur.lastrowid)


def _craft_recipe_from_con(con: sqlite3.Connection, recipe_id: int) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM craft_recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    materials = con.execute(
        "SELECT * FROM craft_recipe_materials WHERE recipe_id = ? ORDER BY id ASC",
        (recipe_id,),
    ).fetchall()
    result["materials"] = [dict(item) for item in materials]
    return result


def craft_create_recipe(
    *,
    guild_id: int,
    product_name: str,
    treasury_cost_per_unit: int,
    duration_minutes_per_unit: int,
    max_batch_size: int,
    materials: list[tuple[str, int]],
    created_by_id: int,
    created_by_display: str | None,
) -> dict[str, Any]:
    product_name = str(product_name or "").strip()
    if not product_name or len(product_name) > 100:
        raise ValueError("craft_bad_product_name")
    if treasury_cost_per_unit < 0:
        raise ValueError("craft_bad_treasury_cost")
    if duration_minutes_per_unit <= 0 or duration_minutes_per_unit > 10080:
        raise ValueError("craft_bad_duration")
    if max_batch_size <= 0 or max_batch_size > 1000:
        raise ValueError("craft_bad_batch_size")
    if not materials or len(materials) > 20:
        raise ValueError("craft_bad_material_count")

    normalized: list[tuple[str, int]] = []
    seen: set[str] = set()
    for name, quantity in materials:
        clean_name = str(name or "").strip()
        key = clean_name.casefold()
        if not clean_name or len(clean_name) > 80 or key in seen or int(quantity) <= 0:
            raise ValueError("craft_bad_material")
        seen.add(key)
        normalized.append((clean_name, int(quantity)))

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            cur = con.execute(
                """
                INSERT INTO craft_recipes(
                    guild_id, product_name, treasury_cost_per_unit,
                    duration_minutes_per_unit, max_batch_size,
                    created_by_id, created_by_display, active, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    guild_id,
                    product_name,
                    int(treasury_cost_per_unit),
                    int(duration_minutes_per_unit),
                    int(max_batch_size),
                    created_by_id,
                    created_by_display,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            con.rollback()
            raise ValueError("craft_recipe_exists") from exc
        recipe_id = int(cur.lastrowid)
        con.executemany(
            """
            INSERT INTO craft_recipe_materials(
                recipe_id, material_name, quantity_per_unit, created_at
            )
            VALUES(?, ?, ?, ?)
            """,
            [(recipe_id, name, quantity, now) for name, quantity in normalized],
        )
        con.execute(
            """
            INSERT INTO craft_recipe_versions(
                recipe_id, version, product_name, treasury_cost_per_unit,
                duration_minutes_per_unit, max_batch_size, materials_json,
                active, changed_by_id, changed_by_display, change_kind, created_at
            )
            VALUES(?, 1, ?, ?, ?, ?, ?, 1, ?, ?, 'created', ?)
            """,
            (
                recipe_id,
                product_name,
                int(treasury_cost_per_unit),
                int(duration_minutes_per_unit),
                int(max_batch_size),
                json.dumps(normalized, ensure_ascii=False),
                created_by_id,
                created_by_display,
                now,
            ),
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=created_by_id,
            actor_display=created_by_display,
            module="craft",
            action_kind="recipe_created",
            target_type="craft_recipe",
            target_id=recipe_id,
            summary=f"Создан рецепт «{product_name}»",
            payload={"recipe_id": recipe_id, "version": 1},
            now=now,
        )
        recipe = _craft_recipe_from_con(con, recipe_id)
        con.commit()
    if recipe is None:
        raise RuntimeError("craft_recipe_create_failed")
    recipe["action_id"] = action_id
    return recipe


def craft_list_recipes(guild_id: int, *, active_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        if active_only:
            rows = con.execute(
                "SELECT id FROM craft_recipes WHERE guild_id = ? AND active = 1 ORDER BY product_name LIMIT ?",
                (guild_id, max(1, min(int(limit), 200))),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT id FROM craft_recipes WHERE guild_id = ? ORDER BY active DESC, product_name LIMIT ?",
                (guild_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [recipe for row in rows if (recipe := _craft_recipe_from_con(con, int(row["id"]))) is not None]


def craft_get_recipe(recipe_id: int, guild_id: int | None = None) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        recipe = _craft_recipe_from_con(con, recipe_id)
    if recipe is not None and guild_id is not None and int(recipe["guild_id"]) != guild_id:
        return None
    return recipe


def craft_recipe_versions(recipe_id: int, guild_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        recipe = con.execute("SELECT guild_id FROM craft_recipes WHERE id = ?", (recipe_id,)).fetchone()
        if recipe is None or (guild_id is not None and int(recipe["guild_id"]) != guild_id):
            return []
        rows = con.execute(
            "SELECT * FROM craft_recipe_versions WHERE recipe_id = ? ORDER BY version DESC LIMIT ?",
            (recipe_id, max(1, min(int(limit), 100))),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["materials"] = json.loads(str(item.get("materials_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            item["materials"] = []
        result.append(item)
    return result


def _normalize_craft_recipe_values(
    *,
    product_name: str,
    treasury_cost_per_unit: int,
    duration_minutes_per_unit: int,
    max_batch_size: int,
    materials: list[tuple[str, int]],
) -> tuple[str, list[tuple[str, int]]]:
    clean_product = str(product_name or "").strip()
    if not clean_product or len(clean_product) > 100:
        raise ValueError("craft_bad_product_name")
    if int(treasury_cost_per_unit) < 0:
        raise ValueError("craft_bad_treasury_cost")
    if int(duration_minutes_per_unit) <= 0 or int(duration_minutes_per_unit) > 10080:
        raise ValueError("craft_bad_duration")
    if int(max_batch_size) <= 0 or int(max_batch_size) > 1000:
        raise ValueError("craft_bad_batch_size")
    if not materials or len(materials) > 20:
        raise ValueError("craft_bad_material_count")
    normalized: list[tuple[str, int]] = []
    seen: set[str] = set()
    for name, quantity in materials:
        clean_name = str(name or "").strip()
        key = clean_name.casefold()
        if not clean_name or len(clean_name) > 80 or key in seen or int(quantity) <= 0:
            raise ValueError("craft_bad_material")
        seen.add(key)
        normalized.append((clean_name, int(quantity)))
    return clean_product, normalized


def craft_update_recipe(
    *,
    guild_id: int,
    recipe_id: int,
    product_name: str,
    treasury_cost_per_unit: int,
    duration_minutes_per_unit: int,
    max_batch_size: int,
    materials: list[tuple[str, int]],
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    clean_product, normalized = _normalize_craft_recipe_values(
        product_name=product_name,
        treasury_cost_per_unit=treasury_cost_per_unit,
        duration_minutes_per_unit=duration_minutes_per_unit,
        max_batch_size=max_batch_size,
        materials=materials,
    )
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        before = _craft_recipe_from_con(con, recipe_id)
        if before is None or int(before["guild_id"]) != guild_id:
            con.rollback()
            raise ValueError("craft_recipe_not_found")
        previous = {
            "product_name": before["product_name"],
            "treasury_cost_per_unit": int(before["treasury_cost_per_unit"]),
            "duration_minutes_per_unit": int(before["duration_minutes_per_unit"]),
            "max_batch_size": int(before["max_batch_size"]),
            "materials": [[item["material_name"], int(item["quantity_per_unit"])] for item in before["materials"]],
            "active": int(before["active"]),
            "version": int(before.get("version") or 1),
        }
        new_version = int(before.get("version") or 1) + 1
        try:
            con.execute(
                """
                UPDATE craft_recipes
                SET product_name = ?, treasury_cost_per_unit = ?,
                    duration_minutes_per_unit = ?, max_batch_size = ?,
                    version = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    clean_product,
                    int(treasury_cost_per_unit),
                    int(duration_minutes_per_unit),
                    int(max_batch_size),
                    new_version,
                    now,
                    recipe_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            con.rollback()
            raise ValueError("craft_recipe_exists") from exc
        con.execute("DELETE FROM craft_recipe_materials WHERE recipe_id = ?", (recipe_id,))
        con.executemany(
            """
            INSERT INTO craft_recipe_materials(recipe_id, material_name, quantity_per_unit, created_at)
            VALUES(?, ?, ?, ?)
            """,
            [(recipe_id, name, quantity, now) for name, quantity in normalized],
        )
        con.execute(
            """
            INSERT INTO craft_recipe_versions(
                recipe_id, version, product_name, treasury_cost_per_unit,
                duration_minutes_per_unit, max_batch_size, materials_json,
                active, changed_by_id, changed_by_display, change_kind, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'updated', ?)
            """,
            (
                recipe_id,
                new_version,
                clean_product,
                int(treasury_cost_per_unit),
                int(duration_minutes_per_unit),
                int(max_batch_size),
                json.dumps(normalized, ensure_ascii=False),
                int(before["active"]),
                actor_id,
                actor_display,
                now,
            ),
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="recipe_updated",
            target_type="craft_recipe",
            target_id=recipe_id,
            summary=f"Обновлён рецепт #{recipe_id}: «{clean_product}», версия {new_version}",
            payload={"recipe_id": recipe_id, "previous": previous, "version": new_version},
            now=now,
        )
        result = _craft_recipe_from_con(con, recipe_id)
        con.commit()
    result["action_id"] = action_id
    return result


def craft_set_recipe_active(
    *,
    guild_id: int,
    recipe_id: int,
    active: bool,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        before = _craft_recipe_from_con(con, recipe_id)
        if before is None or int(before["guild_id"]) != guild_id:
            con.rollback()
            raise ValueError("craft_recipe_not_found")
        if bool(before["active"]) == bool(active):
            result = before
            con.commit()
            result["action_id"] = None
            return result
        new_version = int(before.get("version") or 1) + 1
        con.execute(
            "UPDATE craft_recipes SET active = ?, version = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, new_version, now, recipe_id),
        )
        materials = [[item["material_name"], int(item["quantity_per_unit"])] for item in before["materials"]]
        con.execute(
            """
            INSERT INTO craft_recipe_versions(
                recipe_id, version, product_name, treasury_cost_per_unit,
                duration_minutes_per_unit, max_batch_size, materials_json,
                active, changed_by_id, changed_by_display, change_kind, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recipe_id,
                new_version,
                before["product_name"],
                int(before["treasury_cost_per_unit"]),
                int(before["duration_minutes_per_unit"]),
                int(before["max_batch_size"]),
                json.dumps(materials, ensure_ascii=False),
                1 if active else 0,
                actor_id,
                actor_display,
                "enabled" if active else "disabled",
                now,
            ),
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="recipe_enabled" if active else "recipe_disabled",
            target_type="craft_recipe",
            target_id=recipe_id,
            summary=f"{'Включён' if active else 'Отключён'} рецепт #{recipe_id}: «{before['product_name']}»",
            payload={"recipe_id": recipe_id, "previous_active": int(before["active"]), "version": new_version},
            now=now,
        )
        result = _craft_recipe_from_con(con, recipe_id)
        con.commit()
    result["action_id"] = action_id
    return result


def craft_clone_recipe(
    *,
    guild_id: int,
    recipe_id: int,
    product_name: str,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    source = craft_get_recipe(recipe_id, guild_id)
    if source is None:
        raise ValueError("craft_recipe_not_found")
    return craft_create_recipe(
        guild_id=guild_id,
        product_name=product_name,
        treasury_cost_per_unit=int(source["treasury_cost_per_unit"]),
        duration_minutes_per_unit=int(source["duration_minutes_per_unit"]),
        max_batch_size=int(source["max_batch_size"]),
        materials=[(item["material_name"], int(item["quantity_per_unit"])) for item in source["materials"]],
        created_by_id=actor_id,
        created_by_display=actor_display,
    )


def _craft_plan_from_con(con: sqlite3.Connection, plan_id: int) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM craft_plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    current_recipe = _craft_recipe_from_con(con, int(result["recipe_id"]))
    recipe = dict(current_recipe or {})
    recipe.update(
        {
            "id": int(result["recipe_id"]),
            "product_name": result.get("product_name_snapshot") or recipe.get("product_name") or "Неизвестный продукт",
            "treasury_cost_per_unit": (
                result.get("treasury_cost_per_unit_snapshot")
                if result.get("treasury_cost_per_unit_snapshot") is not None
                else recipe.get("treasury_cost_per_unit", 0)
            ),
            "duration_minutes_per_unit": (
                result.get("duration_minutes_per_unit_snapshot")
                if result.get("duration_minutes_per_unit_snapshot") is not None
                else recipe.get("duration_minutes_per_unit", 1)
            ),
            "max_batch_size": (
                result.get("max_batch_size_snapshot")
                if result.get("max_batch_size_snapshot") is not None
                else recipe.get("max_batch_size", 1)
            ),
            "version": int(result.get("recipe_version") or 1),
        }
    )
    result["recipe"] = recipe
    materials = con.execute(
        "SELECT * FROM craft_plan_materials WHERE plan_id = ? ORDER BY id ASC",
        (plan_id,),
    ).fetchall()
    result["materials"] = [dict(item) for item in materials]
    active_batch = con.execute(
        "SELECT * FROM craft_batches WHERE plan_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (plan_id,),
    ).fetchone()
    last_batch = con.execute(
        "SELECT * FROM craft_batches WHERE plan_id = ? ORDER BY id DESC LIMIT 1",
        (plan_id,),
    ).fetchone()
    result["active_batch"] = _finance_row(active_batch)
    result["last_batch"] = _finance_row(last_batch)
    purchase_row = con.execute(
        "SELECT COALESCE(SUM(total_cost), 0) AS total, COUNT(*) AS count FROM craft_purchases WHERE plan_id = ? AND undone_at IS NULL",
        (plan_id,),
    ).fetchone()
    result["purchase_cost_total"] = int(purchase_row["total"] or 0)
    result["purchase_count"] = int(purchase_row["count"] or 0)
    sale_row = con.execute(
        "SELECT COUNT(*) AS count FROM craft_sales WHERE plan_id = ? AND undone_at IS NULL",
        (plan_id,),
    ).fetchone()
    result["sale_count"] = int(sale_row["count"] or 0)
    return result


def craft_create_plan(
    *,
    guild_id: int,
    recipe_id: int,
    channel_id: int,
    attempts_total: int,
    responsible_id: int,
    responsible_display: str | None,
    created_by_id: int,
    created_by_display: str | None,
) -> dict[str, Any]:
    if attempts_total <= 0 or attempts_total > 1_000_000:
        raise ValueError("craft_bad_attempts")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        recipe = _craft_recipe_from_con(con, recipe_id)
        if recipe is None or int(recipe["guild_id"]) != guild_id or not int(recipe["active"]):
            con.rollback()
            raise ValueError("craft_recipe_not_found")
        material_totals = [int(material["quantity_per_unit"]) * int(attempts_total) for material in recipe["materials"]]
        if any(total > 9_000_000_000_000_000_000 for total in material_totals):
            con.rollback()
            raise ValueError("craft_plan_too_large")
        cur = con.execute(
            """
            INSERT INTO craft_plans(
                guild_id, recipe_id, channel_id, message_id, thread_id,
                responsible_id, responsible_display, created_by_id, created_by_display,
                recipe_version, product_name_snapshot, treasury_cost_per_unit_snapshot,
                duration_minutes_per_unit_snapshot, max_batch_size_snapshot,
                stage, attempts_total, attempts_queued, attempts_completed,
                product_stock, final_product_qty, estimated_unit_price,
                market_listed_qty, sold_qty, total_revenue,
                completion_message_id, created_at, updated_at, completed_at
            )
            VALUES(?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'procurement', ?, 0, 0,
                   0, NULL, NULL, 0, 0, 0, NULL, ?, ?, NULL)
            """,
            (
                guild_id,
                recipe_id,
                channel_id,
                responsible_id,
                responsible_display,
                created_by_id,
                created_by_display,
                int(recipe.get("version") or 1),
                str(recipe["product_name"]),
                int(recipe["treasury_cost_per_unit"]),
                int(recipe["duration_minutes_per_unit"]),
                int(recipe["max_batch_size"]),
                int(attempts_total),
                now,
                now,
            ),
        )
        plan_id = int(cur.lastrowid)
        con.executemany(
            """
            INSERT INTO craft_plan_materials(
                plan_id, recipe_material_id, material_name, quantity_per_unit,
                required_total, stock_quantity, purchased_quantity, spent_total, updated_at
            )
            VALUES(?, ?, ?, ?, ?, 0, 0, 0, ?)
            """,
            [
                (
                    plan_id,
                    int(material["id"]),
                    str(material["material_name"]),
                    int(material["quantity_per_unit"]),
                    material_totals[index],
                    now,
                )
                for index, material in enumerate(recipe["materials"])
            ],
        )
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="plan_created",
            actor_id=created_by_id,
            actor_display=created_by_display,
            details={
                "product_name": recipe["product_name"],
                "attempts_total": int(attempts_total),
                "responsible_id": responsible_id,
                "responsible_display": responsible_display,
            },
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=created_by_id,
            actor_display=created_by_display,
            module="craft",
            action_kind="plan_created",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Создан план крафта #{plan_id}: {recipe['product_name']}",
            payload={"plan_id": plan_id, "craft_event_id": event_id},
            now=now,
        )
        plan = _craft_plan_from_con(con, plan_id)
        con.commit()
    if plan is None:
        raise RuntimeError("craft_plan_create_failed")
    plan["action_id"] = action_id
    return plan


def craft_bind_plan_message(*, plan_id: int, channel_id: int, message_id: int, thread_id: int) -> dict[str, Any] | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE craft_plans
            SET channel_id = ?, message_id = ?, thread_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (channel_id, message_id, thread_id, now, plan_id),
        )
        plan = _craft_plan_from_con(con, plan_id)
        con.commit()
    return plan


def craft_delete_unbound_plan(plan_id: int) -> bool:
    """Remove a plan that Discord could not bind to a public message/thread."""
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            """
            SELECT id FROM craft_plans
            WHERE id = ? AND message_id IS NULL AND thread_id IS NULL
              AND stage = 'procurement' AND attempts_queued = 0 AND attempts_completed = 0
              AND NOT EXISTS (SELECT 1 FROM craft_purchases WHERE plan_id = craft_plans.id)
            """,
            (plan_id,),
        ).fetchone()
        if plan is None:
            con.rollback()
            return False
        con.execute("DELETE FROM craft_events WHERE plan_id = ?", (plan_id,))
        con.execute("DELETE FROM craft_plan_materials WHERE plan_id = ?", (plan_id,))
        con.execute("DELETE FROM bot_actions WHERE target_type = 'craft_plan' AND target_id = ?", (str(plan_id),))
        con.execute("DELETE FROM craft_plans WHERE id = ?", (plan_id,))
        con.commit()
        return True


def craft_get_plan(plan_id: int, guild_id: int | None = None) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        plan = _craft_plan_from_con(con, plan_id)
    if plan is not None and guild_id is not None and int(plan["guild_id"]) != guild_id:
        return None
    return plan


def craft_active_plans(guild_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT id FROM craft_plans
            WHERE guild_id = ? AND stage NOT IN ('completed', 'cancelled')
            ORDER BY id DESC LIMIT ?
            """,
            (guild_id, max(1, min(int(limit), 100))),
        ).fetchall()
        return [plan for row in rows if (plan := _craft_plan_from_con(con, int(row["id"]))) is not None]


def craft_recent_plans(guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            "SELECT id FROM craft_plans WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
            (guild_id, max(1, min(int(limit), 50))),
        ).fetchall()
        return [plan for row in rows if (plan := _craft_plan_from_con(con, int(row["id"]))) is not None]


def craft_add_purchase(
    *,
    guild_id: int,
    plan_id: int,
    plan_material_id: int,
    quantity: int,
    total_cost: int,
    finance_code: str | None,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    if quantity <= 0 or total_cost < 0:
        raise ValueError("craft_bad_purchase")
    now = utc_now_iso()
    clean_code = re.sub(r"[^A-Za-z]", "", str(finance_code or "")).upper() or None
    if clean_code is not None and len(clean_code) != 4:
        raise ValueError("craft_bad_finance_code")

    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            "SELECT * FROM craft_plans WHERE id = ? AND guild_id = ?",
            (plan_id, guild_id),
        ).fetchone()
        if plan is None:
            con.rollback()
            raise ValueError("craft_plan_not_found")
        if str(plan["stage"]) not in {"procurement", "crafting"}:
            con.rollback()
            raise ValueError("craft_not_procurement")
        material = con.execute(
            "SELECT * FROM craft_plan_materials WHERE id = ? AND plan_id = ?",
            (plan_material_id, plan_id),
        ).fetchone()
        if material is None:
            con.rollback()
            raise ValueError("craft_material_not_found")
        remaining_attempts = max(0, int(plan["attempts_total"]) - int(plan["attempts_queued"]))
        target_stock = int(material["quantity_per_unit"]) * remaining_attempts

        finance_event_id = None
        if clean_code is not None:
            finance_event = con.execute(
                """
                SELECT e.* FROM finance_events AS e
                WHERE e.guild_id = ? AND e.event_kind = 'withdraw' AND e.game_code = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM finance_events AS u WHERE u.reversed_event_id = e.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM craft_purchases AS p WHERE p.finance_event_id = e.id AND p.undone_at IS NULL
                  )
                ORDER BY e.id DESC LIMIT 1
                """,
                (guild_id, clean_code),
            ).fetchone()
            if finance_event is None:
                con.rollback()
                raise ValueError("craft_finance_code_not_found")
            if int(finance_event["amount"]) != int(total_cost):
                con.rollback()
                raise ValueError("craft_finance_amount_mismatch")
            finance_event_id = int(finance_event["id"])

        cur = con.execute(
            """
            INSERT INTO craft_purchases(
                plan_id, plan_material_id, actor_id, actor_display,
                quantity, total_cost, finance_event_id, finance_code, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                plan_material_id,
                actor_id,
                actor_display,
                int(quantity),
                int(total_cost),
                finance_event_id,
                clean_code,
                now,
            ),
        )
        purchase_id = int(cur.lastrowid)
        con.execute(
            """
            UPDATE craft_plan_materials
            SET stock_quantity = stock_quantity + ?,
                purchased_quantity = purchased_quantity + ?,
                spent_total = spent_total + ?,
                updated_at = ?
            WHERE id = ?
            """,
            (int(quantity), int(quantity), int(total_cost), now, plan_material_id),
        )
        updated_material = con.execute(
            "SELECT * FROM craft_plan_materials WHERE id = ?",
            (plan_material_id,),
        ).fetchone()
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="purchase",
            actor_id=actor_id,
            actor_display=actor_display,
            details={
                "purchase_id": purchase_id,
                "material_name": str(material["material_name"]),
                "quantity": int(quantity),
                "total_cost": int(total_cost),
                "average_unit_price": (float(total_cost) / int(quantity)),
                "stock_after": int(updated_material["stock_quantity"]),
                "required_total": int(updated_material["required_total"]),
                "required_for_unqueued": target_stock,
                "remaining": max(0, target_stock - int(updated_material["stock_quantity"])),
                "finance_code": clean_code,
                "finance_event_id": finance_event_id,
            },
            now=now,
        )

        material_rows = con.execute(
            "SELECT quantity_per_unit, stock_quantity FROM craft_plan_materials WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
        materials_ready = all(
            int(row["stock_quantity"]) >= int(row["quantity_per_unit"]) * remaining_attempts
            for row in material_rows
        )
        stage_changed = str(plan["stage"]) == "procurement" and materials_ready
        if stage_changed:
            con.execute(
                "UPDATE craft_plans SET stage = 'crafting', updated_at = ? WHERE id = ?",
                (now, plan_id),
            )
            _craft_add_event(
                con,
                plan_id=plan_id,
                guild_id=guild_id,
                event_kind="stage_crafting",
                actor_id=None,
                actor_display=None,
                details={"message": "Все материалы для оставшихся циклов собраны. Можно начинать крафт."},
                now=now,
            )
        else:
            con.execute("UPDATE craft_plans SET updated_at = ? WHERE id = ?", (now, plan_id))
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="craft_purchase",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Закупка для плана #{plan_id}: {material['material_name']} × {int(quantity)}",
            payload={
                "plan_id": plan_id,
                "purchase_id": purchase_id,
                "craft_event_id": event_id,
                "stage_before": str(plan["stage"]),
            },
            now=now,
        )
        result = _craft_plan_from_con(con, plan_id)
        con.commit()
    return {"plan": result, "purchase_id": purchase_id, "stage_changed": stage_changed, "action_id": action_id}


def craft_inventory_check(
    *,
    guild_id: int,
    plan_id: int,
    material_quantities: dict[int, int],
    product_quantity: int,
    note: str | None,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    if product_quantity < 0 or any(int(value) < 0 for value in material_quantities.values()):
        raise ValueError("craft_bad_inventory")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            "SELECT * FROM craft_plans WHERE id = ? AND guild_id = ?",
            (plan_id, guild_id),
        ).fetchone()
        if plan is None or str(plan["stage"]) not in CRAFT_ACTIVE_STAGES:
            con.rollback()
            raise ValueError("craft_plan_not_active")
        materials = con.execute(
            "SELECT * FROM craft_plan_materials WHERE plan_id = ? ORDER BY id",
            (plan_id,),
        ).fetchall()
        expected_ids = {int(row["id"]) for row in materials}
        if set(material_quantities) != expected_ids:
            con.rollback()
            raise ValueError("craft_inventory_incomplete")
        previous_materials = {str(row["id"]): int(row["stock_quantity"]) for row in materials}
        for material in materials:
            material_id = int(material["id"])
            con.execute(
                "UPDATE craft_plan_materials SET stock_quantity = ?, updated_at = ? WHERE id = ?",
                (int(material_quantities[material_id]), now, material_id),
            )
        con.execute(
            "UPDATE craft_plans SET product_stock = ?, updated_at = ? WHERE id = ?",
            (int(product_quantity), now, plan_id),
        )
        material_snapshot = {
            str(row["material_name"]): int(material_quantities[int(row["id"])]) for row in materials
        }
        check_cur = con.execute(
            """
            INSERT INTO craft_inventory_checks(
                plan_id, actor_id, actor_display, materials_json,
                product_quantity, note, previous_materials_json,
                previous_product_quantity, previous_stage, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                actor_id,
                actor_display,
                json.dumps(material_snapshot, ensure_ascii=False),
                int(product_quantity),
                str(note or "").strip() or None,
                json.dumps(previous_materials, ensure_ascii=False),
                int(plan["product_stock"] or 0),
                str(plan["stage"]),
                now,
            ),
        )
        check_id = int(check_cur.lastrowid)
        stage_changed = False
        if str(plan["stage"]) == "procurement":
            missing = con.execute(
                "SELECT COUNT(*) AS n FROM craft_plan_materials WHERE plan_id = ? AND stock_quantity < required_total",
                (plan_id,),
            ).fetchone()
            if int(missing["n"] or 0) == 0:
                con.execute("UPDATE craft_plans SET stage = 'crafting' WHERE id = ?", (plan_id,))
                stage_changed = True
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="inventory_check",
            actor_id=actor_id,
            actor_display=actor_display,
            details={
                "materials": material_snapshot,
                "product_quantity": int(product_quantity),
                "note": str(note or "").strip() or None,
                "stage_changed": stage_changed,
            },
            now=now,
        )
        if stage_changed:
            _craft_add_event(
                con,
                plan_id=plan_id,
                guild_id=guild_id,
                event_kind="stage_crafting",
                actor_id=None,
                actor_display=None,
                details={"message": "Сверка подтвердила наличие всех материалов. Можно начинать крафт."},
                now=now,
            )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="craft_inventory_check",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Сверка склада плана #{plan_id}",
            payload={"plan_id": plan_id, "inventory_check_id": check_id, "craft_event_id": event_id},
            now=now,
        )
        result = _craft_plan_from_con(con, plan_id)
        con.commit()
    return {"plan": result, "stage_changed": stage_changed, "action_id": action_id}


def craft_start_batch(
    *,
    guild_id: int,
    plan_id: int,
    quantity: int,
    actor_id: int,
    actor_display: str | None,
    log_channel_id: int,
    admin_user_id: int,
) -> dict[str, Any]:
    if quantity <= 0:
        raise ValueError("craft_bad_batch_quantity")
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            "SELECT * FROM craft_plans WHERE id = ? AND guild_id = ?",
            (plan_id, guild_id),
        ).fetchone()
        if plan is None:
            con.rollback()
            raise ValueError("craft_plan_not_found")
        stage_before = str(plan["stage"])
        if stage_before not in {"procurement", "crafting"}:
            con.rollback()
            raise ValueError("craft_not_crafting")
        active = con.execute(
            "SELECT id FROM craft_batches WHERE plan_id = ? AND status = 'active' LIMIT 1",
            (plan_id,),
        ).fetchone()
        if active is not None:
            con.rollback()
            raise ValueError("craft_batch_active")
        recipe = {
            "product_name": str(plan["product_name_snapshot"] or "Неизвестный продукт"),
            "treasury_cost_per_unit": int(plan["treasury_cost_per_unit_snapshot"] or 0),
            "duration_minutes_per_unit": int(plan["duration_minutes_per_unit_snapshot"] or 1),
            "max_batch_size": int(plan["max_batch_size_snapshot"] or 1),
        }
        remaining = int(plan["attempts_total"]) - int(plan["attempts_queued"])
        if quantity > remaining or quantity > int(recipe["max_batch_size"]):
            con.rollback()
            raise ValueError("craft_batch_too_large")
        materials = con.execute(
            "SELECT * FROM craft_plan_materials WHERE plan_id = ? ORDER BY id",
            (plan_id,),
        ).fetchall()
        missing_names = []
        for material in materials:
            needed = int(material["quantity_per_unit"]) * int(quantity)
            if int(material["stock_quantity"]) < needed:
                missing_names.append(str(material["material_name"]))
        if missing_names:
            con.rollback()
            raise ValueError("craft_batch_materials_missing:" + ", ".join(missing_names))

        treasury_cost = int(recipe["treasury_cost_per_unit"]) * int(quantity)
        finance_event_id = None
        if treasury_cost > 0:
            latest = con.execute(
                "SELECT balance_after FROM finance_events WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
                (guild_id,),
            ).fetchone()
            balance_before = None if latest is None or latest["balance_after"] is None else int(latest["balance_after"])
            if balance_before is None:
                con.rollback()
                raise ValueError("craft_finance_balance_unknown")
            if balance_before < treasury_cost:
                con.rollback()
                raise ValueError("craft_finance_insufficient")
            balance_after = balance_before - treasury_cost
            finance_cur = con.execute(
                """
                INSERT INTO finance_events(
                    guild_id, event_kind, report_date, prompt_id, reversed_event_id,
                    amount, delta, balance_before, balance_after, reason,
                    captcha_digest, game_code, actor_id, actor_display,
                    channel_id, message_id, created_at
                )
                VALUES(?, 'withdraw', NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    guild_id,
                    treasury_cost,
                    -treasury_cost,
                    balance_before,
                    balance_after,
                    f"Автоматический расход на крафт: {recipe['product_name']} × {quantity} (план #{plan_id})",
                    f"craft:{plan_id}",
                    None,
                    actor_id,
                    actor_display,
                    int(plan["channel_id"]),
                    now,
                ),
            )
            finance_event_id = int(finance_cur.lastrowid)
            _finance_enqueue_notifications(
                con,
                event_id=finance_event_id,
                log_channel_id=log_channel_id,
                admin_user_id=admin_user_id,
                now=now,
            )

        due_dt = now_dt + timedelta(minutes=int(recipe["duration_minutes_per_unit"]) * int(quantity))
        for material in materials:
            needed = int(material["quantity_per_unit"]) * int(quantity)
            con.execute(
                "UPDATE craft_plan_materials SET stock_quantity = stock_quantity - ?, updated_at = ? WHERE id = ?",
                (needed, now, int(material["id"])),
            )
        batch_cur = con.execute(
            """
            INSERT INTO craft_batches(
                plan_id, quantity, started_by_id, started_by_display,
                started_at, due_at, status, completed_at,
                finance_event_id, finance_code
            )
            VALUES(?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?)
            """,
            (
                plan_id,
                int(quantity),
                actor_id,
                actor_display,
                now,
                due_dt.isoformat(),
                finance_event_id,
                None,
            ),
        )
        batch_id = int(batch_cur.lastrowid)
        con.execute(
            """
            UPDATE craft_plans
            SET stage = 'crafting', attempts_queued = attempts_queued + ?, updated_at = ?
            WHERE id = ?
            """,
            (int(quantity), now, plan_id),
        )
        if stage_before == "procurement":
            _craft_add_event(
                con,
                plan_id=plan_id,
                guild_id=guild_id,
                event_kind="stage_crafting",
                actor_id=actor_id,
                actor_display=actor_display,
                details={"message": "Материалов достаточно для выбранного цикла. Производство начато."},
                now=now,
            )
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="batch_started",
            actor_id=actor_id,
            actor_display=actor_display,
            details={
                "batch_id": batch_id,
                "quantity": int(quantity),
                "started_at": now,
                "due_at": due_dt.isoformat(),
                "duration_minutes": int(recipe["duration_minutes_per_unit"]) * int(quantity),
                "treasury_cost": treasury_cost,
                "finance_event_id": finance_event_id,
                "finance_code": None,
            },
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="craft_batch_started",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Запущен цикл плана #{plan_id}: {int(quantity)} шт.",
            payload={
                "plan_id": plan_id,
                "batch_id": batch_id,
                "craft_event_id": event_id,
                "finance_event_id": finance_event_id,
                "log_channel_id": log_channel_id,
                "admin_user_id": admin_user_id,
                "stage_before": stage_before,
            },
            now=now,
        )
        result = _craft_plan_from_con(con, plan_id)
        batch = con.execute("SELECT * FROM craft_batches WHERE id = ?", (batch_id,)).fetchone()
        con.commit()
    return {
        "plan": result,
        "batch": _finance_row(batch),
        "finance_event_id": finance_event_id,
        "action_id": action_id,
    }


def craft_complete_due_batches(now_iso: str | None = None) -> list[int]:
    now = now_iso or utc_now_iso()
    changed: list[int] = []
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute(
            "SELECT * FROM craft_batches WHERE status = 'active' AND due_at <= ? ORDER BY id",
            (now,),
        ).fetchall()
        for batch in rows:
            plan = con.execute("SELECT * FROM craft_plans WHERE id = ?", (int(batch["plan_id"]),)).fetchone()
            if plan is None or str(plan["stage"]) != "crafting":
                con.execute(
                    "UPDATE craft_batches SET status = 'completed', completed_at = ? WHERE id = ?",
                    (now, int(batch["id"])),
                )
                continue
            quantity = int(batch["quantity"])
            con.execute(
                "UPDATE craft_batches SET status = 'completed', completed_at = ? WHERE id = ?",
                (now, int(batch["id"])),
            )
            con.execute(
                """
                UPDATE craft_plans
                SET attempts_completed = attempts_completed + ?,
                    product_stock = product_stock + ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (quantity, quantity, now, int(plan["id"])),
            )
            _craft_add_event(
                con,
                plan_id=int(plan["id"]),
                guild_id=int(plan["guild_id"]),
                event_kind="batch_completed",
                actor_id=None,
                actor_display=None,
                details={
                    "batch_id": int(batch["id"]),
                    "quantity": quantity,
                    "due_at": str(batch["due_at"]),
                    "completed_at": now,
                },
                now=now,
            )
            updated = con.execute("SELECT * FROM craft_plans WHERE id = ?", (int(plan["id"]),)).fetchone()
            if int(updated["attempts_completed"]) >= int(updated["attempts_total"]):
                con.execute(
                    "UPDATE craft_plans SET stage = 'awaiting_output', updated_at = ? WHERE id = ?",
                    (now, int(plan["id"])),
                )
                _craft_add_event(
                    con,
                    plan_id=int(plan["id"]),
                    guild_id=int(plan["guild_id"]),
                    event_kind="stage_awaiting_output",
                    actor_id=None,
                    actor_display=None,
                    details={"message": "Все попытки завершены. Требуется итоговая сверка продукта."},
                    now=now,
                )
            changed.append(int(plan["id"]))
        con.commit()
    return sorted(set(changed))


def craft_reminder_candidates(guild_id: int) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT p.*, b.id AS last_batch_id, b.due_at AS last_due_at
            FROM craft_plans AS p
            JOIN craft_batches AS b ON b.id = (
                SELECT MAX(id) FROM craft_batches WHERE plan_id = p.id AND status = 'completed'
            )
            WHERE p.guild_id = ? AND p.stage = 'crafting'
              AND p.attempts_completed < p.attempts_total
              AND NOT EXISTS (
                  SELECT 1 FROM craft_batches AS active
                  WHERE active.plan_id = p.id AND active.status = 'active'
              )
            ORDER BY p.id
            """,
            (guild_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def craft_claim_reminder(plan_id: int, reminder_key: str) -> bool:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            INSERT INTO craft_reminders(
                plan_id, reminder_key, message_id, created_at, deleted_at
            ) VALUES(?, ?, NULL, ?, NULL)
            ON CONFLICT(plan_id, reminder_key) DO NOTHING
            """,
            (plan_id, reminder_key, now),
        )
        con.commit()
        return int(cursor.rowcount or 0) > 0


def craft_set_reminder_message(plan_id: int, reminder_key: str, message_id: int) -> None:
    with _db_lock, connect() as con:
        con.execute(
            "UPDATE craft_reminders SET message_id = ? WHERE plan_id = ? AND reminder_key = ?",
            (message_id, plan_id, reminder_key),
        )
        con.commit()


def craft_release_reminder(plan_id: int, reminder_key: str) -> None:
    with _db_lock, connect() as con:
        con.execute(
            "DELETE FROM craft_reminders WHERE plan_id = ? AND reminder_key = ? AND message_id IS NULL",
            (plan_id, reminder_key),
        )
        con.commit()


def craft_open_reminder_messages(plan_id: int) -> list[int]:
    with _db_lock, connect() as con:
        rows = con.execute(
            "SELECT message_id FROM craft_reminders WHERE plan_id = ? AND message_id IS NOT NULL AND deleted_at IS NULL",
            (plan_id,),
        ).fetchall()
    return [int(row["message_id"]) for row in rows]


def craft_mark_reminders_deleted(plan_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            "UPDATE craft_reminders SET deleted_at = ? WHERE plan_id = ? AND deleted_at IS NULL",
            (now, plan_id),
        )
        con.commit()


def craft_mark_reminder_deleted(plan_id: int, message_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE craft_reminders SET deleted_at = ?
            WHERE plan_id = ? AND message_id = ? AND deleted_at IS NULL
            """,
            (now, plan_id, message_id),
        )
        con.commit()


def craft_pending_events(limit: int = 50) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT e.*, p.thread_id, p.channel_id
            FROM craft_events AS e
            JOIN craft_plans AS p ON p.id = e.plan_id
            WHERE e.thread_message_id IS NULL AND p.thread_id IS NOT NULL
            ORDER BY e.id ASC LIMIT ?
            """,
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    return [dict(row) for row in rows]


def craft_mark_event_sent(event_id: int, thread_message_id: int) -> None:
    with _db_lock, connect() as con:
        con.execute(
            "UPDATE craft_events SET thread_message_id = ? WHERE id = ?",
            (thread_message_id, event_id),
        )
        con.commit()


def craft_set_final_output(
    *,
    guild_id: int,
    plan_id: int,
    product_quantity: int,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    if product_quantity < 0:
        raise ValueError("craft_bad_output")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            "SELECT * FROM craft_plans WHERE id = ? AND guild_id = ?",
            (plan_id, guild_id),
        ).fetchone()
        if plan is None or str(plan["stage"]) != "awaiting_output":
            con.rollback()
            raise ValueError("craft_not_awaiting_output")
        if product_quantity > int(plan["attempts_completed"]):
            con.rollback()
            raise ValueError("craft_output_too_large")
        if product_quantity == 0:
            stage = "completed"
            completed_at = now
        else:
            stage = "listing"
            completed_at = None
        con.execute(
            """
            UPDATE craft_plans
            SET final_product_qty = ?, product_stock = ?, stage = ?,
                completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (int(product_quantity), int(product_quantity), stage, completed_at, now, plan_id),
        )
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="final_output",
            actor_id=actor_id,
            actor_display=actor_display,
            details={
                "product_quantity": int(product_quantity),
                "attempts_completed": int(plan["attempts_completed"]),
                "next_stage": stage,
            },
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="craft_final_output",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Указан итог плана #{plan_id}: {int(product_quantity)} шт.",
            payload={
                "plan_id": plan_id,
                "craft_event_id": event_id,
                "previous_product_stock": int(plan["product_stock"] or 0),
                "previous_final_product_qty": plan["final_product_qty"],
                "previous_stage": str(plan["stage"]),
                "previous_completed_at": plan["completed_at"],
            },
            now=now,
        )
        result = _craft_plan_from_con(con, plan_id)
        con.commit()
    result["action_id"] = action_id
    return result


def craft_set_estimated_price(
    *,
    guild_id: int,
    plan_id: int,
    unit_price: int,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    if unit_price <= 0:
        raise ValueError("craft_bad_price")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            "SELECT * FROM craft_plans WHERE id = ? AND guild_id = ?",
            (plan_id, guild_id),
        ).fetchone()
        if plan is None or str(plan["stage"]) not in {"listing", "selling"}:
            con.rollback()
            raise ValueError("craft_not_listing")
        con.execute(
            "UPDATE craft_plans SET estimated_unit_price = ?, updated_at = ? WHERE id = ?",
            (int(unit_price), now, plan_id),
        )
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="estimated_price",
            actor_id=actor_id,
            actor_display=actor_display,
            details={"unit_price": int(unit_price)},
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="craft_estimated_price",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Изменена примерная цена плана #{plan_id}: {int(unit_price)}",
            payload={
                "plan_id": plan_id,
                "craft_event_id": event_id,
                "previous_price": plan["estimated_unit_price"],
            },
            now=now,
        )
        result = _craft_plan_from_con(con, plan_id)
        con.commit()
    result["action_id"] = action_id
    return result


def craft_add_market_listing(
    *,
    guild_id: int,
    plan_id: int,
    quantity: int,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    if quantity <= 0:
        raise ValueError("craft_bad_listing")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            "SELECT * FROM craft_plans WHERE id = ? AND guild_id = ?",
            (plan_id, guild_id),
        ).fetchone()
        if plan is None or str(plan["stage"]) != "listing":
            con.rollback()
            raise ValueError("craft_not_listing")
        if plan["estimated_unit_price"] is None:
            con.rollback()
            raise ValueError("craft_price_required")
        remaining = int(plan["final_product_qty"] or 0) - int(plan["market_listed_qty"])
        if quantity > remaining:
            con.rollback()
            raise ValueError("craft_listing_too_large")
        listing_cur = con.execute(
            """
            INSERT INTO craft_market_listings(
                plan_id, actor_id, actor_display, quantity,
                estimated_unit_price, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (plan_id, actor_id, actor_display, int(quantity), plan["estimated_unit_price"], now),
        )
        listing_id = int(listing_cur.lastrowid)
        new_total = int(plan["market_listed_qty"]) + int(quantity)
        new_stage = "selling" if new_total >= int(plan["final_product_qty"] or 0) else "listing"
        con.execute(
            "UPDATE craft_plans SET market_listed_qty = ?, stage = ?, updated_at = ? WHERE id = ?",
            (new_total, new_stage, now, plan_id),
        )
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="market_listing",
            actor_id=actor_id,
            actor_display=actor_display,
            details={
                "quantity": int(quantity),
                "listed_total": new_total,
                "remaining_to_list": max(0, int(plan["final_product_qty"] or 0) - new_total),
                "estimated_unit_price": plan["estimated_unit_price"],
                "next_stage": new_stage,
            },
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="craft_market_listing",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Выставлено на маркет по плану #{plan_id}: {int(quantity)} шт.",
            payload={
                "plan_id": plan_id,
                "listing_id": listing_id,
                "craft_event_id": event_id,
                "previous_stage": str(plan["stage"]),
            },
            now=now,
        )
        result = _craft_plan_from_con(con, plan_id)
        con.commit()
    result["action_id"] = action_id
    return result


def craft_add_sale(
    *,
    guild_id: int,
    plan_id: int,
    quantity: int,
    total_amount: int,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    if quantity <= 0 or total_amount <= 0:
        raise ValueError("craft_bad_sale")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            "SELECT * FROM craft_plans WHERE id = ? AND guild_id = ?",
            (plan_id, guild_id),
        ).fetchone()
        if plan is None or str(plan["stage"]) != "selling":
            con.rollback()
            raise ValueError("craft_not_selling")
        remaining = int(plan["final_product_qty"] or 0) - int(plan["sold_qty"])
        if quantity > remaining:
            con.rollback()
            raise ValueError("craft_sale_too_large")
        sale_cur = con.execute(
            """
            INSERT INTO craft_sales(
                plan_id, actor_id, actor_display, quantity, total_amount, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (plan_id, actor_id, actor_display, int(quantity), int(total_amount), now),
        )
        sale_id = int(sale_cur.lastrowid)
        sold_total = int(plan["sold_qty"]) + int(quantity)
        revenue_total = int(plan["total_revenue"]) + int(total_amount)
        product_stock = max(0, int(plan["final_product_qty"] or 0) - sold_total)
        completed = sold_total >= int(plan["final_product_qty"] or 0)
        new_stage = "completed" if completed else "selling"
        con.execute(
            """
            UPDATE craft_plans
            SET sold_qty = ?, total_revenue = ?, product_stock = ?, stage = ?,
                completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (sold_total, revenue_total, product_stock, new_stage, now if completed else None, now, plan_id),
        )
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="sale",
            actor_id=actor_id,
            actor_display=actor_display,
            details={
                "quantity": int(quantity),
                "total_amount": int(total_amount),
                "average_unit_price": float(total_amount) / int(quantity),
                "sold_total": sold_total,
                "remaining_to_sell": max(0, int(plan["final_product_qty"] or 0) - sold_total),
                "revenue_total": revenue_total,
                "completed": completed,
            },
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="craft_sale",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Продажа по плану #{plan_id}: {int(quantity)} шт. за {int(total_amount)}",
            payload={
                "plan_id": plan_id,
                "sale_id": sale_id,
                "craft_event_id": event_id,
                "previous_stage": str(plan["stage"]),
                "previous_completed_at": plan["completed_at"],
            },
            now=now,
        )
        result = _craft_plan_from_con(con, plan_id)
        con.commit()
    result["action_id"] = action_id
    return result


def craft_completion_candidates(guild_id: int) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT id FROM craft_plans
            WHERE guild_id = ? AND stage = 'completed' AND completion_message_id IS NULL
            ORDER BY id
            """,
            (guild_id,),
        ).fetchall()
        return [plan for row in rows if (plan := _craft_plan_from_con(con, int(row["id"]))) is not None]


def craft_set_completion_message(plan_id: int, message_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            "UPDATE craft_plans SET completion_message_id = ?, updated_at = ? WHERE id = ?",
            (message_id, now, plan_id),
        )
        con.commit()


def craft_stats(guild_id: int, days: int = 30) -> dict[str, Any]:
    period_days = max(1, min(int(days), 3650))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)).isoformat()
    with connect_readonly() as con:
        plans = con.execute(
            "SELECT * FROM craft_plans WHERE guild_id = ? AND created_at >= ? ORDER BY id",
            (guild_id, cutoff),
        ).fetchall()
        plan_ids = [int(row["id"]) for row in plans]
        active_count = sum(1 for row in plans if str(row["stage"]) in CRAFT_ACTIVE_STAGES)
        completed_count = sum(1 for row in plans if str(row["stage"]) == "completed")
        cancelled_count = sum(1 for row in plans if str(row["stage"]) == "cancelled")
        attempts_planned = sum(int(row["attempts_total"] or 0) for row in plans)
        attempts_completed = sum(int(row["attempts_completed"] or 0) for row in plans)
        output_total = sum(int(row["final_product_qty"] or 0) for row in plans)
        sold_total = sum(int(row["sold_qty"] or 0) for row in plans)
        revenue = sum(int(row["total_revenue"] or 0) for row in plans)

        if plan_ids:
            placeholders = ",".join("?" for _ in plan_ids)
            purchases = int(
                con.execute(
                    f"SELECT COALESCE(SUM(total_cost), 0) AS n FROM craft_purchases WHERE undone_at IS NULL AND plan_id IN ({placeholders})",
                    plan_ids,
                ).fetchone()["n"]
                or 0
            )
            batch_row = con.execute(
                f"""
                SELECT COUNT(*) AS batches, COALESCE(SUM(quantity), 0) AS units,
                       AVG(CASE WHEN completed_at IS NOT NULL
                           THEN MAX(0, (julianday(completed_at) - julianday(due_at)) * 86400)
                           ELSE NULL END) AS average_delay_seconds
                FROM craft_batches
                WHERE undone_at IS NULL AND plan_id IN ({placeholders})
                """,
                plan_ids,
            ).fetchone()
            craft_fees = int(
                con.execute(
                    f"""
                    SELECT COALESCE(SUM(e.amount), 0) AS n
                    FROM finance_events e
                    JOIN craft_plans p ON e.captcha_digest = ('craft:' || p.id)
                    WHERE p.id IN ({placeholders})
                      AND e.event_kind = 'withdraw'
                      AND NOT EXISTS (SELECT 1 FROM finance_events u WHERE u.reversed_event_id = e.id)
                    """,
                    plan_ids,
                ).fetchone()["n"]
                or 0
            )
            contributor_rows = con.execute(
                f"""
                SELECT actor_id, MAX(actor_display) AS actor_display,
                       SUM(purchases) AS purchases, SUM(batches) AS batches, SUM(sales) AS sales
                FROM (
                    SELECT actor_id, MAX(actor_display) AS actor_display,
                           COUNT(*) AS purchases, 0 AS batches, 0 AS sales
                    FROM craft_purchases WHERE undone_at IS NULL AND plan_id IN ({placeholders}) GROUP BY actor_id
                    UNION ALL
                    SELECT started_by_id, MAX(started_by_display), 0, COUNT(*), 0
                    FROM craft_batches WHERE undone_at IS NULL AND plan_id IN ({placeholders}) GROUP BY started_by_id
                    UNION ALL
                    SELECT actor_id, MAX(actor_display), 0, 0, COUNT(*)
                    FROM craft_sales WHERE undone_at IS NULL AND plan_id IN ({placeholders}) GROUP BY actor_id
                ) x
                GROUP BY actor_id
                ORDER BY (SUM(purchases) + SUM(batches) + SUM(sales)) DESC, actor_id
                LIMIT 8
                """,
                [*plan_ids, *plan_ids, *plan_ids],
            ).fetchall()
        else:
            purchases = 0
            craft_fees = 0
            batch_row = {"batches": 0, "units": 0, "average_delay_seconds": None}
            contributor_rows = []

        recipe_rows = con.execute(
            """
            SELECT product_name_snapshot AS product_name, COUNT(*) AS plans,
                   SUM(attempts_completed) AS attempts_completed,
                   SUM(COALESCE(final_product_qty, 0)) AS output,
                   SUM(total_revenue) AS revenue
            FROM craft_plans
            WHERE guild_id = ? AND created_at >= ? AND stage != 'cancelled'
            GROUP BY product_name_snapshot
            ORDER BY revenue DESC, attempts_completed DESC LIMIT 8
            """,
            (guild_id, cutoff),
        ).fetchall()
    success_rate = (output_total / attempts_completed * 100) if attempts_completed else 0.0
    return {
        "days": period_days,
        "cutoff": cutoff,
        "plan_count": len(plans),
        "active_count": active_count,
        "completed_count": completed_count,
        "cancelled_count": cancelled_count,
        "attempts_planned": attempts_planned,
        "attempts_completed": attempts_completed,
        "output_total": output_total,
        "success_rate": success_rate,
        "sold_total": sold_total,
        "revenue": revenue,
        "purchase_cost": purchases,
        "craft_fees": craft_fees,
        "profit": revenue - purchases - craft_fees,
        "batch_count": int(batch_row["batches"] or 0),
        "batch_units": int(batch_row["units"] or 0),
        "average_delay_seconds": (
            None if batch_row["average_delay_seconds"] is None else float(batch_row["average_delay_seconds"])
        ),
        "recipes": [dict(row) for row in recipe_rows],
        "contributors": [dict(row) for row in contributor_rows],
    }

__all__ = ['CRAFT_ACTIVE_STAGES', '_craft_add_event', '_craft_recipe_from_con', 'craft_create_recipe', 'craft_list_recipes', 'craft_get_recipe', 'craft_recipe_versions', '_normalize_craft_recipe_values', 'craft_update_recipe', 'craft_set_recipe_active', 'craft_clone_recipe', '_craft_plan_from_con', 'craft_create_plan', 'craft_bind_plan_message', 'craft_delete_unbound_plan', 'craft_get_plan', 'craft_active_plans', 'craft_recent_plans', 'craft_add_purchase', 'craft_inventory_check', 'craft_start_batch', 'craft_complete_due_batches', 'craft_reminder_candidates', 'craft_claim_reminder', 'craft_set_reminder_message', 'craft_release_reminder', 'craft_open_reminder_messages', 'craft_mark_reminders_deleted', 'craft_mark_reminder_deleted', 'craft_pending_events', 'craft_mark_event_sent', 'craft_set_final_output', 'craft_set_estimated_price', 'craft_add_market_listing', 'craft_add_sale', 'craft_completion_candidates', 'craft_set_completion_message', 'craft_stats']
