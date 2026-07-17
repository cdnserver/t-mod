"""Private member profile panel with reusable game characters."""

from __future__ import annotations

import asyncio
import traceback
from datetime import datetime
from typing import Any, Callable

import discord
from discord import app_commands
from discord.ext import commands

from persistence import profile_context as storage
from modules.technical_log import log_technical_event


PROFILE_COLOR = 0x5865F2
PROFILE_TIMEOUT_SECONDS = 900
PROFILE_STATUS_INFO = {
    "active": ("🟢", "Активен", "На связи и готов участвовать"),
    "busy": ("🟠", "Занят", "Может отвечать с задержкой"),
    "away": ("🌙", "Отошёл", "Временно не у компьютера"),
    "vacation": ("🏖️", "В отпуске", "Долгое отсутствие"),
}
PROFILE_ROLE_HIERARCHY = (
    (1488207163879985233, "🛡️", "Администратор", "Администрирование сообщества"),
    (1499717656276635778, "🏛️", "Сопредседатель", "Руководство Товариществом"),
    (1500563715622174881, "📜", "Сенатор Товарищества", "Участие в управлении"),
    (1500567343703527607, "🚔", "Сотрудник силовой фракции", "Служба в силовой структуре"),
    (1500567401580990484, "⚖️", "Судья / прокурор", "Судебная или прокурорская должность"),
    (1500488424191295518, "💼", "Представитель Бюро SGL", "Работа от имени Бюро"),
    (1500488373666840587, "🤝", "Клиент Бюро SGL", "Клиентский статус Бюро"),
    (1526194626531299378, "🛠️", "Старший мастер крафта", "Старшая работа с производством"),
    (1526605619153473546, "📦", "Сотрудник склада", "Младшая работа со складом"),
)
PROFILE_THEME_INFO = {
    "indigo": ("🔵", "Индиго", PROFILE_COLOR),
    "emerald": ("🟢", "Изумруд", 0x3BA55D),
    "gold": ("🟡", "Золото", 0xF1C40F),
    "rose": ("🌸", "Роза", 0xEB459E),
}
PROFILE_VISIBILITY_INFO = {
    "members": ("🌐", "Участникам", "Профиль доступен участникам сервера"),
    "private": ("🔒", "Только мне", "Другие увидят закрытую карточку"),
}
PROFILE_ERROR_MESSAGES = {
    "profile_nickname_invalid": "Ник должен содержать от 2 до 48 символов.",
    "profile_static_invalid": "Статик должен состоять из 1–12 цифр.",
    "profile_static_taken": "Этот статик уже привязан к другому персонажу на сервере.",
    "profile_character_limit": "В профиле уже сохранены три персонажа.",
    "profile_character_not_found": "Персонаж уже удалён или недоступен.",
    "profile_status_invalid": "Не удалось распознать выбранную доступность.",
    "profile_status_note_too_long": "Подпись доступности должна быть не длиннее 120 символов.",
    "profile_character_conflict": "Не удалось сохранить персонажа из-за конфликта данных.",
    "profile_visibility_invalid": "Не удалось распознать режим видимости профиля.",
    "profile_show_activity_invalid": "Не удалось изменить видимость активности.",
    "profile_theme_invalid": "Не удалось распознать оформление профиля.",
    "profile_primary_character_invalid": "Выбранный персонаж больше не находится в вашем профиле.",
}
CHARACTER_NUMBERS = {1: "①", 2: "②", 3: "③"}


def _clean_display(value: Any, *, fallback: str = "Не указано") -> str:
    text = " ".join(str(value or "").strip().split())
    return discord.utils.escape_markdown(text) if text else fallback


def _discord_time(value: datetime | str | None, style: str = "R") -> str:
    if value is None:
        return "не зафиксировано"
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return "не зафиксировано"
    return f"<t:{int(parsed.timestamp())}:{style}>"


def member_position_text(member: discord.Member) -> str:
    role_ids = {
        int(role.id)
        for role in getattr(member, "roles", [])
        if getattr(role, "id", None) is not None
    }
    positions = [entry for entry in PROFILE_ROLE_HIERARCHY if entry[0] in role_ids]
    if not positions:
        return "🕯️ **Прихожанин**\nБазовое положение участника Товарищества."

    _, emoji, label, description = positions[0]
    lines = [f"Основное: {emoji} **{label}**", description]
    if len(positions) > 1:
        additional = " · ".join(f"{item[1]} {item[2]}" for item in positions[1:])
        lines.append(f"Дополнительно: {additional}")
    return "\n".join(lines)


