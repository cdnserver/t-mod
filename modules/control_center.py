import asyncio
import os
import traceback
from datetime import datetime, timezone
import discord
from discord.ext import commands

from persistence import operations_context as storage
from modules.control_center_config import (
    CHANNEL_SPEC_BY_KEY,
    CHANNEL_SPECS,
    CONTROL_CENTER_COLOR,
    FINANCE_LOG_CHANNEL_ID as FINANCE_LOG_CHANNEL_ID,
    OPERATIONS_AUTO_CREATE_CHANNELS,
    OPERATIONS_CATEGORY_ID,
    PANEL_MARKERS,
    WORKSHOP_CHANNEL_ID as WORKSHOP_CHANNEL_ID,
)
from modules.control_center_runtime import register_channel_resolver
from modules.consensus_simulator import start_consensus_simulation
from modules.majestic_api import MajesticApiError, MajesticMarketplaceSummary, get_majestic_api_client
from modules.operations_runtime import refresh_operations_dashboard
from modules.public_panel_runtime import public_panel_provider
from modules.technical_log import log_technical_event as _log_technical_event

_setup_lock = asyncio.Lock()
_persistent_views_registered = False
_TRANSIENT_DISCORD_STATUSES = frozenset({500, 502, 503, 504, 520, 521, 522, 523, 524})


def _channel_meta_key(guild_id: int, key: str) -> str:
    return f"control_center_channel_id:{guild_id}:{key}"


def _panel_meta_key(guild_id: int, key: str) -> str:
    return f"control_center_panel_message_id:{guild_id}:{key}"


def stored_channel_id(guild_id: int, key: str) -> int | None:
    raw = storage.get_meta(_channel_meta_key(guild_id, key))
    if raw and str(raw).isdigit():
        return int(raw)
    spec = CHANNEL_SPEC_BY_KEY.get(key)
    if spec and spec.configured_id > 0:
        return spec.configured_id
    return None


def stored_panel_message_id(guild_id: int, key: str) -> int | None:
    raw = storage.get_meta(_panel_meta_key(guild_id, key))
    return int(raw) if raw and str(raw).isdigit() else None


def public_panel_url(guild_id: int) -> str | None:
    channel_id = stored_channel_id(guild_id, "control_panel")
    message_id = stored_panel_message_id(guild_id, "control_panel")
    if channel_id and message_id:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    if channel_id:
        return f"https://discord.com/channels/{guild_id}/{channel_id}"
    return None


def _normalise_channel_name(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "-").replace(" ", "-")


async def resolve_operations_category(guild: discord.Guild) -> discord.CategoryChannel | None:
    category = guild.get_channel(OPERATIONS_CATEGORY_ID) if OPERATIONS_CATEGORY_ID else None
    if isinstance(category, discord.CategoryChannel):
        return category
    stored = await asyncio.to_thread(
        storage.get_meta,
        f"control_center_category_id:{guild.id}",
    )
    if stored and str(stored).isdigit():
        category = guild.get_channel(int(stored))
        if isinstance(category, discord.CategoryChannel):
            return category
    for candidate in guild.categories:
        name = str(candidate.name).strip().lower()
        if name in {"администрирование tvrs", "операционный центр tvrs", "управление tvrs"}:
            await asyncio.to_thread(
                storage.set_meta_value,
                f"control_center_category_id:{guild.id}",
                str(candidate.id),
            )
            return candidate
    if not OPERATIONS_AUTO_CREATE_CHANNELS:
        return None
    created = await guild.create_category("Администрирование TVRS", reason="Структура операционного центра T-Mod")
    await asyncio.to_thread(
        storage.set_meta_value,
        f"control_center_category_id:{guild.id}",
        str(created.id),
    )
    return created


