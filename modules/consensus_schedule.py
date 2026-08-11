"""Planning workflow and Discord Scheduled Event adapter for Consensus."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from modules.admin_broadcast import ADMIN_BROADCAST_TOPIC
from modules.delivery_runtime import wake_delivery_worker
from modules.tvrs_config import (
    LOCAL_TZ,
    TVRS_CONSENSUS_VOICE_CHANNEL_ID,
    TVRS_DEFAULT_NEXT_PLENARY_NUMBER,
)
from modules.tvrs_formatting import clip_text, ru_ordinal
from modules.tvrs_presentation import is_chair, is_senator
from persistence import broadcast_repository as broadcast_storage
from persistence import consensus_schedule_repository as schedule_storage
from persistence import tvrs_repository as tvrs_storage


def parse_schedule_time(value: str, *, now: datetime | None = None) -> datetime:
    clean = " ".join(str(value or "").strip().split())
    parsed: datetime | None = None
    for pattern in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(clean, pattern).replace(tzinfo=LOCAL_TZ)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("consensus_schedule_time_invalid")
    selected = parsed.astimezone(timezone.utc)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if selected <= current + timedelta(minutes=1):
        raise ValueError("consensus_schedule_time_past")
    if selected > current + timedelta(days=365):
        raise ValueError("consensus_schedule_time_too_far")
    return selected


def schedule_timestamp(schedule: dict[str, Any]) -> int:
    value = datetime.fromisoformat(
        str(schedule.get("scheduled_for") or "").replace("Z", "+00:00")
    )
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def schedule_event_url(schedule: dict[str, Any]) -> str | None:
    event_id = int(schedule.get("discord_event_id") or 0)
    guild_id = int(schedule.get("guild_id") or 0)
    if event_id <= 0 or guild_id <= 0:
        return None
    return f"https://discord.com/events/{guild_id}/{event_id}"


def build_schedule_embed(schedule: dict[str, Any]) -> discord.Embed:
    timestamp = schedule_timestamp(schedule)
    duration = int(schedule.get("duration_minutes") or 90)
    event_url = schedule_event_url(schedule)
    embed = discord.Embed(
        title=f"🏛️ {str(schedule.get('title') or 'Пленарный консенсус')[:100]}",
        description=(
            f"**{ru_ordinal(int(schedule.get('plenary_number') or 0)).capitalize()} "
            "пленарное заседание Товарищества**\n\n"
            f"{clip_text(schedule.get('description'), 900, 'Повестка будет уточнена председателем.')}"
        ),
        color=0xC7A96B,
    )
    embed.add_field(
        name="Начало",
        value=f"<t:{timestamp}:F>\n<t:{timestamp}:R>",
        inline=True,
    )
    embed.add_field(
        name="Продолжительность",
        value=f"Ориентировочно **{duration} мин.**",
        inline=True,
    )
    embed.add_field(
        name="Место",
        value=f"<#{int(schedule.get('voice_channel_id') or 0)}>",
        inline=True,
    )
    if event_url:
        embed.add_field(
            name="Событие Discord",
            value=f"[Открыть событие и отметить участие]({event_url})",
            inline=False,
        )
    invitation = str(schedule.get("invitation_text") or "").strip()
    if invitation:
        embed.add_field(
            name="Обращение председателя",
            value=clip_text(invitation, 900),
            inline=False,
        )
    embed.set_footer(
        text=(
            f"План заседания #{int(schedule.get('id') or 0)} • "
            f"редакция {int(schedule.get('revision') or 1)}"
        )
    )
    return embed


async def sync_schedule_discord_event(
    guild: discord.Guild,
    schedule: dict[str, Any],
) -> dict[str, Any]:
    start_time = datetime.fromisoformat(
        str(schedule["scheduled_for"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    end_time = start_time + timedelta(
        minutes=int(schedule.get("duration_minutes") or 90)
    )
    channel = guild.get_channel(int(schedule.get("voice_channel_id") or 0))
    if channel is None:
        raise ValueError("consensus_schedule_voice_channel_missing")
    event_id = int(schedule.get("discord_event_id") or 0)
    event = guild.get_scheduled_event(event_id) if event_id else None
    if event is None and event_id:
        try:
            event = await guild.fetch_scheduled_event(event_id)
        except discord.NotFound:
            event = None
    description = clip_text(
        schedule.get("description"),
        950,
        "Пленарное заседание Товарищества.",
    )
    if event is None:
        event = await guild.create_scheduled_event(
            name=str(schedule.get("title") or "Пленарный консенсус")[:100],
            description=description,
            start_time=start_time,
            end_time=end_time,
            channel=channel,
            entity_type=discord.EntityType.voice,
            privacy_level=discord.PrivacyLevel.guild_only,
            reason=f"T-Mod Consensus plan #{int(schedule.get('id') or 0)}",
        )
    else:
        event = await event.edit(
            name=str(schedule.get("title") or "Пленарный консенсус")[:100],
            description=description,
            start_time=start_time,
            end_time=end_time,
            channel=channel,
            reason=f"T-Mod Consensus plan #{int(schedule.get('id') or 0)} updated",
        )
    return await asyncio.to_thread(
        schedule_storage.bind_consensus_schedule_event,
        int(schedule["id"]),
        int(event.id),
    )


async def _schedule_recipients(
    guild: discord.Guild,
) -> list[tuple[int, str | None]]:
    if not getattr(guild, "chunked", True):
        await guild.chunk(cache=True)
    recipients: dict[int, str | None] = {}
    for member in guild.members:
        if member.bot:
            continue
        if is_senator(member) or is_chair(member):
            recipients[int(member.id)] = str(member.display_name)
    return sorted(recipients.items())


async def send_schedule_invitations(
    guild: discord.Guild,
    schedule: dict[str, Any],
    actor: discord.Member,
) -> tuple[dict[str, Any], int]:
    recipients = await _schedule_recipients(guild)
    if not recipients:
        raise ValueError("consensus_schedule_recipients_empty")
    timestamp = schedule_timestamp(schedule)
    event_url = schedule_event_url(schedule)
    custom = str(schedule.get("invitation_text") or "").strip()
    body = (
        f"Вас приглашают на **{str(schedule.get('title') or 'пленарный консенсус')}**.\n\n"
        f"Начало: <t:{timestamp}:F> — <t:{timestamp}:R>.\n"
        f"Место: <#{int(schedule.get('voice_channel_id') or 0)}>\n"
        f"Ориентировочная продолжительность: {int(schedule.get('duration_minutes') or 90)} мин."
    )
    if custom:
        body += f"\n\n{custom}"
    draft = await asyncio.to_thread(
        broadcast_storage.create_broadcast_draft,
        guild_id=int(guild.id),
        author_id=int(actor.id),
        author_display=str(actor.display_name),
        kind="consensus",
        title=f"Приглашение · {str(schedule.get('title') or '')[:145]}",
        body=body,
        link_url=event_url,
    )
    broadcast, _ = await asyncio.to_thread(
        broadcast_storage.activate_broadcast,
        int(draft["id"]),
        author_id=int(actor.id),
        recipients=recipients,
        delivery_topic=ADMIN_BROADCAST_TOPIC,
    )
    updated = await asyncio.to_thread(
        schedule_storage.bind_consensus_schedule_invitation,
        int(schedule["id"]),
        int(broadcast["id"]),
    )
    wake_delivery_worker()
    return updated, len(recipients)


async def cancel_schedule_discord_event(
    guild: discord.Guild,
    schedule: dict[str, Any],
) -> None:
    event_id = int(schedule.get("discord_event_id") or 0)
    if not event_id:
        return
    event = guild.get_scheduled_event(event_id)
    if event is None:
        try:
            event = await guild.fetch_scheduled_event(event_id)
        except discord.NotFound:
            return
    if event.status == discord.EventStatus.scheduled:
        await event.cancel(reason=f"T-Mod Consensus plan #{schedule.get('id')} cancelled")


class ConsensusScheduleModal(discord.ui.Modal):
    def __init__(
        self,
        requester_id: int,
        schedule: dict[str, Any] | None = None,
    ) -> None:
        self.requester_id = int(requester_id)
        self.schedule = dict(schedule) if schedule else None
        local_time = (
            datetime.fromisoformat(str(schedule["scheduled_for"]).replace("Z", "+00:00"))
            .astimezone(LOCAL_TZ)
            .strftime("%d.%m.%Y %H:%M")
            if schedule
            else (datetime.now(LOCAL_TZ) + timedelta(days=1)).replace(minute=0).strftime("%d.%m.%Y %H:%M")
        )
        super().__init__(
            title="Изменить план заседания" if schedule else "Запланировать консенсус",
            timeout=900,
        )
        self.heading = discord.ui.TextInput(
            label="Название заседания",
            default=str(schedule.get("title") or "") if schedule else "Пленарный консенсус Товарищества",
            min_length=3,
            max_length=100,
        )
        self.starts_at = discord.ui.TextInput(
            label="Дата и время · Рига",
            default=local_time,
            placeholder="ДД.ММ.ГГГГ ЧЧ:ММ",
            min_length=16,
            max_length=16,
        )
        self.duration = discord.ui.TextInput(
            label="Продолжительность в минутах",
            default=str(schedule.get("duration_minutes") or 90) if schedule else "90",
            min_length=2,
            max_length=3,
        )
        self.description = discord.ui.TextInput(
            label="Повестка и описание",
            default=str(schedule.get("description") or "") if schedule else "",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=1000,
        )
        self.invitation = discord.ui.TextInput(
            label="Обращение в приглашении",
            default=str(schedule.get("invitation_text") or "") if schedule else "",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=1500,
        )
        for item in (
            self.heading,
            self.starts_at,
            self.duration,
            self.description,
            self.invitation,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild is None
            or not isinstance(interaction.user, discord.Member)
            or interaction.user.id != self.requester_id
            or not is_chair(interaction.user)
        ):
            await interaction.response.send_message(
                "Планирование доступно только председателю или сопредседателю.",
                ephemeral=True,
            )
            return
        try:
            starts_at = parse_schedule_time(str(self.starts_at.value))
            duration = int(str(self.duration.value).strip())
        except (TypeError, ValueError) as exc:
            message = {
                "consensus_schedule_time_invalid": "Введите дату как ДД.ММ.ГГГГ ЧЧ:ММ.",
                "consensus_schedule_time_past": "Начало должно быть хотя бы через одну минуту.",
                "consensus_schedule_time_too_far": "Заседание можно планировать максимум на год вперёд.",
            }.get(str(exc), "Продолжительность должна быть числом от 15 до 480 минут.")
            await interaction.response.send_message(message, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        plenary = await asyncio.to_thread(
            tvrs_storage.tvrs_get_next_plenary_number,
            interaction.guild.id,
            TVRS_DEFAULT_NEXT_PLENARY_NUMBER,
        )
        try:
            schedule = await asyncio.to_thread(
                schedule_storage.save_consensus_schedule,
                guild_id=int(interaction.guild.id),
                plenary_number=int(plenary),
                title=str(self.heading.value),
                description=str(self.description.value),
                invitation_text=str(self.invitation.value),
                scheduled_for=starts_at,
                duration_minutes=duration,
                voice_channel_id=TVRS_CONSENSUS_VOICE_CHANNEL_ID,
                actor_id=int(interaction.user.id),
                actor_display=str(interaction.user.display_name),
                schedule_id=(int(self.schedule["id"]) if self.schedule else None),
                expected_revision=(int(self.schedule["revision"]) if self.schedule else None),
            )
        except ValueError as exc:
            message = {
                "consensus_schedule_already_exists": "Будущее заседание уже запланировано. Откройте его и измените существующий план.",
                "consensus_schedule_conflict": "План уже изменён в другом окне. Откройте его заново.",
                "consensus_schedule_duration_invalid": "Продолжительность должна быть от 15 до 480 минут.",
            }.get(str(exc), "Не удалось сохранить план: проверьте заполнение полей.")
            await interaction.edit_original_response(content=message, embed=None, view=None)
            return
        sync_warning = ""
        try:
            schedule = await sync_schedule_discord_event(interaction.guild, schedule)
        except (discord.DiscordException, ValueError) as exc:
            sync_warning = f"\n\n⚠️ План сохранён, но событие Discord пока не синхронизировано: `{type(exc).__name__}`. Нажмите «Синхронизировать»."
        await interaction.edit_original_response(
            content=sync_warning or None,
            embed=build_schedule_embed(schedule),
            view=ConsensusScheduleView(
                int(interaction.user.id),
                int(interaction.guild.id),
                schedule,
            ),
        )


class ConsensusScheduleView(discord.ui.View):
    def __init__(
        self,
        requester_id: int,
        guild_id: int,
        schedule: dict[str, Any],
    ) -> None:
        super().__init__(timeout=900)
        self.requester_id = int(requester_id)
        self.guild_id = int(guild_id)
        self.schedule = dict(schedule)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = (
            interaction.guild is not None
            and isinstance(interaction.user, discord.Member)
            and int(interaction.user.id) == self.requester_id
            and is_chair(interaction.user)
        )
        if not allowed:
            await interaction.response.send_message(
                "Этот план доступен только открывшему его председателю.",
                ephemeral=True,
            )
        return bool(allowed)

    async def _latest(self) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            schedule_storage.get_consensus_schedule,
            int(self.schedule["id"]),
        )

    @discord.ui.button(label="Изменить", emoji="✦", style=discord.ButtonStyle.primary, row=0)
    async def edit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        latest = await self._latest()
        if latest is None or latest.get("status") != "scheduled":
            await interaction.response.send_message("Этот план уже закрыт.", ephemeral=True)
            return
        await interaction.response.send_modal(
            ConsensusScheduleModal(self.requester_id, latest)
        )

    @discord.ui.button(label="Отправить приглашения", emoji="✉️", style=discord.ButtonStyle.success, row=0)
    async def invite(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        await interaction.response.defer(ephemeral=True, thinking=True)
        latest = await self._latest()
        if latest is None or latest.get("status") != "scheduled":
            await interaction.edit_original_response(content="Этот план уже закрыт.", embed=None, view=None)
            return
        try:
            latest, count = await send_schedule_invitations(
                interaction.guild,
                latest,
                interaction.user,
            )
        except ValueError:
            await interaction.edit_original_response(
                content="Не найдено ни одного сенатора или председателя для приглашения.",
                embed=None,
                view=None,
            )
            return
        await interaction.edit_original_response(
            content=f"Приглашения поставлены в надёжную очередь: **{count}** получателей.",
            embed=build_schedule_embed(latest),
            view=ConsensusScheduleView(self.requester_id, self.guild_id, latest),
        )

    @discord.ui.button(label="Синхронизировать", emoji="↻", style=discord.ButtonStyle.secondary, row=0)
    async def sync(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        latest = await self._latest()
        if latest is None or latest.get("status") != "scheduled":
            await interaction.edit_original_response(content="Этот план уже закрыт.", embed=None, view=None)
            return
        try:
            latest = await sync_schedule_discord_event(interaction.guild, latest)
        except (discord.DiscordException, ValueError) as exc:
            await interaction.edit_original_response(
                content=f"Событие Discord пока недоступно: `{type(exc).__name__}`. План в T-Mod сохранён.",
                embed=build_schedule_embed(latest),
                view=ConsensusScheduleView(self.requester_id, self.guild_id, latest),
            )
            return
        await interaction.edit_original_response(
            content="Событие Discord синхронизировано.",
            embed=build_schedule_embed(latest),
            view=ConsensusScheduleView(self.requester_id, self.guild_id, latest),
        )

    @discord.ui.button(label="Проверить готовность", emoji="⚖️", style=discord.ButtonStyle.secondary, row=1)
    async def prepare(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from modules.tvrs_consensus_portal import open_preparation_portal

        await open_preparation_portal(
            interaction,
            schedule_id=int(self.schedule["id"]),
        )

    @discord.ui.button(label="Отменить план", emoji="×", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        latest = await self._latest()
        if latest is None or latest.get("status") != "scheduled":
            await interaction.response.send_message("Этот план уже закрыт.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content="Отменить план заседания и связанное событие Discord? Проведённый консенсус это не затронет.",
            embed=build_schedule_embed(latest),
            view=ConsensusScheduleCancelView(
                self.requester_id,
                self.guild_id,
                latest,
            ),
        )


class ConsensusScheduleCancelView(discord.ui.View):
    def __init__(self, requester_id: int, guild_id: int, schedule: dict[str, Any]) -> None:
        super().__init__(timeout=180)
        self.requester_id = int(requester_id)
        self.guild_id = int(guild_id)
        self.schedule = dict(schedule)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = bool(
            interaction.user.id == self.requester_id
            and interaction.guild is not None
            and isinstance(interaction.user, discord.Member)
            and is_chair(interaction.user)
        )
        if not allowed:
            await interaction.response.send_message(
                "Эта операция доступна только председателю или со-председателю, открывшему план.",
                ephemeral=True,
            )
        return allowed

    @discord.ui.button(label="Не отменять", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content=None,
            embed=build_schedule_embed(self.schedule),
            view=ConsensusScheduleView(self.requester_id, self.guild_id, self.schedule),
        )

    @discord.ui.button(label="Подтвердить отмену", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            cancelled = await asyncio.to_thread(
                schedule_storage.cancel_consensus_schedule,
                int(self.schedule["id"]),
                guild_id=self.guild_id,
                expected_revision=int(self.schedule["revision"]),
            )
        except ValueError:
            await interaction.edit_original_response(
                content="План уже был изменён. Откройте его заново.",
                embed=None,
                view=None,
            )
            return
        try:
            await cancel_schedule_discord_event(interaction.guild, cancelled)
        except discord.DiscordException:
            pass
        await interaction.edit_original_response(
            content="План заседания отменён. Рабочие законопроекты и история консенсусов не изменены.",
            embed=None,
            view=None,
        )


async def open_consensus_schedule(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Планирование работает только на сервере.", ephemeral=True)
        return
    if not is_chair(interaction.user):
        await interaction.response.send_message(
            "Планировать заседание может только председатель или сопредседатель.",
            ephemeral=True,
        )
        return
    schedule = await asyncio.to_thread(
        schedule_storage.get_upcoming_consensus_schedule,
        int(interaction.guild.id),
    )
    if schedule is None:
        await interaction.response.send_modal(
            ConsensusScheduleModal(int(interaction.user.id))
        )
        return
    await interaction.response.edit_message(
        content=None,
        embed=build_schedule_embed(schedule),
        view=ConsensusScheduleView(
            int(interaction.user.id),
            int(interaction.guild.id),
            schedule,
        ),
    )


def public_schedule_payload(schedule: dict[str, Any] | None) -> dict[str, Any] | None:
    if schedule is None:
        return None
    return {
        "id": int(schedule.get("id") or 0),
        "plenary_number": int(schedule.get("plenary_number") or 0),
        "title": str(schedule.get("title") or ""),
        "description": str(schedule.get("description") or ""),
        "scheduled_for": str(schedule.get("scheduled_for") or ""),
        "duration_minutes": int(schedule.get("duration_minutes") or 90),
        "voice_channel_id": int(schedule.get("voice_channel_id") or 0),
        "host": {
            "id": int(schedule.get("created_by_id") or 0) or None,
            "name": str(schedule.get("created_by_display") or "").strip()
            or "Председатель Товарищества",
        },
        "event_url": schedule_event_url(schedule),
        "status": str(schedule.get("status") or "scheduled"),
        "started_at": str(schedule.get("started_at") or "") or None,
        "created_at": str(schedule.get("created_at") or "") or None,
        "revision": int(schedule.get("revision") or 1),
    }


__all__ = [
    "ConsensusScheduleModal",
    "ConsensusScheduleView",
    "build_schedule_embed",
    "cancel_schedule_discord_event",
    "open_consensus_schedule",
    "parse_schedule_time",
    "public_schedule_payload",
    "schedule_event_url",
    "send_schedule_invitations",
    "sync_schedule_discord_event",
]
