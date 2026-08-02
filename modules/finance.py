import asyncio
import hashlib
import re
import secrets
import traceback
from datetime import datetime
from typing import Any, Callable

import discord
from discord.ext import commands

from persistence import finance_context as storage
from modules.control_center_config import ACTIVE_TASKS_CHANNEL_ID
from modules.craft_runtime import build_craft_stats_embed, refresh_craft_plan, wake_craft_worker
from modules.finance_config import (
    FINANCE_ADMIN_USER_ID,
    FINANCE_COMMAND_CHANNEL_ID as FINANCE_COMMAND_CHANNEL_ID,
    FINANCE_DAILY_CHANNEL_ID,
    FINANCE_EMBED_COLOR,
    FINANCE_EVENT_LOG_CHANNEL_ID,
    FINANCE_REPORT_HOUR,
    FINANCE_REPORT_MINUTE,
    GAME_CODE_ALPHABET,
    LOCAL_TZ,
)
from modules.finance_formatting import money_text, parse_money
from modules.finance_runtime import register_finance_runtime, wake_notification_worker
from modules.hub_runtime import HubSurface, register_hub_section
from modules.operations_runtime import wake_operations_worker
from modules.profile_notifications import evaluate_profile_notification
from modules.tvrs_navigation_runtime import open_tvrs_hub
from modules.technical_log import log_technical_event

_worker_task: asyncio.Task[None] | None = None
_notification_wakeup: asyncio.Event | None = None
_persistent_view_registered = False


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def actor_text(event: dict[str, Any]) -> str:
    actor_id = int(event.get("actor_id") or 0)
    display = str(event.get("actor_display") or "Пользователь")
    return f"{display} (<@{actor_id}> · `{actor_id}`)"


def local_time_text(iso_value: str | None) -> str:
    if not iso_value:
        return "—"
    try:
        value = datetime.fromisoformat(iso_value)
        return value.astimezone(LOCAL_TZ).strftime("%d.%m.%Y в %H:%M")
    except (TypeError, ValueError):
        return str(iso_value)


def prompt_marker(prompt_id: int) -> str:
    return f"finance-prompt:{prompt_id}"


def prompt_id_from_message(message: discord.Message | None) -> int | None:
    if message is None:
        return None
    for embed in message.embeds:
        footer = embed.footer.text if embed.footer else None
        if not footer:
            continue
        match = re.search(r"finance-prompt:(\d+)", footer)
        if match:
            return int(match.group(1))
    return None


def daily_prompt_embed(prompt: dict[str, Any], event: dict[str, Any] | None = None) -> discord.Embed:
    report_date = datetime.strptime(str(prompt["report_date"]), "%Y-%m-%d").strftime("%d.%m.%Y")
    filled = str(prompt.get("status")) == "filled" and event is not None
    if not filled:
        embed = discord.Embed(
            title="🏛️ Ежедневная сверка казны",
            description=(
                f"Настало время зафиксировать фактический остаток казны за **{report_date}**.\n"
                "Нажмите **«Ввести»** и укажите сумму, которая находится в казне прямо сейчас."
            ),
            color=FINANCE_EMBED_COLOR,
        )
        embed.add_field(name="Статус", value="🟡 Ожидает заполнения", inline=True)
        embed.add_field(name="Кто может заполнить", value="Любой участник этого канала", inline=True)
        embed.add_field(
            name="Важно",
            value="Указывайте фактическое число без округления. Разделители разрядов разрешены.",
            inline=False,
        )
    else:
        embed = discord.Embed(
            title="✅ Ежедневная сверка казны заполнена",
            description=f"Фактический остаток казны за **{report_date}** сохранён.",
            color=discord.Color.green(),
        )
        embed.add_field(name="В казне", value=f"**{money_text(event.get('amount'))}**", inline=True)
        embed.add_field(name="Заполнил", value=actor_text(event), inline=True)
        embed.add_field(name="Время", value=local_time_text(event.get("created_at")), inline=False)
    embed.set_footer(text=f"Казначейство Товарищества • {prompt_marker(int(prompt['id']))}")
    return embed


def finance_panel_embed(state: dict[str, Any]) -> discord.Embed:
    estimate = state.get("estimated_balance")
    report = state.get("latest_report")
    movements = int(state.get("movements_after_report") or 0)
    embed = discord.Embed(
        title="💼 Финансы Товарищества",
        description=(
            "Здесь отображается расчётный остаток по последней точной сверке и всем "
            "зафиксированным после неё операциям. Технические расходы игры могут сделать "
            "фактическую сумму другой."
        ),
        color=FINANCE_EMBED_COLOR,
    )
    embed.add_field(name="Примерно в казне", value=f"**{money_text(estimate)}**", inline=False)
    if report is None:
        report_value = "Точных отчётов ещё нет. Первая сверка задаст исходный остаток."
    else:
        report_kind = "вечерний отчёт" if report.get("event_kind") == "daily" else "межотчёт"
        report_value = (
            f"{report_kind.capitalize()} от **{local_time_text(report.get('created_at'))}**: "
            f"**{money_text(report.get('amount'))}**"
        )
    embed.add_field(name="Последняя точная сверка", value=report_value, inline=False)
    embed.add_field(name="Операций после сверки", value=str(movements), inline=True)
    embed.set_footer(text="Снятие и пополнение обязательно сопровождаются подробной причиной")
    return embed