async def resolve_control_channel(
    guild: discord.Guild,
    key: str,
    *,
    create: bool | None = None,
) -> discord.TextChannel | None:
    spec = CHANNEL_SPEC_BY_KEY[key]
    should_create = OPERATIONS_AUTO_CREATE_CHANNELS if create is None else bool(create)
    candidate_ids: list[int] = []
    if spec.configured_id > 0:
        candidate_ids.append(spec.configured_id)
    stored = await asyncio.to_thread(
        storage.get_meta,
        _channel_meta_key(guild.id, key),
    )
    if stored and str(stored).isdigit():
        candidate_ids.append(int(stored))
    for channel_id in candidate_ids:
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            await asyncio.to_thread(
                storage.set_meta_value,
                _channel_meta_key(guild.id, key),
                str(channel.id),
            )
            return channel

    category = await resolve_operations_category(guild)
    if category is None:
        return None
    expected = _normalise_channel_name(spec.name)
    for channel in category.text_channels:
        if _normalise_channel_name(channel.name) == expected:
            await asyncio.to_thread(
                storage.set_meta_value,
                _channel_meta_key(guild.id, key),
                str(channel.id),
            )
            return channel
    if not should_create:
        return None

    overwrites = dict(category.overwrites)
    default_overwrite = overwrites.get(guild.default_role, discord.PermissionOverwrite())
    default_overwrite.update(
        send_messages=False,
        add_reactions=False,
        create_public_threads=False,
        create_private_threads=False,
        send_messages_in_threads=False,
    )
    overwrites[guild.default_role] = default_overwrite
    bot_member = guild.me
    if bot_member is not None:
        bot_overwrite = overwrites.get(bot_member, discord.PermissionOverwrite())
        bot_overwrite.update(
            view_channel=True,
            send_messages=True,
            manage_messages=True,
            read_message_history=True,
            create_public_threads=True,
            send_messages_in_threads=True,
        )
        overwrites[bot_member] = bot_overwrite
    channel = await guild.create_text_channel(
        spec.name,
        category=category,
        topic=spec.topic,
        overwrites=overwrites,
        reason="Структура операционного центра T-Mod",
    )
    await asyncio.to_thread(
        storage.set_meta_value,
        _channel_meta_key(guild.id, key),
        str(channel.id),
    )
    return channel


async def resolve_all_channels(guild: discord.Guild) -> dict[str, discord.TextChannel | None]:
    resolved: dict[str, discord.TextChannel | None] = {}
    for spec in CHANNEL_SPECS:
        resolved[spec.key] = await resolve_control_channel(guild, spec.key)
    return resolved


async def order_control_channels(channels: dict[str, discord.TextChannel | None]) -> None:
    ordered = [channels.get(spec.key) for spec in CHANNEL_SPECS]
    available = [channel for channel in ordered if channel is not None]
    if not available:
        return
    base_position = min(channel.position for channel in available)
    for offset, channel in enumerate(available):
        desired = base_position + offset
        if channel.position == desired:
            continue
        await channel.edit(position=desired, reason="Порядок каналов операционного центра T-Mod")


def _panel_marker(message: discord.Message, marker: str) -> bool:
    return any(
        embed.footer and embed.footer.text and marker in embed.footer.text
        for embed in message.embeds
    )


def is_transient_discord_error(exc: BaseException) -> bool:
    return isinstance(exc, discord.HTTPException) and int(exc.status) in _TRANSIENT_DISCORD_STATUSES


def _embed_semantic_payload(embed: discord.Embed) -> dict:
    payload = embed.to_dict()
    payload.pop("timestamp", None)
    return payload


def message_payload_matches(
    message: discord.Message,
    *,
    embed: discord.Embed,
    view: discord.ui.View | None = None,
) -> bool:
    if str(getattr(message, "content", "") or ""):
        return False
    current_embeds = list(getattr(message, "embeds", ()) or ())
    if len(current_embeds) != 1:
        return False
    if _embed_semantic_payload(current_embeds[0]) != _embed_semantic_payload(embed):
        return False
    current_components = [
        component.to_dict()
        for component in (getattr(message, "components", ()) or ())
    ]
    expected_components = view.to_components() if view is not None else []
    return current_components == expected_components


async def edit_message_with_retry(
    message: discord.Message,
    **kwargs: object,
) -> discord.Message:
    for attempt in range(3):
        try:
            return await message.edit(**kwargs)
        except discord.HTTPException as exc:
            if not is_transient_discord_error(exc) or attempt >= 2:
                raise
            await asyncio.sleep(0.75 * (2**attempt))
    raise RuntimeError("unreachable")


async def _find_panel_message(channel: discord.TextChannel, marker: str) -> discord.Message | None:
    async for message in channel.history(limit=50):
        if message.author.bot and _panel_marker(message, marker):
            return message
    return None


async def ensure_panel_message(
    channel: discord.TextChannel,
    key: str,
    *,
    embed: discord.Embed,
    view: discord.ui.View | None = None,
) -> discord.Message:
    marker = PANEL_MARKERS[key]
    message: discord.Message | None = None
    message_id = stored_panel_message_id(channel.guild.id, key)
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            message = None
    if message is None:
        message = await _find_panel_message(channel, marker)
    if message is None:
        message = await channel.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    elif not message_payload_matches(message, embed=embed, view=view):
        await edit_message_with_retry(
            message,
            content=None,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    await asyncio.to_thread(
        storage.set_meta_value,
        _panel_meta_key(channel.guild.id, key),
        str(message.id),
    )
    return message


def _reports_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📊 Отчёты Товарищества",
        description=(
            "Раздел подготовлен для регулярных сводок. Сейчас рабочие данные продолжают "
            "жить в своих разделах; публикация ежедневных и недельных отчётов будет подключена отдельно."
        ),
        color=CONTROL_CENTER_COLOR,
    )
    embed.add_field(
        name="Запланированный каркас",
        value="• состояние казны\n• крафты и продажи\n• решения и законопроекты\n• незакрытые задачи",
        inline=False,
    )
    embed.set_footer(text=f"T-Mod • каркас отчётов • {PANEL_MARKERS['reports']}")
    return embed


