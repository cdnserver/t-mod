import asyncio
import json
import os
import re
import traceback
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

import storage
from modules.finance import (
    FINANCE_ADMIN_USER_ID,
    FINANCE_EVENT_LOG_CHANNEL_ID,
    FinancePanelView,
    finance_panel_embed,
    money_text,
    parse_money,
    wake_notification_worker,
)
from modules.control_center import WORKSHOP_CHANNEL_ID, log_technical_event
from modules.operations import ACTIVE_TASKS_CHANNEL_ID, wake_operations_worker


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw, 0)
    except (TypeError, ValueError):
        return default


CRAFT_CHANNEL_ID = WORKSHOP_CHANNEL_ID
CRAFT_EMBED_COLOR = _env_int("CRAFT_EMBED_COLOR", 0xD9D9D9)
CRAFT_QUIET_START_HOUR = max(0, min(23, _env_int("CRAFT_QUIET_START_HOUR", 2)))
CRAFT_QUIET_END_HOUR = max(0, min(23, _env_int("CRAFT_QUIET_END_HOUR", 9)))
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "Europe/Riga"))

_craft_worker_task: asyncio.Task[None] | None = None
_craft_wakeup: asyncio.Event | None = None
_registered_plan_messages: set[int] = set()


STAGE_LABELS = {
    "procurement": "🛒 Закупка материалов",
    "crafting": "⚙️ Производство",
    "awaiting_output": "📦 Итоговая сверка продукта",
    "listing": "🏷️ Выставление на маркет",
    "selling": "💰 Продажа",
    "completed": "✅ Завершён",
    "cancelled": "↩️ Отменён",
}

ERROR_MESSAGES = {
    "craft_bad_product_name": "Название продукта пустое или слишком длинное.",
    "craft_bad_treasury_cost": "Расход казны на единицу не может быть отрицательным.",
    "craft_bad_duration": "Укажите длительность одной единицы от 1 до 10080 минут.",
    "craft_bad_batch_size": "Размер одного цикла должен быть от 1 до 1000 единиц.",
    "craft_bad_material_count": "В рецепте должен быть хотя бы один и не более 20 материалов.",
    "craft_bad_material": "Проверьте названия и количество материалов: названия не должны повторяться, количество должно быть больше нуля.",
    "craft_recipe_exists": "Рецепт с таким названием уже существует.",
    "craft_bad_attempts": "Количество попыток должно быть от 1 до 1 000 000.",
    "craft_plan_too_large": "План слишком большой: итоговое количество материала выходит за безопасный предел. Уменьшите число попыток.",
    "craft_bad_responsible": "Укажите корректное упоминание или Discord ID ответственного.",
    "craft_recipe_not_found": "Рецепт не найден или отключён.",
    "craft_plan_not_found": "План крафта не найден.",
    "craft_plan_not_active": "Этот план уже завершён или недоступен.",
    "craft_not_procurement": "Закупка для этого плана уже завершена.",
    "craft_material_not_found": "Материал не найден в плане.",
    "craft_bad_purchase": "Количество должно быть больше нуля, а общая стоимость — неотрицательной.",
    "craft_purchase_too_large": "Нельзя закупить больше, чем осталось собрать по этому материалу. Обновите меню и введите актуальный остаток.",
    "craft_bad_finance_code": "Финансовый код должен состоять ровно из четырёх букв.",
    "craft_finance_code_not_found": "Активное снятие с таким кодом не найдено или уже связано с другой закупкой.",
    "craft_finance_amount_mismatch": "Сумма снятия с этим кодом не совпадает с общей стоимостью партии.",
    "craft_bad_inventory": "Количество на складе не может быть отрицательным.",
    "craft_inventory_incomplete": "Укажите количество каждого материала из шаблона.",
    "craft_not_crafting": "Сейчас план не находится на стадии производства.",
    "craft_bad_batch_quantity": "Количество единиц в цикле должно быть больше нуля.",
    "craft_batch_active": "Предыдущий цикл ещё не завершился.",
    "craft_batch_too_large": "Столько единиц нельзя поставить: превышен размер цикла или остаток плана.",
    "craft_finance_balance_unknown": "Сначала нужен точный отчёт казны или межотчёт: текущий остаток неизвестен.",
    "craft_finance_insufficient": "В расчётной казне недостаточно денег для запуска этого цикла.",
    "craft_not_awaiting_output": "Сейчас бот не ожидает итоговое количество продукта.",
    "craft_bad_output": "Итоговое количество продукта не может быть отрицательным.",
    "craft_output_too_large": "Полученных предметов не может быть больше завершённых попыток.",
    "craft_bad_price": "Цена за единицу должна быть больше нуля.",
    "craft_not_listing": "Сейчас план не находится на стадии выставления на маркет.",
    "craft_price_required": "Сначала укажите примерную стоимость продажи за одну штуку.",
    "craft_bad_listing": "Количество для маркета должно быть больше нуля.",
    "craft_listing_too_large": "Нельзя выставить больше предметов, чем осталось на складе.",
    "craft_not_selling": "Сначала необходимо выставить на маркет все готовые предметы.",
    "craft_bad_sale": "Количество и общая сумма продажи должны быть больше нуля.",
    "craft_sale_too_large": "Нельзя записать продажу большего количества, чем осталось.",
}


def display_name(user: discord.abc.User) -> str:
    return str(getattr(user, "display_name", user.name))


def parse_positive_int(value: str, *, allow_zero: bool = False) -> int:
    return parse_money(value, allow_zero=allow_zero)


def format_quantity(value: int | None) -> str:
    return f"{int(value or 0):,}".replace(",", " ")


def optional_money(value: int | float | None) -> str:
    return "—" if value is None else money_text(round(value))


def progress_bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "░" * width
    filled = max(0, min(width, round(width * done / total)))
    return "█" * filled + "░" * (width - filled)


def discord_time(iso_value: str | None, style: str = "F") -> str:
    if not iso_value:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:{style}>"
    except (TypeError, ValueError):
        return str(iso_value)


def error_text(exc: Exception) -> str:
    raw = str(exc)
    if raw.startswith("craft_batch_materials_missing:"):
        names = raw.split(":", 1)[1].strip()
        return f"На складе недостаточно материалов: **{names}**. Проведите сверку или пополните склад."
    return ERROR_MESSAGES.get(raw, "Не удалось выполнить действие. Попробуйте ещё раз или обратитесь к ответственному.")


async def send_interaction_error(interaction: discord.Interaction, exc: Exception) -> None:
    traceback.print_exception(type(exc), exc, exc.__traceback__)
    if interaction.guild is not None:
        await log_technical_event(
            interaction.client,
            interaction.guild,
            title="Ошибка системы крафтов",
            details=f"Канал: <#{interaction.channel_id}>\nПользователь: `{interaction.user.id}`\nОшибка: `{type(exc).__name__}: {str(exc)[:700]}`",
            dedupe_key=f"craft-interaction:{type(exc).__name__}",
            cooldown_seconds=60,
        )
    text = error_text(exc)
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


def parse_material_lines(value: str) -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.fullmatch(r"(.+?)\s*[:=]\s*([0-9\s.,_'’]+)", line)
        if not match:
            raise ValueError("craft_bad_material")
        result.append((match.group(1).strip(), parse_positive_int(match.group(2))))
    return result


def parse_user_id(value: str) -> int:
    match = re.search(r"(\d{15,22})", str(value or ""))
    if not match:
        raise ValueError("craft_bad_responsible")
    return int(match.group(1))


def add_chunked_field(embed: discord.Embed, name: str, lines: list[str], *, inline: bool = False) -> None:
    if not lines:
        embed.add_field(name=name, value="—", inline=inline)
        return
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}".strip()
        if len(candidate) > 1000 and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    for index, chunk in enumerate(chunks):
        embed.add_field(name=name if index == 0 else f"{name} • продолжение", value=chunk, inline=inline)


def recipe_summary(recipe: dict[str, Any]) -> str:
    materials = "\n".join(
        f"• **{item['material_name']}** — {format_quantity(item['quantity_per_unit'])} шт."
        for item in recipe["materials"]
    )


def recipe_embed(recipe: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=f"📘 Рецепт #{recipe['id']} • {recipe['product_name']}",
        description=(
            f"Статус: **{'активен' if recipe.get('active', 1) else 'отключён'}** · версия **{recipe.get('version', 1)}**\n"
            f"Расход казны: **{money_text(recipe['treasury_cost_per_unit'])} / ед.**\n"
            f"Время: **{recipe['duration_minutes_per_unit']} мин. / ед.**\n"
            f"Максимум за цикл: **{recipe['max_batch_size']} шт.**"
        ),
        color=CRAFT_EMBED_COLOR,
    )
    add_chunked_field(
        embed,
        "🧱 Материалы на одну единицу",
        [
            f"• **{item['material_name']}** — {format_quantity(item['quantity_per_unit'])} шт."
            for item in recipe["materials"]
        ],
    )
    return embed


def recipe_versions_embed(recipe: dict[str, Any], versions: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title=f"🕘 История рецепта #{recipe['id']} · {recipe['product_name']}",
        color=CRAFT_EMBED_COLOR,
    )
    if not versions:
        embed.description = "История версий пока отсутствует."
        return embed
    labels = {
        "created": "создан",
        "updated": "изменён",
        "enabled": "включён",
        "disabled": "отключён",
        "undo_restore": "восстановлен отменой",
    }
    lines = []
    for version in versions[:15]:
        materials = version.get("materials") or []
        lines.append(
            f"**v{version['version']} · {labels.get(version['change_kind'], version['change_kind'])}** · "
            f"{'активен' if version['active'] else 'отключён'}\n"
            f"{version['duration_minutes_per_unit']} мин./шт. · цикл {version['max_batch_size']} · "
            f"казна {money_text(version['treasury_cost_per_unit'])}/шт. · материалов {len(materials)} · "
            f"{discord_time(version['created_at'], 'f')}"
        )
    embed.description = "\n\n".join(lines)
    return embed
    return (
        f"**Рецепт #{recipe['id']} • {recipe['product_name']}**\n"
        f"Расход казны: **{money_text(recipe['treasury_cost_per_unit'])} / ед.**\n"
        f"Время: **{recipe['duration_minutes_per_unit']} мин. / ед.**\n"
        f"Максимум за цикл: **{recipe['max_batch_size']} шт.**\n"
        f"{materials}"
    )


