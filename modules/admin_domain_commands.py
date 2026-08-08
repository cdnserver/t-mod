"""Shared administrative command adapters for the Nuclear Reactor.

The web panel never mutates Discord state or SQLite ad hoc.  Each command is
translated into the same transactional repositories and runtime refresh ports
used by the Discord controls.
"""

from __future__ import annotations

import asyncio
from typing import Any

import discord

from modules.craft_runtime import refresh_craft_plan, wake_craft_worker
from modules.finance_config import FINANCE_ADMIN_USER_ID, FINANCE_EVENT_LOG_CHANNEL_ID
from modules.finance_runtime import wake_notification_worker
from persistence import craft_repository as craft_storage
from persistence import finance_repository as finance_storage
from persistence import tvrs_repository as tvrs_storage


CRAFT_ACTION_LABELS = {
    "purchase": "Закупка материала записана.",
    "start_batch": "Производственный цикл запущен.",
    "final_output": "Итог производства сохранён.",
    "estimated_price": "Ожидаемая цена сохранена.",
    "market_listing": "Выставление на маркет записано.",
    "sale": "Продажа записана.",
    "inventory": "Склад сверен.",
    "create_plan": "Производственный план создан.",
    "create_recipe": "Рецепт создан.",
    "update_recipe": "Новая версия рецепта сохранена.",
    "clone_recipe": "Копия рецепта создана.",
    "toggle_recipe": "Состояние рецепта изменено.",
}

CRAFT_ERROR_MESSAGES = {
    "craft_bad_purchase": "Количество должно быть положительным, а стоимость — неотрицательной.",
    "craft_bad_finance_code": "Финансовый код должен состоять из четырёх букв.",
    "craft_finance_code_not_found": "Подходящее снятие с этим финансовым кодом не найдено.",
    "craft_finance_amount_mismatch": "Сумма снятия не совпадает со стоимостью закупки.",
    "craft_plan_not_found": "Производственный план не найден.",
    "craft_plan_not_active": "Производственный план уже закрыт.",
    "craft_material_not_found": "Материал отсутствует в этом плане.",
    "craft_not_procurement": "Сейчас материалы этого плана пополнять нельзя.",
    "craft_not_crafting": "План не находится на стадии производства.",
    "craft_batch_active": "Предыдущий производственный цикл ещё не завершён.",
    "craft_batch_too_large": "Количество превышает остаток плана или размер одного цикла.",
    "craft_batch_materials_missing": "Для запуска цикла не хватает материалов.",
    "craft_finance_balance_unknown": "Сначала зафиксируйте актуальный остаток казны.",
    "craft_finance_insufficient": "В расчётной казне недостаточно средств.",
    "craft_not_awaiting_output": "План пока не ожидает итог производства.",
    "craft_output_too_large": "Итог не может превышать число завершённых попыток.",
    "craft_not_listing": "План не находится на стадии выставления.",
    "craft_price_required": "Сначала укажите ожидаемую цену за единицу.",
    "craft_listing_too_large": "Нельзя выставить больше оставшегося товара.",
    "craft_not_selling": "Сначала необходимо выставить весь товар.",
    "craft_sale_too_large": "Нельзя продать больше оставшегося товара.",
    "craft_inventory_incomplete": "Передайте остаток каждого материала плана.",
    "craft_recipe_not_found": "Рецепт не найден или отключён.",
    "craft_bad_product_name": "Название продукта пустое или слишком длинное.",
    "craft_bad_treasury_cost": "Расход казны на единицу не может быть отрицательным.",
    "craft_bad_duration": "Длительность единицы должна быть от 1 до 10080 минут.",
    "craft_bad_batch_size": "Размер одного цикла должен быть от 1 до 1000.",
    "craft_bad_material_count": "Добавьте от одного до двадцати материалов.",
    "craft_bad_material": "Проверьте названия и количество материалов.",
    "craft_recipe_exists": "Рецепт с таким названием уже существует.",
}

FINANCE_ERROR_MESSAGES = {
    "finance_bad_amount": "Сумма операции указана неверно.",
    "finance_bad_movement_kind": "Выберите поступление или расход.",
    "finance_reason_too_short": "Причина должна содержать не менее десяти символов.",
    "finance_bad_game_code": "Игровой код должен состоять из четырёх латинских букв.",
    "finance_action_locked_by_craft": "Операция связана с крафтом и отменяется только через аудит связанного действия.",
}

BILL_ERROR_MESSAGES = {
    "bill_locked_by_active_consensus": "Проект уже участвует в активном консенсусе и защищён от изменений.",
    "bill_delivery_in_progress": "По проекту ещё выполняется доставка сообщений. Повторите позже.",
    "bill_status_not_allowed": "Такой статус законопроекта недоступен.",
}


def _required_int(body: dict[str, Any], key: str, *, allow_zero: bool = False) -> int:
    try:
        value = int(body.get(key))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid_{key}") from exc
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"invalid_{key}")
    return value


