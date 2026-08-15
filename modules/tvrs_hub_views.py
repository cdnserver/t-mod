from __future__ import annotations

import asyncio
import traceback
from typing import Any

import discord

from modules.async_safety import run_blocking_cancellation_safe
from persistence import tvrs_repository as storage
from modules.consensus_core import ConsensusStateError
from modules.consensus_runtime import (
    active_sessions as _active_sessions,
    coordinator as _consensus,
    registry as _consensus_registry,
    session_lock as consensus_session_lock,
)
from modules.consensus_v3 import resolve_consensus_access
from modules.delivery_runtime import wake_delivery_worker
from modules.hub_runtime import open_hub_section
from modules.tvrs_config import (
    TVRS_EMBED_COLOR,
    TVRS_MATERIALS_CHANNEL_ID,
)
from modules.tvrs_embeds import (
    build_result_embed,
)
from modules.tvrs_formatting import (
    format_bill_number,
)
from modules.tvrs_navigation_runtime import open_tvrs_hub
from modules.tvrs_delivery import (
    TVRS_BILL_PUBLICATION_TOPIC,
)
from modules.technical_log import log_technical_event
from modules.consensus_web_auth import consensus_web_entry_url as build_web_entry_url

from modules.tvrs_presentation import (
    build_live_vote_embed,
    build_main_panel_embed,
    build_queue_embed,
    build_registration_embed,
    build_universality_embed,
    build_universality_help_embed,
    is_chair,
    is_senator,
)

async def ensure_sticky_message(*args, **kwargs):
    from modules.tvrs_recovery import ensure_sticky_message as _implementation
    return await _implementation(*args, **kwargs)

def TVRSAfterResultView(*args, **kwargs):
    from modules.tvrs_consensus_views import TVRSAfterResultView as _implementation
    return _implementation(*args, **kwargs)

def TVRSHostVoteView(*args, **kwargs):
    from modules.tvrs_consensus_views import TVRSHostVoteView as _implementation
    return _implementation(*args, **kwargs)

def TVRSRegistrationView(*args, **kwargs):
    from modules.tvrs_consensus_views import TVRSRegistrationView as _implementation
    return _implementation(*args, **kwargs)

class TVRSBaseView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: Any) -> None:
        expected = isinstance(error, ConsensusStateError)
        if not expected:
            traceback.print_exception(type(error), error, error.__traceback__)
        message = str(error) if expected else "Внутренняя ошибка действия. Попробуйте ещё раз или обновите панель /tvrs."
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
            else:
                await interaction.followup.send(message, ephemeral=True)
        except discord.DiscordException:
            pass
        if not expected and interaction.guild is not None:
            await log_technical_event(
                interaction.client,
                interaction.guild,
                title="Ошибка интерфейса TVRS",
                details=f"Элемент: `{getattr(item, 'custom_id', None) or getattr(item, 'label', 'неизвестно')}`\nОшибка: `{type(error).__name__}: {str(error)[:700]}`",
                dedupe_key=f"tvrs-view:{type(error).__name__}",
                cooldown_seconds=60,
            )


