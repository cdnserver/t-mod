"""Private, durable and AI-assisted editor for Tovarischestvo bills."""

from __future__ import annotations

import asyncio
import re
import traceback
from typing import Any

import discord

from modules.bill_editor_ai import (
    BILL_EDITOR_MODEL,
    CATEGORY_LABELS,
    generate_bill_editor_draft,
)
from modules.consensus_runtime import (
    active_sessions,
    session_lock as consensus_session_lock,
)
from modules.delivery_runtime import wake_delivery_worker
from modules.technical_log import log_technical_event
from modules.tvrs_config import TVRS_EMBED_COLOR, TVRS_MATERIALS_CHANNEL_ID
from modules.tvrs_delivery import TVRS_BILL_PUBLICATION_TOPIC
from modules.tvrs_formatting import format_bill_number
from persistence import bill_workspace_repository as workspace_storage
from persistence import tvrs_repository as bill_storage


WORKSPACE_ERROR_MESSAGES = {
    "bill_workspace_revision_conflict": (
        "Черновик уже изменился в другом окне. Панель обновлена — повторите действие."
    ),
    "bill_workspace_not_editable": "Эта редакционная комната уже закрыта.",
    "bill_workspace_category_invalid": "Категория решения не распознана.",
    "bill_editor_ai_not_configured": (
        "ИИ-редактор не настроен. Ручной редактор продолжает работать."
    ),
    "bill_editor_ai_payload_invalid": (
        "ИИ вернул неполный черновик. Исходная идея сохранена — нажмите «Улучшить ИИ» ещё раз."
    ),
    "bill_editor_ai_category_invalid": "ИИ предложил неизвестную категорию решения.",
}
CATEGORY_ALIASES = {
    "ordinary": "ordinary",
    "обычное": "ordinary",
    "50": "ordinary",
    "heavy": "heavy",
    "усиленное": "heavy",
    "75": "heavy",
    "unanimous": "unanimous",
    "единогласное": "unanimous",
    "100": "unanimous",
}


def _safe(value: Any, *, fallback: str = "Не заполнено") -> str:
    text = str(value or "").strip()
    return discord.utils.escape_markdown(text) if text else fallback


def _truncate(value: Any, maximum: int) -> str:
    text = str(value or "").strip()
    if len(text) <= maximum:
        return text
    return text[: max(0, maximum - 1)].rstrip() + "…"


