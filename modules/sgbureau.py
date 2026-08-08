import asyncio
import os
import re
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from persistence import bureau_context as storage
from localization import safe_command_description, safe_command_name, t
from modules.sgcontract import SGLContractModal


def env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def env_color(name: str, default: str = "0xD9D9D9") -> int:
    raw = os.getenv(name, default).strip()
    try:
        return int(raw, 16) if raw.lower().startswith("0x") else int(raw)
    except (TypeError, ValueError):
        return 0xD9D9D9


SGBUREAU_CATEGORY_ID = env_int("SGBUREAU_CATEGORY_ID", 1500836076120576000)
SGBUREAU_COMMAND_CHANNEL_ID = env_int("SGBUREAU_COMMAND_CHANNEL_ID", 1500837640067485717)
SGBUREAU_STAFF_ROLE_ID = env_int("SGBUREAU_STAFF_ROLE_ID", 1500488424191295518)
SGBUREAU_CLIENT_ROLE_ID = env_int("SGBUREAU_CLIENT_ROLE_ID", 1500488373666840587)
SGBUREAU_NEWS_CHANNEL_ID = env_int("SGBUREAU_NEWS_CHANNEL_ID", 1500838788082044978)
SGBUREAU_GLOBAL_NEWS_CHANNEL_ID = env_int("SGBUREAU_GLOBAL_NEWS_CHANNEL_ID", 1492471269365383168)
SGBUREAU_START_CHANNEL_ID = env_int("SGBUREAU_START_CHANNEL_ID", 1500838151219052574)
SGBUREAU_COSTS_CHANNEL_ID = env_int("SGBUREAU_COSTS_CHANNEL_ID", 1500838270613852331)
SGBUREAU_PORTFOLIO_CHANNEL_ID = env_int("SGBUREAU_PORTFOLIO_CHANNEL_ID", 1500838320077406311)
SGBUREAU_ARCHIVE_CATEGORY_ID = env_int("SGBUREAU_ARCHIVE_CATEGORY_ID", 1505657602774798447)
SGBUREAU_ARCHIVE_AFTER_SECONDS = env_int("SGBUREAU_ARCHIVE_AFTER_SECONDS", 86400)
SGBUREAU_REORDER_DEBOUNCE_SECONDS = env_int("SGBUREAU_REORDER_DEBOUNCE_SECONDS", 20)
SGBUREAU_DEFAULT_LAWYER_ID = env_int("SGBUREAU_DEFAULT_LAWYER_ID", 902235631952998410)
SGBUREAU_EMBED_COLOR = env_color("SGBUREAU_EMBED_COLOR", "0xD9D9D9")
SGBUREAU_LAWYER_BANK_ACCOUNT = os.getenv("SGBUREAU_LAWYER_BANK_ACCOUNT", "215436").strip() or "215436"
SGBUREAU_COURT_FEE_BANK_ACCOUNT = os.getenv("SGBUREAU_COURT_FEE_BANK_ACCOUNT", "72476").strip() or "72476"
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "Europe/Riga"))

SG_ADDNEWS_COMMAND_NAME = safe_command_name("sgbureau.commands.sg_addnews_name", "sg_addnews")
SG_ADDNEWS_COMMAND_DESCRIPTION = safe_command_description("sgbureau.commands.sg_addnews_description", "Add SGL Bureau news")
SG_INITIATE_COMMAND_NAME = safe_command_name("sgbureau.commands.sg_initiate_name", "sg_initiate")
SG_INITIATE_COMMAND_DESCRIPTION = safe_command_description("sgbureau.commands.sg_initiate_description", "Initiate SGL Bureau channels")
SG_NEWCASE_COMMAND_NAME = safe_command_name("sgbureau.commands.sg_newcase_name", "sg_newcase")
SG_NEWCASE_COMMAND_DESCRIPTION = safe_command_description("sgbureau.commands.sg_newcase_description", "Create SGL Bureau case")
SG_CLINK_COMMAND_NAME = safe_command_name("sgbureau.commands.sg_clink_name", "sg_clink")
SG_CLINK_COMMAND_DESCRIPTION = safe_command_description("sgbureau.commands.sg_clink_description", "Attach case statement link")
SG_CLOSECASE_COMMAND_NAME = safe_command_name("sgbureau.commands.sg_closecase_name", "sg_closecase")
SG_CLOSECASE_COMMAND_DESCRIPTION = safe_command_description("sgbureau.commands.sg_closecase_description", "Close SGL Bureau case")
SG_COMMAND_NAME = safe_command_name("sgbureau.commands.sg_name", "sg")
SG_COMMAND_DESCRIPTION = safe_command_description("sgbureau.commands.sg_description", "SGL Bureau control panel")
SG_REGISTRY_COMMAND_NAME = safe_command_name("sgbureau.commands.sg_registry_name", "sg_registry")
SG_REGISTRY_COMMAND_DESCRIPTION = safe_command_description("sgbureau.commands.sg_registry_description", "SGL registry search")
SG_LAWYERADD_COMMAND_NAME = safe_command_name("sgbureau.commands.sg_lawyeradd_name", "sg_lawyeradd")
SG_LAWYERADD_COMMAND_DESCRIPTION = safe_command_description("sgbureau.commands.sg_lawyeradd_description", "Add or update lawyer registry profile")
SG_ADMIN_COMMAND_NAME = safe_command_name("sgbureau.commands.sg_admin_name", "sg_admin")
SG_ADMIN_COMMAND_DESCRIPTION = safe_command_description("sgbureau.commands.sg_admin_description", "Manual SGL database editor")


YES_VALUES = {"да", "yes", "y", "1", "+", "true", "д", "ага", "oui"}
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(LOCAL_TZ)


def format_full_datetime(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M:%S %Z")


def is_bureau_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(role.id == SGBUREAU_STAFF_ROLE_ID for role in member.roles)


def is_yes(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in YES_VALUES


def safe_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value if value else None


async def _send_admin_modal_reply_safely(
    interaction: discord.Interaction,
    content: str,
) -> bool:
    """Best-effort reply after an admin operation may have deleted its channel."""

    try:
        await interaction.followup.send(content, ephemeral=True)
    except discord.HTTPException:
        # The database operation has already completed and the interaction may
        # no longer have a channel/message to receive its ephemeral reply.
        return False
    return True


def parse_amount(value: str) -> int | None:
    cleaned = re.sub(r"[^0-9]", "", str(value or ""))
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def format_money(amount: int | None) -> str:
    if amount is None:
        return t("common.no_data")
    return f"${int(amount):,}".replace(",", ".")


COURT_TYPES = {
    "d": {"code": "d", "suffix": "DC", "label": "Окружная юрисдикция", "fee": 35000, "aliases": {"d", "dc", "д", "окружной", "окружная", "окр", "округ", "district"}},
    "s": {"code": "s", "suffix": "SC", "label": "Верховная юрисдикция", "fee": 45000, "aliases": {"s", "sc", "с", "в", "верховный", "верховная", "вс", "supreme"}},
    "k": {"code": "k", "suffix": "AC", "label": "Апелляционная / кассационная жалоба", "fee": 45000, "aliases": {"k", "к", "a", "а", "ап", "апелляция", "апелляционная", "кассация", "кассационная", "appeal", "cassation"}},
    "o": {"code": "o", "suffix": "SCQ", "label": "Обращение в Верховный суд", "fee": 25000, "aliases": {"o", "о", "обращение", "обращ", "запрос", "верховный суд обращение"}},
    "e": {"code": "e", "suffix": "OT", "label": "Иное", "fee": None, "aliases": {"e", "е", "иное", "другое", "other", "custom"}},
}


def resolve_court_type(value: str | None) -> dict | None:
    raw = str(value or "").strip().lower()
    raw = re.sub(r"\s+", " ", raw)
    for item in COURT_TYPES.values():
        if raw in item["aliases"]:
            return item
    # fuzzy Russian shortcuts
    if "окруж" in raw:
        return COURT_TYPES["d"]
    if "верх" in raw and "обращ" not in raw:
        return COURT_TYPES["s"]
    if "апел" in raw or "кас" in raw:
        return COURT_TYPES["k"]
    if "обращ" in raw:
        return COURT_TYPES["o"]
    if "ин" in raw or "друг" in raw:
        return COURT_TYPES["e"]
    return None


def format_case_number(case_number: int) -> str:
    return f"{int(case_number):03d}"


def case_has_client_data(case: storage.SGLCase) -> bool:
    return all([
        case.request_type,
        case.client_nick,
        case.static_id,
        case.bank_account,
        case.phone,
        case.passport_url,
    ])


def case_has_situation(case: storage.SGLCase) -> bool:
    return bool((case.situation_text or "").strip())


def case_status_emoji(case: storage.SGLCase) -> str:
    if case.status == "closed":
        return "✋"
    if case.claim_link:
        return "🧘‍♂️"
    if case_has_client_data(case) and case_has_situation(case):
        return "👀"
    return "⏳"


def case_channel_name(case: storage.SGLCase) -> str:
    suffix = ""
    try:
        receipt = storage.get_latest_confirmed_sgl_receipt_for_case(case.id)
        if receipt and receipt.court_suffix:
            suffix = f"-{safe_channel_name(receipt.court_suffix).upper()}"
    except Exception:
        suffix = ""
    return f"{format_case_number(case.case_number)}-{case_status_emoji(case)}{suffix}"


def safe_channel_name(raw: int | str) -> str:
    text = str(raw).strip().lower()
    cleaned = re.sub(r"[^a-z0-9_-]", "-", text)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned[:90] or "case"


def member_label(member: discord.Member | discord.User | None) -> str:
    if member is None:
        return t("common.no_data")
    display = getattr(member, "display_name", None) or getattr(member, "name", None) or str(member)
    user_id = getattr(member, "id", 0)
    return f"{display} ({user_id})"


def case_participant_ids(case: storage.SGLCase) -> set[int]:
    ids = {case.client_id, case.lead_lawyer_id}
    if case.secretary_id:
        ids.add(case.secretary_id)
    return ids


def can_work_with_case(member: discord.Member, case: storage.SGLCase) -> bool:
    if member.guild_permissions.administrator:
        return True
    if member.id in case_participant_ids(case):
        return True
    return any(role.id == SGBUREAU_STAFF_ROLE_ID for role in member.roles)


def can_close_case(member: discord.Member, case: storage.SGLCase) -> bool:
    if member.guild_permissions.administrator:
        return True
    if member.id == case.lead_lawyer_id:
        return True
    if case.secretary_id and member.id == case.secretary_id:
        return True
    return False


async def resolve_text_channel(guild: discord.Guild, bot: commands.Bot, channel_id: int) -> discord.TextChannel | None:
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            fetched = await bot.fetch_channel(channel_id)
            channel = fetched if isinstance(fetched, discord.TextChannel) else None
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            channel = None
    return channel if isinstance(channel, discord.TextChannel) else None


async def resolve_category(guild: discord.Guild, bot: commands.Bot, category_id: int) -> discord.CategoryChannel | None:
    channel = guild.get_channel(category_id)
    if isinstance(channel, discord.CategoryChannel):
        return channel
    try:
        fetched = await bot.fetch_channel(category_id)
        return fetched if isinstance(fetched, discord.CategoryChannel) else None
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def fetch_member_safe(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


def base_embed(title: str, description: str | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=SGBUREAU_EMBED_COLOR, timestamp=now_local())


def build_news_embed(
    *,
    title: str,
    body: str,
    note: str | None,
    image_url: str | None,
    interaction: discord.Interaction,
    created_dt: datetime,
) -> discord.Embed:
    user = interaction.user
    author_display = getattr(user, "display_name", str(user))
    author_id = getattr(user, "id", 0)
    datetime_text = format_full_datetime(created_dt)

    embed = discord.Embed(title=title, description=body, color=SGBUREAU_EMBED_COLOR, timestamp=created_dt)
    if note:
        embed.add_field(name=t("sgbureau.news.note_field_name"), value=note, inline=False)
    if image_url and image_url.lower().startswith(("http://", "https://")):
        embed.set_image(url=image_url)
    embed.set_footer(
        text=t(
            "sgbureau.news.footer",
            bot_name=t("bot.brand_name"),
            datetime=datetime_text,
            author_display=author_display,
            author_id=author_id,
        )
    )
    return embed


def build_initiate_intro_embed() -> discord.Embed:
    embed = base_embed(t("sgbureau.initiate.intro_title"), t("sgbureau.initiate.intro_description"))
    embed.add_field(name=t("sgbureau.initiate.contact_field_name"), value=t("sgbureau.initiate.contact_value"), inline=False)
    embed.set_footer(text=t("sgbureau.initiate.footer"))
    return embed


def build_initiate_costs_embed() -> discord.Embed:
    embed = base_embed(t("sgbureau.initiate.costs_title"), t("sgbureau.initiate.costs_description"))
    embed.add_field(name=t("sgbureau.initiate.recommend_court_title"), value=t("sgbureau.initiate.recommend_court_value"), inline=False)
    embed.add_field(name=t("sgbureau.initiate.recommend_prosecutor_title"), value=t("sgbureau.initiate.recommend_prosecutor_value"), inline=False)
    embed.add_field(name=t("sgbureau.initiate.other_services_title"), value=t("sgbureau.initiate.other_services_value"), inline=False)
    embed.add_field(name=t("sgbureau.initiate.contact_field_name"), value=t("sgbureau.initiate.contact_value"), inline=False)
    embed.set_footer(text=t("sgbureau.initiate.footer"))
    return embed


def build_initiate_costs_message() -> str:
    return t("sgbureau.initiate.costs_plain")


def build_case_greeting_embed(case: storage.SGLCase, client: discord.Member, lawyer: discord.Member, secretary: discord.Member | None) -> discord.Embed:
    secretary_text = secretary.mention if secretary else t("sgbureau.case.no_secretary")
    embed = base_embed(
        t("sgbureau.case.greeting_title", case_number=format_case_number(case.case_number)),
        t("sgbureau.case.greeting_description", case_number=format_case_number(case.case_number), client=client.mention, lawyer=lawyer.mention, secretary=secretary_text),
    )
    embed.add_field(name=t("sgbureau.case.client_field"), value=client.mention, inline=True)
    embed.add_field(name=t("sgbureau.case.lawyer_field"), value=lawyer.mention, inline=True)
    if secretary:
        embed.add_field(name=t("sgbureau.case.secretary_field"), value=secretary.mention, inline=True)
    embed.add_field(name=t("sgbureau.case.next_step_field"), value=t("sgbureau.case.next_step_value"), inline=False)
    embed.set_footer(text=t("sgbureau.case.footer", bot_name=t("bot.brand_name")))
    return embed


def build_client_data_embed(case: storage.SGLCase, actor: discord.Member) -> discord.Embed:
    embed = base_embed(t("sgbureau.case.client_data_title", case_number=format_case_number(case.case_number)))
    embed.add_field(name=t("sgbureau.case.request_type_field"), value=case.request_type or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.case.nickname_field"), value=case.client_nick or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.case.static_field"), value=case.static_id or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.case.bank_field"), value=case.bank_account or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.case.phone_field"), value=case.phone or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.case.passport_field"), value=case.passport_url or t("common.no_data"), inline=False)
    embed.set_footer(text=t("sgbureau.case.filled_by_footer", actor=member_label(actor), datetime=format_full_datetime(now_local())))
    return embed


def build_situation_request_embed(case: storage.SGLCase) -> discord.Embed:
    embed = base_embed(t("sgbureau.case.situation_title", case_number=format_case_number(case.case_number)), t("sgbureau.case.situation_description"))
    embed.add_field(name=t("sgbureau.case.situation_required_field"), value=t("sgbureau.case.situation_required_value"), inline=False)
    embed.set_footer(text=t("sgbureau.case.situation_footer"))
    return embed


def build_situation_embed(case: storage.SGLCase, author: discord.Member, text: str) -> discord.Embed:
    embed = base_embed(t("sgbureau.case.situation_saved_title", case_number=format_case_number(case.case_number)), text[:3900])
    embed.set_footer(text=t("sgbureau.case.situation_saved_footer", author=member_label(author), datetime=format_full_datetime(now_local())))
    return embed



def case_status_label(case: storage.SGLCase) -> str:
    if case.status == "closed":
        return t("sgbureau.sg.status_closed")
    if case.claim_link:
        return t("sgbureau.sg.status_claim_link")
    if case_has_client_data(case) and case_has_situation(case):
        return t("sgbureau.sg.status_ready_no_link")
    return t("sgbureau.sg.status_waiting_data")


def case_short_line(case: storage.SGLCase) -> str:
    channel = f" <#{case.channel_id}>" if case.channel_id else ""
    request_type = case.request_type or t("common.no_data")
    nick = case.client_nick or case.client_display or str(case.client_id)
    return t(
        "sgbureau.sg.case_short_line",
        case_number=format_case_number(case.case_number),
        emoji=case_status_emoji(case),
        status=case_status_label(case),
        request_type=request_type,
        nick=nick,
        channel=channel,
    )




def client_profile_line(profile: storage.ClientProfile) -> str:
    return t("sgbureau.registry.client_line", nick=profile.client_nick, static=profile.static_id, phone=profile.phone, bank=profile.bank_account)


def lawyer_profile_line(profile: storage.LawyerProfile) -> str:
    return t("sgbureau.registry.lawyer_line", nick=profile.lawyer_nick, static=profile.static_id or t("common.no_data"), phone=profile.phone or t("common.no_data"), bank=profile.bank_account or t("common.no_data"))


def build_registry_embed(kind: str, query: str | None, items: list[object]) -> discord.Embed:
    if kind == "lawyer":
        title = t("sgbureau.registry.lawyers_title")
        empty = t("sgbureau.registry.lawyers_empty")
        lines = [lawyer_profile_line(x) for x in items]  # type: ignore[arg-type]
    else:
        title = t("sgbureau.registry.clients_title")
        empty = t("sgbureau.registry.clients_empty")
        lines = [client_profile_line(x) for x in items]  # type: ignore[arg-type]
    embed = base_embed(title, t("sgbureau.registry.description", query=query or t("common.no_data")))
    embed.add_field(name=t("sgbureau.registry.result_field", count=len(items)), value=("\n".join(lines)[:4000] if lines else empty), inline=False)
    embed.set_footer(text=t("sgbureau.registry.footer"))
    return embed


def apply_client_profile_to_case(case: storage.SGLCase, profile: storage.ClientProfile, actor: discord.Member) -> storage.SGLCase | None:
    updated = storage.update_sgl_case_params(
        guild_id=case.guild_id,
        channel_id=case.channel_id or 0,
        request_type=case.request_type or t("sgbureau.case.service_value.other"),
        client_nick=profile.client_nick,
        static_id=profile.static_id,
        bank_account=profile.bank_account,
        phone=profile.phone,
        passport_url=profile.passport_url,
        actor_id=actor.id,
        actor_display=actor.display_name,
    )
    return updated


async def post_case_params_messages(channel: discord.TextChannel, case: storage.SGLCase, actor: discord.Member) -> None:
    await channel.send(embed=build_client_data_embed(case, actor), allowed_mentions=discord.AllowedMentions.none())
    await channel.send(embed=build_situation_request_embed(case), allowed_mentions=discord.AllowedMentions.none())

def build_user_overview_embed(member: discord.Member, cases: list[storage.SGLCase]) -> discord.Embed:
    latest = cases[0] if cases else None
    identity_case = next((case for case in cases if case.client_nick or case.static_id or case.phone or case.bank_account), latest)
    embed = base_embed(
        t("sgbureau.sg.user_title", display=member.display_name),
        t("sgbureau.sg.user_description", mention=member.mention, user_id=member.id),
    )
    embed.add_field(name=t("sgbureau.sg.discord_field"), value=f"{member.mention}\n`{member.id}`", inline=True)
    embed.add_field(name=t("sgbureau.sg.nickname_field"), value=(identity_case.client_nick if identity_case else None) or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.sg.static_field"), value=(identity_case.static_id if identity_case else None) or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.sg.phone_field"), value=(identity_case.phone if identity_case else None) or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.sg.bank_field"), value=(identity_case.bank_account if identity_case else None) or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.sg.case_count_field"), value=str(len(cases)), inline=True)
    profiles = storage.list_client_profiles_for_user(member.guild.id, member.id, limit=5)
    if profiles:
        embed.add_field(name=t("sgbureau.registry.profiles_field"), value="\n".join(client_profile_line(p) for p in profiles)[:1024], inline=False)
    if cases:
        lines = [case_short_line(case) for case in cases[:10]]
        if len(cases) > 10:
            lines.append(t("sgbureau.sg.more_cases", count=len(cases) - 10))
        embed.add_field(name=t("sgbureau.sg.client_cases_field"), value="\n".join(lines)[:1024], inline=False)
    else:
        embed.add_field(name=t("sgbureau.sg.client_cases_field"), value=t("sgbureau.sg.no_cases"), inline=False)
    embed.set_footer(text=t("sgbureau.sg.user_footer", bot_name=t("bot.brand_name")))
    return embed