class TVRSRequesterView(TVRSBaseView):
    def __init__(self, requester_id: int, *, timeout: float | None = 900) -> None:
        super().__init__(timeout=timeout)
        self.requester_id = int(requester_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Это приватное меню открыто не для вас.", ephemeral=True)
            return False
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return False
        return True


class TVRSUniversalityView(TVRSRequesterView):
    def __init__(self, requester_id: int) -> None:
        super().__init__(requester_id, timeout=900)

    @discord.ui.button(label="Казна", emoji="💼", style=discord.ButtonStyle.primary, row=0)
    async def finance(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_hub_section("finance", interaction, self.requester_id, surface="replace")

    @discord.ui.button(label="Крафты", emoji="🏭", style=discord.ButtonStyle.success, row=0)
    async def craft(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_hub_section("craft", interaction, self.requester_id, surface="replace")

    @discord.ui.button(label="Аудит", emoji="🧾", style=discord.ButtonStyle.secondary, row=0)
    async def audit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_hub_section("audit", interaction, self.requester_id, surface="replace")

    @discord.ui.button(label="Консенсус", emoji="⚖️", style=discord.ButtonStyle.secondary, row=1)
    async def consensus(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        await interaction.response.edit_message(
            content=None,
            embed=build_main_panel_embed(interaction.guild),
            view=TVRSMainPanelView(
                self.requester_id,
                guild_id=interaction.guild.id,
                has_chair_access=is_chair(interaction.user),
                back_to_hub=True,
            ),
        )

    @discord.ui.button(label="Законопроекты", emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def bills(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.edit_message(
            content=None,
            embed=build_queue_embed(interaction.guild),
            view=TVRSQueueHubView(self.requester_id, interaction.guild.id),
        )

    @discord.ui.button(label="Справка", emoji="🧭", style=discord.ButtonStyle.secondary, row=1)
    async def help(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content=None,
            embed=build_universality_help_embed(),
            view=TVRSHelpView(self.requester_id),
        )

    @discord.ui.button(label="Ссылки", emoji="🔗", style=discord.ButtonStyle.secondary, row=2)
    async def links(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(
            title="🔗 Ссылки Товарищества",
            description="Выберите нужное направление. Ссылки открываются обычными кнопками Discord.",
            color=TVRS_EMBED_COLOR,
        )
        embed.set_footer(text="Меню видно только вам")
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=TVRSLinksView(self.requester_id),
        )

    @discord.ui.button(label="Рынок", emoji="📈", style=discord.ButtonStyle.primary, row=2)
    async def market(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_hub_section("market", interaction, self.requester_id, surface="replace")

    @discord.ui.button(label="Обновить", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.defer()
        embed = await asyncio.to_thread(build_universality_embed, interaction.guild, interaction.user.id)
        await interaction.edit_original_response(content=None, embed=embed, view=self)


class TVRSPublicPanelView(TVRSBaseView):
    """Persistent shared menu whose working surfaces are always private."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Панель работает только на сервере Discord.", ephemeral=True)
            return False
        return True

    @discord.ui.button(
        label="Казна",
        emoji="💼",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="tmod_public_tvrs_finance",
    )
    async def finance(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_hub_section("finance", interaction, interaction.user.id, surface="ephemeral")

    @discord.ui.button(
        label="Крафты",
        emoji="🏭",
        style=discord.ButtonStyle.success,
        row=0,
        custom_id="tmod_public_tvrs_craft",
    )
    async def craft(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_hub_section("craft", interaction, interaction.user.id, surface="ephemeral")

    @discord.ui.button(
        label="Аудит",
        emoji="🧾",
        style=discord.ButtonStyle.secondary,
        row=0,
        custom_id="tmod_public_tvrs_audit",
    )
    async def audit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_hub_section("audit", interaction, interaction.user.id, surface="ephemeral")

    @discord.ui.button(
        label="Консенсус",
        emoji="⚖️",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="tmod_public_tvrs_consensus",
    )
    async def consensus(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        await interaction.response.send_message(
            embed=build_main_panel_embed(interaction.guild),
            view=TVRSMainPanelView(
                interaction.user.id,
                guild_id=interaction.guild.id,
                has_chair_access=is_chair(interaction.user),
                back_to_hub=True,
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Законопроекты",
        emoji="📜",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="tmod_public_tvrs_bills",
    )
    async def bills(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.send_message(
            embed=build_queue_embed(interaction.guild),
            view=TVRSQueueHubView(interaction.user.id, interaction.guild.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Справка",
        emoji="🧭",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="tmod_public_tvrs_help",
    )
    async def help(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=build_universality_help_embed(),
            view=TVRSHelpView(interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Ссылки",
        emoji="🔗",
        style=discord.ButtonStyle.secondary,
        row=2,
        custom_id="tmod_public_tvrs_links",
    )
    async def links(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(
            title="🔗 Ссылки Товарищества",
            description="Выберите нужное направление. Ссылки открываются обычными кнопками Discord.",
            color=TVRS_EMBED_COLOR,
        )
        embed.set_footer(text="Меню видно только вам")
        await interaction.response.send_message(
            embed=embed,
            view=TVRSLinksView(interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Рынок",
        emoji="📈",
        style=discord.ButtonStyle.primary,
        row=2,
        custom_id="tmod_public_tvrs_market",
    )
    async def market(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_hub_section("market", interaction, interaction.user.id, surface="ephemeral")

    @discord.ui.button(
        label="Обновить",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        row=2,
        custom_id="tmod_public_tvrs_refresh",
    )
    async def personal(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await asyncio.to_thread(build_universality_embed, interaction.guild, interaction.user.id)
        await interaction.followup.send(
            embed=embed,
            view=TVRSUniversalityView(interaction.user.id),
            ephemeral=True,
        )


class TVRSQueueHubView(TVRSRequesterView):
    def __init__(self, requester_id: int, guild_id: int) -> None:
        super().__init__(requester_id, timeout=900)
        self.add_item(
            discord.ui.Button(
                label="Перейти к подаче",
                emoji="📝",
                style=discord.ButtonStyle.link,
                url=build_web_entry_url(
                    "https://tvr.lat",
                    guild_id=int(guild_id),
                    user_id=int(requester_id),
                    destination="/reactor",
                ) + "#editor",
            )
        )

    @discord.ui.button(label="Назад", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_tvrs_hub(interaction)


class TVRSHelpView(TVRSRequesterView):
    @discord.ui.button(label="Назад", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_tvrs_hub(interaction)


class TVRSLinksView(TVRSRequesterView):
    def __init__(self, requester_id: int) -> None:
        from modules.links import DISCORD_BUREAU_LINK, DISCORD_TVRS_LINK

        super().__init__(requester_id, timeout=900)
        self.add_item(
            discord.ui.Button(
                label="Доступ к Бюро",
                emoji="⚖️",
                style=discord.ButtonStyle.link,
                url=DISCORD_BUREAU_LINK,
                row=0,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Заявка в Товарищество",
                emoji="🏛️",
                style=discord.ButtonStyle.link,
                url=DISCORD_TVRS_LINK,
                row=0,
            )
        )

    @discord.ui.button(label="Назад", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await open_tvrs_hub(interaction)


class TVRSStickyView(TVRSBaseView):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Создать", style=discord.ButtonStyle.secondary, custom_id="tvrs_bill_create")
    async def create_bill(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        if not is_senator(interaction.user):
            await interaction.response.send_message("Законопроект может предложить только сенатор или председатель Товарищества.", ephemeral=True)
            return
        url = build_web_entry_url(
            "https://tvr.lat",
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            destination="/reactor",
        ) + "#editor"
        view = discord.ui.View(timeout=300)
        view.add_item(discord.ui.Button(label="Открыть мастерскую", emoji="📝", style=discord.ButtonStyle.link, url=url))
        await interaction.response.send_message(
            "Законопроекты теперь создаются в личном Реакторе. Там доступны блоки исполнения, история и модерация.",
            view=view,
            ephemeral=True,
        )


class TVRSBillModal(discord.ui.Modal):
    def __init__(self, next_number: int) -> None:
        super().__init__(title=f"Драфт законопроекта {format_bill_number(next_number)}"[:45], timeout=900)
        self.heading = discord.ui.TextInput(
            label="Заголовок законопроекта",
            placeholder="О принятии Имя Фамилия в состав Товарищества",
            min_length=5,
            max_length=180,
            required=True,
        )
        self.summary = discord.ui.TextInput(
            label="Суть законопроекта",
            placeholder="Предлагается рассмотреть вступление кандидата и условия его участия в Товариществе.",
            style=discord.TextStyle.paragraph,
            min_length=10,
            max_length=3000,
            required=True,
        )
        self.materials = discord.ui.TextInput(
            label="Материалы",
            placeholder="Ссылки через запятую: https://example.com/doc, https://example.com/proof",
            style=discord.TextStyle.paragraph,
            min_length=0,
            max_length=1000,
            required=False,
        )
        self.add_item(self.heading)
        self.add_item(self.summary)
        self.add_item(self.materials)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or interaction.channel is None:
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        if interaction.channel_id != TVRS_MATERIALS_CHANNEL_ID:
            await interaction.response.send_message(f"Эта система работает только в канале <#{TVRS_MATERIALS_CHANNEL_ID}>.", ephemeral=True)
            return
        if not is_senator(interaction.user):
            await interaction.response.send_message("Законопроект может предложить только сенатор или председатель Товарищества.", ephemeral=True)
            return
        active = _active_sessions.get(interaction.guild.id)
        if active and not active.finished:
            await interaction.response.send_message("Сейчас идет пленарный консенсус. Подача новых законопроектов будет снова доступна после завершения.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with consensus_session_lock(interaction.guild.id):
                active = _active_sessions.get(interaction.guild.id)
                if active and not active.finished:
                    await interaction.followup.send(
                        "Сейчас идёт пленарный консенсус. Подача новых законопроектов будет снова доступна после завершения.",
                        ephemeral=True,
                    )
                    return
                bill, _, created = await asyncio.to_thread(
                    storage.tvrs_create_bill_with_publication,
                    guild_id=interaction.guild.id,
                    channel_id=interaction.channel_id,
                    author_id=interaction.user.id,
                    author_display=interaction.user.display_name,
                    title=str(self.heading.value).strip(),
                    summary=str(self.summary.value).strip(),
                    materials=str(self.materials.value).strip() or None,
                    delivery_topic=TVRS_BILL_PUBLICATION_TOPIC,
                )
            wake_delivery_worker()
            await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)
            state = "создан и поставлен в надёжную очередь публикации" if created else "уже был создан; публикация продолжится автоматически"
            await interaction.followup.send(
                f"Законопроект **{format_bill_number(bill.bill_number)}** {state}.",
                ephemeral=True,
            )
        except ValueError as exc:
            if str(exc) == "bill_submission_locked_by_active_consensus":
                await interaction.followup.send(
                    "Сейчас идёт пленарный консенсус. Подача новых законопроектов будет снова доступна после завершения.",
                    ephemeral=True,
                )
                return
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            await interaction.followup.send(f"Не удалось создать законопроект: {str(exc)[:800]}", ephemeral=True)
        except Exception as exc:
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            await interaction.followup.send(f"Не удалось создать законопроект: {str(exc)[:800]}", ephemeral=True)


class TVRSMainPanelView(TVRSBaseView):
    def __init__(
        self,
        requester_id: int,
        *,
        guild_id: int = 0,
        has_chair_access: bool = False,
        back_to_hub: bool = False,
    ) -> None:
        super().__init__(timeout=600)
        self.requester_id = int(requester_id)
        self.guild_id = int(guild_id)
        self.has_chair_access = bool(has_chair_access)
        self.back_to_hub = back_to_hub
        session = _active_sessions.get(self.guild_id)
        access = resolve_consensus_access(
            session,
            user_id=self.requester_id,
            has_chair_access=self.has_chair_access,
        )
        primary_styles = {
            "prepare": discord.ButtonStyle.success,
            "manage": discord.ButtonStyle.primary,
            "participate": discord.ButtonStyle.primary,
            "observe": discord.ButtonStyle.secondary,
            "unavailable": discord.ButtonStyle.secondary,
        }
        primary = discord.ui.Button(
            label=access.primary_label,
            emoji={
                "prepare": "🧭",
                "manage": "🎛️",
                "participate": "🗳️",
                "observe": "👁️",
                "unavailable": "⏸️",
            }[access.primary_action],
            style=primary_styles[access.primary_action],
            disabled=access.primary_action == "unavailable",
            row=0,
        )
        primary.callback = self.primary_action
        self.add_item(primary)
        if self.has_chair_access:
            schedule = discord.ui.Button(
                label="Планирование",
                emoji="🗓️",
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            schedule.callback = self.schedule
            self.add_item(schedule)
        if session is not None and not session.finished and access.primary_action != "observe":
            observer = discord.ui.Button(
                label="Режим наблюдения",
                emoji="👁️",
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            observer.callback = self.observe
            self.add_item(observer)
        if (
            session is not None
            and not session.finished
            and self.has_chair_access
            and int(session.leader_id) != self.requester_id
        ):
            administration = discord.ui.Button(
                label="Восстановление",
                emoji="🛟",
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            administration.callback = self.administration
            self.add_item(administration)
        queue = discord.ui.Button(
            label="Очередь",
            emoji="📚",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        queue.callback = self.queue
        self.add_item(queue)
        refresh = discord.ui.Button(
            label="Обновить",
            emoji="🔄",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        refresh.callback = self.refresh
        self.add_item(refresh)
        if back_to_hub:
            back = discord.ui.Button(
                label="Назад",
                emoji="⬅️",
                style=discord.ButtonStyle.secondary,
                row=1,
            )
            back.callback = self.back
            self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Это меню открыто не для вас.", ephemeral=True)
            return False
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return False
        return True

    async def primary_action(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        session = _active_sessions.get(interaction.guild.id)
        access = resolve_consensus_access(
            session,
            user_id=interaction.user.id,
            has_chair_access=is_chair(interaction.user),
        )
        if access.primary_action == "prepare":
            from modules.tvrs_consensus_portal import open_preparation_portal

            await open_preparation_portal(interaction)
            return
        if access.primary_action == "manage":
            await self.open_active_consensus(interaction)
            return
        if access.primary_action == "participate" and session is not None:
            from modules.tvrs_consensus_portal import open_participant_portal

            await open_participant_portal(interaction, session)
            return
        if access.primary_action == "observe" and session is not None:
            from modules.tvrs_consensus_portal import open_observer_portal

            await open_observer_portal(interaction, session)
            return
        await interaction.response.send_message(
            "Активного заседания сейчас нет.",
            ephemeral=True,
        )

    async def schedule(self, interaction: discord.Interaction) -> None:
        from modules.consensus_schedule import open_consensus_schedule

        await open_consensus_schedule(interaction)

    async def open_active_consensus(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = _active_sessions.get(interaction.guild.id)
        if session is None or session.finished:
            await interaction.response.send_message("Активного заседания сейчас нет.", ephemeral=True)
            return
        if interaction.user.id != session.leader_id:
            await interaction.response.send_message(
                f"Активным заседанием управляет ведущий <@{session.leader_id}>.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        previous_host = None
        async with consensus_session_lock(interaction.guild.id):
            current = _consensus_registry.find(session.session_key)
            if current is not session or session.finished:
                embed = None
                view = None
            else:
                previous_host = session.host_message_obj
                session.host_message_obj = interaction.message
                await run_blocking_cancellation_safe(
                    _consensus.bind_host_message,
                    session,
                    getattr(interaction.message, "id", None),
                    "host_panel_reopened",
                )
                if session.stage == "registration":
                    embed = build_registration_embed(session)
                    view = TVRSRegistrationView(session.session_key)
                elif session.stage == "after_result" and session.results:
                    embed = build_result_embed(session.results[-1], session)
                    view = TVRSAfterResultView(session.session_key)
                else:
                    embed = build_live_vote_embed(session)
                    view = TVRSHostVoteView(session.session_key)
        if embed is None:
            await interaction.edit_original_response(
                content="Заседание уже завершено или было заменено новым.",
                embed=None,
                view=None,
            )
            return
        if previous_host is not None and previous_host is not interaction.message and hasattr(previous_host, "edit"):
            try:
                await previous_host.edit(view=None)
            except Exception:
                pass
        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

    async def administration(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        session = _active_sessions.get(interaction.guild.id)
        if session is None or session.finished:
            await interaction.response.send_message("Активного заседания сейчас нет.", ephemeral=True)
            return
        if not is_chair(interaction.user):
            await interaction.response.send_message("Восстановление доступно только председателю.", ephemeral=True)
            return
        from modules.tvrs_consensus_admin import open_consensus_admin_panel

        await open_consensus_admin_panel(interaction, session)

    async def observe(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = _active_sessions.get(interaction.guild.id)
        if session is None or session.finished:
            await interaction.response.send_message(
                "Активного заседания сейчас нет.",
                ephemeral=True,
            )
            return
        from modules.tvrs_consensus_portal import open_observer_portal

        await open_observer_portal(interaction, session)

    async def queue(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.send_message(
            embed=build_queue_embed(interaction.guild),
            ephemeral=True,
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        await interaction.response.edit_message(
            content=None,
            embed=build_main_panel_embed(interaction.guild),
            view=TVRSMainPanelView(
                self.requester_id,
                guild_id=interaction.guild.id,
                has_chair_access=is_chair(interaction.user),
                back_to_hub=self.back_to_hub,
            ),
        )

    async def back(self, interaction: discord.Interaction) -> None:
        await open_tvrs_hub(interaction)

__all__ = ['TVRSBaseView', 'TVRSRequesterView', 'TVRSUniversalityView', 'TVRSPublicPanelView', 'TVRSQueueHubView', 'TVRSHelpView', 'TVRSLinksView', 'TVRSStickyView', 'TVRSBillModal', 'TVRSMainPanelView']
