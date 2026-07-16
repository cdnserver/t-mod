import asyncio
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

import storage
from localization import LOCALIZATION_FILE, safe_command_description, safe_command_name, t
from modules.sgbureau import SGBUREAU_CATEGORY_ID, SGBUREAU_COMMAND_CHANNEL_ID, SGBUREAU_NEWS_CHANNEL_ID, register_sgbureau_persistent_views, setup_sgbureau
from modules.sglaudio import setup_sglaudio
from modules.zigmund import setup_zigmund
from modules.tvrs import register_tvrs_persistent_views, setup_tvrs, tvrs_ensure_sticky_all
from modules.links import setup_links
from modules.finance import setup_finance
from modules.craft import setup_craft
from modules.control_center import setup_control_center
from modules.market import setup_market
from modules.operations import setup_operations
from modules.technical_log import log_technical_event
from modules.delivery_runtime import setup_delivery


TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
TRACK_ALL_MEMBERS = os.getenv("TRACK_ALL_MEMBERS", "true").lower() in {"1", "true", "yes", "on"}
TRACK_ONLY_ROLE_ID_RAW = os.getenv("TRACK_ONLY_ROLE_ID", "").strip()
TRACK_ONLY_ROLE_ID = int(TRACK_ONLY_ROLE_ID_RAW) if TRACK_ONLY_ROLE_ID_RAW.isdigit() else None
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "Europe/Riga"))

