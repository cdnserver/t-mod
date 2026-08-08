"""Administrator-created, policy-aware DM broadcasts to all senators."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import discord
from discord.ext import commands

from modules.delivery_outbox import DeliveryDeferred, DeliveryReceipt, OutboxMessage
from modules.delivery_runtime import register_delivery_handler, wake_delivery_worker
from modules.discord_delivery import (
    DeliveryDestinationUnavailable,
    raise_classified_discord_error,
    resolve_delivery_member,
)
from modules.profile_notifications import evaluate_profile_notification
from modules.tvrs_config import (
    TVRS_COCHAIR_IDS,
    TVRS_EMBED_COLOR,
    TVRS_SENATOR_ROLE_ID,
)
from persistence import broadcast_repository as storage


ADMIN_BROADCAST_TOPIC = "admin.broadcast.dm.v1"
KIND_LABELS = {
    "global": ("📢", "Общее уведомление", "system"),
    "consensus": ("🏛️", "Уведомление о консенсусе", "consensus"),
}


def _is_administrator(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(
        interaction.guild is not None
        and permissions is not None
        and permissions.administrator
    )


def broadcast_message_embed(payload: dict[str, Any]) -> discord.Embed:
    kind = str(payload.get("kind") or "global")
    icon, label, _ = KIND_LABELS.get(kind, KIND_LABELS["global"])
    link = str(payload.get("link_url") or "").strip() or None
    embed = discord.Embed(
        title=f"{icon} {str(payload.get('title') or 'Уведомление')[:180]}",
        description=str(payload.get("body") or "")[:3000],
        url=link,
        color=0xD6B46A if kind == "consensus" else TVRS_EMBED_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    if link:
        embed.add_field(
            name="Ссылка",
            value=f"[Открыть связанный раздел]({link})",
            inline=False,
        )
    author = str(payload.get("author_display") or "").strip()
    embed.set_footer(
        text=f"{label} Товарищества"
        + (f" • отправил {author}" if author else "")
    )
    return embed


def broadcast_report_embed(report: dict[str, Any] | None) -> discord.Embed:
    if report is None:
        return discord.Embed(
            title="📨 Рассылок пока нет",
            description="После первой подтверждённой отправки здесь появится отчёт.",
            color=TVRS_EMBED_COLOR,
        )
    counts = dict(report.get("counts") or {})
    total = int(report.get("recipient_count") or 0)
    delivered = int(counts.get("delivered") or 0)
    skipped = int(counts.get("skipped") or 0)
    unavailable = int(counts.get("unavailable") or 0)
    deferred = int(counts.get("deferred") or 0)
    failed = int(counts.get("failed") or 0)
    active = max(0, total - delivered - skipped - unavailable - failed)
    icon, label, _ = KIND_LABELS.get(
        str(report.get("kind") or "global"),
        KIND_LABELS["global"],
    )
    embed = discord.Embed(
        title=f"{icon} Отчёт: {str(report.get('title') or label)[:180]}",
        description=(
            f"Получателей в снимке роли: **{total}**\n"
            f"Доставлено: **{delivered}**\n"
            f"Ожидает отправки: **{active}**"
            + (f", из них тихие часы: **{deferred}**" if deferred else "")
            + f"\nОтключили этот тип ЛС: **{skipped}**\n"
            f"ЛС недоступны или роль снята: **{unavailable}**\n"
            f"Остановлено после повторных попыток: **{failed}**"
        ),
        color=(
            discord.Color.green()
            if total > 0 and delivered == total
            else discord.Color.orange()
        ),
    )
    status = str(report.get("status") or "queued")
    embed.set_footer(
        text=(
            "Рассылка завершена"
            if status == "completed"
            else "Рассылка продолжается через надёжную очередь"
        )
    )
    return embed


async def _senator_recipients(
    guild: discord.Guild,
) -> list[tuple[int, str | None]]:
    role = guild.get_role(TVRS_SENATOR_ROLE_ID)
    if role is None:
        raise ValueError("broadcast_senator_role_missing")
    if not getattr(guild, "chunked", True):
        await guild.chunk(cache=True)
    recipients = {
        int(member.id): str(member.display_name)
        for member in role.members
        if not member.bot
    }
    for user_id in TVRS_COCHAIR_IDS:
        member = guild.get_member(int(user_id))
        if member is not None and not member.bot:
            recipients[int(member.id)] = str(member.display_name)
    return sorted(recipients.items())


async def deliver_admin_broadcast(
    message: OutboxMessage,
    bot: discord.Client,
) -> DeliveryReceipt:
    payload = dict(message.payload)
    broadcast_id = int(payload.get("broadcast_id") or 0)
    guild_id = int(payload.get("guild_id") or 0)
    user_id = int(payload.get("user_id") or 0)
    if broadcast_id <= 0 or guild_id <= 0 or user_id <= 0:
        raise ValueError("broadcast_delivery_payload_invalid")
    recipient = await asyncio.to_thread(
        storage.get_broadcast_recipient,
        broadcast_id,
        user_id,
    )
    if recipient is None:
        raise ValueError("broadcast_recipient_missing")
    status = str(recipient.get("status") or "")
    if status == "delivered":
        return DeliveryReceipt(
            message_id=(
                int(recipient["dm_message_id"])
                if recipient.get("dm_message_id")
                else None
            )
        )
    if status in {"skipped", "unavailable"}:
        return DeliveryReceipt()

    guild = bot.get_guild(guild_id)
    if guild is None:
        raise RuntimeError("broadcast_guild_unavailable")
    try:
        member = await resolve_delivery_member(guild, user_id)
    except DeliveryDestinationUnavailable:
        await asyncio.to_thread(
            storage.mark_broadcast_recipient,
            broadcast_id,
            user_id,
            status="unavailable",
            reason="member_unavailable",
        )
        return DeliveryReceipt()
    if (
        user_id not in TVRS_COCHAIR_IDS
        and not any(role.id == TVRS_SENATOR_ROLE_ID for role in member.roles)
    ):
        await asyncio.to_thread(
            storage.mark_broadcast_recipient,
            broadcast_id,
            user_id,
            status="unavailable",
            reason="senator_role_removed",
        )
        return DeliveryReceipt()

    kind = str(payload.get("kind") or "global")
    notification_kind = KIND_LABELS.get(kind, KIND_LABELS["global"])[2]
    decision = await asyncio.to_thread(
        evaluate_profile_notification,
        guild_id,
        user_id,
        notification_kind,
    )
    if not decision.allowed:
        if decision.resume_at is not None:
            await asyncio.to_thread(
                storage.mark_broadcast_recipient,
                broadcast_id,
                user_id,
                status="deferred",
                reason="quiet_hours",
                available_at=decision.resume_at.isoformat(),
            )
            raise DeliveryDeferred(decision.resume_at, "profile_quiet_hours")
        await asyncio.to_thread(
            storage.mark_broadcast_recipient,
            broadcast_id,
            user_id,
            status="skipped",
            reason=decision.reason,
        )
        return DeliveryReceipt()

    try:
        sent = await member.send(
            embed=broadcast_message_embed(payload),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.DiscordException as exc:
        try:
            raise_classified_discord_error(exc, missing_is_permanent=True)
        except DeliveryDestinationUnavailable:
            await asyncio.to_thread(
                storage.mark_broadcast_recipient,
                broadcast_id,
                user_id,
                status="unavailable",
                reason="dm_unavailable",
            )
            return DeliveryReceipt()
        except DeliveryDeferred as deferred:
            await asyncio.to_thread(
                storage.mark_broadcast_recipient,
                broadcast_id,
                user_id,
                status="deferred",
                reason=deferred.reason,
                available_at=deferred.available_at.isoformat(),
            )
            raise
    await asyncio.to_thread(
        storage.mark_broadcast_recipient,
        broadcast_id,
        user_id,
        status="delivered",
        dm_message_id=int(sent.id),
    )
    return DeliveryReceipt(message_id=int(sent.id))


class BroadcastComposeModal(discord.ui.Modal):
    heading = discord.ui.TextInput(
        label="Заголовок",
        min_length=3,
        max_length=180,
    )
    body = discord.ui.TextInput(
        label="Текст уведомления",
        placeholder="Напишите понятное сообщение для участников",
        style=discord.TextStyle.paragraph,
        min_length=5,
        max_length=3000,
    )
    link_url = discord.ui.TextInput(
        label="Ссылка",
        placeholder="Необязательно: ссылка на канал, сообщение или документ",
        required=False,
        max_length=500,
    )

    def __init__(self, kind: str, author_id: int) -> None:
        icon, label, _ = KIND_LABELS[kind]
        super().__init__(title=f"{icon} {label}", timeout=900)
        self.kind = kind
        self.author_id = int(author_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            not _is_administrator(interaction)
            or interaction.user.id != self.author_id
            or interaction.guild is None
        ):
            await interaction.response.send_message(
                "Рассылка доступна только администратору.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            recipients = await _senator_recipients(interaction.guild)
            if not recipients:
                raise ValueError("broadcast_recipients_empty")
            draft = await asyncio.to_thread(
                storage.create_broadcast_draft,
                guild_id=interaction.guild.id,
                author_id=interaction.user.id,
                author_display=getattr(
                    interaction.user,
                    "display_name",
                    str(interaction.user),
                ),
                kind=self.kind,
                title=str(self.heading.value),
                body=str(self.body.value),
                link_url=str(self.link_url.value),
            )
        except ValueError as exc:
            message = {
                "broadcast_senator_role_missing": "Роль сенатора не найдена.",
                "broadcast_recipients_empty": "На сервере нет участников с ролью сенатора.",
                "broadcast_link_invalid": "Ссылка должна начинаться с http:// или https://.",
            }.get(str(exc), "Не удалось подготовить рассылку: проверьте поля.")
            await interaction.followup.send(message, ephemeral=True)
            return
        preview = broadcast_message_embed(draft)
        preview.insert_field_at(
            0,
            name="Предпросмотр",
            value=f"Будет поставлено в очередь: **{len(recipients)}** сенаторов.",
            inline=False,
        )
        await interaction.followup.send(
            embed=preview,
            view=BroadcastConfirmView(
                int(draft["id"]),
                interaction.user.id,
            ),
            ephemeral=True,
        )


class BroadcastConfirmView(discord.ui.View):
    def __init__(self, broadcast_id: int, author_id: int) -> None:
        super().__init__(timeout=900)
        self.broadcast_id = int(broadcast_id)
        self.author_id = int(author_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _is_administrator(interaction) or interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Это подтверждение доступно только автору рассылки.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Отправить сенаторам",
        emoji="📨",
        style=discord.ButtonStyle.success,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        recipients = await _senator_recipients(interaction.guild)
        try:
            broadcast, _ = await asyncio.to_thread(
                storage.activate_broadcast,
                self.broadcast_id,
                author_id=interaction.user.id,
                recipients=recipients,
                delivery_topic=ADMIN_BROADCAST_TOPIC,
            )
        except ValueError as exc:
            message = (
                "На сервере нет участников с ролью сенатора."
                if str(exc) == "broadcast_recipients_empty"
                else "Рассылка уже недоступна или принадлежит другому администратору."
            )
            await interaction.edit_original_response(
                content=message,
                embed=None,
                view=None,
            )
            return
        wake_delivery_worker()
        report = await asyncio.to_thread(
            storage.broadcast_report,
            broadcast_id=int(broadcast["id"]),
        )
        await interaction.edit_original_response(
            content=None,
            embed=broadcast_report_embed(report),
            view=BroadcastReportView(
                interaction.user.id,
                int(broadcast["id"]),
            ),
        )

    @discord.ui.button(
        label="Не отправлять",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Рассылка не отправлена.",
            embed=None,
            view=None,
        )


class BroadcastReportView(discord.ui.View):
    def __init__(self, author_id: int, broadcast_id: int) -> None:
        super().__init__(timeout=900)
        self.author_id = int(author_id)
        self.broadcast_id = int(broadcast_id)

    @discord.ui.button(
        label="Обновить отчёт",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
    )
    async def refresh(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if not _is_administrator(interaction):
            await interaction.response.send_message(
                "Отчёт доступен только администратору.",
                ephemeral=True,
            )
            return
        report = await asyncio.to_thread(
            storage.broadcast_report,
            broadcast_id=self.broadcast_id,
        )
        await interaction.response.edit_message(
            embed=broadcast_report_embed(report),
            view=self,
        )


class BroadcastAdminView(discord.ui.View):
    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=900)
        self.author_id = int(author_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _is_administrator(interaction) or interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Административная рассылка вам недоступна.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Общее",
        emoji="📢",
        style=discord.ButtonStyle.primary,
    )
    async def global_notice(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            BroadcastComposeModal("global", self.author_id)
        )

    @discord.ui.button(
        label="О консенсусе",
        emoji="🏛️",
        style=discord.ButtonStyle.primary,
    )
    async def consensus_notice(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            BroadcastComposeModal("consensus", self.author_id)
        )

    @discord.ui.button(
        label="Последний отчёт",
        emoji="📊",
        style=discord.ButtonStyle.secondary,
    )
    async def latest_report(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        assert interaction.guild is not None
        report = await asyncio.to_thread(
            storage.broadcast_report,
            guild_id=interaction.guild.id,
        )
        await interaction.response.send_message(
            embed=broadcast_report_embed(report),
            view=(
                BroadcastReportView(
                    interaction.user.id,
                    int(report["id"]),
                )
                if report is not None
                else None
            ),
            ephemeral=True,
        )


async def open_broadcast_admin_panel(interaction: discord.Interaction) -> None:
    if not _is_administrator(interaction):
        await interaction.response.send_message(
            "Рассылка доступна только администратору.",
            ephemeral=True,
        )
        return
    assert interaction.guild is not None
    role = interaction.guild.get_role(TVRS_SENATOR_ROLE_ID)
    count = len([member for member in role.members if not member.bot]) if role else 0
    embed = discord.Embed(
        title="📨 Уведомления сенаторам",
        description=(
            "Выберите тип, подготовьте текст и проверьте предпросмотр. "
            "Сообщения отправляются лично участникам с ролью сенатора через "
            "надёжную очередь.\n\n"
            f"Сейчас в роли: **{count}** участников."
        ),
        color=TVRS_EMBED_COLOR,
    )
    embed.add_field(
        name="Уважение настроек",
        value=(
            "Общие сообщения учитывают системные уведомления профиля, сообщения "
            "о консенсусе — настройку консенсуса. Тихие часы не отменяют сообщение, "
            "а откладывают его."
        ),
        inline=False,
    )
    await interaction.response.send_message(
        embed=embed,
        view=BroadcastAdminView(interaction.user.id),
        ephemeral=True,
    )


def setup_admin_broadcast(bot: commands.Bot) -> None:
    register_delivery_handler(
        ADMIN_BROADCAST_TOPIC,
        lambda message: deliver_admin_broadcast(message, bot),
    )


__all__ = [
    "ADMIN_BROADCAST_TOPIC",
    "BroadcastAdminView",
    "BroadcastComposeModal",
    "BroadcastConfirmView",
    "BroadcastReportView",
    "broadcast_message_embed",
    "broadcast_report_embed",
    "deliver_admin_broadcast",
    "open_broadcast_admin_panel",
    "setup_admin_broadcast",
]
