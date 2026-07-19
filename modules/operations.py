import asyncio
import os
import traceback
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from persistence import operations_context as storage
from modules.consensus_runtime import active_consensus_snapshot as _active_consensus_snapshot
from modules.control_center_config import ACTIVE_TASKS_CHANNEL_ID, OPERATIONS_CATEGORY_ID
from modules.control_center import (
    edit_message_with_retry,
    ensure_public_control_panel,
    is_transient_discord_error,
    message_payload_matches,
    public_panel_url,
    resolve_control_channel,
)
from modules.operations_runtime import (
    bind_worker_wakeup,
    register_dashboard_refresher,
)
from modules.technical_log import log_technical_event


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw, 0)
    except (TypeError, ValueError):
        return default


OPERATIONS_REFRESH_SECONDS = max(10, _env_int("OPERATIONS_REFRESH_SECONDS", 30))
OPERATIONS_STICKY_DEBOUNCE_SECONDS = max(1, _env_int("OPERATIONS_STICKY_DEBOUNCE_SECONDS", 2))
OPERATIONS_EMBED_COLOR = _env_int("OPERATIONS_EMBED_COLOR", 0xD9D9D9)
FINANCE_REPORT_HOUR = max(0, min(23, _env_int("FINANCE_REPORT_HOUR", 18)))
FINANCE_REPORT_MINUTE = max(0, min(59, _env_int("FINANCE_REPORT_MINUTE", 0)))
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "Europe/Riga"))

DASHBOARD_MARKER = "tmod-operations-dashboard"

_dashboard_locks: dict[int, asyncio.Lock] = {}
_sticky_tasks: dict[int, asyncio.Task[None]] = {}
_worker_task: asyncio.Task[None] | None = None
_category_warning_shown: set[int] = set()
_pruned_guilds: set[int] = set()


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def _message_url(guild_id: int, channel_id: int | None, message_id: int | None) -> str | None:
    if not channel_id or not message_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{int(channel_id)}/{int(message_id)}"


def _linked_title(title: str, url: str | None) -> str:
    return f"[{title}]({url})" if url else title


def _discord_time(value: str | None, style: str = "R") -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return f"<t:{int(parsed.timestamp())}:{style}>"
    except (TypeError, ValueError):
        return str(value)


def _craft_operation_line(plan: dict[str, Any]) -> tuple[str, str]:
    stage = str(plan.get("stage") or "")
    plan_id = int(plan["id"])
    recipe = plan.get("recipe") or {}
    product = str(recipe.get("product_name") or f"План #{plan_id}")
    responsible_id = int(plan.get("responsible_id") or 0)
    link = _message_url(
        int(plan.get("guild_id") or 0),
        int(plan.get("channel_id") or 0),
        int(plan.get("message_id") or 0),
    )
    title = _linked_title(f"Крафт #{plan_id} · {product}", link)
    responsible = f"<@{responsible_id}>" if responsible_id else "не назначен"

    if stage == "procurement":
        missing = sum(
            1
            for item in plan.get("materials") or []
            if int(item.get("stock_quantity") or 0) < int(item.get("required_total") or 0)
        )
        return "attention", f"🛒 **{title}** — собрать материалы ({missing} поз.) · {responsible}"
    if stage == "crafting":
        batch = plan.get("active_batch")
        if batch:
            return "progress", f"⚙️ **{title}** — цикл завершится {_discord_time(batch.get('due_at'))} · {responsible}"
        remaining = max(0, int(plan.get("attempts_total") or 0) - int(plan.get("attempts_queued") or 0))
        return "attention", f"▶️ **{title}** — поставить следующий цикл, осталось {remaining} · {responsible}"
    if stage == "awaiting_output":
        return "attention", f"📦 **{title}** — внести итог производства · {responsible}"
    if stage == "listing":
        return "attention", f"🏷️ **{title}** — оценить и выставить продукт · {responsible}"
    if stage == "selling":
        remaining = max(0, int(plan.get("final_product_qty") or 0) - int(plan.get("sold_qty") or 0))
        return "progress", f"💰 **{title}** — продажи на маркете, осталось {remaining} · {responsible}"
    return "progress", f"🟡 **{title}** — {stage or 'в работе'} · {responsible}"