def bill_workspace_embed(workspace: dict[str, Any]) -> discord.Embed:
    status = str(workspace.get("status") or "draft")
    title = str(workspace.get("title") or "").strip()
    ready = all(
        str(workspace.get(key) or "").strip()
        for key in ("title", "summary", "implementation_plan", "leadership_actions")
    )
    if status == "submitted":
        description = (
            "✅ Черновик опубликован как законопроект. Редакционная комната сохранена "
            "как история подготовки и больше не принимает изменения."
        )
    elif status == "cancelled":
        description = "Черновик отменён автором."
    else:
        description = (
            "Приватная редакционная комната. Опишите идею, улучшите текст с ИИ или "
            "отредактируйте всё вручную. Публикация произойдёт только после подтверждения."
        )
    embed = discord.Embed(
        title=f"Редактор законопроекта · #{int(workspace['id']):04d}",
        description=description,
        color=0x3BA55D if ready else TVRS_EMBED_COLOR,
    )
    embed.add_field(
        name="Проект",
        value=(
            f"**{_safe(title)}**\n{_truncate(_safe(workspace.get('summary')), 850)}"
        )[:1024],
        inline=False,
    )
    category = str(workspace.get("decision_category") or "ordinary")
    embed.add_field(
        name="Категория решения",
        value=CATEGORY_LABELS.get(category, CATEGORY_LABELS["ordinary"]),
        inline=True,
    )
    embed.add_field(
        name="Редакция",
        value=(
            f"Черновик **v{int(workspace.get('revision') or 1)}** · "
            f"ИИ-правок: **{int(workspace.get('ai_revision') or 0)}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="После принятия",
        value=_truncate(_safe(workspace.get("implementation_plan")), 900),
        inline=False,
    )
    embed.add_field(
        name="Действия руководства",
        value=_truncate(_safe(workspace.get("leadership_actions")), 900),
        inline=False,
    )
    if workspace.get("idea"):
        embed.add_field(
            name="Исходная идея автора",
            value=_truncate(_safe(workspace.get("idea")), 700),
            inline=False,
        )
    if status in {"draft", "review"}:
        embed.set_footer(
            text=(
                "Готово к публикации"
                if ready
                else "Для публикации нужны текст проекта и план исполнения"
            )
        )
    return embed


async def _workspace_channel(
    bot: discord.Client,
    workspace: dict[str, Any],
) -> discord.Thread | None:
    thread_id = int(workspace.get("thread_id") or 0)
    if not thread_id:
        return None
    channel = bot.get_channel(thread_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden):
            return None
    return channel if isinstance(channel, discord.Thread) else None


async def refresh_bill_workspace_panel(
    bot: discord.Client,
    workspace: dict[str, Any],
) -> None:
    thread = await _workspace_channel(bot, workspace)
    if thread is None:
        return
    status = str(workspace.get("status") or "draft")
    view = BillWorkspaceView(int(workspace["id"])) if status in {"draft", "review"} else None
    message_id = int(workspace.get("panel_message_id") or 0)
    message = None
    if message_id:
        try:
            message = await thread.fetch_message(message_id)
        except discord.NotFound:
            message = None
    if message is not None:
        await message.edit(
            embed=bill_workspace_embed(workspace),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    sent = await thread.send(
        embed=bill_workspace_embed(workspace),
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    if status in {"draft", "review"}:
        await asyncio.to_thread(
            workspace_storage.attach_bill_workspace_panel,
            int(workspace["id"]),
            panel_message_id=int(sent.id),
        )


async def _report_editor_error(
    interaction: discord.Interaction,
    error: Exception,
) -> None:
    expected = isinstance(error, (ValueError, RuntimeError)) and str(error) in (
        WORKSPACE_ERROR_MESSAGES
    )
    if not expected:
        traceback.print_exception(type(error), error, error.__traceback__)
        if interaction.guild is not None:
            await log_technical_event(
                interaction.client,
                interaction.guild,
                title="Ошибка редактора законопроектов",
                details=(
                    f"Пользователь: <@{interaction.user.id}> (`{interaction.user.id}`)\n"
                    f"Ошибка: `{type(error).__name__}: {str(error)[:700]}`"
                ),
                dedupe_key=f"bill-editor:{type(error).__name__}",
                cooldown_seconds=60,
            )
    message = WORKSPACE_ERROR_MESSAGES.get(
        str(error),
        "Не удалось выполнить действие. Черновик сохранён; попробуйте ещё раз.",
    )
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.DiscordException:
        pass


class BillWorkspaceModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _report_editor_error(interaction, error)


class BillIdeaModal(BillWorkspaceModal, title="Идея законопроекта"):
    idea = discord.ui.TextInput(
        label="Что вы предлагаете",
        placeholder="Опишите проблему и вашу идею решения",
        style=discord.TextStyle.paragraph,
        min_length=10,
        max_length=1800,
    )
    desired_outcome = discord.ui.TextInput(
        label="Какой результат нужен",
        placeholder="Что должно измениться после принятия",
        style=discord.TextStyle.paragraph,
        min_length=5,
        max_length=1000,
    )
    constraints_text = discord.ui.TextInput(
        label="Условия, сроки, ссылки",
        placeholder="Необязательно: ограничения, документы, ответственные",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, workspace: dict[str, Any]) -> None:
        super().__init__(timeout=600)
        self.workspace = workspace
        self.idea.default = str(workspace.get("idea") or "")
        self.desired_outcome.default = str(workspace.get("desired_outcome") or "")
        self.constraints_text.default = str(workspace.get("constraints_text") or "")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != int(self.workspace["author_id"]):
            await interaction.response.send_message("Это чужой черновик.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        updated = await asyncio.to_thread(
            workspace_storage.update_bill_workspace,
            int(self.workspace["id"]),
            expected_revision=int(self.workspace["revision"]),
            idea=str(self.idea.value),
            desired_outcome=str(self.desired_outcome.value),
            constraints_text=str(self.constraints_text.value),
            status="review",
        )
        try:
            draft = await asyncio.to_thread(
                generate_bill_editor_draft,
                idea=str(updated.get("idea") or ""),
                desired_outcome=str(updated.get("desired_outcome") or ""),
                constraints_text=str(updated.get("constraints_text") or ""),
                current_draft=updated,
            )
            updated = await asyncio.to_thread(
                workspace_storage.update_bill_workspace,
                int(updated["id"]),
                expected_revision=int(updated["revision"]),
                title=draft.title,
                summary=draft.summary,
                materials=draft.materials or "",
                decision_category=draft.decision_category,
                implementation_plan=draft.implementation_plan,
                leadership_actions=draft.leadership_actions,
                ai_model=BILL_EDITOR_MODEL,
                increment_ai_revision=True,
            )
            await refresh_bill_workspace_panel(interaction.client, updated)
            message = "ИИ подготовил черновик. Проверьте текст и план исполнения."
            if draft.clarification:
                message += f"\n\n**Стоит уточнить:** {_truncate(draft.clarification, 700)}"
            await interaction.followup.send(message, ephemeral=True)
        except Exception as exc:
            await refresh_bill_workspace_panel(interaction.client, updated)
            await _report_editor_error(interaction, exc)


def _normalize_category(value: str) -> str:
    raw = str(value or "").strip().lower().replace("%", "")
    category = CATEGORY_ALIASES.get(raw)
    if category is None:
        raise ValueError("bill_workspace_category_invalid")
    return category


class BillTextModal(BillWorkspaceModal, title="Текст законопроекта"):
    heading = discord.ui.TextInput(
        label="Название",
        min_length=5,
        max_length=180,
    )
    summary = discord.ui.TextInput(
        label="Текст и суть",
        style=discord.TextStyle.paragraph,
        min_length=20,
        max_length=3000,
    )
    materials = discord.ui.TextInput(
        label="Материалы",
        placeholder="Ссылки или приложения; можно оставить пустым",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )
    category = discord.ui.TextInput(
        label="Категория: ordinary / heavy / unanimous",
        placeholder="ordinary",
        min_length=2,
        max_length=20,
    )

    def __init__(self, workspace: dict[str, Any]) -> None:
        super().__init__(timeout=600)
        self.workspace = workspace
        self.heading.default = str(workspace.get("title") or "")
        self.summary.default = str(workspace.get("summary") or "")
        self.materials.default = str(workspace.get("materials") or "")
        self.category.default = str(workspace.get("decision_category") or "ordinary")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != int(self.workspace["author_id"]):
            await interaction.response.send_message("Это чужой черновик.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        updated = await asyncio.to_thread(
            workspace_storage.update_bill_workspace,
            int(self.workspace["id"]),
            expected_revision=int(self.workspace["revision"]),
            title=str(self.heading.value),
            summary=str(self.summary.value),
            materials=str(self.materials.value),
            decision_category=_normalize_category(str(self.category.value)),
            status="review",
        )
        await refresh_bill_workspace_panel(interaction.client, updated)
        await interaction.followup.send("Текст черновика сохранён.", ephemeral=True)


class BillExecutionModal(BillWorkspaceModal, title="Исполнение решения"):
    implementation_plan = discord.ui.TextInput(
        label="Что произойдёт после принятия",
        placeholder="Этапы, сроки, результат",
        style=discord.TextStyle.paragraph,
        min_length=5,
        max_length=1800,
    )
    leadership_actions = discord.ui.TextInput(
        label="Что нужно сделать руководству",
        placeholder="Конкретный чек-лист исполнения и контроля",
        style=discord.TextStyle.paragraph,
        min_length=5,
        max_length=1800,
    )

    def __init__(self, workspace: dict[str, Any]) -> None:
        super().__init__(timeout=600)
        self.workspace = workspace
        self.implementation_plan.default = str(
            workspace.get("implementation_plan") or ""
        )
        self.leadership_actions.default = str(
            workspace.get("leadership_actions") or ""
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != int(self.workspace["author_id"]):
            await interaction.response.send_message("Это чужой черновик.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        updated = await asyncio.to_thread(
            workspace_storage.update_bill_workspace,
            int(self.workspace["id"]),
            expected_revision=int(self.workspace["revision"]),
            implementation_plan=str(self.implementation_plan.value),
            leadership_actions=str(self.leadership_actions.value),
            status="review",
        )
        await refresh_bill_workspace_panel(interaction.client, updated)
        await interaction.followup.send("План исполнения сохранён.", ephemeral=True)


async def _load_owned_workspace(
    interaction: discord.Interaction,
    workspace_id: int,
) -> dict[str, Any] | None:
    workspace = await asyncio.to_thread(
        workspace_storage.get_bill_workspace,
        int(workspace_id),
    )
    if (
        workspace is None
        or interaction.guild is None
        or int(workspace["guild_id"]) != interaction.guild.id
        or int(workspace["author_id"]) != interaction.user.id
    ):
        await interaction.response.send_message(
            "Эта редакционная комната вам недоступна.",
            ephemeral=True,
        )
        return None
    if str(workspace.get("status")) not in {"draft", "review"}:
        await interaction.response.send_message(
            "Эта редакционная комната уже закрыта.",
            ephemeral=True,
        )
        return None
    return workspace


class BillWorkspaceView(discord.ui.View):
    def __init__(self, workspace_id: int) -> None:
        super().__init__(timeout=None)
        self.workspace_id = int(workspace_id)
        controls = (
            ("Описать идею", "💡", discord.ButtonStyle.primary, 0, self.idea),
            ("Улучшить ИИ", "✨", discord.ButtonStyle.success, 0, self.ai),
            ("Править текст", "✏️", discord.ButtonStyle.secondary, 0, self.text),
            ("План исполнения", "📋", discord.ButtonStyle.secondary, 1, self.execution),
            ("Опубликовать", "✅", discord.ButtonStyle.success, 1, self.submit),
            ("Отменить", "🗑️", discord.ButtonStyle.danger, 1, self.cancel),
        )
        for key, emoji, style, row, callback in controls:
            slug = {
                "Описать идею": "idea",
                "Улучшить ИИ": "ai",
                "Править текст": "text",
                "План исполнения": "execution",
                "Опубликовать": "submit",
                "Отменить": "cancel",
            }[key]
            button = discord.ui.Button(
                label=key,
                emoji=emoji,
                style=style,
                row=row,
                custom_id=f"tvrs:bill-editor:{self.workspace_id}:{slug}",
            )
            button.callback = callback
            self.add_item(button)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        await _report_editor_error(interaction, error)

    async def idea(self, interaction: discord.Interaction) -> None:
        workspace = await _load_owned_workspace(interaction, self.workspace_id)
        if workspace:
            await interaction.response.send_modal(BillIdeaModal(workspace))

    async def text(self, interaction: discord.Interaction) -> None:
        workspace = await _load_owned_workspace(interaction, self.workspace_id)
        if workspace:
            await interaction.response.send_modal(BillTextModal(workspace))

    async def execution(self, interaction: discord.Interaction) -> None:
        workspace = await _load_owned_workspace(interaction, self.workspace_id)
        if workspace:
            await interaction.response.send_modal(BillExecutionModal(workspace))

    async def ai(self, interaction: discord.Interaction) -> None:
        workspace = await _load_owned_workspace(interaction, self.workspace_id)
        if workspace is None:
            return
        if not workspace.get("idea") or not workspace.get("desired_outcome"):
            await interaction.response.send_message(
                "Сначала нажмите **«Описать идею»**.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            draft = await asyncio.to_thread(
                generate_bill_editor_draft,
                idea=str(workspace.get("idea") or ""),
                desired_outcome=str(workspace.get("desired_outcome") or ""),
                constraints_text=str(workspace.get("constraints_text") or ""),
                current_draft=workspace,
            )
            updated = await asyncio.to_thread(
                workspace_storage.update_bill_workspace,
                int(workspace["id"]),
                expected_revision=int(workspace["revision"]),
                title=draft.title,
                summary=draft.summary,
                materials=draft.materials or "",
                decision_category=draft.decision_category,
                implementation_plan=draft.implementation_plan,
                leadership_actions=draft.leadership_actions,
                ai_model=BILL_EDITOR_MODEL,
                increment_ai_revision=True,
                status="review",
            )
            await refresh_bill_workspace_panel(interaction.client, updated)
            message = "Черновик улучшен. Перед публикацией проверьте каждую формулировку."
            if draft.clarification:
                message += f"\n\n**Стоит уточнить:** {_truncate(draft.clarification, 700)}"
            await interaction.followup.send(message, ephemeral=True)
        except Exception as exc:
            await _report_editor_error(interaction, exc)

    async def submit(self, interaction: discord.Interaction) -> None:
        workspace = await _load_owned_workspace(interaction, self.workspace_id)
        if workspace is None:
            return
        missing = [
            label
            for key, label in (
                ("title", "название"),
                ("summary", "текст"),
                ("implementation_plan", "план исполнения"),
                ("leadership_actions", "действия руководства"),
            )
            if not str(workspace.get(key) or "").strip()
        ]
        if missing:
            await interaction.response.send_message(
                "Перед публикацией заполните: " + ", ".join(missing) + ".",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Опубликовать законопроект?",
                description=(
                    f"**{_safe(workspace.get('title'))}**\n\n"
                    "После подтверждения проект получит номер и попадёт в надёжную "
                    "очередь публикации. Черновик станет доступен только для чтения."
                ),
                color=0xF1C40F,
            ),
            view=BillSubmitConfirmView(int(workspace["id"]), interaction.user.id),
            ephemeral=True,
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        workspace = await _load_owned_workspace(interaction, self.workspace_id)
        if workspace is None:
            return
        await interaction.response.defer(ephemeral=True)
        closed = await asyncio.to_thread(
            workspace_storage.finish_bill_workspace,
            int(workspace["id"]),
            author_id=interaction.user.id,
            status="cancelled",
        )
        await refresh_bill_workspace_panel(interaction.client, closed)
        if isinstance(interaction.channel, discord.Thread):
            try:
                await interaction.channel.edit(archived=True, locked=True)
            except discord.DiscordException:
                pass
        await interaction.followup.send("Черновик отменён.", ephemeral=True)


class BillSubmitConfirmView(discord.ui.View):
    def __init__(self, workspace_id: int, author_id: int) -> None:
        super().__init__(timeout=120)
        self.workspace_id = int(workspace_id)
        self.author_id = int(author_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Это чужое подтверждение.", ephemeral=True)
            return False
        return True

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        await _report_editor_error(interaction, error)

    @discord.ui.button(label="Да, опубликовать", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        workspace = await _load_owned_workspace(interaction, self.workspace_id)
        if workspace is None:
            return
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with consensus_session_lock(interaction.guild.id):
            session = active_sessions.get(interaction.guild.id)
            if session and not session.finished:
                await interaction.followup.send(
                    "Сейчас идёт пленарный консенсус. Публикация снова откроется после завершения.",
                    ephemeral=True,
                )
                return
            bill, _, created = await asyncio.to_thread(
                bill_storage.tvrs_create_bill_with_publication,
                guild_id=interaction.guild.id,
                channel_id=TVRS_MATERIALS_CHANNEL_ID,
                author_id=interaction.user.id,
                author_display=getattr(interaction.user, "display_name", str(interaction.user)),
                title=str(workspace["title"]),
                summary=str(workspace["summary"]),
                materials=str(workspace.get("materials") or "") or None,
                decision_category=str(workspace.get("decision_category") or "ordinary"),
                implementation_plan=str(workspace.get("implementation_plan") or ""),
                leadership_actions=str(workspace.get("leadership_actions") or ""),
                editor_workspace_id=int(workspace["id"]),
                delivery_topic=TVRS_BILL_PUBLICATION_TOPIC,
            )
            closed = await asyncio.to_thread(
                workspace_storage.finish_bill_workspace,
                int(workspace["id"]),
                author_id=interaction.user.id,
                status="submitted",
                submitted_bill_id=int(bill.id),
            )
        wake_delivery_worker()
        from modules.tvrs_recovery import ensure_sticky_message

        await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)
        await refresh_bill_workspace_panel(interaction.client, closed)
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="Законопроект принят системой",
                description=(
                    f"Проект **{format_bill_number(bill.bill_number)}** "
                    + (
                        "поставлен в надёжную очередь публикации."
                        if created
                        else "уже существовал; публикация безопасно продолжена."
                    )
                ),
                color=0x3BA55D,
            ),
            view=None,
        )

    @discord.ui.button(label="Вернуться к правкам", style=discord.ButtonStyle.secondary)
    async def back(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Публикация отменена. Черновик не изменён.",
            embed=None,
            view=None,
        )


async def start_bill_workspace(interaction: discord.Interaction) -> None:
    if (
        interaction.guild is None
        or not isinstance(interaction.user, discord.Member)
        or not isinstance(interaction.channel, discord.TextChannel)
    ):
        await interaction.response.send_message(
            "Редактор работает в канале подачи законопроектов.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    workspace, created = await asyncio.to_thread(
        workspace_storage.create_or_get_bill_workspace,
        guild_id=interaction.guild.id,
        author_id=interaction.user.id,
        author_display=interaction.user.display_name,
        parent_channel_id=interaction.channel.id,
    )
    thread = await _workspace_channel(interaction.client, workspace)
    try:
        if thread is None:
            clean_name = re.sub(r"[^\wа-яА-ЯёЁ-]+", "-", interaction.user.display_name)
            thread = await interaction.channel.create_thread(
                name=f"редактор-{int(workspace['id']):04d}-{clean_name}"[:100],
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=1440,
                reason=f"Редактор законопроекта для {interaction.user} ({interaction.user.id})",
            )
            await thread.add_user(interaction.user)
            workspace = await asyncio.to_thread(
                workspace_storage.attach_bill_workspace_thread,
                int(workspace["id"]),
                thread_id=int(thread.id),
            )
        elif thread.archived:
            await thread.edit(archived=False)
        if not workspace.get("panel_message_id"):
            await refresh_bill_workspace_panel(interaction.client, workspace)
            workspace = await asyncio.to_thread(
                workspace_storage.get_bill_workspace,
                int(workspace["id"]),
            )
        await interaction.followup.send(
            (
                f"{'Создана' if created else 'Открыта существующая'} приватная "
                f"редакционная комната: {thread.mention}"
            ),
            ephemeral=True,
        )
    except Exception:
        if created:
            try:
                await asyncio.to_thread(
                    workspace_storage.finish_bill_workspace,
                    int(workspace["id"]),
                    author_id=interaction.user.id,
                    status="cancelled",
                )
            except Exception:
                traceback.print_exc()
        raise


async def restore_bill_workspace_views(bot: discord.Client) -> None:
    workspaces = await asyncio.to_thread(workspace_storage.list_open_bill_workspaces)
    for workspace in workspaces:
        bot.add_view(BillWorkspaceView(int(workspace["id"])))


def register_bill_workspace_views(bot: discord.Client) -> None:
    """Register persistent controls before Discord starts dispatching events."""

    for workspace in workspace_storage.list_open_bill_workspaces():
        bot.add_view(BillWorkspaceView(int(workspace["id"])))


__all__ = [
    "BillExecutionModal",
    "BillIdeaModal",
    "BillSubmitConfirmView",
    "BillTextModal",
    "BillWorkspaceView",
    "bill_workspace_embed",
    "refresh_bill_workspace_panel",
    "register_bill_workspace_views",
    "restore_bill_workspace_views",
    "start_bill_workspace",
]