def _test_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🧪 Безопасная проверка T-Mod",
        description=(
            "Проверки не создают финансовых операций, крафтов или настоящих законопроектов. "
            "Симулятор консенсуса работает в отдельной учебной карточке."
        ),
        color=CONTROL_CENTER_COLOR,
    )
    embed.add_field(
        name="🌐 Majestic API",
        value="Администратор может приватно проверить подключение к рыночной статистике RU15.",
        inline=False,
    )
    embed.add_field(
        name="⚖️ Симулятор консенсуса",
        value=(
            "Администратор становится ведущим, фейковые участники проходят весь текущий "
            "сценарий без проверки голосового канала и без доступа к рабочей базе."
        ),
        inline=False,
    )
    embed.set_footer(text=f"T-Mod • тестовый контур • {PANEL_MARKERS['test']}")
    return embed


def _channel_status_lines(channels: dict[str, discord.TextChannel | None]) -> str:
    lines: list[str] = []
    for spec in CHANNEL_SPECS:
        channel = channels.get(spec.key)
        status = f"<#{channel.id}>" if channel is not None else "❌ не найден"
        lines.append(f"**{spec.name}** — {status}")
    return "\n".join(lines)


def settings_embed(channels: dict[str, discord.TextChannel | None]) -> discord.Embed:
    ready = sum(1 for channel in channels.values() if channel is not None)
    embed = discord.Embed(
        title="⚙️ Настройки и обслуживание T-Mod",
        description=(
            "Панель показывает рабочую маршрутизацию. Кнопки доступны только администраторам; "
            "ответы и результаты диагностики отправляются лично."
        ),
        color=CONTROL_CENTER_COLOR,
    )
    embed.add_field(name=f"Каналы · {ready}/{len(CHANNEL_SPECS)}", value=_channel_status_lines(channels), inline=False)
    embed.add_field(
        name="Правило маршрутизации",
        value="Команда открывает личный интерфейс → бот публикует исходное сообщение в профильном отсеке → активные задачи дают только ссылку.",
        inline=False,
    )
    embed.add_field(
        name="Уведомления сенаторам",
        value="Администратор может подготовить общее сообщение или уведомление о консенсусе с предпросмотром и отчётом доставки.",
        inline=False,
    )
    embed.set_footer(text=f"T-Mod • панель администратора • {PANEL_MARKERS['settings']}")
    return embed


