import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DATA_DIR = Path(os.getenv("DATA_DIR", "/app/persistent/data"))
DATABASE_FILE = Path(os.getenv("DATABASE_FILE", str(DATA_DIR / "tmod.db")))
LEGACY_ACTIVITY_FILE = Path(os.getenv("LEGACY_ACTIVITY_FILE", str(DATA_DIR / "activity.json")))
CONSENSUS_V2_RESET_ID = "2026-07-16-clean-consensus-v2"
CONSENSUS_RESULT_DEDUP_ID = "2026-07-17-live-result-dedup-v1"

_db_lock = threading.RLock()


@dataclass(slots=True)
class ActivitySummary:
    guild_id: int
    user_id: int
    display_name: str | None
    name: str | None
    mention: str | None
    last_activity_at: str | None
    last_event: str | None
    last_event_text: str | None
    last_channel_id: int | None
    last_channel_name: str | None
    last_category_id: int | None
    last_category_name: str | None
    last_details: str | None
    total_events: int


@dataclass(slots=True)
class ActivityEvent:
    id: int
    guild_id: int
    user_id: int
    event_type: str
    event_text: str | None
    at: str
    channel_id: int | None
    channel_name: str | None
    category_id: int | None
    category_name: str | None
    details: str | None
    message_id: int | None


@dataclass(slots=True)
class MemberProfile:
    guild_id: int
    user_id: int
    status: str
    status_note: str | None
    visibility: str
    show_activity: bool
    show_availability: bool
    show_position: bool
    show_characters: bool
    show_join_date: bool
    theme: str
    primary_character_id: int | None
    dm_notifications: bool
    dm_market: bool
    dm_craft: bool
    dm_consensus: bool
    dm_finance: bool
    dm_system: bool
    quiet_hours_enabled: bool
    quiet_start_minute: int
    quiet_end_minute: int
    created_at: str
    updated_at: str


@dataclass(slots=True)
class ProfileCharacter:
    id: int
    guild_id: int
    user_id: int
    nickname: str
    static_id: str
    position: int
    created_at: str
    updated_at: str




@dataclass(slots=True)
class SGLReceipt:
    id: int
    guild_id: int
    case_id: int
    case_number: int
    channel_id: int | None
    client_id: int
    lead_lawyer_id: int
    created_by_id: int | None
    created_by_display: str | None
    court_code: str
    court_label: str
    court_suffix: str
    total_amount: int
    lawyer_amount: int
    duty_amount: int
    lawyer_bank: str
    duty_bank: str
    invoice_message_id: int | None
    proof_services_url: str | None
    proof_duty_url: str | None
    proof_submitted_by_id: int | None
    proof_submitted_by_display: str | None
    proof_submitted_at: str | None
    confirmation_message_id: int | None
    confirmed_by_id: int | None
    confirmed_by_display: str | None
    confirmed_at: str | None
    status: str
    created_at: str
    updated_at: str

@dataclass(slots=True)
class SGLCase:
    id: int
    guild_id: int
    case_number: int
    channel_id: int | None
    client_id: int
    client_display: str | None
    lead_lawyer_id: int
    lead_lawyer_display: str | None
    secretary_id: int | None
    secretary_display: str | None
    status: str
    request_type: str | None
    client_nick: str | None
    static_id: str | None
    bank_account: str | None
    phone: str | None
    passport_url: str | None
    situation_text: str | None
    situation_author_id: int | None
    situation_author_display: str | None
    claim_link: str | None
    claim_message_id: int | None
    portfolio_message_id: int | None
    portfolio_description: str | None
    created_by_id: int | None
    created_by_display: str | None
    created_at: str
    updated_at: str
    closed_at: str | None
    archived_at: str | None
    archive_category_id: int | None


@dataclass(slots=True)
class SGLCaseArchive:
    id: int
    guild_id: int
    case_id: int | None
    case_number: int
    original_channel_id: int
    original_channel_name: str
    original_topic: str | None
    original_category_id: int | None
    status: str
    message_count: int
    attachment_count: int
    total_bytes: int
    snapshot_started_at: str
    snapshot_completed_at: str | None
    source_deleted_at: str | None
    metadata_json: str
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class SGLArchiveMessage:
    id: int
    archive_id: int
    original_message_id: int
    container_id: int
    container_type: str
    container_name: str
    author_id: int | None
    author_name: str
    author_display: str
    author_avatar_url: str | None
    author_is_bot: bool
    content: str
    embeds_json: str
    attachments_json: str
    stickers_json: str
    reactions_json: str
    components_json: str
    reference_message_id: int | None
    created_at: str
    edited_at: str | None
    pinned: bool
    position: int