def event_notification_embed(event: dict[str, Any]) -> discord.Embed:
    kind = str(event.get("event_kind") or "")
    titles = {
        "daily": ("📋 Заполнен ежедневный отчёт казны", discord.Color.green()),
        "interim": ("🔎 Выполнен межотчёт казны", discord.Color.blue()),
        "deposit": ("📥 Деньги положены в казну", discord.Color.green()),
        "withdraw": ("📤 Деньги сняты из казны", discord.Color.red()),
        "undo": ("↩️ Финансовое действие отменено", discord.Color.orange()),
    }
    title, color = titles.get(kind, ("Финансовое событие", FINANCE_EMBED_COLOR))
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Сумма", value=f"**{money_text(event.get('amount'))}**", inline=True)
    if kind in storage.FINANCE_MOVEMENT_KINDS:
        automatic_craft = str(event.get("captcha_digest") or "").startswith("craft:")
        embed.add_field(name="Остаток до", value=money_text(event.get("balance_before")), inline=True)
        embed.add_field(name="Расчётный остаток", value=money_text(event.get("balance_after")), inline=True)
        embed.add_field(name="Причина", value=str(event.get("reason") or "—")[:1024], inline=False)
        if automatic_craft:
            embed.add_field(name="Источник", value="⚙️ Автоматический расход крафта", inline=True)
        else:
            embed.add_field(name="Код для причины в игре", value=f"`{event.get('game_code')}`", inline=True)
            embed.add_field(name="Проверка", value="✅ Проверена", inline=True)
    elif kind in storage.FINANCE_SNAPSHOT_KINDS:
        label = "Вечерний отчёт" if kind == "daily" else "Межотчёт"
        embed.add_field(name="Тип сверки", value=label, inline=True)
    elif kind == "undo":
        reversed_event = event.get("reversed_event") or {}
        kind_labels = {
            "daily": "вечерний отчёт",
            "interim": "межотчёт",
            "deposit": "пополнение",
            "withdraw": "снятие",
        }
        embed.add_field(
            name="Отменённое действие",
            value=f"{kind_labels.get(reversed_event.get('event_kind'), 'финансовое действие')} `#{reversed_event.get('id', '—')}`",
            inline=True,
        )
        embed.add_field(name="Автор действия", value=actor_text(reversed_event), inline=False)
        embed.add_field(name="Остаток до отмены", value=money_text(event.get("balance_before")), inline=True)
        embed.add_field(name="Остаток после отмены", value=money_text(event.get("balance_after")), inline=True)
    actor_field = "Отменил" if kind == "undo" else "Внёс запись"
    embed.add_field(name=actor_field, value=actor_text(event), inline=False)
    embed.add_field(name="Дата и время", value=local_time_text(event.get("created_at")), inline=True)
    embed.add_field(name="Запись в БД", value=f"`#{event.get('id')}`", inline=True)
    embed.set_footer(text="Финансовый журнал Товарищества")
    return embed


def finance_audit_embed(events: list[dict[str, Any]], *, title_suffix: str | None = None) -> discord.Embed:
    title = "🔍 Финансовый аудит"
    if title_suffix:
        title += f" · {title_suffix}"
    embed = discord.Embed(title=title, color=FINANCE_EMBED_COLOR)
    if not events:
        embed.description = "Операции по заданным условиям не найдены."
        return embed
    kind_labels = {
        "daily": "отчёт",
        "interim": "межотчёт",
        "deposit": "пополнение",
        "withdraw": "снятие",
        "undo": "отмена",
    }
    lines = []
    for event in events[:15]:
        state = "↩️ отменена" if event.get("is_undone") else "✅ действует"
        code = f" · код `{event['game_code']}`" if event.get("game_code") else ""
        craft_link = ""
        plan_id = event.get("craft_purchase_plan_id") or event.get("craft_batch_plan_id")
        if plan_id:
            craft_link = f" · крафт #{plan_id}"
        action_link = f" · действие #{event['action_id']}" if event.get("action_id") else ""
        reason = str(event.get("reason") or "Без причины").replace("\n", " ")[:180]
        lines.append(
            f"**#{event['id']} · {kind_labels.get(event['event_kind'], event['event_kind'])} · {money_text(event['amount'])}**\n"
            f"{state}{code}{craft_link}{action_link} · <@{event['actor_id']}> · {local_time_text(event['created_at'])}\n"
            f"{reason}"
        )
    embed.description = "\n\n".join(lines)
    embed.set_footer(text=f"Найдено: {len(events)} · показано: {min(len(events), 15)}")
    return embed


def finance_stats_embed(stats: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=f"📊 Статистика казны · {stats['days']} дн.",
        color=FINANCE_EMBED_COLOR,
    )
    embed.add_field(name="Остаток в начале", value=money_text(stats.get("starting_balance")), inline=True)
    embed.add_field(name="Остаток сейчас", value=money_text(stats.get("ending_balance")), inline=True)
    embed.add_field(name="Чистый поток", value=money_text(stats.get("net_flow")), inline=True)
    embed.add_field(name="📥 Пополнения", value=money_text(stats.get("deposits")), inline=True)
    embed.add_field(name="📤 Снятия", value=money_text(stats.get("withdrawals")), inline=True)
    embed.add_field(name="⚙️ Из них крафты", value=money_text(stats.get("automatic_craft_expenses")), inline=True)
    embed.add_field(
        name="Операции",
        value=(
            f"Движений: **{stats['movement_count']}**\n"
            f"Сверок: **{stats['report_count']}**\n"
            f"Отмен: **{stats['undo_count']}**\n"
            f"С кодом: **{stats['coded_movement_count']}**"
        ),
        inline=True,
    )
    actors = [
        f"<@{row['actor_id']}> — {row['operations']} оп. · +{money_text(row['deposits'])} / −{money_text(row['withdrawals'])}"
        for row in stats.get("top_actors", [])
    ]
    if actors:
        embed.add_field(name="Самые активные участники", value="\n".join(actors)[:1024], inline=False)
    reasons = [
        f"**{str(row['reason'])[:90]}** — {row['operations']} оп. · {money_text(row['total'])}"
        for row in stats.get("top_reasons", [])
    ]
    if reasons:
        embed.add_field(name="Крупнейшие назначения", value="\n".join(reasons)[:1024], inline=False)
    embed.set_footer(text="Отменённые операции исключены из сумм")
    return embed