def _profile_status(profile: Any | None) -> tuple[str, str, str, str | None]:
    key = str(getattr(profile, "status", None) or "active")
    emoji, label, description = PROFILE_STATUS_INFO.get(key, PROFILE_STATUS_INFO["active"])
    return emoji, label, description, getattr(profile, "status_note", None)


def _profile_theme(profile: Any | None) -> tuple[str, str, int]:
    key = str(getattr(profile, "theme", None) or "indigo")
    return PROFILE_THEME_INFO.get(key, PROFILE_THEME_INFO["indigo"])


def profile_embed(
    member: discord.Member,
    profile: Any | None,
    characters: list[Any],
    activity: Any | None,
    *,
    editable: bool,
) -> discord.Embed:
    emoji, status_label, status_description, status_note = _profile_status(profile)
    _, _, theme_color = _profile_theme(profile)
    visibility = str(getattr(profile, "visibility", None) or "members")
    description = (
        "Личная карточка участника Товарищества. "
        + ("Данные видны только в открывшем её меню." if editable else "Профиль открыт в режиме просмотра.")
    )
    embed = discord.Embed(
        title=f"Профиль · {str(getattr(member, 'display_name', 'Участник'))[:220]}",
        description=description,
        color=theme_color,
    )
    avatar = getattr(getattr(member, "display_avatar", None), "url", None)
    if avatar:
        embed.set_thumbnail(url=str(avatar))
    if not editable and visibility == "private":
        embed.description = (
            "🔒 Владелец сделал профиль личным. Персонажи, доступность и активность скрыты."
        )
        embed.set_footer(text="Личная настройка участника • T-Mod Profile")
        return embed
    status_value = f"{emoji} **{status_label}** — {status_description}"
    if status_note:
        status_value += f"\n> {_clean_display(status_note)}"
    embed.add_field(name="Доступность", value=status_value[:1024], inline=False)

    joined_at = getattr(member, "joined_at", None)
    show_activity = bool(getattr(profile, "show_activity", True))
    last_activity = getattr(activity, "last_activity_at", None)
    total_events = max(0, int(getattr(activity, "total_events", 0) or 0))
    participation = (
        f"На сервере: {_discord_time(joined_at, 'D')}\n"
        f"Последняя активность: "
        f"{_discord_time(last_activity) if show_activity else 'скрыта владельцем'}"
    )
    if total_events and show_activity:
        participation += f" · событий: **{total_events:,}**".replace(",", " ")
    embed.add_field(name="Участие", value=participation, inline=False)
    embed.add_field(
        name="Положение в Товариществе",
        value=member_position_text(member)[:1024],
        inline=False,
    )

    if not characters:
        embed.add_field(
            name="Персонажи · 0/3",
            value=(
                "Персонажи ещё не добавлены."
                + (" Нажмите **«Добавить»**, чтобы сохранить первого." if editable else "")
            ),
            inline=False,
        )
    else:
        primary_character_id = getattr(profile, "primary_character_id", None)
        for character in characters[: storage.PROFILE_MAX_CHARACTERS]:
            position = int(getattr(character, "position", 0) or 0)
            nickname = _clean_display(getattr(character, "nickname", "Персонаж"))
            static_id = _clean_display(getattr(character, "static_id", "—"))
            embed.add_field(
                name=(
                    f"{'⭐ ' if primary_character_id == character.id else ''}"
                    f"{CHARACTER_NUMBERS.get(position, '◆')} {nickname}"
                )[:256],
                value=(
                    f"Статик: `{static_id}`"
                    + ("\nОсновной персонаж" if primary_character_id == character.id else "")
                ),
                inline=True,
            )
    embed.set_footer(
        text=f"Персонажей: {len(characters)}/{storage.PROFILE_MAX_CHARACTERS} • T-Mod Profile"
    )
    return embed