BOT_STATUS_ENABLED = os.getenv("BOT_STATUS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
BOT_STATUS_TEXT = os.getenv("BOT_STATUS_TEXT", "Товарищество - светлый круг").strip()
BOT_STATUS_TYPE = os.getenv("BOT_STATUS_TYPE", "custom").strip().lower()
BOT_ONLINE_STATUS = os.getenv("BOT_ONLINE_STATUS", "online").strip().lower()

TRACK_MESSAGES = os.getenv("TRACK_MESSAGES", "true").lower() in {"1", "true", "yes", "on"}
TRACK_MESSAGE_EDITS = os.getenv("TRACK_MESSAGE_EDITS", "true").lower() in {"1", "true", "yes", "on"}
TRACK_MESSAGE_DELETES = os.getenv("TRACK_MESSAGE_DELETES", "true").lower() in {"1", "true", "yes", "on"}
TRACK_REACTIONS = os.getenv("TRACK_REACTIONS", "true").lower() in {"1", "true", "yes", "on"}
TRACK_VOICE = os.getenv("TRACK_VOICE", "true").lower() in {"1", "true", "yes", "on"}
TRACK_VOICE_STATUS = os.getenv("TRACK_VOICE_STATUS", "true").lower() in {"1", "true", "yes", "on"}
TRACK_TYPING = os.getenv("TRACK_TYPING", "true").lower() in {"1", "true", "yes", "on"}
TRACK_MEMBER_UPDATES = os.getenv("TRACK_MEMBER_UPDATES", "true").lower() in {"1", "true", "yes", "on"}
TRACK_MEMBER_JOIN_LEAVE = os.getenv("TRACK_MEMBER_JOIN_LEAVE", "true").lower() in {"1", "true", "yes", "on"}
TRACK_PRESENCE = os.getenv("TRACK_PRESENCE", "false").lower() in {"1", "true", "yes", "on"}
LOG_PRESENCE_EVENTS = os.getenv("LOG_PRESENCE_EVENTS", "false").lower() in {"1", "true", "yes", "on"}
TRACK_COMMANDS = os.getenv("TRACK_COMMANDS", "true").lower() in {"1", "true", "yes", "on"}
ACTIVITY_DEBOUNCE_SECONDS = int(os.getenv("ACTIVITY_DEBOUNCE_SECONDS", "30"))
RECENT_EVENTS_LIMIT = int(os.getenv("RECENT_EVENTS_LIMIT", "15"))
ACTIVITY_QUEUE_MAXSIZE = int(os.getenv("ACTIVITY_QUEUE_MAXSIZE", "20000"))

SGL_COMMAND_NAME = safe_command_name("commands.sgl_name", "sgl")
SGL_COMMAND_DESCRIPTION = safe_command_description("commands.sgl_description", "Check bot response")
ACTIVITY_COMMAND_NAME = safe_command_name("commands.activity_name", "activity")
ACTIVITY_COMMAND_DESCRIPTION = safe_command_description("commands.activity_description", "Check role activity")

ACTIVITY_TYPE_CHOICES = [
    app_commands.Choice(name="all", value="all"),
    app_commands.Choice(name="tvrs", value="tvrs"),
    app_commands.Choice(name="sgl", value="sgl"),
]

TVRS_EVENT_TYPES = {
    "message",
    "message_edit",
    "message_delete",
    "reaction_add",
    "reaction_remove",
    "voice_join",
    "voice_leave",
    "voice_move",
    "typing",
    "member_join",
    "member_leave",
    "command_sgl",
    "command_activity",
    "command_sg",
    "command_sg_addnews",
    "command_sg_initiate",
    "command_sg_newcase",
    "command_sg_clink",
    "command_sg_closecase",
    "command_sglaudio",
    "command_zigmund",
    "command_sg_registry",
    "command_sg_lawyeradd",
    "command_sg_admin",
    "command_tvrs_setbill",
    "command_tvrs_sticky",
    "command_tvrs",
    "command_finance",
    "command_finance_undo",
    "command_craft",
    "command_market",
}


BOOT_LOG_ENABLED = os.getenv("BOOT_LOG_ENABLED", "true").lower() in {"1", "true", "yes", "on"}


def boot_line(text: str) -> None:
    if BOOT_LOG_ENABLED:
        print(text, flush=True)


def boot_banner() -> None:
    if not BOOT_LOG_ENABLED:
        return
    print("", flush=True)
    print("============================================================", flush=True)
    print(" T-MOD BOOT SEQUENCE", flush=True)
    print(" TVRS | SGL Bureau | Registry | Audio AI | Zigmund AI", flush=True)
    print("============================================================", flush=True)


def boot_module(name: str) -> None:
    boot_line(f"[LOAD] {name} ... OK")


def require_discord_token() -> str:
    token = str(TOKEN or "").strip()
    if token in {"", "paste_new_token_here", "YOUR_TOKEN_HERE"}:
        print(t("console.token_missing"), file=sys.stderr)
        raise SystemExit(1)
    return token


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def format_dt(dt: datetime | None) -> str:
    if dt is None:
        return t("common.no_data")
    return dt.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")


def ago_text(dt: datetime | None) -> str:
    if dt is None:
        return t("common.no_data")

    total_seconds = max(0, int((utc_now() - dt).total_seconds()))
    minutes = total_seconds // 60
    hours = total_seconds // 3600
    days = total_seconds // 86400

    if minutes < 1:
        return t("common.just_now")
    if minutes < 60:
        return t("common.ago_minutes", minutes=minutes)
    if hours < 24:
        return t("common.ago_hours", hours=hours)
    return t("common.ago_days", days=days)


def event_text(event_type: str | None) -> str:
    if not event_type:
        return t("common.no_data")
    value = t(f"events.{event_type}")
    return value if value != f"events.{event_type}" else event_type


def detail_text(key: str, **kwargs: Any) -> str:
    value = t(f"details.{key}", **kwargs)
    return value if value != f"details.{key}" else t("common.details_empty")


def channel_label(channel: Any) -> tuple[int | None, str | None]:
    if channel is None:
        return None, None
    channel_id = getattr(channel, "id", None)
    channel_name = getattr(channel, "name", None)
    if channel_name is None:
        channel_name = str(channel)
    return channel_id, channel_name


def category_label(channel: Any) -> tuple[int | None, str | None]:
    if channel is None:
        return None, None

    category = getattr(channel, "category", None)
    if category is not None:
        return getattr(category, "id", None), getattr(category, "name", None)

    category_id = getattr(channel, "category_id", None)
    if category_id is not None:
        return int(category_id), None

    parent = getattr(channel, "parent", None)
    parent_category = getattr(parent, "category", None)
    if parent_category is not None:
        return getattr(parent_category, "id", None), getattr(parent_category, "name", None)

    parent_category_id = getattr(parent, "category_id", None)
    if parent_category_id is not None:
        return int(parent_category_id), None

    return None, None


def has_tracked_role(member: discord.Member | None) -> bool:
    if member is None or member.bot:
        return False
    if TRACK_ALL_MEMBERS or TRACK_ONLY_ROLE_ID is None:
        return True
    return any(role.id == TRACK_ONLY_ROLE_ID for role in getattr(member, "roles", []))


_activity_queue: asyncio.Queue[dict[str, Any]] | None = None
_activity_dropped = 0


def queue_activity_write(payload: dict[str, Any]) -> None:
    """Put activity writes into a background queue.

    SQLite writes from high-volume events, especially presence updates, must not run
    in the Discord event loop. If they do, Discord interactions can miss the
    3-second acknowledgement window and every command/modal appears to hang.
    """
    global _activity_dropped
    queue = _activity_queue
    if queue is None:
        try:
            storage.remember_activity(**payload)
        except Exception:
            traceback.print_exc()
        return
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        _activity_dropped += 1
        if _activity_dropped == 1 or _activity_dropped % 100 == 0:
            print(f"Activity queue is full; dropped {_activity_dropped} activity events", file=sys.stderr)


async def activity_writer_worker() -> None:
    assert _activity_queue is not None
    while True:
        payload = await _activity_queue.get()
        try:
            await asyncio.to_thread(storage.remember_activity, **payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
        finally:
            _activity_queue.task_done()


def remember_activity(
    member: discord.Member,
    event_type: str,
    channel: Any = None,
    details: str | None = None,
    message_id: int | None = None,
    force: bool = False,
) -> None:
    if not has_tracked_role(member):
        return
    channel_id, channel_name = channel_label(channel)
    category_id, category_name = category_label(channel)
    queue_activity_write({
        "guild_id": member.guild.id,
        "user_id": member.id,
        "display_name": member.display_name,
        "name": member.name,
        "mention": member.mention,
        "is_bot": member.bot,
        "event_type": event_type,
        "event_text": event_text(event_type),
        "channel_id": channel_id,
        "channel_name": channel_name,
        "category_id": category_id,
        "category_name": category_name,
        "details": details,
        "message_id": message_id,
        "debounce_seconds": ACTIVITY_DEBOUNCE_SECONDS,
        "force": force,
    })


def remember_command_activity(interaction: discord.Interaction, event_type: str, details: str) -> None:
    if not TRACK_COMMANDS:
        return
    if isinstance(interaction.user, discord.Member):
        remember_activity(interaction.user, event_type, interaction.channel, details=details, force=True)


def chunk_lines(header: str, lines: list[str], footer: str, limit: int = 1900) -> list[str]:
    chunks: list[str] = []
    current = header

    for line in lines:
        candidate = current + "\n" + line
        if len(candidate + "\n" + footer) > limit:
            chunks.append(current + "\n" + footer)
            current = header + "\n" + line
        else:
            current = candidate

    chunks.append(current + "\n" + footer)
    return chunks


def get_sgl_channel_ids(guild: discord.Guild) -> set[int]:
    ids = {SGBUREAU_COMMAND_CHANNEL_ID, SGBUREAU_NEWS_CHANNEL_ID}
    category = guild.get_channel(SGBUREAU_CATEGORY_ID)
    if isinstance(category, discord.CategoryChannel):
        ids.update(channel.id for channel in category.channels)
    return ids


def activity_scope_label(activity_type: str) -> str:
    return t(f"activity.scope.{activity_type}")


def activity_scope_filter(guild: discord.Guild, activity_type: str) -> dict[str, Any]:
    if activity_type == "tvrs":
        return {"event_types": sorted(TVRS_EVENT_TYPES)}
    if activity_type == "sgl":
        return {
            "category_id": SGBUREAU_CATEGORY_ID,
            "channel_ids": sorted(get_sgl_channel_ids(guild)),
        }
    return {}


def get_summaries_by_scope(guild: discord.Guild, user_ids: list[int], activity_type: str) -> dict[int, storage.ActivitySummary]:
    if activity_type == "all":
        return storage.get_summaries(guild.id, user_ids)
    return storage.get_filtered_summaries(guild.id, user_ids, **activity_scope_filter(guild, activity_type))


def get_recent_events_by_scope(guild: discord.Guild, user_id: int, activity_type: str) -> list[storage.ActivityEvent]:
    return storage.get_recent_events(
        guild.id,
        user_id,
        limit=RECENT_EVENTS_LIMIT,
        **activity_scope_filter(guild, activity_type),
    )


async def get_member_from_raw_payload(payload: discord.RawReactionActionEvent) -> discord.Member | None:
    if payload.guild_id is None or payload.user_id is None:
        return None

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return None

    member = getattr(payload, "member", None)
    if isinstance(member, discord.Member):
        return member

    cached = guild.get_member(payload.user_id)
    if cached is not None:
        return cached

    try:
        return await guild.fetch_member(payload.user_id)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return None


def bot_status_value() -> discord.Status:
    status_map = {
        "online": discord.Status.online,
        "idle": discord.Status.idle,
        "dnd": discord.Status.dnd,
        "do_not_disturb": discord.Status.dnd,
        "invisible": discord.Status.invisible,
        "offline": discord.Status.invisible,
    }
    return status_map.get(BOT_ONLINE_STATUS, discord.Status.online)


def build_bot_activity() -> discord.BaseActivity | None:
    text = BOT_STATUS_TEXT.strip()
    if not BOT_STATUS_ENABLED or not text:
        return None

    if BOT_STATUS_TYPE == "custom":
        return discord.CustomActivity(name=text)
    if BOT_STATUS_TYPE == "watching":
        return discord.Activity(type=discord.ActivityType.watching, name=text)
    if BOT_STATUS_TYPE == "listening":
        return discord.Activity(type=discord.ActivityType.listening, name=text)
    if BOT_STATUS_TYPE == "competing":
        return discord.Activity(type=discord.ActivityType.competing, name=text)
    if BOT_STATUS_TYPE == "streaming":
        return discord.Streaming(name=text, url=os.getenv("BOT_STATUS_STREAM_URL", "https://twitch.tv/discord"))
    return discord.Game(name=text)


async def apply_bot_status() -> None:
    if not BOT_STATUS_ENABLED:
        return
    try:
        await bot.change_presence(status=bot_status_value(), activity=build_bot_activity())
        print(t("console.status_set", status=BOT_ONLINE_STATUS, activity_type=BOT_STATUS_TYPE, text=BOT_STATUS_TEXT))
    except Exception as exc:
        print(t("console.status_failed", error=exc), file=sys.stderr)


class TModBot(commands.Bot):
    async def setup_hook(self) -> None:
        global _activity_queue
        if _activity_queue is None:
            _activity_queue = asyncio.Queue(maxsize=ACTIVITY_QUEUE_MAXSIZE)
            self.loop.create_task(activity_writer_worker())
        try:
            if GUILD_ID:
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(t("console.synced_guild", count=len(synced), guild_id=GUILD_ID))
            else:
                synced = await self.tree.sync()
                print(t("console.synced_global", count=len(synced)))
        except Exception as exc:
            print(t("console.sync_failed", error=exc), file=sys.stderr)
            raise


intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.messages = True
intents.message_content = os.getenv("ENABLE_MESSAGE_CONTENT_INTENT", "true").lower() in {"1", "true", "yes", "on"}
intents.reactions = True
intents.guilds = True
intents.typing = TRACK_TYPING
intents.presences = TRACK_PRESENCE

bot = TModBot(command_prefix="!", intents=intents)


@bot.tree.command(name=SGL_COMMAND_NAME, description=SGL_COMMAND_DESCRIPTION)
async def sgl(interaction: discord.Interaction) -> None:
    remember_command_activity(interaction, "command_sgl", detail_text("command_sgl"))
    await interaction.response.send_message(t("sgl.response"), ephemeral=True)


async def send_activity_report(interaction: discord.Interaction, role: discord.Role, activity_type: str) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send(t("activity.guild_only"), ephemeral=True)
        return

    try:
        if not guild.chunked:
            await guild.chunk(cache=True)
    except discord.Forbidden:
        await interaction.followup.send(t("activity.missing_members_intent"), ephemeral=True)
        return
    except discord.HTTPException:
        pass

    members = sorted([member for member in role.members if not member.bot], key=lambda member: member.display_name.lower())
    summaries = get_summaries_by_scope(guild, [member.id for member in members], activity_type)

    rows: list[tuple[int, str]] = []
    for member in members:
        summary = summaries.get(member.id)
        if summary is None:
            line = t("activity.line_no_data", mention=member.mention, no_data=t("common.no_data"))
            rows.append((10**9, line))
            continue

        last_dt = parse_iso(summary.last_activity_at)
        inactive_seconds = int((utc_now() - last_dt).total_seconds()) if last_dt else 10**18
        inactive_days = inactive_seconds // 86400 if last_dt else 10**9
        last_event = event_text(summary.last_event) if summary.last_event else (summary.last_event_text or t("common.no_data"))
        channel_name = summary.last_channel_name or t("common.channel_empty")
        details = summary.last_details or t("common.details_empty")

        if last_dt is None:
            line = t("activity.line_no_data", mention=member.mention, no_data=t("common.no_data"))
        else:
            line = t(
                "activity.line_active",
                mention=member.mention,
                display_name=member.display_name,
                user_name=member.name,
                user_id=member.id,
                ago=ago_text(last_dt),
                event=last_event,
                event_type=summary.last_event or "",
                datetime=format_dt(last_dt),
                channel=channel_name,
                details=details,
                total_events=summary.total_events,
                scope=activity_scope_label(activity_type),
            )

        rows.append((inactive_days, line))

    rows.sort(key=lambda row: row[0], reverse=True)
    lines = [line for _, line in rows]

    if not lines:
        await interaction.followup.send(
            t("activity.no_members", role_name=role.name, role_id=role.id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    tracked = []
    if TRACK_MESSAGES:
        tracked.append(t("tracked.messages"))
    if TRACK_MESSAGE_EDITS:
        tracked.append(t("tracked.message_edits"))
    if TRACK_MESSAGE_DELETES:
        tracked.append(t("tracked.message_deletes"))
    if TRACK_REACTIONS:
        tracked.append(t("tracked.reactions"))
    if TRACK_VOICE:
        tracked.append(t("tracked.voice"))
    if TRACK_VOICE_STATUS and activity_type == "all":
        tracked.append(t("tracked.voice_status"))
    if TRACK_TYPING:
        tracked.append(t("tracked.typing"))
    if TRACK_MEMBER_UPDATES and activity_type == "all":
        tracked.append(t("tracked.member_updates"))
    if TRACK_MEMBER_JOIN_LEAVE:
        tracked.append(t("tracked.member_join_leave"))
    if TRACK_PRESENCE and activity_type == "all":
        tracked.append(t("tracked.presence"))
    if TRACK_COMMANDS:
        tracked.append(t("tracked.commands"))

    separator = t("common.list_separator")
    header = t(
        "activity.header",
        role_name=role.name,
        role_id=role.id,
        member_count=len(members),
        tracked=separator.join(tracked) if tracked else t("tracked.nothing"),
        scope=activity_scope_label(activity_type),
        activity_type=activity_type,
    )
    footer = t("activity.footer")
    chunks = chunk_lines(header, lines, footer)

    for chunk in chunks:
        await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def send_user_activity_log(interaction: discord.Interaction, member: discord.Member, activity_type: str) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send(t("activity.guild_only"), ephemeral=True)
        return

    events = get_recent_events_by_scope(guild, member.id, activity_type)
    if not events:
        await interaction.followup.send(
            t(
                "activity.user_no_events",
                mention=member.mention,
                display_name=member.display_name,
                scope=activity_scope_label(activity_type),
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    lines: list[str] = []
    for event in events:
        dt = parse_iso(event.at)
        channel = f"#{event.channel_name}" if event.channel_name else t("common.channel_empty")
        details = event.details or t("common.details_empty")
        lines.append(
            t(
                "activity.user_event_line",
                datetime=format_dt(dt),
                ago=ago_text(dt),
                event=event_text(event.event_type),
                event_type=event.event_type,
                channel=channel,
                details=details,
            )
        )

    header = t(
        "activity.user_header",
        mention=member.mention,
        display_name=member.display_name,
        user_id=member.id,
        scope=activity_scope_label(activity_type),
        limit=len(events),
    )
    footer = t("activity.footer")
    chunks = chunk_lines(header, lines, footer)
    for chunk in chunks:
        await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class ActivityRoleSelect(discord.ui.RoleSelect):
    def __init__(self) -> None:
        super().__init__(placeholder=t("activity.menu_placeholder"), min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, ActivityRoleSelectView) and interaction.user.id != view.requester_id:
            await interaction.response.send_message(t("activity.menu_not_for_you"), ephemeral=True)
            return

        selected_role = self.values[0]
        activity_type = view.activity_type if isinstance(view, ActivityRoleSelectView) else "all"
        await interaction.response.defer(ephemeral=True)
        await interaction.edit_original_response(
            content=t(
                "activity.role_selected",
                role_name=selected_role.name,
                role_id=selected_role.id,
                scope=activity_scope_label(activity_type),
                activity_type=activity_type,
            ),
            view=None,
        )
        await send_activity_report(interaction, selected_role, activity_type)


class ActivityRoleSelectView(discord.ui.View):
    def __init__(self, requester_id: int, activity_type: str) -> None:
        super().__init__(timeout=180)
        self.requester_id = requester_id
        self.activity_type = activity_type
        self.add_item(ActivityRoleSelect())


@bot.tree.command(name=ACTIVITY_COMMAND_NAME, description=ACTIVITY_COMMAND_DESCRIPTION)
@app_commands.guild_only()
@app_commands.rename(activity_type="type")
@app_commands.describe(
    activity_type=safe_command_description("activity.options.type_description", "Activity scope"),
    user=safe_command_description("activity.options.user_description", "Show recent actions for a user"),
)
@app_commands.choices(activity_type=ACTIVITY_TYPE_CHOICES)
async def activity(
    interaction: discord.Interaction,
    activity_type: str = "all",
    user: discord.Member | None = None,
) -> None:
    remember_command_activity(interaction, "command_activity", detail_text("command_activity"))

    if interaction.guild is None:
        await interaction.response.send_message(t("activity.guild_only"), ephemeral=True)
        return

    selected_type = activity_type or "all"
    if selected_type not in {"all", "tvrs", "sgl"}:
        selected_type = "all"

    if user is not None:
        await interaction.response.defer(ephemeral=True)
        await send_user_activity_log(interaction, user, selected_type)
        return

    view = ActivityRoleSelectView(requester_id=interaction.user.id, activity_type=selected_type)
    await interaction.response.send_message(
        t("activity.menu_title", scope=activity_scope_label(selected_type), activity_type=selected_type),
        ephemeral=True,
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    error_id = int(utc_now().timestamp())
    print(t("console.interaction_failed", error=f"interaction_error_{error_id}: {error}"), file=sys.stderr)
    traceback.print_exception(type(error), error, error.__traceback__)
    if interaction.guild is not None:
        await log_technical_event(
            bot,
            interaction.guild,
            title=f"Ошибка взаимодействия #{error_id}",
            details=(
                f"Команда: `{getattr(interaction.command, 'qualified_name', 'неизвестно')}`\n"
                f"Канал: <#{interaction.channel_id}>\n"
                f"Пользователь: `{interaction.user.id}`\n"
                f"Ошибка: `{type(error).__name__}: {str(error)[:700]}`"
            ),
            dedupe_key=f"app-command:{type(error).__name__}:{getattr(interaction.command, 'qualified_name', 'unknown')}",
            cooldown_seconds=60,
        )
    message = t("errors.generic_interaction_error_with_id", error_id=error_id)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_message(message: discord.Message) -> None:
    if not TRACK_MESSAGES:
        return
    if message.guild is None or not isinstance(message.author, discord.Member):
        return
    if message.author.bot:
        return
    remember_activity(message.author, "message", message.channel, details=detail_text("message"), message_id=message.id)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    if not TRACK_MESSAGE_EDITS:
        return
    if after.guild is None or not isinstance(after.author, discord.Member):
        return
    if after.author.bot:
        return
    remember_activity(after.author, "message_edit", after.channel, details=detail_text("message_edit"), message_id=after.id)


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if not TRACK_MESSAGE_DELETES:
        return
    if message.guild is None or not isinstance(message.author, discord.Member):
        return
    if message.author.bot:
        return
    remember_activity(message.author, "message_delete", message.channel, details=detail_text("message_delete"), message_id=message.id, force=True)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if not TRACK_REACTIONS:
        return
    member = await get_member_from_raw_payload(payload)
    if not has_tracked_role(member):
        return
    assert member is not None
    guild = member.guild
    channel = guild.get_channel(payload.channel_id)
    remember_activity(member, "reaction_add", channel, details=detail_text("reaction", emoji=str(payload.emoji)), message_id=payload.message_id)
    print(t("console.reaction_add", member=member, emoji=payload.emoji))


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
    if not TRACK_REACTIONS:
        return
    member = await get_member_from_raw_payload(payload)
    if not has_tracked_role(member):
        return
    assert member is not None
    guild = member.guild
    channel = guild.get_channel(payload.channel_id)
    remember_activity(member, "reaction_remove", channel, details=detail_text("reaction", emoji=str(payload.emoji)), message_id=payload.message_id)
    print(t("console.reaction_remove", member=member, emoji=payload.emoji))


def voice_status_changes(before: discord.VoiceState, after: discord.VoiceState) -> list[str]:
    changes = []
    if before.self_mute != after.self_mute or before.mute != after.mute:
        changes.append(detail_text("voice_status_item_mute"))
    if before.self_deaf != after.self_deaf or before.deaf != after.deaf:
        changes.append(detail_text("voice_status_item_deaf"))
    if before.self_stream != after.self_stream:
        changes.append(detail_text("voice_status_item_stream"))
    if before.self_video != after.self_video:
        changes.append(detail_text("voice_status_item_video"))
    if before.suppress != after.suppress:
        changes.append(detail_text("voice_status_item_suppress"))
    if before.requested_to_speak_at != after.requested_to_speak_at:
        changes.append(detail_text("voice_status_item_requested_to_speak"))
    return changes


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    if member.bot:
        return
    if not has_tracked_role(member):
        return

    before_channel = before.channel
    after_channel = after.channel

    if TRACK_VOICE:
        if before_channel is None and after_channel is not None:
            details = detail_text("voice_join", channel=after_channel.name)
            remember_activity(member, "voice_join", after_channel, details=details, force=True)
            print(t("console.voice_join", member=member, channel=after_channel.name))
            return
        if before_channel is not None and after_channel is None:
            details = detail_text("voice_leave", channel=before_channel.name)
            remember_activity(member, "voice_leave", before_channel, details=details, force=True)
            print(t("console.voice_leave", member=member, channel=before_channel.name))
            return
        if before_channel is not None and after_channel is not None and before_channel.id != after_channel.id:
            details = detail_text("voice_move", before_channel=before_channel.name, after_channel=after_channel.name)
            remember_activity(member, "voice_move", after_channel, details=details, force=True)
            print(t("console.voice_move", member=member, before_channel=before_channel.name, after_channel=after_channel.name))
            return

    if TRACK_VOICE_STATUS and after_channel is not None:
        changes = voice_status_changes(before, after)
        if changes:
            separator = t("common.list_separator")
            details = detail_text("voice_status", items=separator.join(changes))
            remember_activity(member, "voice_status", after_channel, details=details)
            print(t("console.voice_status", member=member, details=details))


@bot.event
async def on_typing(channel: discord.abc.Messageable, user: discord.User | discord.Member, when: datetime) -> None:
    if not TRACK_TYPING:
        return
    if not isinstance(user, discord.Member):
        return
    if not has_tracked_role(user):
        return
    remember_activity(user, "typing", channel, details=detail_text("typing"))


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    if not TRACK_MEMBER_UPDATES:
        return
    if not has_tracked_role(after):
        return

    changed = []
    if before.display_name != after.display_name:
        changed.append(detail_text("member_display_name"))
    before_roles = {role.id for role in before.roles}
    after_roles = {role.id for role in after.roles}
    if before_roles != after_roles:
        changed.append(detail_text("member_roles"))

    if changed:
        separator = t("common.list_separator")
        details = detail_text("member_update", items=separator.join(changed))
        remember_activity(after, "member_update", None, details=details, force=True)
        print(t("console.member_update", member=after, details=details))


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if not TRACK_MEMBER_JOIN_LEAVE:
        return
    if not has_tracked_role(member):
        return
    remember_activity(member, "member_join", None, details=detail_text("member_join"), force=True)
    print(t("console.member_join", member=member))


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    if not TRACK_MEMBER_JOIN_LEAVE:
        return
    if member.bot:
        return
    if not has_tracked_role(member):
        return
    remember_activity(member, "member_leave", None, details=detail_text("member_leave"), force=True)
    print(t("console.member_leave", member=member))


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member) -> None:
    if not TRACK_PRESENCE:
        return
    if not has_tracked_role(after):
        return

    if before.status != after.status:
        event_type = f"presence_{after.status}"
        details = detail_text("presence_status", before_status=before.status, after_status=after.status)
        remember_activity(after, event_type, None, details=details)
        if LOG_PRESENCE_EVENTS:
            print(t("console.presence_status", member=after, before_status=before.status, after_status=after.status))
    elif before.activities != after.activities:
        details = detail_text("presence_activity")
        remember_activity(after, "presence_update", None, details=details)
        if LOG_PRESENCE_EVENTS:
            print(t("console.presence_activity", member=after))


_SGBUREAU_VIEWS_REGISTERED = False
_TVRS_VIEWS_REGISTERED = False


@bot.event
async def on_ready() -> None:
    user = bot.user
    if user is None:
        print(t("console.ready_no_user"))
        return

    global _SGBUREAU_VIEWS_REGISTERED, _TVRS_VIEWS_REGISTERED
    if not _SGBUREAU_VIEWS_REGISTERED:
        register_sgbureau_persistent_views(bot)
        _SGBUREAU_VIEWS_REGISTERED = True
    if not _TVRS_VIEWS_REGISTERED:
        register_tvrs_persistent_views(bot)
        _TVRS_VIEWS_REGISTERED = True
    await tvrs_ensure_sticky_all(bot)

    await apply_bot_status()

    print(t("console.ready", user=user, user_id=user.id))
    print(t("console.persistent_localization", path=LOCALIZATION_FILE))
    print(t("console.db_path", path=storage.DATABASE_FILE))
    print(f"Activity queue max size: {ACTIVITY_QUEUE_MAXSIZE}; dropped: {_activity_dropped}")
    print(t("console.tracking_scope", track_all=TRACK_ALL_MEMBERS, role_id=TRACK_ONLY_ROLE_ID))
    print(
        t(
            "console.enabled_trackers",
            messages=TRACK_MESSAGES,
            message_edits=TRACK_MESSAGE_EDITS,
            message_deletes=TRACK_MESSAGE_DELETES,
            reactions=TRACK_REACTIONS,
            voice=TRACK_VOICE,
            voice_status=TRACK_VOICE_STATUS,
            typing=TRACK_TYPING,
            member_updates=TRACK_MEMBER_UPDATES,
            member_join_leave=TRACK_MEMBER_JOIN_LEAVE,
            presence=TRACK_PRESENCE,
            commands=TRACK_COMMANDS,
        )
    )


boot_banner()
boot_module("Durable Delivery")
setup_delivery(bot)
boot_module("TVRS Control Center")
setup_control_center(bot)
boot_module("Operations Center")
setup_operations(bot)
boot_module("TVRS Consensus")
setup_tvrs(bot, remember_command_activity)
setup_links(bot)
boot_module("Treasury Finance")
setup_finance(bot, remember_command_activity)
boot_module("Craft Production")
setup_craft(bot, remember_command_activity)
boot_module("RU15 Market")
setup_market(bot, remember_command_activity)
boot_module("SGL Bureau")
setup_sgbureau(bot, remember_command_activity)
boot_module("SGL Contracts")
boot_module("SGL Audio")
setup_sglaudio(bot, remember_command_activity)
boot_module("Zigmund AI")
setup_zigmund(bot, remember_command_activity)


if __name__ == "__main__":
    runtime_token = require_discord_token()
    boot_line("[DB] SQLite migration check ...")
    storage.init_db()
    boot_line(f"[DB] Ready: {storage.DATABASE_FILE}")
    migration = storage.migrate_legacy_activity_json()
    if migration.get("status") == "imported":
        print(t("console.db_migration", events=migration.get("events", 0), users=migration.get("users", 0), counters=migration.get("counters", 0)))
    else:
        print(t("console.db_migration_skipped", reason=migration.get("reason", "unknown")))
    bot.run(runtime_token)