@dataclass(slots=True)
class SGLArchiveRestoration:
    id: int
    archive_id: int
    guild_id: int
    restored_channel_id: int
    restored_by_id: int
    restored_by_display: str
    status: str
    restored_at: str
    completed_at: str | None
    expires_at: str
    deleted_at: str | None
    last_error: str | None


@dataclass(slots=True)
class ClientProfile:
    id: int
    guild_id: int
    discord_user_id: int
    client_nick: str
    static_id: str
    bank_account: str
    phone: str
    passport_url: str
    notes: str | None
    last_case_id: int | None
    created_at: str
    updated_at: str
    last_used_at: str | None


@dataclass(slots=True)
class LawyerProfile:
    id: int
    guild_id: int
    discord_user_id: int | None
    lawyer_nick: str
    static_id: str | None
    bank_account: str | None
    phone: str | None
    email: str | None
    passport_url: str | None
    notes: str | None
    created_at: str
    updated_at: str



@dataclass(slots=True)
class TVRSBill:
    id: int
    guild_id: int
    bill_number: int
    channel_id: int | None
    message_id: int | None
    author_id: int
    author_display: str | None
    title: str
    summary: str
    materials: str | None
    status: str
    created_at: str
    updated_at: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATABASE_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _add_column_if_missing(con: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _table_columns(con, table):
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")




def _client_profile_from_row(row: sqlite3.Row | None) -> ClientProfile | None:
    if row is None:
        return None
    return ClientProfile(
        id=int(row["id"]), guild_id=int(row["guild_id"]), discord_user_id=int(row["discord_user_id"]),
        client_nick=str(row["client_nick"] or ""), static_id=str(row["static_id"] or ""),
        bank_account=str(row["bank_account"] or ""), phone=str(row["phone"] or ""),
        passport_url=str(row["passport_url"] or ""), notes=row["notes"], last_case_id=row["last_case_id"],
        created_at=str(row["created_at"]), updated_at=str(row["updated_at"]), last_used_at=row["last_used_at"],
    )


def _lawyer_profile_from_row(row: sqlite3.Row | None) -> LawyerProfile | None:
    if row is None:
        return None
    columns = set(row.keys())
    return LawyerProfile(
        id=int(row["id"]), guild_id=int(row["guild_id"]), discord_user_id=row["discord_user_id"],
        lawyer_nick=str(row["lawyer_nick"] or ""), static_id=row["static_id"], bank_account=row["bank_account"],
        phone=row["phone"], email=row["email"], passport_url=(row["passport_url"] if "passport_url" in columns else None),
        notes=row["notes"], created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
    )


def _tvrs_bill_from_row(row: sqlite3.Row | None) -> TVRSBill | None:
    if row is None:
        return None
    return TVRSBill(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        bill_number=int(row["bill_number"]),
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        author_id=int(row["author_id"]),
        author_display=row["author_display"],
        title=str(row["title"] or ""),
        summary=str(row["summary"] or ""),
        materials=row["materials"],
        status=str(row["status"] or "draft"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )

__all__ = ['DATA_DIR', 'DATABASE_FILE', 'LEGACY_ACTIVITY_FILE', 'CONSENSUS_V2_RESET_ID', 'CONSENSUS_RESULT_DEDUP_ID', '_db_lock', 'ActivitySummary', 'ActivityEvent', 'MemberProfile', 'ProfileCharacter', 'SGLReceipt', 'SGLCase', 'SGLCaseArchive', 'SGLArchiveMessage', 'SGLArchiveRestoration', 'ClientProfile', 'LawyerProfile', 'TVRSBill', 'utc_now_iso', 'connect', '_table_columns', '_add_column_if_missing', '_client_profile_from_row', '_lawyer_profile_from_row', '_tvrs_bill_from_row']