def craft_menu_embed(guild_id: int) -> discord.Embed:
    active = storage.craft_active_plans(guild_id, 100)
    recipes = storage.craft_list_recipes(guild_id, limit=100)
    embed = discord.Embed(
        title="🏭 Центр крафтов Товарищества",
        description=(
            "Создавайте производственные планы, собирайте материалы, запускайте циклы и "
            "фиксируйте продажи. Публичная карточка каждого плана обновляется автоматически."
        ),
        color=CRAFT_EMBED_COLOR,
    )
    embed.add_field(name="Активных планов", value=str(len(active)), inline=True)
    embed.add_field(name="Доступных рецептов", value=str(len(recipes)), inline=True)
    embed.add_field(
        name="Быстрый выбор",
        value=(
            "**Текущие крафты** — открыть активные планы\n"
            "**Новый план** — назначить ответственного и объём\n"
            "**Рецепты** — посмотреть или добавить рецепт"
        ),
        inline=False,
    )
    embed.set_footer(text="Меню доступно всем участникам этого канала")
    return embed


def recipe_list_embed(recipes: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(title="📚 Рецепты крафта", color=CRAFT_EMBED_COLOR)
    if not recipes:
        embed.description = "Рецептов пока нет. Нажмите **«Добавить рецепт»**."
        return embed
    visible = recipes[:10]
    lines = []
    for recipe in visible:
        preview = recipe["materials"][:3]
        material_text = ", ".join(
            f"{item['material_name']} × {format_quantity(item['quantity_per_unit'])}"
            for item in preview
        )
        if len(recipe["materials"]) > len(preview):
            material_text += f" и ещё {len(recipe['materials']) - len(preview)}"
        lines.append(
            f"**#{recipe['id']} · {recipe['product_name']}**\n"
            f"{recipe['duration_minutes_per_unit']} мин./шт. · цикл до {recipe['max_batch_size']} · "
            f"казна {money_text(recipe['treasury_cost_per_unit'])}/шт.\n"
            f"Материалы: {material_text}"
        )
    add_chunked_field(embed, "Доступные рецепты", lines)
    if len(recipes) > len(visible):
        embed.set_footer(
            text=f"Показано {len(visible)} из {len(recipes)}. В «Новом плане» доступны все рецепты по страницам."
        )
    return embed


def active_plans_embed(plans: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(title="🏗️ Текущие крафты", color=CRAFT_EMBED_COLOR)
    if not plans:
        embed.description = "Активных планов сейчас нет."
        return embed
    lines = []
    visible = plans[:15]
    for plan in visible:
        recipe = plan["recipe"]
        link = (
            f"https://discord.com/channels/{plan['guild_id']}/{plan['channel_id']}/{plan['message_id']}"
            if plan.get("message_id")
            else None
        )
        title = f"[План #{plan['id']} · {recipe['product_name']}]({link})" if link else f"План #{plan['id']} · {recipe['product_name']}"
        lines.append(
            f"**{title}**\n{STAGE_LABELS.get(plan['stage'], plan['stage'])} · "
            f"ответственный <@{plan['responsible_id']}> · "
            f"{plan['attempts_completed']}/{plan['attempts_total']} завершено"
        )
    add_chunked_field(embed, "Планы", lines)
    if len(plans) > len(visible):
        embed.set_footer(text=f"Показано {len(visible)} из {len(plans)} активных планов.")
    return embed


def plan_embed(plan: dict[str, Any]) -> discord.Embed:
    recipe = plan["recipe"]
    stage = str(plan["stage"])
    embed = discord.Embed(
        title=f"🏭 Крафт #{plan['id']} • {recipe['product_name']}",
        description=(
            f"**Стадия:** {STAGE_LABELS.get(stage, stage)}\n"
            f"**Ответственный:** <@{plan['responsible_id']}>\n"
            f"**Создал план:** <@{plan['created_by_id']}>"
        ),
        color=(discord.Color.green() if stage == "completed" else discord.Color.orange() if stage == "cancelled" else CRAFT_EMBED_COLOR),
    )
    total = int(plan["attempts_total"])
    completed = int(plan["attempts_completed"])
    queued = int(plan["attempts_queued"])
    embed.add_field(
        name="📋 План",
        value=(
            f"Попыток: **{format_quantity(total)}**\n"
            f"Поставлено: **{format_quantity(queued)}**\n"
            f"Завершено: **{format_quantity(completed)}**\n"
            f"`{progress_bar(completed, total)}` {completed}/{total}"
        ),
        inline=True,
    )
    embed.add_field(
        name="⏱️ Рецепт",
        value=(
            f"**{recipe['duration_minutes_per_unit']} мин.** за 1 ед.\n"
            f"До **{recipe['max_batch_size']} ед.** за цикл\n"
            f"Казна: **{money_text(recipe['treasury_cost_per_unit'])} / ед.**"
        ),
        inline=True,
    )

    material_lines = []
    for material in plan["materials"]:
        stock = int(material["stock_quantity"])
        required = int(material["required_total"])
        remaining = max(0, required - stock) if stage == "procurement" else stock
        if stage == "procurement":
            icon = "✅" if stock >= required else "🟡"
            tail = f"осталось {format_quantity(max(0, required - stock))}"
        else:
            icon = "📦"
            tail = f"на складе {format_quantity(stock)}"
        material_lines.append(
            f"{icon} **{material['material_name']}** — {format_quantity(stock)} / {format_quantity(required)} · {tail}"
        )
    add_chunked_field(embed, "🧱 Материалы", material_lines)

    if stage == "procurement":
        total_required = sum(int(item["required_total"]) for item in plan["materials"])
        total_stock = sum(min(int(item["stock_quantity"]), int(item["required_total"])) for item in plan["materials"])
        embed.add_field(
            name="🛒 Закупка",
            value=(
                f"Готовность: `{progress_bar(total_stock, total_required)}`\n"
                f"Партий записано: **{plan['purchase_count']}**\n"
                f"Потрачено: **{money_text(plan['purchase_cost_total'])}**"
            ),
            inline=False,
        )
    elif stage == "crafting":
        batch = plan.get("active_batch")
        if batch:
            batch_text = (
                f"Сейчас производится **{batch['quantity']} шт.**\n"
                f"Поставил: <@{batch['started_by_id']}>\n"
                f"Готово: {discord_time(batch['due_at'])} ({discord_time(batch['due_at'], 'R')})"
            )
        elif completed < total:
            batch_text = "Текущего цикла нет. **Пора поставить следующую партию.**"
        else:
            batch_text = "Все циклы поставлены. Ожидается завершение последнего."
        embed.add_field(name="⚙️ Текущий цикл", value=batch_text, inline=False)
    elif stage == "awaiting_output":
        embed.add_field(
            name="📦 Требуется итог",
            value=(
                "Все попытки завершены. Проверьте склад и нажмите **«Указать результат»**, "
                "чтобы записать фактическое количество полученного продукта."
            ),
            inline=False,
        )
    elif stage in {"listing", "selling", "completed"}:
        final_qty = int(plan.get("final_product_qty") or 0)
        sold = int(plan.get("sold_qty") or 0)
        listed = int(plan.get("market_listed_qty") or 0)
        revenue = int(plan.get("total_revenue") or 0)
        average = revenue / sold if sold else 0
        embed.add_field(
            name="🏷️ Маркет и продажи",
            value=(
                f"Получено: **{format_quantity(final_qty)} шт.**\n"
                f"Оценка: **{optional_money(plan.get('estimated_unit_price'))} / шт.**\n"
                f"Выставлено: **{format_quantity(listed)} / {format_quantity(final_qty)}**\n"
                f"Продано: **{format_quantity(sold)} / {format_quantity(final_qty)}**\n"
                f"Выручка: **{money_text(revenue)}**\n"
                f"Средняя продажа: **{optional_money(average if sold else None)} / шт.**"
            ),
            inline=False,
        )
    embed.set_footer(text=f"План #{plan['id']} • все действия сохраняются в ветке сообщения")
    return embed


def event_log_embed(event: dict[str, Any]) -> discord.Embed:
    details = json.loads(str(event.get("details_json") or "{}"))
    kind = str(event.get("event_kind") or "")
    titles = {
        "plan_created": "📋 Создан план крафта",
        "purchase": "🛒 Закуплена партия материала",
        "stage_crafting": "⚙️ Все материалы собраны",
        "inventory_check": "📦 Проведена сверка склада",
        "batch_started": "▶️ Цикл крафта поставлен",
        "batch_completed": "✅ Цикл крафта завершён",
        "stage_awaiting_output": "📊 Все попытки завершены",
        "final_output": "📦 Зафиксирован итог производства",
        "estimated_price": "🏷️ Указана примерная цена",
        "market_listing": "🛍️ Продукт выставлен на маркет",
        "sale": "💰 Записана продажа",
        "action_undone": "↩️ Действие крафта отменено",
    }
    embed = discord.Embed(title=titles.get(kind, "Событие крафта"), color=CRAFT_EMBED_COLOR)
    if event.get("actor_id"):
        embed.add_field(
            name="Участник",
            value=f"<@{event['actor_id']}> · `{event['actor_id']}`",
            inline=False,
        )
    if kind == "plan_created":
        embed.add_field(name="Продукт", value=str(details.get("product_name")), inline=True)
        embed.add_field(name="Попыток", value=format_quantity(details.get("attempts_total")), inline=True)
        embed.add_field(name="Ответственный", value=f"<@{details.get('responsible_id')}>", inline=False)
    elif kind == "purchase":
        qty = int(details.get("quantity") or 0)
        total_cost = int(details.get("total_cost") or 0)
        embed.add_field(name="Материал", value=str(details.get("material_name")), inline=True)
        embed.add_field(name="Количество", value=f"{format_quantity(qty)} шт.", inline=True)
        embed.add_field(name="Стоимость партии", value=money_text(total_cost), inline=True)
        embed.add_field(name="Средняя цена", value=f"{money_text(round(total_cost / qty) if qty else 0)} / шт.", inline=True)
        embed.add_field(name="Осталось собрать", value=f"{format_quantity(details.get('remaining'))} шт.", inline=True)
        code = details.get("finance_code")
        embed.add_field(name="Связь с финансами", value=f"✅ Код `{code}`" if code else "⚠️ Код не указан", inline=False)
    elif kind == "inventory_check":
        materials = details.get("materials") or {}
        add_chunked_field(
            embed,
            "Материалы на складе",
            [f"**{name}:** {format_quantity(qty)}" for name, qty in materials.items()],
        )
        embed.add_field(name="Готовый продукт", value=f"{format_quantity(details.get('product_quantity'))} шт.", inline=True)
        if details.get("note"):
            embed.add_field(name="Комментарий", value=str(details["note"])[:1024], inline=False)
    elif kind == "batch_started":
        embed.add_field(name="Поставлено", value=f"{format_quantity(details.get('quantity'))} шт.", inline=True)
        embed.add_field(name="Длительность", value=f"{details.get('duration_minutes')} мин.", inline=True)
        embed.add_field(name="Завершение", value=discord_time(details.get("due_at")), inline=False)
        embed.add_field(name="Расход казны", value=money_text(details.get("treasury_cost")), inline=True)
        if details.get("finance_code"):
            embed.add_field(name="Финансовый код", value=f"`{details['finance_code']}`", inline=True)
    elif kind == "batch_completed":
        embed.add_field(name="Завершено", value=f"{format_quantity(details.get('quantity'))} шт.", inline=True)
        embed.add_field(name="Плановое время", value=discord_time(details.get("due_at")), inline=True)
    elif kind == "final_output":
        embed.add_field(name="Получено продукта", value=f"{format_quantity(details.get('product_quantity'))} шт.", inline=True)
        embed.add_field(name="Попыток завершено", value=format_quantity(details.get("attempts_completed")), inline=True)
    elif kind == "estimated_price":
        embed.add_field(name="Цена за единицу", value=money_text(details.get("unit_price")), inline=True)
    elif kind == "market_listing":
        embed.add_field(name="Выставлено сейчас", value=f"{format_quantity(details.get('quantity'))} шт.", inline=True)
        embed.add_field(name="Всего выставлено", value=f"{format_quantity(details.get('listed_total'))} шт.", inline=True)
        embed.add_field(name="Осталось выставить", value=f"{format_quantity(details.get('remaining_to_list'))} шт.", inline=True)
    elif kind == "sale":
        qty = int(details.get("quantity") or 0)
        amount = int(details.get("total_amount") or 0)
        embed.add_field(name="Продано", value=f"{format_quantity(qty)} шт.", inline=True)
        embed.add_field(name="Получено", value=money_text(amount), inline=True)
        embed.add_field(name="Средняя цена", value=f"{money_text(round(amount / qty) if qty else 0)} / шт.", inline=True)
        embed.add_field(name="Осталось продать", value=f"{format_quantity(details.get('remaining_to_sell'))} шт.", inline=True)
        embed.add_field(name="Общая выручка", value=money_text(details.get("revenue_total")), inline=True)
    elif kind == "action_undone":
        embed.add_field(name="ID действия", value=f"#{details.get('action_id')}", inline=True)
        embed.add_field(name="Тип", value=f"`{details.get('original_action') or 'неизвестно'}`", inline=True)
        embed.add_field(name="Причина", value=str(details.get("reason") or "Не указана")[:1024], inline=False)
    else:
        message = details.get("message")
        if message:
            embed.description = str(message)
    embed.set_footer(text=f"Событие #{event['id']} • {discord_time(event.get('created_at'))}")
    return embed


def completion_embed(plan: dict[str, Any]) -> discord.Embed:
    recipe = plan["recipe"]
    attempts = int(plan["attempts_total"])
    output = int(plan.get("final_product_qty") or 0)
    sold = int(plan.get("sold_qty") or 0)
    revenue = int(plan.get("total_revenue") or 0)
    purchases = int(plan.get("purchase_cost_total") or 0)
    craft_fees = int(recipe["treasury_cost_per_unit"]) * int(plan["attempts_queued"])
    average_sale = revenue / sold if sold else 0
    average_purchase_per_output = purchases / output if output else 0
    embed = discord.Embed(
        title=f"✅ Крафт #{plan['id']} завершён • {recipe['product_name']}",
        description=f"Полный цикл производства и продаж завершён. Ответственный: <@{plan['responsible_id']}>.",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="⚙️ Производство",
        value=(
            f"Запланировано попыток: **{format_quantity(attempts)}**\n"
            f"Завершено попыток: **{format_quantity(plan['attempts_completed'])}**\n"
            f"Получено продукта: **{format_quantity(output)} шт.**\n"
            f"Успешность: **{(output / attempts * 100) if attempts else 0:.1f}%**"
        ),
        inline=False,
    )
    embed.add_field(
        name="🧱 Затраты",
        value=(
            f"Закупки материалов: **{money_text(purchases)}**\n"
            f"Расход казны на крафт: **{money_text(craft_fees)}**\n"
            f"Закупки на единицу результата: **{optional_money(average_purchase_per_output if output else None)}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="💰 Продажи",
        value=(
            f"Продано: **{format_quantity(sold)} шт.**\n"
            f"Выручка: **{money_text(revenue)}**\n"
            f"Средняя цена: **{optional_money(average_sale if sold else None)} / шт.**"
        ),
        inline=True,
    )
    result = revenue - purchases - craft_fees
    embed.add_field(name="📊 Финансовый результат", value=f"**{money_text(result)}**", inline=False)
    embed.set_footer(text=f"План #{plan['id']} • подробная история находится в ветке исходного сообщения")
    return embed


def craft_stats_embed(stats: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=f"📊 Статистика крафтов · {stats['days']} дн.",
        color=CRAFT_EMBED_COLOR,
    )
    embed.add_field(
        name="Планы",
        value=(
            f"Всего: **{stats['plan_count']}**\n"
            f"Активных: **{stats['active_count']}**\n"
            f"Завершено: **{stats['completed_count']}**\n"
            f"Отменено: **{stats['cancelled_count']}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Производство",
        value=(
            f"Запланировано: **{format_quantity(stats['attempts_planned'])}**\n"
            f"Завершено: **{format_quantity(stats['attempts_completed'])}**\n"
            f"Получено: **{format_quantity(stats['output_total'])}**\n"
            f"Успешность: **{stats['success_rate']:.1f}%**"
        ),
        inline=True,
    )
    delay = stats.get("average_delay_seconds")
    delay_text = "—" if delay is None else f"{round(float(delay) / 60, 1)} мин."
    embed.add_field(
        name="Циклы",
        value=(
            f"Циклов: **{stats['batch_count']}**\n"
            f"Единиц в циклах: **{format_quantity(stats['batch_units'])}**\n"
            f"Средняя задержка: **{delay_text}**"
        ),
        inline=True,
    )
    embed.add_field(name="Закупки", value=money_text(stats["purchase_cost"]), inline=True)
    embed.add_field(name="Расход крафта", value=money_text(stats["craft_fees"]), inline=True)
    embed.add_field(name="Выручка", value=money_text(stats["revenue"]), inline=True)
    embed.add_field(name="Финансовый результат", value=f"**{money_text(stats['profit'])}**", inline=False)
    recipes = [
        f"**{row['product_name'] or 'Без названия'}** — {row['plans']} пл. · "
        f"{format_quantity(row['output'])} получено · {money_text(row['revenue'])}"
        for row in stats.get("recipes", [])
    ]
    if recipes:
        embed.add_field(name="Рецепты", value="\n".join(recipes)[:1024], inline=False)
    contributors = [
        f"<@{row['actor_id']}> — закупок {row['purchases']}, циклов {row['batches']}, продаж {row['sales']}"
        for row in stats.get("contributors", [])
    ]
    if contributors:
        embed.add_field(name="Участники", value="\n".join(contributors)[:1024], inline=False)
    embed.set_footer(text="Отменённые закупки, циклы и продажи исключены")
    return embed


async def craft_channel_only(
    interaction: discord.Interaction,
    *,
    allow_any_channel: bool = False,
) -> bool:
    if interaction.guild is None:
        await interaction.response.send_message("Система крафтов работает только на сервере.", ephemeral=True)
        return False
    return True


def can_manage_market(interaction: discord.Interaction, plan: dict[str, Any]) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    return (
        interaction.user.id == int(plan["responsible_id"])
        or interaction.user.id == FINANCE_ADMIN_USER_ID
        or bool(permissions and permissions.administrator)
    )


async def _get_channel(bot: commands.Bot, channel_id: int) -> Any:
    channel = bot.get_channel(channel_id)
    return channel if channel is not None else await bot.fetch_channel(channel_id)


async def update_plan_message(bot: commands.Bot, plan_id: int) -> None:
    plan = await asyncio.to_thread(storage.craft_get_plan, plan_id)
    if plan is None or not plan.get("message_id"):
        return
    channel = await _get_channel(bot, int(plan["channel_id"]))
    message = await channel.fetch_message(int(plan["message_id"]))
    view = None if plan["stage"] in {"completed", "cancelled"} else CraftPlanView(plan)
    await message.edit(embed=plan_embed(plan), view=view)


async def delete_plan_reminders(bot: commands.Bot, plan: dict[str, Any]) -> None:
    message_ids = await asyncio.to_thread(storage.craft_open_reminder_messages, int(plan["id"]))
    if not message_ids:
        return
    channel = await _get_channel(bot, int(plan["channel_id"]))
    for message_id in message_ids:
        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
            await asyncio.to_thread(storage.craft_mark_reminder_deleted, int(plan["id"]), message_id)
        except discord.NotFound:
            await asyncio.to_thread(storage.craft_mark_reminder_deleted, int(plan["id"]), message_id)
        except discord.DiscordException:
            traceback.print_exc()


def migrated_plan_embed(plan: dict[str, Any], new_url: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"↪️ Крафт #{plan['id']} перенесён",
        description=(
            "Единственная рабочая карточка этого плана теперь находится в мастерской. "
            f"Продолжайте работу здесь: [открыть актуальную карточку]({new_url})."
        ),
        color=discord.Color.light_grey(),
    )
    embed.set_footer(text="Старая карточка отключена и больше не получает обновления")
    return embed


async def migrate_active_plan_channels(bot: commands.Bot, guild: discord.Guild) -> None:
    plans = await asyncio.to_thread(storage.craft_active_plans, guild.id, 100)
    outdated = [plan for plan in plans if int(plan.get("channel_id") or 0) != CRAFT_CHANNEL_ID]
    if not outdated:
        return
    target_channel = await _get_channel(bot, CRAFT_CHANNEL_ID)
    if getattr(target_channel, "guild", None) is None or int(target_channel.guild.id) != guild.id:
        raise RuntimeError("WORKSHOP_CHANNEL_ID не принадлежит серверу плана крафта")

    for plan in outdated:
        new_message: discord.Message | None = None
        new_thread: discord.Thread | None = None
        rebound: dict[str, Any] | None = None
        old_channel_id = int(plan.get("channel_id") or 0)
        old_message_id = int(plan.get("message_id") or 0)
        old_thread_id = int(plan.get("thread_id") or 0)
        old_url = (
            f"https://discord.com/channels/{guild.id}/{old_channel_id}/{old_message_id}"
            if old_channel_id and old_message_id
            else None
        )
        try:
            await delete_plan_reminders(bot, plan)
            new_message = await target_channel.send(embed=plan_embed(plan), view=CraftPlanView(plan))
            thread_name = f"крафт-{plan['id']}-{plan['recipe']['product_name']}"[:100]
            new_thread = await new_message.create_thread(name=thread_name, auto_archive_duration=1440)
            rebound = await asyncio.to_thread(
                storage.craft_bind_plan_message,
                plan_id=int(plan["id"]),
                channel_id=CRAFT_CHANNEL_ID,
                message_id=new_message.id,
                thread_id=new_thread.id,
            )
            if rebound is None:
                raise RuntimeError("craft_plan_rebind_failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
            if new_message is not None:
                try:
                    await new_message.delete()
                except discord.DiscordException:
                    pass
            continue

        try:
            await new_message.edit(embed=plan_embed(rebound), view=CraftPlanView(rebound))
            history_note = "План автоматически перенесён в «Мастерскую»."
            if old_url:
                history_note += f" Предыдущая карточка и история: {old_url}"
            await new_thread.send(history_note, allowed_mentions=discord.AllowedMentions.none())
        except discord.DiscordException:
            traceback.print_exc()
        try:
            bot.add_view(CraftPlanView(rebound), message_id=new_message.id)
            _registered_plan_messages.add(new_message.id)
        except Exception:
            traceback.print_exc()

        if old_channel_id and old_message_id:
            try:
                old_channel = await _get_channel(bot, old_channel_id)
                old_message = await old_channel.fetch_message(old_message_id)
                if old_channel_id == ACTIVE_TASKS_CHANNEL_ID:
                    await old_message.delete()
                else:
                    await old_message.edit(embed=migrated_plan_embed(plan, new_message.jump_url), view=None)
            except discord.DiscordException:
                pass
        if old_thread_id:
            try:
                old_thread = await _get_channel(bot, old_thread_id)
                if isinstance(old_thread, discord.Thread):
                    if old_thread.archived:
                        await old_thread.edit(archived=False)
                    await old_thread.send(
                        f"Работа продолжена в новой ветке: <#{new_thread.id}>.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    await old_thread.edit(archived=True)
            except discord.DiscordException:
                pass
        wake_operations_worker()


def wake_craft_worker() -> None:
    if _craft_wakeup is not None:
        _craft_wakeup.set()
    wake_operations_worker()


class RecipeModal(discord.ui.Modal):
    def __init__(self, *, allow_any_channel: bool = False) -> None:
        super().__init__(title="Новый рецепт", timeout=600)
        self.allow_any_channel = allow_any_channel
        self.product = discord.ui.TextInput(
            label="Название продукта",
            placeholder="Промышленные металлы",
            min_length=2,
            max_length=100,
        )
        self.treasury_cost = discord.ui.TextInput(
            label="Расход казны на 1 единицу",
            placeholder="1000",
            min_length=1,
            max_length=30,
        )
        self.duration = discord.ui.TextInput(
            label="Минут на крафт 1 единицы",
            placeholder="10",
            min_length=1,
            max_length=10,
        )
        self.batch_size = discord.ui.TextInput(
            label="Максимум единиц за один цикл",
            placeholder="10",
            min_length=1,
            max_length=10,
        )
        self.materials = discord.ui.TextInput(
            label="Материалы на 1 единицу",
            placeholder="Железная руда: 50\nСеребряная руда: 30\nМедная руда: 15\nОловянная руда: 5",
            style=discord.TextStyle.paragraph,
            min_length=3,
            max_length=2000,
        )
        for item in (self.product, self.treasury_cost, self.duration, self.batch_size, self.materials):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await craft_channel_only(interaction, allow_any_channel=self.allow_any_channel):
            return
        try:
            recipe = await asyncio.to_thread(
                storage.craft_create_recipe,
                guild_id=interaction.guild.id,
                product_name=self.product.value,
                treasury_cost_per_unit=parse_positive_int(self.treasury_cost.value, allow_zero=True),
                duration_minutes_per_unit=parse_positive_int(self.duration.value),
                max_batch_size=parse_positive_int(self.batch_size.value),
                materials=parse_material_lines(self.materials.value),
                created_by_id=interaction.user.id,
                created_by_display=display_name(interaction.user),
            )
        except Exception as exc:
            await send_interaction_error(interaction, exc)
            return
        await interaction.response.send_message(
            content=f"Рецепт создан. Действие для отмены: **#{recipe['action_id']}**.",
            embed=recipe_embed(recipe),
            ephemeral=True,
        )


class RecipeEditModal(discord.ui.Modal):
    def __init__(self, recipe: dict[str, Any], *, allow_any_channel: bool = False) -> None:
        super().__init__(title=f"Изменить рецепт #{recipe['id']}", timeout=600)
        self.recipe_id = int(recipe["id"])
        self.allow_any_channel = allow_any_channel
        self.product = discord.ui.TextInput(
            label="Название продукта",
            default=str(recipe["product_name"]),
            min_length=2,
            max_length=100,
        )
        self.treasury_cost = discord.ui.TextInput(
            label="Расход казны на 1 единицу",
            default=str(recipe["treasury_cost_per_unit"]),
            min_length=1,
            max_length=30,
        )
        self.duration = discord.ui.TextInput(
            label="Минут на крафт 1 единицы",
            default=str(recipe["duration_minutes_per_unit"]),
            min_length=1,
            max_length=10,
        )
        self.batch_size = discord.ui.TextInput(
            label="Максимум единиц за один цикл",
            default=str(recipe["max_batch_size"]),
            min_length=1,
            max_length=10,
        )
        material_text = "\n".join(
            f"{item['material_name']}: {item['quantity_per_unit']}" for item in recipe["materials"]
        )
        self.materials = discord.ui.TextInput(
            label="Материалы на 1 единицу",
            default=material_text[:2000],
            style=discord.TextStyle.paragraph,
            min_length=3,
            max_length=2000,
        )
        for item in (self.product, self.treasury_cost, self.duration, self.batch_size, self.materials):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await craft_channel_only(interaction, allow_any_channel=self.allow_any_channel):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            recipe = await asyncio.to_thread(
                storage.craft_update_recipe,
                guild_id=interaction.guild.id,
                recipe_id=self.recipe_id,
                product_name=self.product.value,
                treasury_cost_per_unit=parse_positive_int(self.treasury_cost.value, allow_zero=True),
                duration_minutes_per_unit=parse_positive_int(self.duration.value),
                max_batch_size=parse_positive_int(self.batch_size.value),
                materials=parse_material_lines(self.materials.value),
                actor_id=interaction.user.id,
                actor_display=display_name(interaction.user),
            )
            await interaction.followup.send(
                content=f"Рецепт обновлён. Действие для отмены: **#{recipe['action_id']}**.",
                embed=recipe_embed(recipe),
                view=RecipeDetailView(recipe, allow_any_channel=self.allow_any_channel),
                ephemeral=True,
            )
        except Exception as exc:
            await send_interaction_error(interaction, exc)


class RecipeCloneModal(discord.ui.Modal):
    def __init__(self, recipe: dict[str, Any], *, allow_any_channel: bool = False) -> None:
        super().__init__(title=f"Копировать рецепт #{recipe['id']}", timeout=300)
        self.recipe_id = int(recipe["id"])
        self.allow_any_channel = allow_any_channel
        self.product = discord.ui.TextInput(
            label="Название нового продукта",
            default=(str(recipe["product_name"]) + " — копия")[:100],
            min_length=2,
            max_length=100,
        )
        self.add_item(self.product)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await craft_channel_only(interaction, allow_any_channel=self.allow_any_channel):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            recipe = await asyncio.to_thread(
                storage.craft_clone_recipe,
                guild_id=interaction.guild.id,
                recipe_id=self.recipe_id,
                product_name=self.product.value,
                actor_id=interaction.user.id,
                actor_display=display_name(interaction.user),
            )
            await interaction.followup.send(
                content=f"Копия создана. Действие для отмены: **#{recipe['action_id']}**.",
                embed=recipe_embed(recipe),
                view=RecipeDetailView(recipe, allow_any_channel=self.allow_any_channel),
                ephemeral=True,
            )
        except Exception as exc:
            await send_interaction_error(interaction, exc)


class RecipeDetailView(discord.ui.View):
    def __init__(self, recipe: dict[str, Any], *, allow_any_channel: bool = False) -> None:
        super().__init__(timeout=900)
        self.recipe_id = int(recipe["id"])
        self.active = bool(recipe.get("active", 1))
        self.allow_any_channel = allow_any_channel

    @discord.ui.button(label="Изменить", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await craft_channel_only(interaction, allow_any_channel=self.allow_any_channel):
            return
        recipe = await asyncio.to_thread(storage.craft_get_recipe, self.recipe_id, interaction.guild.id)
        if recipe is None:
            await interaction.response.send_message("Рецепт не найден.", ephemeral=True)
            return
        await interaction.response.send_modal(
            RecipeEditModal(recipe, allow_any_channel=self.allow_any_channel)
        )

    @discord.ui.button(label="Копировать", emoji="📄", style=discord.ButtonStyle.secondary)
    async def clone(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await craft_channel_only(interaction, allow_any_channel=self.allow_any_channel):
            return
        recipe = await asyncio.to_thread(storage.craft_get_recipe, self.recipe_id, interaction.guild.id)
        if recipe is None:
            await interaction.response.send_message("Рецепт не найден.", ephemeral=True)
            return
        await interaction.response.send_modal(
            RecipeCloneModal(recipe, allow_any_channel=self.allow_any_channel)
        )

    @discord.ui.button(label="Включить / отключить", emoji="⏯️", style=discord.ButtonStyle.danger)
    async def toggle(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await craft_channel_only(interaction, allow_any_channel=self.allow_any_channel):
            return
        try:
            recipe = await asyncio.to_thread(
                storage.craft_set_recipe_active,
                guild_id=interaction.guild.id,
                recipe_id=self.recipe_id,
                active=not self.active,
                actor_id=interaction.user.id,
                actor_display=display_name(interaction.user),
            )
            action_note = (
                f" Действие для отмены: **#{recipe['action_id']}**."
                if recipe.get("action_id") is not None
                else " Состояние рецепта уже было таким — новое действие не создано."
            )
            await interaction.response.edit_message(
                content=(
                    f"Рецепт {'включён' if recipe['active'] else 'отключён'}."
                    f"{action_note}"
                ),
                embed=recipe_embed(recipe),
                view=RecipeDetailView(recipe, allow_any_channel=self.allow_any_channel),
            )
        except Exception as exc:
            await send_interaction_error(interaction, exc)

    @discord.ui.button(label="Версии", emoji="🕘", style=discord.ButtonStyle.secondary)
    async def versions(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await craft_channel_only(interaction, allow_any_channel=self.allow_any_channel):
            return
        recipe = await asyncio.to_thread(storage.craft_get_recipe, self.recipe_id, interaction.guild.id)
        versions = await asyncio.to_thread(storage.craft_recipe_versions, self.recipe_id, interaction.guild.id, 20)
        if recipe is None:
            await interaction.response.send_message("Рецепт не найден.", ephemeral=True)
            return
        await interaction.response.send_message(embed=recipe_versions_embed(recipe, versions), ephemeral=True)


class RecipeAdminSelect(discord.ui.Select):
    def __init__(self, recipes: list[dict[str, Any]], *, allow_any_channel: bool = False) -> None:
        self.allow_any_channel = allow_any_channel
        options = [
            discord.SelectOption(
                label=str(recipe["product_name"])[:100],
                value=str(recipe["id"]),
                description=(
                    f"v{recipe.get('version', 1)} · {'активен' if recipe.get('active', 1) else 'отключён'} · "
                    f"{len(recipe['materials'])} материалов"
                )[:100],
            )
            for recipe in recipes
        ]
        super().__init__(placeholder="Выберите рецепт для управления", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        recipe = await asyncio.to_thread(storage.craft_get_recipe, int(self.values[0]), interaction.guild.id)
        if recipe is None:
            await interaction.response.send_message("Рецепт не найден.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=recipe_embed(recipe),
            view=RecipeDetailView(recipe, allow_any_channel=self.allow_any_channel),
            ephemeral=True,
        )


class RecipeAdminSelectView(discord.ui.View):
    def __init__(
        self,
        recipes: list[dict[str, Any]],
        page: int = 0,
        *,
        allow_any_channel: bool = False,
    ) -> None:
        super().__init__(timeout=300)
        self.recipes = recipes
        self.allow_any_channel = allow_any_channel
        self.page_count = max(1, (len(recipes) + 24) // 25)
        self.page = max(0, min(page, self.page_count - 1))
        start = self.page * 25
        self.add_item(
            RecipeAdminSelect(
                recipes[start : start + 25],
                allow_any_channel=self.allow_any_channel,
            )
        )
        if self.page > 0:
            previous = discord.ui.Button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)

            async def previous_callback(interaction: discord.Interaction) -> None:
                await interaction.response.edit_message(
                    content=(
                        "Выберите рецепт для изменения, копирования, отключения или просмотра версий "
                        f"• страница {self.page}/{self.page_count}:"
                    ),
                    view=RecipeAdminSelectView(
                        self.recipes,
                        self.page - 1,
                        allow_any_channel=self.allow_any_channel,
                    ),
                )

            previous.callback = previous_callback
            self.add_item(previous)
        if self.page + 1 < self.page_count:
            following = discord.ui.Button(label="Далее", emoji="▶️", style=discord.ButtonStyle.secondary)

            async def following_callback(interaction: discord.Interaction) -> None:
                await interaction.response.edit_message(
                    content=(
                        "Выберите рецепт для изменения, копирования, отключения или просмотра версий "
                        f"• страница {self.page + 2}/{self.page_count}:"
                    ),
                    view=RecipeAdminSelectView(
                        self.recipes,
                        self.page + 1,
                        allow_any_channel=self.allow_any_channel,
                    ),
                )

            following.callback = following_callback
            self.add_item(following)


class PlanCreateModal(discord.ui.Modal):
    def __init__(self, recipe_id: int, *, allow_any_channel: bool = False) -> None:
        super().__init__(title="Новый план крафта", timeout=600)
        self.recipe_id = recipe_id
        self.allow_any_channel = allow_any_channel
        self.attempts = discord.ui.TextInput(
            label="Количество попыток крафта",
            placeholder="100",
            min_length=1,
            max_length=20,
        )
        self.responsible = discord.ui.TextInput(
            label="Ответственный: упоминание или Discord ID",
            placeholder="@Участник или 902235631952998410",
            min_length=15,
            max_length=60,
        )
        self.add_item(self.attempts)
        self.add_item(self.responsible)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await craft_channel_only(interaction, allow_any_channel=self.allow_any_channel):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        plan: dict[str, Any] | None = None
        public_message: discord.Message | None = None
        bound = False
        try:
            responsible_id = parse_user_id(self.responsible.value)
            member = interaction.guild.get_member(responsible_id)
            if member is None:
                member = await interaction.guild.fetch_member(responsible_id)
            target_channel_id = CRAFT_CHANNEL_ID
            target_channel = await _get_channel(interaction.client, target_channel_id)
            plan = await asyncio.to_thread(
                storage.craft_create_plan,
                guild_id=interaction.guild.id,
                recipe_id=self.recipe_id,
                channel_id=target_channel_id,
                attempts_total=parse_positive_int(self.attempts.value),
                responsible_id=member.id,
                responsible_display=member.display_name,
                created_by_id=interaction.user.id,
                created_by_display=display_name(interaction.user),
            )
            public_message = await target_channel.send(embed=plan_embed(plan), view=CraftPlanView(plan))
            thread_name = f"крафт-{plan['id']}-{plan['recipe']['product_name']}"[:100]
            thread = await public_message.create_thread(name=thread_name, auto_archive_duration=1440)
            plan = await asyncio.to_thread(
                storage.craft_bind_plan_message,
                plan_id=int(plan["id"]),
                channel_id=target_channel_id,
                message_id=public_message.id,
                thread_id=thread.id,
            )
            bound = True
            try:
                await public_message.edit(embed=plan_embed(plan), view=CraftPlanView(plan))
                await thread.send(
                    f"Подробный журнал плана **#{plan['id']}**. Ответственный: <@{plan['responsible_id']}>."
                )
            except discord.DiscordException:
                traceback.print_exc()
            wake_craft_worker()
            await interaction.followup.send(
                f"План **#{plan['id']}** создан: {public_message.jump_url}",
                ephemeral=True,
            )
        except Exception as exc:
            if plan is not None and not bound:
                await asyncio.to_thread(storage.craft_delete_unbound_plan, int(plan["id"]))
                if public_message is not None:
                    try:
                        await public_message.delete()
                    except discord.DiscordException:
                        traceback.print_exc()
            await send_interaction_error(interaction, exc)


class RecipeSelect(discord.ui.Select):
    def __init__(self, recipes: list[dict[str, Any]], *, allow_any_channel: bool = False) -> None:
        self.allow_any_channel = allow_any_channel
        options = [
            discord.SelectOption(
                label=str(recipe["product_name"])[:100],
                value=str(recipe["id"]),
                description=(
                    f"{recipe['duration_minutes_per_unit']} мин/шт · цикл {recipe['max_batch_size']} · "
                    f"{len(recipe['materials'])} материалов"
                )[:100],
            )
            for recipe in recipes
        ]
        super().__init__(placeholder="Выберите рецепт", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            PlanCreateModal(
                int(self.values[0]),
                allow_any_channel=self.allow_any_channel,
            )
        )


class RecipeSelectView(discord.ui.View):
    def __init__(
        self,
        recipes: list[dict[str, Any]],
        page: int = 0,
        *,
        allow_any_channel: bool = False,
    ) -> None:
        super().__init__(timeout=300)
        self.recipes = recipes
        self.allow_any_channel = allow_any_channel
        self.page_count = max(1, (len(recipes) + 24) // 25)
        self.page = max(0, min(page, self.page_count - 1))
        start = self.page * 25
        self.add_item(
            RecipeSelect(
                recipes[start : start + 25],
                allow_any_channel=self.allow_any_channel,
            )
        )
        if self.page > 0:
            previous = discord.ui.Button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)

            async def previous_callback(interaction: discord.Interaction) -> None:
                await interaction.response.edit_message(
                    content=f"Выберите рецепт для нового плана • страница {self.page}/{self.page_count}:",
                    view=RecipeSelectView(
                        self.recipes,
                        self.page - 1,
                        allow_any_channel=self.allow_any_channel,
                    ),
                )

            previous.callback = previous_callback
            self.add_item(previous)
        if self.page + 1 < self.page_count:
            following = discord.ui.Button(label="Далее", emoji="▶️", style=discord.ButtonStyle.secondary)

            async def following_callback(interaction: discord.Interaction) -> None:
                await interaction.response.edit_message(
                    content=f"Выберите рецепт для нового плана • страница {self.page + 2}/{self.page_count}:",
                    view=RecipeSelectView(
                        self.recipes,
                        self.page + 1,
                        allow_any_channel=self.allow_any_channel,
                    ),
                )

            following.callback = following_callback
            self.add_item(following)


class CraftMenuView(discord.ui.View):
    def __init__(
        self,
        *,
        allow_any_channel: bool = False,
        requester_id: int | None = None,
        back_to_tvrs: bool = False,
    ) -> None:
        super().__init__(timeout=900)
        self.allow_any_channel = allow_any_channel
        self.requester_id = requester_id
        if back_to_tvrs:
            back = discord.ui.Button(label="Назад", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)

            async def back_callback(interaction: discord.Interaction) -> None:
                from modules.tvrs import open_tvrs_hub

                await open_tvrs_hub(interaction)

            back.callback = back_callback
            self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.requester_id is not None and interaction.user.id != self.requester_id:
            await interaction.response.send_message("Это приватное меню открыто не для вас.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Текущие крафты", emoji="🏗️", style=discord.ButtonStyle.primary)
    async def current(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await craft_channel_only(interaction, allow_any_channel=self.allow_any_channel):
            return
        plans = await asyncio.to_thread(storage.craft_active_plans, interaction.guild.id, 50)
        await interaction.response.send_message(embed=active_plans_embed(plans), ephemeral=True)

    @discord.ui.button(label="Новый план", emoji="➕", style=discord.ButtonStyle.success)
    async def new_plan(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await craft_channel_only(interaction, allow_any_channel=self.allow_any_channel):
            return
        recipes = await asyncio.to_thread(storage.craft_list_recipes, interaction.guild.id, active_only=True, limit=100)
        if not recipes:
            await interaction.response.send_message(
                "Сначала добавьте хотя бы один рецепт через кнопку **«Рецепты»**.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Выберите рецепт для нового плана • страница 1/{max(1, (len(recipes) + 24) // 25)}:",
            view=RecipeSelectView(recipes, allow_any_channel=self.allow_any_channel),
            ephemeral=True,
        )

    @discord.ui.button(label="Рецепты", emoji="📚", style=discord.ButtonStyle.secondary)
    async def recipes(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await craft_channel_only(interaction, allow_any_channel=self.allow_any_channel):
            return
        recipes = await asyncio.to_thread(storage.craft_list_recipes, interaction.guild.id, active_only=True, limit=100)
        await interaction.response.send_message(
            embed=recipe_list_embed(recipes),
            view=RecipeManageView(allow_any_channel=self.allow_any_channel),
            ephemeral=True,
        )


class RecipeManageView(discord.ui.View):
    def __init__(self, *, allow_any_channel: bool = False) -> None:
        super().__init__(timeout=900)
        self.allow_any_channel = allow_any_channel

    @discord.ui.button(label="Добавить рецепт", emoji="➕", style=discord.ButtonStyle.success)
    async def add_recipe(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await craft_channel_only(interaction, allow_any_channel=self.allow_any_channel):
            return
        await interaction.response.send_modal(
            RecipeModal(allow_any_channel=self.allow_any_channel)
        )

    @discord.ui.button(label="Управление рецептами", emoji="⚙️", style=discord.ButtonStyle.primary)
    async def manage_recipes(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await craft_channel_only(interaction, allow_any_channel=self.allow_any_channel):
            return
        recipes = await asyncio.to_thread(
            storage.craft_list_recipes,
            interaction.guild.id,
            active_only=False,
            limit=200,
        )
        if not recipes:
            await interaction.response.send_message("Рецептов пока нет.", ephemeral=True)
            return
        await interaction.response.send_message(
            (
                "Выберите рецепт для изменения, копирования, отключения или просмотра версий "
                f"• страница 1/{max(1, (len(recipes) + 24) // 25)}:"
            ),
            view=RecipeAdminSelectView(
                recipes,
                allow_any_channel=self.allow_any_channel,
            ),
            ephemeral=True,
        )


class PurchaseModal(discord.ui.Modal):
    def __init__(self, plan_id: int, material: dict[str, Any]) -> None:
        super().__init__(title=f"Закупка: {str(material['material_name'])[:32]}", timeout=600)
        self.plan_id = plan_id
        self.material_id = int(material["id"])
        remaining = max(0, int(material["required_total"]) - int(material["stock_quantity"]))
        self.quantity = discord.ui.TextInput(
            label="Количество материала",
            placeholder=f"Осталось собрать: {format_quantity(remaining)}",
            min_length=1,
            max_length=30,
        )
        self.total_cost = discord.ui.TextInput(
            label="Общая стоимость партии",
            placeholder="Например: 250 000. Если бесплатно — 0",
            min_length=1,
            max_length=30,
        )
        self.finance_code = discord.ui.TextInput(
            label="Код снятия из /finance",
            placeholder="Необязательно. Например: KXRM",
            required=False,
            max_length=20,
        )
        self.add_item(self.quantity)
        self.add_item(self.total_cost)
        self.add_item(self.finance_code)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await craft_channel_only(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await asyncio.to_thread(
                storage.craft_add_purchase,
                guild_id=interaction.guild.id,
                plan_id=self.plan_id,
                plan_material_id=self.material_id,
                quantity=parse_positive_int(self.quantity.value),
                total_cost=parse_positive_int(self.total_cost.value, allow_zero=True),
                finance_code=self.finance_code.value,
                actor_id=interaction.user.id,
                actor_display=display_name(interaction.user),
            )
            await update_plan_message(interaction.client, self.plan_id)
            wake_craft_worker()
            message = f"Закупка записана и добавлена на склад. Действие: **#{result['action_id']}**."
            if not self.finance_code.value.strip():
                message += " ⚠️ Финансовый код не указан — связь со снятием не подтверждена."
            if result["stage_changed"]:
                message += f"\n\nВсе материалы собраны. Можно начинать производство. Ответственный: <@{result['plan']['responsible_id']}>."
                await interaction.channel.send(
                    f"<@{result['plan']['responsible_id']}>, все материалы для плана **#{self.plan_id}** собраны. "
                    "Можно ставить первый цикл крафта.",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            await interaction.followup.send(message, ephemeral=True)
        except Exception as exc:
            await send_interaction_error(interaction, exc)


class InventoryModal(discord.ui.Modal):
    def __init__(self, plan: dict[str, Any]) -> None:
        super().__init__(title="Сверка склада", timeout=900)
        self.plan = plan
        example = "\n".join(f"{item['material_name']} = {item['stock_quantity']}" for item in plan["materials"])
        self.materials = discord.ui.TextInput(
            label="Каждый материал: название = количество",
            placeholder="Железная руда = 5000\nСеребряная руда = 3000"[:100],
            default=example[:4000],
            style=discord.TextStyle.paragraph,
            min_length=3,
            max_length=4000,
        )
        self.product = discord.ui.TextInput(
            label=(f"Продукт на складе: {plan['recipe']['product_name']}")[:45],
            placeholder="0",
            default=str(plan.get("product_stock") or 0),
            min_length=1,
            max_length=30,
        )
        self.note = discord.ui.TextInput(
            label="Комментарий",
            placeholder="Необязательно",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
        )
        self.add_item(self.materials)
        self.add_item(self.product)
        self.add_item(self.note)

    def parse_inventory(self) -> dict[int, int]:
        by_name = {str(item["material_name"]).casefold(): int(item["id"]) for item in self.plan["materials"]}
        result: dict[int, int] = {}
        for raw_line in self.materials.value.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = re.fullmatch(r"(.+?)\s*[:=]\s*([0-9\s.,_'’]+)", line)
            if not match:
                raise ValueError("craft_inventory_incomplete")
            material_id = by_name.get(match.group(1).strip().casefold())
            if material_id is None:
                raise ValueError("craft_inventory_incomplete")
            result[material_id] = parse_positive_int(match.group(2), allow_zero=True)
        return result

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await craft_channel_only(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await asyncio.to_thread(
                storage.craft_inventory_check,
                guild_id=interaction.guild.id,
                plan_id=int(self.plan["id"]),
                material_quantities=self.parse_inventory(),
                product_quantity=parse_positive_int(self.product.value, allow_zero=True),
                note=self.note.value,
                actor_id=interaction.user.id,
                actor_display=display_name(interaction.user),
            )
            await update_plan_message(interaction.client, int(self.plan["id"]))
            wake_craft_worker()
            text = f"Сверка склада сохранена. Действие: **#{result['action_id']}**."
            if result["stage_changed"]:
                text += " Все материалы подтверждены — стадия производства открыта."
                await interaction.channel.send(
                    f"<@{result['plan']['responsible_id']}>, сверка подтвердила все материалы для "
                    f"плана **#{self.plan['id']}**. Можно ставить первый цикл крафта.",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            await interaction.followup.send(text, ephemeral=True)
        except Exception as exc:
            await send_interaction_error(interaction, exc)


async def start_batch(interaction: discord.Interaction, plan_id: int, quantity: int) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        changed = await asyncio.to_thread(storage.craft_complete_due_batches)
        for changed_id in changed:
            await update_plan_message(interaction.client, changed_id)
        plan = await asyncio.to_thread(storage.craft_get_plan, plan_id, interaction.guild.id)
        if plan is None:
            raise ValueError("craft_plan_not_found")
        result = await asyncio.to_thread(
            storage.craft_start_batch,
            guild_id=interaction.guild.id,
            plan_id=plan_id,
            quantity=quantity,
            actor_id=interaction.user.id,
            actor_display=display_name(interaction.user),
            log_channel_id=FINANCE_EVENT_LOG_CHANNEL_ID,
            admin_user_id=FINANCE_ADMIN_USER_ID,
        )
        await delete_plan_reminders(interaction.client, plan)
        await update_plan_message(interaction.client, plan_id)
        wake_notification_worker()
        wake_craft_worker()
        batch = result["batch"]
        text = (
            f"Цикл на **{quantity} шт.** записан. Завершение: {discord_time(batch['due_at'])} "
            f"({discord_time(batch['due_at'], 'R')}). Действие: **#{result['action_id']}**."
        )
        if result.get("finance_event_id"):
            text += "\nРасход казны на производство записан автоматически — отдельно нажимать «Снял» не нужно."
        await interaction.followup.send(text, ephemeral=True)
    except Exception as exc:
        await send_interaction_error(interaction, exc)


class BatchQuantityModal(discord.ui.Modal):
    def __init__(self, plan_id: int, maximum: int) -> None:
        super().__init__(title="Поставлен цикл крафта", timeout=300)
        self.plan_id = plan_id
        self.maximum = maximum
        self.quantity = discord.ui.TextInput(
            label=f"Количество, максимум {maximum}",
            placeholder=str(maximum),
            min_length=1,
            max_length=20,
        )
        self.add_item(self.quantity)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        quantity = parse_positive_int(self.quantity.value)
        if quantity > self.maximum:
            await interaction.response.send_message(f"Максимум для этого цикла: **{self.maximum} шт.**", ephemeral=True)
            return
        await start_batch(interaction, self.plan_id, quantity)


class FinalOutputModal(discord.ui.Modal):
    def __init__(self, plan_id: int, maximum: int) -> None:
        super().__init__(title="Итог производства", timeout=300)
        self.plan_id = plan_id
        self.quantity = discord.ui.TextInput(
            label=f"Получено продукта, максимум {maximum}",
            placeholder=str(maximum),
            min_length=1,
            max_length=30,
        )
        self.add_item(self.quantity)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            plan = await asyncio.to_thread(
                storage.craft_set_final_output,
                guild_id=interaction.guild.id,
                plan_id=self.plan_id,
                product_quantity=parse_positive_int(self.quantity.value, allow_zero=True),
                actor_id=interaction.user.id,
                actor_display=display_name(interaction.user),
            )
            await update_plan_message(interaction.client, self.plan_id)
            wake_craft_worker()
            if plan["stage"] == "completed":
                text = f"Итог сохранён: успешных продуктов нет. План завершён. Действие: **#{plan['action_id']}**."
            else:
                text = (
                    f"Итог сохранён: **{format_quantity(plan['final_product_qty'])} шт.**\n"
                    f"Действие: **#{plan['action_id']}**.\n"
                    f"<@{plan['responsible_id']}>, укажите примерную цену и выставьте весь продукт на маркет."
                )
                try:
                    channel = await _get_channel(interaction.client, int(plan["channel_id"]))
                    await channel.send(
                        f"<@{plan['responsible_id']}>, производство по плану **#{plan['id']}** завершено. "
                        f"Получено **{format_quantity(plan['final_product_qty'])} шт. {plan['recipe']['product_name']}**. "
                        "Пора выставить продукт на маркет.",
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except discord.DiscordException:
                    traceback.print_exc()
            await interaction.followup.send(text, ephemeral=True)
        except Exception as exc:
            await send_interaction_error(interaction, exc)


class PriceModal(discord.ui.Modal):
    def __init__(self, plan_id: int) -> None:
        super().__init__(title="Примерная цена продажи", timeout=300)
        self.plan_id = plan_id
        self.price = discord.ui.TextInput(
            label="Ожидаемая цена за 1 шт.",
            placeholder="Например: 125 000",
            min_length=1,
            max_length=30,
        )
        self.add_item(self.price)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            plan = await asyncio.to_thread(
                storage.craft_set_estimated_price,
                guild_id=interaction.guild.id,
                plan_id=self.plan_id,
                unit_price=parse_positive_int(self.price.value),
                actor_id=interaction.user.id,
                actor_display=display_name(interaction.user),
            )
            await update_plan_message(interaction.client, self.plan_id)
            wake_craft_worker()
            await interaction.followup.send(
                f"Примерная цена продажи сохранена. Действие: **#{plan['action_id']}**.",
                ephemeral=True,
            )
        except Exception as exc:
            await send_interaction_error(interaction, exc)


class ListingModal(discord.ui.Modal):
    def __init__(self, plan_id: int, remaining: int) -> None:
        super().__init__(title="Выставление на маркет", timeout=300)
        self.plan_id = plan_id
        self.quantity = discord.ui.TextInput(
            label=f"Сколько выставлено, осталось {remaining}",
            placeholder=str(remaining),
            min_length=1,
            max_length=30,
        )
        self.add_item(self.quantity)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            plan = await asyncio.to_thread(
                storage.craft_add_market_listing,
                guild_id=interaction.guild.id,
                plan_id=self.plan_id,
                quantity=parse_positive_int(self.quantity.value),
                actor_id=interaction.user.id,
                actor_display=display_name(interaction.user),
            )
            await update_plan_message(interaction.client, self.plan_id)
            wake_craft_worker()
            remaining = int(plan["final_product_qty"] or 0) - int(plan["market_listed_qty"])
            text = (
                f"Выставление записано. Действие: **#{plan['action_id']}**. "
                f"Осталось выставить: **{format_quantity(remaining)} шт.**"
            )
            if plan["stage"] == "selling":
                text += "\nВесь продукт на маркете. Теперь записывайте продажи по мере их появления."
            await interaction.followup.send(text, ephemeral=True)
        except Exception as exc:
            await send_interaction_error(interaction, exc)


class FinanceLinkView(discord.ui.View):
    def __init__(self, requester_id: int) -> None:
        super().__init__(timeout=300)
        self.requester_id = int(requester_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Эта личная кнопка открыта не для вас.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Открыть казну", emoji="💼", style=discord.ButtonStyle.primary)
    async def open_finance(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Казна работает только на сервере.", ephemeral=True)
            return
        state = await asyncio.to_thread(storage.finance_get_latest_state, interaction.guild.id)
        await interaction.response.send_message(
            embed=finance_panel_embed(state),
            view=FinancePanelView(allow_any_channel=True, requester_id=interaction.user.id),
            ephemeral=True,
        )


class SaleModal(discord.ui.Modal):
    def __init__(self, plan_id: int, remaining: int) -> None:
        super().__init__(title="Данные продажи с маркета", timeout=300)
        self.plan_id = plan_id
        self.quantity = discord.ui.TextInput(
            label=f"Сколько продано, осталось {remaining}",
            placeholder="Например: 3",
            min_length=1,
            max_length=30,
        )
        self.total_amount = discord.ui.TextInput(
            label="Получено денег за эту продажу",
            placeholder="Например: 375 000",
            min_length=1,
            max_length=30,
        )
        self.add_item(self.quantity)
        self.add_item(self.total_amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            quantity = parse_positive_int(self.quantity.value)
            amount = parse_positive_int(self.total_amount.value)
            plan = await asyncio.to_thread(
                storage.craft_add_sale,
                guild_id=interaction.guild.id,
                plan_id=self.plan_id,
                quantity=quantity,
                total_amount=amount,
                actor_id=interaction.user.id,
                actor_display=display_name(interaction.user),
            )
            await update_plan_message(interaction.client, self.plan_id)
            wake_craft_worker()
            remaining = int(plan["final_product_qty"] or 0) - int(plan["sold_qty"])
            text = (
                f"Продажа записана: **{format_quantity(quantity)} шт.** за **{money_text(amount)}**. "
                f"Действие: **#{plan['action_id']}**.\n"
                f"Осталось продать: **{format_quantity(remaining)} шт.**\n\n"
                f"Откройте казну кнопкой ниже или вызовите `/finance` здесь → **«Положил»** "
                f"и внесите **{money_text(amount)}** в казну."
            )
            if plan["stage"] == "completed":
                text += "\n\nВсе предметы проданы. План завершён."
            await interaction.followup.send(
                text,
                view=FinanceLinkView(interaction.user.id),
                ephemeral=True,
            )
        except Exception as exc:
            await send_interaction_error(interaction, exc)


class CraftPlanView(discord.ui.View):
    def __init__(self, plan: dict[str, Any]) -> None:
        super().__init__(timeout=None)
        self.plan_id = int(plan["id"])
        stage = str(plan["stage"])
        row = 0
        if stage == "procurement":
            for material in plan["materials"]:
                if int(material["stock_quantity"]) >= int(material["required_total"]):
                    continue
                button = discord.ui.Button(
                    label=("Добавить " + str(material["material_name"]))[:80],
                    emoji="➕",
                    style=discord.ButtonStyle.success,
                    custom_id=f"craft:buy:{self.plan_id}:{material['id']}",
                    row=min(row // 5, 3),
                )

                async def purchase_callback(
                    interaction: discord.Interaction,
                    material_data: dict[str, Any] = dict(material),
                ) -> None:
                    if not await craft_channel_only(interaction):
                        return
                    await interaction.response.send_modal(PurchaseModal(self.plan_id, material_data))

                button.callback = purchase_callback
                self.add_item(button)
                row += 1
        elif stage == "crafting":
            active = plan.get("active_batch") is not None
            remaining = max(0, int(plan["attempts_total"]) - int(plan["attempts_queued"]))
            max_batch = min(int(plan["recipe"]["max_batch_size"]), remaining)
            if remaining > 0:
                one = discord.ui.Button(
                    label="Поставил 1 шт.",
                    emoji="▶️",
                    style=discord.ButtonStyle.primary,
                    custom_id=f"craft:batch1:{self.plan_id}",
                    disabled=active,
                )

                async def one_callback(interaction: discord.Interaction) -> None:
                    if not await craft_channel_only(interaction):
                        return
                    await start_batch(interaction, self.plan_id, 1)

                one.callback = one_callback
                self.add_item(one)
                if max_batch > 1:
                    maximum = discord.ui.Button(
                        label=f"Поставил {max_batch} шт.",
                        emoji="⏩",
                        style=discord.ButtonStyle.success,
                        custom_id=f"craft:batchmax:{self.plan_id}",
                        disabled=active,
                    )

                    async def max_callback(interaction: discord.Interaction, qty: int = max_batch) -> None:
                        if not await craft_channel_only(interaction):
                            return
                        await start_batch(interaction, self.plan_id, qty)

                    maximum.callback = max_callback
                    self.add_item(maximum)
                    custom = discord.ui.Button(
                        label="Другое количество",
                        emoji="🔢",
                        style=discord.ButtonStyle.secondary,
                        custom_id=f"craft:batchcustom:{self.plan_id}",
                        disabled=active,
                    )

                    async def custom_callback(interaction: discord.Interaction, maximum_value: int = max_batch) -> None:
                        if not await craft_channel_only(interaction):
                            return
                        await interaction.response.send_modal(BatchQuantityModal(self.plan_id, maximum_value))

                    custom.callback = custom_callback
                    self.add_item(custom)
        elif stage == "awaiting_output":
            output = discord.ui.Button(
                label="Указать результат",
                emoji="📦",
                style=discord.ButtonStyle.success,
                custom_id=f"craft:output:{self.plan_id}",
            )

            async def output_callback(interaction: discord.Interaction) -> None:
                if not await craft_channel_only(interaction):
                    return
                await interaction.response.send_modal(FinalOutputModal(self.plan_id, int(plan["attempts_completed"])))

            output.callback = output_callback
            self.add_item(output)
        elif stage in {"listing", "selling"}:
            price = discord.ui.Button(
                label="Цена за 1 шт.",
                emoji="🏷️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"craft:price:{self.plan_id}",
            )

            async def price_callback(interaction: discord.Interaction) -> None:
                if not await craft_channel_only(interaction):
                    return
                current = await asyncio.to_thread(storage.craft_get_plan, self.plan_id, interaction.guild.id)
                if current is None or not can_manage_market(interaction, current):
                    await interaction.response.send_message("Эту кнопку использует ответственный за план или администратор.", ephemeral=True)
                    return
                await interaction.response.send_modal(PriceModal(self.plan_id))

            price.callback = price_callback
            self.add_item(price)
            if stage == "listing":
                remaining = int(plan["final_product_qty"] or 0) - int(plan["market_listed_qty"])
                listing = discord.ui.Button(
                    label="Выставил на маркет",
                    emoji="🛍️",
                    style=discord.ButtonStyle.primary,
                    custom_id=f"craft:list:{self.plan_id}",
                )

                async def listing_callback(interaction: discord.Interaction, remaining_qty: int = remaining) -> None:
                    if not await craft_channel_only(interaction):
                        return
                    current = await asyncio.to_thread(storage.craft_get_plan, self.plan_id, interaction.guild.id)
                    if current is None or not can_manage_market(interaction, current):
                        await interaction.response.send_message("Выставление отмечает ответственный за план или администратор.", ephemeral=True)
                        return
                    await interaction.response.send_modal(ListingModal(self.plan_id, remaining_qty))

                listing.callback = listing_callback
                self.add_item(listing)
            else:
                remaining = int(plan["final_product_qty"] or 0) - int(plan["sold_qty"])
                sale = discord.ui.Button(
                    label="Записать продажу",
                    emoji="💰",
                    style=discord.ButtonStyle.success,
                    custom_id=f"craft:sale:{self.plan_id}",
                )

                async def sale_callback(interaction: discord.Interaction, remaining_qty: int = remaining) -> None:
                    if not await craft_channel_only(interaction):
                        return
                    await interaction.response.send_modal(SaleModal(self.plan_id, remaining_qty))

                sale.callback = sale_callback
                self.add_item(sale)

        if stage in {"procurement", "crafting", "awaiting_output", "listing", "selling"}:
            inventory = discord.ui.Button(
                label="Сверка склада",
                emoji="📦",
                style=discord.ButtonStyle.secondary,
                custom_id=f"craft:inventory:{self.plan_id}",
                row=4,
            )

            async def inventory_callback(interaction: discord.Interaction) -> None:
                if not await craft_channel_only(interaction):
                    return
                current = await asyncio.to_thread(storage.craft_get_plan, self.plan_id, interaction.guild.id)
                if current is None:
                    await interaction.response.send_message("План не найден.", ephemeral=True)
                    return
                await interaction.response.send_modal(InventoryModal(current))

            inventory.callback = inventory_callback
            self.add_item(inventory)


async def dispatch_craft_events(bot: commands.Bot) -> None:
    events = await asyncio.to_thread(storage.craft_pending_events, 50)
    for event in events:
        try:
            thread = await _get_channel(bot, int(event["thread_id"]))
            if isinstance(thread, discord.Thread) and thread.archived:
                await thread.edit(archived=False)
            message = await thread.send(embed=event_log_embed(event))
            await asyncio.to_thread(storage.craft_mark_event_sent, int(event["id"]), message.id)
        except asyncio.CancelledError:
            raise
        except discord.DiscordException:
            traceback.print_exc()


def quiet_hours(local_now: datetime) -> bool:
    if CRAFT_QUIET_START_HOUR == CRAFT_QUIET_END_HOUR:
        return False
    if CRAFT_QUIET_START_HOUR < CRAFT_QUIET_END_HOUR:
        return CRAFT_QUIET_START_HOUR <= local_now.hour < CRAFT_QUIET_END_HOUR
    return local_now.hour >= CRAFT_QUIET_START_HOUR or local_now.hour < CRAFT_QUIET_END_HOUR


async def send_craft_reminders(bot: commands.Bot, guild_id: int) -> None:
    now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(LOCAL_TZ)
    if quiet_hours(local_now):
        return
    candidates = await asyncio.to_thread(storage.craft_reminder_candidates, guild_id)
    for plan in candidates:
        try:
            due = datetime.fromisoformat(str(plan["last_due_at"]))
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            overdue_minutes = int((now_utc - due.astimezone(timezone.utc)).total_seconds() // 60)
            if overdue_minutes < 10:
                continue
            mention_everyone = False
            if overdue_minutes >= 60:
                slot = max(1, overdue_minutes // 60)
                reminder_key = f"b{plan['last_batch_id']}:h{slot}"
                mention_everyone = slot % 2 == 0
            elif overdue_minutes >= 30:
                reminder_key = f"b{plan['last_batch_id']}:m30"
                mention_everyone = True
            else:
                reminder_key = f"b{plan['last_batch_id']}:m10"
            claimed = await asyncio.to_thread(storage.craft_claim_reminder, int(plan["id"]), reminder_key)
            if not claimed:
                continue
            prefix = "@everyone " if mention_everyone else ""
            content = (
                f"{prefix}⚙️ Пора поставить следующий крафт по плану **#{plan['id']}**. "
                f"Ответственный: <@{plan['responsible_id']}>. Предыдущий цикл завершился "
                f"{discord_time(plan['last_due_at'], 'R')}."
            )
            channel = await _get_channel(bot, int(plan["channel_id"]))
            try:
                message = await channel.send(
                    content,
                    allowed_mentions=discord.AllowedMentions(everyone=mention_everyone, users=True, roles=False),
                )
            except Exception:
                await asyncio.to_thread(storage.craft_release_reminder, int(plan["id"]), reminder_key)
                raise
            await asyncio.to_thread(storage.craft_set_reminder_message, int(plan["id"]), reminder_key, message.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()


async def publish_completion_summaries(bot: commands.Bot, guild_id: int) -> None:
    plans = await asyncio.to_thread(storage.craft_completion_candidates, guild_id)
    for plan in plans:
        try:
            channel = await _get_channel(bot, int(plan["channel_id"]))
            message = await channel.send(embed=completion_embed(plan))
            await asyncio.to_thread(storage.craft_set_completion_message, int(plan["id"]), message.id)
            await update_plan_message(bot, int(plan["id"]))
        except asyncio.CancelledError:
            raise
        except discord.DiscordException:
            traceback.print_exc()


async def register_active_plan_views(bot: commands.Bot) -> None:
    for guild in bot.guilds:
        plans = await asyncio.to_thread(storage.craft_active_plans, guild.id, 100)
        for plan in plans:
            message_id = plan.get("message_id")
            if not message_id or int(message_id) in _registered_plan_messages:
                continue
            bot.add_view(CraftPlanView(plan), message_id=int(message_id))
            _registered_plan_messages.add(int(message_id))


async def craft_worker(bot: commands.Bot) -> None:
    global _craft_wakeup
    _craft_wakeup = asyncio.Event()
    while not bot.is_closed():
        try:
            for guild in bot.guilds:
                await migrate_active_plan_channels(bot, guild)
            changed = await asyncio.to_thread(storage.craft_complete_due_batches)
            for plan_id in changed:
                await update_plan_message(bot, plan_id)
            await dispatch_craft_events(bot)
            for guild in bot.guilds:
                await send_craft_reminders(bot, guild.id)
                await publish_completion_summaries(bot, guild.id)
            await register_active_plan_views(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
        try:
            await asyncio.wait_for(_craft_wakeup.wait(), timeout=30)
            _craft_wakeup.clear()
        except asyncio.TimeoutError:
            pass


def setup_craft(
    bot: commands.Bot,
    remember_command_activity: Callable[[discord.Interaction, str, str], None],
) -> None:
    @bot.tree.command(name="craft", description="Открыть центр крафтов Товарищества")
    async def craft(interaction: discord.Interaction) -> None:
        if not await craft_channel_only(interaction):
            return
        remember_command_activity(interaction, "command_craft", "/craft")
        embed = await asyncio.to_thread(craft_menu_embed, interaction.guild.id)
        await interaction.response.send_message(embed=embed, view=CraftMenuView(), ephemeral=True)

    @bot.tree.command(name="craft-stats", description="Показать статистику производства и продаж")
    @discord.app_commands.describe(days="Период статистики в днях")
    async def craft_stats_command(
        interaction: discord.Interaction,
        days: discord.app_commands.Range[int, 1, 3650] = 30,
    ) -> None:
        if not await craft_channel_only(interaction):
            return
        stats = await asyncio.to_thread(storage.craft_stats, interaction.guild.id, int(days))
        await interaction.response.send_message(embed=craft_stats_embed(stats), ephemeral=True)

    async def craft_ready_listener() -> None:
        global _craft_worker_task
        await register_active_plan_views(bot)
        if _craft_worker_task is None or _craft_worker_task.done():
            _craft_worker_task = asyncio.create_task(craft_worker(bot), name="tmod-craft-worker")

    bot.add_listener(craft_ready_listener, "on_ready")
