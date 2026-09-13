"""Private member profile panel with reusable game characters."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import discord
from discord import app_commands
from discord.ext import commands

from persistence import profile_context as storage
from persistence import web_auth_repository as web_auth_storage
from persistence import voice_control_context as voice_storage
from modules.technical_log import log_technical_event
from modules.control_center_runtime import resolve_registered_channel
from modules.error_inbox import capture_runtime_event
from modules.profile_notifications import PROFILE_TIMEZONE_NAME
from modules.voice_control_service import VoiceDiagnosticResult, get_voice_control
from modules.profile_voice import ProfileMicrophoneView, profile_microphone_embed
from modules.tvrs_config import TVRS_SENATOR_ROLE_ID


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
PROFILE_DM_INFO = {
    "dm_market": ("📈", "Рынок"),
    "dm_craft": ("🛠️", "Крафт"),
    "dm_consensus": ("🏛️", "Консенсус"),
    "dm_finance": ("💰", "Финансы"),
    "dm_system": ("🤖", "Системные"),
}
PROFILE_ERROR_MESSAGES = {
    "profile_nickname_invalid": "Ник должен содержать от 2 до 48 символов.",
    "profile_static_invalid": "Статик должен состоять из 1–12 цифр.",
    "profile_static_taken": "Этот статик уже привязан к другому персонажу на сервере.",
    "profile_character_limit": "В профиле уже сохранены три персонажа.",
    "profile_character_not_found": "Персонаж уже удалён или недоступен.",
    "profile_character_visibility_invalid": "Не удалось изменить видимость персонажа.",
    "profile_status_invalid": "Не удалось распознать выбранную доступность.",
    "profile_status_note_too_long": "Подпись доступности должна быть не длиннее 120 символов.",
    "profile_character_conflict": "Не удалось сохранить персонажа из-за конфликта данных.",
    "profile_visibility_invalid": "Не удалось распознать режим видимости профиля.",
    "profile_show_activity_invalid": "Не удалось изменить видимость активности.",
    "profile_theme_invalid": "Не удалось распознать оформление профиля.",
    "profile_primary_character_invalid": "Выбранный персонаж больше не находится в вашем профиле.",
    "profile_boolean_preference_invalid": "Не удалось изменить персональную настройку.",
    "profile_biography_invalid": "Расскажите о себе в 3–500 символах.",
    "profile_contribution_invalid": "Опишите свою деятельность в 3–500 символах.",
    "profile_responsibilities_invalid": "Укажите зону ответственности или интересов в 3–700 символах.",
    "profile_membership_since_invalid": "Укажите дату вступления в формате ГГГГ-ММ-ДД.",
    "profile_preferred_name_invalid": "Укажите, как к вам обращаться: от 2 до 24 символов без знака |.",
    "profile_character_full_name_required": "Укажите имя и фамилию персонажа через пробел.",
    "profile_discord_nickname_too_long": "Итоговый ник Discord длиннее 32 символов. Сократите фамилию или имя для обращения.",
    "profile_quiet_hours_invalid": (
        "Проверьте время тихих часов: используйте ЧЧ:ММ, начало и конец должны отличаться."
    ),
    "web_login_invalid": "Логин: 3–32 латинских символа, цифры, точка, дефис или подчёркивание.",
    "web_pin_invalid": "PIN должен состоять ровно из 8 цифр.",
    "web_pin_mismatch": "Введённые PIN не совпадают.",
    "web_login_taken": "Этот логин уже занят другим участником.",
}
CHARACTER_NUMBERS = {1: "①", 2: "②", 3: "③"}
MEMBER_ONBOARDING_REMINDER_SECONDS = 24 * 60 * 60
MEMBER_ONBOARDING_WORKER_SECONDS = 60 * 60
_CUSTOM_EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_~]{1,32}:\d{15,25}>$")


def _clean_display(value: Any, *, fallback: str = "Не указано") -> str:
    text = " ".join(str(value or "").strip().split())
    return discord.utils.escape_markdown(text) if text else fallback


def _safe_component_emoji(value: Any, fallback: str | None = None) -> str | None:
    """Return an emoji Discord can serialize, or omit it safely.

    Profile menus contain a few values assembled dynamically (select options,
    preference buttons and persisted profile data). Discord rejects malformed
    custom-emoji markup with HTTP 400 ``Invalid emoji``. Keep valid Unicode and
    Discord custom emoji, while dropping arbitrary/control text before a
    component can reach the API.
    """

    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    if text.startswith("<"):
        return text if _CUSTOM_EMOJI_RE.fullmatch(text) else fallback
    if len(text) > 8:
        return fallback
    for character in text:
        category = unicodedata.category(character)
        if category.startswith("C") and character not in ("\ufe0f", "\u200d"):
            return fallback
    # Plain words/punctuation are not Unicode emoji and produce Invalid emoji.
    if all(unicodedata.category(character)[0] in {"L", "N", "P", "Z"} for character in text):
        return fallback
    return text


def _normalize_profile_component(item: discord.ui.Item[Any]) -> None:
    """Sanitize a profile button/select and all of its select options."""

    if hasattr(item, "emoji"):
        item.emoji = _safe_component_emoji(getattr(item, "emoji", None))
    if isinstance(item, discord.ui.Select):
        for option in item.options:
            option.emoji = _safe_component_emoji(option.emoji)


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
    if editable or bool(getattr(profile, "show_availability", True)):
        status_value = f"{emoji} **{status_label}** — {status_description}"
        if status_note:
            status_value += f"\n> {_clean_display(status_note)}"
        embed.add_field(name="Доступность", value=status_value[:1024], inline=False)

    joined_at = getattr(member, "joined_at", None)
    show_activity = editable or bool(getattr(profile, "show_activity", True))
    show_join_date = editable or bool(getattr(profile, "show_join_date", True))
    last_activity = getattr(activity, "last_activity_at", None)
    total_events = max(0, int(getattr(activity, "total_events", 0) or 0))
    participation_lines = []
    if show_join_date:
        participation_lines.append(f"На сервере: {_discord_time(joined_at, 'D')}")
    if show_activity:
        activity_line = f"Последняя активность: {_discord_time(last_activity)}"
        if total_events:
            activity_line += f" · событий: **{total_events:,}**".replace(",", " ")
        participation_lines.append(activity_line)
    if participation_lines:
        embed.add_field(name="Участие", value="\n".join(participation_lines), inline=False)
    if editable or bool(getattr(profile, "show_position", True)):
        embed.add_field(
            name="Положение в Товариществе",
            value=member_position_text(member)[:1024],
            inline=False,
        )

    show_directory = editable or bool(getattr(profile, "show_directory", True))
    biography = getattr(profile, "biography", None)
    contribution = getattr(profile, "contribution", None)
    responsibilities = getattr(profile, "responsibilities", None)
    membership_since = getattr(profile, "membership_since", None)
    if show_directory and any((biography, contribution, responsibilities, membership_since)):
        if biography:
            embed.add_field(
                name="Справочник · о себе",
                value=_clean_display(biography)[:1024],
                inline=False,
            )
        if contribution:
            embed.add_field(
                name="Справочник · деятельность",
                value=_clean_display(contribution)[:1024],
                inline=False,
            )
        if membership_since:
            try:
                joined_stamp = int(
                    datetime.fromisoformat(str(membership_since))
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                )
                embed.add_field(
                    name="В Товариществе",
                    value=f"<t:{joined_stamp}:D>",
                    inline=True,
                )
            except ValueError:
                pass
        if responsibilities:
            embed.add_field(
                name="Ответственность и интересы",
                value=_clean_display(responsibilities)[:1024],
                inline=False,
            )
    elif editable:
        required = bool(getattr(profile, "directory_required", False))
        embed.add_field(
            name="Справочник участников",
            value=(
                "🔴 Карточку необходимо заполнить как часть процедуры вступления."
                if required
                else "Карточка пока не заполнена. Для действующих участников это рекомендуется."
            ),
            inline=False,
        )

    show_characters = editable or bool(getattr(profile, "show_characters", True))
    displayed_characters = (
        list(characters)
        if editable
        else [character for character in characters if bool(getattr(character, "is_public", True))]
    )
    if show_characters and not displayed_characters:
        embed.add_field(
            name="Персонажи",
            value=(
                "Персонажи ещё не добавлены. Нажмите **«Добавить»**, чтобы сохранить первого."
                if editable
                else "Владелец пока не опубликовал персонажей."
            ),
            inline=False,
        )
    elif show_characters:
        primary_character_id = getattr(profile, "primary_character_id", None)
        for display_position, character in enumerate(
            displayed_characters[: storage.PROFILE_MAX_CHARACTERS],
            start=1,
        ):
            # A compact public sequence does not disclose that a character between
            # two visible entries exists but is hidden by its owner.
            position = (
                int(getattr(character, "position", 0) or 0)
                if editable
                else display_position
            )
            nickname = _clean_display(getattr(character, "nickname", "Персонаж"))
            static_id = _clean_display(getattr(character, "static_id", "—"))
            is_public = bool(getattr(character, "is_public", True))
            embed.add_field(
                name=(
                    f"{'⭐ ' if primary_character_id == character.id else ''}"
                    f"{CHARACTER_NUMBERS.get(position, '◆')} {nickname}"
                )[:256],
                value=(
                    f"Статик: `{static_id}`"
                    + ("\nОсновной персонаж" if primary_character_id == character.id else "")
                    + ("\n🔒 Скрыт от других участников" if editable and not is_public else "")
                ),
                inline=True,
            )
    visible_count = sum(
        1 for character in characters if bool(getattr(character, "is_public", True))
    )
    footer_count = (
        f"Персонажей: {len(characters)}/{storage.PROFILE_MAX_CHARACTERS} · открыто: {visible_count}"
        if editable
        else f"Открытых персонажей: {len(displayed_characters)}"
    )
    embed.set_footer(text=f"{footer_count} • T-Mod Profile")
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
    embed.add_field(
        name="Видимость",
        value=(
            "🌐 Видим другим участникам"
            if bool(getattr(character, "is_public", True))
            else "🔒 Скрыт от других участников"
        ),
        inline=False,
    )
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
            f"{'🌐' if bool(getattr(character, 'is_public', True)) else '🔒'} "
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
    voice_profile: Any | None = None,
    local_status: str = "unknown",
    web_credential: Any | None = None,
) -> discord.Embed:
    visibility = str(getattr(profile, "visibility", None) or "members")
    visibility_emoji, visibility_label, visibility_description = PROFILE_VISIBILITY_INFO.get(
        visibility,
        PROFILE_VISIBILITY_INFO["members"],
    )
    theme_emoji, theme_label, theme_color = _profile_theme(profile)
    privacy_fields = (
        ("show_availability", "доступность"),
        ("show_position", "положение"),
        ("show_characters", "персонажи"),
        ("show_join_date", "дата вступления"),
        ("show_activity", "активность"),
        ("show_directory", "справочник"),
    )
    hidden_fields = [
        label for field, label in privacy_fields if not bool(getattr(profile, field, True))
    ]
    dm_enabled = bool(getattr(profile, "dm_notifications", True))
    enabled_dm_kinds = sum(
        1 for field in PROFILE_DM_INFO if bool(getattr(profile, field, True))
    )
    quiet_enabled = bool(getattr(profile, "quiet_hours_enabled", False))
    quiet_start = int(getattr(profile, "quiet_start_minute", 0) or 0)
    quiet_end = int(getattr(profile, "quiet_end_minute", 480) or 0)
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
        name="Приватность",
        value=(
            f"{visibility_emoji} **{visibility_label}**\n"
            + (
                f"Скрыто разделов: **{len(hidden_fields)}**"
                if hidden_fields
                else visibility_description
            )
        ),
        inline=True,
    )
    embed.add_field(
        name="Уведомления в ЛС",
        value=(
            f"🔔 **Включены** · разделов {enabled_dm_kinds}/{len(PROFILE_DM_INFO)}"
            if dm_enabled
            else "🔕 **Все отключены**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Тихие часы",
        value=(
            f"🌙 **{quiet_start // 60:02d}:{quiet_start % 60:02d}–"
            f"{quiet_end // 60:02d}:{quiet_end % 60:02d}**"
            if quiet_enabled
            else "☀️ **Выключены**"
        ),
        inline=True,
    )
    embed.add_field(name="Оформление", value=f"{theme_emoji} **{theme_label}**", inline=True)
    embed.add_field(
        name="Веб-доступ",
        value=(
            f"🔐 **{_clean_display(web_credential.login)}** · постоянный вход включён"
            if web_credential is not None
            else "🔒 **Не настроен** · вход только по ссылке Discord"
        ),
        inline=True,
    )
    quality_score = int(getattr(voice_profile, "quality_score", 0) or 0)
    calibrated_at = getattr(voice_profile, "calibrated_at", None)
    local_ready = local_status == "ready"
    embed.add_field(
        name="Голос и микрофон",
        value=(
            f"{'🟢' if local_ready else '🟡'} Быстрый движок: "
            f"**{'готов' if local_ready else 'подготавливается'}**\n"
            + (
                f"Калибровка: **{quality_score}/100** · {_discord_time(calibrated_at)}"
                if calibrated_at
                else "Калибровка ещё не выполнена"
            )
        ),
        inline=True,
    )
    embed.add_field(name="Основной персонаж", value=primary_text, inline=False)
    embed.set_footer(text="Настройки можно вернуть к стандартным одной кнопкой • T-Mod Profile")
    return embed


def profile_privacy_embed(member: discord.Member, profile: Any | None) -> discord.Embed:
    visibility = str(getattr(profile, "visibility", None) or "members")
    emoji, label, description = PROFILE_VISIBILITY_INFO.get(
        visibility,
        PROFILE_VISIBILITY_INFO["members"],
    )
    settings = (
        ("show_availability", "Доступность"),
        ("show_position", "Положение в Товариществе"),
        ("show_characters", "Персонажи"),
        ("show_join_date", "Дата вступления"),
        ("show_activity", "Последняя активность"),
        ("show_directory", "Карточка справочника"),
    )
    lines = [
        f"{'👁️' if bool(getattr(profile, field, True)) else '🙈'} **{name}**"
        for field, name in settings
    ]
    embed = discord.Embed(
        title="Приватность профиля",
        description=f"Настройки видимости **{_clean_display(member.display_name)}**.",
        color=_profile_theme(profile)[2],
    )
    embed.add_field(name="Доступ к профилю", value=f"{emoji} **{label}**\n{description}", inline=False)
    embed.add_field(name="Отдельные разделы", value="\n".join(lines), inline=False)
    embed.set_footer(text="Настройки влияют только на просмотр профиля другими участниками")
    return embed


def profile_notifications_embed(member: discord.Member, profile: Any | None) -> discord.Embed:
    all_enabled = bool(getattr(profile, "dm_notifications", True))
    lines = [
        f"{'🔔' if bool(getattr(profile, field, True)) else '🔕'} {emoji} **{label}**"
        for field, (emoji, label) in PROFILE_DM_INFO.items()
    ]
    embed = discord.Embed(
        title="Уведомления в ЛС",
        description=f"Личные уведомления **{_clean_display(member.display_name)}**.",
        color=_profile_theme(profile)[2],
    )
    embed.add_field(
        name="Общий переключатель",
        value=(
            "🔔 **Уведомления разрешены**"
            if all_enabled
            else "🔕 **Все необязательные уведомления отключены**"
        ),
        inline=False,
    )
    embed.add_field(name="Разделы", value="\n".join(lines), inline=False)
    embed.add_field(
        name="Важно",
        value=(
            "Служебные сообщения, без которых нельзя подтвердить участие, проголосовать или "
            "завершить обязательное действие, не блокируются этой настройкой."
        ),
        inline=False,
    )
    return embed


def profile_quiet_hours_embed(member: discord.Member, profile: Any | None) -> discord.Embed:
    enabled = bool(getattr(profile, "quiet_hours_enabled", False))
    start = int(getattr(profile, "quiet_start_minute", 0) or 0)
    end = int(getattr(profile, "quiet_end_minute", 480) or 0)
    embed = discord.Embed(
        title="Тихие часы",
        description=f"Период без необязательных ЛС для **{_clean_display(member.display_name)}**.",
        color=_profile_theme(profile)[2],
    )
    embed.add_field(
        name="Текущий режим",
        value=(
            f"🌙 **{start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d}**"
            if enabled
            else "☀️ **Выключен**"
        ),
        inline=False,
    )
    embed.add_field(name="Часовой пояс", value=f"`{PROFILE_TIMEZONE_NAME}`", inline=True)
    embed.add_field(
        name="Как работает",
        value=(
            "Уведомления не теряются и не расходуют попытки доставки. T-Mod откладывает их до "
            "конца тихого периода. Обязательные интерактивные сообщения приходят сразу."
        ),
        inline=False,
    )
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
    (profile, characters), voice_profile, web_credential = await asyncio.gather(
        asyncio.to_thread(
            storage.get_profile_snapshot,
            member.guild.id,
            member.id,
        ),
        asyncio.to_thread(
            voice_storage.get_voice_user_profile,
            member.guild.id,
            member.id,
        ),
        asyncio.to_thread(
            web_auth_storage.get_web_credential,
            member.guild.id,
            member.id,
        ),
    )
    voice_control = get_voice_control(interaction.client)
    await interaction.edit_original_response(
        content=None,
        embed=profile_settings_embed(
            member,
            profile,
            characters,
            voice_profile,
            voice_control.local_status if voice_control is not None else "unavailable",
            web_credential,
        ),
        view=ProfileSettingsView(requester_id, member, profile, characters),
    )


def profile_web_access_embed(
    member: discord.Member,
    credential: Any | None,
) -> discord.Embed:
    embed = discord.Embed(
        title="Веб-доступ T-Mod",
        description=(
            "Логин и восьмизначный PIN задаются только здесь, в личном меню Discord. "
            "Права на сайте всегда берутся из текущих ролей сервера."
        ),
        color=PROFILE_COLOR,
    )
    embed.add_field(
        name="Состояние",
        value=(
            (
                f"🔴 **Требуется сброс**\nЛогин: `{_clean_display(credential.login)}`\n"
                "Отправьте `/reset` боту в ЛС и задайте новый PIN."
                if bool(getattr(credential, "reset_required", False))
                else f"🟢 **Включён**\nЛогин: `{_clean_display(credential.login)}`"
            )
            if credential is not None
            else "⚪ **Не настроен**\nИспользуйте кнопку «Настроить»."
        ),
        inline=False,
    )
    embed.add_field(
        name="Безопасность",
        value=(
            "PIN хранится только в виде защищённого хэша. После трёх ошибочных попыток "
            "вход блокируется до установки нового PIN через `/reset`. Смена или "
            "отключение доступа завершает старые сессии."
        ),
        inline=False,
    )
    if credential is not None and credential.last_login_at:
        embed.add_field(
            name="Последний вход",
            value=_discord_time(credential.last_login_at),
            inline=True,
        )
    return embed


async def _edit_profile_web_access(
    interaction: discord.Interaction,
    requester_id: int,
    member: discord.Member,
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    credential = await asyncio.to_thread(
        web_auth_storage.get_web_credential,
        member.guild.id,
        member.id,
    )
    payload = {
        "content": None,
        "embed": profile_web_access_embed(member, credential),
        "view": ProfileWebAccessView(requester_id, member, credential),
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    try:
        await interaction.edit_original_response(**payload)
    except discord.NotFound as exc:
        # A modal opened directly by /reset has no component message to update.
        # Discord may also invalidate an old ephemeral message between submit
        # and render. In both cases the saved credential is still valid, so
        # render a fresh private panel instead of reporting a failed reset.
        if int(getattr(exc, "code", 0) or 0) != 10008:
            raise
        await interaction.followup.send(ephemeral=True, **payload)


async def _edit_profile_microphone(
    interaction: discord.Interaction,
    requester_id: int,
    member: discord.Member,
    *,
    diagnostic: VoiceDiagnosticResult | None = None,
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    voice_profile = await asyncio.to_thread(
        voice_storage.get_voice_user_profile,
        member.guild.id,
        member.id,
    )
    voice_control = get_voice_control(interaction.client)
    await interaction.edit_original_response(
        content=None,
        embed=profile_microphone_embed(
            member,
            voice_profile,
            local_status=(voice_control.local_status if voice_control else "unavailable"),
            diagnostic=diagnostic,
        ),
        view=ProfileMicrophoneView(
            requester_id,
            member,
            render=_edit_profile_microphone,
            back=_edit_profile_settings,
            report_error=_report_unexpected_profile_error,
        ),
    )


async def _edit_profile_privacy(
    interaction: discord.Interaction,
    requester_id: int,
    member: discord.Member,
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    profile = await asyncio.to_thread(storage.get_member_profile, member.guild.id, member.id)
    await interaction.edit_original_response(
        content=None,
        embed=profile_privacy_embed(member, profile),
        view=ProfilePrivacyView(requester_id, member, profile),
    )


async def _edit_profile_notifications(
    interaction: discord.Interaction,
    requester_id: int,
    member: discord.Member,
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    profile = await asyncio.to_thread(storage.get_member_profile, member.guild.id, member.id)
    await interaction.edit_original_response(
        content=None,
        embed=profile_notifications_embed(member, profile),
        view=ProfileNotificationsView(requester_id, member, profile),
    )


async def _edit_profile_quiet_hours(
    interaction: discord.Interaction,
    requester_id: int,
    member: discord.Member,
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    profile = await asyncio.to_thread(storage.get_member_profile, member.guild.id, member.id)
    await interaction.edit_original_response(
        content=None,
        embed=profile_quiet_hours_embed(member, profile),
        view=ProfileQuietHoursView(requester_id, member, profile),
    )


async def _send_profile_error(interaction: discord.Interaction, exc: ValueError) -> None:
    message = PROFILE_ERROR_MESSAGES.get(str(exc), "Не удалось сохранить изменения профиля.")
    await interaction.followup.send(message, ephemeral=True)


async def _save_profile_preferences(
    interaction: discord.Interaction,
    requester_id: int,
    member: discord.Member,
    *,
    return_to: str = "settings",
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
    destination = {
        "privacy": _edit_profile_privacy,
        "notifications": _edit_profile_notifications,
        "quiet_hours": _edit_profile_quiet_hours,
    }.get(return_to, _edit_profile_settings)
    await destination(interaction, requester_id, member)


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
        for item in self.children:
            _normalize_profile_component(item)

    def add_item(self, item: discord.ui.Item[Any]) -> "ProfileBaseView":
        _normalize_profile_component(item)
        super().add_item(item)
        return self

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


class WebAccessModal(ProfileModal, title="Веб-доступ T-Mod"):
    login = discord.ui.TextInput(
        label="Логин",
        placeholder="latin.login",
        min_length=3,
        max_length=32,
    )
    pin = discord.ui.TextInput(
        label="PIN — ровно 8 цифр",
        placeholder="••••••••",
        min_length=8,
        max_length=8,
    )
    pin_repeat = discord.ui.TextInput(
        label="Повторите PIN",
        placeholder="••••••••",
        min_length=8,
        max_length=8,
    )

    def __init__(
        self,
        requester_id: int,
        member: discord.Member,
        credential: Any | None,
    ) -> None:
        super().__init__(timeout=300)
        self.requester_id = int(requester_id)
        self.member = member
        if credential is not None:
            self.login.default = str(credential.login)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Нельзя изменить чужой веб-доступ.",
                ephemeral=True,
            )
            return
        if str(self.pin.value) != str(self.pin_repeat.value):
            await interaction.response.send_message(
                PROFILE_ERROR_MESSAGES["web_pin_mismatch"],
                ephemeral=True,
            )
            return
        # Modal submissions created directly by /reset do not have an original
        # message. A thinking response creates one that can be safely edited.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await asyncio.to_thread(
                web_auth_storage.configure_web_credential,
                self.member.guild.id,
                self.member.id,
                str(self.login.value),
                str(self.pin.value),
            )
        except ValueError as exc:
            await _send_profile_error(interaction, exc)
            return
        await _edit_profile_web_access(interaction, self.requester_id, self.member)


class ProfileWebAccessDisableView(ProfileBaseView):
    def __init__(self, requester_id: int, member: discord.Member) -> None:
        super().__init__(requester_id)
        self.member = member

    @discord.ui.button(label="Отключить вход", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        await asyncio.to_thread(
            web_auth_storage.delete_web_credential,
            self.member.guild.id,
            self.member.id,
        )
        await _edit_profile_web_access(interaction, self.requester_id, self.member)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_web_access(interaction, self.requester_id, self.member)


class ProfileWebAccessView(ProfileBaseView):
    def __init__(
        self,
        requester_id: int,
        member: discord.Member,
        credential: Any | None,
    ) -> None:
        super().__init__(requester_id)
        self.member = member
        self.credential = credential
        if credential is None:
            self.remove_item(self.disable)

    @discord.ui.button(label="Настроить", emoji="🔑", style=discord.ButtonStyle.primary)
    async def configure(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            WebAccessModal(self.requester_id, self.member, self.credential)
        )

    @discord.ui.button(label="Отключить", emoji="🔒", style=discord.ButtonStyle.danger)
    async def disable(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(
            title="Отключить постоянный веб-вход?",
            description=(
                "Логин будет удалён, а открытые с его помощью сессии перестанут работать. "
                "Персональные ссылки Discord останутся доступны."
            ),
            color=0xED4245,
        )
        await interaction.response.edit_message(
            embed=embed,
            view=ProfileWebAccessDisableView(self.requester_id, self.member),
        )

    @discord.ui.button(label="Назад", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_settings(interaction, self.requester_id, self.member)


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


def _tmod_account_embed(
    user: discord.abc.User,
    characters: list[Any],
    credential: Any | None,
) -> discord.Embed:
    active = bool(credential)
    embed = discord.Embed(
        title="T-Mod Account",
        description=(
            "Единая учётная запись активна. Она узнаёт вас в сервисах T-Mod."
            if active
            else "Задайте логин и восьмизначный PIN. Персонажа можно добавить до подачи заявок в сервисах."
        ),
        color=0x57F2C8 if active else 0x5865F2,
    )
    embed.add_field(
        name="Статус",
        value="🟢 Активен" if active else "◌ Требуется настройка",
        inline=True,
    )
    embed.add_field(
        name="Уровень",
        value="Нулевой пользователь" if not isinstance(user, discord.Member) else "Участник T-Mod",
        inline=True,
    )
    embed.add_field(
        name="Персонажи",
        value=(
            "\n".join(f"**{item.nickname}** · `{item.static_id}`" for item in characters)
            if characters
            else "Персонаж ещё не добавлен"
        ),
        inline=False,
    )
    embed.add_field(
        name="Веб-вход",
        value=f"Логин: `{credential.login}`" if credential else "Логин и PIN ещё не заданы",
        inline=False,
    )
    if not isinstance(user, discord.Member):
        embed.set_footer(text="Нулевой уровень не открывает Товарищество и персональный Reactor")
    return embed


async def _edit_tmod_account(
    interaction: discord.Interaction,
    guild_id: int,
    requester_id: int,
) -> None:
    characters, credential, profile = await asyncio.gather(
        asyncio.to_thread(storage.list_profile_characters, guild_id, requester_id),
        asyncio.to_thread(web_auth_storage.get_web_credential, guild_id, requester_id),
        asyncio.to_thread(storage.get_member_profile, guild_id, requester_id),
    )
    guild = interaction.client.get_guild(int(guild_id))
    account_user = guild.get_member(int(requester_id)) if guild is not None else None
    await interaction.edit_original_response(
        embed=_tmod_account_embed(account_user or interaction.user, characters, credential),
        view=TModAccountView(
            guild_id,
            requester_id,
            characters,
            credential,
            fellowship_member=isinstance(account_user, discord.Member),
            onboarding_required=bool(getattr(profile, "directory_required", False)),
        ),
        allowed_mentions=discord.AllowedMentions.none(),
    )


class TModAccountCharacterModal(ProfileModal, title="Персонаж T-Mod Account"):
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

    def __init__(self, guild_id: int, requester_id: int, character: Any | None = None) -> None:
        super().__init__(timeout=300)
        self.guild_id = int(guild_id)
        self.requester_id = int(requester_id)
        self.character_id = int(character.id) if character is not None else None
        if character is not None:
            self.nickname.default = str(character.nickname)
            self.static_id.default = str(character.static_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Нельзя изменить чужой аккаунт.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if self.character_id is None:
                await asyncio.to_thread(
                    storage.add_profile_character,
                    self.guild_id,
                    self.requester_id,
                    str(self.nickname.value),
                    str(self.static_id.value),
                )
            else:
                await asyncio.to_thread(
                    storage.update_profile_character,
                    self.guild_id,
                    self.requester_id,
                    self.character_id,
                    str(self.nickname.value),
                    str(self.static_id.value),
                )
        except ValueError as exc:
            await _send_profile_error(interaction, exc)
            return
        await _edit_tmod_account(interaction, self.guild_id, self.requester_id)


class TModAccountCredentialModal(ProfileModal, title="Веб-доступ T-Mod"):
    login = discord.ui.TextInput(label="Логин", placeholder="latin.login", min_length=3, max_length=32)
    pin = discord.ui.TextInput(label="PIN — ровно 8 цифр", placeholder="••••••••", min_length=8, max_length=8)
    pin_repeat = discord.ui.TextInput(label="Повторите PIN", placeholder="••••••••", min_length=8, max_length=8)

    def __init__(self, guild_id: int, requester_id: int, credential: Any | None) -> None:
        super().__init__(timeout=300)
        self.guild_id = int(guild_id)
        self.requester_id = int(requester_id)
        if credential is not None:
            self.login.default = str(credential.login)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Нельзя изменить чужой аккаунт.", ephemeral=True)
            return
        if str(self.pin.value) != str(self.pin_repeat.value):
            await interaction.response.send_message(PROFILE_ERROR_MESSAGES["web_pin_mismatch"], ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await asyncio.to_thread(
                web_auth_storage.configure_web_credential,
                self.guild_id,
                self.requester_id,
                str(self.login.value),
                str(self.pin.value),
            )
        except ValueError as exc:
            await _send_profile_error(interaction, exc)
            return
        await _edit_tmod_account(interaction, self.guild_id, self.requester_id)


def _tmod_account_character_embed(character: Any) -> discord.Embed:
    return discord.Embed(
        title=str(character.nickname)[:250],
        description=f"Игровой персонаж · статик `{character.static_id}`",
        color=0x5865F2,
    )


class TModAccountCharacterSelect(discord.ui.Select):
    def __init__(self, guild_id: int, requester_id: int, characters: list[Any]) -> None:
        super().__init__(
            placeholder="Выберите персонажа",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=str(item.nickname)[:100],
                    description=f"Статик {item.static_id}"[:100],
                    value=str(item.id),
                )
                for item in characters
            ],
        )
        self.guild_id = int(guild_id)
        self.requester_id = int(requester_id)
        self.characters = {int(item.id): item for item in characters}

    async def callback(self, interaction: discord.Interaction) -> None:
        character = self.characters.get(int(self.values[0]))
        if character is None:
            await interaction.response.send_message("Персонаж уже изменён. Обновите аккаунт.", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=_tmod_account_character_embed(character),
            view=TModAccountCharacterDetailView(self.guild_id, self.requester_id, character),
        )


class TModAccountCharactersView(ProfileBaseView):
    def __init__(self, guild_id: int, requester_id: int, characters: list[Any]) -> None:
        super().__init__(requester_id)
        self.guild_id = int(guild_id)
        self.add_item(TModAccountCharacterSelect(guild_id, requester_id, characters))

    @discord.ui.button(label="К аккаунту", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        await _edit_tmod_account(interaction, self.guild_id, self.requester_id)


class TModAccountDeleteConfirmView(ProfileBaseView):
    def __init__(self, guild_id: int, requester_id: int, character: Any) -> None:
        super().__init__(requester_id)
        self.guild_id = int(guild_id)
        self.character = character

    @discord.ui.button(label="Удалить", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        try:
            await asyncio.to_thread(
                storage.delete_profile_character,
                self.guild_id,
                self.requester_id,
                int(self.character.id),
            )
        except ValueError as exc:
            await _send_profile_error(interaction, exc)
            return
        await _edit_tmod_account(interaction, self.guild_id, self.requester_id)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=_tmod_account_character_embed(self.character),
            view=TModAccountCharacterDetailView(self.guild_id, self.requester_id, self.character),
        )


class TModAccountCharacterDetailView(ProfileBaseView):
    def __init__(self, guild_id: int, requester_id: int, character: Any) -> None:
        super().__init__(requester_id)
        self.guild_id = int(guild_id)
        self.character = character

    @discord.ui.button(label="Изменить", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            TModAccountCharacterModal(self.guild_id, self.requester_id, self.character)
        )

    @discord.ui.button(label="Удалить", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(
            title="Удалить персонажа?",
            description=f"**{self.character.nickname}** · `{self.character.static_id}` будет удалён.",
            color=0xED4245,
        )
        await interaction.response.edit_message(
            embed=embed,
            view=TModAccountDeleteConfirmView(self.guild_id, self.requester_id, self.character),
        )

    @discord.ui.button(label="К списку", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        characters = await asyncio.to_thread(
            storage.list_profile_characters,
            self.guild_id,
            self.requester_id,
        )
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Персонажи T-Mod Account",
                description="Выберите запись, чтобы изменить или удалить её.",
                color=0x5865F2,
            ),
            view=TModAccountCharactersView(self.guild_id, self.requester_id, characters),
        )


class TModBugReportModal(ProfileModal, title="Баг-репорт T-Mod"):
    service = discord.ui.TextInput(
        label="Где возникла проблема",
        placeholder="Например: Atlas, Reactor, Consensus, Discord",
        min_length=2,
        max_length=80,
    )
    summary = discord.ui.TextInput(
        label="Кратко",
        placeholder="Что именно не работает?",
        min_length=5,
        max_length=120,
    )
    details = discord.ui.TextInput(
        label="Что произошло",
        placeholder="Ожидание и результат — без PIN, токенов и личных данных",
        style=discord.TextStyle.paragraph,
        min_length=15,
        max_length=1800,
    )
    steps = discord.ui.TextInput(
        label="Как повторить — если известно",
        placeholder="Последовательность действий, устройство или браузер",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=800,
    )

    def __init__(self, guild_id: int, requester_id: int) -> None:
        super().__init__(timeout=600)
        self.guild_id = int(guild_id)
        self.requester_id = int(requester_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Этот баг-репорт открыт для другого пользователя.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.client.get_guild(self.guild_id)
        if guild is None:
            await interaction.edit_original_response(
                content="Сервер Товарищества сейчас недоступен. Попробуйте ещё раз позже."
            )
            return
        ticket_id = (
            datetime.now(timezone.utc).strftime("%y%m%d")
            + "-"
            + secrets.token_hex(3).upper()
        )
        service = str(self.service.value).strip()
        summary = str(self.summary.value).strip()
        details = str(self.details.value).strip()
        steps = str(self.steps.value).strip()
        report = (
            f"Ticket: {ticket_id}\n"
            f"Service: {service}\n"
            f"Summary: {summary}\n"
            f"Details: {details}\n"
            f"Steps: {steps or 'Не указаны'}"
        )
        await capture_runtime_event(
            title=f"Пользовательский баг-репорт: {summary}",
            details=report,
            component=f"user-report.{service.lower()[:48] or 'tmod'}",
            level="error",
            fingerprint_hint=f"manual-bug:{ticket_id}",
        )
        channel = await resolve_registered_channel(guild, "tech_log")
        if channel is None or not callable(getattr(channel, "send", None)):
            await interaction.edit_original_response(
                content=(
                    f"Тикет `{ticket_id}` сохранён, но технический канал временно "
                    "недоступен. Повторно отправлять отчёт не нужно."
                )
            )
            return
        embed = discord.Embed(
            title=f"🪲 Баг-репорт · {ticket_id}",
            description=details[:3900],
            color=0xF0B232,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Сервис", value=service[:1024], inline=True)
        embed.add_field(
            name="Автор",
            value=f"{interaction.user.mention}\n`{interaction.user.id}`",
            inline=True,
        )
        embed.add_field(name="Кратко", value=summary[:1024], inline=False)
        embed.add_field(
            name="Как повторить",
            value=(steps or "Не указано")[:1024],
            inline=False,
        )
        embed.set_footer(text="T-Mod Technologies · пользовательский тикет")
        message = await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        create_thread = getattr(message, "create_thread", None)
        if callable(create_thread):
            try:
                thread = await create_thread(
                    name=f"bug-{ticket_id.lower()} · {summary[:55]}"
                )
                await thread.send(
                    "Тикет открыт. Здесь можно фиксировать диагностику, решение и выпуск исправления.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.DiscordException:
                pass
        await interaction.edit_original_response(
            content=(
                f"Готово — тикет `{ticket_id}` создан и передан Технологиям "
                "Товарищества. Не отправляйте PIN, токены и пароли в дополнениях."
            )
        )


class TModAccountView(ProfileBaseView):
    def __init__(
        self,
        guild_id: int,
        requester_id: int,
        characters: list[Any],
        credential: Any | None,
        *,
        fellowship_member: bool = False,
        onboarding_required: bool = False,
    ) -> None:
        super().__init__(requester_id)
        self.guild_id = int(guild_id)
        self.characters = characters
        self.credential = credential
        self.add_character.disabled = len(characters) >= storage.PROFILE_MAX_CHARACTERS
        self.manage_characters.disabled = not bool(characters)
        self.web_access.disabled = False
        if fellowship_member:
            from modules.consensus_web import consensus_web_entry_url

            self.add_item(
                discord.ui.Button(
                    label=(
                        "Пройти веб-онбординг"
                        if onboarding_required
                        else "Открыть Реактор"
                    ),
                    emoji="⚛️",
                    style=discord.ButtonStyle.link,
                    url=consensus_web_entry_url(
                        guild_id=self.guild_id,
                        user_id=self.requester_id,
                        destination="/reactor",
                    ),
                    row=1,
                )
            )

    @discord.ui.button(label="Добавить персонажа", emoji="➕", style=discord.ButtonStyle.primary)
    async def add_character(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(TModAccountCharacterModal(self.guild_id, self.requester_id))

    @discord.ui.button(label="Персонажи", emoji="🎭", style=discord.ButtonStyle.secondary)
    async def manage_characters(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Персонажи T-Mod Account",
                description="Выберите запись, чтобы изменить или удалить её.",
                color=0x5865F2,
            ),
            view=TModAccountCharactersView(self.guild_id, self.requester_id, self.characters),
        )

    @discord.ui.button(label="Логин и PIN", emoji="🔑", style=discord.ButtonStyle.secondary)
    async def web_access(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            TModAccountCredentialModal(self.guild_id, self.requester_id, self.credential)
        )

    @discord.ui.button(label="Баг-репорт", emoji="🪲", style=discord.ButtonStyle.secondary, row=1)
    async def bug_report(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            TModBugReportModal(self.guild_id, self.requester_id)
        )


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


class MemberDirectoryModal(ProfileModal, title="Карточка участника"):
    biography = discord.ui.TextInput(
        label="Кто вы",
        placeholder="Коротко представьтесь",
        style=discord.TextStyle.paragraph,
        min_length=3,
        max_length=storage.PROFILE_BIOGRAPHY_MAX_LENGTH,
    )
    contribution = discord.ui.TextInput(
        label="Чем занимаетесь в Товариществе",
        placeholder="Роль, проекты и текущая деятельность",
        style=discord.TextStyle.paragraph,
        min_length=3,
        max_length=storage.PROFILE_CONTRIBUTION_MAX_LENGTH,
    )
    responsibilities = discord.ui.TextInput(
        label="Ответственность и интересы",
        placeholder="За что отвечаете или чем хотите заниматься",
        style=discord.TextStyle.paragraph,
        min_length=3,
        max_length=storage.PROFILE_RESPONSIBILITIES_MAX_LENGTH,
    )
    membership_since = discord.ui.TextInput(
        label="В Товариществе с",
        placeholder="ГГГГ-ММ-ДД",
        min_length=10,
        max_length=10,
    )

    def __init__(
        self,
        requester_id: int,
        member: discord.Member,
        profile: Any | None,
    ) -> None:
        super().__init__(timeout=600)
        self.requester_id = int(requester_id)
        self.member = member
        self.biography.default = str(getattr(profile, "biography", None) or "")
        self.contribution.default = str(getattr(profile, "contribution", None) or "")
        self.responsibilities.default = str(
            getattr(profile, "responsibilities", None) or ""
        )
        joined_at = getattr(member, "joined_at", None)
        self.membership_since.default = str(
            getattr(profile, "membership_since", None)
            or (joined_at.date().isoformat() if joined_at else "")
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Нельзя изменить чужую карточку.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        try:
            await asyncio.to_thread(
                storage.update_member_directory,
                self.member.guild.id,
                self.member.id,
                biography=str(self.biography.value),
                contribution=str(self.contribution.value),
                responsibilities=str(self.responsibilities.value),
                membership_since=str(self.membership_since.value),
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
                emoji=_safe_component_emoji(emoji),
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
                emoji=_safe_component_emoji(emoji),
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
            return_to="privacy",
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
                emoji=_safe_component_emoji(emoji),
                description=f"Цвет карточки: {label.lower()}",
                default=key == current,
            )
            for key, (emoji, label, _) in PROFILE_THEME_INFO.items()
        ]
        super().__init__(placeholder="Оформление карточки", options=options, row=0)

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
        super().__init__(placeholder="Основной персонаж", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_id = None if self.values[0] == "none" else int(self.values[0])
        await _save_profile_preferences(
            interaction,
            self.requester_id,
            self.member,
            primary_character_id=selected_id,
        )


class ProfilePreferenceToggle(discord.ui.Button):
    def __init__(
        self,
        requester_id: int,
        member: discord.Member,
        *,
        field: str,
        label: str,
        current: bool,
        return_to: str,
        row: int,
        emoji: str,
    ) -> None:
        super().__init__(
            label=label,
            emoji=_safe_component_emoji(emoji),
            style=discord.ButtonStyle.success if current else discord.ButtonStyle.secondary,
            row=row,
        )
        self.requester_id = int(requester_id)
        self.member = member
        self.field = field
        self.current = bool(current)
        self.return_to = return_to

    async def callback(self, interaction: discord.Interaction) -> None:
        await _save_profile_preferences(
            interaction,
            self.requester_id,
            self.member,
            return_to=self.return_to,
            **{self.field: not self.current},
        )


class ProfilePrivacyView(ProfileBaseView):
    def __init__(self, requester_id: int, member: discord.Member, profile: Any | None) -> None:
        super().__init__(requester_id)
        self.member = member
        visibility = str(getattr(profile, "visibility", None) or "members")
        self.add_item(ProfileVisibilitySelect(requester_id, member, visibility))
        for field, label, emoji, row in (
            ("show_availability", "Доступность", "🟢", 1),
            ("show_position", "Положение", "🏛️", 1),
            ("show_characters", "Персонажи", "🎭", 1),
            ("show_join_date", "Дата вступления", "📅", 2),
            ("show_activity", "Активность", "📊", 2),
            ("show_directory", "Справочник", "📇", 2),
        ):
            self.add_item(
                ProfilePreferenceToggle(
                    requester_id,
                    member,
                    field=field,
                    label=label,
                    current=bool(getattr(profile, field, True)),
                    return_to="privacy",
                    row=row,
                    emoji=emoji,
                )
            )

    @discord.ui.button(label="Назад", emoji="↩️", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_settings(interaction, self.requester_id, self.member)


class ProfileNotificationsView(ProfileBaseView):
    def __init__(self, requester_id: int, member: discord.Member, profile: Any | None) -> None:
        super().__init__(requester_id)
        self.member = member
        self.add_item(
            ProfilePreferenceToggle(
                requester_id,
                member,
                field="dm_notifications",
                label="Все уведомления",
                current=bool(getattr(profile, "dm_notifications", True)),
                return_to="notifications",
                row=0,
                emoji="🔔",
            )
        )
        for field, (emoji, label) in PROFILE_DM_INFO.items():
            self.add_item(
                ProfilePreferenceToggle(
                    requester_id,
                    member,
                    field=field,
                    label=label,
                    current=bool(getattr(profile, field, True)),
                    return_to="notifications",
                    row=1,
                    emoji=emoji,
                )
            )

    @discord.ui.button(label="Назад", emoji="↩️", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_settings(interaction, self.requester_id, self.member)


def parse_profile_clock(value: str) -> int:
    raw = str(value or "").strip()
    parts = raw.split(":")
    if len(parts) != 2 or not all(part.isascii() and part.isdigit() for part in parts):
        raise ValueError("profile_quiet_hours_invalid")
    hour, minute = (int(part) for part in parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("profile_quiet_hours_invalid")
    return hour * 60 + minute


class QuietHoursModal(ProfileModal, title="Свои тихие часы"):
    start = discord.ui.TextInput(label="Начало", placeholder="23:00", min_length=4, max_length=5)
    end = discord.ui.TextInput(label="Конец", placeholder="08:00", min_length=4, max_length=5)

    def __init__(
        self,
        requester_id: int,
        member: discord.Member,
        start_minute: int,
        end_minute: int,
    ) -> None:
        super().__init__(timeout=300)
        self.requester_id = int(requester_id)
        self.member = member
        self.start.default = f"{start_minute // 60:02d}:{start_minute % 60:02d}"
        self.end.default = f"{end_minute // 60:02d}:{end_minute % 60:02d}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Нельзя изменить чужие настройки.", ephemeral=True)
            return
        try:
            start_minute = parse_profile_clock(str(self.start.value))
            end_minute = parse_profile_clock(str(self.end.value))
            if start_minute == end_minute:
                raise ValueError("profile_quiet_hours_invalid")
        except ValueError as exc:
            await interaction.response.send_message(
                PROFILE_ERROR_MESSAGES.get(str(exc), "Укажите время в формате ЧЧ:ММ."),
                ephemeral=True,
            )
            return
        await _save_profile_preferences(
            interaction,
            self.requester_id,
            self.member,
            return_to="quiet_hours",
            quiet_hours_enabled=True,
            quiet_start_minute=start_minute,
            quiet_end_minute=end_minute,
        )


class QuietHoursPresetSelect(discord.ui.Select):
    PRESETS = {
        "off": (False, 0, 480),
        "23-07": (True, 23 * 60, 7 * 60),
        "00-08": (True, 0, 8 * 60),
        "02-09": (True, 2 * 60, 9 * 60),
    }

    def __init__(
        self,
        requester_id: int,
        member: discord.Member,
        profile: Any | None,
    ) -> None:
        self.requester_id = int(requester_id)
        self.member = member
        enabled = bool(getattr(profile, "quiet_hours_enabled", False))
        start = int(getattr(profile, "quiet_start_minute", 0) or 0)
        end = int(getattr(profile, "quiet_end_minute", 480) or 0)
        current = next(
            (
                key
                for key, values in self.PRESETS.items()
                if values == (enabled, start, end)
            ),
            None,
        )
        options = [
            discord.SelectOption(label="Выключить", value="off", emoji="☀️", default=current == "off"),
            discord.SelectOption(label="23:00–07:00", value="23-07", emoji="🌙", default=current == "23-07"),
            discord.SelectOption(label="00:00–08:00", value="00-08", emoji="🌙", default=current == "00-08"),
            discord.SelectOption(label="02:00–09:00", value="02-09", emoji="🌙", default=current == "02-09"),
        ]
        super().__init__(placeholder="Выберите готовый режим", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        enabled, start, end = self.PRESETS[self.values[0]]
        await _save_profile_preferences(
            interaction,
            self.requester_id,
            self.member,
            return_to="quiet_hours",
            quiet_hours_enabled=enabled,
            quiet_start_minute=start,
            quiet_end_minute=end,
        )


class ProfileQuietHoursView(ProfileBaseView):
    def __init__(self, requester_id: int, member: discord.Member, profile: Any | None) -> None:
        super().__init__(requester_id)
        self.member = member
        self.start_minute = int(getattr(profile, "quiet_start_minute", 0) or 0)
        self.end_minute = int(getattr(profile, "quiet_end_minute", 480) or 0)
        self.add_item(QuietHoursPresetSelect(requester_id, member, profile))

    @discord.ui.button(label="Своё время", emoji="✍️", style=discord.ButtonStyle.primary, row=1)
    async def custom(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            QuietHoursModal(
                self.requester_id,
                self.member,
                self.start_minute,
                self.end_minute,
            )
        )

    @discord.ui.button(label="Назад", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_settings(interaction, self.requester_id, self.member)


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
        theme = str(getattr(profile, "theme", None) or "indigo")
        primary_character_id = getattr(profile, "primary_character_id", None)
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

    @discord.ui.button(label="Приватность", emoji="🔐", style=discord.ButtonStyle.secondary, row=2)
    async def privacy(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_privacy(interaction, self.requester_id, self.member)

    @discord.ui.button(label="Уведомления", emoji="🔔", style=discord.ButtonStyle.secondary, row=2)
    async def notifications(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_notifications(interaction, self.requester_id, self.member)

    @discord.ui.button(label="Тихие часы", emoji="🌙", style=discord.ButtonStyle.secondary, row=2)
    async def quiet_hours(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_quiet_hours(interaction, self.requester_id, self.member)

    @discord.ui.button(label="Микрофон", emoji="🎙️", style=discord.ButtonStyle.secondary, row=2)
    async def microphone(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_microphone(interaction, self.requester_id, self.member)

    @discord.ui.button(label="Веб-доступ", emoji="🔑", style=discord.ButtonStyle.secondary, row=3)
    async def web_access(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_web_access(interaction, self.requester_id, self.member)

    @discord.ui.button(label="По умолчанию", emoji="♻️", style=discord.ButtonStyle.secondary, row=3)
    async def reset(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _save_profile_preferences(
            interaction,
            self.requester_id,
            self.member,
            visibility="members",
            show_activity=True,
            show_availability=True,
            show_position=True,
            show_characters=True,
            show_join_date=True,
            show_directory=True,
            theme="indigo",
            primary_character_id=(self.characters[0].id if self.characters else None),
            dm_notifications=True,
            dm_market=True,
            dm_craft=True,
            dm_consensus=True,
            dm_finance=True,
            dm_system=True,
            quiet_hours_enabled=False,
            quiet_start_minute=0,
            quiet_end_minute=480,
        )

    @discord.ui.button(label="Назад", emoji="↩️", style=discord.ButtonStyle.secondary, row=3)
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
        is_public = bool(getattr(character, "is_public", True))
        self.visibility.label = "Скрыть" if is_public else "Показывать"
        self.visibility.emoji = _safe_component_emoji("🔒" if is_public else "🌐")

    @discord.ui.button(label="Изменить", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            CharacterModal(self.requester_id, self.member, character=self.character)
        )

    @discord.ui.button(label="Скрыть", emoji="🔒", style=discord.ButtonStyle.secondary)
    async def visibility(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        current = bool(getattr(self.character, "is_public", True))
        await interaction.response.defer()
        try:
            character = await asyncio.to_thread(
                storage.set_profile_character_visibility,
                self.member.guild.id,
                self.member.id,
                self.character.id,
                is_public=not current,
            )
        except ValueError as exc:
            await _send_profile_error(interaction, exc)
            return
        await interaction.edit_original_response(
            embed=character_embed(self.member, character),
            view=CharacterDetailView(self.requester_id, self.member, character),
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
            self.remove_item(self.directory)

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

    @discord.ui.button(label="О себе", emoji="📇", style=discord.ButtonStyle.secondary, row=1)
    async def directory(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        profile = await asyncio.to_thread(
            storage.get_member_profile,
            self.member.guild.id,
            self.member.id,
        )
        await interaction.response.send_modal(
            MemberDirectoryModal(self.requester_id, self.member, profile)
        )

    @discord.ui.button(label="Обновить", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _edit_profile_home(interaction, self.requester_id, self.member)


def member_onboarding_embed(*, reminder: bool = False) -> discord.Embed:
    embed = discord.Embed(
        title=(
            "Завершите активацию личного Реактора"
            if reminder
            else "Добро пожаловать в Товарищество"
        ),
        description=(
            "Ваш личный Реактор уже открыт, но активация ещё не завершена. "
            "Заполните короткий профиль — после этого напоминания остановятся."
            if reminder
            else (
                "Для вас открыт персональный веб-онбординг. Он создаст первый "
                "профиль персонажа, зафиксирует ваше имя для общения и соберёт "
                "карточку **«Мой мандат»** в Реакторе."
            )
        ),
        color=PROFILE_COLOR,
    )
    embed.add_field(
        name="Что потребуется",
        value=(
            "Имя для обращения, первый персонаж, статик и несколько строк о вашей "
            "работе в Товариществе. Обычно это занимает меньше трёх минут."
        ),
        inline=False,
    )
    embed.add_field(
        name="Единый ник Discord",
        value=(
            "После завершения T-Mod установит ник в формате "
            "`S. Goodman | 263345 | Иван`. Администраторов смена ника не затрагивает."
        ),
        inline=False,
    )
    embed.set_footer(
        text=(
            "Напоминание приходит раз в сутки и прекратится сразу после активации."
            if reminder
            else "Если ссылка устареет, откройте /account и выберите веб-онбординг."
        )
    )
    return embed


async def send_member_onboarding_reminder(
    member: discord.Member,
    *,
    reminder: bool,
    attempted_at: str | None = None,
) -> tuple[bool, Exception | None]:
    """Send a fresh signed onboarding URL and durably throttle the next attempt."""

    error: Exception | None = None
    delivered = False
    try:
        from modules.consensus_web import consensus_web_entry_url

        onboarding_url = consensus_web_entry_url(
            guild_id=member.guild.id,
            user_id=member.id,
            destination="/reactor",
        )
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Активировать личный Реактор",
                style=discord.ButtonStyle.link,
                url=onboarding_url,
                emoji="⚛️",
            )
        )
        await asyncio.wait_for(
            member.send(embed=member_onboarding_embed(reminder=reminder), view=view),
            timeout=12,
        )
        delivered = True
    except Exception as exc:  # Delivery and signed-link failures share one durable cadence.
        error = exc
    await asyncio.to_thread(
        storage.record_member_onboarding_reminder,
        member.guild.id,
        member.id,
        attempted_at=attempted_at,
        delivered=delivered,
        error=(f"{type(error).__name__}: {error}" if error is not None else None),
    )
    return delivered, error


async def deliver_due_member_onboarding_reminders(
    bot: commands.Bot,
    guild: discord.Guild,
    *,
    now: datetime | None = None,
) -> int:
    """Deliver at most one activation reminder per member per 24 hours."""

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    attempted_at = current.isoformat()
    due_before = (current - timedelta(seconds=MEMBER_ONBOARDING_REMINDER_SECONDS)).isoformat()
    due = await asyncio.to_thread(
        storage.list_due_member_onboarding_reminders,
        guild.id,
        due_before=due_before,
    )
    delivered_count = 0
    for item in due:
        user_id = int(item["user_id"])
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await asyncio.to_thread(
                    storage.record_member_onboarding_reminder,
                    guild.id,
                    user_id,
                    attempted_at=attempted_at,
                    delivered=False,
                    error="member_unavailable",
                )
                continue
        role_ids = {int(role.id) for role in getattr(member, "roles", [])}
        if member.bot or TVRS_SENATOR_ROLE_ID not in role_ids:
            await asyncio.to_thread(
                storage.record_member_onboarding_reminder,
                guild.id,
                user_id,
                attempted_at=attempted_at,
                delivered=False,
                error="senator_role_missing",
            )
            continue
        delivered, error = await send_member_onboarding_reminder(
            member,
            reminder=True,
            attempted_at=attempted_at,
        )
        if delivered:
            delivered_count += 1
        elif error is not None:
            await log_technical_event(
                bot,
                guild,
                title="Не доставлено напоминание об активации Реактора",
                details=(
                    f"Участник: <@{user_id}> (`{user_id}`)\n"
                    f"Следующая попытка будет не раньше чем через сутки.\n"
                    f"Ошибка: `{type(error).__name__}: {str(error)[:500]}`"
                ),
                dedupe_key=f"profile-onboarding-reminder:{user_id}",
                cooldown_seconds=MEMBER_ONBOARDING_REMINDER_SECONDS,
            )
    return delivered_count


def setup_profile(
    bot: commands.Bot,
    remember_command_activity: Callable[[discord.Interaction, str, str], None] | None = None,
) -> None:
    reminder_task: asyncio.Task[None] | None = None

    async def member_onboarding_reminder_worker() -> None:
        await bot.wait_until_ready()
        while not bot.is_closed():
            for guild in list(bot.guilds):
                try:
                    await deliver_due_member_onboarding_reminders(bot, guild)
                except Exception as exc:
                    await log_technical_event(
                        bot,
                        guild,
                        title="Ошибка цикла напоминаний об активации Реактора",
                        details=f"`{type(exc).__name__}: {str(exc)[:800]}`",
                        dedupe_key=f"profile-onboarding-reminder-worker:{guild.id}",
                        cooldown_seconds=MEMBER_ONBOARDING_WORKER_SECONDS,
                    )
            await asyncio.sleep(MEMBER_ONBOARDING_WORKER_SECONDS)

    @bot.listen("on_ready")
    async def profile_onboarding_reminder_ready() -> None:
        nonlocal reminder_task
        if reminder_task is None or reminder_task.done():
            reminder_task = asyncio.create_task(
                member_onboarding_reminder_worker(),
                name="member-onboarding-reminders",
            )

    def account_guild_id(interaction: discord.Interaction) -> int | None:
        configured = str(os.getenv("DISCORD_GUILD_ID", "")).strip()
        if configured.isdigit():
            return int(configured)
        if interaction.guild_id:
            return int(interaction.guild_id)
        return int(bot.guilds[0].id) if bot.guilds else None

    async def resolve_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(int(user_id))
        if member is not None:
            return member
        try:
            return await guild.fetch_member(int(user_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    @bot.tree.command(name="account", description="Открыть единый аккаунт и персонажей T-Mod")
    @app_commands.describe(user="Участник, чей аккаунт нужно посмотреть")
    async def account(
        interaction: discord.Interaction,
        user: discord.User | None = None,
    ) -> None:
        selected_guild_id = account_guild_id(interaction)
        if selected_guild_id is None:
            await interaction.response.send_message("T-Mod ещё подключается. Повторите через минуту.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = bot.get_guild(int(selected_guild_id))
        requester = (
            await resolve_member(guild, int(interaction.user.id))
            if guild is not None
            else None
        )
        if requester is None:
            if user is not None and int(user.id) != int(interaction.user.id):
                await interaction.edit_original_response(
                    content="Нулевой аккаунт может управлять только собственными данными.",
                    embed=None,
                    view=None,
                )
                return
            await _edit_tmod_account(interaction, selected_guild_id, int(interaction.user.id))
            return

        target = requester
        if user is not None and int(user.id) != int(requester.id):
            target = await resolve_member(guild, int(user.id)) if guild is not None else None
            if target is None:
                await interaction.edit_original_response(
                    content="Этот пользователь не является участником Товарищества.",
                    embed=None,
                    view=None,
                )
                return
        if remember_command_activity is not None:
            details = "/account" if target.id == requester.id else f"/account user:{target.id}"
            remember_command_activity(interaction, "command_profile", details)
        profile_data, characters, activity = await _load_profile(target)
        editable = target.id == requester.id
        await interaction.edit_original_response(
            embed=profile_embed(target, profile_data, characters, activity, editable=editable),
            view=ProfileHomeView(requester.id, target, characters, editable=editable),
            content=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(
        name="reset",
        description="Сбросить заблокированный логин и PIN веб-панели T-Mod",
    )
    async def reset_web_access(interaction: discord.Interaction) -> None:
        selected_guild_id = account_guild_id(interaction)
        if selected_guild_id is None:
            await interaction.response.send_message(
                "T-Mod ещё подключается. Повторите через минуту.",
                ephemeral=True,
            )
            return
        credential = await asyncio.to_thread(
            web_auth_storage.get_web_credential,
            selected_guild_id,
            interaction.user.id,
        )
        await interaction.response.send_modal(
            TModAccountCredentialModal(selected_guild_id, interaction.user.id, credential)
        )

    @bot.listen("on_member_update")
    async def profile_member_admission_listener(
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        before_roles = {int(role.id) for role in before.roles}
        after_roles = {int(role.id) for role in after.roles}
        if (
            TVRS_SENATOR_ROLE_ID <= 0
            or TVRS_SENATOR_ROLE_ID in before_roles
            or TVRS_SENATOR_ROLE_ID not in after_roles
            or after.bot
        ):
            return
        _, newly_required = await asyncio.to_thread(
            storage.require_member_directory,
            after.guild.id,
            after.id,
            prompted=True,
        )
        if not newly_required:
            return
        delivered, exc = await send_member_onboarding_reminder(after, reminder=False)
        if not delivered and exc is not None:
            await log_technical_event(
                bot,
                after.guild,
                title="Не доставлено приглашение заполнить профиль",
                details=(
                    f"Участник: <@{after.id}> (`{after.id}`)\n"
                    f"Карточка всё равно отмечена обязательной.\n"
                    f"Ошибка: `{type(exc).__name__}: {str(exc)[:500]}`"
                ),
                dedupe_key=f"profile-onboarding-dm:{after.id}",
                cooldown_seconds=3600,
            )


__all__ = [
    "CharacterDeleteView",
    "CharacterDetailView",
    "CharacterManagerView",
    "CharacterModal",
    "ProfileBaseView",
    "ProfileHomeView",
    "ProfileMicrophoneView",
    "MemberDirectoryModal",
    "ProfileSettingsView",
    "ProfileStatusView",
    "ProfileWebAccessView",
    "StatusNoteModal",
    "TModBugReportModal",
    "WebAccessModal",
    "TModAccountView",
    "character_embed",
    "character_manager_embed",
    "member_position_text",
    "profile_embed",
    "profile_microphone_embed",
    "profile_settings_embed",
    "profile_web_access_embed",
    "member_onboarding_embed",
    "send_member_onboarding_reminder",
    "deliver_due_member_onboarding_reminders",
    "setup_profile",
]