def build_case_panel_embed(case: storage.SGLCase, guild: discord.Guild) -> discord.Embed:
    client = guild.get_member(case.client_id)
    lawyer = guild.get_member(case.lead_lawyer_id)
    secretary = guild.get_member(case.secretary_id) if case.secretary_id else None
    embed = base_embed(
        t("sgbureau.sg.case_title", case_number=format_case_number(case.case_number), emoji=case_status_emoji(case)),
        t("sgbureau.sg.case_description", status=case_status_label(case), channel=f"<#{case.channel_id}>" if case.channel_id else t("common.no_data")),
    )
    embed.add_field(name=t("sgbureau.case.client_field"), value=(client.mention if client else f"`{case.client_id}`"), inline=True)
    embed.add_field(name=t("sgbureau.case.lawyer_field"), value=(lawyer.mention if lawyer else f"`{case.lead_lawyer_id}`"), inline=True)
    embed.add_field(name=t("sgbureau.case.secretary_field"), value=(secretary.mention if secretary else t("common.no_data")), inline=True)
    embed.add_field(name=t("sgbureau.case.request_type_field"), value=case.request_type or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.case.nickname_field"), value=case.client_nick or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.case.static_field"), value=case.static_id or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.sg.claim_link_field"), value=case.claim_link or t("common.no_data"), inline=False)
    receipts = storage.list_sgl_receipts_for_case(case.id, limit=3)
    if receipts:
        lines = []
        for receipt in receipts:
            status_key = f"sgbureau.receipt.status.{receipt.status}"
            status_text = t(status_key)
            if status_text == status_key:
                status_text = receipt.status
            proof_text = t("sgbureau.receipt.proofs_short_yes") if receipt.proof_services_url and receipt.proof_duty_url else t("sgbureau.receipt.proofs_short_no")
            lines.append(t(
                "sgbureau.receipt.case_panel_line",
                id=receipt.id,
                suffix=receipt.court_suffix,
                court=receipt.court_label,
                total=format_money(receipt.total_amount),
                lawyer=format_money(receipt.lawyer_amount),
                duty=format_money(receipt.duty_amount),
                status=status_text,
                proofs=proof_text,
            ))
        embed.add_field(name=t("sgbureau.receipt.case_panel_field"), value="\n".join(lines)[:1024], inline=False)
    if case.situation_text:
        embed.add_field(name=t("sgbureau.sg.situation_field"), value=case.situation_text[:900], inline=False)
    events = storage.get_sgl_case_events(case.id, limit=5)
    if events:
        lines = []
        for event in events:
            action_key = f"sgbureau.sg.action.{event.get('action')}"
            action_text = t(action_key)
            if action_text == action_key:
                action_text = str(event.get("action") or "event")
            actor = event.get("actor_display") or t("common.no_data")
            created = str(event.get("created_at") or "")[:19].replace("T", " ")
            lines.append(t("sgbureau.sg.event_line", datetime=created, action=action_text, actor=actor))
        embed.add_field(name=t("sgbureau.sg.recent_events_field"), value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text=t("sgbureau.sg.case_footer", bot_name=t("bot.brand_name")))
    return embed