def diagnostics_embed(bot: commands.Bot | discord.Client, guild: discord.Guild) -> discord.Embed:
    channels = {
        spec.key: guild.get_channel(stored_channel_id(guild.id, spec.key) or 0)
        for spec in CHANNEL_SPECS
    }
    ready = sum(1 for channel in channels.values() if isinstance(channel, discord.TextChannel))
    latency_ms = max(0, round(float(getattr(bot, "latency", 0.0)) * 1000))
    active_crafts = len(storage.craft_active_plans(guild.id, 100))
    open_bills = len(storage.tvrs_queue_bills(guild.id, 100))
    finance = storage.finance_get_latest_state(guild.id)
    market_server_id = os.getenv("MAJESTIC_SERVER_ID", "RU15")
    market_categories = ("items", "vehicles", "clothes")
    market = {
        category: storage.market_catalog_status(market_server_id, category)
        for category in market_categories
    }
    market_alerts = [
        storage.market_alert_stats(market_server_id, category)
        for category in market_categories
    ]
    embed = discord.Embed(
        title="🩺 Диагностика T-Mod",
        color=discord.Color.green() if ready == len(CHANNEL_SPECS) else discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Discord", value=f"Задержка: **{latency_ms} мс**", inline=True)
    embed.add_field(name="Каналы", value=f"Готово: **{ready}/{len(CHANNEL_SPECS)}**", inline=True)
    embed.add_field(
        name="Рабочие данные",
        value=(
            f"Крафтов в работе: **{active_crafts}**\n"
            f"Законопроектов в очереди: **{open_bills}**\n"
            f"Остаток казны рассчитан: **{'да' if finance.get('estimated_balance') is not None else 'нет'}**\n"
            f"Рынок RU15: **{int(market['items'].get('record_count') or 0)}** предметов · "
            f"**{int(market['vehicles'].get('record_count') or 0)}** авто · "
            f"**{int(market['clothes'].get('record_count') or 0)}** вариантов одежды\n"
            f"Активных рыночных сигналов: "
            f"**{sum(entry['active'] + entry['notifying'] for entry in market_alerts)}** "
            f"(в очереди ЛС: **{sum(entry['pending_notifications'] for entry in market_alerts)}**)"
        ),
        inline=False,
    )
    embed.set_footer(text="Проверка ничего не изменяет")
    return embed


def tech_health_embed(bot: commands.Bot | discord.Client, guild: discord.Guild) -> discord.Embed:
    latency_ms = max(0, round(float(getattr(bot, "latency", 0.0)) * 1000))
    embed = discord.Embed(
        title="🛠️ Техническое состояние T-Mod",
        description="🟢 Бот подключён и принимает взаимодействия.",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Сервер", value=f"{guild.name}\n`{guild.id}`", inline=True)
    embed.add_field(name="Discord", value=f"Задержка **{latency_ms} мс**", inline=True)
    embed.add_field(name="Хранилище", value="SQLite подключено", inline=True)
    embed.set_footer(text=f"Живое состояние • {PANEL_MARKERS['tech_log']}")
    return embed


def majestic_test_embed(summary: MajesticMarketplaceSummary, diagnostics: dict[str, object]) -> discord.Embed:
    def number(value: int | float | None) -> str:
        if value is None:
            return "нет данных"
        return f"{value:,.0f}".replace(",", " ")

    embed = discord.Embed(
        title="✅ Majestic API отвечает",
        description=f"Сервер **{summary.server_id} · {summary.server_name}** доступен из T-Mod.",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Каталог", value=f"Предметы: **{number(summary.record_count)}** позиций", inline=True)
    embed.add_field(name="Период", value=f"**{summary.period_days or '—'} дней**", inline=True)
    embed.add_field(
        name="Лимит клиента",
        value=(
            f"Осталось **{diagnostics.get('remaining_process_budget', '—')}/"
            f"{diagnostics.get('requests_per_window', '—')}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Рыночная статистика",
        value=(
            f"Размещено: **{number(summary.total_count)}**\n"
            f"Продано: **{number(summary.total_sold)}**\n"
            f"Средняя цена: **{number(summary.overall_average_price)}**"
        ),
        inline=False,
    )
    embed.add_field(name="Данные Majestic обновлены", value=summary.last_updated or "нет данных", inline=False)
    embed.set_footer(text="Ответ приватный • одинаковые проверки кэшируются на 60 секунд")
    return embed


class SettingsPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not permissions or not permissions.administrator:
            await interaction.response.send_message("Панель обслуживания доступна только администратору.", ephemeral=True)
            return False
        if interaction.guild is None:
            await interaction.response.send_message("Панель работает только на сервере.", ephemeral=True)
            return False
        return True

    @discord.ui.button(
        label="Диагностика",
        emoji="🩺",
        style=discord.ButtonStyle.primary,
        custom_id="tmod_settings_diagnostics",
    )
    async def diagnostics(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        embed = await asyncio.to_thread(diagnostics_embed, interaction.client, interaction.guild)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Проверить структуру",
        emoji="🧭",
        style=discord.ButtonStyle.secondary,
        custom_id="tmod_settings_reconcile",
    )
    async def reconcile(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        channels = await ensure_control_center(interaction.client, interaction.guild)
        ready = sum(1 for channel in channels.values() if channel is not None)
        await interaction.followup.send(
            f"Структура проверена: **{ready}/{len(CHANNEL_SPECS)}** каналов готовы.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Обновить панели",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        custom_id="tmod_settings_refresh_panels",
    )
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        await ensure_control_center(interaction.client, interaction.guild)
        await refresh_operations_dashboard(interaction.client, interaction.guild)
        await interaction.followup.send("Публичные панели обновлены.", ephemeral=True)

    @discord.ui.button(
        label="Уведомления",
        emoji="📨",
        style=discord.ButtonStyle.primary,
        custom_id="tmod_settings_broadcasts",
    )
    async def broadcasts(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        from modules.admin_broadcast import open_broadcast_admin_panel

        await open_broadcast_admin_panel(interaction)

    @discord.ui.button(
        label="Веб-панель",
        emoji="🖥️",
        style=discord.ButtonStyle.secondary,
        custom_id="tmod_settings_consensus_web",
    )
    async def consensus_web(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        from modules.consensus_web import open_consensus_web_info

        await open_consensus_web_info(interaction)


class BotTestView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Проверить T-Mod",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="tmod_safe_test",
    )
    async def run_test(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Проверка работает только на сервере.", ephemeral=True)
            return
        embed = await asyncio.to_thread(diagnostics_embed, interaction.client, interaction.guild)
        embed.title = "✅ T-Mod отвечает"
        embed.description = "Тест выполнен без создания рабочих записей."
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Проверить Majestic",
        emoji="🌐",
        style=discord.ButtonStyle.primary,
        custom_id="tmod_majestic_test",
    )
    async def run_majestic_test(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Проверка работает только на сервере.", ephemeral=True)
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not permissions or not permissions.administrator:
            await interaction.response.send_message(
                "Проверка Majestic доступна только администратору.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            client = get_majestic_api_client()
            summary = await client.marketplace_summary_async("items", use_cache=True)
            embed = majestic_test_embed(summary, client.diagnostics())
        except MajesticApiError as exc:
            embed = discord.Embed(
                title="⚠️ Majestic API пока недоступен",
                description=str(exc),
                color=discord.Color.orange(),
            )
            embed.set_footer(text="Проверьте MAJESTIC_API_ENABLED, ключ и настройки сервера")
        except Exception as exc:
            traceback.print_exc()
            embed = discord.Embed(
                title="❌ Не удалось проверить Majestic API",
                description=f"Безопасная ошибка: `{type(exc).__name__}`",
                color=discord.Color.red(),
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Симулятор консенсуса",
        emoji="⚖️",
        style=discord.ButtonStyle.secondary,
        custom_id="tmod_consensus_simulation",
    )
    async def run_consensus_simulation(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await start_consensus_simulation(interaction)


async def ensure_public_control_panel(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
) -> discord.Message | None:
    channel = await resolve_control_channel(guild, "control_panel")
    if channel is None:
        return None
    provider = public_panel_provider()
    if provider is None:
        return None
    panel_factory, view_factory = provider
    panel = await asyncio.to_thread(panel_factory, guild)
    return await ensure_panel_message(
        channel,
        "control_panel",
        embed=panel,
        view=view_factory(),
    )


async def ensure_control_center(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
) -> dict[str, discord.TextChannel | None]:
    async with _setup_lock:
        channels = await resolve_all_channels(guild)
        try:
            await order_control_channels(channels)
        except discord.DiscordException:
            traceback.print_exc()

        if channels.get("control_panel") is not None:
            await ensure_public_control_panel(bot, guild)

        reports_channel = channels.get("reports")
        if reports_channel is not None:
            await ensure_panel_message(reports_channel, "reports", embed=_reports_embed())

        settings_channel = channels.get("settings")
        if settings_channel is not None:
            await ensure_panel_message(
                settings_channel,
                "settings",
                embed=settings_embed(channels),
                view=SettingsPanelView(),
            )

        test_channel = channels.get("test")
        if test_channel is not None:
            await ensure_panel_message(test_channel, "test", embed=_test_embed(), view=BotTestView())

        tech_channel = channels.get("tech_log")
        if tech_channel is not None:
            await ensure_panel_message(
                tech_channel,
                "tech_log",
                embed=tech_health_embed(bot, guild),
            )

        return channels


async def log_technical_event(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    *,
    title: str,
    details: str,
    level: str = "error",
    dedupe_key: str | None = None,
    cooldown_seconds: int = 300,
    mention_everyone: bool = False,
) -> None:
    """Compatibility facade for integrations importing from control_center."""
    register_channel_resolver(resolve_control_channel)
    await _log_technical_event(
        bot,
        guild,
        title=title,
        details=details,
        level=level,
        dedupe_key=dedupe_key,
        cooldown_seconds=cooldown_seconds,
        mention_everyone=mention_everyone,
    )


def setup_control_center(bot: commands.Bot) -> None:
    from modules.admin_broadcast import setup_admin_broadcast

    register_channel_resolver(resolve_control_channel)
    setup_admin_broadcast(bot)

    async def control_center_ready_listener() -> None:
        global _persistent_views_registered
        if not _persistent_views_registered:
            bot.add_view(SettingsPanelView())
            bot.add_view(BotTestView())
            _persistent_views_registered = True
        for guild in bot.guilds:
            try:
                await ensure_control_center(bot, guild)
            except asyncio.CancelledError:
                raise
            except Exception:
                traceback.print_exc()

    bot.add_listener(control_center_ready_listener, "on_ready")