def _recipe_materials(body: dict[str, Any]) -> list[tuple[str, int]]:
    materials = body.get("materials")
    if not isinstance(materials, list):
        raise ValueError("craft_bad_material")
    normalized: list[tuple[str, int]] = []
    for item in materials:
        if isinstance(item, dict):
            normalized.append(
                (
                    str(item.get("material_name") or ""),
                    int(item.get("quantity_per_unit") or 0),
                )
            )
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            normalized.append((str(item[0]), int(item[1])))
        else:
            raise ValueError("craft_bad_material")
    return normalized


async def _refresh_plan_projection(bot: discord.Client, plan_id: int) -> str | None:
    try:
        await refresh_craft_plan(bot, int(plan_id))
    except (discord.DiscordException, OSError, RuntimeError) as exc:
        return type(exc).__name__
    return None


async def execute_craft_command(
    bot: discord.Client,
    *,
    guild_id: int,
    actor_id: int,
    actor_display: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    action = str(body.get("action") or "").strip().lower()
    if action not in CRAFT_ACTION_LABELS:
        raise ValueError("craft_action_invalid")

    if action == "create_plan":
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            raise ValueError("guild_unavailable")
        from modules.craft import create_craft_plan

        plan = await create_craft_plan(
            bot,  # type: ignore[arg-type]
            guild,
            recipe_id=_required_int(body, "recipe_id"),
            attempts_total=_required_int(body, "attempts_total"),
            responsible_id=_required_int(body, "responsible_id"),
            created_by_id=int(actor_id),
            created_by_display=str(actor_display),
        )
        return {
            "message": CRAFT_ACTION_LABELS[action],
            "plan": plan,
            "projection_warning": None,
        }

    if action == "create_recipe":
        recipe = await asyncio.to_thread(
            craft_storage.craft_create_recipe,
            guild_id=int(guild_id),
            product_name=str(body.get("product_name") or ""),
            treasury_cost_per_unit=_required_int(
                body,
                "treasury_cost_per_unit",
                allow_zero=True,
            ),
            duration_minutes_per_unit=_required_int(
                body,
                "duration_minutes_per_unit",
            ),
            max_batch_size=_required_int(body, "max_batch_size"),
            materials=_recipe_materials(body),
            created_by_id=int(actor_id),
            created_by_display=str(actor_display),
        )
        return {"message": CRAFT_ACTION_LABELS[action], "recipe": recipe}

    if action == "update_recipe":
        recipe = await asyncio.to_thread(
            craft_storage.craft_update_recipe,
            guild_id=int(guild_id),
            recipe_id=_required_int(body, "recipe_id"),
            product_name=str(body.get("product_name") or ""),
            treasury_cost_per_unit=_required_int(
                body,
                "treasury_cost_per_unit",
                allow_zero=True,
            ),
            duration_minutes_per_unit=_required_int(
                body,
                "duration_minutes_per_unit",
            ),
            max_batch_size=_required_int(body, "max_batch_size"),
            materials=_recipe_materials(body),
            actor_id=int(actor_id),
            actor_display=str(actor_display),
        )
        return {"message": CRAFT_ACTION_LABELS[action], "recipe": recipe}

    if action == "clone_recipe":
        recipe = await asyncio.to_thread(
            craft_storage.craft_clone_recipe,
            guild_id=int(guild_id),
            recipe_id=_required_int(body, "recipe_id"),
            product_name=str(body.get("product_name") or ""),
            actor_id=int(actor_id),
            actor_display=str(actor_display),
        )
        return {"message": CRAFT_ACTION_LABELS[action], "recipe": recipe}

    if action == "toggle_recipe":
        recipe = await asyncio.to_thread(
            craft_storage.craft_set_recipe_active,
            guild_id=int(guild_id),
            recipe_id=_required_int(body, "recipe_id"),
            active=body.get("active") is True,
            actor_id=int(actor_id),
            actor_display=str(actor_display),
        )
        return {"message": CRAFT_ACTION_LABELS[action], "recipe": recipe}

    plan_id = _required_int(body, "plan_id")
    common = {
        "guild_id": int(guild_id),
        "plan_id": plan_id,
        "actor_id": int(actor_id),
        "actor_display": str(actor_display),
    }
    if action == "purchase":
        result = await asyncio.to_thread(
            craft_storage.craft_add_purchase,
            **common,
            plan_material_id=_required_int(body, "plan_material_id"),
            quantity=_required_int(body, "quantity"),
            total_cost=_required_int(body, "total_cost", allow_zero=True),
            finance_code=str(body.get("finance_code") or "").strip() or None,
        )
        plan = result["plan"]
    elif action == "start_batch":
        result = await asyncio.to_thread(
            craft_storage.craft_start_batch,
            **common,
            quantity=_required_int(body, "quantity"),
            log_channel_id=FINANCE_EVENT_LOG_CHANNEL_ID,
            admin_user_id=FINANCE_ADMIN_USER_ID,
        )
        plan = result["plan"]
        wake_notification_worker()
    elif action == "final_output":
        plan = await asyncio.to_thread(
            craft_storage.craft_set_final_output,
            **common,
            product_quantity=_required_int(body, "product_quantity", allow_zero=True),
        )
    elif action == "estimated_price":
        plan = await asyncio.to_thread(
            craft_storage.craft_set_estimated_price,
            **common,
            unit_price=_required_int(body, "unit_price"),
        )
    elif action == "market_listing":
        plan = await asyncio.to_thread(
            craft_storage.craft_add_market_listing,
            **common,
            quantity=_required_int(body, "quantity"),
        )
    elif action == "sale":
        plan = await asyncio.to_thread(
            craft_storage.craft_add_sale,
            **common,
            quantity=_required_int(body, "quantity"),
            total_amount=_required_int(body, "total_amount"),
        )
    else:
        raw_materials = body.get("material_quantities")
        if not isinstance(raw_materials, dict):
            raise ValueError("craft_inventory_incomplete")
        material_quantities = {
            int(key): int(value) for key, value in raw_materials.items()
        }
        result = await asyncio.to_thread(
            craft_storage.craft_inventory_check,
            **common,
            material_quantities=material_quantities,
            product_quantity=_required_int(body, "product_quantity", allow_zero=True),
            note=str(body.get("note") or "")[:500],
        )
        plan = result["plan"]

    warning = await _refresh_plan_projection(bot, plan_id)
    wake_craft_worker()
    return {
        "message": CRAFT_ACTION_LABELS[action],
        "plan": plan,
        "projection_warning": warning,
    }


async def execute_finance_command(
    *,
    guild_id: int,
    actor_id: int,
    actor_display: str,
    idempotency_key: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    action = str(body.get("action") or "").strip().lower()
    common = {
        "guild_id": int(guild_id),
        "actor_id": int(actor_id),
        "actor_display": str(actor_display),
        "channel_id": None,
        "message_id": None,
        "log_channel_id": FINANCE_EVENT_LOG_CHANNEL_ID,
        "admin_user_id": FINANCE_ADMIN_USER_ID,
    }
    if action in {"deposit", "withdraw"}:
        event = await asyncio.to_thread(
            finance_storage.finance_record_movement,
            **common,
            event_kind=action,
            amount=_required_int(body, "amount"),
            reason=str(body.get("reason") or ""),
            captcha_digest=f"web:{idempotency_key}",
            game_code=str(body.get("game_code") or ""),
        )
        message = "Поступление записано." if action == "deposit" else "Расход записан."
    elif action == "snapshot":
        event = await asyncio.to_thread(
            finance_storage.finance_record_snapshot,
            **common,
            event_kind="interim",
            amount=_required_int(body, "amount", allow_zero=True),
        )
        message = "Контрольный остаток казны зафиксирован."
    elif action == "undo":
        target_event_id = _required_int(body, "event_id")
        target = await asyncio.to_thread(
            finance_storage.finance_get_event,
            target_event_id,
        )
        if target is None or int(target.get("guild_id") or 0) != int(guild_id):
            raise ValueError("finance_event_not_found")
        result = await asyncio.to_thread(
            finance_storage.finance_undo_last_action,
            guild_id=int(guild_id),
            target_actor_id=int(target.get("actor_id") or actor_id),
            undone_by_id=int(actor_id),
            undone_by_display=str(actor_display),
            channel_id=None,
            message_id=None,
            log_channel_id=FINANCE_EVENT_LOG_CHANNEL_ID,
            admin_user_id=FINANCE_ADMIN_USER_ID,
            target_event_id=target_event_id,
        )
        if result is None:
            raise ValueError("finance_event_not_found")
        event = result["undo_event"]
        message = "Финансовая операция отменена с сохранением аудита."
    else:
        raise ValueError("finance_action_invalid")
    wake_notification_worker()
    return {"message": message, "event": event}


async def execute_bill_command(
    *,
    guild_id: int,
    actor_id: int,
    actor_display: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    action = str(body.get("action") or "").strip().lower()
    bill_number = _required_int(body, "bill_number")
    if action == "update":
        field = str(body.get("field") or "").strip()
        if field not in {"title", "summary", "materials", "status"}:
            raise ValueError("bill_field_invalid")
        value = str(body.get("value") or "").strip()
        if not value:
            raise ValueError("bill_value_required")
        bill = await asyncio.to_thread(
            tvrs_storage.tvrs_update_bill_field,
            int(guild_id),
            bill_number,
            field,
            value,
            int(actor_id),
            str(actor_display),
        )
        message = "Законопроект обновлён. Изменение записано в аудит."
    elif action == "delete":
        bill = await asyncio.to_thread(
            tvrs_storage.tvrs_delete_bill_by_number,
            int(guild_id),
            bill_number,
            int(actor_id),
            str(actor_display),
        )
        message = "Законопроект удалён с возможностью восстановления через аудит."
    else:
        raise ValueError("bill_action_invalid")
    if bill is None:
        raise ValueError("bill_not_found")
    return {"message": message, "bill": bill}


__all__ = [
    "BILL_ERROR_MESSAGES",
    "CRAFT_ERROR_MESSAGES",
    "FINANCE_ERROR_MESSAGES",
    "execute_bill_command",
    "execute_craft_command",
    "execute_finance_command",
]