async def create_sgl_case_channel(
    *,
    bot: commands.Bot,
    guild: discord.Guild,
    created_by: discord.Member,
    client: discord.Member,
    lawyer: discord.Member | None = None,
    secretary: discord.Member | None = None,
) -> tuple[storage.SGLCase, discord.TextChannel]:
    lead_lawyer = lawyer or await fetch_member_safe(guild, SGBUREAU_DEFAULT_LAWYER_ID)
    if lead_lawyer is None:
        raise RuntimeError(t("sgbureau.case.errors.default_lawyer_not_found", user_id=SGBUREAU_DEFAULT_LAWYER_ID))

    category = await resolve_category(guild, bot, SGBUREAU_CATEGORY_ID)
    if category is None:
        raise RuntimeError(t("sgbureau.case.errors.category_not_found", category_id=SGBUREAU_CATEGORY_ID))

    reserved = await asyncio.to_thread(
        storage.reserve_sgl_case,
        guild_id=guild.id,
        client_id=client.id,
        client_display=client.display_name,
        lead_lawyer_id=lead_lawyer.id,
        lead_lawyer_display=lead_lawyer.display_name,
        secretary_id=secretary.id if secretary else None,
        secretary_display=secretary.display_name if secretary else None,
        created_by_id=created_by.id,
        created_by_display=created_by.display_name,
    )

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        client: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
        lead_lawyer: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, manage_messages=True),
    }
    if secretary:
        overwrites[secretary] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, manage_messages=True)
    staff_role = guild.get_role(SGBUREAU_STAFF_ROLE_ID)
    if staff_role is not None:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, manage_messages=True)
    if guild.me is not None:
        overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, manage_messages=True, manage_channels=True)

    try:
        channel = await guild.create_text_channel(
            name=case_channel_name(reserved),
            category=category,
            overwrites=overwrites,
            topic=t("sgbureau.case.channel_topic", case_number=format_case_number(reserved.case_number), client=member_label(client), lawyer=member_label(lead_lawyer)),
            reason=t("sgbureau.case.audit_newcase_reason", case_number=format_case_number(reserved.case_number)),
        )
    except discord.HTTPException as exc:
        await asyncio.to_thread(
            storage.mark_sgl_case_error,
            reserved.id,
            f"channel_create_failed:{str(exc)[:200]}",
        )
        raise RuntimeError(t("sgbureau.case.errors.channel_create_failed", error=str(exc)[:180])) from exc

    case = await asyncio.to_thread(
        storage.attach_sgl_case_channel,
        reserved.id,
        channel.id,
    )
    if case is None:
        raise RuntimeError(t("sgbureau.case.errors.db_case_attach_failed"))

    schedule_case_refresh(bot, guild, case)
    await channel.send(embed=build_case_greeting_embed(case, client, lead_lawyer, secretary), view=CaseInitView(), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    return case, channel

def build_clink_embed(case: storage.SGLCase, link: str, author: discord.Member) -> discord.Embed:
    embed = base_embed(t("sgbureau.case.link_title", case_number=format_case_number(case.case_number)), t("sgbureau.case.link_description", link=link))
    embed.set_footer(text=t("sgbureau.case.link_footer", author=member_label(author), datetime=format_full_datetime(now_local())))
    return embed


def build_portfolio_embed(case: storage.SGLCase, description: str) -> discord.Embed:
    link = case.claim_link or t("common.no_data")
    embed = base_embed(
        t("sgbureau.case.portfolio_title", case_number=format_case_number(case.case_number)),
        t("sgbureau.case.portfolio_description", case_number=format_case_number(case.case_number), link=link, description=description or t("common.no_data")),
    )
    embed.set_footer(text=t("sgbureau.case.portfolio_footer", datetime=format_full_datetime(now_local())))
    return embed


def build_receipt_invoice_content(case: storage.SGLCase, receipt: storage.SGLReceipt, client: discord.Member | None) -> str:
    client_mention = client.mention if client else f"<@{case.client_id}>"
    return t(
        "sgbureau.receipt.invoice_content",
        client=client_mention,
        case_number=format_case_number(case.case_number),
        receipt_id=receipt.id,
    )


def build_receipt_invoice_embed(case: storage.SGLCase, receipt: storage.SGLReceipt, client: discord.Member | None, lawyer: discord.Member | None, author: discord.Member | None) -> discord.Embed:
    lawyer_mention = lawyer.mention if lawyer else f"<@{case.lead_lawyer_id}>"
    embed = base_embed(
        t("sgbureau.receipt.invoice_title", case_number=format_case_number(case.case_number), receipt_id=receipt.id),
        t(
            "sgbureau.receipt.invoice_description",
            court=receipt.court_label,
            suffix=receipt.court_suffix,
            total=format_money(receipt.total_amount),
        ),
    )
    embed.add_field(
        name=t("sgbureau.receipt.invoice_lawyer_field"),
        value=t(
            "sgbureau.receipt.invoice_lawyer_value",
            amount=format_money(receipt.lawyer_amount),
            bank=receipt.lawyer_bank,
            lawyer=lawyer_mention,
        ),
        inline=True,
    )
    embed.add_field(
        name=t("sgbureau.receipt.invoice_duty_field"),
        value=t(
            "sgbureau.receipt.invoice_duty_value",
            amount=format_money(receipt.duty_amount),
            bank=receipt.duty_bank,
        ),
        inline=True,
    )
    embed.add_field(
        name=t("sgbureau.receipt.invoice_notice_field"),
        value=t("sgbureau.receipt.invoice_notice_value"),
        inline=False,
    )
    embed.set_footer(text=t("sgbureau.receipt.invoice_footer", author=member_label(author), datetime=format_full_datetime(now_local())))
    return embed


def build_receipt_proofs_embed(case: storage.SGLCase, receipt: storage.SGLReceipt, actor: discord.Member) -> discord.Embed:
    embed = base_embed(
        t("sgbureau.receipt.proofs_title", case_number=format_case_number(case.case_number)),
        t("sgbureau.receipt.proofs_description", receipt_id=receipt.id, actor=actor.mention),
    )
    embed.add_field(name=t("sgbureau.receipt.services_proof_field"), value=receipt.proof_services_url or t("common.no_data"), inline=False)
    embed.add_field(name=t("sgbureau.receipt.duty_proof_field"), value=receipt.proof_duty_url or t("common.no_data"), inline=False)
    embed.set_footer(text=t("sgbureau.receipt.proofs_footer", datetime=format_full_datetime(now_local())))
    return embed


def build_receipt_confirmation_embed(case: storage.SGLCase, receipt: storage.SGLReceipt) -> discord.Embed:
    embed = base_embed(
        t("sgbureau.receipt.confirm_title", case_number=format_case_number(case.case_number)),
        t("sgbureau.receipt.confirm_description", receipt_id=receipt.id, suffix=receipt.court_suffix, total=format_money(receipt.total_amount)),
    )
    embed.add_field(name=t("sgbureau.receipt.services_proof_field"), value=receipt.proof_services_url or t("common.no_data"), inline=False)
    embed.add_field(name=t("sgbureau.receipt.duty_proof_field"), value=receipt.proof_duty_url or t("common.no_data"), inline=False)
    return embed


def build_receipt_confirmed_embed(case: storage.SGLCase, receipt: storage.SGLReceipt, actor: discord.Member) -> discord.Embed:
    embed = base_embed(
        t("sgbureau.receipt.confirmed_title", case_number=format_case_number(case.case_number)),
        t("sgbureau.receipt.confirmed_description", suffix=receipt.court_suffix, actor=actor.mention),
    )
    embed.set_footer(text=t("sgbureau.receipt.confirmed_footer", datetime=format_full_datetime(now_local())))
    return embed


async def update_case_channel_appearance(guild: discord.Guild, bot: commands.Bot, case: storage.SGLCase) -> None:
    if not case.channel_id:
        return
    channel = guild.get_channel(case.channel_id)
    if channel is None:
        try:
            fetched = await bot.fetch_channel(case.channel_id)
            channel = fetched if isinstance(fetched, discord.TextChannel) else None
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            channel = None
    if not isinstance(channel, discord.TextChannel):
        return
    desired_name = case_channel_name(case)
    try:
        if channel.name != desired_name:
            await channel.edit(name=desired_name, reason=t("sgbureau.case.audit_rename_reason", case_number=format_case_number(case.case_number), status=case_status_emoji(case)))
    except discord.HTTPException:
        pass


async def reorder_case_channels(guild: discord.Guild, bot: commands.Bot, category_id: int) -> None:
    category = await resolve_category(guild, bot, category_id)
    if category is None:
        return
    stored_cases = await asyncio.to_thread(
        storage.list_sgl_cases_with_channels,
        guild.id,
    )
    cases = [case for case in stored_cases if case.channel_id]
    channel_to_case = {int(case.channel_id): case for case in cases if case.channel_id}
    case_channels = []
    for channel in category.text_channels:
        case = channel_to_case.get(channel.id)
        if case is not None:
            case_channels.append((case.case_number, channel))
    case_channels.sort(key=lambda item: item[0])

    # Reordering channels is a heavily rate-limited Discord endpoint. Do it slowly
    # and never block command/modal responses on this maintenance task.
    for index, (_, channel) in enumerate(case_channels):
        try:
            await channel.edit(position=index)
            await asyncio.sleep(1.2)
        except discord.HTTPException:
            await asyncio.sleep(3.0)
            continue


_REORDER_TASKS: dict[tuple[int, int], asyncio.Task] = {}


def schedule_case_reorder(bot: commands.Bot, guild_id: int, category_id: int) -> None:
    key = (guild_id, category_id)
    existing = _REORDER_TASKS.get(key)
    if existing is not None and not existing.done():
        return

    async def runner() -> None:
        try:
            await asyncio.sleep(max(1, SGBUREAU_REORDER_DEBOUNCE_SECONDS))
            guild = bot.get_guild(guild_id)
            if guild is None:
                return
            await reorder_case_channels(guild, bot, category_id)
        except Exception as exc:
            print(t("sgbureau.console.reorder_failed", error=str(exc)[:180]))
        finally:
            _REORDER_TASKS.pop(key, None)

    _REORDER_TASKS[key] = bot.loop.create_task(runner())


async def refresh_case_channel(guild: discord.Guild, bot: commands.Bot, case: storage.SGLCase, category_id: int | None = None) -> None:
    await update_case_channel_appearance(guild, bot, case)
    schedule_case_reorder(bot, guild.id, category_id or SGBUREAU_CATEGORY_ID)


def schedule_case_refresh(bot: commands.Bot, guild: discord.Guild, case: storage.SGLCase, category_id: int | None = None) -> None:
    async def runner() -> None:
        try:
            await refresh_case_channel(guild, bot, case, category_id)
        except Exception as exc:
            print(t("sgbureau.console.reorder_failed", error=f"refresh_case_channel: {str(exc)[:180]}"))

    bot.loop.create_task(runner())


async def archive_case_channel(bot: commands.Bot, guild: discord.Guild, case: storage.SGLCase) -> None:
    if not case.channel_id:
        return
    channel = guild.get_channel(case.channel_id)
    if channel is None:
        try:
            fetched = await bot.fetch_channel(case.channel_id)
            channel = fetched if isinstance(fetched, discord.TextChannel) else None
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            channel = None
    if not isinstance(channel, discord.TextChannel):
        return

    archive_category = await resolve_category(guild, bot, SGBUREAU_ARCHIVE_CATEGORY_ID)
    if archive_category is None:
        try:
            await channel.send(t("sgbureau.case.archive_category_missing", category_id=SGBUREAU_ARCHIVE_CATEGORY_ID), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
        return

    # Keep explicit permissions so the client can still see the archived channel even if the archive category denies access.
    try:
        client = await fetch_member_safe(guild, case.client_id)
        lawyer = await fetch_member_safe(guild, case.lead_lawyer_id)
        secretary = await fetch_member_safe(guild, case.secretary_id) if case.secretary_id else None
        if client is not None:
            await channel.set_permissions(client, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
        if lawyer is not None:
            await channel.set_permissions(lawyer, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, manage_messages=True)
        if secretary is not None:
            await channel.set_permissions(secretary, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, manage_messages=True)
        staff_role = guild.get_role(SGBUREAU_STAFF_ROLE_ID)
        if staff_role is not None:
            await channel.set_permissions(staff_role, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, manage_messages=True)
        if guild.me is not None:
            await channel.set_permissions(guild.me, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, manage_messages=True, manage_channels=True, manage_threads=True)
    except discord.HTTPException:
        pass

    try:
        await channel.edit(category=archive_category, sync_permissions=False, reason=t("sgbureau.case.audit_archive_reason", case_number=format_case_number(case.case_number)))
        archived = await asyncio.to_thread(
            storage.mark_sgl_case_archived,
            guild.id,
            channel.id,
            archive_category.id,
        )
        if archived is not None:
            await refresh_case_channel(guild, bot, archived, SGBUREAU_ARCHIVE_CATEGORY_ID)
        try:
            await channel.send(t("sgbureau.case.archived_message", case_number=format_case_number(case.case_number)), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
    except discord.HTTPException:
        try:
            await channel.send(t("sgbureau.case.archive_failed"), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


async def archive_case_later(bot: commands.Bot, guild_id: int, channel_id: int, delay_seconds: int | None = None) -> None:
    delay = SGBUREAU_ARCHIVE_AFTER_SECONDS if delay_seconds is None else max(0, int(delay_seconds))
    if delay > 0:
        await asyncio.sleep(delay)
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    case = await asyncio.to_thread(
        storage.get_sgl_case_by_channel,
        guild_id,
        channel_id,
    )
    if case is None or case.status != "closed" or case.archived_at:
        return
    await archive_case_channel(bot, guild, case)


async def schedule_pending_case_archives(bot: commands.Bot) -> None:
    for guild in bot.guilds:
        cases = await asyncio.to_thread(
            storage.list_sgl_closed_cases_pending_archive,
            guild.id,
        )
        for case in cases:
            delay = SGBUREAU_ARCHIVE_AFTER_SECONDS
            if case.closed_at:
                try:
                    closed_dt = datetime.fromisoformat(case.closed_at)
                    if closed_dt.tzinfo is None:
                        closed_dt = closed_dt.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - closed_dt.astimezone(timezone.utc)).total_seconds()
                    delay = max(0, SGBUREAU_ARCHIVE_AFTER_SECONDS - int(elapsed))
                except Exception:
                    delay = SGBUREAU_ARCHIVE_AFTER_SECONDS
            bot.loop.create_task(archive_case_later(bot, guild.id, int(case.channel_id), delay))



class AddNewsModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, source_interaction: discord.Interaction) -> None:
        super().__init__(title=t("sgbureau.modal.title"), timeout=600)
        self.bot = bot
        self.source_channel_id = source_interaction.channel_id

        self.news_title = discord.ui.TextInput(label=t("sgbureau.modal.field_title_label"), placeholder=t("sgbureau.modal.field_title_placeholder"), min_length=1, max_length=256, required=True)
        self.news_body = discord.ui.TextInput(label=t("sgbureau.modal.field_body_label"), placeholder=t("sgbureau.modal.field_body_placeholder"), min_length=1, max_length=3900, required=True, style=discord.TextStyle.paragraph)
        self.news_note = discord.ui.TextInput(label=t("sgbureau.modal.field_note_label"), placeholder=t("sgbureau.modal.field_note_placeholder"), min_length=0, max_length=800, required=False, style=discord.TextStyle.paragraph)
        self.news_image = discord.ui.TextInput(label=t("sgbureau.modal.field_image_label"), placeholder=t("sgbureau.modal.field_image_placeholder"), min_length=0, max_length=400, required=False)
        self.news_global = discord.ui.TextInput(label=t("sgbureau.modal.field_global_label"), placeholder=t("sgbureau.modal.field_global_placeholder"), min_length=0, max_length=20, required=False)

        self.add_item(self.news_title)
        self.add_item(self.news_body)
        self.add_item(self.news_note)
        self.add_item(self.news_image)
        self.add_item(self.news_global)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        if interaction.channel_id != SGBUREAU_COMMAND_CHANNEL_ID:
            await interaction.response.send_message(t("sgbureau.errors.wrong_channel", allowed_channel_id=SGBUREAU_COMMAND_CHANNEL_ID), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        if not is_bureau_staff(interaction.user):
            await interaction.response.send_message(t("sgbureau.errors.no_permission"), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        title = str(self.news_title.value).strip()
        body = str(self.news_body.value).strip()
        note = safe_optional(str(self.news_note.value))
        image_url = safe_optional(str(self.news_image.value))
        publish_global = is_yes(str(self.news_global.value))
        if not title or not body:
            await interaction.response.send_message(t("sgbureau.errors.empty_news"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        target_channel = await resolve_text_channel(interaction.guild, self.bot, SGBUREAU_NEWS_CHANNEL_ID)
        if target_channel is None:
            await interaction.followup.send(t("sgbureau.errors.target_channel_not_found", target_channel_id=SGBUREAU_NEWS_CHANNEL_ID), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        global_channel: discord.TextChannel | None = None
        if publish_global:
            global_channel = await resolve_text_channel(interaction.guild, self.bot, SGBUREAU_GLOBAL_NEWS_CHANNEL_ID)
            if global_channel is None:
                await interaction.followup.send(t("sgbureau.errors.global_channel_not_found", target_channel_id=SGBUREAU_GLOBAL_NEWS_CHANNEL_ID), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                return

        created_dt = now_local()
        embed = build_news_embed(title=title, body=body, note=note, image_url=image_url, interaction=interaction, created_dt=created_dt)
        try:
            bureau_message = await target_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            global_message: discord.Message | None = None
            if publish_global and global_channel is not None:
                global_message = await global_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            await interaction.followup.send(t("sgbureau.errors.send_failed", target_channel_id=SGBUREAU_NEWS_CHANNEL_ID, error=str(exc)[:180]), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        try:
            record_id = await asyncio.to_thread(
                storage.record_bureau_announcement,
                guild_id=interaction.guild.id,
                author_id=interaction.user.id,
                author_display=interaction.user.display_name,
                source_channel_id=interaction.channel_id,
                target_channel_id=target_channel.id,
                message_id=bureau_message.id,
                global_channel_id=global_channel.id if global_channel else None,
                global_message_id=global_message.id if global_message else None,
                title=title,
                content=body,
                note=note,
                image_url=image_url,
                publish_global=publish_global,
                created_at=created_dt.astimezone(timezone.utc).isoformat(),
            )
        except Exception as exc:
            await interaction.followup.send(t("sgbureau.errors.db_failed", error=str(exc)[:180]), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        print(t("sgbureau.console.sent", author=interaction.user, target_channel_id=target_channel.id, record_id=record_id))
        if publish_global and global_channel is not None:
            await interaction.followup.send(t("sgbureau.news.success_global", bureau_channel_id=target_channel.id, global_channel_id=global_channel.id, record_id=record_id), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.followup.send(t("sgbureau.news.success", target_channel_id=target_channel.id, record_id=record_id), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class CaseInitView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label=t("sgbureau.case.init_params_button"), style=discord.ButtonStyle.secondary, custom_id="sgl_case_init_params")
    async def init_params(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or interaction.channel_id is None:
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_channel,
            interaction.guild.id,
            interaction.channel_id,
        )
        if case is None:
            await interaction.followup.send(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if not can_work_with_case(interaction.user, case):
            await interaction.followup.send(t("sgbureau.case.errors.not_case_participant"), ephemeral=True)
            return
        await interaction.followup.send(t("sgbureau.case.service_select_intro", case_number=format_case_number(case.case_number)), view=ServiceTypeView(case.case_number), ephemeral=True)


class ServiceTypeSelect(discord.ui.Select):
    def __init__(self, case_number: int) -> None:
        self.case_number = case_number
        options = [
            discord.SelectOption(label=t("sgbureau.case.service_court_label"), value="court", description=t("sgbureau.case.service_court_description")),
            discord.SelectOption(label=t("sgbureau.case.service_prosecutor_label"), value="prosecutor", description=t("sgbureau.case.service_prosecutor_description")),
            discord.SelectOption(label=t("sgbureau.case.service_other_label"), value="other", description=t("sgbureau.case.service_other_description")),
        ]
        super().__init__(placeholder=t("sgbureau.case.service_select_placeholder"), min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        request_type = self.values[0]
        if interaction.guild is None or interaction.channel_id is None:
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_channel,
            interaction.guild.id,
            interaction.channel_id,
        )
        if case is None:
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        profiles = await asyncio.to_thread(
            storage.list_client_profiles_for_user,
            interaction.guild.id,
            case.client_id,
            limit=20,
        )
        if profiles:
            await interaction.response.send_message(t("sgbureau.registry.profile_select_intro"), ephemeral=True, view=ExistingProfileView(self.case_number, request_type, profiles))
        else:
            await interaction.response.send_modal(ClientParamsModal(case_number=self.case_number, request_type=request_type))


class ServiceTypeView(discord.ui.View):
    def __init__(self, case_number: int) -> None:
        super().__init__(timeout=180)
        self.add_item(ServiceTypeSelect(case_number))


class ClientParamsModal(discord.ui.Modal):
    def __init__(self, case_number: int, request_type: str) -> None:
        super().__init__(title=t("sgbureau.case.params_modal_title", case_number=format_case_number(case_number)), timeout=900)
        self.case_number = case_number
        self.request_type = request_type
        self.client_nick = discord.ui.TextInput(label=t("sgbureau.case.field_nick_label"), placeholder=t("sgbureau.case.field_nick_placeholder"), min_length=3, max_length=60, required=True)
        self.static_id = discord.ui.TextInput(label=t("sgbureau.case.field_static_label"), placeholder=t("sgbureau.case.field_static_placeholder"), min_length=1, max_length=20, required=True)
        self.bank_account = discord.ui.TextInput(label=t("sgbureau.case.field_bank_label"), placeholder=t("sgbureau.case.field_bank_placeholder"), min_length=1, max_length=30, required=True)
        self.phone = discord.ui.TextInput(label=t("sgbureau.case.field_phone_label"), placeholder=t("sgbureau.case.field_phone_placeholder"), min_length=1, max_length=30, required=True)
        self.passport_url = discord.ui.TextInput(label=t("sgbureau.case.field_passport_label"), placeholder=t("sgbureau.case.field_passport_placeholder"), min_length=8, max_length=400, required=True)
        self.add_item(self.client_nick)
        self.add_item(self.static_id)
        self.add_item(self.bank_account)
        self.add_item(self.phone)
        self.add_item(self.passport_url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or interaction.channel_id is None:
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_channel,
            interaction.guild.id,
            interaction.channel_id,
        )
        if case is None:
            await interaction.followup.send(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if case.case_number != self.case_number:
            await interaction.followup.send(t("sgbureau.case.errors.case_mismatch"), ephemeral=True)
            return
        if not can_work_with_case(interaction.user, case):
            await interaction.followup.send(t("sgbureau.case.errors.not_case_participant"), ephemeral=True)
            return

        nick = str(self.client_nick.value).strip()
        static_id = str(self.static_id.value).strip()
        bank = str(self.bank_account.value).strip()
        phone = str(self.phone.value).strip()
        passport = str(self.passport_url.value).strip()

        errors: list[str] = []
        if len(nick.split()) < 2:
            errors.append(t("sgbureau.case.errors.nick_format"))
        if not static_id.isdigit():
            errors.append(t("sgbureau.case.errors.static_digits"))
        if not bank.isdigit():
            errors.append(t("sgbureau.case.errors.bank_digits"))
        if not phone.isdigit():
            errors.append(t("sgbureau.case.errors.phone_digits"))
        if not URL_RE.match(passport):
            errors.append(t("sgbureau.case.errors.passport_url"))
        if "yapix" in passport.lower() or "япикс" in passport.lower():
            errors.append(t("sgbureau.case.errors.passport_yapix"))

        if errors:
            await interaction.followup.send("\n".join(f"• {x}" for x in errors), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        service_text = t(f"sgbureau.case.service_value.{self.request_type}")
        updated = await asyncio.to_thread(
            storage.update_sgl_case_params,
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id,
            request_type=service_text,
            client_nick=nick,
            static_id=static_id,
            bank_account=bank,
            phone=phone,
            passport_url=passport,
            actor_id=interaction.user.id,
            actor_display=interaction.user.display_name,
        )
        if updated is None:
            await interaction.followup.send(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return

        await asyncio.to_thread(
            storage.save_client_profile,
            guild_id=interaction.guild.id,
            discord_user_id=case.client_id,
            client_nick=nick,
            static_id=static_id,
            bank_account=bank,
            phone=phone,
            passport_url=passport,
            last_case_id=updated.id,
        )
        await interaction.followup.send(t("sgbureau.case.params_saved_private"), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        schedule_case_refresh(interaction.client, interaction.guild, updated)
        if isinstance(interaction.channel, discord.TextChannel):
            await post_case_params_messages(interaction.channel, updated, interaction.user)




class ExistingProfileSelect(discord.ui.Select):
    def __init__(self, case_number: int, request_type: str, profiles: list[storage.ClientProfile]) -> None:
        self.case_number = case_number
        self.request_type = request_type
        self.profile_map = {str(p.id): p for p in profiles}
        options = [discord.SelectOption(label=f"{p.client_nick} [{p.static_id}]", value=str(p.id), description=f"{p.phone} | {p.bank_account}") for p in profiles[:25]]
        super().__init__(placeholder=t("sgbureau.registry.profile_select_placeholder"), min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or interaction.channel_id is None:
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_channel,
            interaction.guild.id,
            interaction.channel_id,
        )
        if case is None or case.case_number != self.case_number:
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        profile = self.profile_map.get(self.values[0])
        if profile is None:
            await interaction.response.send_message(t("sgbureau.registry.profile_not_found"), ephemeral=True)
            return
        # Save request type first so it matches selection
        updated = await asyncio.to_thread(
            storage.update_sgl_case_params,
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id,
            request_type=t(f"sgbureau.case.service_value.{self.request_type}"),
            client_nick=profile.client_nick,
            static_id=profile.static_id,
            bank_account=profile.bank_account,
            phone=profile.phone,
            passport_url=profile.passport_url,
            actor_id=interaction.user.id,
            actor_display=interaction.user.display_name,
        )
        if updated is None:
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        await asyncio.to_thread(
            storage.save_client_profile,
            guild_id=interaction.guild.id, discord_user_id=case.client_id, client_nick=profile.client_nick, static_id=profile.static_id,
            bank_account=profile.bank_account, phone=profile.phone, passport_url=profile.passport_url, last_case_id=updated.id,
        )
        await interaction.response.send_message(t("sgbureau.registry.profile_applied"), ephemeral=True)
        schedule_case_refresh(interaction.client, interaction.guild, updated)
        if isinstance(interaction.channel, discord.TextChannel):
            await post_case_params_messages(interaction.channel, updated, interaction.user)


class ExistingProfileView(discord.ui.View):
    def __init__(self, case_number: int, request_type: str, profiles: list[storage.ClientProfile]) -> None:
        super().__init__(timeout=300)
        self.case_number = case_number
        self.request_type = request_type
        self.add_item(ExistingProfileSelect(case_number, request_type, profiles))

    @discord.ui.button(label=t("sgbureau.registry.new_profile_button"), style=discord.ButtonStyle.secondary)
    async def new_profile(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ClientParamsModal(case_number=self.case_number, request_type=self.request_type))

class CloseCaseModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, case: storage.SGLCase) -> None:
        super().__init__(title=t("sgbureau.case.close_modal_title", case_number=format_case_number(case.case_number)), timeout=900)
        self.bot = bot
        self.case_number = case.case_number
        self.publish = discord.ui.TextInput(label=t("sgbureau.case.close_publish_label"), placeholder=t("sgbureau.case.close_publish_placeholder"), min_length=0, max_length=20, required=False)
        self.description = discord.ui.TextInput(label=t("sgbureau.case.close_description_label"), placeholder=t("sgbureau.case.close_description_placeholder"), min_length=0, max_length=1500, required=False, style=discord.TextStyle.paragraph)
        self.add_item(self.publish)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or interaction.channel_id is None:
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_channel,
            interaction.guild.id,
            interaction.channel_id,
        )
        if case is None or case.case_number != self.case_number:
            await interaction.followup.send(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if not can_close_case(interaction.user, case):
            await interaction.followup.send(t("sgbureau.case.errors.close_no_permission"), ephemeral=True)
            return

        publish_portfolio = is_yes(str(self.publish.value))
        description = str(self.description.value).strip()
        portfolio_message_id: int | None = None
        if publish_portfolio and not case.claim_link:
            await interaction.followup.send(t("sgbureau.case.errors.no_claim_link"), ephemeral=True)
            return

        if publish_portfolio:
            portfolio_channel = await resolve_text_channel(interaction.guild, self.bot, SGBUREAU_PORTFOLIO_CHANNEL_ID)
            if portfolio_channel is None:
                await interaction.followup.send(t("sgbureau.case.errors.portfolio_not_found", channel_id=SGBUREAU_PORTFOLIO_CHANNEL_ID), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                return
            msg = await portfolio_channel.send(embed=build_portfolio_embed(case, description), allowed_mentions=discord.AllowedMentions.none())
            portfolio_message_id = msg.id

        closed = await asyncio.to_thread(
            storage.close_sgl_case,
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id,
            actor_id=interaction.user.id,
            actor_display=interaction.user.display_name,
            publish_portfolio=publish_portfolio,
            portfolio_description=description,
            portfolio_message_id=portfolio_message_id,
        )
        if closed is None:
            await interaction.followup.send(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        await interaction.followup.send(t("sgbureau.case.close_saved_private", case_number=format_case_number(case.case_number)), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        schedule_case_refresh(self.bot, interaction.guild, closed)
        self.bot.loop.create_task(archive_case_later(self.bot, interaction.guild.id, interaction.channel_id))
        if isinstance(interaction.channel, discord.TextChannel):
            embed = base_embed(t("sgbureau.case.closed_title", case_number=format_case_number(case.case_number)), t("sgbureau.case.closed_description", actor=interaction.user.mention, archive_hours=max(1, SGBUREAU_ARCHIVE_AFTER_SECONDS // 3600)))
            await interaction.channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


class CaseLinkModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, case: storage.SGLCase) -> None:
        super().__init__(title=t("sgbureau.sg.link_modal_title", case_number=format_case_number(case.case_number)), timeout=600)
        self.bot = bot
        self.case_number = case.case_number
        self.link = discord.ui.TextInput(
            label=t("sgbureau.sg.link_modal_label"),
            placeholder=t("sgbureau.sg.link_modal_placeholder"),
            min_length=8,
            max_length=400,
            required=True,
        )
        self.add_item(self.link)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or interaction.channel_id is None:
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_channel,
            interaction.guild.id,
            interaction.channel_id,
        )
        if case is None or case.case_number != self.case_number:
            await interaction.followup.send(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if not can_close_case(interaction.user, case) and not is_bureau_staff(interaction.user):
            await interaction.followup.send(t("sgbureau.case.errors.link_no_permission"), ephemeral=True)
            return
        clean_link = str(self.link.value).strip()
        if not URL_RE.match(clean_link):
            await interaction.followup.send(t("sgbureau.case.errors.link_url"), ephemeral=True)
            return
        updated = await asyncio.to_thread(
            storage.set_sgl_case_link,
            interaction.guild.id,
            interaction.channel_id,
            clean_link,
            interaction.user.id,
            interaction.user.display_name,
        )
        if updated is None:
            await interaction.followup.send(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        schedule_case_refresh(self.bot, interaction.guild, updated)
        if isinstance(interaction.channel, discord.TextChannel):
            msg = await interaction.channel.send(embed=build_clink_embed(updated, clean_link, interaction.user), allowed_mentions=discord.AllowedMentions.none())
            try:
                await msg.pin(reason=t("sgbureau.case.audit_pin_link_reason", case_number=format_case_number(updated.case_number)))
                await asyncio.to_thread(
                    storage.set_sgl_case_link_message,
                    interaction.guild.id,
                    interaction.channel_id,
                    msg.id,
                )
            except discord.HTTPException:
                await interaction.followup.send(t("sgbureau.case.link_pin_failed"), ephemeral=True)
        await interaction.followup.send(t("sgbureau.sg.link_saved", case_number=format_case_number(updated.case_number)), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class ReceiptModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, case: storage.SGLCase) -> None:
        super().__init__(title=t("sgbureau.receipt.modal_title", case_number=format_case_number(case.case_number)), timeout=900)
        self.bot = bot
        self.case_number = case.case_number
        self.where = discord.ui.TextInput(label=t("sgbureau.receipt.where_label"), placeholder=t("sgbureau.receipt.where_placeholder"), min_length=1, max_length=80, required=True)
        self.total_amount = discord.ui.TextInput(label=t("sgbureau.receipt.total_label"), placeholder=t("sgbureau.receipt.total_placeholder"), min_length=1, max_length=30, required=True)
        self.duty_amount = discord.ui.TextInput(label=t("sgbureau.receipt.duty_label"), placeholder=t("sgbureau.receipt.duty_placeholder"), min_length=0, max_length=30, required=False)
        self.add_item(self.where)
        self.add_item(self.total_amount)
        self.add_item(self.duty_amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or interaction.channel_id is None:
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_channel,
            interaction.guild.id,
            interaction.channel_id,
        )
        if case is None or case.case_number != self.case_number:
            await interaction.followup.send(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if not can_close_case(interaction.user, case) and not is_bureau_staff(interaction.user):
            await interaction.followup.send(t("sgbureau.receipt.no_permission"), ephemeral=True)
            return

        court = resolve_court_type(str(self.where.value))
        if court is None:
            await interaction.followup.send(t("sgbureau.receipt.errors.unknown_court"), ephemeral=True)
            return
        total = parse_amount(str(self.total_amount.value))
        if total is None or total <= 0:
            await interaction.followup.send(t("sgbureau.receipt.errors.total_amount"), ephemeral=True)
            return
        if court["fee"] is None:
            duty = parse_amount(str(self.duty_amount.value))
            if duty is None or duty < 0:
                await interaction.followup.send(t("sgbureau.receipt.errors.custom_duty"), ephemeral=True)
                return
        else:
            duty = int(court["fee"])
        lawyer_amount = int(total) - int(duty)
        if lawyer_amount < 0:
            await interaction.followup.send(t("sgbureau.receipt.errors.duty_bigger_than_total", total=format_money(total), duty=format_money(duty)), ephemeral=True)
            return

        receipt = await asyncio.to_thread(
            storage.create_sgl_receipt,
            guild_id=interaction.guild.id,
            case=case,
            created_by_id=interaction.user.id,
            created_by_display=interaction.user.display_name,
            court_code=str(court["code"]),
            court_label=str(court["label"]),
            court_suffix=str(court["suffix"]),
            total_amount=int(total),
            lawyer_amount=int(lawyer_amount),
            duty_amount=int(duty),
            lawyer_bank=SGBUREAU_LAWYER_BANK_ACCOUNT,
            duty_bank=SGBUREAU_COURT_FEE_BANK_ACCOUNT,
        )
        client = await fetch_member_safe(interaction.guild, case.client_id)
        lawyer = await fetch_member_safe(interaction.guild, case.lead_lawyer_id)
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        msg = await interaction.channel.send(
            content=build_receipt_invoice_content(case, receipt, client),
            embed=build_receipt_invoice_embed(case, receipt, client, lawyer, interaction.user),
            view=ReceiptSubmitProofsView(self.bot),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        await asyncio.to_thread(
            storage.set_sgl_receipt_invoice_message,
            receipt.id,
            msg.id,
        )
        await interaction.followup.send(t("sgbureau.receipt.created_private", receipt_id=receipt.id), ephemeral=True)


class ReceiptProofsModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, invoice_message_id: int) -> None:
        super().__init__(title=t("sgbureau.receipt.proof_modal_title"), timeout=900)
        self.bot = bot
        self.invoice_message_id = invoice_message_id
        self.services_url = discord.ui.TextInput(label=t("sgbureau.receipt.services_proof_label"), placeholder=t("sgbureau.receipt.proof_placeholder"), min_length=8, max_length=400, required=True)
        self.duty_url = discord.ui.TextInput(label=t("sgbureau.receipt.duty_proof_label"), placeholder=t("sgbureau.receipt.proof_placeholder"), min_length=8, max_length=400, required=True)
        self.add_item(self.services_url)
        self.add_item(self.duty_url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or interaction.channel_id is None:
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        receipt = await asyncio.to_thread(
            storage.get_sgl_receipt_by_invoice_message,
            interaction.guild.id,
            self.invoice_message_id,
        )
        if receipt is None:
            await interaction.followup.send(t("sgbureau.receipt.errors.receipt_not_found"), ephemeral=True)
            return
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_number,
            interaction.guild.id,
            receipt.case_number,
        )
        if case is None or case.channel_id != interaction.channel_id:
            await interaction.followup.send(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if not can_work_with_case(interaction.user, case):
            await interaction.followup.send(t("sgbureau.case.errors.not_case_participant"), ephemeral=True)
            return
        services_url = str(self.services_url.value).strip()
        duty_url = str(self.duty_url.value).strip()
        errors: list[str] = []
        for label, url in [(t("sgbureau.receipt.services_proof_field"), services_url), (t("sgbureau.receipt.duty_proof_field"), duty_url)]:
            if not URL_RE.match(url):
                errors.append(t("sgbureau.receipt.errors.proof_url", field=label))
            if "yapix" in url.lower() or "япикс" in url.lower():
                errors.append(t("sgbureau.receipt.errors.proof_yapix", field=label))
        if errors:
            await interaction.followup.send("\n".join(f"• {x}" for x in errors), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        updated = await asyncio.to_thread(
            storage.submit_sgl_receipt_proofs_by_invoice_message,
            guild_id=interaction.guild.id,
            invoice_message_id=self.invoice_message_id,
            services_url=services_url,
            duty_url=duty_url,
            actor_id=interaction.user.id,
            actor_display=interaction.user.display_name,
        )
        if updated is None:
            await interaction.followup.send(t("sgbureau.receipt.errors.receipt_not_found"), ephemeral=True)
            return
        dm_sent = False
        if isinstance(interaction.channel, discord.TextChannel):
            await interaction.channel.send(embed=build_receipt_proofs_embed(case, updated, interaction.user), allowed_mentions=discord.AllowedMentions.none())
            lawyer = await fetch_member_safe(interaction.guild, case.lead_lawyer_id)
            if lawyer is not None:
                try:
                    confirm_msg = await lawyer.send(
                        content=t("sgbureau.receipt.confirm_dm_content", case_number=format_case_number(case.case_number), receipt_id=updated.id),
                        embed=build_receipt_confirmation_embed(case, updated),
                        view=ReceiptConfirmView(self.bot),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    await asyncio.to_thread(
                        storage.set_sgl_receipt_confirmation_message,
                        updated.id,
                        confirm_msg.id,
                    )
                    dm_sent = True
                except discord.HTTPException:
                    dm_sent = False
            if dm_sent:
                await interaction.channel.send(
                    t("sgbureau.receipt.confirm_dm_sent_channel", lawyer=lawyer.mention if lawyer else f"<@{case.lead_lawyer_id}>"),
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
            else:
                await interaction.channel.send(
                    t("sgbureau.receipt.confirm_dm_failed_channel", lawyer=f"<@{case.lead_lawyer_id}>"),
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
        await interaction.followup.send(t("sgbureau.receipt.proofs_saved_private"), ephemeral=True)


class ReceiptSubmitProofsView(discord.ui.View):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label=t("sgbureau.receipt.submit_proofs_button"), style=discord.ButtonStyle.secondary, custom_id="sgl_receipt_submit_proofs")
    async def submit_proofs(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or interaction.message is None:
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        receipt = await asyncio.to_thread(
            storage.get_sgl_receipt_by_invoice_message,
            interaction.guild.id,
            interaction.message.id,
        )
        if receipt is None:
            await interaction.response.send_message(t("sgbureau.receipt.errors.receipt_not_found"), ephemeral=True)
            return
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_number,
            interaction.guild.id,
            receipt.case_number,
        )
        if case is None:
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if not can_work_with_case(interaction.user, case):
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_participant"), ephemeral=True)
            return
        await interaction.response.send_modal(ReceiptProofsModal(self.bot, interaction.message.id))


class ReceiptConfirmView(discord.ui.View):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label=t("sgbureau.receipt.confirm_button"), style=discord.ButtonStyle.success, custom_id="sgl_receipt_confirm_payment")
    async def confirm_payment(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.message is None:
            await interaction.response.send_message(t("sgbureau.receipt.errors.receipt_not_found"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=interaction.guild is not None)

        if interaction.guild is not None:
            receipt = await asyncio.to_thread(
                storage.get_sgl_receipt_by_confirmation_message,
                interaction.guild.id,
                interaction.message.id,
            )
        else:
            receipt = await asyncio.to_thread(
                storage.get_sgl_receipt_by_confirmation_message_any,
                interaction.message.id,
            )
        if receipt is None:
            await interaction.followup.send(t("sgbureau.receipt.errors.receipt_not_found"), ephemeral=interaction.guild is not None)
            return

        guild = interaction.guild or self.bot.get_guild(receipt.guild_id)
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_number,
            receipt.guild_id,
            receipt.case_number,
        )
        if case is None:
            await interaction.followup.send(t("sgbureau.case.errors.not_case_channel"), ephemeral=interaction.guild is not None)
            return

        is_admin = False
        if isinstance(interaction.user, discord.Member):
            is_admin = bool(interaction.user.guild_permissions.administrator)
        if not (is_admin or interaction.user.id == case.lead_lawyer_id):
            await interaction.followup.send(t("sgbureau.receipt.errors.only_lead_lawyer_confirm"), ephemeral=interaction.guild is not None)
            return

        updated = await asyncio.to_thread(
            storage.confirm_sgl_receipt_by_confirmation_message_any,
            confirmation_message_id=interaction.message.id,
            actor_id=interaction.user.id,
            actor_display=getattr(interaction.user, "display_name", str(interaction.user)),
        )
        if updated is None:
            await interaction.followup.send(t("sgbureau.receipt.errors.receipt_not_found"), ephemeral=interaction.guild is not None)
            return

        if guild is not None:
            schedule_case_refresh(self.bot, guild, case)

        case_channel = self.bot.get_channel(case.channel_id or 0)
        if not isinstance(case_channel, discord.TextChannel) and case.channel_id:
            try:
                fetched = await self.bot.fetch_channel(case.channel_id)
                case_channel = fetched if isinstance(fetched, discord.TextChannel) else None
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                case_channel = None
        if isinstance(case_channel, discord.TextChannel):
            await case_channel.send(embed=build_receipt_confirmed_embed(case, updated, interaction.user), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

        await interaction.followup.send(t("sgbureau.receipt.confirmed_private"), ephemeral=interaction.guild is not None)





def page_count(total: int, per_page: int = 10) -> int:
    return max(1, (max(0, int(total)) + per_page - 1) // per_page)


def build_bureau_dashboard_embed(guild: discord.Guild) -> discord.Embed:
    clients = storage.count_client_profiles(guild.id)
    lawyers = storage.count_lawyer_profiles(guild.id)
    open_cases = storage.count_open_sgl_cases(guild.id)
    closed_cases = storage.count_closed_sgl_cases(guild.id)
    embed = base_embed(t("sgbureau.panel.title"), t("sgbureau.panel.description"))
    embed.add_field(name=t("sgbureau.panel.stats_field"), value=t("sgbureau.panel.stats_value", clients=clients, lawyers=lawyers, open_cases=open_cases, closed_cases=closed_cases), inline=False)
    embed.add_field(name=t("sgbureau.panel.modules_field"), value=t("sgbureau.panel.modules_value"), inline=False)
    embed.set_footer(text=t("sgbureau.panel.footer"))
    return embed


def build_pricing_embed() -> discord.Embed:
    fees = []
    for key in ("d", "s", "k", "o", "e"):
        item = COURT_TYPES[key]
        fee = format_money(item["fee"]) if item["fee"] is not None else t("sgbureau.panel.pricing_custom")
        fees.append(t("sgbureau.panel.pricing_line", suffix=item["suffix"], label=item["label"], fee=fee))
    embed = base_embed(t("sgbureau.panel.pricing_title"), t("sgbureau.panel.pricing_description"))
    embed.add_field(name=t("sgbureau.panel.pricing_court_field"), value="\n".join(fees), inline=False)
    embed.add_field(name=t("sgbureau.panel.pricing_banks_field"), value=t("sgbureau.panel.pricing_banks_value", lawyer_bank=SGBUREAU_LAWYER_BANK_ACCOUNT, duty_bank=SGBUREAU_COURT_FEE_BANK_ACCOUNT), inline=False)
    embed.set_footer(text=t("sgbureau.panel.pricing_footer"))
    return embed


def build_registry_home_embed() -> discord.Embed:
    embed = base_embed(t("sgbureau.registry.menu_title"), t("sgbureau.registry.menu_description"))
    embed.add_field(name=t("sgbureau.registry.menu_clients_field"), value=t("sgbureau.registry.menu_clients_value"), inline=False)
    embed.add_field(name=t("sgbureau.registry.menu_lawyers_field"), value=t("sgbureau.registry.menu_lawyers_value"), inline=False)
    embed.set_footer(text=t("sgbureau.registry.menu_footer"))
    return embed

def build_admin_home_embed() -> discord.Embed:
    embed = base_embed(t("sgbureau.admin.title"), t("sgbureau.admin.description"))
    embed.add_field(name=t("sgbureau.admin.targets_field"), value=t("sgbureau.admin.targets_value"), inline=False)
    embed.add_field(name=t("sgbureau.admin.actions_field"), value=t("sgbureau.admin.actions_value"), inline=False)
    embed.set_footer(text=t("sgbureau.admin.footer"))
    return embed


def build_admin_record_embed(target: str, identifier: str, record: dict[str, object]) -> discord.Embed:
    normalized = storage.normalize_admin_target(target) or target
    title = t("sgbureau.admin.record_title", target=normalized, identifier=identifier)
    body = storage.admin_format_record(record, max_value_length=220)
    embed = base_embed(title)
    chunks = [body[i:i + 1000] for i in range(0, len(body), 1000)] or [t("common.no_data")]
    for idx, chunk in enumerate(chunks[:4], start=1):
        embed.add_field(name=t("sgbureau.admin.record_chunk", index=idx), value=f"```text\n{chunk}\n```", inline=False)
    fields = ", ".join(storage.admin_allowed_fields(normalized))
    if fields:
        embed.add_field(name=t("sgbureau.admin.allowed_fields"), value=fields[:1000], inline=False)
    return embed


def admin_short_result(target: str, identifier: str, field: str | None = None) -> str:
    normalized = storage.normalize_admin_target(target) or target
    if field:
        return t("sgbureau.admin.edit_success", target=normalized, identifier=identifier, field=field)
    return t("sgbureau.admin.delete_success", target=normalized, identifier=identifier)



def registry_title(kind: str) -> str:
    return t("sgbureau.registry.clients_title") if kind == "clients" else t("sgbureau.registry.lawyers_title")


def registry_empty(kind: str) -> str:
    return t("sgbureau.registry.clients_empty") if kind == "clients" else t("sgbureau.registry.lawyers_empty")


def registry_item_line(kind: str, index: int, item: object) -> str:
    if kind == "clients":
        p = item  # type: ignore[assignment]
        return t("sgbureau.registry.page_client_line", index=index, nick=p.client_nick, static=p.static_id, phone=p.phone, bank=p.bank_account, discord_id=p.discord_user_id)
    p = item  # type: ignore[assignment]
    return t("sgbureau.registry.page_lawyer_line", index=index, nick=p.lawyer_nick, static=p.static_id or t("common.no_data"), phone=p.phone or t("common.no_data"), bank=p.bank_account or t("common.no_data"), discord_id=p.discord_user_id or t("common.no_data"))


def build_registry_page_embed(kind: str, guild_id: int, page: int, query: str | None, items: list[object], total: int) -> discord.Embed:
    pages = page_count(total)
    embed = base_embed(registry_title(kind), t("sgbureau.registry.page_description", page=page + 1, pages=pages, total=total, query=query or t("common.no_data")))
    lines = [registry_item_line(kind, i + 1, item) for i, item in enumerate(items)]
    embed.add_field(name=t("sgbureau.registry.result_field", count=len(items)), value=("\n".join(lines)[:4000] if lines else registry_empty(kind)), inline=False)
    embed.set_footer(text=t("sgbureau.registry.page_footer"))
    return embed


def build_client_profile_detail_embed(profile: storage.ClientProfile, guild: discord.Guild) -> discord.Embed:
    member = guild.get_member(profile.discord_user_id)
    cases = storage.list_sgl_cases_for_client(guild.id, profile.discord_user_id, limit=10)
    embed = base_embed(t("sgbureau.registry.client_detail_title", nick=profile.client_nick), t("sgbureau.registry.client_detail_description"))
    embed.add_field(name=t("sgbureau.sg.discord_field"), value=(member.mention if member else f"`{profile.discord_user_id}`"), inline=True)
    embed.add_field(name=t("sgbureau.sg.nickname_field"), value=profile.client_nick, inline=True)
    embed.add_field(name=t("sgbureau.sg.static_field"), value=profile.static_id, inline=True)
    embed.add_field(name=t("sgbureau.sg.phone_field"), value=profile.phone, inline=True)
    embed.add_field(name=t("sgbureau.sg.bank_field"), value=profile.bank_account, inline=True)
    embed.add_field(name=t("sgbureau.case.passport_field"), value=profile.passport_url or t("common.no_data"), inline=False)
    if cases:
        embed.add_field(name=t("sgbureau.sg.client_cases_field"), value="\n".join(case_short_line(c) for c in cases)[:1024], inline=False)
    else:
        embed.add_field(name=t("sgbureau.sg.client_cases_field"), value=t("sgbureau.sg.no_cases"), inline=False)
    embed.set_footer(text=t("sgbureau.registry.detail_footer", profile_id=profile.id))
    return embed


def build_lawyer_profile_detail_embed(profile: storage.LawyerProfile, guild: discord.Guild) -> discord.Embed:
    member = guild.get_member(profile.discord_user_id) if profile.discord_user_id else None
    embed = base_embed(t("sgbureau.registry.lawyer_detail_title", nick=profile.lawyer_nick), t("sgbureau.registry.lawyer_detail_description"))
    embed.add_field(name=t("sgbureau.sg.discord_field"), value=(member.mention if member else (f"`{profile.discord_user_id}`" if profile.discord_user_id else t("common.no_data"))), inline=True)
    embed.add_field(name=t("sgbureau.sg.nickname_field"), value=profile.lawyer_nick, inline=True)
    embed.add_field(name=t("sgbureau.sg.static_field"), value=profile.static_id or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.sg.phone_field"), value=profile.phone or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.sg.bank_field"), value=profile.bank_account or t("common.no_data"), inline=True)
    embed.add_field(name=t("sgbureau.registry.email_field"), value=profile.email or t("common.no_data"), inline=True)
    if profile.notes:
        embed.add_field(name=t("sgbureau.registry.notes_field"), value=profile.notes[:1024], inline=False)
    embed.set_footer(text=t("sgbureau.registry.detail_footer", profile_id=profile.id))
    return embed


class SGRequesterView(discord.ui.View):
    def __init__(self, requester_id: int, timeout: int = 600) -> None:
        super().__init__(timeout=timeout)
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(t("sgbureau.sg.menu_not_for_you"), ephemeral=True)
            return False
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return False
        if not is_bureau_staff(interaction.user):
            await interaction.response.send_message(t("sgbureau.errors.no_permission"), ephemeral=True)
            return False
        return True


class SGBureauDashboardView(SGRequesterView):
    def __init__(self, requester_id: int) -> None:
        super().__init__(requester_id)

    @discord.ui.button(label=t("sgbureau.panel.pricing_button"), style=discord.ButtonStyle.secondary)
    async def pricing(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=build_pricing_embed(), view=SGBureauPricingView(self.requester_id), allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label=t("sgbureau.panel.registry_button"), style=discord.ButtonStyle.secondary)
    async def registry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=build_registry_home_embed(), view=RegistryHomeView(self.requester_id), allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label=t("sgbureau.admin.button"), style=discord.ButtonStyle.secondary)
    async def admin(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=build_admin_home_embed(), view=SGBureauAdminView(self.requester_id), allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label=t("sgbureau.panel.refresh_button"), style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.edit_message(embed=build_bureau_dashboard_embed(interaction.guild), view=SGBureauDashboardView(self.requester_id), allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label=t("sgbureau.panel.exit_button"), style=discord.ButtonStyle.danger)
    async def exit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=t("sgbureau.panel.closed"), embed=None, view=None)


class SGBureauPricingView(SGRequesterView):
    def __init__(self, requester_id: int) -> None:
        super().__init__(requester_id)

    @discord.ui.button(label=t("sgbureau.panel.back_button"), style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.edit_message(embed=build_bureau_dashboard_embed(interaction.guild), view=SGBureauDashboardView(self.requester_id), allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label=t("sgbureau.panel.exit_button"), style=discord.ButtonStyle.danger)
    async def exit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=t("sgbureau.panel.closed"), embed=None, view=None)



class AdminEditModal(discord.ui.Modal):
    def __init__(self, requester_id: int, mode: str) -> None:
        super().__init__(title=t(f"sgbureau.admin.{mode}_modal_title"), timeout=300)
        self.requester_id = requester_id
        self.mode = mode
        self.target = discord.ui.TextInput(label=t("sgbureau.admin.field_target"), placeholder="case / client / lawyer / receipt", min_length=3, max_length=20, required=True)
        self.identifier = discord.ui.TextInput(label=t("sgbureau.admin.field_identifier"), placeholder=t("sgbureau.admin.field_identifier_placeholder"), min_length=1, max_length=40, required=True)
        self.add_item(self.target)
        self.add_item(self.identifier)
        if mode == "edit":
            self.field = discord.ui.TextInput(label=t("sgbureau.admin.field_field"), placeholder=t("sgbureau.admin.field_field_placeholder"), min_length=1, max_length=80, required=True)
            self.value = discord.ui.TextInput(label=t("sgbureau.admin.field_value"), placeholder=t("sgbureau.admin.field_value_placeholder"), style=discord.TextStyle.paragraph, max_length=1000, required=True)
            self.add_item(self.field)
            self.add_item(self.value)
        else:
            self.confirm = discord.ui.TextInput(label=t("sgbureau.admin.field_confirm"), placeholder="DELETE", min_length=6, max_length=20, required=True)
            self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(t("sgbureau.sg.menu_not_for_you"), ephemeral=True)
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not is_bureau_staff(interaction.user):
            await interaction.response.send_message(t("sgbureau.errors.no_permission"), ephemeral=True)
            return
        target = str(self.target.value).strip()
        identifier = str(self.identifier.value).strip()
        await interaction.response.defer(ephemeral=True)
        try:
            if self.mode == "edit":
                field = str(self.field.value).strip()
                value = str(self.value.value)
                record = await asyncio.to_thread(
                    storage.admin_update_record,
                    guild_id=interaction.guild.id,
                    target=target,
                    identifier=identifier,
                    field=field,
                    value=value,
                    actor_id=interaction.user.id,
                    actor_display=interaction.user.display_name,
                )
                if record is None:
                    await _send_admin_modal_reply_safely(interaction, t("sgbureau.admin.not_found"))
                    return
                # If case was edited, try refreshing the channel name.
                normalized_target = await asyncio.to_thread(
                    storage.normalize_admin_target,
                    target,
                )
                if (normalized_target or "") == "case":
                    case = await asyncio.to_thread(
                        storage.get_sgl_case_by_number,
                        interaction.guild.id,
                        int(str(identifier).replace("SGL-", "").lstrip("0") or "0"),
                    )
                    if case:
                        schedule_case_refresh(interaction.client, interaction.guild, case)
                await _send_admin_modal_reply_safely(
                    interaction,
                    admin_short_result(target, identifier, field),
                )
                return

            confirm = str(self.confirm.value).strip().upper()
            if confirm not in {"DELETE", "УДАЛИТЬ"}:
                await _send_admin_modal_reply_safely(interaction, t("sgbureau.admin.confirm_failed"))
                return
            record = await asyncio.to_thread(
                storage.admin_delete_record,
                guild_id=interaction.guild.id,
                target=target,
                identifier=identifier,
                actor_id=interaction.user.id,
                actor_display=interaction.user.display_name,
            )
            if record is None:
                await _send_admin_modal_reply_safely(interaction, t("sgbureau.admin.not_found"))
                return
            normalized_target = await asyncio.to_thread(
                storage.normalize_admin_target,
                target,
            )
            if (normalized_target or "") == "case":
                channel_id = record.get("channel_id")
                if channel_id:
                    channel = interaction.guild.get_channel(int(channel_id))
                    if isinstance(channel, discord.TextChannel):
                        try:
                            await channel.delete(reason=t("sgbureau.admin.delete_case_reason", actor=member_label(interaction.user)))
                        except discord.HTTPException:
                            pass
            await _send_admin_modal_reply_safely(
                interaction,
                admin_short_result(target, identifier),
            )
        except ValueError as exc:
            code = str(exc)
            if code == "field_not_allowed":
                fields = ", ".join(
                    await asyncio.to_thread(storage.admin_allowed_fields, target)
                ) or t("common.no_data")
                await _send_admin_modal_reply_safely(
                    interaction,
                    t("sgbureau.admin.field_not_allowed", fields=fields),
                )
            else:
                await _send_admin_modal_reply_safely(
                    interaction,
                    t("sgbureau.admin.bad_request", error=code),
                )
        except Exception as exc:
            await _send_admin_modal_reply_safely(
                interaction,
                t("sgbureau.admin.failed", error=str(exc)[:500]),
            )


class SGBureauAdminView(SGRequesterView):
    def __init__(self, requester_id: int) -> None:
        super().__init__(requester_id)

    @discord.ui.button(label=t("sgbureau.admin.edit_button"), style=discord.ButtonStyle.secondary)
    async def edit_record(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AdminEditModal(self.requester_id, "edit"))

    @discord.ui.button(label=t("sgbureau.admin.delete_button"), style=discord.ButtonStyle.danger)
    async def delete_record(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AdminEditModal(self.requester_id, "delete"))

    @discord.ui.button(label=t("sgbureau.panel.back_button"), style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.edit_message(embed=build_bureau_dashboard_embed(interaction.guild), view=SGBureauDashboardView(self.requester_id), allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label=t("sgbureau.panel.exit_button"), style=discord.ButtonStyle.danger)
    async def exit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=t("sgbureau.panel.closed"), embed=None, view=None)


class RegistryHomeView(SGRequesterView):
    def __init__(self, requester_id: int) -> None:
        super().__init__(requester_id)

    @discord.ui.button(label=t("sgbureau.registry.clients_button"), style=discord.ButtonStyle.secondary)
    async def clients(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await show_registry_page(interaction, self.requester_id, "clients", 0, None)

    @discord.ui.button(label=t("sgbureau.registry.lawyers_button"), style=discord.ButtonStyle.secondary)
    async def lawyers(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await show_registry_page(interaction, self.requester_id, "lawyers", 0, None)

    @discord.ui.button(label=t("sgbureau.registry.search_clients_button"), style=discord.ButtonStyle.secondary)
    async def search_clients(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RegistrySearchModal(self.requester_id, "clients"))

    @discord.ui.button(label=t("sgbureau.registry.search_lawyers_button"), style=discord.ButtonStyle.secondary)
    async def search_lawyers(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RegistrySearchModal(self.requester_id, "lawyers"))

    @discord.ui.button(label=t("sgbureau.panel.back_button"), style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        await interaction.response.edit_message(embed=build_bureau_dashboard_embed(interaction.guild), view=SGBureauDashboardView(self.requester_id), allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label=t("sgbureau.panel.exit_button"), style=discord.ButtonStyle.danger)
    async def exit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=t("sgbureau.panel.closed"), embed=None, view=None)


class RegistrySearchModal(discord.ui.Modal):
    def __init__(self, requester_id: int, kind: str) -> None:
        super().__init__(title=t("sgbureau.registry.search_modal_title"), timeout=300)
        self.requester_id = requester_id
        self.kind = kind
        self.query = discord.ui.TextInput(label=t("sgbureau.registry.search_modal_label"), placeholder=t("sgbureau.registry.search_modal_placeholder"), min_length=1, max_length=80, required=True)
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(t("sgbureau.sg.menu_not_for_you"), ephemeral=True)
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not is_bureau_staff(interaction.user):
            await interaction.response.send_message(t("sgbureau.errors.no_permission"), ephemeral=True)
            return
        await show_registry_page(interaction, self.requester_id, self.kind, 0, str(self.query.value).strip(), new_response=True)


class RegistryNumberButton(discord.ui.Button):
    def __init__(self, parent: "RegistryPageView", index: int, item: object) -> None:
        super().__init__(label=str(index + 1), style=discord.ButtonStyle.secondary, row=2 if index < 5 else 3)
        self.parent_view = parent
        self.index = index
        self.item = item

    async def callback(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        if self.parent_view.kind == "clients":
            embed = build_client_profile_detail_embed(self.item, interaction.guild)  # type: ignore[arg-type]
        else:
            embed = build_lawyer_profile_detail_embed(self.item, interaction.guild)  # type: ignore[arg-type]
        await interaction.response.edit_message(embed=embed, view=RegistryDetailView(self.parent_view.requester_id, self.parent_view.kind, self.parent_view.page, self.parent_view.query), allowed_mentions=discord.AllowedMentions.none())


class RegistryPageView(SGRequesterView):
    def __init__(self, requester_id: int, kind: str, page: int, query: str | None, total: int, items: list[object]) -> None:
        super().__init__(requester_id)
        self.kind = kind
        self.page = page
        self.query = query
        self.total = total
        self.items = items
        self.pages = page_count(total)
        for i, item in enumerate(items[:10]):
            self.add_item(RegistryNumberButton(self, i, item))

    @discord.ui.button(label="⬅", style=discord.ButtonStyle.secondary, row=0)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_registry_page(interaction, self.requester_id, self.kind, max(0, self.page - 1), self.query)

    @discord.ui.button(label=t("sgbureau.panel.back_button"), style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=build_registry_home_embed(), view=RegistryHomeView(self.requester_id), allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="➡", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_registry_page(interaction, self.requester_id, self.kind, min(self.pages - 1, self.page + 1), self.query)

    @discord.ui.button(label=t("sgbureau.registry.search_button"), style=discord.ButtonStyle.secondary, row=1)
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RegistrySearchModal(self.requester_id, self.kind))

    @discord.ui.button(label=t("sgbureau.panel.exit_button"), style=discord.ButtonStyle.danger, row=1)
    async def exit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=t("sgbureau.panel.closed"), embed=None, view=None)


class RegistryDetailView(SGRequesterView):
    def __init__(self, requester_id: int, kind: str, page: int, query: str | None) -> None:
        super().__init__(requester_id)
        self.kind = kind
        self.page = page
        self.query = query

    @discord.ui.button(label=t("sgbureau.panel.back_button"), style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_registry_page(interaction, self.requester_id, self.kind, self.page, self.query)

    @discord.ui.button(label=t("sgbureau.panel.exit_button"), style=discord.ButtonStyle.danger)
    async def exit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=t("sgbureau.panel.closed"), embed=None, view=None)


async def show_registry_page(interaction: discord.Interaction, requester_id: int, kind: str, page: int, query: str | None, new_response: bool = False) -> None:
    assert interaction.guild is not None
    if kind == "lawyers":
        total, items = await asyncio.gather(
            asyncio.to_thread(
                storage.count_lawyer_profiles,
                interaction.guild.id,
                query,
            ),
            asyncio.to_thread(
                storage.list_lawyer_profiles_page,
                interaction.guild.id,
                page=page,
                per_page=10,
                query=query,
            ),
        )
    else:
        kind = "clients"
        total, items = await asyncio.gather(
            asyncio.to_thread(
                storage.count_client_profiles,
                interaction.guild.id,
                query,
            ),
            asyncio.to_thread(
                storage.list_client_profiles_page,
                interaction.guild.id,
                page=page,
                per_page=10,
                query=query,
            ),
        )
    pages = page_count(total)
    page = min(max(0, page), pages - 1)
    embed = build_registry_page_embed(kind, interaction.guild.id, page, query, items, total)
    view = RegistryPageView(requester_id, kind, page, query, total, items)
    if new_response:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.edit_message(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())


def build_case_creation_embed(
    guild: discord.Guild,
    *,
    client_id: int,
    lawyer_id: int,
    secretary_id: int | None,
) -> discord.Embed:
    client = guild.get_member(int(client_id))
    lawyer = guild.get_member(int(lawyer_id))
    secretary = (
        guild.get_member(int(secretary_id))
        if secretary_id is not None
        else None
    )
    embed = base_embed(
        "Создание кейса SGL",
        (
            "Проверьте состав приватного кейса. Ведущего адвоката и "
            "секретаря можно выбрать ниже до создания канала."
        ),
    )
    embed.add_field(
        name=t("sgbureau.case.client_field"),
        value=client.mention if client else f"<@{int(client_id)}>",
        inline=False,
    )
    embed.add_field(
        name=t("sgbureau.case.lawyer_field"),
        value=lawyer.mention if lawyer else f"<@{int(lawyer_id)}>",
        inline=True,
    )
    embed.add_field(
        name=t("sgbureau.case.secretary_field"),
        value=(
            secretary.mention
            if secretary is not None
            else t("sgbureau.case.no_secretary")
        ),
        inline=True,
    )
    embed.add_field(
        name="Доступ",
        value=(
            "Клиент, ведущий адвокат и выбранный секретарь получат доступ "
            "к каналу сразу после создания."
        ),
        inline=False,
    )
    embed.set_footer(
        text="SGL Bureau • параметры можно изменить до подтверждения"
    )
    return embed


class SGLCaseMemberUnavailableError(RuntimeError):
    pass


async def create_sgl_case_from_selection(
    *,
    bot: commands.Bot,
    guild: discord.Guild,
    created_by: discord.Member,
    client_id: int,
    lawyer_id: int,
    secretary_id: int | None,
) -> tuple[storage.SGLCase, discord.TextChannel]:
    client, lawyer, secretary = await asyncio.gather(
        fetch_member_safe(guild, int(client_id)),
        fetch_member_safe(guild, int(lawyer_id)),
        (
            fetch_member_safe(guild, int(secretary_id))
            if secretary_id is not None
            else asyncio.sleep(0, result=None)
        ),
    )
    if client is None or lawyer is None or (
        secretary_id is not None and secretary is None
    ):
        raise SGLCaseMemberUnavailableError("sgl_case_member_unavailable")
    return await create_sgl_case_channel(
        bot=bot,
        guild=guild,
        created_by=created_by,
        client=client,
        lawyer=lawyer,
        secretary=secretary,
    )


class CaseCreationMemberSelect(discord.ui.UserSelect):
    def __init__(
        self,
        parent_view: "SGCaseCreationView",
        *,
        kind: str,
    ) -> None:
        self.parent_view = parent_view
        self.kind = kind
        super().__init__(
            placeholder=(
                "Выберите ведущего адвоката"
                if kind == "lawyer"
                else "Выберите секретаря"
            ),
            min_values=0,
            max_values=1,
            row=0 if kind == "lawyer" else 1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_id = int(self.values[0].id) if self.values else None
        if self.kind == "lawyer":
            self.parent_view.lawyer_id = (
                selected_id or SGBUREAU_DEFAULT_LAWYER_ID
            )
        else:
            self.parent_view.secretary_id = selected_id
        assert interaction.guild is not None
        await interaction.response.edit_message(
            embed=self.parent_view.build_embed(interaction.guild),
            view=self.parent_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class SGCaseCreationView(SGRequesterView):
    def __init__(
        self,
        bot: commands.Bot,
        requester_id: int,
        client_id: int,
    ) -> None:
        super().__init__(requester_id, timeout=300)
        self.bot = bot
        self.client_id = int(client_id)
        self.lawyer_id = int(SGBUREAU_DEFAULT_LAWYER_ID)
        self.secretary_id: int | None = None
        self.creating = False
        self.add_item(CaseCreationMemberSelect(self, kind="lawyer"))
        self.add_item(CaseCreationMemberSelect(self, kind="secretary"))

    def build_embed(self, guild: discord.Guild) -> discord.Embed:
        return build_case_creation_embed(
            guild,
            client_id=self.client_id,
            lawyer_id=self.lawyer_id,
            secretary_id=self.secretary_id,
        )

    def set_disabled(self, disabled: bool) -> None:
        for item in self.children:
            item.disabled = bool(disabled)

    @discord.ui.button(
        label="Создать кейс",
        style=discord.ButtonStyle.success,
        row=2,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        assert (
            interaction.guild is not None
            and isinstance(interaction.user, discord.Member)
        )
        if self.creating:
            await interaction.response.send_message(
                "Кейс уже создаётся. Подождите завершения операции.",
                ephemeral=True,
            )
            return
        self.creating = True
        self.set_disabled(True)
        await interaction.response.defer(ephemeral=True)
        await interaction.edit_original_response(
            embed=self.build_embed(interaction.guild),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        try:
            case, channel = await create_sgl_case_from_selection(
                bot=self.bot,
                guild=interaction.guild,
                created_by=interaction.user,
                client_id=self.client_id,
                lawyer_id=self.lawyer_id,
                secretary_id=self.secretary_id,
            )
        except SGLCaseMemberUnavailableError:
            self.creating = False
            self.set_disabled(False)
            await interaction.edit_original_response(
                embed=self.build_embed(interaction.guild),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await interaction.followup.send(
                "Один из выбранных участников больше не доступен на сервере. "
                "Обновите выбор и повторите.",
                ephemeral=True,
            )
            return
        except Exception as exc:
            self.creating = False
            self.set_disabled(False)
            await interaction.edit_original_response(
                embed=self.build_embed(interaction.guild),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await interaction.followup.send(
                t(
                    "sgbureau.sg.create_case_failed",
                    error=str(exc)[:180],
                ),
                ephemeral=True,
            )
            return

        self.stop()
        await interaction.edit_original_response(
            content=t(
                "sgbureau.sg.create_case_success",
                case_number=format_case_number(case.case_number),
                channel_id=channel.id,
            ),
            embed=None,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Без секретаря",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def clear_secretary(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        assert interaction.guild is not None
        self.secretary_id = None
        await interaction.response.edit_message(
            embed=self.build_embed(interaction.guild),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Адвокат по умолчанию",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def reset_lawyer(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        assert interaction.guild is not None
        self.lawyer_id = int(SGBUREAU_DEFAULT_LAWYER_ID)
        await interaction.response.edit_message(
            embed=self.build_embed(interaction.guild),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Назад",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def back(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        assert interaction.guild is not None
        target = await fetch_member_safe(interaction.guild, self.client_id)
        if target is None:
            await interaction.response.send_message(
                t(
                    "sgbureau.sg.user_not_found",
                    user_id=self.client_id,
                ),
                ephemeral=True,
            )
            return
        cases = await asyncio.to_thread(
            storage.list_sgl_cases_for_client,
            interaction.guild.id,
            target.id,
            limit=25,
        )
        await interaction.response.edit_message(
            embed=build_user_overview_embed(target, cases),
            view=SGUserOverviewView(
                self.bot,
                self.requester_id,
                self.client_id,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )


class SGUserOverviewView(discord.ui.View):
    def __init__(self, bot: commands.Bot, requester_id: int, target_user_id: int) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.requester_id = requester_id
        self.target_user_id = target_user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(t("sgbureau.sg.menu_not_for_you"), ephemeral=True)
            return False
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return False
        if not is_bureau_staff(interaction.user):
            await interaction.response.send_message(t("sgbureau.errors.no_permission"), ephemeral=True)
            return False
        return True

    async def _target_member(self, guild: discord.Guild) -> discord.Member | None:
        return await fetch_member_safe(guild, self.target_user_id)

    @discord.ui.button(label=t("sgbureau.sg.create_case_button"), style=discord.ButtonStyle.secondary)
    async def create_case(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        target = await self._target_member(interaction.guild)
        if target is None:
            await interaction.response.send_message(t("sgbureau.sg.user_not_found", user_id=self.target_user_id), ephemeral=True)
            return
        view = SGCaseCreationView(
            self.bot,
            interaction.user.id,
            target.id,
        )
        await interaction.response.edit_message(
            embed=view.build_embed(interaction.guild),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label=t("sgbureau.sg.refresh_user_button"), style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert interaction.guild is not None
        target = await self._target_member(interaction.guild)
        if target is None:
            await interaction.response.send_message(t("sgbureau.sg.user_not_found", user_id=self.target_user_id), ephemeral=True)
            return
        cases = await asyncio.to_thread(
            storage.list_sgl_cases_for_client,
            interaction.guild.id,
            target.id,
            limit=25,
        )
        await interaction.response.edit_message(embed=build_user_overview_embed(target, cases), view=self, allowed_mentions=discord.AllowedMentions.none())


class SGCasePanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot, requester_id: int, case: storage.SGLCase) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.requester_id = requester_id
        self.case_number = case.case_number
        if case.channel_id:
            self.add_item(discord.ui.Button(label=t("sgbureau.sg.open_channel_button"), style=discord.ButtonStyle.link, url=f"https://discord.com/channels/{case.guild_id}/{case.channel_id}"))
        if case.claim_link:
            self.add_item(discord.ui.Button(label=t("sgbureau.sg.open_claim_button"), style=discord.ButtonStyle.link, url=case.claim_link))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(t("sgbureau.sg.menu_not_for_you"), ephemeral=True)
            return False
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return False
        case = self._case(interaction)
        if case is None:
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return False
        if not can_work_with_case(interaction.user, case) and not is_bureau_staff(interaction.user):
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_participant"), ephemeral=True)
            return False
        return True

    def _case(self, interaction: discord.Interaction) -> storage.SGLCase | None:
        if interaction.guild is None:
            return None
        return storage.get_sgl_case_by_number(interaction.guild.id, self.case_number)

    @discord.ui.button(label=t("sgbureau.sg.params_button"), style=discord.ButtonStyle.secondary)
    async def init_params(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        case = self._case(interaction)
        if case is None:
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        await interaction.response.send_message(t("sgbureau.case.service_select_intro", case_number=format_case_number(case.case_number)), view=ServiceTypeView(case.case_number), ephemeral=True)

    @discord.ui.button(label=t("sgbureau.sg.add_link_button"), style=discord.ButtonStyle.secondary)
    async def add_link(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        case = self._case(interaction)
        if case is None:
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if interaction.channel_id != case.channel_id:
            await interaction.response.send_message(t("sgbureau.sg.case_action_only_in_case_channel", channel_id=case.channel_id or 0), ephemeral=True)
            return
        await interaction.response.send_modal(CaseLinkModal(self.bot, case))

    @discord.ui.button(label=t("sgbureau.sg.close_case_button"), style=discord.ButtonStyle.danger)
    async def close_case(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        case = self._case(interaction)
        if case is None:
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not can_close_case(interaction.user, case):
            await interaction.response.send_message(t("sgbureau.case.errors.close_no_permission"), ephemeral=True)
            return
        if interaction.channel_id != case.channel_id:
            await interaction.response.send_message(t("sgbureau.sg.case_action_only_in_case_channel", channel_id=case.channel_id or 0), ephemeral=True)
            return
        await interaction.response.send_modal(CloseCaseModal(self.bot, case))

    @discord.ui.button(label=t("sgbureau.receipt.create_button"), style=discord.ButtonStyle.secondary)
    async def create_receipt(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        case = self._case(interaction)
        if case is None:
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or (not can_close_case(interaction.user, case) and not is_bureau_staff(interaction.user)):
            await interaction.response.send_message(t("sgbureau.receipt.no_permission"), ephemeral=True)
            return
        if interaction.channel_id != case.channel_id:
            await interaction.response.send_message(t("sgbureau.sg.case_action_only_in_case_channel", channel_id=case.channel_id or 0), ephemeral=True)
            return
        await interaction.response.send_modal(ReceiptModal(self.bot, case))

    @discord.ui.button(label=t("sgbureau.contract.button"), style=discord.ButtonStyle.secondary)
    async def create_contract(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        case = self._case(interaction)
        if case is None:
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not can_work_with_case(interaction.user, case):
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_participant"), ephemeral=True)
            return
        if interaction.channel_id != case.channel_id:
            await interaction.response.send_message(t("sgbureau.sg.case_action_only_in_case_channel", channel_id=case.channel_id or 0), ephemeral=True)
            return
        await interaction.response.send_modal(SGLContractModal(self.bot, case, interaction.guild))

    @discord.ui.button(label=t("sgbureau.admin.case_edit_button"), style=discord.ButtonStyle.secondary)
    async def manual_case_edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        case = self._case(interaction)
        if case is None:
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not is_bureau_staff(interaction.user):
            await interaction.response.send_message(t("sgbureau.errors.no_permission"), ephemeral=True)
            return
        await interaction.response.send_modal(AdminEditModal(self.requester_id, "edit"))

    @discord.ui.button(label=t("sgbureau.admin.case_delete_button"), style=discord.ButtonStyle.danger)
    async def manual_case_delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        case = self._case(interaction)
        if case is None:
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not is_bureau_staff(interaction.user):
            await interaction.response.send_message(t("sgbureau.errors.no_permission"), ephemeral=True)
            return
        await interaction.response.send_modal(AdminEditModal(self.requester_id, "delete"))

    @discord.ui.button(label=t("sgbureau.sg.refresh_case_button"), style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        case = self._case(interaction)
        if case is None or interaction.guild is None:
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        await interaction.response.edit_message(embed=build_case_panel_embed(case, interaction.guild), view=SGCasePanelView(self.bot, self.requester_id, case), allowed_mentions=discord.AllowedMentions.none())


async def ensure_command_channel(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
        return False
    if interaction.channel_id != SGBUREAU_COMMAND_CHANNEL_ID:
        await interaction.response.send_message(t("sgbureau.errors.wrong_channel", allowed_channel_id=SGBUREAU_COMMAND_CHANNEL_ID), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return False
    if not is_bureau_staff(interaction.user):
        await interaction.response.send_message(t("sgbureau.errors.no_permission"), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return False
    return True


def register_sgbureau_persistent_views(bot: commands.Bot) -> None:
    bot.add_view(CaseInitView())
    bot.add_view(ReceiptSubmitProofsView(bot))
    bot.add_view(ReceiptConfirmView(bot))


def setup_sgbureau(bot: commands.Bot, remember_command_activity: Callable[[discord.Interaction, str, str], None]) -> None:


    @bot.tree.command(name=SG_COMMAND_NAME, description=SG_COMMAND_DESCRIPTION)
    @app_commands.guild_only()
    @app_commands.rename(case_number="case")
    @app_commands.describe(
        user=safe_command_description("sgbureau.commands.sg_user_description", "SGL client/user overview"),
        case_number=safe_command_description("sgbureau.commands.sg_case_description", "SGL case number"),
    )
    async def sg(interaction: discord.Interaction, user: discord.Member | None = None, case_number: app_commands.Range[int, 1, 999999999] | None = None) -> None:
        remember_command_activity(interaction, "command_sg", t("details.command_sg"))
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return

        has_args = user is not None or case_number is not None
        if user is not None and case_number is not None:
            await interaction.response.send_message(t("sgbureau.sg.errors.only_one_arg"), ephemeral=True)
            return

        if has_args:
            if not await ensure_command_channel(interaction):
                return
            await interaction.response.defer(ephemeral=True)
            if user is not None:
                cases = await asyncio.to_thread(
                    storage.list_sgl_cases_for_client,
                    interaction.guild.id,
                    user.id,
                    limit=25,
                )
                await interaction.followup.send(
                    embed=build_user_overview_embed(user, cases),
                    view=SGUserOverviewView(bot, interaction.user.id, user.id),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            assert case_number is not None
            case = await asyncio.to_thread(
                storage.get_sgl_case_by_number,
                interaction.guild.id,
                int(case_number),
            )
            if case is None:
                await interaction.followup.send(t("sgbureau.sg.errors.case_not_found", case_number=format_case_number(int(case_number))), ephemeral=True)
                return
            await interaction.followup.send(
                embed=build_case_panel_embed(case, interaction.guild),
                view=SGCasePanelView(bot, interaction.user.id, case),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # Without arguments, /sg is a case management command and must be used inside a case channel.
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_channel,
            interaction.guild.id,
            interaction.channel_id or 0,
        )
        if case is None:
            if interaction.channel_id == SGBUREAU_COMMAND_CHANNEL_ID:
                if not is_bureau_staff(interaction.user):
                    await interaction.response.send_message(t("sgbureau.errors.no_permission"), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                    return
                await interaction.response.send_message(
                    embed=build_bureau_dashboard_embed(interaction.guild),
                    view=SGBureauDashboardView(interaction.user.id),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(t("sgbureau.sg.errors.no_case_here"), ephemeral=True)
            return
        if not can_work_with_case(interaction.user, case):
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_participant"), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=build_case_panel_embed(case, interaction.guild),
            view=SGCasePanelView(bot, interaction.user.id, case),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(name=SG_ADDNEWS_COMMAND_NAME, description=SG_ADDNEWS_COMMAND_DESCRIPTION)
    @app_commands.guild_only()
    async def sg_addnews(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        remember_command_activity(interaction, "command_sg_addnews", t("details.command_sg_addnews"))
        if not await ensure_command_channel(interaction):
            return
        await interaction.response.send_modal(AddNewsModal(bot, interaction))

    @bot.tree.command(name=SG_INITIATE_COMMAND_NAME, description=SG_INITIATE_COMMAND_DESCRIPTION)
    @app_commands.guild_only()
    @app_commands.describe(cs=safe_command_description("sgbureau.commands.sg_initiate_cs_description", "First case number"))
    async def sg_initiate(interaction: discord.Interaction, cs: app_commands.Range[int, 1, 999999999]) -> None:
        remember_command_activity(interaction, "command_sg_initiate", t("details.command_sg_initiate"))
        if not await ensure_command_channel(interaction):
            return
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)

        start_channel = await resolve_text_channel(interaction.guild, bot, SGBUREAU_START_CHANNEL_ID)
        costs_channel = await resolve_text_channel(interaction.guild, bot, SGBUREAU_COSTS_CHANNEL_ID)
        missing = []
        if start_channel is None:
            missing.append(str(SGBUREAU_START_CHANNEL_ID))
        if costs_channel is None:
            missing.append(str(SGBUREAU_COSTS_CHANNEL_ID))
        if missing:
            await interaction.followup.send(t("sgbureau.initiate.channels_missing", channels=", ".join(missing)), ephemeral=True)
            return

        seed_status = await asyncio.to_thread(
            storage.set_sgl_case_seed,
            interaction.guild.id,
            int(cs),
        )
        await start_channel.send(embed=build_initiate_intro_embed(), allowed_mentions=discord.AllowedMentions.none())
        await costs_channel.send(build_initiate_costs_message(), allowed_mentions=discord.AllowedMentions.none())
        await interaction.followup.send(t("sgbureau.initiate.success", start_channel_id=start_channel.id, costs_channel_id=costs_channel.id, cs=cs, seed_status=seed_status), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())




    @bot.tree.command(name=SG_ADMIN_COMMAND_NAME, description=SG_ADMIN_COMMAND_DESCRIPTION)
    @app_commands.guild_only()
    @app_commands.describe(
        target=safe_command_description("sgbureau.commands.sg_admin_target", "case, client, lawyer or receipt"),
        action=safe_command_description("sgbureau.commands.sg_admin_action", "view, edit or delete"),
        identifier=safe_command_description("sgbureau.commands.sg_admin_identifier", "Case number or registry ID"),
        field=safe_command_description("sgbureau.commands.sg_admin_field", "Field name for edit"),
        value=safe_command_description("sgbureau.commands.sg_admin_value", "New value for edit"),
        confirm=safe_command_description("sgbureau.commands.sg_admin_confirm", "Required for delete"),
    )
    @app_commands.choices(
        target=[
            app_commands.Choice(name="case", value="case"),
            app_commands.Choice(name="client", value="client"),
            app_commands.Choice(name="lawyer", value="lawyer"),
            app_commands.Choice(name="receipt", value="receipt"),
        ],
        action=[
            app_commands.Choice(name="view", value="view"),
            app_commands.Choice(name="edit", value="edit"),
            app_commands.Choice(name="delete", value="delete"),
        ],
    )
    async def sg_admin(interaction: discord.Interaction, target: str, action: str, identifier: str, field: str | None = None, value: str | None = None, confirm: str | None = None) -> None:
        remember_command_activity(interaction, "command_sg_admin", "/sg_admin")
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        if not await ensure_command_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        target_clean = (
            await asyncio.to_thread(storage.normalize_admin_target, target)
        ) or target
        action_clean = str(action or "").strip().lower()
        try:
            if action_clean == "view":
                record = await asyncio.to_thread(
                    storage.admin_get_record,
                    interaction.guild.id,
                    target_clean,
                    identifier,
                )
                if record is None:
                    await interaction.followup.send(t("sgbureau.admin.not_found"), ephemeral=True)
                    return
                await interaction.followup.send(embed=build_admin_record_embed(target_clean, identifier, record), ephemeral=True)
                return

            if action_clean == "edit":
                if not field:
                    await interaction.followup.send(t("sgbureau.admin.field_required"), ephemeral=True)
                    return
                record = await asyncio.to_thread(
                    storage.admin_update_record,
                    guild_id=interaction.guild.id,
                    target=target_clean,
                    identifier=identifier,
                    field=field,
                    value=value or "",
                    actor_id=interaction.user.id,
                    actor_display=interaction.user.display_name,
                )
                if record is None:
                    await interaction.followup.send(t("sgbureau.admin.not_found"), ephemeral=True)
                    return
                if target_clean == "case":
                    parsed = int(str(identifier).replace("SGL-", "").lstrip("0") or "0")
                    case = await asyncio.to_thread(
                        storage.get_sgl_case_by_number,
                        interaction.guild.id,
                        parsed,
                    )
                    if case:
                        schedule_case_refresh(bot, interaction.guild, case)
                await interaction.followup.send(embed=build_admin_record_embed(target_clean, identifier, record), content=admin_short_result(target_clean, identifier, field), ephemeral=True)
                return

            if action_clean == "delete":
                if str(confirm or "").strip().upper() not in {"DELETE", "УДАЛИТЬ"}:
                    await interaction.followup.send(t("sgbureau.admin.confirm_failed"), ephemeral=True)
                    return
                record = await asyncio.to_thread(
                    storage.admin_delete_record,
                    guild_id=interaction.guild.id,
                    target=target_clean,
                    identifier=identifier,
                    actor_id=interaction.user.id,
                    actor_display=interaction.user.display_name,
                )
                if record is None:
                    await interaction.followup.send(t("sgbureau.admin.not_found"), ephemeral=True)
                    return
                if target_clean == "case":
                    channel_id = record.get("channel_id")
                    if channel_id:
                        channel = interaction.guild.get_channel(int(channel_id))
                        if isinstance(channel, discord.TextChannel):
                            try:
                                await channel.delete(reason=t("sgbureau.admin.delete_case_reason", actor=member_label(interaction.user)))
                            except discord.HTTPException:
                                pass
                await interaction.followup.send(admin_short_result(target_clean, identifier), ephemeral=True)
                return

            await interaction.followup.send(t("sgbureau.admin.bad_action"), ephemeral=True)
        except ValueError as exc:
            code = str(exc)
            if code == "field_not_allowed":
                fields = ", ".join(
                    await asyncio.to_thread(
                        storage.admin_allowed_fields,
                        target_clean,
                    )
                ) or t("common.no_data")
                await interaction.followup.send(t("sgbureau.admin.field_not_allowed", fields=fields), ephemeral=True)
            else:
                await interaction.followup.send(t("sgbureau.admin.bad_request", error=code), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(t("sgbureau.admin.failed", error=str(exc)[:500]), ephemeral=True)

    @bot.tree.command(name=SG_REGISTRY_COMMAND_NAME, description=SG_REGISTRY_COMMAND_DESCRIPTION)
    @app_commands.describe(kind=safe_command_description("sgbureau.commands.sg_registry_kind", "Registry type: clients or lawyers"), query=safe_command_description("sgbureau.commands.sg_registry_query", "Partial nickname / static / id"))
    async def sg_registry(interaction: discord.Interaction, kind: str, query: str | None = None) -> None:
        remember_command_activity(interaction, "command_sg_registry", "/sg_registry")
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        if not await ensure_command_channel(interaction):
            return
        if not is_bureau_staff(interaction.user):
            await interaction.response.send_message(t("sgbureau.errors.no_permission"), ephemeral=True)
            return
        kind_clean = (kind or "").strip().lower()
        await interaction.response.defer(ephemeral=True)
        if kind_clean in {"lawyer", "lawyers", "адвокат", "адвокаты", "a"}:
            items = await asyncio.to_thread(
                storage.search_lawyer_profiles,
                interaction.guild.id,
                query,
                limit=25,
            )
            await interaction.followup.send(embed=build_registry_embed("lawyer", query, items), ephemeral=True)
        else:
            if not query:
                items = await asyncio.to_thread(
                    storage.list_client_profiles_for_user,
                    interaction.guild.id,
                    interaction.user.id,
                    limit=25,
                )
            else:
                items = await asyncio.to_thread(
                    storage.search_client_profiles,
                    interaction.guild.id,
                    query,
                    limit=25,
                )
            await interaction.followup.send(embed=build_registry_embed("client", query, items), ephemeral=True)

    @bot.tree.command(name=SG_LAWYERADD_COMMAND_NAME, description=SG_LAWYERADD_COMMAND_DESCRIPTION)
    @app_commands.describe(user=safe_command_description("sgbureau.commands.sg_lawyeradd_user", "Discord user (optional)"), nickname=safe_command_description("sgbureau.commands.sg_lawyeradd_nickname", "Lawyer nickname"), static_id=safe_command_description("sgbureau.commands.sg_lawyeradd_static", "Static ID"), bank=safe_command_description("sgbureau.commands.sg_lawyeradd_bank", "Bank account"), phone=safe_command_description("sgbureau.commands.sg_lawyeradd_phone", "Phone"), email=safe_command_description("sgbureau.commands.sg_lawyeradd_email", "Email"), notes=safe_command_description("sgbureau.commands.sg_lawyeradd_notes", "Notes"))
    async def sg_lawyeradd(interaction: discord.Interaction, nickname: str, user: discord.Member | None = None, static_id: str | None = None, bank: str | None = None, phone: str | None = None, email: str | None = None, notes: str | None = None) -> None:
        remember_command_activity(interaction, "command_sg_lawyeradd", "/sg_lawyeradd")
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        if not await ensure_command_channel(interaction):
            return
        if not is_bureau_staff(interaction.user):
            await interaction.response.send_message(t("sgbureau.errors.no_permission"), ephemeral=True)
            return
        profile_id = await asyncio.to_thread(
            storage.save_lawyer_profile,
            guild_id=interaction.guild.id,
            discord_user_id=(user.id if user else None),
            lawyer_nick=nickname.strip(),
            static_id=safe_optional(static_id),
            bank_account=safe_optional(bank),
            phone=safe_optional(phone),
            email=safe_optional(email),
            notes=safe_optional(notes),
        )
        await interaction.response.send_message(t("sgbureau.registry.lawyer_saved", profile_id=profile_id), ephemeral=True)

    # Legacy /sg_newcase hidden. Use /sg user:@client -> Create case.
    @app_commands.guild_only()
    @app_commands.describe(
        client=safe_command_description("sgbureau.commands.sg_newcase_client_description", "Client"),
        lawyer=safe_command_description("sgbureau.commands.sg_newcase_lawyer_description", "Lead lawyer"),
        secretary=safe_command_description("sgbureau.commands.sg_newcase_secretary_description", "Secretary"),
    )
    async def sg_newcase(interaction: discord.Interaction, client: discord.Member, lawyer: discord.Member | None = None, secretary: discord.Member | None = None) -> None:
        remember_command_activity(interaction, "command_sg_newcase", t("details.command_sg_newcase"))
        if not await ensure_command_channel(interaction):
            return
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        await interaction.response.defer(ephemeral=True)

        lead_lawyer = lawyer or await fetch_member_safe(interaction.guild, SGBUREAU_DEFAULT_LAWYER_ID)
        if lead_lawyer is None:
            await interaction.followup.send(t("sgbureau.case.errors.default_lawyer_not_found", user_id=SGBUREAU_DEFAULT_LAWYER_ID), ephemeral=True)
            return

        category = await resolve_category(interaction.guild, bot, SGBUREAU_CATEGORY_ID)
        if category is None:
            await interaction.followup.send(t("sgbureau.case.errors.category_not_found", category_id=SGBUREAU_CATEGORY_ID), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        reserved = await asyncio.to_thread(
            storage.reserve_sgl_case,
            guild_id=interaction.guild.id,
            client_id=client.id,
            client_display=client.display_name,
            lead_lawyer_id=lead_lawyer.id,
            lead_lawyer_display=lead_lawyer.display_name,
            secretary_id=secretary.id if secretary else None,
            secretary_display=secretary.display_name if secretary else None,
            created_by_id=interaction.user.id,
            created_by_display=interaction.user.display_name,
        )
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            client: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
            lead_lawyer: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, manage_messages=True),
        }
        if secretary:
            overwrites[secretary] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, manage_messages=True)
        staff_role = interaction.guild.get_role(SGBUREAU_STAFF_ROLE_ID)
        if staff_role is not None:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, manage_messages=True)
        if interaction.guild.me is not None:
            overwrites[interaction.guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, manage_messages=True, manage_channels=True, manage_threads=True)

        try:
            channel = await interaction.guild.create_text_channel(
                name=case_channel_name(reserved),
                category=category,
                overwrites=overwrites,
                topic=t("sgbureau.case.channel_topic", case_number=format_case_number(reserved.case_number), client=member_label(client), lawyer=member_label(lead_lawyer)),
                reason=t("sgbureau.case.audit_newcase_reason", case_number=format_case_number(reserved.case_number)),
            )
        except discord.HTTPException as exc:
            await asyncio.to_thread(
                storage.mark_sgl_case_error,
                reserved.id,
                f"channel_create_failed:{str(exc)[:200]}",
            )
            await interaction.followup.send(t("sgbureau.case.errors.channel_create_failed", error=str(exc)[:180]), ephemeral=True)
            return

        case = await asyncio.to_thread(
            storage.attach_sgl_case_channel,
            reserved.id,
            channel.id,
        )
        if case is None:
            await interaction.followup.send(t("sgbureau.case.errors.db_case_attach_failed"), ephemeral=True)
            return

        schedule_case_refresh(bot, interaction.guild, case)
        await channel.send(embed=build_case_greeting_embed(case, client, lead_lawyer, secretary), view=CaseInitView(), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        await interaction.followup.send(t("sgbureau.case.newcase_success", case_number=format_case_number(case.case_number), channel_id=channel.id), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # Legacy /sg_clink hidden. Use /sg in a case channel -> Add link.
    @app_commands.guild_only()
    @app_commands.describe(link=safe_command_description("sgbureau.commands.sg_clink_link_description", "Statement link"))
    async def sg_clink(interaction: discord.Interaction, link: str) -> None:
        remember_command_activity(interaction, "command_sg_clink", t("details.command_sg_clink"))
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or interaction.channel_id is None:
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        await interaction.response.defer(thinking=False, ephemeral=False)
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_channel,
            interaction.guild.id,
            interaction.channel_id,
        )
        if case is None:
            await interaction.followup.send(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if not can_close_case(interaction.user, case) and not is_bureau_staff(interaction.user):
            await interaction.followup.send(t("sgbureau.case.errors.link_no_permission"), ephemeral=True)
            return
        clean_link = link.strip()
        if not URL_RE.match(clean_link):
            await interaction.followup.send(t("sgbureau.case.errors.link_url"), ephemeral=True)
            return
        updated = await asyncio.to_thread(
            storage.set_sgl_case_link,
            interaction.guild.id,
            interaction.channel_id,
            clean_link,
            interaction.user.id,
            interaction.user.display_name,
        )
        if updated is None:
            await interaction.followup.send(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        embed = build_clink_embed(updated, clean_link, interaction.user)
        msg = await interaction.followup.send(embed=embed, wait=True, allowed_mentions=discord.AllowedMentions.none())
        schedule_case_refresh(bot, interaction.guild, updated)
        try:
            await msg.pin(reason=t("sgbureau.case.audit_pin_link_reason", case_number=format_case_number(updated.case_number)))
            await asyncio.to_thread(
                storage.set_sgl_case_link_message,
                interaction.guild.id,
                interaction.channel_id,
                msg.id,
            )
        except discord.HTTPException:
            await interaction.followup.send(t("sgbureau.case.link_pin_failed"), ephemeral=True)

    # Legacy /sg_closecase hidden. Use /sg in a case channel -> Close case.
    @app_commands.guild_only()
    async def sg_closecase(interaction: discord.Interaction) -> None:
        remember_command_activity(interaction, "command_sg_closecase", t("details.command_sg_closecase"))
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or interaction.channel_id is None:
            await interaction.response.send_message(t("sgbureau.errors.guild_only"), ephemeral=True)
            return
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_channel,
            interaction.guild.id,
            interaction.channel_id,
        )
        if case is None:
            await interaction.response.send_message(t("sgbureau.case.errors.not_case_channel"), ephemeral=True)
            return
        if not can_close_case(interaction.user, case):
            await interaction.response.send_message(t("sgbureau.case.errors.close_no_permission"), ephemeral=True)
            return
        await interaction.response.send_modal(CloseCaseModal(bot, case))

    async def handle_case_message(message: discord.Message) -> None:
        if message.guild is None or message.author.bot or not isinstance(message.author, discord.Member):
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        content = (message.content or "").strip()
        if not content or content.startswith("/"):
            return
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_channel,
            message.guild.id,
            message.channel.id,
        )
        if case is None:
            return
        if case.status != "awaiting_situation":
            return
        if not can_work_with_case(message.author, case):
            return
        updated = await asyncio.to_thread(
            storage.update_sgl_case_situation,
            message.guild.id,
            message.channel.id,
            content,
            message.author.id,
            message.author.display_name,
        )
        if updated is None:
            return
        schedule_case_refresh(bot, message.guild, updated)
        await message.channel.send(embed=build_situation_embed(updated, message.author, content), allowed_mentions=discord.AllowedMentions.none())


    async def handle_sgbureau_ready() -> None:
        await schedule_pending_case_archives(bot)
        for guild in bot.guilds:
            schedule_case_reorder(bot, guild.id, SGBUREAU_CATEGORY_ID)

    bot.add_listener(handle_sgbureau_ready, "on_ready")
    bot.add_listener(handle_case_message, "on_message")