def actions_embed(actions: list[dict[str, Any]], target: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(title=f"🧾 Действия пользователя {target}", color=FINANCE_EMBED_COLOR)
    if not actions:
        embed.description = "Записанных действий пока нет."
        return embed
    status_labels = {"active": "✅ можно отменить", "undone": "↩️ отменено", "blocked": "⛔ заблокировано"}
    lines = []
    for action in actions[:20]:
        reversible = status_labels.get(str(action["status"]), str(action["status"]))
        if not int(action.get("reversible") or 0):
            reversible = "🔒 внешний эффект"
        lines.append(
            f"**Действие #{action['id']} · {action['module']}** — {reversible}\n"
            f"{action['summary']} · {local_time_text(action['created_at'])}"
        )
    embed.description = "\n\n".join(lines)
    embed.set_footer(text="Для точечной отмены: /undo action_id:<номер> reason:<причина>")
    return embed


async def _get_channel(bot: commands.Bot, channel_id: int) -> Any:
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    return await bot.fetch_channel(channel_id)


async def _edit_prompt_message(bot: commands.Bot, prompt: dict[str, Any]) -> None:
    message_id = prompt.get("message_id")
    if not message_id:
        return
    channel = await _get_channel(bot, int(prompt["channel_id"]))
    message = await channel.fetch_message(int(message_id))
    event = (
        await asyncio.to_thread(
            storage.finance_get_event,
            int(prompt["event_id"]),
        )
        if prompt.get("event_id")
        else None
    )
    await message.edit(
        embed=daily_prompt_embed(prompt, event),
        view=FinanceDailyPromptView(disabled=str(prompt.get("status")) == "filled"),
    )


async def _find_unbound_prompt_message(channel: Any, prompt_id: int) -> discord.Message | None:
    marker = prompt_marker(prompt_id)
    async for message in channel.history(limit=100):
        if not message.author.bot:
            continue
        if any(embed.footer and embed.footer.text and marker in embed.footer.text for embed in message.embeds):
            return message
    return None


async def _retire_moved_prompt_message(
    bot: commands.Bot,
    previous_prompt: dict[str, Any] | None,
    new_message: discord.Message,
) -> None:
    if not previous_prompt:
        return
    old_channel_id = int(previous_prompt.get("channel_id") or 0)
    old_message_id = int(previous_prompt.get("message_id") or 0)
    if not old_channel_id or not old_message_id or old_channel_id == new_message.channel.id:
        return
    try:
        old_channel = await _get_channel(bot, old_channel_id)
        old_message = await old_channel.fetch_message(old_message_id)
        if old_channel_id == ACTIVE_TASKS_CHANNEL_ID:
            await old_message.delete()
            return
        embed = discord.Embed(
            title="↪️ Ежедневная сверка перенесена",
            description=(
                "Эта задача теперь находится в финансовом журнале: "
                f"[открыть актуальную сверку]({new_message.jump_url})."
            ),
            color=discord.Color.light_grey(),
        )
        embed.set_footer(text="Старая кнопка отключена")
        await old_message.edit(embed=embed, view=FinanceDailyPromptView(disabled=True))
    except discord.DiscordException:
        pass


async def ensure_today_prompt(bot: commands.Bot) -> bool:
    local_now = now_local()
    due = (local_now.hour, local_now.minute) >= (FINANCE_REPORT_HOUR, FINANCE_REPORT_MINUTE)
    if not due:
        return False
    channel = await _get_channel(bot, FINANCE_DAILY_CHANNEL_ID)
    guild = getattr(channel, "guild", None)
    if guild is None:
        raise RuntimeError("FINANCE_EVENT_LOG_CHANNEL_ID не указывает на канал сервера")
    previous_prompt = next(
        (
            item
            for item in await asyncio.to_thread(
                storage.finance_recent_daily_prompts,
                guild_id=guild.id,
                limit=3,
            )
            if str(item.get("report_date")) == local_now.date().isoformat()
        ),
        None,
    )
    if previous_prompt is not None and str(previous_prompt.get("status")) == "filled":
        return True
    prompt = await asyncio.to_thread(
        storage.finance_get_or_create_daily_prompt,
        guild_id=guild.id,
        report_date=local_now.date().isoformat(),
        channel_id=channel.id,
    )
    if str(prompt.get("status")) == "filled":
        return True
    if prompt.get("message_id"):
        try:
            await _edit_prompt_message(bot, prompt)
            return True
        except discord.NotFound:
            # The audit row remains authoritative if somebody deleted the Discord message.
            # Re-publish it with the same prompt marker and bind the new message id.
            pass

    existing = await _find_unbound_prompt_message(channel, int(prompt["id"]))
    if existing is None:
        existing = await channel.send(
            embed=daily_prompt_embed(prompt),
            view=FinanceDailyPromptView(),
        )
    await asyncio.to_thread(
        storage.finance_bind_daily_prompt_message,
        prompt_id=int(prompt["id"]),
        channel_id=channel.id,
        message_id=existing.id,
    )
    await _retire_moved_prompt_message(bot, previous_prompt, existing)
    wake_operations_worker()
    return True


async def dispatch_pending_notifications(bot: commands.Bot) -> None:
    pending = await asyncio.to_thread(storage.finance_pending_notifications, 25)
    for notification in pending:
        try:
            event = await asyncio.to_thread(storage.finance_get_event, int(notification["event_id"]))
            if event is None:
                raise RuntimeError("Финансовая запись не найдена")
            destination_kind = str(notification["destination_kind"])
            destination_id = int(notification["destination_id"])
            if destination_kind == "log_channel":
                if FINANCE_EVENT_LOG_CHANNEL_ID <= 0 or destination_id != FINANCE_EVENT_LOG_CHANNEL_ID:
                    await asyncio.to_thread(
                        storage.finance_mark_notification_sent,
                        int(notification["id"]),
                        None,
                    )
                    continue
                target = await _get_channel(bot, destination_id)
            elif destination_kind == "admin_dm":
                decision = await asyncio.to_thread(
                    evaluate_profile_notification,
                    int(event.get("guild_id") or 0),
                    destination_id,
                    "finance",
                )
                if not decision.allowed:
                    if decision.resume_at is not None:
                        await asyncio.to_thread(
                            storage.finance_defer_notification,
                            int(notification["id"]),
                            decision.resume_at.isoformat(),
                        )
                    else:
                        await asyncio.to_thread(
                            storage.finance_mark_notification_sent,
                            int(notification["id"]),
                            None,
                        )
                    continue
                target = bot.get_user(destination_id) or await bot.fetch_user(destination_id)
            else:
                raise RuntimeError(f"Неизвестный адрес уведомления: {destination_kind}")
            sent = await target.send(embed=event_notification_embed(event))
            await asyncio.to_thread(storage.finance_mark_notification_sent, int(notification["id"]), sent.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await asyncio.to_thread(
                storage.finance_mark_notification_failed,
                int(notification["id"]),
                f"{type(exc).__name__}: {exc}",
            )


def _wake_notification_worker_impl() -> None:
    if _notification_wakeup is not None:
        _notification_wakeup.set()
    wake_operations_worker()


async def finance_worker(bot: commands.Bot) -> None:
    global _notification_wakeup
    _notification_wakeup = asyncio.Event()
    completed_prompt_date: str | None = None
    while not bot.is_closed():
        try:
            local_now = now_local()
            due = (local_now.hour, local_now.minute) >= (FINANCE_REPORT_HOUR, FINANCE_REPORT_MINUTE)
            if due and completed_prompt_date != local_now.date().isoformat():
                if await ensure_today_prompt(bot):
                    completed_prompt_date = local_now.date().isoformat()
            await dispatch_pending_notifications(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            traceback.print_exc()
            for guild in bot.guilds:
                await log_technical_event(
                    bot,
                    guild,
                    title="Сбой финансового рабочего цикла",
                    details=f"Автоматическая обработка будет повторена. Ошибка: `{type(exc).__name__}: {str(exc)[:700]}`",
                    dedupe_key=f"finance-worker:{type(exc).__name__}",
                )
        try:
            await asyncio.wait_for(_notification_wakeup.wait(), timeout=30)
            _notification_wakeup.clear()
        except asyncio.TimeoutError:
            pass


async def interaction_wrong_channel(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
        return True
    return False


class FinanceDailyPromptView(discord.ui.View):
    def __init__(self, *, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.enter_button.disabled = disabled

    @discord.ui.button(
        label="Ввести",
        emoji="✍️",
        style=discord.ButtonStyle.primary,
        custom_id="finance_daily_enter",
    )
    async def enter_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or interaction.channel_id != FINANCE_DAILY_CHANNEL_ID:
            await interaction.response.send_message("Эта кнопка работает только на исходной сверке в финансовом журнале.", ephemeral=True)
            return
        message_id = interaction.message.id if interaction.message else 0
        prompt = await asyncio.to_thread(
            storage.finance_get_daily_prompt_by_message,
            guild_id=interaction.guild.id,
            message_id=message_id,
        )
        if prompt is None:
            prompt_id = prompt_id_from_message(interaction.message)
            if prompt_id is not None:
                prompt = await asyncio.to_thread(storage.finance_get_daily_prompt, prompt_id)
                if prompt is not None and interaction.message is not None:
                    prompt = await asyncio.to_thread(
                        storage.finance_bind_daily_prompt_message,
                        prompt_id=prompt_id,
                        channel_id=interaction.channel_id,
                        message_id=interaction.message.id,
                    )
        if prompt is None:
            await interaction.response.send_message("Не удалось найти этот отчёт в базе данных.", ephemeral=True)
            return
        if str(prompt.get("status")) != "open":
            await interaction.response.send_message("Этот отчёт уже заполнен.", ephemeral=True)
            return
        await interaction.response.send_modal(DailySnapshotModal(int(prompt["id"])))


class DailySnapshotModal(discord.ui.Modal):
    def __init__(self, prompt_id: int) -> None:
        super().__init__(title="Ежедневный отчёт казны", timeout=300)
        self.prompt_id = prompt_id
        self.amount = discord.ui.TextInput(
            label="Сколько денег сейчас в казне?",
            placeholder="Например: 12 500 000",
            min_length=1,
            max_length=30,
            required=True,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            amount = parse_money(self.amount.value, allow_zero=True)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("Отчёт можно заполнить только на сервере.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        event = await asyncio.to_thread(
            storage.finance_record_snapshot,
            guild_id=interaction.guild.id,
            event_kind="daily",
            amount=amount,
            actor_id=interaction.user.id,
            actor_display=getattr(interaction.user, "display_name", interaction.user.name),
            channel_id=FINANCE_DAILY_CHANNEL_ID,
            message_id=None,
            log_channel_id=FINANCE_EVENT_LOG_CHANNEL_ID,
            admin_user_id=FINANCE_ADMIN_USER_ID,
            prompt_id=self.prompt_id,
        )
        if event.get("already_recorded"):
            await interaction.followup.send("Этот отчёт уже успел заполнить другой участник.", ephemeral=True)
            return
        prompt = await asyncio.to_thread(storage.finance_get_daily_prompt, self.prompt_id)
        if prompt is not None:
            try:
                await _edit_prompt_message(interaction.client, prompt)
            except discord.DiscordException:
                traceback.print_exc()
        wake_notification_worker()
        await interaction.followup.send(
            (
                f"Готово. Остаток **{money_text(amount)}** сохранён в финансовом журнале. "
                f"Действие для отмены: **#{event['action_id']}**."
            ),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        traceback.print_exception(type(error), error, error.__traceback__)
        if interaction.response.is_done():
            await interaction.followup.send("Не удалось сохранить отчёт. Попробуйте ещё раз.", ephemeral=True)
        else:
            await interaction.response.send_message("Не удалось сохранить отчёт. Попробуйте ещё раз.", ephemeral=True)


class MovementModal(discord.ui.Modal):
    def __init__(self, event_kind: str, *, allow_any_channel: bool = False) -> None:
        title = "Снятие денег" if event_kind == "withdraw" else "Пополнение казны"
        super().__init__(title=title, timeout=300)
        self.event_kind = event_kind
        self.allow_any_channel = allow_any_channel
        self.captcha_value = f"{secrets.randbelow(1000):03d}"
        self.amount = discord.ui.TextInput(
            label="Сумма",
            placeholder="Например: 750 000",
            min_length=1,
            max_length=30,
            required=True,
        )
        self.reason = discord.ui.TextInput(
            label="Подробная причина",
            placeholder="Опишите, для чего и при каких обстоятельствах проводится операция",
            style=discord.TextStyle.paragraph,
            min_length=10,
            max_length=1000,
            required=True,
        )
        self.captcha = discord.ui.TextInput(
            label="Проверка: перепишите 3 цифры",
            placeholder=f"Код: {self.captcha_value}",
            min_length=3,
            max_length=3,
            required=True,
        )
        self.add_item(self.amount)
        self.add_item(self.reason)
        self.add_item(self.captcha)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Операцию можно провести только на сервере Discord.", ephemeral=True)
            return
        if self.captcha.value.strip() != self.captcha_value:
            await interaction.response.send_message(
                "Капча введена неверно. Операция не сохранена — откройте форму ещё раз.",
                ephemeral=True,
            )
            return
        try:
            amount = parse_money(self.amount.value, allow_zero=False)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        reason = self.reason.value.strip()
        game_code = "".join(secrets.choice(GAME_CODE_ALPHABET) for _ in range(4))
        digest = hashlib.sha256(
            f"{interaction.guild.id}:{interaction.user.id}:{self.captcha_value}".encode("utf-8")
        ).hexdigest()
        await interaction.response.defer(ephemeral=True, thinking=True)
        event = await asyncio.to_thread(
            storage.finance_record_movement,
            guild_id=interaction.guild.id,
            event_kind=self.event_kind,
            amount=amount,
            reason=reason,
            captcha_digest=digest,
            game_code=game_code,
            actor_id=interaction.user.id,
            actor_display=getattr(interaction.user, "display_name", interaction.user.name),
            channel_id=interaction.channel_id,
            message_id=None,
            log_channel_id=FINANCE_EVENT_LOG_CHANNEL_ID,
            admin_user_id=FINANCE_ADMIN_USER_ID,
        )
        wake_notification_worker()
        verb = "снятие" if self.event_kind == "withdraw" else "пополнение"
        response = discord.Embed(
            title="✅ Операция записана",
            description=(
                f"Зафиксировано **{verb} на {money_text(amount)}**.\n\n"
                "В поле причины операции непосредственно в игре введите этот код:"
            ),
            color=discord.Color.green(),
        )
        response.add_field(name="Код из 4 букв", value=f"# **`{game_code}`**", inline=False)
        response.add_field(name="Расчётный остаток", value=money_text(event.get("balance_after")), inline=False)
        response.set_footer(text=f"Финансовая запись #{event['id']} • действие для /undo #{event['action_id']}")
        await interaction.followup.send(embed=response, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        traceback.print_exception(type(error), error, error.__traceback__)
        if interaction.response.is_done():
            await interaction.followup.send("Не удалось сохранить операцию. Попробуйте ещё раз.", ephemeral=True)
        else:
            await interaction.response.send_message("Не удалось сохранить операцию. Попробуйте ещё раз.", ephemeral=True)


class InterimSnapshotModal(discord.ui.Modal):
    def __init__(self, *, allow_any_channel: bool = False) -> None:
        super().__init__(title="Межотчёт казны", timeout=300)
        self.allow_any_channel = allow_any_channel
        self.amount = discord.ui.TextInput(
            label="Фактическая сумма в казне сейчас",
            placeholder="Например: 11 840 000",
            min_length=1,
            max_length=30,
            required=True,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Межотчёт можно провести только на сервере Discord.", ephemeral=True)
            return
        try:
            amount = parse_money(self.amount.value, allow_zero=True)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        event = await asyncio.to_thread(
            storage.finance_record_snapshot,
            guild_id=interaction.guild.id,
            event_kind="interim",
            amount=amount,
            actor_id=interaction.user.id,
            actor_display=getattr(interaction.user, "display_name", interaction.user.name),
            channel_id=interaction.channel_id,
            message_id=None,
            log_channel_id=FINANCE_EVENT_LOG_CHANNEL_ID,
            admin_user_id=FINANCE_ADMIN_USER_ID,
            report_date=now_local().date().isoformat(),
        )
        wake_notification_worker()
        await interaction.followup.send(
            (
                f"Межотчёт сохранён. Новый точный остаток: **{money_text(amount)}**. "
                f"Финансовая запись `#{event['id']}`, действие для отмены **#{event['action_id']}**."
            ),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        traceback.print_exception(type(error), error, error.__traceback__)
        if interaction.response.is_done():
            await interaction.followup.send("Не удалось сохранить межотчёт. Попробуйте ещё раз.", ephemeral=True)
        else:
            await interaction.response.send_message("Не удалось сохранить межотчёт. Попробуйте ещё раз.", ephemeral=True)


class FinancePanelView(discord.ui.View):
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
                await open_tvrs_hub(interaction)

            back.callback = back_callback
            self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.requester_id is not None and interaction.user.id != self.requester_id:
            await interaction.response.send_message("Это приватное меню открыто не для вас.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Снял", emoji="📤", style=discord.ButtonStyle.danger)
    async def withdraw(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.allow_any_channel and await interaction_wrong_channel(interaction):
            return
        await interaction.response.send_modal(MovementModal("withdraw", allow_any_channel=self.allow_any_channel))

    @discord.ui.button(label="Положил", emoji="📥", style=discord.ButtonStyle.success)
    async def deposit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.allow_any_channel and await interaction_wrong_channel(interaction):
            return
        await interaction.response.send_modal(MovementModal("deposit", allow_any_channel=self.allow_any_channel))

    @discord.ui.button(label="Межотчёт", emoji="🔎", style=discord.ButtonStyle.primary)
    async def interim(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.allow_any_channel and await interaction_wrong_channel(interaction):
            return
        await interaction.response.send_modal(InterimSnapshotModal(allow_any_channel=self.allow_any_channel))


async def _open_finance_panel_impl(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Казна работает только на сервере.", ephemeral=True)
        return
    state = await asyncio.to_thread(storage.finance_get_latest_state, interaction.guild.id)
    await interaction.response.send_message(
        embed=finance_panel_embed(state),
        view=FinancePanelView(allow_any_channel=True, requester_id=interaction.user.id),
        ephemeral=True,
    )


async def execute_undo_interaction(
    interaction: discord.Interaction,
    *,
    action_id: int | None = None,
    user: discord.abc.User | None = None,
    reason: str | None = None,
) -> None:
    """Shared implementation for the slash command and the button-driven audit center."""
    if interaction.guild is None:
        await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
        return
    target = user or interaction.user
    selected_action = None
    target_actor_id = int(target.id)
    if action_id is not None:
        selected_action = await asyncio.to_thread(storage.bot_get_action, int(action_id), interaction.guild.id)
        if selected_action is None:
            await interaction.response.send_message("Действие с таким ID не найдено.", ephemeral=True)
            return
        if selected_action.get("actor_id") is None:
            await interaction.response.send_message(
                "У системного действия нет автора, поэтому пользовательская отмена для него недоступна.",
                ephemeral=True,
            )
            return
        target_actor_id = int(selected_action["actor_id"])
    undoing_other = target_actor_id != interaction.user.id
    permissions = getattr(interaction.user, "guild_permissions", None)
    is_server_admin = bool(permissions and permissions.administrator)
    if undoing_other and interaction.user.id != FINANCE_ADMIN_USER_ID and not is_server_admin:
        await interaction.response.send_message(
            "Вы можете отменять только свои действия. Для чужой записи нужен финансовый администратор.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        result = await asyncio.to_thread(
            storage.bot_undo_action,
            guild_id=interaction.guild.id,
            target_actor_id=target_actor_id,
            undone_by_id=interaction.user.id,
            undone_by_display=getattr(interaction.user, "display_name", interaction.user.name),
            reason=reason,
            log_channel_id=FINANCE_EVENT_LOG_CHANNEL_ID,
            admin_user_id=FINANCE_ADMIN_USER_ID,
            channel_id=interaction.channel_id,
            action_id=action_id,
        )
        if result is None and action_id is None:
            legacy = await asyncio.to_thread(
                storage.finance_undo_last_action,
                guild_id=interaction.guild.id,
                target_actor_id=target_actor_id,
                undone_by_id=interaction.user.id,
                undone_by_display=getattr(interaction.user, "display_name", interaction.user.name),
                channel_id=interaction.channel_id,
                message_id=None,
                log_channel_id=FINANCE_EVENT_LOG_CHANNEL_ID,
                admin_user_id=FINANCE_ADMIN_USER_ID,
            )
            result = None if legacy is None else {"module": "finance", "finance": legacy, "action": None}
    except ValueError as exc:
        raw = str(exc)
        messages = {
            "finance_action_locked_by_craft": "Сначала отмените связанное действие крафта, затем финансовую операцию.",
            "bot_action_already_undone": "Это действие уже отменено.",
            "bot_action_not_reversible": "Действие имеет внешний необратимый эффект и автоматически не отменяется.",
            "bot_action_unsupported": "Для этого старого типа действия безопасный компенсатор ещё недоступен.",
            "bot_action_locked_by_active_consensus": (
                "Действие относится к незавершённому консенсусу. "
                "Сначала завершите заседание, затем повторите отмену."
            ),
        }
        if raw.startswith("bot_action_has_dependents:"):
            dependent_id = raw.split(":", 1)[1]
            message = f"Сначала отмените более позднее зависимое действие **#{dependent_id}**."
        else:
            message = messages.get(raw, f"Не удалось отменить действие: `{raw}`")
        await interaction.followup.send(message, ephemeral=True)
        return
    if result is None:
        await interaction.followup.send("Нет действующих записей, которые можно отменить.", ephemeral=True)
        return

    finance_result = result.get("finance")
    if finance_result:
        prompt = finance_result.get("prompt")
        if prompt is not None:
            try:
                await _edit_prompt_message(interaction.client, prompt)
            except discord.DiscordException:
                traceback.print_exc()
        wake_notification_worker()
    plan_id = result.get("refresh_plan_id")
    if plan_id:
        try:
            await refresh_craft_plan(interaction.client, int(plan_id))
        except discord.DiscordException:
            traceback.print_exc()
        completion_message_id = result.get("completion_message_id")
        if completion_message_id:
            plan = await asyncio.to_thread(storage.craft_get_plan, int(plan_id), interaction.guild.id)
            if plan:
                try:
                    channel = interaction.client.get_channel(int(plan["channel_id"])) or await interaction.client.fetch_channel(int(plan["channel_id"]))
                    message = await channel.fetch_message(int(completion_message_id))
                    await message.delete()
                except discord.NotFound:
                    pass
                except discord.DiscordException:
                    traceback.print_exc()
        wake_craft_worker()
        wake_notification_worker()
    action = result.get("action") or selected_action
    summary = action.get("summary") if action else "финансовое действие старой версии"
    action_label = f"#{action['id']}" if action else "без ID"
    response = discord.Embed(
        title="↩️ Действие отменено",
        description=f"**{action_label}:** {summary}",
        color=discord.Color.orange(),
    )
    if finance_result:
        response.add_field(
            name="Остаток после отмены",
            value=money_text(finance_result["undo_event"].get("balance_after")),
            inline=False,
        )
    response.add_field(name="Причина", value=str(reason or "Отмена действия пользователем")[:1024], inline=False)
    await interaction.followup.send(embed=response, ephemeral=True)


def audit_center_embed(guild_id: int, actor_id: int) -> discord.Embed:
    actions = storage.bot_list_actions(guild_id, actor_id=actor_id, limit=100)
    active = [row for row in actions if str(row.get("status")) == "active"]
    undone = [row for row in actions if str(row.get("status")) == "undone"]
    finance = storage.finance_stats(guild_id, 30)
    craft = storage.craft_stats(guild_id, 30)
    embed = discord.Embed(
        title="🧾 Центр аудита Товарищества",
        description=(
            "Проверяйте операции, смотрите журнал и исправляйте ошибки кнопками. "
            "Все результаты видны только вам."
        ),
        color=FINANCE_EMBED_COLOR,
    )
    embed.add_field(
        name="Ваш журнал",
        value=f"Активных действий: **{len(active)}**\nОтменено: **{len(undone)}**",
        inline=True,
    )
    embed.add_field(
        name="Казна · 30 дней",
        value=f"Операций: **{finance['movement_count']}**\nЧистый поток: **{money_text(finance['net_flow'])}**",
        inline=True,
    )
    embed.add_field(
        name="Крафты · 30 дней",
        value=f"Планов: **{craft['plan_count']}**\nРезультат: **{money_text(craft['profit'])}**",
        inline=True,
    )
    embed.add_field(
        name="Быстрые действия",
        value=(
            "**Мои действия** — номера и состояние записей\n"
            "**Найти операцию** — код, тип, период или причина\n"
            "**Отменить** — исправить последнее или точное действие"
        ),
        inline=False,
    )
    embed.set_footer(text="Отмена сохраняет исходную запись и создаёт прозрачную компенсацию")
    return embed


class FinanceAuditSearchModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title="Поиск финансовой операции", timeout=600)
        self.code = discord.ui.TextInput(
            label="Код из 4 букв",
            placeholder="Например: KXRM; можно оставить пустым",
            required=False,
            max_length=20,
        )
        self.kind = discord.ui.TextInput(
            label="Тип",
            placeholder="deposit, withdraw, daily, interim или undo",
            required=False,
            max_length=20,
        )
        self.days = discord.ui.TextInput(
            label="Период в днях",
            placeholder="30",
            default="30",
            required=False,
            max_length=4,
        )
        self.text = discord.ui.TextInput(
            label="Фрагмент причины",
            placeholder="Например: материалы или автомобиль",
            required=False,
            max_length=200,
        )
        self.user_id = discord.ui.TextInput(
            label="Discord ID автора",
            placeholder="Необязательно",
            required=False,
            max_length=30,
        )
        for item in (self.code, self.kind, self.days, self.text, self.user_id):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Поиск работает только на сервере.", ephemeral=True)
            return
        try:
            days = int(self.days.value.strip()) if self.days.value.strip() else None
            if days is not None and not 1 <= days <= 3650:
                raise ValueError("Период должен быть от 1 до 3650 дней")
            raw_user = re.sub(r"\D", "", self.user_id.value)
            actor_id = int(raw_user) if raw_user else None
            events = await asyncio.to_thread(
                storage.finance_search_events,
                interaction.guild.id,
                code=self.code.value or None,
                event_kind=self.kind.value.strip().lower() or None,
                actor_id=actor_id,
                days=days,
                text=self.text.value or None,
                limit=25,
            )
        except ValueError as exc:
            await interaction.response.send_message(f"Проверьте фильтры: `{exc}`.", ephemeral=True)
            return
        suffix = f"код {re.sub(r'[^A-Za-z]', '', self.code.value).upper()}" if self.code.value.strip() else None
        await interaction.response.send_message(embed=finance_audit_embed(events, title_suffix=suffix), ephemeral=True)


class UndoActionModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title="Безопасная отмена действия", timeout=600)
        self.action_id = discord.ui.TextInput(
            label="ID действия",
            placeholder="Пусто — отменить ваше последнее действие",
            required=False,
            max_length=20,
        )
        self.reason = discord.ui.TextInput(
            label="Причина отмены",
            placeholder="Кратко объясните, что было введено неверно",
            style=discord.TextStyle.paragraph,
            min_length=3,
            max_length=500,
        )
        self.add_item(self.action_id)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.action_id.value.strip()
        if raw and not raw.isdigit():
            await interaction.response.send_message("ID действия должен состоять только из цифр.", ephemeral=True)
            return
        await execute_undo_interaction(
            interaction,
            action_id=int(raw) if raw else None,
            reason=self.reason.value,
        )


class AuditCenterView(discord.ui.View):
    def __init__(self, requester_id: int, *, back_to_tvrs: bool = False) -> None:
        super().__init__(timeout=900)
        self.requester_id = int(requester_id)
        if back_to_tvrs:
            back = discord.ui.Button(label="Назад", emoji="⬅️", style=discord.ButtonStyle.secondary, row=2)

            async def back_callback(interaction: discord.Interaction) -> None:
                await open_tvrs_hub(interaction)

            back.callback = back_callback
            self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Это приватное меню открыто не для вас.", ephemeral=True)
            return False
        if interaction.guild is None:
            await interaction.response.send_message("Центр аудита работает только на сервере.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Мои действия", emoji="📋", style=discord.ButtonStyle.primary, row=0)
    async def actions(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        rows = await asyncio.to_thread(
            storage.bot_list_actions,
            interaction.guild.id,
            actor_id=interaction.user.id,
            limit=20,
        )
        await interaction.response.send_message(embed=actions_embed(rows, interaction.user), ephemeral=True)

    @discord.ui.button(label="Найти операцию", emoji="🔍", style=discord.ButtonStyle.secondary, row=0)
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(FinanceAuditSearchModal())

    @discord.ui.button(label="Отменить", emoji="↩️", style=discord.ButtonStyle.danger, row=0)
    async def undo(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(UndoActionModal())

    @discord.ui.button(label="Статистика казны", emoji="💼", style=discord.ButtonStyle.secondary, row=1)
    async def finance_stats(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        stats = await asyncio.to_thread(storage.finance_stats, interaction.guild.id, 30)
        await interaction.response.send_message(embed=finance_stats_embed(stats), ephemeral=True)

    @discord.ui.button(label="Статистика крафтов", emoji="🏭", style=discord.ButtonStyle.secondary, row=1)
    async def craft_stats(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        stats = await asyncio.to_thread(storage.craft_stats, interaction.guild.id, 30)
        embed = build_craft_stats_embed(stats)
        if embed is None:
            await interaction.response.send_message(
                "Модуль крафтов ещё запускается. Повторите действие через несколько секунд.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Обновить", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        embed = await asyncio.to_thread(audit_center_embed, interaction.guild.id, interaction.user.id)
        await interaction.response.edit_message(content=None, embed=embed, view=self)


async def _open_finance_hub_section(
    interaction: discord.Interaction,
    requester_id: int,
    surface: HubSurface,
) -> None:
    assert interaction.guild is not None
    if surface == "replace":
        await interaction.response.defer()
    else:
        await interaction.response.defer(ephemeral=True, thinking=True)
    state = await asyncio.to_thread(storage.finance_get_latest_state, interaction.guild.id)
    embed = finance_panel_embed(state)
    view = FinancePanelView(
        allow_any_channel=True,
        requester_id=requester_id,
        back_to_tvrs=True,
    )
    if surface == "replace":
        await interaction.edit_original_response(content=None, embed=embed, view=view)
    else:
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def _open_audit_hub_section(
    interaction: discord.Interaction,
    requester_id: int,
    surface: HubSurface,
) -> None:
    assert interaction.guild is not None
    embed = await asyncio.to_thread(audit_center_embed, interaction.guild.id, interaction.user.id)
    view = AuditCenterView(requester_id, back_to_tvrs=True)
    if surface == "replace":
        await interaction.response.edit_message(content=None, embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


def setup_finance(
    bot: commands.Bot,
    remember_command_activity: Callable[[discord.Interaction, str, str], None],
) -> None:
    register_finance_runtime(
        panel_opener=_open_finance_panel_impl,
        worker_wakeup=_wake_notification_worker_impl,
    )
    register_hub_section("finance", _open_finance_hub_section)
    register_hub_section("audit", _open_audit_hub_section)

    @bot.tree.command(name="finance", description="Открыть обозреватель казны Товарищества")
    async def finance(interaction: discord.Interaction) -> None:
        if await interaction_wrong_channel(interaction):
            return
        remember_command_activity(interaction, "command_finance", "/finance")
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = await asyncio.to_thread(storage.finance_get_latest_state, interaction.guild.id)
        await interaction.followup.send(
            embed=finance_panel_embed(state),
            view=FinancePanelView(),
            ephemeral=True,
        )

    @bot.tree.command(name="actions", description="Показать журнал отменяемых действий с ботом")
    @discord.app_commands.describe(
        user="Участник; чужой журнал доступен финансовому администратору",
        module="Фильтр модуля: finance, craft, sgl, tvrs",
        limit="Количество записей от 1 до 20",
    )
    async def actions(
        interaction: discord.Interaction,
        user: discord.User | None = None,
        module: str | None = None,
        limit: discord.app_commands.Range[int, 1, 20] = 10,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
            return
        target = user or interaction.user
        permissions = getattr(interaction.user, "guild_permissions", None)
        can_view_other = interaction.user.id == FINANCE_ADMIN_USER_ID or bool(permissions and permissions.administrator)
        if target.id != interaction.user.id and not can_view_other:
            await interaction.response.send_message("Чужой журнал доступен только администратору.", ephemeral=True)
            return
        clean_module = str(module or "").strip().lower() or None
        if clean_module not in {None, "finance", "craft", "sgl", "tvrs", "bureau"}:
            await interaction.response.send_message("Модуль: `finance`, `craft`, `sgl`, `tvrs` или `bureau`.", ephemeral=True)
            return
        rows = await asyncio.to_thread(
            storage.bot_list_actions,
            interaction.guild.id,
            actor_id=target.id,
            module=clean_module,
            limit=int(limit),
        )
        await interaction.response.send_message(embed=actions_embed(rows, target), ephemeral=True)

    @bot.tree.command(name="undo", description="Безопасно отменить последнее или выбранное действие с ботом")
    @discord.app_commands.describe(
        action_id="ID из команды /actions; пусто — последнее действие",
        user="Чьё последнее действие отменить; только для администратора",
        reason="Причина отмены для журнала",
    )
    async def undo(
        interaction: discord.Interaction,
        action_id: int | None = None,
        user: discord.User | None = None,
        reason: str | None = None,
    ) -> None:
        remember_command_activity(interaction, "command_global_undo", "/undo")
        await execute_undo_interaction(
            interaction,
            action_id=action_id,
            user=user,
            reason=reason,
        )

    @bot.tree.command(name="finance-audit", description="Найти и проверить финансовые операции")
    @discord.app_commands.describe(
        code="Четырёхбуквенный код операции",
        user="Автор операции",
        kind="daily, interim, deposit, withdraw или undo",
        days="Период поиска в днях",
        text="Фрагмент причины",
    )
    async def finance_audit(
        interaction: discord.Interaction,
        code: str | None = None,
        user: discord.User | None = None,
        kind: str | None = None,
        days: discord.app_commands.Range[int, 1, 3650] | None = None,
        text: str | None = None,
    ) -> None:
        if await interaction_wrong_channel(interaction):
            return
        try:
            events = await asyncio.to_thread(
                storage.finance_search_events,
                interaction.guild.id,
                code=code,
                event_kind=str(kind).strip().lower() if kind else None,
                actor_id=user.id if user else None,
                days=int(days) if days else None,
                text=text,
                limit=25,
            )
        except ValueError as exc:
            await interaction.response.send_message(f"Некорректный фильтр: `{exc}`.", ephemeral=True)
            return
        suffix = f"код {re.sub(r'[^A-Za-z]', '', code).upper()}" if code else None
        await interaction.response.send_message(embed=finance_audit_embed(events, title_suffix=suffix), ephemeral=True)

    @bot.tree.command(name="finance-stats", description="Показать статистику казны")
    @discord.app_commands.describe(days="Период статистики в днях")
    async def finance_stats_command(
        interaction: discord.Interaction,
        days: discord.app_commands.Range[int, 1, 3650] = 30,
    ) -> None:
        if await interaction_wrong_channel(interaction):
            return
        stats = await asyncio.to_thread(storage.finance_stats, interaction.guild.id, int(days))
        await interaction.response.send_message(embed=finance_stats_embed(stats), ephemeral=True)

    async def finance_ready_listener() -> None:
        global _persistent_view_registered, _worker_task
        if not _persistent_view_registered:
            bot.add_view(FinanceDailyPromptView())
            _persistent_view_registered = True
        if _worker_task is None or _worker_task.done():
            _worker_task = asyncio.create_task(finance_worker(bot), name="tmod-finance-worker")

    bot.add_listener(finance_ready_listener, "on_ready")