def character_embed(member: discord.Member, character: Any) -> discord.Embed:
    position = int(getattr(character, "position", 0) or 0)
    embed = discord.Embed(
        title=f"{CHARACTER_NUMBERS.get(position, '◆')} {_clean_display(character.nickname)}",
        description="Карточка игрового персонажа. Ник и статик можно безопасно обновить здесь.",
        color=PROFILE_COLOR,
    )
    embed.add_field(name="Владелец", value=_clean_display(member.display_name), inline=True)
    embed.add_field(name="Статик", value=f"`{_clean_display(character.static_id)}`", inline=True)
    embed.add_field(name="Слот", value=f"{position} из {storage.PROFILE_MAX_CHARACTERS}", inline=True)
    embed.set_footer(text="Статик уникален в пределах сервера • T-Mod Profile")
    return embed


def character_manager_embed(member: discord.Member, characters: list[Any]) -> discord.Embed:
    embed = discord.Embed(
        title="Персонажи профиля",
        description=(
            f"Владелец: **{_clean_display(member.display_name)}**\n"
            "Выберите персонажа, чтобы изменить его данные или удалить запись."
        ),
        color=PROFILE_COLOR,
    )
    if characters:
        lines = [
            f"{CHARACTER_NUMBERS.get(int(character.position), '◆')} "
            f"**{_clean_display(character.nickname)}** · `{_clean_display(character.static_id)}`"
            for character in characters
        ]
        embed.add_field(name=f"Сохранено · {len(characters)}/3", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Сохранено · 0/3", value="Пока пусто.", inline=False)
    return embed


def profile_settings_embed(
    member: discord.Member,
    profile: Any | None,
    characters: list[Any],
) -> discord.Embed:
    visibility = str(getattr(profile, "visibility", None) or "members")
    visibility_emoji, visibility_label, visibility_description = PROFILE_VISIBILITY_INFO.get(
        visibility,
        PROFILE_VISIBILITY_INFO["members"],
    )
    theme_emoji, theme_label, theme_color = _profile_theme(profile)
    show_activity = bool(getattr(profile, "show_activity", True))
    primary_character_id = getattr(profile, "primary_character_id", None)
    primary = next(
        (character for character in characters if character.id == primary_character_id),
        None,
    )
    primary_text = (
        f"⭐ **{_clean_display(primary.nickname)}** · `{_clean_display(primary.static_id)}`"
        if primary is not None
        else "Не выбран"
    )
    embed = discord.Embed(
        title="Настройки профиля",
        description=(
            f"Персональные параметры **{_clean_display(member.display_name)}**. "
            "Они действуют только для вашего профиля."
        ),
        color=theme_color,
    )
    embed.add_field(
        name="Видимость",
        value=f"{visibility_emoji} **{visibility_label}**\n{visibility_description}",
        inline=True,
    )
    embed.add_field(
        name="Активность",
        value=(
            "👁️ **Показывается**\nПоследняя активность видна в карточке"
            if show_activity
            else "🙈 **Скрыта**\nВ карточке остаётся только дата вступления"
        ),
        inline=True,
    )
    embed.add_field(
        name="Оформление",
        value=f"{theme_emoji} **{theme_label}**",
        inline=True,
    )
    embed.add_field(name="Основной персонаж", value=primary_text, inline=False)
    embed.set_footer(text="Настройки можно вернуть к стандартным одной кнопкой • T-Mod Profile")
    return embed


async def _load_profile(member: discord.Member) -> tuple[Any | None, list[Any], Any | None]:
    profile, characters = await asyncio.to_thread(
        storage.get_profile_snapshot,
        member.guild.id,
        member.id,
    )
    summaries = await asyncio.to_thread(storage.get_summaries, member.guild.id, [member.id])
    return profile, characters, summaries.get(member.id)


async def _edit_profile_home(
    interaction: discord.Interaction,
    requester_id: int,
    member: discord.Member,
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    profile, characters, activity = await _load_profile(member)
    editable = requester_id == member.id
    await interaction.edit_original_response(
        content=None,
        embed=profile_embed(member, profile, characters, activity, editable=editable),
        view=ProfileHomeView(requester_id, member, characters, editable=editable),
    )


async def _edit_profile_settings(
    interaction: discord.Interaction,
    requester_id: int,
    member: discord.Member,
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    profile, characters = await asyncio.to_thread(
        storage.get_profile_snapshot,
        member.guild.id,
        member.id,
    )
    await interaction.edit_original_response(
        content=None,
        embed=profile_settings_embed(member, profile, characters),
        view=ProfileSettingsView(requester_id, member, profile, characters),
    )


async def _send_profile_error(interaction: discord.Interaction, exc: ValueError) -> None:
    message = PROFILE_ERROR_MESSAGES.get(str(exc), "Не удалось сохранить изменения профиля.")
    await interaction.followup.send(message, ephemeral=True)


async def _save_profile_preferences(
    interaction: discord.Interaction,
    requester_id: int,
    member: discord.Member,
    **changes: Any,
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    try:
        await asyncio.to_thread(
            storage.update_member_profile_preferences,
            member.guild.id,
            member.id,
            **changes,
        )
    except ValueError as exc:
        await _send_profile_error(interaction, exc)
        return
    await _edit_profile_settings(interaction, requester_id, member)


async def _report_unexpected_profile_error(
    interaction: discord.Interaction,
    error: Exception,
) -> None:
    traceback.print_exception(type(error), error, error.__traceback__)
    if interaction.guild is not None:
        try:
            await log_technical_event(
                interaction.client,
                interaction.guild,
                title="Ошибка профиля участника",
                details=(
                    f"Канал: <#{interaction.channel_id}>\n"
                    f"Пользователь: `{interaction.user.id}`\n"
                    f"Ошибка: `{type(error).__name__}: {str(error)[:700]}`"
                ),
                dedupe_key=f"profile-interaction:{type(error).__name__}",
                cooldown_seconds=60,
            )
        except Exception:
            traceback.print_exc()
    message = "Не удалось обновить профиль. Попробуйте ещё раз; ошибка записана в лог-тех."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.DiscordException:
        pass


class ProfileBaseView(discord.ui.View):
    def __init__(self, requester_id: int) -> None:
        super().__init__(timeout=PROFILE_TIMEOUT_SECONDS)
        self.requester_id = int(requester_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Это меню профиля открыто для другого пользователя.",
                ephemeral=True,
            )
            return False
        return True

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        await _report_unexpected_profile_error(interaction, error)


class ProfileModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _report_unexpected_profile_error(interaction, error)


class CharacterModal(ProfileModal):
    nickname = discord.ui.TextInput(
        label="Ник персонажа",
        placeholder="Например: Robert Williams",
        min_length=2,
        max_length=storage.PROFILE_NICKNAME_MAX_LENGTH,
    )
    static_id = discord.ui.TextInput(
        label="Статик",
        placeholder="Только цифры",
        min_length=1,
        max_length=12,
    )

    def __init__(
        self,
        requester_id: int,
        member: discord.Member,
        *,
        character: Any | None = None,
    ) -> None:
        super().__init__(title="Изменить персонажа" if character else "Новый персонаж", timeout=300)
        self.requester_id = int(requester_id)
        self.member = member
        self.character_id = int(character.id) if character is not None else None
        if character is not None:
            self.nickname.default = str(character.nickname)
            self.static_id.default = str(character.static_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Нельзя изменить чужой профиль.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            if self.character_id is None:
                await asyncio.to_thread(
                    storage.add_profile_character,
                    self.member.guild.id,
                    self.member.id,
                    str(self.nickname.value),
                    str(self.static_id.value),
                )
            else:
                await asyncio.to_thread(
                    storage.update_profile_character,
                    self.member.guild.id,
                    self.member.id,
                    self.character_id,
                    str(self.nickname.value),
                    str(self.static_id.value),
                )
        except ValueError as exc:
            await _send_profile_error(interaction, exc)
            return
        await _edit_profile_home(interaction, self.requester_id, self.member)


class StatusNoteModal(ProfileModal, title="Подпись доступности"):
    note = discord.ui.TextInput(
        label="Короткая подпись",
        placeholder="Например: вернусь вечером",
        required=False,
        max_length=storage.PROFILE_STATUS_NOTE_MAX_LENGTH,
        style=discord.TextStyle.paragraph,
    )

    def __init__(
        self,
        requester_id: int,
        member: discord.Member,
        status: str,
        current_note: str | None,
    ) -> None:
        super().__init__(timeout=300)
        self.requester_id = int(requester_id)
        self.member = member
        self.status = status
        self.note.default = str(current_note or "")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Нельзя изменить чужой профиль.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await asyncio.to_thread(
                storage.set_member_profile_status,
                self.member.guild.id,
                self.member.id,
                self.status,
                note=str(self.note.value),
            )
        except ValueError as exc:
            await _send_profile_error(interaction, exc)
            return
        await _edit_profile_home(interaction, self.requester_id, self.member)


class ProfileStatusSelect(discord.ui.Select):
    def __init__(
        self,
        requester_id: int,
        member: discord.Member,
        current_status: str,
        current_note: str | None,
    ) -> None:
        self.requester_id = int(requester_id)
        self.member = member
        self.current_note = current_note
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                emoji=emoji,
                description=description,
                default=key == current_status,
            )
            for key, (emoji, label, description) in PROFILE_STATUS_INFO.items()
        ]
        super().__init__(placeholder="Выберите вашу текущую доступность", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            await asyncio.to_thread(
                storage.set_member_profile_status,
                self.member.guild.id,
                self.member.id,
                self.values[0],
                note=self.current_note,
            )
        except ValueError as exc:
            await _send_profile_error(interaction, exc)
            return
        await _edit_profile_home(interaction, self.requester_id, self.member)


class ProfileStatusView(ProfileBaseView):
    def __init__(self, requester_id: int, member: discord.Member, profile: Any | None) -> None:
        super().__init__(requester_id)
        self.member = member
        self.status = str(getattr(profile, "status", None) or "active")
        self.note = getattr(profile, "status_note", None)
        self.add_item(
            ProfileStatusSelect(requester_id, member, self.status, self.note)
        )

    @discord.ui.button(label="Подпись", emoji="✍️", style=discord.ButtonStyle.secondary, row=1)
    async def note_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            StatusNoteModal(self.requester_id, self.member, self.status, self.note)
        )

    @discord.ui.button(label="Назад", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_home(interaction, self.requester_id, self.member)


class ProfileVisibilitySelect(discord.ui.Select):
    def __init__(self, requester_id: int, member: discord.Member, current: str) -> None:
        self.requester_id = int(requester_id)
        self.member = member
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                emoji=emoji,
                description=description,
                default=key == current,
            )
            for key, (emoji, label, description) in PROFILE_VISIBILITY_INFO.items()
        ]
        super().__init__(placeholder="Кому доступен профиль", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _save_profile_preferences(
            interaction,
            self.requester_id,
            self.member,
            visibility=self.values[0],
        )


class ProfileThemeSelect(discord.ui.Select):
    def __init__(self, requester_id: int, member: discord.Member, current: str) -> None:
        self.requester_id = int(requester_id)
        self.member = member
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                emoji=emoji,
                description=f"Цвет карточки: {label.lower()}",
                default=key == current,
            )
            for key, (emoji, label, _) in PROFILE_THEME_INFO.items()
        ]
        super().__init__(placeholder="Оформление карточки", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _save_profile_preferences(
            interaction,
            self.requester_id,
            self.member,
            theme=self.values[0],
        )


class ProfilePrimaryCharacterSelect(discord.ui.Select):
    def __init__(
        self,
        requester_id: int,
        member: discord.Member,
        characters: list[Any],
        current_id: int | None,
    ) -> None:
        self.requester_id = int(requester_id)
        self.member = member
        options = [
            discord.SelectOption(
                label="Не выделять",
                value="none",
                emoji="➖",
                description="Не назначать основного персонажа",
                default=current_id is None,
            )
        ]
        options.extend(
            discord.SelectOption(
                label=str(character.nickname)[:100],
                value=str(character.id),
                emoji="⭐",
                description=f"Статик {character.static_id} · слот {character.position}"[:100],
                default=character.id == current_id,
            )
            for character in characters
        )
        super().__init__(placeholder="Основной персонаж", options=options, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_id = None if self.values[0] == "none" else int(self.values[0])
        await _save_profile_preferences(
            interaction,
            self.requester_id,
            self.member,
            primary_character_id=selected_id,
        )


class ProfileSettingsView(ProfileBaseView):
    def __init__(
        self,
        requester_id: int,
        member: discord.Member,
        profile: Any | None,
        characters: list[Any],
    ) -> None:
        super().__init__(requester_id)
        self.member = member
        self.characters = characters
        self.show_activity = bool(getattr(profile, "show_activity", True))
        visibility = str(getattr(profile, "visibility", None) or "members")
        theme = str(getattr(profile, "theme", None) or "indigo")
        primary_character_id = getattr(profile, "primary_character_id", None)
        self.add_item(ProfileVisibilitySelect(requester_id, member, visibility))
        self.add_item(ProfileThemeSelect(requester_id, member, theme))
        if characters:
            self.add_item(
                ProfilePrimaryCharacterSelect(
                    requester_id,
                    member,
                    characters,
                    primary_character_id,
                )
            )
        self.activity.label = (
            "Активность: видна" if self.show_activity else "Активность: скрыта"
        )
        self.activity.emoji = "👁️" if self.show_activity else "🙈"

    @discord.ui.button(label="Активность", style=discord.ButtonStyle.secondary, row=3)
    async def activity(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _save_profile_preferences(
            interaction,
            self.requester_id,
            self.member,
            show_activity=not self.show_activity,
        )

    @discord.ui.button(label="По умолчанию", emoji="♻️", style=discord.ButtonStyle.secondary, row=4)
    async def reset(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _save_profile_preferences(
            interaction,
            self.requester_id,
            self.member,
            visibility="members",
            show_activity=True,
            theme="indigo",
            primary_character_id=(self.characters[0].id if self.characters else None),
        )

    @discord.ui.button(label="Назад", emoji="↩️", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_home(interaction, self.requester_id, self.member)


class CharacterSelect(discord.ui.Select):
    def __init__(self, requester_id: int, member: discord.Member, characters: list[Any]) -> None:
        self.requester_id = int(requester_id)
        self.member = member
        options = [
            discord.SelectOption(
                label=str(character.nickname)[:100],
                value=str(character.id),
                description=f"Статик {character.static_id} · слот {character.position}"[:100],
                emoji="🎭",
            )
            for character in characters
        ]
        super().__init__(placeholder="Выберите персонажа", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        character = await asyncio.to_thread(
            storage.get_profile_character,
            self.member.guild.id,
            self.member.id,
            int(self.values[0]),
        )
        if character is None:
            await interaction.followup.send("Персонаж уже удалён.", ephemeral=True)
            return
        await interaction.edit_original_response(
            content=None,
            embed=character_embed(self.member, character),
            view=CharacterDetailView(self.requester_id, self.member, character),
        )


class CharacterManagerView(ProfileBaseView):
    def __init__(self, requester_id: int, member: discord.Member, characters: list[Any]) -> None:
        super().__init__(requester_id)
        self.member = member
        self.characters = characters
        if characters:
            self.add_item(CharacterSelect(requester_id, member, characters))
        self.add.disabled = len(characters) >= storage.PROFILE_MAX_CHARACTERS

    @discord.ui.button(label="Добавить", emoji="➕", style=discord.ButtonStyle.primary, row=1)
    async def add(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CharacterModal(self.requester_id, self.member))

    @discord.ui.button(label="Назад", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_home(interaction, self.requester_id, self.member)


class CharacterDeleteView(ProfileBaseView):
    def __init__(self, requester_id: int, member: discord.Member, character: Any) -> None:
        super().__init__(requester_id)
        self.member = member
        self.character = character

    @discord.ui.button(label="Удалить персонажа", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        try:
            await asyncio.to_thread(
                storage.delete_profile_character,
                self.member.guild.id,
                self.member.id,
                self.character.id,
            )
        except ValueError as exc:
            await _send_profile_error(interaction, exc)
            return
        await _edit_profile_home(interaction, self.requester_id, self.member)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=character_embed(self.member, self.character),
            view=CharacterDetailView(self.requester_id, self.member, self.character),
        )


class CharacterDetailView(ProfileBaseView):
    def __init__(self, requester_id: int, member: discord.Member, character: Any) -> None:
        super().__init__(requester_id)
        self.member = member
        self.character = character

    @discord.ui.button(label="Изменить", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            CharacterModal(self.requester_id, self.member, character=self.character)
        )

    @discord.ui.button(label="Удалить", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(
            title="Удалить персонажа?",
            description=(
                f"Запись **{_clean_display(self.character.nickname)}** со статиком "
                f"`{_clean_display(self.character.static_id)}` будет удалена из профиля."
            ),
            color=0xED4245,
        )
        await interaction.response.edit_message(
            embed=embed,
            view=CharacterDeleteView(self.requester_id, self.member, self.character),
        )

    @discord.ui.button(label="К списку", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        characters = await asyncio.to_thread(
            storage.list_profile_characters,
            self.member.guild.id,
            self.member.id,
        )
        await interaction.edit_original_response(
            embed=character_manager_embed(self.member, characters),
            view=CharacterManagerView(self.requester_id, self.member, characters),
        )


class ProfileHomeView(ProfileBaseView):
    def __init__(
        self,
        requester_id: int,
        member: discord.Member,
        characters: list[Any],
        *,
        editable: bool,
    ) -> None:
        super().__init__(requester_id)
        self.member = member
        self.characters = characters
        if editable:
            self.add.disabled = len(characters) >= storage.PROFILE_MAX_CHARACTERS
            self.manage.disabled = not characters
        else:
            self.remove_item(self.add)
            self.remove_item(self.manage)
            self.remove_item(self.status)
            self.remove_item(self.settings)

    @discord.ui.button(label="Добавить", emoji="➕", style=discord.ButtonStyle.primary, row=0)
    async def add(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CharacterModal(self.requester_id, self.member))

    @discord.ui.button(label="Персонажи", emoji="🎭", style=discord.ButtonStyle.secondary, row=0)
    async def manage(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=character_manager_embed(self.member, self.characters),
            view=CharacterManagerView(self.requester_id, self.member, self.characters),
        )

    @discord.ui.button(label="Доступность", emoji="🟢", style=discord.ButtonStyle.secondary, row=0)
    async def status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        profile = await asyncio.to_thread(
            storage.get_member_profile,
            self.member.guild.id,
            self.member.id,
        )
        emoji, label, description, note = _profile_status(profile)
        embed = discord.Embed(
            title="Доступность участника",
            description=(
                f"Сейчас: {emoji} **{label}** — {description}\n"
                "Доступность задаётся вручную и не зависит от индикатора Discord."
            ),
            color=PROFILE_COLOR,
        )
        if note:
            embed.add_field(name="Подпись", value=_clean_display(note), inline=False)
        await interaction.edit_original_response(
            embed=embed,
            view=ProfileStatusView(self.requester_id, self.member, profile),
        )

    @discord.ui.button(label="Настройки", emoji="⚙️", style=discord.ButtonStyle.secondary, row=0)
    async def settings(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_settings(interaction, self.requester_id, self.member)

    @discord.ui.button(label="Обновить", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_home(interaction, self.requester_id, self.member)


def setup_profile(
    bot: commands.Bot,
    remember_command_activity: Callable[[discord.Interaction, str, str], None] | None = None,
) -> None:
    @bot.tree.command(name="profile", description="Открыть профиль участника и его персонажей")
    @app_commands.guild_only()
    @app_commands.describe(user="Участник, чей профиль нужно посмотреть")
    async def profile(interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает на сервере.", ephemeral=True)
            return
        target = user or interaction.user
        if remember_command_activity is not None:
            details = "/profile" if target.id == interaction.user.id else f"/profile user:{target.id}"
            remember_command_activity(interaction, "command_profile", details)
        await interaction.response.defer(ephemeral=True, thinking=True)
        profile_data, characters, activity = await _load_profile(target)
        editable = target.id == interaction.user.id
        await interaction.edit_original_response(
            embed=profile_embed(target, profile_data, characters, activity, editable=editable),
            view=ProfileHomeView(interaction.user.id, target, characters, editable=editable),
            allowed_mentions=discord.AllowedMentions.none(),
        )


__all__ = [
    "CharacterDeleteView",
    "CharacterDetailView",
    "CharacterManagerView",
    "CharacterModal",
    "ProfileBaseView",
    "ProfileHomeView",
    "ProfileSettingsView",
    "ProfileStatusView",
    "StatusNoteModal",
    "character_embed",
    "character_manager_embed",
    "member_position_text",
    "profile_embed",
    "profile_settings_embed",
    "setup_profile",
]