def _append_limited(lines: list[str], additions: list[str], *, limit: int = 8) -> None:
    remaining = max(0, limit - len(lines))
    lines.extend(additions[:remaining])
    hidden = len(additions) - remaining
    if hidden > 0 and len(lines) < limit + 1:
        lines.append(f"…и ещё **{hidden}**")


def _field_value(lines: list[str], empty: str = "—") -> str:
    if not lines:
        return empty
    accepted: list[str] = []
    for index, line in enumerate(lines):
        candidate = "\n".join([*accepted, line])
        if len(candidate) <= 1000:
            accepted.append(line)
            continue
        hidden = len(lines) - index
        suffix = f"…и ещё **{hidden}**"
        if len("\n".join([*accepted, suffix])) <= 1024:
            accepted.append(suffix)
        break
    return "\n".join(accepted) or empty


def build_operations_embed(
    guild: discord.Guild | Any,
    *,
    local_now: datetime | None = None,
    consensus: dict[str, Any] | None | object = ...,
) -> discord.Embed:
    guild_id = int(guild.id)
    current = local_now or now_local()
    attention: list[str] = []
    progress: list[str] = []
    queue: list[str] = []

    due = (current.hour, current.minute) >= (FINANCE_REPORT_HOUR, FINANCE_REPORT_MINUTE)
    today = current.date().isoformat()
    prompts = storage.finance_recent_daily_prompts(guild_id=guild_id, limit=3)
    today_prompt = next((item for item in prompts if str(item.get("report_date")) == today), None)
    if due and (today_prompt is None or str(today_prompt.get("status")) == "open"):
        link = None
        if today_prompt:
            link = _message_url(guild_id, today_prompt.get("channel_id"), today_prompt.get("message_id"))
        title = _linked_title("Ежедневная сверка казны", link)
        status = "ожидает заполнения" if today_prompt else "ожидает публикации ботом"
        attention.append(f"💼 **{title}** — {status}")

    active_consensus = _active_consensus_snapshot(guild_id) if consensus is ... else consensus
    if isinstance(active_consensus, dict):
        stage = str(active_consensus.get("stage") or "")
        stage_label = str(active_consensus.get("stage_label") or stage)
        plenary = int(active_consensus.get("plenary_number") or 0)
        leader_id = int(active_consensus.get("leader_id") or 0)
        bill = active_consensus.get("current_bill") or {}
        bill_text = ""
        if bill:
            bill_text = f" · проект `{int(bill.get('bill_number') or 0):03d}`"
        title = _linked_title(f"{plenary}-й консенсус", public_panel_url(guild_id))
        line = f"⚖️ **{title}** — {stage_label}{bill_text} · ведущий <@{leader_id}>"
        if stage in {"registration", "paused", "after_result", "discussion_type"}:
            attention.append(line)
        else:
            progress.append(line)

    bills = storage.tvrs_queue_bills(guild_id, limit=100)
    if bills:
        if active_consensus is None:
            title = _linked_title("Законопроекты", public_panel_url(guild_id))
            attention.append(f"📜 **{title}** — {len(bills)} ожидают запуска консенсуса")
        preview = []
        for item in bills[:5]:
            number = int(item.get("bill_number") or 0)
            title = str(item.get("title") or "Без названия")[:80]
            link = _message_url(guild_id, item.get("channel_id"), item.get("message_id"))
            preview.append(f"`{number:03d}` · {_linked_title(title, link)}")
        queue.extend(preview)
        if len(bills) > len(preview):
            queue.append(f"…и ещё **{len(bills) - len(preview)}**")

    craft_attention: list[str] = []
    craft_progress: list[str] = []
    for plan in storage.craft_active_plans(guild_id, 100):
        bucket, line = _craft_operation_line(plan)
        (craft_attention if bucket == "attention" else craft_progress).append(line)
    _append_limited(attention, craft_attention)
    _append_limited(progress, craft_progress)

    active_count = len(attention) + len(progress)
    embed = discord.Embed(
        title="🧭 Операционный центр",
        description=(
            f"Сейчас отслеживается активных элементов: **{active_count}**. "
            "Здесь остаются только дела Товарищества, которые требуют участия людей или ещё не завершены."
            if active_count
            else "🟢 Сейчас у Товарищества нет задач, требующих внимания, и активных процессов."
        ),
        color=OPERATIONS_EMBED_COLOR,
        timestamp=current,
    )
    embed.add_field(
        name=f"🔴 Требует действия · {len(attention)}",
        value=_field_value(attention, "Ничего не ожидает действий."),
        inline=False,
    )
    if progress:
        embed.add_field(
            name=f"🟡 В процессе · {len(progress)}",
            value=_field_value(progress),
            inline=False,
        )
    if queue:
        embed.add_field(name=f"📥 Очередь решений · {len(bills)}", value=_field_value(queue), inline=False)
    embed.add_field(
        name="Как устроен центр",
        value=(
            "Каждый пункт ведёт к единственному исходному сообщению в нужном разделе. "
            "Карточки здесь не копируются, поэтому рабочая история не распадается."
        ),
        inline=False,
    )
    embed.set_footer(text=f"T-Mod • обновление каждые {OPERATIONS_REFRESH_SECONDS} сек. • {DASHBOARD_MARKER}")
    return embed


async def _get_operations_channel(guild: discord.Guild) -> Any:
    channel = await resolve_control_channel(guild, "active_tasks")
    if channel is None:
        raise RuntimeError("Канал «активные-задачи» не найден")
    return channel


def _dashboard_meta_key(guild_id: int, channel_id: int) -> str:
    return f"operations_dashboard_message_id:{guild_id}:{channel_id}"


def _is_dashboard_message(message: discord.Message) -> bool:
    return any(
        embed.footer and embed.footer.text and DASHBOARD_MARKER in embed.footer.text
        for embed in message.embeds
    )


async def ensure_operations_dashboard(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    *,
    force_repost: bool = False,
) -> None:
    channel = await _get_operations_channel(guild)
    if getattr(channel, "guild", None) is None or int(channel.guild.id) != guild.id:
        raise RuntimeError("ACTIVE_TASKS_CHANNEL_ID не принадлежит текущему серверу")
    if OPERATIONS_CATEGORY_ID and getattr(channel, "category_id", None) != OPERATIONS_CATEGORY_ID:
        if guild.id not in _category_warning_shown:
            print(
                f"[OPERATIONS] Channel {ACTIVE_TASKS_CHANNEL_ID} is outside configured category "
                f"{OPERATIONS_CATEGORY_ID}",
                flush=True,
            )
            _category_warning_shown.add(guild.id)

    lock = _dashboard_locks.setdefault(int(channel.id), asyncio.Lock())
    async with lock:
        embed = await asyncio.to_thread(build_operations_embed, guild)
        meta_key = _dashboard_meta_key(guild.id, int(channel.id))
        raw_message_id = await asyncio.to_thread(storage.get_meta, meta_key)
        old_message = None
        if raw_message_id and str(raw_message_id).isdigit():
            try:
                old_message = await channel.fetch_message(int(raw_message_id))
            except discord.NotFound:
                old_message = None
            except discord.DiscordException:
                if not force_repost:
                    raise

        if old_message is not None and not force_repost:
            if message_payload_matches(old_message, embed=embed):
                return
            await edit_message_with_retry(
                old_message,
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if old_message is not None:
            try:
                await old_message.delete()
            except discord.DiscordException:
                pass
        message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await asyncio.to_thread(storage.set_meta_value, meta_key, str(message.id))


async def prune_legacy_bot_messages(bot: commands.Bot, guild: discord.Guild) -> None:
    """Leave only the live dashboard in active tasks; source records live elsewhere."""
    if guild.id in _pruned_guilds or bot.user is None:
        return
    channel = await _get_operations_channel(guild)
    current_raw = await asyncio.to_thread(storage.get_meta, _dashboard_meta_key(guild.id, int(channel.id)))
    current_id = int(current_raw) if current_raw and str(current_raw).isdigit() else None
    preserve_ids = {
        int(plan["message_id"])
        for plan in await asyncio.to_thread(storage.craft_active_plans, guild.id, 100)
        if int(plan.get("channel_id") or 0) == int(channel.id) and plan.get("message_id")
    }
    preserve_ids.update(
        int(prompt["message_id"])
        for prompt in await asyncio.to_thread(
            storage.finance_recent_daily_prompts,
            guild_id=guild.id,
            limit=30,
        )
        if (
            str(prompt.get("status")) == "open"
            and int(prompt.get("channel_id") or 0) == int(channel.id)
            and prompt.get("message_id")
        )
    )
    async for message in channel.history(limit=250):
        if message.author.id != bot.user.id or message.id == current_id or message.id in preserve_ids:
            continue
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.DiscordException:
            traceback.print_exc()
    _pruned_guilds.add(guild.id)


def schedule_operations_sticky(bot: commands.Bot, guild: discord.Guild) -> None:
    key = guild.id
    task = _sticky_tasks.get(key)
    if task is not None and not task.done():
        return

    async def runner() -> None:
        try:
            await asyncio.sleep(OPERATIONS_STICKY_DEBOUNCE_SECONDS)
            await ensure_operations_dashboard(bot, guild, force_repost=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if is_transient_discord_error(exc):
                print(
                    f"[OPERATIONS] Discord API temporarily unavailable ({exc.status}); "
                    "the deferred dashboard refresh will retry on the next cycle.",
                    flush=True,
                )
                return
            traceback.print_exc()
            await log_technical_event(
                bot,
                guild,
                title="Не удалось обновить активные задачи",
                details="Проверьте доступ T-Mod к каналу и состояние базы данных.",
                dedupe_key="operations-dashboard-refresh",
            )
        finally:
            _sticky_tasks.pop(key, None)

    _sticky_tasks[key] = asyncio.create_task(runner(), name=f"tmod-operations-sticky-{key}")


async def operations_worker(bot: commands.Bot) -> None:
    worker_wakeup = bind_worker_wakeup(asyncio.Event())
    while not bot.is_closed():
        for guild in bot.guilds:
            try:
                await ensure_operations_dashboard(bot, guild)
                await ensure_public_control_panel(bot, guild)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if is_transient_discord_error(exc):
                    print(
                        f"[OPERATIONS] Discord API temporarily unavailable ({exc.status}); "
                        "the next cycle will retry automatically.",
                        flush=True,
                    )
                    continue
                traceback.print_exc()
                await log_technical_event(
                    bot,
                    guild,
                    title="Сбой рабочего цикла активных задач",
                    details="Автоматическое обновление будет повторено. Проверьте лог контейнера для трассировки.",
                    dedupe_key="operations-worker",
                )
        try:
            await asyncio.wait_for(worker_wakeup.wait(), timeout=OPERATIONS_REFRESH_SECONDS)
            worker_wakeup.clear()
        except asyncio.TimeoutError:
            pass


def setup_operations(bot: commands.Bot) -> None:
    register_dashboard_refresher(ensure_operations_dashboard)

    async def operations_ready_listener() -> None:
        global _worker_task
        for guild in bot.guilds:
            try:
                await ensure_operations_dashboard(bot, guild)
                await prune_legacy_bot_messages(bot, guild)
            except asyncio.CancelledError:
                raise
            except Exception:
                traceback.print_exc()
        if _worker_task is None or _worker_task.done():
            _worker_task = asyncio.create_task(operations_worker(bot), name="tmod-operations-worker")

    async def operations_message_listener(message: discord.Message) -> None:
        if message.guild is None or message.channel.id != ACTIVE_TASKS_CHANNEL_ID:
            return
        if _is_dashboard_message(message):
            return
        schedule_operations_sticky(bot, message.guild)

    bot.add_listener(operations_ready_listener, "on_ready")
    bot.add_listener(operations_message_listener, "on_message")
