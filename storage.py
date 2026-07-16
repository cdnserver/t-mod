import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


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


def _backup_before_consensus_reset() -> Path | None:
    """Create a consistent SQLite backup before the one-time destructive reset."""
    if not DATABASE_FILE.exists() or DATABASE_FILE.stat().st_size == 0:
        return None
    source = sqlite3.connect(DATABASE_FILE, timeout=30)
    source.row_factory = sqlite3.Row
    try:
        tables = {
            str(row["name"])
            for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "meta" not in tables or "tvrs_bills" not in tables:
            return None
        marker = source.execute(
            "SELECT value FROM meta WHERE key = ?",
            (_consensus_reset_meta_key(CONSENSUS_V2_RESET_ID),),
        ).fetchone()
        if marker is not None:
            return None
        counts = sum(
            int(source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "tvrs_bills",
                "tvrs_votes",
                "tvrs_live_results",
                "tvrs_consensus_sessions",
                "tvrs_consensus_events",
            )
            if table in tables
        )
        if "bot_actions" in tables:
            counts += int(
                source.execute(
                    "SELECT COUNT(*) FROM bot_actions WHERE module = 'tvrs'"
                ).fetchone()[0]
            )
        if counts <= 0:
            return None
        backup_dir = DATA_DIR / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = backup_dir / f"tmod-before-consensus-reset-{stamp}.db"
        with sqlite3.connect(target) as backup:
            source.backup(backup)
        return target
    finally:
        source.close()


def _consensus_result_dedup_meta_key(migration_id: str) -> str:
    return f"migration:consensus-result-dedup:{str(migration_id).strip()}"


def _backup_before_consensus_result_dedup() -> Path | None:
    """Back up a previously reset live DB before removing legacy duplicates."""

    if not DATABASE_FILE.exists() or DATABASE_FILE.stat().st_size == 0:
        return None
    source = sqlite3.connect(DATABASE_FILE, timeout=30)
    source.row_factory = sqlite3.Row
    try:
        tables = {
            str(row["name"])
            for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "meta" not in tables or "tvrs_live_results" not in tables:
            return None
        # If the destructive reset is still pending, its own full backup is the
        # relevant recovery point and all legacy results will be removed anyway.
        reset_marker = source.execute(
            "SELECT 1 FROM meta WHERE key = ?",
            (_consensus_reset_meta_key(CONSENSUS_V2_RESET_ID),),
        ).fetchone()
        if reset_marker is None:
            return None
        marker = source.execute(
            "SELECT 1 FROM meta WHERE key = ?",
            (_consensus_result_dedup_meta_key(CONSENSUS_RESULT_DEDUP_ID),),
        ).fetchone()
        if marker is not None:
            return None
        duplicates = source.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT session_key, bill_id
                FROM tvrs_live_results
                GROUP BY session_key, bill_id
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        if int(duplicates or 0) <= 0:
            return None
        backup_dir = DATA_DIR / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = backup_dir / f"tmod-before-consensus-result-dedup-{stamp}.db"
        with sqlite3.connect(target) as backup:
            source.backup(backup)
        return target
    finally:
        source.close()


def init_db() -> None:
    with _db_lock:
        _backup_before_consensus_reset()
        _backup_before_consensus_result_dedup()
    with _db_lock, connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS members (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT,
                name TEXT,
                mention TEXT,
                is_bot INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS activity_summary (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                last_activity_at TEXT,
                last_event TEXT,
                last_event_text TEXT,
                last_channel_id INTEGER,
                last_channel_name TEXT,
                last_category_id INTEGER,
                last_category_name TEXT,
                last_details TEXT,
                total_events INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id),
                FOREIGN KEY (guild_id, user_id) REFERENCES members(guild_id, user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS activity_counters (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, event_type),
                FOREIGN KEY (guild_id, user_id) REFERENCES members(guild_id, user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS activity_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_text TEXT,
                at TEXT NOT NULL,
                channel_id INTEGER,
                channel_name TEXT,
                category_id INTEGER,
                category_name TEXT,
                details TEXT,
                message_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (guild_id, user_id) REFERENCES members(guild_id, user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS bureau_announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                author_display TEXT,
                source_channel_id INTEGER,
                target_channel_id INTEGER NOT NULL,
                message_id INTEGER,
                global_channel_id INTEGER,
                global_message_id INTEGER,
                title TEXT,
                content TEXT NOT NULL,
                note TEXT,
                image_url TEXT,
                publish_global INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sgl_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                case_number INTEGER NOT NULL,
                channel_id INTEGER,
                client_id INTEGER NOT NULL,
                client_display TEXT,
                lead_lawyer_id INTEGER NOT NULL,
                lead_lawyer_display TEXT,
                secretary_id INTEGER,
                secretary_display TEXT,
                status TEXT NOT NULL DEFAULT 'reserved',
                request_type TEXT,
                client_nick TEXT,
                static_id TEXT,
                bank_account TEXT,
                phone TEXT,
                passport_url TEXT,
                situation_text TEXT,
                situation_author_id INTEGER,
                situation_author_display TEXT,
                claim_link TEXT,
                claim_message_id INTEGER,
                portfolio_message_id INTEGER,
                portfolio_description TEXT,
                created_by_id INTEGER,
                created_by_display TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                archived_at TEXT,
                archive_category_id INTEGER,
                UNIQUE(guild_id, case_number),
                UNIQUE(guild_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS sgl_case_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER,
                guild_id INTEGER NOT NULL,
                case_number INTEGER NOT NULL,
                actor_id INTEGER,
                actor_display TEXT,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            );


            CREATE TABLE IF NOT EXISTS sgl_receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                case_id INTEGER NOT NULL,
                case_number INTEGER NOT NULL,
                channel_id INTEGER,
                client_id INTEGER NOT NULL,
                lead_lawyer_id INTEGER NOT NULL,
                created_by_id INTEGER,
                created_by_display TEXT,
                court_code TEXT NOT NULL,
                court_label TEXT NOT NULL,
                court_suffix TEXT NOT NULL,
                total_amount INTEGER NOT NULL,
                lawyer_amount INTEGER NOT NULL,
                duty_amount INTEGER NOT NULL,
                lawyer_bank TEXT NOT NULL,
                duty_bank TEXT NOT NULL,
                invoice_message_id INTEGER,
                proof_services_url TEXT,
                proof_duty_url TEXT,
                proof_submitted_by_id INTEGER,
                proof_submitted_by_display TEXT,
                proof_submitted_at TEXT,
                confirmation_message_id INTEGER,
                confirmed_by_id INTEGER,
                confirmed_by_display TEXT,
                confirmed_at TEXT,
                status TEXT NOT NULL DEFAULT 'issued',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );


            CREATE TABLE IF NOT EXISTS client_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                client_nick TEXT NOT NULL,
                static_id TEXT NOT NULL,
                bank_account TEXT NOT NULL,
                phone TEXT NOT NULL,
                passport_url TEXT NOT NULL,
                notes TEXT,
                last_case_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT
            );

            CREATE TABLE IF NOT EXISTS lawyer_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER,
                lawyer_nick TEXT NOT NULL,
                static_id TEXT,
                bank_account TEXT,
                phone TEXT,
                email TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );


            CREATE TABLE IF NOT EXISTS tvrs_bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                bill_number INTEGER NOT NULL,
                channel_id INTEGER,
                message_id INTEGER,
                author_id INTEGER NOT NULL,
                author_display TEXT,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                materials TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(guild_id, bill_number)
            );

            CREATE TABLE IF NOT EXISTS tvrs_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                bill_id INTEGER NOT NULL,
                voter_id INTEGER NOT NULL,
                voter_display TEXT,
                vote TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(bill_id, voter_id)
            );


            CREATE TABLE IF NOT EXISTS tvrs_live_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                session_key TEXT NOT NULL,
                plenary_number INTEGER NOT NULL,
                bill_id INTEGER NOT NULL,
                bill_number INTEGER NOT NULL,
                bill_title TEXT NOT NULL,
                status TEXT NOT NULL,
                internal_percent REAL NOT NULL DEFAULT 0,
                overall_percent REAL NOT NULL DEFAULT 0,
                internal_active INTEGER NOT NULL DEFAULT 0,
                votes_json TEXT,
                veto_by_id INTEGER,
                veto_by_display TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tvrs_consensus_sessions (
                session_key TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                plenary_number INTEGER NOT NULL,
                stage TEXT NOT NULL,
                leader_id INTEGER NOT NULL,
                current_bill_id INTEGER,
                snapshot_json TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS tvrs_consensus_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                actor_id INTEGER,
                actor_display TEXT,
                stage_from TEXT,
                stage_to TEXT,
                details_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS delivery_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 8,
                available_at TEXT NOT NULL,
                lease_owner TEXT,
                lease_token TEXT,
                lease_until TEXT,
                message_id INTEGER,
                last_error TEXT,
                delivered_at TEXT,
                dead_notified_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(topic, dedupe_key)
            );

            CREATE TABLE IF NOT EXISTS audio_generations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_display TEXT,
                prompt TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                seconds_elapsed INTEGER NOT NULL DEFAULT 0,
                output_filename TEXT,
                output_mime TEXT,
                output_size_bytes INTEGER,
                dm_message_id INTEGER,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS finance_daily_prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                report_date TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'open',
                event_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(guild_id, report_date)
            );

            CREATE TABLE IF NOT EXISTS finance_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                event_kind TEXT NOT NULL,
                report_date TEXT,
                prompt_id INTEGER,
                reversed_event_id INTEGER,
                amount INTEGER NOT NULL,
                delta INTEGER,
                balance_before INTEGER,
                balance_after INTEGER,
                reason TEXT,
                captcha_digest TEXT,
                game_code TEXT,
                actor_id INTEGER NOT NULL,
                actor_display TEXT,
                channel_id INTEGER,
                message_id INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(prompt_id)
            );

            CREATE TABLE IF NOT EXISTS finance_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                destination_kind TEXT NOT NULL,
                destination_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                message_id INTEGER,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(event_id, destination_kind, destination_id)
            );

            CREATE TABLE IF NOT EXISTS bot_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                actor_id INTEGER,
                actor_display TEXT,
                module TEXT NOT NULL,
                action_kind TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT,
                summary TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                reversible INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                undone_by_id INTEGER,
                undone_by_display TEXT,
                undone_at TEXT,
                undo_reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS craft_recipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                treasury_cost_per_unit INTEGER NOT NULL DEFAULT 0,
                duration_minutes_per_unit INTEGER NOT NULL,
                max_batch_size INTEGER NOT NULL DEFAULT 10,
                created_by_id INTEGER NOT NULL,
                created_by_display TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(guild_id, product_name)
            );

            CREATE TABLE IF NOT EXISTS craft_recipe_materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipe_id INTEGER NOT NULL,
                material_name TEXT NOT NULL,
                quantity_per_unit INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(recipe_id, material_name)
            );

            CREATE TABLE IF NOT EXISTS craft_recipe_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipe_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                treasury_cost_per_unit INTEGER NOT NULL,
                duration_minutes_per_unit INTEGER NOT NULL,
                max_batch_size INTEGER NOT NULL,
                materials_json TEXT NOT NULL,
                active INTEGER NOT NULL,
                changed_by_id INTEGER,
                changed_by_display TEXT,
                change_kind TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(recipe_id, version)
            );

            CREATE TABLE IF NOT EXISTS craft_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                recipe_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                thread_id INTEGER,
                responsible_id INTEGER NOT NULL,
                responsible_display TEXT,
                created_by_id INTEGER NOT NULL,
                created_by_display TEXT,
                recipe_version INTEGER NOT NULL DEFAULT 1,
                product_name_snapshot TEXT,
                treasury_cost_per_unit_snapshot INTEGER,
                duration_minutes_per_unit_snapshot INTEGER,
                max_batch_size_snapshot INTEGER,
                stage TEXT NOT NULL DEFAULT 'procurement',
                attempts_total INTEGER NOT NULL,
                attempts_queued INTEGER NOT NULL DEFAULT 0,
                attempts_completed INTEGER NOT NULL DEFAULT 0,
                product_stock INTEGER NOT NULL DEFAULT 0,
                final_product_qty INTEGER,
                estimated_unit_price INTEGER,
                market_listed_qty INTEGER NOT NULL DEFAULT 0,
                sold_qty INTEGER NOT NULL DEFAULT 0,
                total_revenue INTEGER NOT NULL DEFAULT 0,
                completion_message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS craft_plan_materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                recipe_material_id INTEGER NOT NULL,
                material_name TEXT NOT NULL,
                quantity_per_unit INTEGER NOT NULL,
                required_total INTEGER NOT NULL,
                stock_quantity INTEGER NOT NULL DEFAULT 0,
                purchased_quantity INTEGER NOT NULL DEFAULT 0,
                spent_total INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                UNIQUE(plan_id, recipe_material_id)
            );

            CREATE TABLE IF NOT EXISTS craft_purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                plan_material_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                actor_display TEXT,
                quantity INTEGER NOT NULL,
                total_cost INTEGER NOT NULL,
                finance_event_id INTEGER,
                finance_code TEXT,
                undone_at TEXT,
                undone_by_id INTEGER,
                undo_reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS craft_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                started_by_id INTEGER NOT NULL,
                started_by_display TEXT,
                started_at TEXT NOT NULL,
                due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                completed_at TEXT,
                finance_event_id INTEGER,
                finance_code TEXT,
                undone_at TEXT,
                undone_by_id INTEGER,
                undo_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS craft_inventory_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                actor_display TEXT,
                materials_json TEXT NOT NULL,
                product_quantity INTEGER NOT NULL,
                note TEXT,
                previous_materials_json TEXT,
                previous_product_quantity INTEGER,
                previous_stage TEXT,
                undone_at TEXT,
                undone_by_id INTEGER,
                undo_reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS craft_market_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                actor_display TEXT,
                quantity INTEGER NOT NULL,
                estimated_unit_price INTEGER,
                undone_at TEXT,
                undone_by_id INTEGER,
                undo_reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS craft_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                actor_display TEXT,
                quantity INTEGER NOT NULL,
                total_amount INTEGER NOT NULL,
                undone_at TEXT,
                undone_by_id INTEGER,
                undo_reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS craft_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                reminder_key TEXT NOT NULL,
                message_id INTEGER,
                created_at TEXT NOT NULL,
                deleted_at TEXT,
                UNIQUE(plan_id, reminder_key)
            );

            CREATE TABLE IF NOT EXISTS craft_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                event_kind TEXT NOT NULL,
                actor_id INTEGER,
                actor_display TEXT,
                details_json TEXT NOT NULL,
                thread_message_id INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS market_catalogs (
                server_id TEXT NOT NULL,
                category TEXT NOT NULL,
                server_name TEXT NOT NULL DEFAULT '',
                record_count INTEGER NOT NULL DEFAULT 0,
                source_updated_at TEXT,
                period_days INTEGER,
                last_attempt_at TEXT,
                last_success_at TEXT,
                last_error TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (server_id, category)
            );

            CREATE TABLE IF NOT EXISTS market_items (
                server_id TEXT NOT NULL,
                category TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                external_id TEXT NOT NULL DEFAULT '',
                item_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                total_count INTEGER NOT NULL DEFAULT 0,
                sold_count INTEGER NOT NULL DEFAULT 0,
                average_price INTEGER,
                min_price INTEGER,
                max_price INTEGER,
                source_updated_at TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (server_id, category, item_id)
            );

            CREATE TABLE IF NOT EXISTS market_item_history (
                server_id TEXT NOT NULL,
                category TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                external_id TEXT NOT NULL DEFAULT '',
                item_name TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                total_count INTEGER NOT NULL DEFAULT 0,
                sold_count INTEGER NOT NULL DEFAULT 0,
                average_price INTEGER,
                min_price INTEGER,
                max_price INTEGER,
                source_updated_at TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (server_id, category, item_id, source_updated_at)
            );

            CREATE TABLE IF NOT EXISTS market_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id INTEGER NOT NULL,
                user_display TEXT,
                guild_id INTEGER,
                server_id TEXT NOT NULL,
                category TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                target_price INTEGER NOT NULL,
                min_quantity INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                last_evaluated_source_at TEXT,
                last_observed_price INTEGER,
                last_observed_quantity INTEGER,
                triggered_at TEXT,
                last_delivery_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(discord_user_id, server_id, category, item_id)
            );

            CREATE TABLE IF NOT EXISTS market_alert_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id INTEGER NOT NULL,
                source_updated_at TEXT NOT NULL,
                item_name TEXT NOT NULL,
                observed_price INTEGER NOT NULL,
                observed_quantity INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                dm_message_id INTEGER,
                last_error TEXT,
                delivered_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(alert_id, source_updated_at),
                FOREIGN KEY (alert_id) REFERENCES market_alerts(id) ON DELETE CASCADE
            );

            """
        )

        # One-click migrations for old tmod.db files.
        _add_column_if_missing(con, "activity_events", "category_id", "INTEGER")
        _add_column_if_missing(con, "activity_events", "category_name", "TEXT")
        _add_column_if_missing(con, "activity_summary", "last_category_id", "INTEGER")
        _add_column_if_missing(con, "activity_summary", "last_category_name", "TEXT")
        _add_column_if_missing(con, "market_items", "external_id", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(con, "market_items", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        _add_column_if_missing(con, "market_item_history", "external_id", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(con, "market_item_history", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        con.execute(
            "UPDATE market_items SET external_id = CAST(item_id AS TEXT) WHERE external_id IS NULL OR external_id = ''"
        )
        con.execute(
            "UPDATE market_item_history SET external_id = CAST(item_id AS TEXT) WHERE external_id IS NULL OR external_id = ''"
        )
        _add_column_if_missing(con, "bureau_announcements", "global_channel_id", "INTEGER")
        _add_column_if_missing(con, "bureau_announcements", "global_message_id", "INTEGER")
        _add_column_if_missing(con, "bureau_announcements", "title", "TEXT")
        _add_column_if_missing(con, "bureau_announcements", "note", "TEXT")
        _add_column_if_missing(con, "bureau_announcements", "image_url", "TEXT")
        _add_column_if_missing(con, "bureau_announcements", "publish_global", "INTEGER NOT NULL DEFAULT 0")
        for column, definition in {
            "channel_id": "INTEGER",
            "client_display": "TEXT",
            "lead_lawyer_display": "TEXT",
            "secretary_id": "INTEGER",
            "secretary_display": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'reserved'",
            "request_type": "TEXT",
            "client_nick": "TEXT",
            "static_id": "TEXT",
            "bank_account": "TEXT",
            "phone": "TEXT",
            "passport_url": "TEXT",
            "situation_text": "TEXT",
            "situation_author_id": "INTEGER",
            "situation_author_display": "TEXT",
            "claim_link": "TEXT",
            "claim_message_id": "INTEGER",
            "portfolio_message_id": "INTEGER",
            "portfolio_description": "TEXT",
            "created_by_id": "INTEGER",
            "created_by_display": "TEXT",
            "closed_at": "TEXT",
            "archived_at": "TEXT",
            "archive_category_id": "INTEGER",
        }.items():
            _add_column_if_missing(con, "sgl_cases", column, definition)


        for column, definition in {
            "channel_id": "INTEGER NOT NULL DEFAULT 0",
            "user_display": "TEXT",
            "prompt": "TEXT NOT NULL DEFAULT ''",
            "model": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'running'",
            "seconds_elapsed": "INTEGER NOT NULL DEFAULT 0",
            "output_filename": "TEXT",
            "output_mime": "TEXT",
            "output_size_bytes": "INTEGER",
            "dm_message_id": "INTEGER",
            "error": "TEXT",
            "completed_at": "TEXT",
        }.items():
            _add_column_if_missing(con, "audio_generations", column, definition)

        for column, definition in {
            "channel_id": "INTEGER",
            "client_id": "INTEGER NOT NULL DEFAULT 0",
            "lead_lawyer_id": "INTEGER NOT NULL DEFAULT 0",
            "created_by_id": "INTEGER",
            "created_by_display": "TEXT",
            "court_code": "TEXT NOT NULL DEFAULT ''",
            "court_label": "TEXT NOT NULL DEFAULT ''",
            "court_suffix": "TEXT NOT NULL DEFAULT ''",
            "total_amount": "INTEGER NOT NULL DEFAULT 0",
            "lawyer_amount": "INTEGER NOT NULL DEFAULT 0",
            "duty_amount": "INTEGER NOT NULL DEFAULT 0",
            "lawyer_bank": "TEXT NOT NULL DEFAULT '215436'",
            "duty_bank": "TEXT NOT NULL DEFAULT '72476'",
            "invoice_message_id": "INTEGER",
            "proof_services_url": "TEXT",
            "proof_duty_url": "TEXT",
            "proof_submitted_by_id": "INTEGER",
            "proof_submitted_by_display": "TEXT",
            "proof_submitted_at": "TEXT",
            "confirmation_message_id": "INTEGER",
            "confirmed_by_id": "INTEGER",
            "confirmed_by_display": "TEXT",
            "confirmed_at": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'issued'",
        }.items():
            _add_column_if_missing(con, "sgl_receipts", column, definition)


        for column, definition in {
            "discord_user_id": "INTEGER NOT NULL DEFAULT 0",
            "client_nick": "TEXT NOT NULL DEFAULT ''",
            "static_id": "TEXT NOT NULL DEFAULT ''",
            "bank_account": "TEXT NOT NULL DEFAULT ''",
            "phone": "TEXT NOT NULL DEFAULT ''",
            "passport_url": "TEXT NOT NULL DEFAULT ''",
            "notes": "TEXT",
            "last_case_id": "INTEGER",
            "last_used_at": "TEXT",
        }.items():
            _add_column_if_missing(con, "client_profiles", column, definition)

        for column, definition in {
            "discord_user_id": "INTEGER",
            "lawyer_nick": "TEXT NOT NULL DEFAULT ''",
            "static_id": "TEXT",
            "bank_account": "TEXT",
            "phone": "TEXT",
            "email": "TEXT",
            "passport_url": "TEXT",
            "notes": "TEXT",
        }.items():
            _add_column_if_missing(con, "lawyer_profiles", column, definition)


        for column, definition in {
            "channel_id": "INTEGER",
            "message_id": "INTEGER",
            "author_display": "TEXT",
            "materials": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'draft'",
        }.items():
            _add_column_if_missing(con, "tvrs_bills", column, definition)

        for column, definition in {
            "voter_display": "TEXT",
        }.items():
            _add_column_if_missing(con, "tvrs_votes", column, definition)


        for column, definition in {
            "original_bill_id": "INTEGER",
            "attempt": "INTEGER NOT NULL DEFAULT 1",
            "vetoed_by_id": "INTEGER",
            "vetoed_by_display": "TEXT",
            "result_summary": "TEXT",
        }.items():
            _add_column_if_missing(con, "tvrs_bills", column, definition)

        _add_column_if_missing(con, "finance_events", "reversed_event_id", "INTEGER")
        _add_column_if_missing(con, "delivery_outbox", "dead_notified_at", "TEXT")

        _add_column_if_missing(con, "craft_recipes", "version", "INTEGER NOT NULL DEFAULT 1")
        for column, definition in {
            "recipe_version": "INTEGER NOT NULL DEFAULT 1",
            "product_name_snapshot": "TEXT",
            "treasury_cost_per_unit_snapshot": "INTEGER",
            "duration_minutes_per_unit_snapshot": "INTEGER",
            "max_batch_size_snapshot": "INTEGER",
        }.items():
            _add_column_if_missing(con, "craft_plans", column, definition)
        for table in ("craft_purchases", "craft_batches", "craft_inventory_checks", "craft_market_listings", "craft_sales"):
            _add_column_if_missing(con, table, "undone_at", "TEXT")
            _add_column_if_missing(con, table, "undone_by_id", "INTEGER")
            _add_column_if_missing(con, table, "undo_reason", "TEXT")
        _add_column_if_missing(con, "craft_inventory_checks", "previous_materials_json", "TEXT")
        _add_column_if_missing(con, "craft_inventory_checks", "previous_product_quantity", "INTEGER")
        _add_column_if_missing(con, "craft_inventory_checks", "previous_stage", "TEXT")

        con.execute(
            """
            UPDATE craft_plans
            SET product_name_snapshot = COALESCE(
                    product_name_snapshot,
                    (SELECT product_name FROM craft_recipes WHERE id = craft_plans.recipe_id)
                ),
                treasury_cost_per_unit_snapshot = COALESCE(
                    treasury_cost_per_unit_snapshot,
                    (SELECT treasury_cost_per_unit FROM craft_recipes WHERE id = craft_plans.recipe_id)
                ),
                duration_minutes_per_unit_snapshot = COALESCE(
                    duration_minutes_per_unit_snapshot,
                    (SELECT duration_minutes_per_unit FROM craft_recipes WHERE id = craft_plans.recipe_id)
                ),
                max_batch_size_snapshot = COALESCE(
                    max_batch_size_snapshot,
                    (SELECT max_batch_size FROM craft_recipes WHERE id = craft_plans.recipe_id)
                ),
                recipe_version = COALESCE(
                    recipe_version,
                    (SELECT version FROM craft_recipes WHERE id = craft_plans.recipe_id),
                    1
                )
            """
        )

        # Indexes are created only after column migrations. This avoids startup crashes
        # on old databases that do not yet have category_id / last_category_id columns.
        con.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_activity_events_user_at
            ON activity_events(guild_id, user_id, at DESC);

            CREATE INDEX IF NOT EXISTS idx_activity_events_scope
            ON activity_events(guild_id, category_id, channel_id, event_type, at DESC);

            CREATE INDEX IF NOT EXISTS idx_activity_summary_last
            ON activity_summary(guild_id, last_activity_at DESC);

            CREATE INDEX IF NOT EXISTS idx_bureau_announcements_guild_created
            ON bureau_announcements(guild_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_cases_guild_number
            ON sgl_cases(guild_id, case_number DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_cases_channel
            ON sgl_cases(guild_id, channel_id);

            CREATE INDEX IF NOT EXISTS idx_sgl_cases_client
            ON sgl_cases(guild_id, client_id, case_number DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_events_case
            ON sgl_case_events(case_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_receipts_case
            ON sgl_receipts(case_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_receipts_invoice_message
            ON sgl_receipts(guild_id, invoice_message_id);

            CREATE INDEX IF NOT EXISTS idx_sgl_receipts_confirmation_message
            ON sgl_receipts(guild_id, confirmation_message_id);

            CREATE INDEX IF NOT EXISTS idx_audio_generations_guild_created
            ON audio_generations(guild_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_audio_generations_user_created
            ON audio_generations(guild_id, user_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_tvrs_bills_number
            ON tvrs_bills(guild_id, bill_number DESC);

            CREATE INDEX IF NOT EXISTS idx_tvrs_votes_bill
            ON tvrs_votes(guild_id, bill_id);


            CREATE INDEX IF NOT EXISTS idx_tvrs_bills_queue
            ON tvrs_bills(guild_id, status, bill_number ASC);

            CREATE INDEX IF NOT EXISTS idx_tvrs_live_results_session
            ON tvrs_live_results(guild_id, session_key, id ASC);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_tvrs_consensus_one_active_guild
            ON tvrs_consensus_sessions(guild_id)
            WHERE finished_at IS NULL;

            CREATE INDEX IF NOT EXISTS idx_tvrs_consensus_events_session
            ON tvrs_consensus_events(session_key, id ASC);

            CREATE INDEX IF NOT EXISTS idx_delivery_outbox_ready
            ON delivery_outbox(status, available_at, lease_until, id ASC);

            CREATE INDEX IF NOT EXISTS idx_client_profiles_user
            ON client_profiles(guild_id, discord_user_id, updated_at DESC);

            CREATE INDEX IF NOT EXISTS idx_client_profiles_search
            ON client_profiles(guild_id, client_nick, static_id);

            CREATE INDEX IF NOT EXISTS idx_lawyer_profiles_search
            ON lawyer_profiles(guild_id, lawyer_nick, static_id);

            CREATE INDEX IF NOT EXISTS idx_finance_events_guild_id
            ON finance_events(guild_id, id DESC);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_finance_events_one_undo
            ON finance_events(reversed_event_id)
            WHERE reversed_event_id IS NOT NULL;

            CREATE INDEX IF NOT EXISTS idx_finance_prompts_status
            ON finance_daily_prompts(guild_id, status, report_date DESC);

            CREATE INDEX IF NOT EXISTS idx_finance_notifications_pending
            ON finance_notifications(status, next_attempt_at, id ASC);

            CREATE INDEX IF NOT EXISTS idx_bot_actions_actor
            ON bot_actions(guild_id, actor_id, status, id DESC);

            CREATE INDEX IF NOT EXISTS idx_bot_actions_target
            ON bot_actions(guild_id, target_type, target_id, status, id DESC);

            CREATE INDEX IF NOT EXISTS idx_bot_actions_module
            ON bot_actions(guild_id, module, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_craft_recipes_guild_active
            ON craft_recipes(guild_id, active, product_name);

            CREATE INDEX IF NOT EXISTS idx_craft_recipe_versions_recipe
            ON craft_recipe_versions(recipe_id, version DESC);

            CREATE INDEX IF NOT EXISTS idx_craft_plans_guild_stage
            ON craft_plans(guild_id, stage, id DESC);

            CREATE INDEX IF NOT EXISTS idx_craft_batches_due
            ON craft_batches(status, due_at, plan_id);

            CREATE INDEX IF NOT EXISTS idx_craft_events_unsent
            ON craft_events(thread_message_id, id ASC);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_craft_purchase_finance_event
            ON craft_purchases(finance_event_id)
            WHERE finance_event_id IS NOT NULL;

            CREATE INDEX IF NOT EXISTS idx_market_items_name
            ON market_items(server_id, category, active, normalized_name);

            CREATE INDEX IF NOT EXISTS idx_market_items_popular
            ON market_items(server_id, category, active, sold_count DESC);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_market_items_external
            ON market_items(server_id, category, external_id);

            CREATE INDEX IF NOT EXISTS idx_market_history_item
            ON market_item_history(server_id, category, item_id, source_updated_at DESC);

            CREATE INDEX IF NOT EXISTS idx_market_history_fetched
            ON market_item_history(fetched_at);

            CREATE INDEX IF NOT EXISTS idx_market_alerts_user
            ON market_alerts(discord_user_id, status, updated_at DESC);

            CREATE INDEX IF NOT EXISTS idx_market_alerts_evaluation
            ON market_alerts(server_id, category, status, last_evaluated_source_at);

            CREATE INDEX IF NOT EXISTS idx_market_alert_notifications_pending
            ON market_alert_notifications(status, next_attempt_at, id);
            """
        )


        # Auto-migrate existing client data from old/current SGL cases into the client registry.
        # This is idempotent: every startup updates an existing profile by
        # (guild_id, discord_user_id, lower(client_nick), static_id) or creates it if missing.
        migrated_case_profiles = 0
        case_profile_rows = con.execute(
            """
            SELECT
                id, guild_id, case_number, client_id, client_nick, static_id,
                bank_account, phone, passport_url, created_at, updated_at
            FROM sgl_cases
            WHERE COALESCE(client_id, 0) != 0
              AND COALESCE(TRIM(client_nick), '') != ''
              AND COALESCE(TRIM(static_id), '') != ''
            ORDER BY COALESCE(updated_at, created_at) ASC, id ASC
            """
        ).fetchall()
        for row in case_profile_rows:
            guild_id = int(row["guild_id"])
            discord_user_id = int(row["client_id"])
            client_nick = str(row["client_nick"] or "").strip()
            static_id = str(row["static_id"] or "").strip()
            if not client_nick or not static_id:
                continue
            bank_account = str(row["bank_account"] or "").strip()
            phone = str(row["phone"] or "").strip()
            passport_url = str(row["passport_url"] or "").strip()
            case_id = int(row["id"])
            case_updated_at = str(row["updated_at"] or row["created_at"] or utc_now_iso())
            existing = con.execute(
                """
                SELECT id FROM client_profiles
                WHERE guild_id = ?
                  AND discord_user_id = ?
                  AND lower(client_nick) = lower(?)
                  AND static_id = ?
                """,
                (guild_id, discord_user_id, client_nick, static_id),
            ).fetchone()
            if existing:
                con.execute(
                    """
                    UPDATE client_profiles
                    SET bank_account = CASE WHEN ? != '' THEN ? ELSE bank_account END,
                        phone = CASE WHEN ? != '' THEN ? ELSE phone END,
                        passport_url = CASE WHEN ? != '' THEN ? ELSE passport_url END,
                        last_case_id = ?,
                        updated_at = ?,
                        last_used_at = ?
                    WHERE id = ?
                    """,
                    (
                        bank_account, bank_account,
                        phone, phone,
                        passport_url, passport_url,
                        case_id,
                        utc_now_iso(),
                        case_updated_at,
                        int(existing["id"]),
                    ),
                )
            else:
                con.execute(
                    """
                    INSERT INTO client_profiles(
                        guild_id, discord_user_id, client_nick, static_id,
                        bank_account, phone, passport_url, notes, last_case_id,
                        created_at, updated_at, last_used_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                    """,
                    (
                        guild_id,
                        discord_user_id,
                        client_nick,
                        static_id,
                        bank_account,
                        phone,
                        passport_url,
                        case_id,
                        str(row["created_at"] or utc_now_iso()),
                        utc_now_iso(),
                        case_updated_at,
                    ),
                )
            migrated_case_profiles += 1
        set_meta(con, "client_profiles_last_case_migration_count", str(migrated_case_profiles))
        set_meta(con, "client_profiles_last_case_migration_at", utc_now_iso())

        _apply_consensus_v2_reset_in_connection(con, CONSENSUS_V2_RESET_ID)
        _apply_consensus_result_dedup_in_connection(con, CONSENSUS_RESULT_DEDUP_ID)
        set_meta(con, "schema_version", "2026-07-17-production-outbox-v1")
        con.commit()


def _consensus_reset_meta_key(reset_id: str) -> str:
    return f"migration:consensus-reset:{str(reset_id).strip()}"


def _apply_consensus_result_dedup_in_connection(
    con: sqlite3.Connection,
    migration_id: str,
) -> dict[str, int | str]:
    meta_key = _consensus_result_dedup_meta_key(migration_id)
    existing = con.execute("SELECT value FROM meta WHERE key = ?", (meta_key,)).fetchone()
    if existing is not None:
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tvrs_live_results_once "
            "ON tvrs_live_results(session_key, bill_id)"
        )
        return {"status": "already_applied", "removed": 0}
    before = int(con.execute("SELECT COUNT(*) AS n FROM tvrs_live_results").fetchone()["n"])
    # Legacy code appended results. The greatest id is therefore the most
    # recent durable interpretation of a duplicated decision.
    con.execute(
        """
        DELETE FROM tvrs_live_results
        WHERE id NOT IN (
            SELECT MAX(id) FROM tvrs_live_results GROUP BY session_key, bill_id
        )
        """
    )
    after = int(con.execute("SELECT COUNT(*) AS n FROM tvrs_live_results").fetchone()["n"])
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tvrs_live_results_once "
        "ON tvrs_live_results(session_key, bill_id)"
    )
    summary: dict[str, int | str] = {"status": "applied", "removed": before - after}
    set_meta(con, meta_key, json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return summary


def _apply_consensus_v2_reset_in_connection(con: sqlite3.Connection, reset_id: str) -> dict[str, int | str]:
    meta_key = _consensus_reset_meta_key(reset_id)
    existing = con.execute("SELECT value FROM meta WHERE key = ?", (meta_key,)).fetchone()
    if existing is not None:
        return {"status": "already_applied", "bills": 0, "votes": 0, "results": 0, "sessions": 0}
    counts = {
        "bills": int(con.execute("SELECT COUNT(*) AS n FROM tvrs_bills").fetchone()["n"]),
        "votes": int(con.execute("SELECT COUNT(*) AS n FROM tvrs_votes").fetchone()["n"]),
        "results": int(con.execute("SELECT COUNT(*) AS n FROM tvrs_live_results").fetchone()["n"]),
        "sessions": int(con.execute("SELECT COUNT(*) AS n FROM tvrs_consensus_sessions").fetchone()["n"]),
    }
    con.execute("DELETE FROM tvrs_consensus_events")
    con.execute("DELETE FROM tvrs_consensus_sessions")
    con.execute("DELETE FROM tvrs_votes")
    con.execute("DELETE FROM tvrs_live_results")
    con.execute("DELETE FROM tvrs_bills")
    con.execute("DELETE FROM bot_actions WHERE module = 'tvrs'")
    con.execute(
        "DELETE FROM meta WHERE key LIKE 'tvrs_next_bill_number:%' OR key LIKE 'tvrs_next_plenary_number:%'"
    )
    con.execute(
        "DELETE FROM sqlite_sequence WHERE name IN "
        "('tvrs_bills', 'tvrs_votes', 'tvrs_live_results', 'tvrs_consensus_events')"
    )
    summary: dict[str, int | str] = {"status": "applied", **counts}
    set_meta(con, meta_key, json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return summary


def tvrs_apply_consensus_v2_reset(reset_id: str = CONSENSUS_V2_RESET_ID) -> dict[str, int | str]:
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        result = _apply_consensus_v2_reset_in_connection(con, reset_id)
        con.commit()
        return result


# ---------------- Durable delivery outbox ----------------

OUTBOX_OPEN_STATUSES = frozenset({"pending", "processing", "retry"})


def delivery_outbox_enqueue_in_connection(
    con: sqlite3.Connection,
    *,
    topic: str,
    dedupe_key: str,
    payload: dict[str, Any],
    max_attempts: int = 8,
    available_at: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Insert once using the caller's transaction.

    Keeping this operation connection-aware is what lets a domain write and its
    delivery intent commit atomically.  A repeated key deliberately keeps the
    first payload: callers may safely retry a transaction after an uncertain
    response without rewriting an already delivered message.
    """

    clean_topic = str(topic).strip()
    clean_key = str(dedupe_key).strip()
    if not clean_topic or not clean_key:
        raise ValueError("outbox_topic_and_dedupe_key_required")
    created_at = str(now or utc_now_iso())
    ready_at = str(available_at or created_at)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    con.execute(
        """
        INSERT INTO delivery_outbox(
            topic, dedupe_key, payload_json, status, attempts, max_attempts,
            available_at, lease_owner, lease_token, lease_until, message_id,
            last_error, delivered_at, created_at, updated_at
        )
        VALUES(?, ?, ?, 'pending', 0, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
        ON CONFLICT(topic, dedupe_key) DO NOTHING
        """,
        (
            clean_topic,
            clean_key,
            payload_json,
            max(1, int(max_attempts)),
            ready_at,
            created_at,
            created_at,
        ),
    )
    row = con.execute(
        "SELECT * FROM delivery_outbox WHERE topic = ? AND dedupe_key = ?",
        (clean_topic, clean_key),
    ).fetchone()
    if row is None:  # pragma: no cover - protected by the insert/select transaction
        raise RuntimeError("outbox_enqueue_failed")
    return dict(row)


def delivery_outbox_enqueue(
    *,
    topic: str,
    dedupe_key: str,
    payload: dict[str, Any],
    max_attempts: int = 8,
    available_at: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = delivery_outbox_enqueue_in_connection(
            con,
            topic=topic,
            dedupe_key=dedupe_key,
            payload=payload,
            max_attempts=max_attempts,
            available_at=available_at,
            now=now,
        )
        con.commit()
        return row


def delivery_outbox_claim(
    *,
    worker_id: str,
    limit: int = 25,
    lease_seconds: int = 90,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Atomically lease ready messages and fence stale workers with a token."""

    clean_worker = str(worker_id).strip()
    if not clean_worker:
        raise ValueError("outbox_worker_id_required")
    now_dt = datetime.fromisoformat(str(now or utc_now_iso()).replace("Z", "+00:00"))
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_iso = now_dt.astimezone(timezone.utc).isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, int(lease_seconds)))).astimezone(timezone.utc).isoformat()
    claimed: list[dict[str, Any]] = []
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        # A worker that died during its final permitted attempt must not cause an
        # infinite lease/reclaim loop.
        con.execute(
            """
            UPDATE delivery_outbox
            SET status = 'dead', lease_owner = NULL, lease_token = NULL,
                lease_until = NULL, last_error = COALESCE(last_error, 'delivery_lease_expired'),
                updated_at = ?
            WHERE status = 'processing' AND lease_until <= ? AND attempts >= max_attempts
            """,
            (now_iso, now_iso),
        )
        rows = con.execute(
            """
            SELECT id FROM delivery_outbox
            WHERE attempts < max_attempts
              AND (
                    (status IN ('pending', 'retry') AND available_at <= ?)
                 OR (status = 'processing' AND lease_until <= ?)
              )
            ORDER BY available_at ASC, id ASC
            LIMIT ?
            """,
            (now_iso, now_iso, max(1, min(int(limit), 100))),
        ).fetchall()
        for selected in rows:
            item_id = int(selected["id"])
            lease_token = uuid.uuid4().hex
            con.execute(
                """
                UPDATE delivery_outbox
                SET status = 'processing', attempts = attempts + 1,
                    lease_owner = ?, lease_token = ?, lease_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (clean_worker, lease_token, lease_until, now_iso, item_id),
            )
            row = con.execute("SELECT * FROM delivery_outbox WHERE id = ?", (item_id,)).fetchone()
            if row is not None:
                claimed.append(dict(row))
        con.commit()
    return claimed


def delivery_outbox_renew_lease(
    item_id: int,
    *,
    lease_token: str,
    lease_seconds: int = 90,
    now: str | None = None,
) -> bool:
    """Extend an active lease while fencing expired or replaced workers."""

    now_dt = datetime.fromisoformat(str(now or utc_now_iso()).replace("Z", "+00:00"))
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_iso = now_dt.astimezone(timezone.utc).isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, int(lease_seconds)))).astimezone(timezone.utc).isoformat()
    with _db_lock, connect() as con:
        cur = con.execute(
            """
            UPDATE delivery_outbox
            SET lease_until = ?, updated_at = ?
            WHERE id = ? AND status = 'processing' AND lease_token = ?
              AND lease_until > ?
            """,
            (lease_until, now_iso, int(item_id), str(lease_token), now_iso),
        )
        con.commit()
        return cur.rowcount == 1


def delivery_outbox_mark_delivered(
    item_id: int,
    *,
    lease_token: str,
    message_id: int | None = None,
    now: str | None = None,
) -> bool:
    completed_at = str(now or utc_now_iso())
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT payload_json FROM delivery_outbox
            WHERE id = ? AND status = 'processing' AND lease_token = ?
            """,
            (int(item_id), str(lease_token)),
        ).fetchone()
        if row is None:
            con.commit()
            return False
        try:
            original_payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            original_payload = {}
        compact_payload: dict[str, Any] = {"compacted": True}
        if isinstance(original_payload, dict):
            for key in ("payload_version", "guild_id", "session_key", "destination"):
                if original_payload.get(key) is not None:
                    compact_payload[key] = original_payload[key]
            result = original_payload.get("result")
            bill = original_payload.get("bill")
            bill_id = (
                dict(result).get("bill_id")
                if isinstance(result, dict)
                else dict(bill).get("id") if isinstance(bill, dict) else original_payload.get("bill_id")
            )
            if bill_id:
                compact_payload["bill_id"] = int(bill_id)
        cur = con.execute(
            """
            UPDATE delivery_outbox
            SET status = 'delivered', message_id = ?, delivered_at = ?,
                lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                last_error = NULL, payload_json = ?, updated_at = ?
            WHERE id = ? AND status = 'processing' AND lease_token = ?
            """,
            (
                int(message_id) if message_id else None,
                completed_at,
                json.dumps(compact_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                completed_at,
                int(item_id),
                str(lease_token),
            ),
        )
        con.commit()
        return cur.rowcount == 1


def delivery_outbox_mark_failed(
    item_id: int,
    *,
    lease_token: str,
    error: str,
    retry_at: str,
    permanent: bool = False,
    now: str | None = None,
) -> bool:
    updated_at = str(now or utc_now_iso())
    clean_error = " ".join(str(error or "delivery_failed").split())[:1000]
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT attempts, max_attempts FROM delivery_outbox
            WHERE id = ? AND status = 'processing' AND lease_token = ?
            """,
            (int(item_id), str(lease_token)),
        ).fetchone()
        if row is None:
            con.commit()
            return False
        is_dead = bool(permanent) or int(row["attempts"]) >= int(row["max_attempts"])
        con.execute(
            """
            UPDATE delivery_outbox
            SET status = ?, available_at = ?, lease_owner = NULL,
                lease_token = NULL, lease_until = NULL, last_error = ?, updated_at = ?
            WHERE id = ? AND status = 'processing' AND lease_token = ?
            """,
            (
                "dead" if is_dead else "retry",
                str(retry_at),
                clean_error,
                updated_at,
                int(item_id),
                str(lease_token),
            ),
        )
        con.commit()
        return True


def delivery_outbox_get(item_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM delivery_outbox WHERE id = ?", (int(item_id),)).fetchone()
    return dict(row) if row is not None else None


def delivery_outbox_counts() -> dict[str, int]:
    with _db_lock, connect() as con:
        rows = con.execute(
            "SELECT status, COUNT(*) AS n FROM delivery_outbox GROUP BY status"
        ).fetchall()
    return {str(row["status"]): int(row["n"]) for row in rows}


def delivery_outbox_unreported_dead(limit: int = 25) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM delivery_outbox
            WHERE status = 'dead' AND dead_notified_at IS NULL
            ORDER BY updated_at ASC, id ASC LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [dict(row) for row in rows]


def delivery_outbox_mark_dead_notified(item_id: int, *, now: str | None = None) -> bool:
    notified_at = str(now or utc_now_iso())
    with _db_lock, connect() as con:
        cur = con.execute(
            """
            UPDATE delivery_outbox SET dead_notified_at = ?, updated_at = ?
            WHERE id = ? AND status = 'dead' AND dead_notified_at IS NULL
            """,
            (notified_at, notified_at, int(item_id)),
        )
        con.commit()
        return cur.rowcount == 1


def delivery_outbox_requeue_dead(
    item_id: int,
    *,
    guild_id: int | None = None,
    now: str | None = None,
) -> bool:
    ready_at = str(now or utc_now_iso())
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT payload_json FROM delivery_outbox WHERE id = ? AND status = 'dead'",
            (int(item_id),),
        ).fetchone()
        if row is None:
            con.rollback()
            return False
        if guild_id is not None:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
                owner_guild_id = int(payload.get("guild_id") or 0) if isinstance(payload, dict) else 0
            except (TypeError, ValueError, json.JSONDecodeError):
                owner_guild_id = 0
            if owner_guild_id != int(guild_id):
                con.rollback()
                return False
        cur = con.execute(
            """
            UPDATE delivery_outbox
            SET status = 'retry', attempts = 0, available_at = ?, lease_owner = NULL,
                lease_token = NULL, lease_until = NULL, last_error = NULL,
                dead_notified_at = NULL, updated_at = ?
            WHERE id = ? AND status = 'dead'
            """,
            (ready_at, ready_at, int(item_id)),
        )
        con.commit()
        return cur.rowcount == 1


def get_meta(key: str) -> str | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    now = utc_now_iso()
    con.execute(
        """
        INSERT INTO meta(key, value, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now),
    )


def _record_bot_action(
    con: sqlite3.Connection,
    *,
    guild_id: int,
    actor_id: int | None,
    actor_display: str | None,
    module: str,
    action_kind: str,
    target_type: str,
    target_id: int | str | None,
    summary: str,
    payload: dict[str, Any] | None = None,
    reversible: bool = True,
    now: str | None = None,
) -> int:
    cur = con.execute(
        """
        INSERT INTO bot_actions(
            guild_id, actor_id, actor_display, module, action_kind,
            target_type, target_id, summary, payload_json,
            reversible, status, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (
            guild_id,
            actor_id,
            actor_display,
            str(module),
            str(action_kind),
            str(target_type),
            None if target_id is None else str(target_id),
            str(summary)[:1000],
            json.dumps(payload or {}, ensure_ascii=False),
            1 if reversible else 0,
            now or utc_now_iso(),
        ),
    )
    return int(cur.lastrowid)


def bot_record_action(
    *,
    guild_id: int,
    actor_id: int | None,
    actor_display: str | None,
    module: str,
    action_kind: str,
    target_type: str,
    target_id: int | str | None,
    summary: str,
    payload: dict[str, Any] | None = None,
    reversible: bool = False,
) -> int:
    with _db_lock, connect() as con:
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module=module,
            action_kind=action_kind,
            target_type=target_type,
            target_id=target_id,
            summary=summary,
            payload=payload,
            reversible=reversible,
        )
        con.commit()
        return action_id


def _bot_action_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    try:
        result["payload"] = json.loads(str(result.get("payload_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        result["payload"] = {}
    return result


def bot_get_action(action_id: int, guild_id: int | None = None) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        if guild_id is None:
            row = con.execute("SELECT * FROM bot_actions WHERE id = ?", (action_id,)).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM bot_actions WHERE id = ? AND guild_id = ?",
                (action_id, guild_id),
            ).fetchone()
    return _bot_action_dict(row)


def bot_list_actions(
    guild_id: int,
    *,
    actor_id: int | None = None,
    module: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    clauses = ["guild_id = ?"]
    params: list[Any] = [guild_id]
    if actor_id is not None:
        clauses.append("actor_id = ?")
        params.append(actor_id)
    if module:
        clauses.append("module = ?")
        params.append(str(module))
    if status:
        clauses.append("status = ?")
        params.append(str(status))
    params.append(max(1, min(int(limit), 100)))
    with _db_lock, connect() as con:
        rows = con.execute(
            f"SELECT * FROM bot_actions WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return [_bot_action_dict(row) for row in rows if row is not None]


def upsert_member(
    con: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    display_name: str | None,
    name: str | None,
    mention: str | None,
    is_bot: bool = False,
) -> None:
    now = utc_now_iso()
    con.execute(
        """
        INSERT INTO members(guild_id, user_id, display_name, name, mention, is_bot, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            display_name = excluded.display_name,
            name = excluded.name,
            mention = excluded.mention,
            is_bot = excluded.is_bot,
            updated_at = excluded.updated_at
        """,
        (guild_id, user_id, display_name, name, mention, 1 if is_bot else 0, now, now),
    )


def should_debounce(
    con: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    event_type: str,
    channel_id: int | None,
    debounce_seconds: int,
) -> bool:
    row = con.execute(
        """
        SELECT last_event, last_channel_id, last_activity_at
        FROM activity_summary
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id),
    ).fetchone()
    if row is None:
        return False
    if row["last_event"] != event_type:
        return False
    if row["last_channel_id"] != channel_id:
        return False
    try:
        last_dt = datetime.fromisoformat(row["last_activity_at"])
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    return (datetime.now(timezone.utc) - last_dt.astimezone(timezone.utc)).total_seconds() < debounce_seconds


def remember_activity(
    *,
    guild_id: int,
    user_id: int,
    display_name: str | None,
    name: str | None,
    mention: str | None,
    is_bot: bool,
    event_type: str,
    event_text: str,
    channel_id: int | None = None,
    channel_name: str | None = None,
    category_id: int | None = None,
    category_name: str | None = None,
    details: str | None = None,
    message_id: int | None = None,
    debounce_seconds: int = 30,
    force: bool = False,
) -> bool:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        upsert_member(con, guild_id, user_id, display_name, name, mention, is_bot)
        if not force and should_debounce(con, guild_id, user_id, event_type, channel_id, debounce_seconds):
            con.commit()
            return False

        con.execute(
            """
            INSERT INTO activity_events(
                guild_id, user_id, event_type, event_text, at,
                channel_id, channel_name, category_id, category_name,
                details, message_id, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, event_type, event_text, now, channel_id, channel_name, category_id, category_name, details, message_id, now),
        )
        con.execute(
            """
            INSERT INTO activity_counters(guild_id, user_id, event_type, count, updated_at)
            VALUES(?, ?, ?, 1, ?)
            ON CONFLICT(guild_id, user_id, event_type) DO UPDATE SET
                count = count + 1,
                updated_at = excluded.updated_at
            """,
            (guild_id, user_id, event_type, now),
        )
        con.execute(
            """
            INSERT INTO activity_summary(
                guild_id, user_id, last_activity_at, last_event, last_event_text,
                last_channel_id, last_channel_name, last_category_id, last_category_name,
                last_details, total_events, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                last_activity_at = excluded.last_activity_at,
                last_event = excluded.last_event,
                last_event_text = excluded.last_event_text,
                last_channel_id = excluded.last_channel_id,
                last_channel_name = excluded.last_channel_name,
                last_category_id = excluded.last_category_id,
                last_category_name = excluded.last_category_name,
                last_details = excluded.last_details,
                total_events = activity_summary.total_events + 1,
                updated_at = excluded.updated_at
            """,
            (guild_id, user_id, now, event_type, event_text, channel_id, channel_name, category_id, category_name, details, now, now),
        )
        con.commit()
        return True


def _summary_from_row(row: sqlite3.Row) -> ActivitySummary:
    return ActivitySummary(
        guild_id=int(row["guild_id"]),
        user_id=int(row["user_id"]),
        display_name=row["display_name"],
        name=row["name"],
        mention=row["mention"],
        last_activity_at=row["last_activity_at"],
        last_event=row["last_event"],
        last_event_text=row["last_event_text"],
        last_channel_id=row["last_channel_id"],
        last_channel_name=row["last_channel_name"],
        last_category_id=row["last_category_id"] if "last_category_id" in row.keys() else None,
        last_category_name=row["last_category_name"] if "last_category_name" in row.keys() else None,
        last_details=row["last_details"],
        total_events=int(row["total_events"] or 0),
    )


def get_summaries(guild_id: int, user_ids: list[int]) -> dict[int, ActivitySummary]:
    if not user_ids:
        return {}
    placeholders = ",".join("?" for _ in user_ids)
    params: list[Any] = [guild_id, *user_ids]
    query = f"""
        SELECT
            s.guild_id,
            s.user_id,
            m.display_name,
            m.name,
            m.mention,
            s.last_activity_at,
            s.last_event,
            s.last_event_text,
            s.last_channel_id,
            s.last_channel_name,
            s.last_category_id,
            s.last_category_name,
            s.last_details,
            s.total_events
        FROM activity_summary s
        LEFT JOIN members m ON m.guild_id = s.guild_id AND m.user_id = s.user_id
        WHERE s.guild_id = ? AND s.user_id IN ({placeholders})
    """
    with _db_lock, connect() as con:
        rows = con.execute(query, params).fetchall()
    return {int(row["user_id"]): _summary_from_row(row) for row in rows}


def _build_activity_filter(
    *,
    event_types: Iterable[str] | None = None,
    category_id: int | None = None,
    channel_ids: Iterable[int] | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if event_types is not None:
        event_types = list(event_types)
        if not event_types:
            clauses.append("1 = 0")
        else:
            placeholders = ",".join("?" for _ in event_types)
            clauses.append(f"e.event_type IN ({placeholders})")
            params.extend(event_types)

    scope_clauses: list[str] = []
    if category_id is not None:
        scope_clauses.append("e.category_id = ?")
        params.append(category_id)
    if channel_ids is not None:
        ids = [int(x) for x in channel_ids]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            scope_clauses.append(f"e.channel_id IN ({placeholders})")
            params.extend(ids)
    if scope_clauses:
        clauses.append("(" + " OR ".join(scope_clauses) + ")")

    return (" AND ".join(clauses), params)


def get_filtered_summaries(
    guild_id: int,
    user_ids: list[int],
    *,
    event_types: Iterable[str] | None = None,
    category_id: int | None = None,
    channel_ids: Iterable[int] | None = None,
) -> dict[int, ActivitySummary]:
    if not user_ids:
        return {}

    user_placeholders = ",".join("?" for _ in user_ids)
    filter_sql, filter_params = _build_activity_filter(event_types=event_types, category_id=category_id, channel_ids=channel_ids)
    if filter_sql:
        filter_sql = " AND " + filter_sql

    params: list[Any] = [guild_id, *user_ids, *filter_params, guild_id, *user_ids, *filter_params]
    query = f"""
        WITH filtered AS (
            SELECT e.*
            FROM activity_events e
            WHERE e.guild_id = ? AND e.user_id IN ({user_placeholders}) {filter_sql}
        ),
        latest AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY at DESC, id DESC) AS rn
            FROM filtered
        ),
        counts AS (
            SELECT e.user_id, COUNT(*) AS total_events
            FROM activity_events e
            WHERE e.guild_id = ? AND e.user_id IN ({user_placeholders}) {filter_sql}
            GROUP BY e.user_id
        )
        SELECT
            l.guild_id,
            l.user_id,
            m.display_name,
            m.name,
            m.mention,
            l.at AS last_activity_at,
            l.event_type AS last_event,
            l.event_text AS last_event_text,
            l.channel_id AS last_channel_id,
            l.channel_name AS last_channel_name,
            l.category_id AS last_category_id,
            l.category_name AS last_category_name,
            l.details AS last_details,
            COALESCE(c.total_events, 0) AS total_events
        FROM latest l
        LEFT JOIN counts c ON c.user_id = l.user_id
        LEFT JOIN members m ON m.guild_id = l.guild_id AND m.user_id = l.user_id
        WHERE l.rn = 1
    """
    with _db_lock, connect() as con:
        rows = con.execute(query, params).fetchall()
    return {int(row["user_id"]): _summary_from_row(row) for row in rows}


def get_recent_events(
    guild_id: int,
    user_id: int,
    *,
    limit: int = 15,
    event_types: Iterable[str] | None = None,
    category_id: int | None = None,
    channel_ids: Iterable[int] | None = None,
) -> list[ActivityEvent]:
    filter_sql, filter_params = _build_activity_filter(event_types=event_types, category_id=category_id, channel_ids=channel_ids)
    if filter_sql:
        filter_sql = " AND " + filter_sql
    query = f"""
        SELECT id, guild_id, user_id, event_type, event_text, at,
               channel_id, channel_name, category_id, category_name, details, message_id
        FROM activity_events e
        WHERE e.guild_id = ? AND e.user_id = ? {filter_sql}
        ORDER BY e.at DESC, e.id DESC
        LIMIT ?
    """
    params: list[Any] = [guild_id, user_id, *filter_params, max(1, min(int(limit), 50))]
    with _db_lock, connect() as con:
        rows = con.execute(query, params).fetchall()
    return [
        ActivityEvent(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            event_type=str(row["event_type"]),
            event_text=row["event_text"],
            at=str(row["at"]),
            channel_id=row["channel_id"],
            channel_name=row["channel_name"],
            category_id=row["category_id"],
            category_name=row["category_name"],
            details=row["details"],
            message_id=row["message_id"],
        )
        for row in rows
    ]


def get_counters(guild_id: int, user_id: int) -> dict[str, int]:
    with _db_lock, connect() as con:
        rows = con.execute(
            "SELECT event_type, count FROM activity_counters WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchall()
    return {str(row["event_type"]): int(row["count"] or 0) for row in rows}


def record_bureau_announcement(
    *,
    guild_id: int,
    author_id: int,
    author_display: str,
    source_channel_id: int | None,
    target_channel_id: int,
    message_id: int | None,
    content: str,
    created_at: str,
    title: str | None = None,
    note: str | None = None,
    image_url: str | None = None,
    publish_global: bool = False,
    global_channel_id: int | None = None,
    global_message_id: int | None = None,
) -> int:
    with _db_lock, connect() as con:
        cur = con.execute(
            """
            INSERT INTO bureau_announcements(
                guild_id, author_id, author_display, source_channel_id,
                target_channel_id, message_id, global_channel_id, global_message_id,
                title, content, note, image_url, publish_global, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                author_id,
                author_display,
                source_channel_id,
                target_channel_id,
                message_id,
                global_channel_id,
                global_message_id,
                title,
                content,
                note,
                image_url,
                1 if publish_global else 0,
                created_at,
            ),
        )
        con.commit()
        return int(cur.lastrowid)


def _case_from_row(row: sqlite3.Row) -> SGLCase:
    return SGLCase(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        case_number=int(row["case_number"]),
        channel_id=row["channel_id"],
        client_id=int(row["client_id"]),
        client_display=row["client_display"],
        lead_lawyer_id=int(row["lead_lawyer_id"]),
        lead_lawyer_display=row["lead_lawyer_display"],
        secretary_id=row["secretary_id"],
        secretary_display=row["secretary_display"],
        status=str(row["status"]),
        request_type=row["request_type"],
        client_nick=row["client_nick"],
        static_id=row["static_id"],
        bank_account=row["bank_account"],
        phone=row["phone"],
        passport_url=row["passport_url"],
        situation_text=row["situation_text"],
        situation_author_id=row["situation_author_id"],
        situation_author_display=row["situation_author_display"],
        claim_link=row["claim_link"],
        claim_message_id=row["claim_message_id"],
        portfolio_message_id=row["portfolio_message_id"],
        portfolio_description=row["portfolio_description"],
        created_by_id=row["created_by_id"],
        created_by_display=row["created_by_display"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        closed_at=row["closed_at"],
        archived_at=row["archived_at"] if "archived_at" in row.keys() else None,
        archive_category_id=row["archive_category_id"] if "archive_category_id" in row.keys() else None,
    )


def _record_case_event(
    con: sqlite3.Connection,
    *,
    case_id: int | None,
    guild_id: int,
    case_number: int,
    actor_id: int | None,
    actor_display: str | None,
    action: str,
    details: str | None = None,
) -> None:
    con.execute(
        """
        INSERT INTO sgl_case_events(case_id, guild_id, case_number, actor_id, actor_display, action, details, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (case_id, guild_id, case_number, actor_id, actor_display, action, details, utc_now_iso()),
    )


def set_sgl_case_seed(guild_id: int, first_case_number: int) -> str:
    """Set the initial case number only when no case exists yet."""
    first_case_number = max(1, int(first_case_number))
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT MAX(case_number) AS max_case FROM sgl_cases WHERE guild_id = ?", (guild_id,)).fetchone()
        has_cases = row is not None and row["max_case"] is not None
        if has_cases:
            set_meta(con, f"sgl_case_seed_ignored:{guild_id}:{utc_now_iso()}", str(first_case_number))
            con.commit()
            return "kept_existing_cases"
        set_meta(con, f"sgl_case_next_number:{guild_id}", str(first_case_number))
        con.commit()
        return "seed_set"


def _next_case_number(con: sqlite3.Connection, guild_id: int) -> int:
    row = con.execute("SELECT MAX(case_number) AS max_case FROM sgl_cases WHERE guild_id = ?", (guild_id,)).fetchone()
    if row is not None and row["max_case"] is not None:
        return int(row["max_case"]) + 1
    seed_row = con.execute("SELECT value FROM meta WHERE key = ?", (f"sgl_case_next_number:{guild_id}",)).fetchone()
    if seed_row:
        try:
            return max(1, int(seed_row["value"]))
        except (TypeError, ValueError):
            return 1
    return 1


def reserve_sgl_case(
    *,
    guild_id: int,
    client_id: int,
    client_display: str | None,
    lead_lawyer_id: int,
    lead_lawyer_display: str | None,
    secretary_id: int | None,
    secretary_display: str | None,
    created_by_id: int | None,
    created_by_display: str | None,
) -> SGLCase:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        case_number = _next_case_number(con, guild_id)
        cur = con.execute(
            """
            INSERT INTO sgl_cases(
                guild_id, case_number, channel_id, client_id, client_display,
                lead_lawyer_id, lead_lawyer_display, secretary_id, secretary_display,
                status, created_by_id, created_by_display, created_at, updated_at
            )
            VALUES(?, ?, NULL, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?)
            """,
            (
                guild_id,
                case_number,
                client_id,
                client_display,
                lead_lawyer_id,
                lead_lawyer_display,
                secretary_id,
                secretary_display,
                created_by_id,
                created_by_display,
                now,
                now,
            ),
        )
        case_id = int(cur.lastrowid)
        _record_case_event(con, case_id=case_id, guild_id=guild_id, case_number=case_number, actor_id=created_by_id, actor_display=created_by_display, action="reserved")
        row = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (case_id,)).fetchone()
        con.commit()
    return _case_from_row(row)


def attach_sgl_case_channel(case_id: int, channel_id: int) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute("UPDATE sgl_cases SET channel_id = ?, status = 'created', updated_at = ? WHERE id = ?", (channel_id, now, case_id))
        _record_case_event(con, case_id=case_id, guild_id=int(row["guild_id"]), case_number=int(row["case_number"]), actor_id=None, actor_display=None, action="channel_created", details=str(channel_id))
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (case_id,)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def mark_sgl_case_error(case_id: int, details: str) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (case_id,)).fetchone()
        if row is not None:
            con.execute("UPDATE sgl_cases SET status = 'error', updated_at = ? WHERE id = ?", (now, case_id))
            _record_case_event(con, case_id=case_id, guild_id=int(row["guild_id"]), case_number=int(row["case_number"]), actor_id=None, actor_display=None, action="error", details=details)
        con.commit()


def get_sgl_case_by_channel(guild_id: int, channel_id: int) -> SGLCase | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
    return _case_from_row(row) if row else None


def get_sgl_case_by_number(guild_id: int, case_number: int) -> SGLCase | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND case_number = ?", (guild_id, case_number)).fetchone()
    return _case_from_row(row) if row else None


def update_sgl_case_params(
    *,
    guild_id: int,
    channel_id: int,
    request_type: str,
    client_nick: str,
    static_id: str,
    bank_account: str,
    phone: str,
    passport_url: str,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_cases
            SET request_type = ?, client_nick = ?, static_id = ?, bank_account = ?, phone = ?, passport_url = ?,
                status = 'awaiting_situation', updated_at = ?
            WHERE id = ?
            """,
            (request_type, client_nick, static_id, bank_account, phone, passport_url, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="params_saved")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_case",
            target_id=int(row["id"]),
            summary=f"Изменены параметры дела №{int(row['case_number'])}",
            payload={"table": "sgl_cases", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def update_sgl_case_situation(
    guild_id: int,
    channel_id: int,
    situation_text: str,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_cases
            SET situation_text = ?, situation_author_id = ?, situation_author_display = ?, status = 'awaiting_link', updated_at = ?
            WHERE id = ?
            """,
            (situation_text, actor_id, actor_display, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="situation_saved")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_case",
            target_id=int(row["id"]),
            summary=f"Изменено описание ситуации в деле №{int(row['case_number'])}",
            payload={"table": "sgl_cases", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def set_sgl_case_link(
    guild_id: int,
    channel_id: int,
    claim_link: str,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute("UPDATE sgl_cases SET claim_link = ?, status = 'awaiting_close', updated_at = ? WHERE id = ?", (claim_link, now, int(row["id"])))
        _record_case_event(con, case_id=int(row["id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="claim_link_saved", details=claim_link)
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_case",
            target_id=int(row["id"]),
            summary=f"Изменена ссылка на иск в деле №{int(row['case_number'])}",
            payload={"table": "sgl_cases", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def set_sgl_case_link_message(guild_id: int, channel_id: int, message_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("UPDATE sgl_cases SET claim_message_id = ?, updated_at = ? WHERE guild_id = ? AND channel_id = ?", (message_id, now, guild_id, channel_id))
        con.commit()


def close_sgl_case(
    *,
    guild_id: int,
    channel_id: int,
    actor_id: int | None,
    actor_display: str | None,
    publish_portfolio: bool,
    portfolio_description: str | None,
    portfolio_message_id: int | None,
) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_cases
            SET status = 'closed', portfolio_description = ?, portfolio_message_id = ?, closed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (portfolio_description, portfolio_message_id, now, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="closed", details=f"publish_portfolio={publish_portfolio}")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_case",
            target_id=int(row["id"]),
            summary=f"Закрыто дело №{int(row['case_number'])}",
            payload={"table": "sgl_cases", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def list_sgl_cases_with_channels(guild_id: int) -> list[SGLCase]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_cases
            WHERE guild_id = ? AND channel_id IS NOT NULL
            ORDER BY case_number ASC
            """,
            (guild_id,),
        ).fetchall()
    return [_case_from_row(row) for row in rows]



def list_sgl_cases_for_client(guild_id: int, client_id: int, limit: int = 25) -> list[SGLCase]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_cases
            WHERE guild_id = ? AND client_id = ?
            ORDER BY case_number DESC
            LIMIT ?
            """,
            (guild_id, client_id, int(limit)),
        ).fetchall()
    return [_case_from_row(row) for row in rows]


def list_sgl_cases_for_participant(guild_id: int, user_id: int, limit: int = 25) -> list[SGLCase]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_cases
            WHERE guild_id = ?
              AND (client_id = ? OR lead_lawyer_id = ? OR secretary_id = ?)
            ORDER BY case_number DESC
            LIMIT ?
            """,
            (guild_id, user_id, user_id, user_id, int(limit)),
        ).fetchall()
    return [_case_from_row(row) for row in rows]


def get_sgl_case_events(case_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM sgl_case_events
            WHERE case_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (case_id, int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]

def list_sgl_closed_cases_pending_archive(guild_id: int | None = None) -> list[SGLCase]:
    params: list[Any] = []
    where = "status = 'closed' AND channel_id IS NOT NULL AND archived_at IS NULL"
    if guild_id is not None:
        where += " AND guild_id = ?"
        params.append(guild_id)
    with _db_lock, connect() as con:
        rows = con.execute(f"SELECT * FROM sgl_cases WHERE {where} ORDER BY closed_at ASC, case_number ASC", params).fetchall()
    return [_case_from_row(row) for row in rows]


def mark_sgl_case_archived(guild_id: int, channel_id: int, archive_category_id: int) -> SGLCase | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_cases WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_cases
            SET archived_at = ?, archive_category_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, archive_category_id, now, int(row["id"])),
        )
        _record_case_event(
            con,
            case_id=int(row["id"]),
            guild_id=guild_id,
            case_number=int(row["case_number"]),
            actor_id=None,
            actor_display=None,
            action="archived",
            details=str(archive_category_id),
        )
        updated = con.execute("SELECT * FROM sgl_cases WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _case_from_row(updated) if updated else None


def _receipt_from_row(row: sqlite3.Row) -> SGLReceipt:
    return SGLReceipt(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        case_id=int(row["case_id"]),
        case_number=int(row["case_number"]),
        channel_id=row["channel_id"],
        client_id=int(row["client_id"]),
        lead_lawyer_id=int(row["lead_lawyer_id"]),
        created_by_id=row["created_by_id"],
        created_by_display=row["created_by_display"],
        court_code=str(row["court_code"]),
        court_label=str(row["court_label"]),
        court_suffix=str(row["court_suffix"]),
        total_amount=int(row["total_amount"]),
        lawyer_amount=int(row["lawyer_amount"]),
        duty_amount=int(row["duty_amount"]),
        lawyer_bank=str(row["lawyer_bank"]),
        duty_bank=str(row["duty_bank"]),
        invoice_message_id=row["invoice_message_id"],
        proof_services_url=row["proof_services_url"],
        proof_duty_url=row["proof_duty_url"],
        proof_submitted_by_id=row["proof_submitted_by_id"],
        proof_submitted_by_display=row["proof_submitted_by_display"],
        proof_submitted_at=row["proof_submitted_at"],
        confirmation_message_id=row["confirmation_message_id"],
        confirmed_by_id=row["confirmed_by_id"],
        confirmed_by_display=row["confirmed_by_display"],
        confirmed_at=row["confirmed_at"],
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def create_sgl_receipt(
    *,
    guild_id: int,
    case: SGLCase,
    created_by_id: int | None,
    created_by_display: str | None,
    court_code: str,
    court_label: str,
    court_suffix: str,
    total_amount: int,
    lawyer_amount: int,
    duty_amount: int,
    lawyer_bank: str,
    duty_bank: str,
) -> SGLReceipt:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute(
            """
            INSERT INTO sgl_receipts(
                guild_id, case_id, case_number, channel_id, client_id, lead_lawyer_id,
                created_by_id, created_by_display, court_code, court_label, court_suffix,
                total_amount, lawyer_amount, duty_amount, lawyer_bank, duty_bank,
                status, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', ?, ?)
            """,
            (
                guild_id, case.id, case.case_number, case.channel_id, case.client_id, case.lead_lawyer_id,
                created_by_id, created_by_display, court_code, court_label, court_suffix,
                int(total_amount), int(lawyer_amount), int(duty_amount), lawyer_bank, duty_bank,
                now, now,
            ),
        )
        receipt_id = int(cur.lastrowid)
        _record_case_event(con, case_id=case.id, guild_id=guild_id, case_number=case.case_number, actor_id=created_by_id, actor_display=created_by_display, action="receipt_created", details=f"receipt_id={receipt_id};{court_suffix};total={total_amount}")
        row = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (receipt_id,)).fetchone()
        con.commit()
    return _receipt_from_row(row)


def set_sgl_receipt_invoice_message(receipt_id: int, message_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("UPDATE sgl_receipts SET invoice_message_id = ?, updated_at = ? WHERE id = ?", (message_id, now, receipt_id))
        con.commit()


def get_sgl_receipt_by_id(receipt_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (receipt_id,)).fetchone()
    return _receipt_from_row(row) if row else None


def get_sgl_receipt_by_invoice_message(guild_id: int, message_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE guild_id = ? AND invoice_message_id = ?", (guild_id, message_id)).fetchone()
    return _receipt_from_row(row) if row else None


def get_sgl_receipt_by_confirmation_message(guild_id: int, message_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE guild_id = ? AND confirmation_message_id = ?", (guild_id, message_id)).fetchone()
    return _receipt_from_row(row) if row else None


def get_sgl_receipt_by_confirmation_message_any(message_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE confirmation_message_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1", (message_id,)).fetchone()
    return _receipt_from_row(row) if row else None


def list_sgl_receipts_for_case(case_id: int, limit: int = 10) -> list[SGLReceipt]:
    with _db_lock, connect() as con:
        rows = con.execute("SELECT * FROM sgl_receipts WHERE case_id = ? ORDER BY created_at DESC, id DESC LIMIT ?", (case_id, int(limit))).fetchall()
    return [_receipt_from_row(row) for row in rows]


def get_latest_sgl_receipt_for_case(case_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE case_id = ? ORDER BY CASE WHEN status = 'confirmed' THEN 0 ELSE 1 END, created_at DESC, id DESC LIMIT 1", (case_id,)).fetchone()
    return _receipt_from_row(row) if row else None


def get_latest_confirmed_sgl_receipt_for_case(case_id: int) -> SGLReceipt | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM sgl_receipts WHERE case_id = ? AND status = 'confirmed' ORDER BY confirmed_at DESC, id DESC LIMIT 1", (case_id,)).fetchone()
    return _receipt_from_row(row) if row else None


def submit_sgl_receipt_proofs_by_invoice_message(
    *,
    guild_id: int,
    invoice_message_id: int,
    services_url: str,
    duty_url: str,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLReceipt | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_receipts WHERE guild_id = ? AND invoice_message_id = ?", (guild_id, invoice_message_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_receipts
            SET proof_services_url = ?, proof_duty_url = ?, proof_submitted_by_id = ?, proof_submitted_by_display = ?, proof_submitted_at = ?, status = 'proofs_submitted', updated_at = ?
            WHERE id = ?
            """,
            (services_url, duty_url, actor_id, actor_display, now, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["case_id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="receipt_proofs_submitted", details=f"receipt_id={int(row['id'])}")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_receipt",
            target_id=int(row["id"]),
            summary=f"Добавлены доказательства оплаты по квитанции #{int(row['id'])}",
            payload={"table": "sgl_receipts", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _receipt_from_row(updated) if updated else None


def set_sgl_receipt_confirmation_message(receipt_id: int, message_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("UPDATE sgl_receipts SET confirmation_message_id = ?, updated_at = ? WHERE id = ?", (message_id, now, receipt_id))
        con.commit()


def confirm_sgl_receipt_by_confirmation_message(
    *,
    guild_id: int,
    confirmation_message_id: int,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLReceipt | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_receipts WHERE guild_id = ? AND confirmation_message_id = ?", (guild_id, confirmation_message_id)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_receipts
            SET confirmed_by_id = ?, confirmed_by_display = ?, confirmed_at = ?, status = 'confirmed', updated_at = ?
            WHERE id = ?
            """,
            (actor_id, actor_display, now, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["case_id"]), guild_id=guild_id, case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="receipt_confirmed", details=f"receipt_id={int(row['id'])}")
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_receipt",
            target_id=int(row["id"]),
            summary=f"Подтверждена квитанция #{int(row['id'])}",
            payload={"table": "sgl_receipts", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _receipt_from_row(updated) if updated else None


def confirm_sgl_receipt_by_confirmation_message_any(
    *,
    confirmation_message_id: int,
    actor_id: int | None,
    actor_display: str | None,
) -> SGLReceipt | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM sgl_receipts WHERE confirmation_message_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1", (confirmation_message_id,)).fetchone()
        if row is None:
            con.commit()
            return None
        con.execute(
            """
            UPDATE sgl_receipts
            SET confirmed_by_id = ?, confirmed_by_display = ?, confirmed_at = ?, status = 'confirmed', updated_at = ?
            WHERE id = ?
            """,
            (actor_id, actor_display, now, now, int(row["id"])),
        )
        _record_case_event(con, case_id=int(row["case_id"]), guild_id=int(row["guild_id"]), case_number=int(row["case_number"]), actor_id=actor_id, actor_display=actor_display, action="receipt_confirmed", details=f"receipt_id={int(row['id'])}")
        _record_bot_action(
            con,
            guild_id=int(row["guild_id"]),
            actor_id=actor_id,
            actor_display=actor_display,
            module="sgl",
            action_kind="generic_row_restore",
            target_type="sgl_receipt",
            target_id=int(row["id"]),
            summary=f"Подтверждена квитанция #{int(row['id'])}",
            payload={"table": "sgl_receipts", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        updated = con.execute("SELECT * FROM sgl_receipts WHERE id = ?", (int(row["id"]),)).fetchone()
        con.commit()
    return _receipt_from_row(updated) if updated else None


def _read_legacy_activity_json() -> dict[str, Any] | None:
    if not LEGACY_ACTIVITY_FILE.exists() or not LEGACY_ACTIVITY_FILE.is_file():
        return None
    try:
        with LEGACY_ACTIVITY_FILE.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def migrate_legacy_activity_json() -> dict[str, int | str]:
    result: dict[str, int | str] = {"users": 0, "events": 0, "counters": 0, "status": "skipped", "reason": "legacy file not found"}
    if not LEGACY_ACTIVITY_FILE.exists():
        return result

    raw = LEGACY_ACTIVITY_FILE.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    meta_key = f"legacy_activity_json_sha256:{sha}"
    if get_meta(meta_key) == "imported":
        result["reason"] = "same legacy file already imported"
        return result

    data = _read_legacy_activity_json()
    if not data:
        result["reason"] = "legacy file empty or invalid"
        return result

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        users = 0
        events = 0
        counters = 0
        for guild_key, guild_data in data.items():
            if not isinstance(guild_data, dict):
                continue
            try:
                guild_id = int(guild_key)
            except (TypeError, ValueError):
                continue
            for user_key, item in guild_data.items():
                if not isinstance(item, dict):
                    continue
                try:
                    user_id = int(item.get("user_id") or user_key)
                except (TypeError, ValueError):
                    continue
                upsert_member(
                    con,
                    guild_id,
                    user_id,
                    item.get("display_name"),
                    item.get("name"),
                    item.get("mention"),
                    False,
                )
                users += 1

                counters_data = item.get("counters") if isinstance(item.get("counters"), dict) else {}
                total_events = int(counters_data.get("total", 0) or 0)
                for event_type, count in counters_data.items():
                    if event_type == "total":
                        continue
                    try:
                        count_int = int(count)
                    except (TypeError, ValueError):
                        continue
                    con.execute(
                        """
                        INSERT INTO activity_counters(guild_id, user_id, event_type, count, updated_at)
                        VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(guild_id, user_id, event_type) DO UPDATE SET
                            count = MAX(activity_counters.count, excluded.count),
                            updated_at = excluded.updated_at
                        """,
                        (guild_id, user_id, str(event_type), count_int, now),
                    )
                    counters += 1

                recent_events = item.get("recent_events") if isinstance(item.get("recent_events"), list) else []
                for event in reversed(recent_events):
                    if not isinstance(event, dict):
                        continue
                    con.execute(
                        """
                        INSERT INTO activity_events(
                            guild_id, user_id, event_type, event_text, at,
                            channel_id, channel_name, category_id, category_name,
                            details, message_id, created_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, ?)
                        """,
                        (
                            guild_id,
                            user_id,
                            str(event.get("event") or "legacy_event"),
                            event.get("event_text"),
                            str(event.get("at") or now),
                            event.get("channel_id"),
                            event.get("channel_name"),
                            event.get("details"),
                            now,
                        ),
                    )
                    events += 1

                con.execute(
                    """
                    INSERT INTO activity_summary(
                        guild_id, user_id, last_activity_at, last_event, last_event_text,
                        last_channel_id, last_channel_name, last_category_id, last_category_name,
                        last_details, total_events, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                        last_activity_at = CASE
                            WHEN activity_summary.last_activity_at IS NULL THEN excluded.last_activity_at
                            WHEN excluded.last_activity_at IS NULL THEN activity_summary.last_activity_at
                            WHEN excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_activity_at
                            ELSE activity_summary.last_activity_at
                        END,
                        last_event = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_event
                            ELSE activity_summary.last_event
                        END,
                        last_event_text = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_event_text
                            ELSE activity_summary.last_event_text
                        END,
                        last_channel_id = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_channel_id
                            ELSE activity_summary.last_channel_id
                        END,
                        last_channel_name = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_channel_name
                            ELSE activity_summary.last_channel_name
                        END,
                        last_details = CASE
                            WHEN activity_summary.last_activity_at IS NULL OR excluded.last_activity_at > activity_summary.last_activity_at THEN excluded.last_details
                            ELSE activity_summary.last_details
                        END,
                        total_events = MAX(activity_summary.total_events, excluded.total_events),
                        updated_at = excluded.updated_at
                    """,
                    (
                        guild_id,
                        user_id,
                        item.get("last_activity_at"),
                        item.get("last_event"),
                        item.get("last_event_text"),
                        item.get("last_channel_id"),
                        item.get("last_channel_name"),
                        item.get("last_details"),
                        total_events,
                        now,
                        now,
                    ),
                )

        set_meta(con, meta_key, "imported")
        set_meta(con, "legacy_activity_json_last_sha256", sha)
        con.commit()

    result.update({"users": users, "events": events, "counters": counters, "status": "imported", "reason": "ok"})
    return result


def create_audio_generation(
    *,
    guild_id: int,
    channel_id: int,
    user_id: int,
    user_display: str | None,
    prompt: str,
    model: str,
) -> int:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        cur = con.execute(
            """
            INSERT INTO audio_generations(
                guild_id, channel_id, user_id, user_display, prompt, model,
                status, seconds_elapsed, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, 'running', 0, ?, ?)
            """,
            (guild_id, channel_id, user_id, user_display, prompt, model, now, now),
        )
        con.commit()
        return int(cur.lastrowid)


def update_audio_generation(record_id: int, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "status",
        "seconds_elapsed",
        "output_filename",
        "output_mime",
        "output_size_bytes",
        "dm_message_id",
        "error",
        "completed_at",
        "updated_at",
    }
    clean = {key: value for key, value in fields.items() if key in allowed}
    if not clean:
        return
    clean["updated_at"] = clean.get("updated_at") or utc_now_iso()
    assignments = ", ".join(f"{key} = ?" for key in clean.keys())
    params = [*clean.values(), record_id]
    with _db_lock, connect() as con:
        con.execute(f"UPDATE audio_generations SET {assignments} WHERE id = ?", params)
        con.commit()


def count_recent_running_audio_generations(guild_id: int, within_minutes: int = 30) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(1, int(within_minutes)))).isoformat()
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT COUNT(*) AS count
            FROM audio_generations
            WHERE guild_id = ? AND status = 'running' AND updated_at >= ?
            """,
            (guild_id, cutoff),
        ).fetchone()
    return int(row["count"] or 0) if row else 0


def save_client_profile(
    *,
    guild_id: int,
    discord_user_id: int,
    client_nick: str,
    static_id: str,
    bank_account: str,
    phone: str,
    passport_url: str,
    notes: str | None = None,
    last_case_id: int | None = None,
) -> int:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        existing = con.execute(
            "SELECT id FROM client_profiles WHERE guild_id = ? AND discord_user_id = ? AND lower(client_nick) = lower(?) AND static_id = ?",
            (guild_id, discord_user_id, client_nick, static_id),
        ).fetchone()
        if existing:
            profile_id = int(existing["id"])
            con.execute(
                """
                UPDATE client_profiles
                SET bank_account = ?, phone = ?, passport_url = ?, notes = COALESCE(?, notes), last_case_id = ?, updated_at = ?, last_used_at = ?
                WHERE id = ?
                """,
                (bank_account, phone, passport_url, notes, last_case_id, now, now, profile_id),
            )
        else:
            cur = con.execute(
                """
                INSERT INTO client_profiles(
                    guild_id, discord_user_id, client_nick, static_id, bank_account, phone, passport_url, notes, last_case_id, created_at, updated_at, last_used_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, discord_user_id, client_nick, static_id, bank_account, phone, passport_url, notes, last_case_id, now, now, now),
            )
            profile_id = int(cur.lastrowid)
        con.commit()
        return profile_id


def list_client_profiles_for_user(guild_id: int, discord_user_id: int, limit: int = 20) -> list[ClientProfile]:
    with _db_lock, connect() as con:
        rows = con.execute(
            "SELECT * FROM client_profiles WHERE guild_id = ? AND discord_user_id = ? ORDER BY COALESCE(last_used_at, updated_at) DESC, id DESC LIMIT ?",
            (guild_id, discord_user_id, limit),
        ).fetchall()
    return [_client_profile_from_row(r) for r in rows if r is not None]


def get_client_profile(profile_id: int, guild_id: int | None = None) -> ClientProfile | None:
    with _db_lock, connect() as con:
        if guild_id is None:
            row = con.execute("SELECT * FROM client_profiles WHERE id = ?", (profile_id,)).fetchone()
        else:
            row = con.execute("SELECT * FROM client_profiles WHERE id = ? AND guild_id = ?", (profile_id, guild_id)).fetchone()
    return _client_profile_from_row(row)


def search_client_profiles(guild_id: int, query: str, limit: int = 25) -> list[ClientProfile]:
    like = f"%{query.strip().lower()}%"
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM client_profiles
            WHERE guild_id = ? AND (
                lower(client_nick) LIKE ? OR lower(static_id) LIKE ? OR lower(bank_account) LIKE ? OR lower(phone) LIKE ? OR CAST(discord_user_id AS TEXT) LIKE ?
            )
            ORDER BY COALESCE(last_used_at, updated_at) DESC, id DESC
            LIMIT ?
            """,
            (guild_id, like, like, like, like, like, limit),
        ).fetchall()
    return [_client_profile_from_row(r) for r in rows if r is not None]


def save_lawyer_profile(
    *,
    guild_id: int,
    lawyer_nick: str,
    discord_user_id: int | None = None,
    static_id: str | None = None,
    bank_account: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    passport_url: str | None = None,
    notes: str | None = None,
) -> int:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        existing = None
        if discord_user_id:
            existing = con.execute("SELECT id FROM lawyer_profiles WHERE guild_id = ? AND discord_user_id = ?", (guild_id, discord_user_id)).fetchone()
        if existing is None:
            existing = con.execute("SELECT id FROM lawyer_profiles WHERE guild_id = ? AND lower(lawyer_nick) = lower(?) AND COALESCE(static_id,'') = COALESCE(?, '')", (guild_id, lawyer_nick, static_id)).fetchone()
        if existing:
            profile_id = int(existing["id"])
            con.execute(
                """UPDATE lawyer_profiles SET lawyer_nick=?, discord_user_id=?, static_id=?, bank_account=?, phone=?, email=?, passport_url=?, notes=?, updated_at=? WHERE id=?""",
                (lawyer_nick, discord_user_id, static_id, bank_account, phone, email, passport_url, notes, now, profile_id),
            )
        else:
            cur = con.execute(
                """INSERT INTO lawyer_profiles(guild_id, discord_user_id, lawyer_nick, static_id, bank_account, phone, email, passport_url, notes, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (guild_id, discord_user_id, lawyer_nick, static_id, bank_account, phone, email, passport_url, notes, now, now),
            )
            profile_id = int(cur.lastrowid)
        con.commit()
        return profile_id



def get_lawyer_profile_for_user(guild_id: int, discord_user_id: int) -> LawyerProfile | None:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM lawyer_profiles WHERE guild_id = ? AND discord_user_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
            (guild_id, discord_user_id),
        ).fetchone()
    return _lawyer_profile_from_row(row) if row else None

def search_lawyer_profiles(guild_id: int, query: str | None = None, limit: int = 25) -> list[LawyerProfile]:
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            rows = con.execute(
                """SELECT * FROM lawyer_profiles WHERE guild_id = ? AND (lower(lawyer_nick) LIKE ? OR lower(COALESCE(static_id,'')) LIKE ? OR lower(COALESCE(phone,'')) LIKE ? OR CAST(COALESCE(discord_user_id,'') AS TEXT) LIKE ?) ORDER BY updated_at DESC, id DESC LIMIT ?""",
                (guild_id, like, like, like, like, limit),
            ).fetchall()
        else:
            rows = con.execute("SELECT * FROM lawyer_profiles WHERE guild_id = ? ORDER BY updated_at DESC, id DESC LIMIT ?", (guild_id, limit)).fetchall()
    return [_lawyer_profile_from_row(r) for r in rows if r is not None]


# Registry pagination helpers for /sg inline panels.
def count_client_profiles(guild_id: int, query: str | None = None) -> int:
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            row = con.execute(
                """
                SELECT COUNT(*) AS c FROM client_profiles
                WHERE guild_id = ? AND (
                    lower(client_nick) LIKE ? OR lower(static_id) LIKE ? OR lower(bank_account) LIKE ? OR lower(phone) LIKE ? OR CAST(discord_user_id AS TEXT) LIKE ?
                )
                """,
                (guild_id, like, like, like, like, like),
            ).fetchone()
        else:
            row = con.execute("SELECT COUNT(*) AS c FROM client_profiles WHERE guild_id = ?", (guild_id,)).fetchone()
    return int(row["c"] or 0) if row else 0


def list_client_profiles_page(guild_id: int, page: int = 0, per_page: int = 10, query: str | None = None) -> list[ClientProfile]:
    page = max(0, int(page))
    per_page = max(1, min(25, int(per_page)))
    offset = page * per_page
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            rows = con.execute(
                """
                SELECT * FROM client_profiles
                WHERE guild_id = ? AND (
                    lower(client_nick) LIKE ? OR lower(static_id) LIKE ? OR lower(bank_account) LIKE ? OR lower(phone) LIKE ? OR CAST(discord_user_id AS TEXT) LIKE ?
                )
                ORDER BY lower(client_nick) ASC, CAST(static_id AS INTEGER) ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (guild_id, like, like, like, like, like, per_page, offset),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT * FROM client_profiles
                WHERE guild_id = ?
                ORDER BY lower(client_nick) ASC, CAST(static_id AS INTEGER) ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (guild_id, per_page, offset),
            ).fetchall()
    return [_client_profile_from_row(r) for r in rows if r is not None]


def count_lawyer_profiles(guild_id: int, query: str | None = None) -> int:
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            row = con.execute(
                """
                SELECT COUNT(*) AS c FROM lawyer_profiles
                WHERE guild_id = ? AND (
                    lower(lawyer_nick) LIKE ? OR lower(COALESCE(static_id,'')) LIKE ? OR lower(COALESCE(phone,'')) LIKE ? OR lower(COALESCE(bank_account,'')) LIKE ? OR CAST(COALESCE(discord_user_id,'') AS TEXT) LIKE ?
                )
                """,
                (guild_id, like, like, like, like, like),
            ).fetchone()
        else:
            row = con.execute("SELECT COUNT(*) AS c FROM lawyer_profiles WHERE guild_id = ?", (guild_id,)).fetchone()
    return int(row["c"] or 0) if row else 0


def list_lawyer_profiles_page(guild_id: int, page: int = 0, per_page: int = 10, query: str | None = None) -> list[LawyerProfile]:
    page = max(0, int(page))
    per_page = max(1, min(25, int(per_page)))
    offset = page * per_page
    with _db_lock, connect() as con:
        if query and query.strip():
            like = f"%{query.strip().lower()}%"
            rows = con.execute(
                """
                SELECT * FROM lawyer_profiles
                WHERE guild_id = ? AND (
                    lower(lawyer_nick) LIKE ? OR lower(COALESCE(static_id,'')) LIKE ? OR lower(COALESCE(phone,'')) LIKE ? OR lower(COALESCE(bank_account,'')) LIKE ? OR CAST(COALESCE(discord_user_id,'') AS TEXT) LIKE ?
                )
                ORDER BY lower(lawyer_nick) ASC, CAST(COALESCE(static_id,'0') AS INTEGER) ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (guild_id, like, like, like, like, like, per_page, offset),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT * FROM lawyer_profiles
                WHERE guild_id = ?
                ORDER BY lower(lawyer_nick) ASC, CAST(COALESCE(static_id,'0') AS INTEGER) ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (guild_id, per_page, offset),
            ).fetchall()
    return [_lawyer_profile_from_row(r) for r in rows if r is not None]


def count_open_sgl_cases(guild_id: int) -> int:
    with _db_lock, connect() as con:
        row = con.execute("SELECT COUNT(*) AS c FROM sgl_cases WHERE guild_id = ? AND status != 'closed'", (guild_id,)).fetchone()
    return int(row["c"] or 0) if row else 0


def count_closed_sgl_cases(guild_id: int) -> int:
    with _db_lock, connect() as con:
        row = con.execute("SELECT COUNT(*) AS c FROM sgl_cases WHERE guild_id = ? AND status = 'closed'", (guild_id,)).fetchone()
    return int(row["c"] or 0) if row else 0

# Manual SGL admin editor helpers.
_ADMIN_TARGETS = {
    "case": {
        "table": "sgl_cases",
        "id_field": "case_number",
        "fields": {
            "case_number", "channel_id", "client_id", "client_display", "lead_lawyer_id", "lead_lawyer_display",
            "secretary_id", "secretary_display", "status", "request_type", "client_nick", "static_id",
            "bank_account", "phone", "passport_url", "situation_text", "situation_author_id", "situation_author_display",
            "claim_link", "claim_message_id", "portfolio_message_id", "portfolio_description", "created_by_id",
            "created_by_display", "closed_at", "archived_at", "archive_category_id"
        },
        "int_fields": {"case_number", "channel_id", "client_id", "lead_lawyer_id", "secretary_id", "situation_author_id", "claim_message_id", "portfolio_message_id", "created_by_id", "archive_category_id"},
    },
    "client": {
        "table": "client_profiles",
        "id_field": "id",
        "fields": {"discord_user_id", "client_nick", "static_id", "bank_account", "phone", "passport_url", "notes", "last_case_id", "last_used_at"},
        "int_fields": {"discord_user_id", "last_case_id"},
    },
    "lawyer": {
        "table": "lawyer_profiles",
        "id_field": "id",
        "fields": {"discord_user_id", "lawyer_nick", "static_id", "bank_account", "phone", "email", "passport_url", "notes"},
        "int_fields": {"discord_user_id"},
    },
    "receipt": {
        "table": "sgl_receipts",
        "id_field": "id",
        "fields": {
            "case_id", "case_number", "channel_id", "client_id", "lead_lawyer_id", "created_by_id",
            "created_by_display", "court_code", "court_label", "court_suffix", "total_amount", "lawyer_amount",
            "duty_amount", "lawyer_bank", "duty_bank", "invoice_message_id", "proof_services_url",
            "proof_duty_url", "proof_submitted_by_id", "proof_submitted_by_display", "proof_submitted_at",
            "confirmation_message_id", "confirmed_by_id", "confirmed_by_display", "confirmed_at", "status"
        },
        "int_fields": {"case_id", "case_number", "channel_id", "client_id", "lead_lawyer_id", "created_by_id", "total_amount", "lawyer_amount", "duty_amount", "invoice_message_id", "proof_submitted_by_id", "confirmation_message_id", "confirmed_by_id"},
    },
}


def normalize_admin_target(target: str) -> str | None:
    raw = str(target or "").strip().lower()
    aliases = {
        "case": "case", "cases": "case", "кейс": "case", "кейсы": "case",
        "client": "client", "clients": "client", "клиент": "client", "клиенты": "client",
        "lawyer": "lawyer", "lawyers": "lawyer", "адвокат": "lawyer", "адвокаты": "lawyer",
        "receipt": "receipt", "receipts": "receipt", "чек": "receipt", "чеки": "receipt",
    }
    return aliases.get(raw)


def admin_allowed_fields(target: str) -> list[str]:
    target = normalize_admin_target(target) or target
    cfg = _ADMIN_TARGETS.get(target)
    return sorted(cfg["fields"]) if cfg else []


def _admin_parse_identifier(target: str, identifier: str) -> int | None:
    value = str(identifier or "").strip()
    if target == "case":
        value = value.lstrip("#").replace("SGL-", "").replace("sgl-", "")
    value = value.lstrip("0") or "0"
    try:
        return int(value)
    except ValueError:
        return None


def _admin_record_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def admin_get_record(guild_id: int, target: str, identifier: str) -> dict[str, Any] | None:
    target = normalize_admin_target(target) or ""
    cfg = _ADMIN_TARGETS.get(target)
    if not cfg:
        return None
    parsed_id = _admin_parse_identifier(target, identifier)
    if parsed_id is None:
        return None
    table = cfg["table"]
    id_field = cfg["id_field"]
    with _db_lock, connect() as con:
        row = con.execute(f"SELECT * FROM {table} WHERE guild_id = ? AND {id_field} = ?", (guild_id, parsed_id)).fetchone()
    return _admin_record_to_dict(row)


def admin_format_record(record: dict[str, Any], *, max_value_length: int = 300) -> str:
    lines = []
    for key in sorted(record.keys()):
        value = record.get(key)
        if value is None:
            value_text = "NULL"
        else:
            value_text = str(value).replace("\n", "\\n")
            if len(value_text) > max_value_length:
                value_text = value_text[:max_value_length] + "..."
        lines.append(f"{key}: {value_text}")
    return "\n".join(lines)


def _coerce_admin_value(target: str, field: str, value: str) -> Any:
    if value is None:
        return None
    raw = str(value)
    if raw.strip().lower() in {"null", "none", "пусто", "empty", "__null__"}:
        return None
    cfg = _ADMIN_TARGETS[target]
    if field in cfg["int_fields"]:
        cleaned = re.sub(r"[^0-9-]", "", raw)
        if cleaned in {"", "-"}:
            return None
        return int(cleaned)
    return raw


def admin_update_record(
    *,
    guild_id: int,
    target: str,
    identifier: str,
    field: str,
    value: str,
    actor_id: int | None = None,
    actor_display: str | None = None,
) -> dict[str, Any] | None:
    target = normalize_admin_target(target) or ""
    cfg = _ADMIN_TARGETS.get(target)
    if not cfg:
        raise ValueError("unknown_target")
    field = str(field or "").strip()
    if field not in cfg["fields"]:
        raise ValueError("field_not_allowed")
    parsed_id = _admin_parse_identifier(target, identifier)
    if parsed_id is None:
        raise ValueError("bad_identifier")
    table = cfg["table"]
    id_field = cfg["id_field"]
    new_value = _coerce_admin_value(target, field, value)
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        before = con.execute(f"SELECT * FROM {table} WHERE guild_id = ? AND {id_field} = ?", (guild_id, parsed_id)).fetchone()
        if before is None:
            con.commit()
            return None
        updates = f"{field} = ?"
        params = [new_value]
        if "updated_at" in _table_columns(con, table):
            updates += ", updated_at = ?"
            params.append(now)
        params.extend([guild_id, parsed_id])
        con.execute(f"UPDATE {table} SET {updates} WHERE guild_id = ? AND {id_field} = ?", params)
        after = con.execute(f"SELECT * FROM {table} WHERE guild_id = ? AND {id_field} = ?", (guild_id, parsed_id)).fetchone()
        if target == "case":
            _record_case_event(
                con,
                case_id=int(before["id"]),
                guild_id=guild_id,
                case_number=int(before["case_number"]),
                actor_id=actor_id,
                actor_display=actor_display,
                action="manual_edit",
                details=f"{field}: {before[field] if field in before.keys() else None} -> {new_value}",
            )
        target_types = {
            "case": "sgl_case",
            "client": "client_profile",
            "lawyer": "lawyer_profile",
            "receipt": "sgl_receipt",
        }
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="bureau",
            action_kind="generic_row_restore",
            target_type=target_types[target],
            target_id=int(before["id"]),
            summary=f"Административное изменение {target} #{parsed_id}: {field}",
            payload={"table": table, "row_id": int(before["id"]), "before": dict(before)},
            now=now,
        )
        con.commit()
    return _admin_record_to_dict(after)


def admin_delete_record(
    *,
    guild_id: int,
    target: str,
    identifier: str,
    actor_id: int | None = None,
    actor_display: str | None = None,
) -> dict[str, Any] | None:
    target = normalize_admin_target(target) or ""
    cfg = _ADMIN_TARGETS.get(target)
    if not cfg:
        raise ValueError("unknown_target")
    parsed_id = _admin_parse_identifier(target, identifier)
    if parsed_id is None:
        raise ValueError("bad_identifier")
    table = cfg["table"]
    id_field = cfg["id_field"]
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(f"SELECT * FROM {table} WHERE guild_id = ? AND {id_field} = ?", (guild_id, parsed_id)).fetchone()
        if row is None:
            con.commit()
            return None
        record = _admin_record_to_dict(row)
        if target == "case":
            case_id = int(row["id"])
            case_number = int(row["case_number"])
            receipt_rows = con.execute(
                "SELECT * FROM sgl_receipts WHERE guild_id = ? AND (case_id = ? OR case_number = ?)",
                (guild_id, case_id, case_number),
            ).fetchall()
            event_rows = con.execute(
                "SELECT * FROM sgl_case_events WHERE guild_id = ? AND (case_id = ? OR case_number = ?)",
                (guild_id, case_id, case_number),
            ).fetchall()
            _record_case_event(con, case_id=case_id, guild_id=guild_id, case_number=case_number, actor_id=actor_id, actor_display=actor_display, action="manual_delete", details="case deleted")
            latest_event = con.execute(
                "SELECT * FROM sgl_case_events WHERE guild_id = ? AND case_id = ? ORDER BY id DESC LIMIT 1",
                (guild_id, case_id),
            ).fetchone()
            snapshots = [{"table": "sgl_cases", "row_id": case_id, "before": dict(row)}]
            snapshots.extend(
                {"table": "sgl_receipts", "row_id": int(item["id"]), "before": dict(item)}
                for item in receipt_rows
            )
            snapshots.extend(
                {"table": "sgl_case_events", "row_id": int(item["id"]), "before": dict(item)}
                for item in event_rows
            )
            if latest_event is not None:
                snapshots.append(
                    {"table": "sgl_case_events", "row_id": int(latest_event["id"]), "before": dict(latest_event)}
                )
            _record_bot_action(
                con,
                guild_id=guild_id,
                actor_id=actor_id,
                actor_display=actor_display,
                module="bureau",
                action_kind="generic_rows_restore",
                target_type="sgl_case",
                target_id=case_id,
                summary=f"Удалено дело №{case_number}",
                payload={"rows": snapshots},
            )
            con.execute("DELETE FROM sgl_receipts WHERE guild_id = ? AND (case_id = ? OR case_number = ?)", (guild_id, case_id, case_number))
            con.execute("DELETE FROM sgl_case_events WHERE guild_id = ? AND (case_id = ? OR case_number = ?)", (guild_id, case_id, case_number))
            con.execute("DELETE FROM sgl_cases WHERE guild_id = ? AND id = ?", (guild_id, case_id))
        else:
            target_types = {"client": "client_profile", "lawyer": "lawyer_profile", "receipt": "sgl_receipt"}
            _record_bot_action(
                con,
                guild_id=guild_id,
                actor_id=actor_id,
                actor_display=actor_display,
                module="bureau",
                action_kind="generic_row_restore",
                target_type=target_types[target],
                target_id=int(row["id"]),
                summary=f"Удалена запись {target} #{parsed_id}",
                payload={"table": table, "row_id": int(row["id"]), "before": dict(row)},
            )
            con.execute(f"DELETE FROM {table} WHERE guild_id = ? AND {id_field} = ?", (guild_id, parsed_id))
        con.commit()
    return record


def set_meta_value(key: str, value: str) -> None:
    with _db_lock, connect() as con:
        set_meta(con, key, value)
        con.commit()


def tvrs_next_bill_number(guild_id: int, default_next: int = 9) -> int:
    meta_key = f"tvrs_next_bill_number:{guild_id}"
    raw = get_meta(meta_key)
    if raw and str(raw).isdigit():
        return max(1, int(raw))
    with _db_lock, connect() as con:
        row = con.execute("SELECT MAX(bill_number) AS n FROM tvrs_bills WHERE guild_id = ?", (guild_id,)).fetchone()
    max_num = int(row["n"] or 0) if row else 0
    return max(default_next, max_num + 1)


def tvrs_set_next_bill_number(guild_id: int, next_number: int) -> None:
    set_meta_value(f"tvrs_next_bill_number:{guild_id}", str(max(1, int(next_number))))


def tvrs_set_last_accepted_bill_number(guild_id: int, last_number: int) -> int:
    next_number = max(1, int(last_number) + 1)
    tvrs_set_next_bill_number(guild_id, next_number)
    return next_number


def tvrs_create_bill(
    *,
    guild_id: int,
    channel_id: int | None,
    author_id: int,
    author_display: str | None,
    title: str,
    summary: str,
    materials: str | None,
) -> TVRSBill:
    now = utc_now_iso()
    meta_key = f"tvrs_next_bill_number:{guild_id}"
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        meta_row = con.execute("SELECT value FROM meta WHERE key = ?", (meta_key,)).fetchone()
        raw = str(meta_row["value"]) if meta_row else None
        if raw and raw.isdigit():
            number = max(1, int(raw))
        else:
            row = con.execute("SELECT MAX(bill_number) AS n FROM tvrs_bills WHERE guild_id = ?", (guild_id,)).fetchone()
            number = max(9, int(row["n"] or 0) + 1)
        cur = con.execute(
            """
            INSERT INTO tvrs_bills(guild_id, bill_number, channel_id, message_id, author_id, author_display, title, summary, materials, status, created_at, updated_at)
            VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (guild_id, number, channel_id, author_id, author_display, title, summary, materials, now, now),
        )
        set_meta(con, meta_key, str(number + 1))
        row = con.execute("SELECT * FROM tvrs_bills WHERE id = ?", (int(cur.lastrowid),)).fetchone()
        con.commit()
    bill = _tvrs_bill_from_row(row)
    if bill is None:
        raise RuntimeError("tvrs_bill_create_failed")
    return bill


def tvrs_create_bill_with_publication(
    *,
    guild_id: int,
    channel_id: int,
    author_id: int,
    author_display: str | None,
    title: str,
    summary: str,
    materials: str | None,
    delivery_topic: str,
    max_attempts: int = 12,
) -> tuple[TVRSBill, dict[str, Any], bool]:
    """Atomically create a bill and its durable public-card intent.

    An identical submission by the same author is reused for a short window.
    This covers an interaction retry after the database committed but Discord
    disconnected before the user received confirmation.
    """

    now = utc_now_iso()
    duplicate_after = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    clean_topic = str(delivery_topic).strip()
    if not clean_topic:
        raise ValueError("bill_publication_topic_required")
    meta_key = f"tvrs_next_bill_number:{int(guild_id)}"
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        active = con.execute(
            """
            SELECT 1 FROM tvrs_consensus_sessions
            WHERE guild_id = ? AND finished_at IS NULL LIMIT 1
            """,
            (int(guild_id),),
        ).fetchone()
        if active is not None:
            con.rollback()
            raise ValueError("bill_submission_locked_by_active_consensus")
        row = con.execute(
            """
            SELECT * FROM tvrs_bills
            WHERE guild_id = ? AND author_id = ? AND title = ? AND summary = ?
              AND COALESCE(materials, '') = COALESCE(?, '')
              AND status IN ('publishing', 'draft', 'queued', 'requeued')
              AND created_at >= ?
            ORDER BY id DESC LIMIT 1
            """,
            (
                int(guild_id),
                int(author_id),
                str(title),
                str(summary),
                materials,
                duplicate_after,
            ),
        ).fetchone()
        created = row is None
        if row is None:
            meta_row = con.execute("SELECT value FROM meta WHERE key = ?", (meta_key,)).fetchone()
            raw = str(meta_row["value"]) if meta_row else None
            if raw and raw.isdigit():
                number = max(1, int(raw))
            else:
                latest = con.execute(
                    "SELECT MAX(bill_number) AS n FROM tvrs_bills WHERE guild_id = ?",
                    (int(guild_id),),
                ).fetchone()
                number = max(9, int(latest["n"] or 0) + 1)
            inserted = con.execute(
                """
                INSERT INTO tvrs_bills(
                    guild_id, bill_number, channel_id, message_id, author_id,
                    author_display, title, summary, materials, status, created_at, updated_at
                ) VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, 'publishing', ?, ?)
                """,
                (
                    int(guild_id),
                    number,
                    int(channel_id),
                    int(author_id),
                    author_display,
                    str(title),
                    str(summary),
                    materials,
                    now,
                    now,
                ),
            )
            set_meta(con, meta_key, str(number + 1))
            row = con.execute(
                "SELECT * FROM tvrs_bills WHERE id = ?",
                (int(inserted.lastrowid),),
            ).fetchone()
        if row is None:  # pragma: no cover - insert/select is in one transaction
            con.rollback()
            raise RuntimeError("tvrs_bill_create_failed")
        bill = dict(row)
        delivery = delivery_outbox_enqueue_in_connection(
            con,
            topic=clean_topic,
            dedupe_key=f"tvrs:bill:{int(bill['id'])}:publication",
            payload={
                "payload_version": 1,
                "guild_id": int(guild_id),
                "channel_id": int(channel_id),
                "publication_kind": "initial",
                "bill": {
                    key: bill.get(key)
                    for key in (
                        "id",
                        "guild_id",
                        "bill_number",
                        "channel_id",
                        "message_id",
                        "author_id",
                        "author_display",
                        "title",
                        "summary",
                        "materials",
                        "status",
                    )
                },
            },
            max_attempts=max_attempts,
            now=now,
        )
        if str(delivery.get("status") or "") == "dead":
            con.execute(
                """
                UPDATE delivery_outbox
                SET status = 'retry', attempts = 0, available_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                    last_error = NULL, dead_notified_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'dead'
                """,
                (now, now, int(delivery["id"])),
            )
            refreshed = con.execute(
                "SELECT * FROM delivery_outbox WHERE id = ?",
                (int(delivery["id"]),),
            ).fetchone()
            if refreshed is not None:
                delivery = dict(refreshed)
        con.commit()
    result = _tvrs_bill_from_row(row)
    if result is None:  # pragma: no cover - row validated above
        raise RuntimeError("tvrs_bill_create_failed")
    return result, delivery, created


def tvrs_set_bill_message(bill_id: int, message_id: int, channel_id: int | None = None) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        if channel_id is None:
            con.execute("UPDATE tvrs_bills SET message_id = ?, updated_at = ? WHERE id = ?", (message_id, now, bill_id))
        else:
            con.execute("UPDATE tvrs_bills SET message_id = ?, channel_id = ?, updated_at = ? WHERE id = ?", (message_id, channel_id, now, bill_id))
        con.commit()


def tvrs_complete_bill_publication(
    bill_id: int,
    message_id: int,
    channel_id: int,
) -> dict[str, Any] | None:
    """Bind the public card and release an initial bill into the queue."""

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            UPDATE tvrs_bills
            SET message_id = ?, channel_id = ?,
                status = CASE WHEN status = 'publishing' THEN 'draft' ELSE status END,
                updated_at = ?
            WHERE id = ?
            """,
            (int(message_id), int(channel_id), now, int(bill_id)),
        )
        row = con.execute("SELECT * FROM tvrs_bills WHERE id = ?", (int(bill_id),)).fetchone()
        con.commit()
    return dict(row) if row is not None else None


def tvrs_get_bill_by_id(bill_id: int) -> TVRSBill | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM tvrs_bills WHERE id = ?", (bill_id,)).fetchone()
    return _tvrs_bill_from_row(row)


def tvrs_get_bill_by_message(guild_id: int, message_id: int) -> TVRSBill | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM tvrs_bills WHERE guild_id = ? AND message_id = ?", (guild_id, message_id)).fetchone()
    return _tvrs_bill_from_row(row)


def tvrs_cast_vote(*, guild_id: int, bill_id: int, voter_id: int, voter_display: str | None, vote: str) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        before = con.execute(
            "SELECT * FROM tvrs_votes WHERE guild_id = ? AND bill_id = ? AND voter_id = ?",
            (guild_id, bill_id, voter_id),
        ).fetchone()
        con.execute(
            """
            INSERT INTO tvrs_votes(guild_id, bill_id, voter_id, voter_display, vote, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bill_id, voter_id) DO UPDATE SET
                voter_display = excluded.voter_display, vote = excluded.vote, updated_at = excluded.updated_at
            """,
            (guild_id, bill_id, voter_id, voter_display, vote, now, now),
        )
        after = con.execute(
            "SELECT * FROM tvrs_votes WHERE guild_id = ? AND bill_id = ? AND voter_id = ?",
            (guild_id, bill_id, voter_id),
        ).fetchone()
        if after is not None:
            _record_bot_action(
                con,
                guild_id=guild_id,
                actor_id=voter_id,
                actor_display=voter_display,
                module="tvrs",
                action_kind="generic_row_restore",
                target_type="tvrs_vote",
                target_id=int(after["id"]),
                summary=f"Голос по законопроекту #{bill_id}: {vote}",
                payload={"table": "tvrs_votes", "row_id": int(after["id"]), "before": None if before is None else dict(before)},
                now=now,
            )
        con.commit()


def tvrs_votes_for_bill(guild_id: int, bill_id: int) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute("SELECT * FROM tvrs_votes WHERE guild_id = ? AND bill_id = ? ORDER BY updated_at ASC", (guild_id, bill_id)).fetchall()
    return [dict(row) for row in rows]


def tvrs_vote_counts(guild_id: int, bill_id: int) -> dict[str, int]:
    counts = {"approve": 0, "abstain": 0, "reject": 0}
    for row in tvrs_votes_for_bill(guild_id, bill_id):
        vote = str(row.get("vote") or "")
        if vote in counts:
            counts[vote] += 1
    counts["total"] = sum(counts.values())
    return counts


def tvrs_recent_bills(guild_id: int, limit: int = 10) -> list[TVRSBill]:
    with _db_lock, connect() as con:
        rows = con.execute("SELECT * FROM tvrs_bills WHERE guild_id = ? ORDER BY bill_number DESC LIMIT ?", (guild_id, limit)).fetchall()
    return [b for row in rows if (b := _tvrs_bill_from_row(row)) is not None]


# ---------------- TVRS live plenary consensus ----------------


def tvrs_consensus_save_session(
    snapshot: dict[str, Any],
    *,
    event_type: str,
    actor_id: int | None = None,
    actor_display: str | None = None,
    stage_from: str | None = None,
    details: dict[str, Any] | None = None,
    deliveries: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    session_key = str(snapshot.get("session_key") or "").strip()
    if not session_key:
        raise ValueError("consensus_session_key_required")
    guild_id = int(snapshot.get("guild_id") or 0)
    if guild_id <= 0:
        raise ValueError("consensus_guild_id_required")
    stage = str(snapshot.get("stage") or "").strip()
    if not stage:
        raise ValueError("consensus_stage_required")
    current_bill = snapshot.get("current_bill") if isinstance(snapshot.get("current_bill"), dict) else {}
    current_bill_id = int(current_bill.get("id") or 0) or None
    now = utc_now_iso()
    created_at = str(snapshot.get("created_at") or now)
    finished = bool(snapshot.get("finished")) or stage in {"finished", "cancelled"}
    expected_revision = int(snapshot.get("revision") or 0)
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT revision, stage, guild_id, created_at, finished_at FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        if existing is None:
            if expected_revision != 0:
                con.rollback()
                raise RuntimeError("consensus_session_revision_conflict")
            revision = 1
        else:
            if existing["finished_at"] is not None:
                con.rollback()
                raise RuntimeError("consensus_session_already_finished")
            if int(existing["guild_id"] or 0) != guild_id:
                con.rollback()
                raise RuntimeError("consensus_session_guild_conflict")
            if int(existing["revision"] or 0) != expected_revision:
                con.rollback()
                raise RuntimeError("consensus_session_revision_conflict")
            revision = expected_revision + 1
        stored_snapshot = dict(snapshot)
        stored_snapshot["revision"] = revision
        payload = json.dumps(stored_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        previous_stage = stage_from if stage_from is not None else (str(existing["stage"]) if existing else None)
        if existing is None:
            con.execute(
                """
                INSERT INTO tvrs_consensus_sessions(
                    session_key, guild_id, plenary_number, stage, leader_id,
                    current_bill_id, snapshot_json, revision, created_at, updated_at, finished_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_key,
                    guild_id,
                    int(snapshot.get("plenary_number") or 0),
                    stage,
                    int(snapshot.get("leader_id") or 0),
                    current_bill_id,
                    payload,
                    revision,
                    created_at,
                    now,
                    now if finished else None,
                ),
            )
        else:
            updated = con.execute(
                """
                UPDATE tvrs_consensus_sessions
                SET plenary_number = ?, stage = ?, leader_id = ?, current_bill_id = ?,
                    snapshot_json = ?, revision = ?, updated_at = ?, finished_at = ?
                WHERE session_key = ? AND revision = ? AND finished_at IS NULL
                """,
                (
                    int(snapshot.get("plenary_number") or 0),
                    stage,
                    int(snapshot.get("leader_id") or 0),
                    current_bill_id,
                    payload,
                    revision,
                    now,
                    now if finished else None,
                    session_key,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                con.rollback()
                raise RuntimeError("consensus_session_revision_conflict")
        con.execute(
            """
            INSERT INTO tvrs_consensus_events(
                session_key, guild_id, event_type, actor_id, actor_display,
                stage_from, stage_to, details_json, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_key,
                guild_id,
                str(event_type)[:80],
                actor_id,
                actor_display,
                previous_stage,
                stage,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        for delivery in deliveries:
            delivery_outbox_enqueue_in_connection(
                con,
                topic=str(delivery.get("topic") or ""),
                dedupe_key=str(delivery.get("dedupe_key") or ""),
                payload=dict(delivery.get("payload") or {}),
                max_attempts=int(delivery.get("max_attempts") or 8),
                available_at=delivery.get("available_at"),
                now=now,
            )
        row = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        con.commit()
    return dict(row) if row else {}


def tvrs_consensus_commit_begin_bill(
    snapshot: dict[str, Any],
    *,
    expected_revision: int,
    bill_id: int,
    event_type: str,
    actor_id: int | None,
    actor_display: str | None,
    details: dict[str, Any] | None = None,
    deliveries: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Atomically activate a bill, persist the voting stage and enqueue DMs."""

    session_key = str(snapshot.get("session_key") or "").strip()
    guild_id = int(snapshot.get("guild_id") or 0)
    expected = int(expected_revision)
    selected_bill_id = int(bill_id)
    current_bill = snapshot.get("current_bill") if isinstance(snapshot.get("current_bill"), dict) else {}
    if not session_key or guild_id <= 0 or expected < 1 or selected_bill_id <= 0:
        raise ValueError("consensus_begin_bill_identity_required")
    if str(snapshot.get("stage") or "") != "voting" or int(current_bill.get("id") or 0) != selected_bill_id:
        raise ValueError("consensus_begin_bill_snapshot_invalid")

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        persisted = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        if persisted is None:
            con.rollback()
            raise RuntimeError("consensus_session_not_persisted")
        if (
            str(persisted["stage"] or "") == "voting"
            and int(persisted["current_bill_id"] or 0) == selected_bill_id
            and int(persisted["revision"] or 0) == expected + 1
        ):
            outbox_ids: list[int] = []
            for delivery in deliveries:
                outbox_row = con.execute(
                    "SELECT id FROM delivery_outbox WHERE topic = ? AND dedupe_key = ?",
                    (str(delivery.get("topic") or ""), str(delivery.get("dedupe_key") or "")),
                ).fetchone()
                if outbox_row is None:
                    con.rollback()
                    raise RuntimeError("consensus_begin_bill_delivery_missing")
                outbox_ids.append(int(outbox_row["id"]))
            con.commit()
            return {"session": dict(persisted), "outbox_ids": outbox_ids, "idempotent": True}
        if (
            int(persisted["revision"] or 0) != expected
            or str(persisted["stage"] or "") not in {"registration", "after_result"}
            or persisted["current_bill_id"] is not None
            or persisted["finished_at"] is not None
        ):
            con.rollback()
            raise RuntimeError("consensus_begin_bill_revision_conflict")
        bill_update = con.execute(
            """
            UPDATE tvrs_bills SET status = 'voting', updated_at = ?
            WHERE id = ? AND guild_id = ? AND status IN ('draft', 'queued', 'requeued')
            """,
            (now, selected_bill_id, guild_id),
        )
        if bill_update.rowcount != 1:
            con.rollback()
            raise RuntimeError("consensus_begin_bill_unavailable")

        new_revision = expected + 1
        stored_snapshot = dict(snapshot)
        stored_snapshot["revision"] = new_revision
        snapshot_json = json.dumps(stored_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        session_update = con.execute(
            """
            UPDATE tvrs_consensus_sessions
            SET stage = 'voting', current_bill_id = ?, snapshot_json = ?, revision = ?, updated_at = ?
            WHERE session_key = ? AND revision = ? AND finished_at IS NULL
              AND current_bill_id IS NULL
            """,
            (selected_bill_id, snapshot_json, new_revision, now, session_key, expected),
        )
        if session_update.rowcount != 1:
            con.rollback()
            raise RuntimeError("consensus_begin_bill_revision_conflict")
        con.execute(
            """
            INSERT INTO tvrs_consensus_events(
                session_key, guild_id, event_type, actor_id, actor_display,
                stage_from, stage_to, details_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'voting', ?, ?)
            """,
            (
                session_key,
                guild_id,
                str(event_type)[:80],
                actor_id,
                actor_display,
                str(persisted["stage"] or ""),
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        outbox_ids: list[int] = []
        for delivery in deliveries:
            outbox_row = delivery_outbox_enqueue_in_connection(
                con,
                topic=str(delivery.get("topic") or ""),
                dedupe_key=str(delivery.get("dedupe_key") or ""),
                payload=dict(delivery.get("payload") or {}),
                max_attempts=int(delivery.get("max_attempts") or 8),
                available_at=delivery.get("available_at"),
                now=now,
            )
            outbox_ids.append(int(outbox_row["id"]))
        final_session = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        con.commit()
    return {
        "session": dict(final_session) if final_session is not None else {},
        "outbox_ids": outbox_ids,
        "idempotent": False,
    }


def tvrs_consensus_commit_finalization(
    snapshot: dict[str, Any],
    *,
    expected_revision: int,
    result: dict[str, Any],
    bill_status: str,
    result_summary: str,
    event_type: str,
    actor_id: int | None = None,
    actor_display: str | None = None,
    details: dict[str, Any] | None = None,
    deliveries: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Commit a consensus decision and every delivery intent atomically.

    This is the production write path for finalization.  The legacy granular
    functions remain available for administration and compatibility, but the
    live workflow must not create a result, advance the session, and enqueue
    notifications in separate transactions.
    """

    session_key = str(snapshot.get("session_key") or "").strip()
    guild_id = int(snapshot.get("guild_id") or 0)
    bill_id = int(result.get("bill_id") or 0)
    if not session_key or guild_id <= 0 or bill_id <= 0:
        raise ValueError("consensus_finalization_identity_required")
    if str(snapshot.get("stage") or "") != "after_result" or snapshot.get("current_bill") is not None:
        raise ValueError("consensus_finalization_snapshot_invalid")
    expected = int(expected_revision)
    if expected < 1:
        raise ValueError("consensus_finalization_revision_required")

    try:
        normalized_votes_json = json.dumps(
            json.loads(str(result.get("votes_json") or "{}")),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("consensus_result_votes_invalid") from exc

    def result_conflicts(row: sqlite3.Row, expected_values: dict[str, Any]) -> bool:
        for key, value in expected_values.items():
            if key == "votes_json":
                try:
                    persisted_votes = json.loads(str(row[key] or "{}"))
                    expected_votes = json.loads(str(value or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    return True
                if persisted_votes != expected_votes:
                    return True
            elif row[key] != value:
                return True
        return False

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        persisted = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        if persisted is None:
            con.rollback()
            raise RuntimeError("consensus_session_not_persisted")

        persisted_stage = str(persisted["stage"] or "")
        persisted_revision = int(persisted["revision"] or 0)
        existing_result = con.execute(
            "SELECT * FROM tvrs_live_results WHERE session_key = ? AND bill_id = ?",
            (session_key, bill_id),
        ).fetchone()
        if persisted_stage == "after_result" and existing_result is not None:
            # The whole transaction either committed or did not.  Returning the
            # previous receipt makes recovery and caller retries idempotent.
            expected_existing = {
                "guild_id": guild_id,
                "bill_number": int(result.get("bill_number") or 0),
                "bill_title": str(result.get("bill_title") or result.get("title") or ""),
                "status": str(result.get("status") or bill_status),
                "internal_percent": float(result.get("internal_percent") or 0.0),
                "overall_percent": float(result.get("overall_percent") or 0.0),
                "internal_active": 1 if bool(result.get("internal_active")) else 0,
                "votes_json": normalized_votes_json,
                "veto_by_id": int(result["veto_by_id"]) if result.get("veto_by_id") else None,
                "veto_by_display": result.get("veto_by_display"),
            }
            if result_conflicts(existing_result, expected_existing):
                con.rollback()
                raise RuntimeError("consensus_result_conflict")
            outbox_ids: list[int] = []
            for delivery in deliveries:
                outbox_row = con.execute(
                    "SELECT id FROM delivery_outbox WHERE topic = ? AND dedupe_key = ?",
                    (str(delivery.get("topic") or ""), str(delivery.get("dedupe_key") or "")),
                ).fetchone()
                if outbox_row is not None:
                    outbox_ids.append(int(outbox_row["id"]))
            con.commit()
            return {
                "session": dict(persisted),
                "result_id": int(existing_result["id"]),
                "outbox_ids": outbox_ids,
                "idempotent": True,
            }
        if (
            persisted_stage != "finalizing"
            or persisted_revision != expected
            or int(persisted["current_bill_id"] or 0) != bill_id
        ):
            con.rollback()
            raise RuntimeError("consensus_finalization_revision_conflict")
        try:
            persisted_snapshot = json.loads(str(persisted["snapshot_json"] or "{}"))
            pending_kind = str(dict(persisted_snapshot.get("pending_action") or {}).get("kind") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            pending_kind = ""
        result_kind = "veto" if str(result.get("status") or bill_status) == "vetoed" else "vote"
        if pending_kind != result_kind:
            con.rollback()
            raise RuntimeError("consensus_finalization_kind_conflict")

        result_values = (
            guild_id,
            session_key,
            int(snapshot.get("plenary_number") or 0),
            bill_id,
            int(result.get("bill_number") or 0),
            str(result.get("bill_title") or result.get("title") or ""),
            str(result.get("status") or bill_status),
            float(result.get("internal_percent") or 0.0),
            float(result.get("overall_percent") or 0.0),
            1 if bool(result.get("internal_active")) else 0,
            normalized_votes_json,
            int(result["veto_by_id"]) if result.get("veto_by_id") else None,
            result.get("veto_by_display"),
            now,
        )
        if existing_result is None:
            cur = con.execute(
                """
                INSERT INTO tvrs_live_results(
                    guild_id, session_key, plenary_number, bill_id, bill_number,
                    bill_title, status, internal_percent, overall_percent,
                    internal_active, votes_json, veto_by_id, veto_by_display, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                result_values,
            )
            result_id = int(cur.lastrowid)
        else:
            comparable = {
                "guild_id": guild_id,
                "bill_number": int(result.get("bill_number") or 0),
                "bill_title": str(result.get("bill_title") or result.get("title") or ""),
                "status": str(result.get("status") or bill_status),
                "internal_percent": float(result.get("internal_percent") or 0.0),
                "overall_percent": float(result.get("overall_percent") or 0.0),
                "internal_active": 1 if bool(result.get("internal_active")) else 0,
                "votes_json": normalized_votes_json,
                "veto_by_id": int(result["veto_by_id"]) if result.get("veto_by_id") else None,
                "veto_by_display": result.get("veto_by_display"),
            }
            if result_conflicts(existing_result, comparable):
                con.rollback()
                raise RuntimeError("consensus_result_conflict")
            result_id = int(existing_result["id"])

        bill_update = con.execute(
            """
            UPDATE tvrs_bills
            SET status = ?, result_summary = ?,
                vetoed_by_id = COALESCE(?, vetoed_by_id),
                vetoed_by_display = COALESCE(?, vetoed_by_display), updated_at = ?
            WHERE id = ? AND guild_id = ?
            """,
            (
                str(bill_status),
                str(result_summary),
                int(result["veto_by_id"]) if result.get("veto_by_id") else None,
                result.get("veto_by_display"),
                now,
                bill_id,
                guild_id,
            ),
        )
        if bill_update.rowcount != 1:
            con.rollback()
            raise RuntimeError("consensus_bill_not_found")

        retry_bill_number = int(result.get("retry_bill_number") or 0)
        if str(result.get("status") or bill_status) == "vetoed" and retry_bill_number:
            retry_update = con.execute(
                """
                UPDATE tvrs_bills
                SET status = 'requeued', updated_at = ?
                WHERE guild_id = ? AND bill_number = ?
                  AND status IN ('pending_veto', 'requeued')
                """,
                (now, guild_id, retry_bill_number),
            )
            if retry_update.rowcount != 1:
                con.rollback()
                raise RuntimeError("consensus_retry_bill_not_found")

        new_revision = expected + 1
        stored_snapshot = dict(snapshot)
        stored_snapshot["revision"] = new_revision
        snapshot_json = json.dumps(stored_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        session_update = con.execute(
            """
            UPDATE tvrs_consensus_sessions
            SET stage = 'after_result', current_bill_id = NULL, snapshot_json = ?,
                revision = ?, updated_at = ?, finished_at = NULL
            WHERE session_key = ? AND revision = ? AND stage = 'finalizing'
              AND current_bill_id = ?
            """,
            (snapshot_json, new_revision, now, session_key, expected, bill_id),
        )
        if session_update.rowcount != 1:
            con.rollback()
            raise RuntimeError("consensus_finalization_revision_conflict")
        con.execute(
            """
            INSERT INTO tvrs_consensus_events(
                session_key, guild_id, event_type, actor_id, actor_display,
                stage_from, stage_to, details_json, created_at
            ) VALUES(?, ?, ?, ?, ?, 'finalizing', 'after_result', ?, ?)
            """,
            (
                session_key,
                guild_id,
                str(event_type)[:80],
                actor_id,
                actor_display,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        outbox_ids: list[int] = []
        for delivery in deliveries:
            row = delivery_outbox_enqueue_in_connection(
                con,
                topic=str(delivery.get("topic") or ""),
                dedupe_key=str(delivery.get("dedupe_key") or ""),
                payload=dict(delivery.get("payload") or {}),
                max_attempts=int(delivery.get("max_attempts") or 8),
                available_at=delivery.get("available_at"),
                now=now,
            )
            outbox_ids.append(int(row["id"]))
        final_session = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        con.commit()
    return {
        "session": dict(final_session) if final_session is not None else {},
        "result_id": result_id,
        "outbox_ids": outbox_ids,
        "idempotent": False,
    }


def tvrs_consensus_commit_finish(
    snapshot: dict[str, Any],
    *,
    expected_revision: int,
    current_bill_id: int | None,
    advance_plenary: bool,
    event_type: str,
    actor_id: int | None,
    actor_display: str | None,
    details: dict[str, Any] | None = None,
    deliveries: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Atomically close a session and release any in-flight bill."""

    session_key = str(snapshot.get("session_key") or "").strip()
    guild_id = int(snapshot.get("guild_id") or 0)
    target_stage = str(snapshot.get("stage") or "")
    expected = int(expected_revision)
    if not session_key or guild_id <= 0 or expected < 1:
        raise ValueError("consensus_finish_identity_required")
    if target_stage not in {"finished", "cancelled"} or not bool(snapshot.get("finished")):
        raise ValueError("consensus_finish_snapshot_invalid")
    if snapshot.get("current_bill") is not None:
        raise ValueError("consensus_finish_bill_not_cleared")

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        persisted = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        if persisted is None:
            con.rollback()
            raise RuntimeError("consensus_session_not_persisted")
        if str(persisted["stage"] or "") == target_stage and persisted["finished_at"]:
            con.commit()
            return {"session": dict(persisted), "idempotent": True}
        if int(persisted["revision"] or 0) != expected or persisted["finished_at"]:
            con.rollback()
            raise RuntimeError("consensus_finish_revision_conflict")

        persisted_bill_id = int(persisted["current_bill_id"] or 0)
        requested_bill_id = int(current_bill_id or 0)
        if persisted_bill_id != requested_bill_id:
            con.rollback()
            raise RuntimeError("consensus_finish_bill_conflict")
        if requested_bill_id:
            bill_update = con.execute(
                """
                UPDATE tvrs_bills
                SET status = 'requeued', result_summary = ?, updated_at = ?
                WHERE id = ? AND guild_id = ? AND status IN ('voting', 'requeued')
                """,
                (
                    "Рассмотрение отложено из-за завершения консенсуса.",
                    now,
                    requested_bill_id,
                    guild_id,
                ),
            )
            if bill_update.rowcount != 1:
                con.rollback()
                raise RuntimeError("consensus_finish_bill_not_requeued")

        if advance_plenary:
            meta_key = f"tvrs_next_plenary_number:{guild_id}"
            next_number = max(1, int(snapshot.get("plenary_number") or 0) + 1)
            set_meta(con, meta_key, str(next_number))

        new_revision = expected + 1
        stored_snapshot = dict(snapshot)
        stored_snapshot["revision"] = new_revision
        snapshot_json = json.dumps(stored_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        update = con.execute(
            """
            UPDATE tvrs_consensus_sessions
            SET stage = ?, current_bill_id = NULL, snapshot_json = ?, revision = ?,
                updated_at = ?, finished_at = ?
            WHERE session_key = ? AND revision = ? AND finished_at IS NULL
              AND COALESCE(current_bill_id, 0) = ?
            """,
            (
                target_stage,
                snapshot_json,
                new_revision,
                now,
                now,
                session_key,
                expected,
                requested_bill_id,
            ),
        )
        if update.rowcount != 1:
            con.rollback()
            raise RuntimeError("consensus_finish_revision_conflict")
        con.execute(
            """
            INSERT INTO tvrs_consensus_events(
                session_key, guild_id, event_type, actor_id, actor_display,
                stage_from, stage_to, details_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_key,
                guild_id,
                str(event_type)[:80],
                actor_id,
                actor_display,
                str(persisted["stage"] or ""),
                target_stage,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        outbox_ids: list[int] = []
        for delivery in deliveries:
            outbox_row = delivery_outbox_enqueue_in_connection(
                con,
                topic=str(delivery.get("topic") or ""),
                dedupe_key=str(delivery.get("dedupe_key") or ""),
                payload=dict(delivery.get("payload") or {}),
                max_attempts=int(delivery.get("max_attempts") or 8),
                available_at=delivery.get("available_at"),
                now=now,
            )
            outbox_ids.append(int(outbox_row["id"]))
        final_session = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        con.commit()
    return {
        "session": dict(final_session) if final_session is not None else {},
        "outbox_ids": outbox_ids,
        "idempotent": False,
    }


def tvrs_consensus_active_sessions(guild_id: int | None = None) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        if guild_id is None:
            rows = con.execute(
                """
                SELECT * FROM tvrs_consensus_sessions
                WHERE finished_at IS NULL
                ORDER BY updated_at ASC
                """
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT * FROM tvrs_consensus_sessions
                WHERE guild_id = ? AND finished_at IS NULL
                ORDER BY updated_at ASC
                """,
                (int(guild_id),),
            ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            snapshot = json.loads(str(row["snapshot_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            # Keep enough identity for the restore layer to quarantine the row.
            # Silently skipping it would leave an invisible active session in
            # the database indefinitely.
            snapshot = {
                "snapshot_version": 0,
                "session_key": str(row["session_key"]),
                "guild_id": int(row["guild_id"]),
                "stage": str(row["stage"]),
                "corrupt_snapshot_json": True,
            }
        if isinstance(snapshot, dict):
            snapshot["revision"] = int(row["revision"] or 0)
            snapshot["persisted_updated_at"] = str(row["updated_at"] or "")
            result.append(snapshot)
    return result


def tvrs_consensus_events(session_key: str, limit: int = 200) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM tvrs_consensus_events
            WHERE session_key = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (str(session_key), max(1, min(int(limit), 1000))),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in reversed(rows):
        item = dict(row)
        try:
            item["details"] = json.loads(str(item.get("details_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            item["details"] = {}
        result.append(item)
    return result


def tvrs_consensus_quarantine_session(session_key: str, reason: str) -> bool:
    """Close an unreadable session so it cannot block the guild forever."""
    now = utc_now_iso()
    clean_reason = str(reason or "unreadable snapshot")[:1000]
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM tvrs_consensus_sessions WHERE session_key = ? AND finished_at IS NULL",
            (str(session_key),),
        ).fetchone()
        if row is None:
            con.rollback()
            return False
        current_bill_id = int(row["current_bill_id"] or 0)
        if current_bill_id:
            current_bill = con.execute(
                "SELECT original_bill_id FROM tvrs_bills WHERE id = ?",
                (current_bill_id,),
            ).fetchone()
            root_bill_id = (
                int(current_bill["original_bill_id"] or current_bill_id)
                if current_bill is not None
                else current_bill_id
            )
            con.execute(
                """
                DELETE FROM tvrs_bills
                WHERE guild_id = ? AND status = 'pending_veto' AND original_bill_id = ?
                """,
                (int(row["guild_id"]), root_bill_id),
            )
            con.execute(
                """
                UPDATE tvrs_bills
                SET status = 'requeued', result_summary = ?, updated_at = ?
                WHERE id = ? AND status = 'voting'
                """,
                ("Сессия восстановлению не подлежит; проект возвращён в очередь.", now, current_bill_id),
            )
        con.execute(
            """
            UPDATE tvrs_consensus_sessions
            SET stage = 'cancelled', updated_at = ?, finished_at = ?
            WHERE session_key = ?
            """,
            (now, now, str(session_key)),
        )
        con.execute(
            """
            INSERT INTO tvrs_consensus_events(
                session_key, guild_id, event_type, actor_id, actor_display,
                stage_from, stage_to, details_json, created_at
            )
            VALUES(?, ?, 'session_quarantined', NULL, NULL, ?, 'cancelled', ?, ?)
            """,
            (
                str(session_key),
                int(row["guild_id"]),
                str(row["stage"]),
                json.dumps({"reason": clean_reason}, ensure_ascii=False),
                now,
            ),
        )
        con.commit()
    return True


def tvrs_bill_row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def tvrs_queue_bills(guild_id: int, limit: int = 100) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM tvrs_bills
            WHERE guild_id = ? AND status IN ('draft', 'queued', 'requeued')
            ORDER BY bill_number ASC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def tvrs_get_bill_dict_by_id(bill_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM tvrs_bills WHERE id = ?", (bill_id,)).fetchone()
    return dict(row) if row else None


def tvrs_mark_bill_status(bill_id: int, status: str, result_summary: str | None = None, veto_by_id: int | None = None, veto_by_display: str | None = None) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE tvrs_bills
            SET status = ?, result_summary = ?, vetoed_by_id = COALESCE(?, vetoed_by_id), vetoed_by_display = COALESCE(?, vetoed_by_display), updated_at = ?
            WHERE id = ?
            """,
            (status, result_summary, veto_by_id, veto_by_display, now, bill_id),
        )
        con.commit()


def tvrs_create_retry_bill(original_bill_id: int, author_id: int, author_display: str | None) -> dict[str, Any] | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        original = con.execute("SELECT * FROM tvrs_bills WHERE id = ?", (original_bill_id,)).fetchone()
        if original is None:
            con.commit()
            return None
        original_dict = dict(original)
        current_attempt = int(original_dict.get("attempt") or 1)
        if current_attempt >= 3:
            con.commit()
            return None
        guild_id = int(original_dict["guild_id"])
        root_bill_id = int(original_dict.get("original_bill_id") or original_bill_id)
        next_attempt = current_attempt + 1
        existing_retry = con.execute(
            """
            SELECT * FROM tvrs_bills
            WHERE guild_id = ? AND original_bill_id = ? AND attempt = ?
            ORDER BY id ASC LIMIT 1
            """,
            (guild_id, root_bill_id, next_attempt),
        ).fetchone()
        if existing_retry is not None:
            con.commit()
            return dict(existing_retry)
        meta_key = f"tvrs_next_bill_number:{guild_id}"
        meta_row = con.execute("SELECT value FROM meta WHERE key = ?", (meta_key,)).fetchone()
        raw = str(meta_row["value"]) if meta_row else None
        if raw and raw.isdigit():
            number = max(1, int(raw))
        else:
            row = con.execute("SELECT MAX(bill_number) AS n FROM tvrs_bills WHERE guild_id = ?", (guild_id,)).fetchone()
            number = max(9, int(row["n"] or 0) + 1)
        title = str(original_dict.get("title") or "")
        summary = str(original_dict.get("summary") or "")
        retry_note = f"\n\nПовторная попытка консенсуса: {next_attempt}/3. Законопроект возвращён на рассмотрение после применения права вето."
        cur = con.execute(
            """
            INSERT INTO tvrs_bills(guild_id, bill_number, channel_id, message_id, author_id, author_display, title, summary, materials, status, created_at, updated_at, original_bill_id, attempt)
            VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, 'pending_veto', ?, ?, ?, ?)
            """,
            (
                guild_id,
                number,
                original_dict.get("channel_id"),
                author_id,
                author_display,
                title,
                summary + retry_note,
                original_dict.get("materials"),
                now,
                now,
                root_bill_id,
                next_attempt,
            ),
        )
        set_meta(con, meta_key, str(number + 1))
        row = con.execute("SELECT * FROM tvrs_bills WHERE id = ?", (int(cur.lastrowid),)).fetchone()
        con.commit()
    return dict(row) if row else None


def tvrs_get_next_plenary_number(guild_id: int, default_value: int = 4) -> int:
    raw = get_meta(f"tvrs_next_plenary_number:{guild_id}")
    if raw and str(raw).isdigit():
        return max(1, int(raw))
    return default_value


def tvrs_increment_plenary_number(guild_id: int, current_number: int) -> None:
    set_meta_value(f"tvrs_next_plenary_number:{guild_id}", str(max(1, int(current_number) + 1)))


def tvrs_save_live_result(
    *,
    guild_id: int,
    session_key: str,
    plenary_number: int,
    bill_id: int,
    bill_number: int,
    bill_title: str,
    status: str,
    internal_percent: float,
    overall_percent: float,
    internal_active: bool,
    votes_json: str,
    veto_by_id: int | None = None,
    veto_by_display: str | None = None,
) -> int:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        cur = con.execute(
            """
            INSERT INTO tvrs_live_results(guild_id, session_key, plenary_number, bill_id, bill_number, bill_title, status, internal_percent, overall_percent, internal_active, votes_json, veto_by_id, veto_by_display, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_key, bill_id) DO UPDATE SET
                plenary_number = excluded.plenary_number,
                bill_number = excluded.bill_number,
                bill_title = excluded.bill_title,
                status = excluded.status,
                internal_percent = excluded.internal_percent,
                overall_percent = excluded.overall_percent,
                internal_active = excluded.internal_active,
                votes_json = excluded.votes_json,
                veto_by_id = excluded.veto_by_id,
                veto_by_display = excluded.veto_by_display
            """,
            (guild_id, session_key, plenary_number, bill_id, bill_number, bill_title, status, internal_percent, overall_percent, 1 if internal_active else 0, votes_json, veto_by_id, veto_by_display, now),
        )
        row = con.execute(
            "SELECT id FROM tvrs_live_results WHERE session_key = ? AND bill_id = ?",
            (str(session_key), int(bill_id)),
        ).fetchone()
        con.commit()
        return int(row["id"]) if row else int(cur.lastrowid)


def tvrs_live_result_for_bill(session_key: str, bill_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM tvrs_live_results WHERE session_key = ? AND bill_id = ?",
            (str(session_key), int(bill_id)),
        ).fetchone()
    return dict(row) if row else None


# ---------------- TVRS admin helpers ----------------

def _tvrs_open_delivery_payloads(
    con: sqlite3.Connection,
    guild_id: int,
) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT payload_json FROM delivery_outbox
        WHERE topic LIKE 'tvrs.%'
          AND status IN ('pending', 'processing', 'retry')
        """
    ).fetchall()
    payloads: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and int(payload.get("guild_id") or 0) == int(guild_id):
            payloads.append(payload)
    return payloads


def _tvrs_assert_no_open_delivery(
    con: sqlite3.Connection,
    *,
    guild_id: int,
    bill_id: int | None = None,
    session_key: str | None = None,
    plenary_number: int | None = None,
) -> None:
    selected_bill_id = int(bill_id or 0)
    for payload in _tvrs_open_delivery_payloads(con, guild_id):
        payload_bill_ids = {
            int(payload.get("bill_id") or 0),
            int(dict(payload.get("bill") or {}).get("id") or 0),
            int(dict(payload.get("result") or {}).get("bill_id") or 0),
        }
        payload_bill_ids.update(
            int(dict(item).get("bill_id") or 0)
            for item in payload.get("results") or []
            if isinstance(item, dict)
        )
        if selected_bill_id > 0 and selected_bill_id in payload_bill_ids:
            raise ValueError("tvrs_delivery_pending")
        if session_key is not None and str(payload.get("session_key") or "") == str(session_key):
            raise ValueError("tvrs_delivery_pending")
        if (
            plenary_number is not None
            and int(payload.get("plenary_number") or 0) == int(plenary_number)
        ):
            raise ValueError("tvrs_delivery_pending")


def _tvrs_bill_referenced_by_active_consensus(
    con: sqlite3.Connection,
    bill_id: int,
    guild_id: int | None = None,
) -> bool:
    guild_filter = ""
    if guild_id is not None:
        guild_filter = "AND session.guild_id = ?"
    row = con.execute(
        f"""
        SELECT 1
        FROM tvrs_consensus_sessions AS session
        WHERE session.finished_at IS NULL
          {guild_filter}
          AND (
              session.current_bill_id = ?
              OR EXISTS(
                  SELECT 1
                  FROM tvrs_live_results AS result
                  WHERE result.session_key = session.session_key
                    AND result.bill_id = ?
              )
          )
        LIMIT 1
        """,
        # The optional guild predicate appears before the bill predicates.
        ([int(guild_id)] if guild_id is not None else []) + [int(bill_id), int(bill_id)],
    ).fetchone()
    return row is not None


def _tvrs_assert_bill_admin_mutable(con: sqlite3.Connection, bill: sqlite3.Row) -> None:
    bill_id = int(bill["id"])
    status = str(bill["status"] or "")
    active = _tvrs_bill_referenced_by_active_consensus(
        con,
        bill_id,
        int(bill["guild_id"]),
    )
    if status in {"voting", "pending_veto"} or active:
        raise ValueError("bill_locked_by_active_consensus")
    _tvrs_assert_no_open_delivery(
        con,
        guild_id=int(bill["guild_id"]),
        bill_id=bill_id,
    )


def _tvrs_assert_result_admin_mutable(con: sqlite3.Connection, result: sqlite3.Row) -> None:
    active = con.execute(
        """
        SELECT 1 FROM tvrs_consensus_sessions
        WHERE session_key = ? AND finished_at IS NULL
        LIMIT 1
        """,
        (str(result["session_key"]),),
    ).fetchone()
    if active is not None:
        raise ValueError("result_locked_by_active_consensus")
    _tvrs_assert_no_open_delivery(
        con,
        guild_id=int(result["guild_id"]),
        bill_id=int(result["bill_id"]),
        session_key=str(result["session_key"]),
    )

def tvrs_get_bill_by_number(guild_id: int, bill_number: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM tvrs_bills WHERE guild_id = ? AND bill_number = ?", (guild_id, bill_number)).fetchone()
    return dict(row) if row else None


def tvrs_delete_bill_by_number(
    guild_id: int,
    bill_number: int,
    actor_id: int | None = None,
    actor_display: str | None = None,
) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM tvrs_bills WHERE guild_id = ? AND bill_number = ?", (guild_id, bill_number)).fetchone()
        if row is None:
            con.commit()
            return None
        _tvrs_assert_bill_admin_mutable(con, row)
        bill = dict(row)
        bill_id = int(bill["id"])
        votes = con.execute(
            "SELECT * FROM tvrs_votes WHERE guild_id = ? AND bill_id = ?",
            (guild_id, bill_id),
        ).fetchall()
        results = con.execute(
            "SELECT * FROM tvrs_live_results WHERE guild_id = ? AND bill_id = ?",
            (guild_id, bill_id),
        ).fetchall()
        snapshots = [{"table": "tvrs_bills", "row_id": bill_id, "before": bill}]
        snapshots.extend(
            {"table": "tvrs_votes", "row_id": int(item["id"]), "before": dict(item)} for item in votes
        )
        snapshots.extend(
            {"table": "tvrs_live_results", "row_id": int(item["id"]), "before": dict(item)} for item in results
        )
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="tvrs",
            action_kind="generic_rows_restore",
            target_type="tvrs_bill",
            target_id=bill_id,
            summary=f"Удалён законопроект №{bill_number}",
            payload={"rows": snapshots},
        )
        con.execute("DELETE FROM tvrs_votes WHERE guild_id = ? AND bill_id = ?", (guild_id, bill_id))
        con.execute("DELETE FROM tvrs_live_results WHERE guild_id = ? AND bill_id = ?", (guild_id, bill_id))
        con.execute("DELETE FROM tvrs_bills WHERE id = ?", (bill_id,))
        con.commit()
    return bill


def tvrs_delete_live_result(
    result_id: int,
    guild_id: int | None = None,
    actor_id: int | None = None,
    actor_display: str | None = None,
) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if guild_id is None:
            row = con.execute("SELECT * FROM tvrs_live_results WHERE id = ?", (result_id,)).fetchone()
        else:
            row = con.execute("SELECT * FROM tvrs_live_results WHERE id = ? AND guild_id = ?", (result_id, guild_id)).fetchone()
        if row is None:
            con.commit()
            return None
        _tvrs_assert_result_admin_mutable(con, row)
        result = dict(row)
        _record_bot_action(
            con,
            guild_id=int(row["guild_id"]),
            actor_id=actor_id,
            actor_display=actor_display,
            module="tvrs",
            action_kind="generic_row_restore",
            target_type="tvrs_live_result",
            target_id=result_id,
            summary=f"Удалён итог голосования #{result_id}",
            payload={"table": "tvrs_live_results", "row_id": result_id, "before": result},
        )
        con.execute("DELETE FROM tvrs_live_results WHERE id = ?", (result_id,))
        con.commit()
    return result


def tvrs_delete_plenary_results(
    guild_id: int,
    plenary_number: int,
    actor_id: int | None = None,
    actor_display: str | None = None,
) -> int:
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute(
            "SELECT * FROM tvrs_live_results WHERE guild_id = ? AND plenary_number = ? ORDER BY id",
            (guild_id, plenary_number),
        ).fetchall()
        if any(
            con.execute(
                """
                SELECT 1 FROM tvrs_consensus_sessions
                WHERE session_key = ? AND finished_at IS NULL LIMIT 1
                """,
                (str(row["session_key"]),),
            ).fetchone()
            is not None
            for row in rows
        ):
            con.rollback()
            raise ValueError("plenary_locked_by_active_consensus")
        _tvrs_assert_no_open_delivery(
            con,
            guild_id=guild_id,
            plenary_number=plenary_number,
        )
        if rows:
            _record_bot_action(
                con,
                guild_id=guild_id,
                actor_id=actor_id,
                actor_display=actor_display,
                module="tvrs",
                action_kind="generic_rows_restore",
                target_type="tvrs_plenary",
                target_id=plenary_number,
                summary=f"Удалены итоги пленарного консенсуса №{plenary_number}",
                payload={
                    "rows": [
                        {"table": "tvrs_live_results", "row_id": int(row["id"]), "before": dict(row)}
                        for row in rows
                    ]
                },
            )
        cur = con.execute("DELETE FROM tvrs_live_results WHERE guild_id = ? AND plenary_number = ?", (guild_id, plenary_number))
        count = int(cur.rowcount or 0)
        con.commit()
    return count


def tvrs_update_bill_field(
    guild_id: int,
    bill_number: int,
    field: str,
    value: str,
    actor_id: int | None = None,
    actor_display: str | None = None,
) -> dict[str, Any] | None:
    allowed = {"title", "summary", "materials", "status", "result_summary"}
    if field not in allowed:
        raise ValueError(f"field_not_allowed:{field}")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM tvrs_bills WHERE guild_id = ? AND bill_number = ?", (guild_id, bill_number)).fetchone()
        if row is None:
            con.commit()
            return None
        _tvrs_assert_bill_admin_mutable(con, row)
        if field == "status" and str(value).strip() not in {
            "draft",
            "queued",
            "requeued",
            "accepted",
            "rejected",
            "vetoed",
        }:
            con.rollback()
            raise ValueError("bill_status_not_allowed")
        con.execute(f"UPDATE tvrs_bills SET {field} = ?, updated_at = ? WHERE guild_id = ? AND bill_number = ?", (value, now, guild_id, bill_number))
        updated = con.execute("SELECT * FROM tvrs_bills WHERE guild_id = ? AND bill_number = ?", (guild_id, bill_number)).fetchone()
        _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="tvrs",
            action_kind="generic_row_restore",
            target_type="tvrs_bill",
            target_id=int(row["id"]),
            summary=f"Изменено поле {field} законопроекта №{bill_number}",
            payload={"table": "tvrs_bills", "row_id": int(row["id"]), "before": dict(row)},
            now=now,
        )
        con.commit()
    return dict(updated) if updated else None

def tvrs_recent_live_results(guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute("SELECT * FROM tvrs_live_results WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (guild_id, limit)).fetchall()
    return [dict(row) for row in rows]


# ---------------- Treasury / finance ledger ----------------

FINANCE_SNAPSHOT_KINDS = {"daily", "interim"}
FINANCE_MOVEMENT_KINDS = {"deposit", "withdraw"}


def _finance_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def finance_get_or_create_daily_prompt(*, guild_id: int, report_date: str, channel_id: int) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            INSERT INTO finance_daily_prompts(
                guild_id, report_date, channel_id, message_id, status,
                event_id, created_at, updated_at
            )
            VALUES(?, ?, ?, NULL, 'open', NULL, ?, ?)
            ON CONFLICT(guild_id, report_date) DO UPDATE SET
                channel_id = excluded.channel_id,
                updated_at = excluded.updated_at
            """,
            (guild_id, report_date, channel_id, now, now),
        )
        row = con.execute(
            "SELECT * FROM finance_daily_prompts WHERE guild_id = ? AND report_date = ?",
            (guild_id, report_date),
        ).fetchone()
        con.commit()
    prompt = _finance_row(row)
    if prompt is None:
        raise RuntimeError("finance_prompt_create_failed")
    return prompt


def finance_get_daily_prompt(prompt_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM finance_daily_prompts WHERE id = ?", (prompt_id,)).fetchone()
    return _finance_row(row)


def finance_get_daily_prompt_by_message(*, guild_id: int, message_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM finance_daily_prompts WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        ).fetchone()
    return _finance_row(row)


def finance_bind_daily_prompt_message(*, prompt_id: int, channel_id: int, message_id: int) -> dict[str, Any] | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE finance_daily_prompts
            SET channel_id = ?, message_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (channel_id, message_id, now, prompt_id),
        )
        row = con.execute("SELECT * FROM finance_daily_prompts WHERE id = ?", (prompt_id,)).fetchone()
        con.commit()
    return _finance_row(row)


def finance_recent_daily_prompts(*, guild_id: int, limit: int = 14) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            "SELECT * FROM finance_daily_prompts WHERE guild_id = ? ORDER BY report_date DESC LIMIT ?",
            (guild_id, max(1, min(int(limit), 100))),
        ).fetchall()
    return [dict(row) for row in rows]


def _finance_enqueue_notifications(
    con: sqlite3.Connection,
    *,
    event_id: int,
    log_channel_id: int,
    admin_user_id: int,
    now: str,
) -> None:
    destinations = []
    if int(log_channel_id) > 0:
        destinations.append(("log_channel", int(log_channel_id)))
    if int(admin_user_id) > 0:
        destinations.append(("admin_dm", int(admin_user_id)))
    for destination_kind, destination_id in destinations:
        con.execute(
            """
            INSERT INTO finance_notifications(
                event_id, destination_kind, destination_id, status, attempts,
                next_attempt_at, message_id, last_error, created_at, updated_at
            )
            VALUES(?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, ?)
            ON CONFLICT(event_id, destination_kind, destination_id) DO NOTHING
            """,
            (event_id, destination_kind, destination_id, now, now, now),
        )


def finance_record_snapshot(
    *,
    guild_id: int,
    event_kind: str,
    amount: int,
    actor_id: int,
    actor_display: str | None,
    channel_id: int | None,
    message_id: int | None,
    log_channel_id: int,
    admin_user_id: int,
    report_date: str | None = None,
    prompt_id: int | None = None,
) -> dict[str, Any]:
    if event_kind not in FINANCE_SNAPSHOT_KINDS:
        raise ValueError("finance_bad_snapshot_kind")
    if int(amount) < 0:
        raise ValueError("finance_bad_amount")
    if event_kind == "daily" and prompt_id is None:
        raise ValueError("finance_daily_prompt_required")

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        prompt = None
        if prompt_id is not None:
            prompt = con.execute(
                "SELECT * FROM finance_daily_prompts WHERE id = ? AND guild_id = ?",
                (prompt_id, guild_id),
            ).fetchone()
            if prompt is None:
                con.rollback()
                raise ValueError("finance_prompt_not_found")
            if str(prompt["status"]) != "open":
                existing = con.execute("SELECT * FROM finance_events WHERE prompt_id = ?", (prompt_id,)).fetchone()
                con.commit()
                if existing is None:
                    raise RuntimeError("finance_filled_prompt_without_event")
                result = dict(existing)
                result["already_recorded"] = True
                return result
            report_date = str(prompt["report_date"])
            channel_id = int(prompt["channel_id"])
            message_id = prompt["message_id"]

        cur = con.execute(
            """
            INSERT INTO finance_events(
                guild_id, event_kind, report_date, prompt_id, amount, delta,
                balance_before, balance_after, reason, captcha_digest, game_code,
                actor_id, actor_display, channel_id, message_id, created_at
            )
            VALUES(?, ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                event_kind,
                report_date,
                prompt_id,
                int(amount),
                int(amount),
                actor_id,
                actor_display,
                channel_id,
                message_id,
                now,
            ),
        )
        event_id = int(cur.lastrowid)
        if prompt_id is not None:
            con.execute(
                """
                UPDATE finance_daily_prompts
                SET status = 'filled', event_id = ?, updated_at = ?
                WHERE id = ? AND status = 'open'
                """,
                (event_id, now, prompt_id),
            )
        _finance_enqueue_notifications(
            con,
            event_id=event_id,
            log_channel_id=log_channel_id,
            admin_user_id=admin_user_id,
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="finance",
            action_kind=f"finance_{event_kind}",
            target_type="finance_event",
            target_id=event_id,
            summary=(
                f"{'Ежедневный отчёт' if event_kind == 'daily' else 'Межотчёт'} казны: {int(amount)}"
            ),
            payload={"finance_event_id": event_id},
            now=now,
        )
        row = con.execute("SELECT * FROM finance_events WHERE id = ?", (event_id,)).fetchone()
        con.commit()
    event = _finance_row(row)
    if event is None:
        raise RuntimeError("finance_snapshot_create_failed")
    event["already_recorded"] = False
    event["action_id"] = action_id
    return event


def finance_record_movement(
    *,
    guild_id: int,
    event_kind: str,
    amount: int,
    reason: str,
    captcha_digest: str,
    game_code: str,
    actor_id: int,
    actor_display: str | None,
    channel_id: int | None,
    message_id: int | None,
    log_channel_id: int,
    admin_user_id: int,
) -> dict[str, Any]:
    if event_kind not in FINANCE_MOVEMENT_KINDS:
        raise ValueError("finance_bad_movement_kind")
    if int(amount) <= 0:
        raise ValueError("finance_bad_amount")
    reason = str(reason or "").strip()
    if len(reason) < 10:
        raise ValueError("finance_reason_too_short")
    game_code = str(game_code or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{4}", game_code):
        raise ValueError("finance_bad_game_code")

    now = utc_now_iso()
    delta = int(amount) if event_kind == "deposit" else -int(amount)
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        latest = con.execute(
            "SELECT balance_after FROM finance_events WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id,),
        ).fetchone()
        balance_before = None if latest is None or latest["balance_after"] is None else int(latest["balance_after"])
        balance_after = None if balance_before is None else balance_before + delta
        cur = con.execute(
            """
            INSERT INTO finance_events(
                guild_id, event_kind, report_date, prompt_id, amount, delta,
                balance_before, balance_after, reason, captcha_digest, game_code,
                actor_id, actor_display, channel_id, message_id, created_at
            )
            VALUES(?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                event_kind,
                int(amount),
                delta,
                balance_before,
                balance_after,
                reason,
                captcha_digest,
                game_code,
                actor_id,
                actor_display,
                channel_id,
                message_id,
                now,
            ),
        )
        event_id = int(cur.lastrowid)
        _finance_enqueue_notifications(
            con,
            event_id=event_id,
            log_channel_id=log_channel_id,
            admin_user_id=admin_user_id,
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="finance",
            action_kind=f"finance_{event_kind}",
            target_type="finance_event",
            target_id=event_id,
            summary=f"{'Пополнение' if event_kind == 'deposit' else 'Снятие'} казны: {int(amount)} · код {game_code}",
            payload={"finance_event_id": event_id},
            now=now,
        )
        row = con.execute("SELECT * FROM finance_events WHERE id = ?", (event_id,)).fetchone()
        con.commit()
    event = _finance_row(row)
    if event is None:
        raise RuntimeError("finance_movement_create_failed")
    event["action_id"] = action_id
    return event


def finance_get_event(event_id: int) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute("SELECT * FROM finance_events WHERE id = ?", (event_id,)).fetchone()
        result = _finance_row(row)
        if result is not None and result.get("reversed_event_id") is not None:
            reversed_row = con.execute(
                "SELECT * FROM finance_events WHERE id = ?",
                (int(result["reversed_event_id"]),),
            ).fetchone()
            result["reversed_event"] = _finance_row(reversed_row)
    return result


def _finance_replay_balance(rows: Iterable[sqlite3.Row], *, omit_event_id: int | None = None) -> int | None:
    balance: int | None = None
    for row in rows:
        event_id = int(row["id"])
        if omit_event_id is not None and event_id == omit_event_id:
            continue
        kind = str(row["event_kind"] or "")
        if kind in FINANCE_SNAPSHOT_KINDS:
            balance = int(row["amount"])
        elif kind in FINANCE_MOVEMENT_KINDS and balance is not None:
            balance += int(row["delta"] or 0)
    return balance


def finance_undo_last_action(
    *,
    guild_id: int,
    target_actor_id: int,
    undone_by_id: int,
    undone_by_display: str | None,
    channel_id: int | None,
    message_id: int | None,
    log_channel_id: int,
    admin_user_id: int,
    target_event_id: int | None = None,
) -> dict[str, Any] | None:
    """Audit and reverse the target user's latest active finance action.

    Original rows are never deleted. An `undo` row points at the reversed event and
    stores the counterfactual current balance after replaying the active ledger.
    """
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if target_event_id is None:
            target = con.execute(
                """
                SELECT e.* FROM finance_events AS e
                WHERE e.guild_id = ?
                  AND e.actor_id = ?
                  AND e.event_kind IN ('daily', 'interim', 'deposit', 'withdraw')
                  AND NOT EXISTS (
                      SELECT 1 FROM finance_events AS u
                      WHERE u.reversed_event_id = e.id
                  )
                ORDER BY e.id DESC
                LIMIT 1
                """,
                (guild_id, target_actor_id),
            ).fetchone()
        else:
            target = con.execute(
                """
                SELECT e.* FROM finance_events AS e
                WHERE e.guild_id = ? AND e.id = ?
                  AND e.event_kind IN ('daily', 'interim', 'deposit', 'withdraw')
                  AND NOT EXISTS (
                      SELECT 1 FROM finance_events AS u
                      WHERE u.reversed_event_id = e.id
                  )
                """,
                (guild_id, int(target_event_id)),
            ).fetchone()
        if target is None:
            con.commit()
            return None
        craft_purchase = con.execute(
            "SELECT id FROM craft_purchases WHERE finance_event_id = ? AND undone_at IS NULL LIMIT 1",
            (int(target["id"]),),
        ).fetchone()
        if str(target["captcha_digest"] or "").startswith("craft:") or craft_purchase is not None:
            con.rollback()
            raise ValueError("finance_action_locked_by_craft")

        active_rows = con.execute(
            """
            SELECT e.* FROM finance_events AS e
            WHERE e.guild_id = ?
              AND e.event_kind IN ('daily', 'interim', 'deposit', 'withdraw')
              AND NOT EXISTS (
                  SELECT 1 FROM finance_events AS u
                  WHERE u.reversed_event_id = e.id
              )
            ORDER BY e.id ASC
            """,
            (guild_id,),
        ).fetchall()
        target_id = int(target["id"])
        balance_before = _finance_replay_balance(active_rows)
        balance_after = _finance_replay_balance(active_rows, omit_event_id=target_id)
        if balance_before is not None and balance_after is not None:
            undo_delta = balance_after - balance_before
        else:
            undo_delta = None

        kind_labels = {
            "daily": "ежедневного отчёта",
            "interim": "межотчёта",
            "deposit": "пополнения",
            "withdraw": "снятия",
        }
        reason = f"Отмена {kind_labels.get(str(target['event_kind']), 'финансового действия')} #{target_id}"
        cur = con.execute(
            """
            INSERT INTO finance_events(
                guild_id, event_kind, report_date, prompt_id, reversed_event_id,
                amount, delta, balance_before, balance_after, reason,
                captcha_digest, game_code, actor_id, actor_display,
                channel_id, message_id, created_at
            )
            VALUES(?, 'undo', NULL, NULL, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                target_id,
                int(target["amount"]),
                undo_delta,
                balance_before,
                balance_after,
                reason,
                undone_by_id,
                undone_by_display,
                channel_id,
                message_id,
                now,
            ),
        )
        undo_id = int(cur.lastrowid)

        prompt = None
        if str(target["event_kind"]) == "daily" and target["prompt_id"] is not None:
            # Free the prompt's unique slot so the reopened daily report can be
            # filled again. The audit relationship is retained by reversed_event_id.
            con.execute("UPDATE finance_events SET prompt_id = NULL WHERE id = ?", (target_id,))
            con.execute(
                """
                UPDATE finance_daily_prompts
                SET status = 'open', event_id = NULL, updated_at = ?
                WHERE id = ? AND event_id = ?
                """,
                (now, int(target["prompt_id"]), target_id),
            )
            prompt = con.execute(
                "SELECT * FROM finance_daily_prompts WHERE id = ?",
                (int(target["prompt_id"]),),
            ).fetchone()

        _finance_enqueue_notifications(
            con,
            event_id=undo_id,
            log_channel_id=log_channel_id,
            admin_user_id=admin_user_id,
            now=now,
        )
        con.execute(
            """
            UPDATE bot_actions
            SET status = 'undone', undone_by_id = ?, undone_by_display = ?,
                undone_at = ?, undo_reason = ?
            WHERE guild_id = ? AND target_type = 'finance_event' AND target_id = ?
              AND status = 'active'
            """,
            (
                undone_by_id,
                undone_by_display,
                now,
                reason,
                guild_id,
                str(target_id),
            ),
        )
        undo_row = con.execute("SELECT * FROM finance_events WHERE id = ?", (undo_id,)).fetchone()
        con.commit()

    return {
        "undo_event": _finance_row(undo_row),
        "reversed_event": _finance_row(target),
        "prompt": _finance_row(prompt),
    }


def finance_get_latest_state(guild_id: int) -> dict[str, Any]:
    with _db_lock, connect() as con:
        latest = con.execute(
            "SELECT * FROM finance_events WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id,),
        ).fetchone()
        report = con.execute(
            """
            SELECT e.* FROM finance_events AS e
            WHERE e.guild_id = ?
              AND e.event_kind IN ('daily', 'interim')
              AND NOT EXISTS (
                  SELECT 1 FROM finance_events AS u
                  WHERE u.reversed_event_id = e.id
              )
            ORDER BY e.id DESC LIMIT 1
            """,
            (guild_id,),
        ).fetchone()
        movement_count = 0
        if report is not None:
            row = con.execute(
                """
                SELECT COUNT(*) AS n FROM finance_events AS e
                WHERE e.guild_id = ? AND e.id > ?
                  AND e.event_kind IN ('deposit', 'withdraw')
                  AND NOT EXISTS (
                      SELECT 1 FROM finance_events AS u
                      WHERE u.reversed_event_id = e.id
                  )
                """,
                (guild_id, int(report["id"])),
            ).fetchone()
            movement_count = int(row["n"] or 0)
    return {
        "latest_event": _finance_row(latest),
        "latest_report": _finance_row(report),
        "estimated_balance": (None if latest is None else latest["balance_after"]),
        "movements_after_report": movement_count,
    }


def finance_search_events(
    guild_id: int,
    *,
    code: str | None = None,
    event_kind: str | None = None,
    actor_id: int | None = None,
    days: int | None = None,
    text: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    clauses = ["e.guild_id = ?"]
    params: list[Any] = [guild_id]
    if code:
        clean_code = re.sub(r"[^A-Za-z]", "", str(code)).upper()
        if len(clean_code) != 4:
            raise ValueError("finance_bad_game_code")
        clauses.append("e.game_code = ?")
        params.append(clean_code)
    if event_kind:
        allowed = FINANCE_SNAPSHOT_KINDS | FINANCE_MOVEMENT_KINDS | {"undo"}
        if event_kind not in allowed:
            raise ValueError("finance_bad_audit_kind")
        clauses.append("e.event_kind = ?")
        params.append(event_kind)
    if actor_id is not None:
        clauses.append("e.actor_id = ?")
        params.append(actor_id)
    if days is not None and int(days) > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=min(int(days), 3650))).isoformat()
        clauses.append("e.created_at >= ?")
        params.append(cutoff)
    if text and str(text).strip():
        clauses.append("lower(COALESCE(e.reason, '')) LIKE ?")
        params.append(f"%{str(text).strip().lower()}%")
    params.append(max(1, min(int(limit), 100)))
    with _db_lock, connect() as con:
        rows = con.execute(
            f"""
            SELECT e.*,
                   EXISTS(SELECT 1 FROM finance_events u WHERE u.reversed_event_id = e.id) AS is_undone,
                   (SELECT u.id FROM finance_events u WHERE u.reversed_event_id = e.id LIMIT 1) AS undo_event_id,
                   (SELECT p.id FROM craft_purchases p WHERE p.finance_event_id = e.id AND p.undone_at IS NULL LIMIT 1) AS craft_purchase_id,
                   (SELECT p.plan_id FROM craft_purchases p WHERE p.finance_event_id = e.id AND p.undone_at IS NULL LIMIT 1) AS craft_purchase_plan_id,
                   (SELECT b.id FROM craft_batches b WHERE b.finance_event_id = e.id AND b.undone_at IS NULL LIMIT 1) AS craft_batch_id,
                   (SELECT b.plan_id FROM craft_batches b WHERE b.finance_event_id = e.id AND b.undone_at IS NULL LIMIT 1) AS craft_batch_plan_id,
                   (SELECT a.id FROM bot_actions a WHERE a.target_type = 'finance_event' AND a.target_id = CAST(e.id AS TEXT) ORDER BY a.id DESC LIMIT 1) AS action_id
            FROM finance_events e
            WHERE {' AND '.join(clauses)}
            ORDER BY e.id DESC LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def finance_stats(guild_id: int, days: int = 30) -> dict[str, Any]:
    period_days = max(1, min(int(days), 3650))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)).isoformat()
    with _db_lock, connect() as con:
        active_rows = con.execute(
            """
            SELECT e.* FROM finance_events e
            WHERE e.guild_id = ? AND e.event_kind IN ('daily', 'interim', 'deposit', 'withdraw')
              AND NOT EXISTS (SELECT 1 FROM finance_events u WHERE u.reversed_event_id = e.id)
            ORDER BY e.id ASC
            """,
            (guild_id,),
        ).fetchall()
        before_rows = [row for row in active_rows if str(row["created_at"]) < cutoff]
        period_rows = [row for row in active_rows if str(row["created_at"]) >= cutoff]
        movements = [row for row in period_rows if str(row["event_kind"]) in FINANCE_MOVEMENT_KINDS]
        deposits = sum(int(row["amount"]) for row in movements if str(row["event_kind"]) == "deposit")
        withdrawals = sum(int(row["amount"]) for row in movements if str(row["event_kind"]) == "withdraw")
        automatic_craft = sum(
            int(row["amount"])
            for row in movements
            if str(row["event_kind"]) == "withdraw" and str(row["captcha_digest"] or "").startswith("craft:")
        )
        reports = sum(1 for row in period_rows if str(row["event_kind"]) in FINANCE_SNAPSHOT_KINDS)
        undo_count = int(
            con.execute(
                "SELECT COUNT(*) AS n FROM finance_events WHERE guild_id = ? AND event_kind = 'undo' AND created_at >= ?",
                (guild_id, cutoff),
            ).fetchone()["n"]
            or 0
        )
        coded = sum(1 for row in movements if row["game_code"])
        top_actors_rows = con.execute(
            """
            SELECT e.actor_id, MAX(e.actor_display) AS actor_display, COUNT(*) AS operations,
                   SUM(CASE WHEN e.event_kind = 'deposit' THEN e.amount ELSE 0 END) AS deposits,
                   SUM(CASE WHEN e.event_kind = 'withdraw' THEN e.amount ELSE 0 END) AS withdrawals
            FROM finance_events e
            WHERE e.guild_id = ? AND e.created_at >= ?
              AND e.event_kind IN ('deposit', 'withdraw')
              AND NOT EXISTS (SELECT 1 FROM finance_events u WHERE u.reversed_event_id = e.id)
            GROUP BY e.actor_id ORDER BY operations DESC, e.actor_id LIMIT 5
            """,
            (guild_id, cutoff),
        ).fetchall()
        top_reasons_rows = con.execute(
            """
            SELECT reason, COUNT(*) AS operations, SUM(amount) AS total
            FROM finance_events e
            WHERE e.guild_id = ? AND e.created_at >= ?
              AND e.event_kind IN ('deposit', 'withdraw')
              AND COALESCE(TRIM(reason), '') != ''
              AND NOT EXISTS (SELECT 1 FROM finance_events u WHERE u.reversed_event_id = e.id)
            GROUP BY lower(reason) ORDER BY total DESC LIMIT 5
            """,
            (guild_id, cutoff),
        ).fetchall()
    return {
        "days": period_days,
        "cutoff": cutoff,
        "starting_balance": _finance_replay_balance(before_rows),
        "ending_balance": _finance_replay_balance(active_rows),
        "deposits": deposits,
        "withdrawals": withdrawals,
        "net_flow": deposits - withdrawals,
        "automatic_craft_expenses": automatic_craft,
        "movement_count": len(movements),
        "report_count": reports,
        "undo_count": undo_count,
        "coded_movement_count": coded,
        "top_actors": [dict(row) for row in top_actors_rows],
        "top_reasons": [dict(row) for row in top_reasons_rows],
    }


def finance_pending_notifications(limit: int = 25) -> list[dict[str, Any]]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM finance_notifications
            WHERE status = 'pending' AND attempts < 20 AND next_attempt_at <= ?
            ORDER BY id ASC LIMIT ?
            """,
            (now, max(1, min(int(limit), 100))),
        ).fetchall()
    return [dict(row) for row in rows]


def finance_mark_notification_sent(notification_id: int, message_id: int | None) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE finance_notifications
            SET status = 'sent', message_id = ?, last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (message_id, now, notification_id),
        )
        con.commit()


def finance_mark_notification_failed(notification_id: int, error: str) -> None:
    now_dt = datetime.now(timezone.utc)
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT attempts FROM finance_notifications WHERE id = ?",
            (notification_id,),
        ).fetchone()
        attempts = int(row["attempts"] or 0) + 1 if row is not None else 1
        delay_seconds = min(3600, 2 ** min(attempts, 12))
        next_attempt = (now_dt + timedelta(seconds=delay_seconds)).isoformat()
        con.execute(
            """
            UPDATE finance_notifications
            SET attempts = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (attempts, next_attempt, str(error)[:1000], now_dt.isoformat(), notification_id),
        )
        con.commit()


# ---------------- Craft production system ----------------

CRAFT_ACTIVE_STAGES = {"procurement", "crafting", "awaiting_output", "listing", "selling"}


def _craft_add_event(
    con: sqlite3.Connection,
    *,
    plan_id: int,
    guild_id: int,
    event_kind: str,
    actor_id: int | None,
    actor_display: str | None,
    details: dict[str, Any],
    now: str,
) -> int:
    cur = con.execute(
        """
        INSERT INTO craft_events(
            plan_id, guild_id, event_kind, actor_id, actor_display,
            details_json, thread_message_id, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (plan_id, guild_id, event_kind, actor_id, actor_display, json.dumps(details, ensure_ascii=False), now),
    )
    return int(cur.lastrowid)


def _craft_recipe_from_con(con: sqlite3.Connection, recipe_id: int) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM craft_recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    materials = con.execute(
        "SELECT * FROM craft_recipe_materials WHERE recipe_id = ? ORDER BY id ASC",
        (recipe_id,),
    ).fetchall()
    result["materials"] = [dict(item) for item in materials]
    return result


def craft_create_recipe(
    *,
    guild_id: int,
    product_name: str,
    treasury_cost_per_unit: int,
    duration_minutes_per_unit: int,
    max_batch_size: int,
    materials: list[tuple[str, int]],
    created_by_id: int,
    created_by_display: str | None,
) -> dict[str, Any]:
    product_name = str(product_name or "").strip()
    if not product_name or len(product_name) > 100:
        raise ValueError("craft_bad_product_name")
    if treasury_cost_per_unit < 0:
        raise ValueError("craft_bad_treasury_cost")
    if duration_minutes_per_unit <= 0 or duration_minutes_per_unit > 10080:
        raise ValueError("craft_bad_duration")
    if max_batch_size <= 0 or max_batch_size > 1000:
        raise ValueError("craft_bad_batch_size")
    if not materials or len(materials) > 20:
        raise ValueError("craft_bad_material_count")

    normalized: list[tuple[str, int]] = []
    seen: set[str] = set()
    for name, quantity in materials:
        clean_name = str(name or "").strip()
        key = clean_name.casefold()
        if not clean_name or len(clean_name) > 80 or key in seen or int(quantity) <= 0:
            raise ValueError("craft_bad_material")
        seen.add(key)
        normalized.append((clean_name, int(quantity)))

    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            cur = con.execute(
                """
                INSERT INTO craft_recipes(
                    guild_id, product_name, treasury_cost_per_unit,
                    duration_minutes_per_unit, max_batch_size,
                    created_by_id, created_by_display, active, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    guild_id,
                    product_name,
                    int(treasury_cost_per_unit),
                    int(duration_minutes_per_unit),
                    int(max_batch_size),
                    created_by_id,
                    created_by_display,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            con.rollback()
            raise ValueError("craft_recipe_exists") from exc
        recipe_id = int(cur.lastrowid)
        con.executemany(
            """
            INSERT INTO craft_recipe_materials(
                recipe_id, material_name, quantity_per_unit, created_at
            )
            VALUES(?, ?, ?, ?)
            """,
            [(recipe_id, name, quantity, now) for name, quantity in normalized],
        )
        con.execute(
            """
            INSERT INTO craft_recipe_versions(
                recipe_id, version, product_name, treasury_cost_per_unit,
                duration_minutes_per_unit, max_batch_size, materials_json,
                active, changed_by_id, changed_by_display, change_kind, created_at
            )
            VALUES(?, 1, ?, ?, ?, ?, ?, 1, ?, ?, 'created', ?)
            """,
            (
                recipe_id,
                product_name,
                int(treasury_cost_per_unit),
                int(duration_minutes_per_unit),
                int(max_batch_size),
                json.dumps(normalized, ensure_ascii=False),
                created_by_id,
                created_by_display,
                now,
            ),
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=created_by_id,
            actor_display=created_by_display,
            module="craft",
            action_kind="recipe_created",
            target_type="craft_recipe",
            target_id=recipe_id,
            summary=f"Создан рецепт «{product_name}»",
            payload={"recipe_id": recipe_id, "version": 1},
            now=now,
        )
        recipe = _craft_recipe_from_con(con, recipe_id)
        con.commit()
    if recipe is None:
        raise RuntimeError("craft_recipe_create_failed")
    recipe["action_id"] = action_id
    return recipe


def craft_list_recipes(guild_id: int, *, active_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        if active_only:
            rows = con.execute(
                "SELECT id FROM craft_recipes WHERE guild_id = ? AND active = 1 ORDER BY product_name LIMIT ?",
                (guild_id, max(1, min(int(limit), 200))),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT id FROM craft_recipes WHERE guild_id = ? ORDER BY active DESC, product_name LIMIT ?",
                (guild_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [recipe for row in rows if (recipe := _craft_recipe_from_con(con, int(row["id"]))) is not None]


def craft_get_recipe(recipe_id: int, guild_id: int | None = None) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        recipe = _craft_recipe_from_con(con, recipe_id)
    if recipe is not None and guild_id is not None and int(recipe["guild_id"]) != guild_id:
        return None
    return recipe


def craft_recipe_versions(recipe_id: int, guild_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        recipe = con.execute("SELECT guild_id FROM craft_recipes WHERE id = ?", (recipe_id,)).fetchone()
        if recipe is None or (guild_id is not None and int(recipe["guild_id"]) != guild_id):
            return []
        rows = con.execute(
            "SELECT * FROM craft_recipe_versions WHERE recipe_id = ? ORDER BY version DESC LIMIT ?",
            (recipe_id, max(1, min(int(limit), 100))),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["materials"] = json.loads(str(item.get("materials_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            item["materials"] = []
        result.append(item)
    return result


def _normalize_craft_recipe_values(
    *,
    product_name: str,
    treasury_cost_per_unit: int,
    duration_minutes_per_unit: int,
    max_batch_size: int,
    materials: list[tuple[str, int]],
) -> tuple[str, list[tuple[str, int]]]:
    clean_product = str(product_name or "").strip()
    if not clean_product or len(clean_product) > 100:
        raise ValueError("craft_bad_product_name")
    if int(treasury_cost_per_unit) < 0:
        raise ValueError("craft_bad_treasury_cost")
    if int(duration_minutes_per_unit) <= 0 or int(duration_minutes_per_unit) > 10080:
        raise ValueError("craft_bad_duration")
    if int(max_batch_size) <= 0 or int(max_batch_size) > 1000:
        raise ValueError("craft_bad_batch_size")
    if not materials or len(materials) > 20:
        raise ValueError("craft_bad_material_count")
    normalized: list[tuple[str, int]] = []
    seen: set[str] = set()
    for name, quantity in materials:
        clean_name = str(name or "").strip()
        key = clean_name.casefold()
        if not clean_name or len(clean_name) > 80 or key in seen or int(quantity) <= 0:
            raise ValueError("craft_bad_material")
        seen.add(key)
        normalized.append((clean_name, int(quantity)))
    return clean_product, normalized


def craft_update_recipe(
    *,
    guild_id: int,
    recipe_id: int,
    product_name: str,
    treasury_cost_per_unit: int,
    duration_minutes_per_unit: int,
    max_batch_size: int,
    materials: list[tuple[str, int]],
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    clean_product, normalized = _normalize_craft_recipe_values(
        product_name=product_name,
        treasury_cost_per_unit=treasury_cost_per_unit,
        duration_minutes_per_unit=duration_minutes_per_unit,
        max_batch_size=max_batch_size,
        materials=materials,
    )
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        before = _craft_recipe_from_con(con, recipe_id)
        if before is None or int(before["guild_id"]) != guild_id:
            con.rollback()
            raise ValueError("craft_recipe_not_found")
        previous = {
            "product_name": before["product_name"],
            "treasury_cost_per_unit": int(before["treasury_cost_per_unit"]),
            "duration_minutes_per_unit": int(before["duration_minutes_per_unit"]),
            "max_batch_size": int(before["max_batch_size"]),
            "materials": [[item["material_name"], int(item["quantity_per_unit"])] for item in before["materials"]],
            "active": int(before["active"]),
            "version": int(before.get("version") or 1),
        }
        new_version = int(before.get("version") or 1) + 1
        try:
            con.execute(
                """
                UPDATE craft_recipes
                SET product_name = ?, treasury_cost_per_unit = ?,
                    duration_minutes_per_unit = ?, max_batch_size = ?,
                    version = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    clean_product,
                    int(treasury_cost_per_unit),
                    int(duration_minutes_per_unit),
                    int(max_batch_size),
                    new_version,
                    now,
                    recipe_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            con.rollback()
            raise ValueError("craft_recipe_exists") from exc
        con.execute("DELETE FROM craft_recipe_materials WHERE recipe_id = ?", (recipe_id,))
        con.executemany(
            """
            INSERT INTO craft_recipe_materials(recipe_id, material_name, quantity_per_unit, created_at)
            VALUES(?, ?, ?, ?)
            """,
            [(recipe_id, name, quantity, now) for name, quantity in normalized],
        )
        con.execute(
            """
            INSERT INTO craft_recipe_versions(
                recipe_id, version, product_name, treasury_cost_per_unit,
                duration_minutes_per_unit, max_batch_size, materials_json,
                active, changed_by_id, changed_by_display, change_kind, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'updated', ?)
            """,
            (
                recipe_id,
                new_version,
                clean_product,
                int(treasury_cost_per_unit),
                int(duration_minutes_per_unit),
                int(max_batch_size),
                json.dumps(normalized, ensure_ascii=False),
                int(before["active"]),
                actor_id,
                actor_display,
                now,
            ),
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="recipe_updated",
            target_type="craft_recipe",
            target_id=recipe_id,
            summary=f"Обновлён рецепт #{recipe_id}: «{clean_product}», версия {new_version}",
            payload={"recipe_id": recipe_id, "previous": previous, "version": new_version},
            now=now,
        )
        result = _craft_recipe_from_con(con, recipe_id)
        con.commit()
    result["action_id"] = action_id
    return result


def craft_set_recipe_active(
    *,
    guild_id: int,
    recipe_id: int,
    active: bool,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        before = _craft_recipe_from_con(con, recipe_id)
        if before is None or int(before["guild_id"]) != guild_id:
            con.rollback()
            raise ValueError("craft_recipe_not_found")
        if bool(before["active"]) == bool(active):
            result = before
            con.commit()
            result["action_id"] = None
            return result
        new_version = int(before.get("version") or 1) + 1
        con.execute(
            "UPDATE craft_recipes SET active = ?, version = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, new_version, now, recipe_id),
        )
        materials = [[item["material_name"], int(item["quantity_per_unit"])] for item in before["materials"]]
        con.execute(
            """
            INSERT INTO craft_recipe_versions(
                recipe_id, version, product_name, treasury_cost_per_unit,
                duration_minutes_per_unit, max_batch_size, materials_json,
                active, changed_by_id, changed_by_display, change_kind, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recipe_id,
                new_version,
                before["product_name"],
                int(before["treasury_cost_per_unit"]),
                int(before["duration_minutes_per_unit"]),
                int(before["max_batch_size"]),
                json.dumps(materials, ensure_ascii=False),
                1 if active else 0,
                actor_id,
                actor_display,
                "enabled" if active else "disabled",
                now,
            ),
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="recipe_enabled" if active else "recipe_disabled",
            target_type="craft_recipe",
            target_id=recipe_id,
            summary=f"{'Включён' if active else 'Отключён'} рецепт #{recipe_id}: «{before['product_name']}»",
            payload={"recipe_id": recipe_id, "previous_active": int(before["active"]), "version": new_version},
            now=now,
        )
        result = _craft_recipe_from_con(con, recipe_id)
        con.commit()
    result["action_id"] = action_id
    return result


def craft_clone_recipe(
    *,
    guild_id: int,
    recipe_id: int,
    product_name: str,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    source = craft_get_recipe(recipe_id, guild_id)
    if source is None:
        raise ValueError("craft_recipe_not_found")
    return craft_create_recipe(
        guild_id=guild_id,
        product_name=product_name,
        treasury_cost_per_unit=int(source["treasury_cost_per_unit"]),
        duration_minutes_per_unit=int(source["duration_minutes_per_unit"]),
        max_batch_size=int(source["max_batch_size"]),
        materials=[(item["material_name"], int(item["quantity_per_unit"])) for item in source["materials"]],
        created_by_id=actor_id,
        created_by_display=actor_display,
    )


def _craft_plan_from_con(con: sqlite3.Connection, plan_id: int) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM craft_plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    current_recipe = _craft_recipe_from_con(con, int(result["recipe_id"]))
    recipe = dict(current_recipe or {})
    recipe.update(
        {
            "id": int(result["recipe_id"]),
            "product_name": result.get("product_name_snapshot") or recipe.get("product_name") or "Неизвестный продукт",
            "treasury_cost_per_unit": (
                result.get("treasury_cost_per_unit_snapshot")
                if result.get("treasury_cost_per_unit_snapshot") is not None
                else recipe.get("treasury_cost_per_unit", 0)
            ),
            "duration_minutes_per_unit": (
                result.get("duration_minutes_per_unit_snapshot")
                if result.get("duration_minutes_per_unit_snapshot") is not None
                else recipe.get("duration_minutes_per_unit", 1)
            ),
            "max_batch_size": (
                result.get("max_batch_size_snapshot")
                if result.get("max_batch_size_snapshot") is not None
                else recipe.get("max_batch_size", 1)
            ),
            "version": int(result.get("recipe_version") or 1),
        }
    )
    result["recipe"] = recipe
    materials = con.execute(
        "SELECT * FROM craft_plan_materials WHERE plan_id = ? ORDER BY id ASC",
        (plan_id,),
    ).fetchall()
    result["materials"] = [dict(item) for item in materials]
    active_batch = con.execute(
        "SELECT * FROM craft_batches WHERE plan_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (plan_id,),
    ).fetchone()
    last_batch = con.execute(
        "SELECT * FROM craft_batches WHERE plan_id = ? ORDER BY id DESC LIMIT 1",
        (plan_id,),
    ).fetchone()
    result["active_batch"] = _finance_row(active_batch)
    result["last_batch"] = _finance_row(last_batch)
    purchase_row = con.execute(
        "SELECT COALESCE(SUM(total_cost), 0) AS total, COUNT(*) AS count FROM craft_purchases WHERE plan_id = ? AND undone_at IS NULL",
        (plan_id,),
    ).fetchone()
    result["purchase_cost_total"] = int(purchase_row["total"] or 0)
    result["purchase_count"] = int(purchase_row["count"] or 0)
    sale_row = con.execute(
        "SELECT COUNT(*) AS count FROM craft_sales WHERE plan_id = ? AND undone_at IS NULL",
        (plan_id,),
    ).fetchone()
    result["sale_count"] = int(sale_row["count"] or 0)
    return result


def craft_create_plan(
    *,
    guild_id: int,
    recipe_id: int,
    channel_id: int,
    attempts_total: int,
    responsible_id: int,
    responsible_display: str | None,
    created_by_id: int,
    created_by_display: str | None,
) -> dict[str, Any]:
    if attempts_total <= 0 or attempts_total > 1_000_000:
        raise ValueError("craft_bad_attempts")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        recipe = _craft_recipe_from_con(con, recipe_id)
        if recipe is None or int(recipe["guild_id"]) != guild_id or not int(recipe["active"]):
            con.rollback()
            raise ValueError("craft_recipe_not_found")
        material_totals = [int(material["quantity_per_unit"]) * int(attempts_total) for material in recipe["materials"]]
        if any(total > 9_000_000_000_000_000_000 for total in material_totals):
            con.rollback()
            raise ValueError("craft_plan_too_large")
        cur = con.execute(
            """
            INSERT INTO craft_plans(
                guild_id, recipe_id, channel_id, message_id, thread_id,
                responsible_id, responsible_display, created_by_id, created_by_display,
                recipe_version, product_name_snapshot, treasury_cost_per_unit_snapshot,
                duration_minutes_per_unit_snapshot, max_batch_size_snapshot,
                stage, attempts_total, attempts_queued, attempts_completed,
                product_stock, final_product_qty, estimated_unit_price,
                market_listed_qty, sold_qty, total_revenue,
                completion_message_id, created_at, updated_at, completed_at
            )
            VALUES(?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'procurement', ?, 0, 0,
                   0, NULL, NULL, 0, 0, 0, NULL, ?, ?, NULL)
            """,
            (
                guild_id,
                recipe_id,
                channel_id,
                responsible_id,
                responsible_display,
                created_by_id,
                created_by_display,
                int(recipe.get("version") or 1),
                str(recipe["product_name"]),
                int(recipe["treasury_cost_per_unit"]),
                int(recipe["duration_minutes_per_unit"]),
                int(recipe["max_batch_size"]),
                int(attempts_total),
                now,
                now,
            ),
        )
        plan_id = int(cur.lastrowid)
        con.executemany(
            """
            INSERT INTO craft_plan_materials(
                plan_id, recipe_material_id, material_name, quantity_per_unit,
                required_total, stock_quantity, purchased_quantity, spent_total, updated_at
            )
            VALUES(?, ?, ?, ?, ?, 0, 0, 0, ?)
            """,
            [
                (
                    plan_id,
                    int(material["id"]),
                    str(material["material_name"]),
                    int(material["quantity_per_unit"]),
                    material_totals[index],
                    now,
                )
                for index, material in enumerate(recipe["materials"])
            ],
        )
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="plan_created",
            actor_id=created_by_id,
            actor_display=created_by_display,
            details={
                "product_name": recipe["product_name"],
                "attempts_total": int(attempts_total),
                "responsible_id": responsible_id,
                "responsible_display": responsible_display,
            },
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=created_by_id,
            actor_display=created_by_display,
            module="craft",
            action_kind="plan_created",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Создан план крафта #{plan_id}: {recipe['product_name']}",
            payload={"plan_id": plan_id, "craft_event_id": event_id},
            now=now,
        )
        plan = _craft_plan_from_con(con, plan_id)
        con.commit()
    if plan is None:
        raise RuntimeError("craft_plan_create_failed")
    plan["action_id"] = action_id
    return plan


def craft_bind_plan_message(*, plan_id: int, channel_id: int, message_id: int, thread_id: int) -> dict[str, Any] | None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE craft_plans
            SET channel_id = ?, message_id = ?, thread_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (channel_id, message_id, thread_id, now, plan_id),
        )
        plan = _craft_plan_from_con(con, plan_id)
        con.commit()
    return plan


def craft_delete_unbound_plan(plan_id: int) -> bool:
    """Remove a plan that Discord could not bind to a public message/thread."""
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            """
            SELECT id FROM craft_plans
            WHERE id = ? AND message_id IS NULL AND thread_id IS NULL
              AND stage = 'procurement' AND attempts_queued = 0 AND attempts_completed = 0
              AND NOT EXISTS (SELECT 1 FROM craft_purchases WHERE plan_id = craft_plans.id)
            """,
            (plan_id,),
        ).fetchone()
        if plan is None:
            con.rollback()
            return False
        con.execute("DELETE FROM craft_events WHERE plan_id = ?", (plan_id,))
        con.execute("DELETE FROM craft_plan_materials WHERE plan_id = ?", (plan_id,))
        con.execute("DELETE FROM bot_actions WHERE target_type = 'craft_plan' AND target_id = ?", (str(plan_id),))
        con.execute("DELETE FROM craft_plans WHERE id = ?", (plan_id,))
        con.commit()
        return True


def craft_get_plan(plan_id: int, guild_id: int | None = None) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        plan = _craft_plan_from_con(con, plan_id)
    if plan is not None and guild_id is not None and int(plan["guild_id"]) != guild_id:
        return None
    return plan


def craft_active_plans(guild_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT id FROM craft_plans
            WHERE guild_id = ? AND stage NOT IN ('completed', 'cancelled')
            ORDER BY id DESC LIMIT ?
            """,
            (guild_id, max(1, min(int(limit), 100))),
        ).fetchall()
        return [plan for row in rows if (plan := _craft_plan_from_con(con, int(row["id"]))) is not None]


def craft_recent_plans(guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            "SELECT id FROM craft_plans WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
            (guild_id, max(1, min(int(limit), 50))),
        ).fetchall()
        return [plan for row in rows if (plan := _craft_plan_from_con(con, int(row["id"]))) is not None]


def craft_add_purchase(
    *,
    guild_id: int,
    plan_id: int,
    plan_material_id: int,
    quantity: int,
    total_cost: int,
    finance_code: str | None,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    if quantity <= 0 or total_cost < 0:
        raise ValueError("craft_bad_purchase")
    now = utc_now_iso()
    clean_code = re.sub(r"[^A-Za-z]", "", str(finance_code or "")).upper() or None
    if clean_code is not None and len(clean_code) != 4:
        raise ValueError("craft_bad_finance_code")

    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            "SELECT * FROM craft_plans WHERE id = ? AND guild_id = ?",
            (plan_id, guild_id),
        ).fetchone()
        if plan is None:
            con.rollback()
            raise ValueError("craft_plan_not_found")
        if str(plan["stage"]) not in {"procurement", "crafting"}:
            con.rollback()
            raise ValueError("craft_not_procurement")
        material = con.execute(
            "SELECT * FROM craft_plan_materials WHERE id = ? AND plan_id = ?",
            (plan_material_id, plan_id),
        ).fetchone()
        if material is None:
            con.rollback()
            raise ValueError("craft_material_not_found")
        remaining_attempts = max(0, int(plan["attempts_total"]) - int(plan["attempts_queued"]))
        target_stock = int(material["quantity_per_unit"]) * remaining_attempts

        finance_event_id = None
        if clean_code is not None:
            finance_event = con.execute(
                """
                SELECT e.* FROM finance_events AS e
                WHERE e.guild_id = ? AND e.event_kind = 'withdraw' AND e.game_code = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM finance_events AS u WHERE u.reversed_event_id = e.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM craft_purchases AS p WHERE p.finance_event_id = e.id AND p.undone_at IS NULL
                  )
                ORDER BY e.id DESC LIMIT 1
                """,
                (guild_id, clean_code),
            ).fetchone()
            if finance_event is None:
                con.rollback()
                raise ValueError("craft_finance_code_not_found")
            if int(finance_event["amount"]) != int(total_cost):
                con.rollback()
                raise ValueError("craft_finance_amount_mismatch")
            finance_event_id = int(finance_event["id"])

        cur = con.execute(
            """
            INSERT INTO craft_purchases(
                plan_id, plan_material_id, actor_id, actor_display,
                quantity, total_cost, finance_event_id, finance_code, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                plan_material_id,
                actor_id,
                actor_display,
                int(quantity),
                int(total_cost),
                finance_event_id,
                clean_code,
                now,
            ),
        )
        purchase_id = int(cur.lastrowid)
        con.execute(
            """
            UPDATE craft_plan_materials
            SET stock_quantity = stock_quantity + ?,
                purchased_quantity = purchased_quantity + ?,
                spent_total = spent_total + ?,
                updated_at = ?
            WHERE id = ?
            """,
            (int(quantity), int(quantity), int(total_cost), now, plan_material_id),
        )
        updated_material = con.execute(
            "SELECT * FROM craft_plan_materials WHERE id = ?",
            (plan_material_id,),
        ).fetchone()
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="purchase",
            actor_id=actor_id,
            actor_display=actor_display,
            details={
                "purchase_id": purchase_id,
                "material_name": str(material["material_name"]),
                "quantity": int(quantity),
                "total_cost": int(total_cost),
                "average_unit_price": (float(total_cost) / int(quantity)),
                "stock_after": int(updated_material["stock_quantity"]),
                "required_total": int(updated_material["required_total"]),
                "required_for_unqueued": target_stock,
                "remaining": max(0, target_stock - int(updated_material["stock_quantity"])),
                "finance_code": clean_code,
                "finance_event_id": finance_event_id,
            },
            now=now,
        )

        material_rows = con.execute(
            "SELECT quantity_per_unit, stock_quantity FROM craft_plan_materials WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
        materials_ready = all(
            int(row["stock_quantity"]) >= int(row["quantity_per_unit"]) * remaining_attempts
            for row in material_rows
        )
        stage_changed = str(plan["stage"]) == "procurement" and materials_ready
        if stage_changed:
            con.execute(
                "UPDATE craft_plans SET stage = 'crafting', updated_at = ? WHERE id = ?",
                (now, plan_id),
            )
            _craft_add_event(
                con,
                plan_id=plan_id,
                guild_id=guild_id,
                event_kind="stage_crafting",
                actor_id=None,
                actor_display=None,
                details={"message": "Все материалы для оставшихся циклов собраны. Можно начинать крафт."},
                now=now,
            )
        else:
            con.execute("UPDATE craft_plans SET updated_at = ? WHERE id = ?", (now, plan_id))
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="craft_purchase",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Закупка для плана #{plan_id}: {material['material_name']} × {int(quantity)}",
            payload={
                "plan_id": plan_id,
                "purchase_id": purchase_id,
                "craft_event_id": event_id,
                "stage_before": str(plan["stage"]),
            },
            now=now,
        )
        result = _craft_plan_from_con(con, plan_id)
        con.commit()
    return {"plan": result, "purchase_id": purchase_id, "stage_changed": stage_changed, "action_id": action_id}


def craft_inventory_check(
    *,
    guild_id: int,
    plan_id: int,
    material_quantities: dict[int, int],
    product_quantity: int,
    note: str | None,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    if product_quantity < 0 or any(int(value) < 0 for value in material_quantities.values()):
        raise ValueError("craft_bad_inventory")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            "SELECT * FROM craft_plans WHERE id = ? AND guild_id = ?",
            (plan_id, guild_id),
        ).fetchone()
        if plan is None or str(plan["stage"]) not in CRAFT_ACTIVE_STAGES:
            con.rollback()
            raise ValueError("craft_plan_not_active")
        materials = con.execute(
            "SELECT * FROM craft_plan_materials WHERE plan_id = ? ORDER BY id",
            (plan_id,),
        ).fetchall()
        expected_ids = {int(row["id"]) for row in materials}
        if set(material_quantities) != expected_ids:
            con.rollback()
            raise ValueError("craft_inventory_incomplete")
        previous_materials = {str(row["id"]): int(row["stock_quantity"]) for row in materials}
        for material in materials:
            material_id = int(material["id"])
            con.execute(
                "UPDATE craft_plan_materials SET stock_quantity = ?, updated_at = ? WHERE id = ?",
                (int(material_quantities[material_id]), now, material_id),
            )
        con.execute(
            "UPDATE craft_plans SET product_stock = ?, updated_at = ? WHERE id = ?",
            (int(product_quantity), now, plan_id),
        )
        material_snapshot = {
            str(row["material_name"]): int(material_quantities[int(row["id"])]) for row in materials
        }
        check_cur = con.execute(
            """
            INSERT INTO craft_inventory_checks(
                plan_id, actor_id, actor_display, materials_json,
                product_quantity, note, previous_materials_json,
                previous_product_quantity, previous_stage, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                actor_id,
                actor_display,
                json.dumps(material_snapshot, ensure_ascii=False),
                int(product_quantity),
                str(note or "").strip() or None,
                json.dumps(previous_materials, ensure_ascii=False),
                int(plan["product_stock"] or 0),
                str(plan["stage"]),
                now,
            ),
        )
        check_id = int(check_cur.lastrowid)
        stage_changed = False
        if str(plan["stage"]) == "procurement":
            missing = con.execute(
                "SELECT COUNT(*) AS n FROM craft_plan_materials WHERE plan_id = ? AND stock_quantity < required_total",
                (plan_id,),
            ).fetchone()
            if int(missing["n"] or 0) == 0:
                con.execute("UPDATE craft_plans SET stage = 'crafting' WHERE id = ?", (plan_id,))
                stage_changed = True
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="inventory_check",
            actor_id=actor_id,
            actor_display=actor_display,
            details={
                "materials": material_snapshot,
                "product_quantity": int(product_quantity),
                "note": str(note or "").strip() or None,
                "stage_changed": stage_changed,
            },
            now=now,
        )
        if stage_changed:
            _craft_add_event(
                con,
                plan_id=plan_id,
                guild_id=guild_id,
                event_kind="stage_crafting",
                actor_id=None,
                actor_display=None,
                details={"message": "Сверка подтвердила наличие всех материалов. Можно начинать крафт."},
                now=now,
            )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="craft_inventory_check",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Сверка склада плана #{plan_id}",
            payload={"plan_id": plan_id, "inventory_check_id": check_id, "craft_event_id": event_id},
            now=now,
        )
        result = _craft_plan_from_con(con, plan_id)
        con.commit()
    return {"plan": result, "stage_changed": stage_changed, "action_id": action_id}


def craft_start_batch(
    *,
    guild_id: int,
    plan_id: int,
    quantity: int,
    actor_id: int,
    actor_display: str | None,
    log_channel_id: int,
    admin_user_id: int,
) -> dict[str, Any]:
    if quantity <= 0:
        raise ValueError("craft_bad_batch_quantity")
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            "SELECT * FROM craft_plans WHERE id = ? AND guild_id = ?",
            (plan_id, guild_id),
        ).fetchone()
        if plan is None:
            con.rollback()
            raise ValueError("craft_plan_not_found")
        stage_before = str(plan["stage"])
        if stage_before not in {"procurement", "crafting"}:
            con.rollback()
            raise ValueError("craft_not_crafting")
        active = con.execute(
            "SELECT id FROM craft_batches WHERE plan_id = ? AND status = 'active' LIMIT 1",
            (plan_id,),
        ).fetchone()
        if active is not None:
            con.rollback()
            raise ValueError("craft_batch_active")
        recipe = {
            "product_name": str(plan["product_name_snapshot"] or "Неизвестный продукт"),
            "treasury_cost_per_unit": int(plan["treasury_cost_per_unit_snapshot"] or 0),
            "duration_minutes_per_unit": int(plan["duration_minutes_per_unit_snapshot"] or 1),
            "max_batch_size": int(plan["max_batch_size_snapshot"] or 1),
        }
        remaining = int(plan["attempts_total"]) - int(plan["attempts_queued"])
        if quantity > remaining or quantity > int(recipe["max_batch_size"]):
            con.rollback()
            raise ValueError("craft_batch_too_large")
        materials = con.execute(
            "SELECT * FROM craft_plan_materials WHERE plan_id = ? ORDER BY id",
            (plan_id,),
        ).fetchall()
        missing_names = []
        for material in materials:
            needed = int(material["quantity_per_unit"]) * int(quantity)
            if int(material["stock_quantity"]) < needed:
                missing_names.append(str(material["material_name"]))
        if missing_names:
            con.rollback()
            raise ValueError("craft_batch_materials_missing:" + ", ".join(missing_names))

        treasury_cost = int(recipe["treasury_cost_per_unit"]) * int(quantity)
        finance_event_id = None
        if treasury_cost > 0:
            latest = con.execute(
                "SELECT balance_after FROM finance_events WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
                (guild_id,),
            ).fetchone()
            balance_before = None if latest is None or latest["balance_after"] is None else int(latest["balance_after"])
            if balance_before is None:
                con.rollback()
                raise ValueError("craft_finance_balance_unknown")
            if balance_before < treasury_cost:
                con.rollback()
                raise ValueError("craft_finance_insufficient")
            balance_after = balance_before - treasury_cost
            finance_cur = con.execute(
                """
                INSERT INTO finance_events(
                    guild_id, event_kind, report_date, prompt_id, reversed_event_id,
                    amount, delta, balance_before, balance_after, reason,
                    captcha_digest, game_code, actor_id, actor_display,
                    channel_id, message_id, created_at
                )
                VALUES(?, 'withdraw', NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    guild_id,
                    treasury_cost,
                    -treasury_cost,
                    balance_before,
                    balance_after,
                    f"Автоматический расход на крафт: {recipe['product_name']} × {quantity} (план #{plan_id})",
                    f"craft:{plan_id}",
                    None,
                    actor_id,
                    actor_display,
                    int(plan["channel_id"]),
                    now,
                ),
            )
            finance_event_id = int(finance_cur.lastrowid)
            _finance_enqueue_notifications(
                con,
                event_id=finance_event_id,
                log_channel_id=log_channel_id,
                admin_user_id=admin_user_id,
                now=now,
            )

        due_dt = now_dt + timedelta(minutes=int(recipe["duration_minutes_per_unit"]) * int(quantity))
        for material in materials:
            needed = int(material["quantity_per_unit"]) * int(quantity)
            con.execute(
                "UPDATE craft_plan_materials SET stock_quantity = stock_quantity - ?, updated_at = ? WHERE id = ?",
                (needed, now, int(material["id"])),
            )
        batch_cur = con.execute(
            """
            INSERT INTO craft_batches(
                plan_id, quantity, started_by_id, started_by_display,
                started_at, due_at, status, completed_at,
                finance_event_id, finance_code
            )
            VALUES(?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?)
            """,
            (
                plan_id,
                int(quantity),
                actor_id,
                actor_display,
                now,
                due_dt.isoformat(),
                finance_event_id,
                None,
            ),
        )
        batch_id = int(batch_cur.lastrowid)
        con.execute(
            """
            UPDATE craft_plans
            SET stage = 'crafting', attempts_queued = attempts_queued + ?, updated_at = ?
            WHERE id = ?
            """,
            (int(quantity), now, plan_id),
        )
        if stage_before == "procurement":
            _craft_add_event(
                con,
                plan_id=plan_id,
                guild_id=guild_id,
                event_kind="stage_crafting",
                actor_id=actor_id,
                actor_display=actor_display,
                details={"message": "Материалов достаточно для выбранного цикла. Производство начато."},
                now=now,
            )
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="batch_started",
            actor_id=actor_id,
            actor_display=actor_display,
            details={
                "batch_id": batch_id,
                "quantity": int(quantity),
                "started_at": now,
                "due_at": due_dt.isoformat(),
                "duration_minutes": int(recipe["duration_minutes_per_unit"]) * int(quantity),
                "treasury_cost": treasury_cost,
                "finance_event_id": finance_event_id,
                "finance_code": None,
            },
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="craft_batch_started",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Запущен цикл плана #{plan_id}: {int(quantity)} шт.",
            payload={
                "plan_id": plan_id,
                "batch_id": batch_id,
                "craft_event_id": event_id,
                "finance_event_id": finance_event_id,
                "log_channel_id": log_channel_id,
                "admin_user_id": admin_user_id,
                "stage_before": stage_before,
            },
            now=now,
        )
        result = _craft_plan_from_con(con, plan_id)
        batch = con.execute("SELECT * FROM craft_batches WHERE id = ?", (batch_id,)).fetchone()
        con.commit()
    return {
        "plan": result,
        "batch": _finance_row(batch),
        "finance_event_id": finance_event_id,
        "action_id": action_id,
    }


def craft_complete_due_batches(now_iso: str | None = None) -> list[int]:
    now = now_iso or utc_now_iso()
    changed: list[int] = []
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute(
            "SELECT * FROM craft_batches WHERE status = 'active' AND due_at <= ? ORDER BY id",
            (now,),
        ).fetchall()
        for batch in rows:
            plan = con.execute("SELECT * FROM craft_plans WHERE id = ?", (int(batch["plan_id"]),)).fetchone()
            if plan is None or str(plan["stage"]) != "crafting":
                con.execute(
                    "UPDATE craft_batches SET status = 'completed', completed_at = ? WHERE id = ?",
                    (now, int(batch["id"])),
                )
                continue
            quantity = int(batch["quantity"])
            con.execute(
                "UPDATE craft_batches SET status = 'completed', completed_at = ? WHERE id = ?",
                (now, int(batch["id"])),
            )
            con.execute(
                """
                UPDATE craft_plans
                SET attempts_completed = attempts_completed + ?,
                    product_stock = product_stock + ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (quantity, quantity, now, int(plan["id"])),
            )
            _craft_add_event(
                con,
                plan_id=int(plan["id"]),
                guild_id=int(plan["guild_id"]),
                event_kind="batch_completed",
                actor_id=None,
                actor_display=None,
                details={
                    "batch_id": int(batch["id"]),
                    "quantity": quantity,
                    "due_at": str(batch["due_at"]),
                    "completed_at": now,
                },
                now=now,
            )
            updated = con.execute("SELECT * FROM craft_plans WHERE id = ?", (int(plan["id"]),)).fetchone()
            if int(updated["attempts_completed"]) >= int(updated["attempts_total"]):
                con.execute(
                    "UPDATE craft_plans SET stage = 'awaiting_output', updated_at = ? WHERE id = ?",
                    (now, int(plan["id"])),
                )
                _craft_add_event(
                    con,
                    plan_id=int(plan["id"]),
                    guild_id=int(plan["guild_id"]),
                    event_kind="stage_awaiting_output",
                    actor_id=None,
                    actor_display=None,
                    details={"message": "Все попытки завершены. Требуется итоговая сверка продукта."},
                    now=now,
                )
            changed.append(int(plan["id"]))
        con.commit()
    return sorted(set(changed))


def craft_reminder_candidates(guild_id: int) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT p.*, b.id AS last_batch_id, b.due_at AS last_due_at
            FROM craft_plans AS p
            JOIN craft_batches AS b ON b.id = (
                SELECT MAX(id) FROM craft_batches WHERE plan_id = p.id AND status = 'completed'
            )
            WHERE p.guild_id = ? AND p.stage = 'crafting'
              AND p.attempts_completed < p.attempts_total
              AND NOT EXISTS (
                  SELECT 1 FROM craft_batches AS active
                  WHERE active.plan_id = p.id AND active.status = 'active'
              )
            ORDER BY p.id
            """,
            (guild_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def craft_claim_reminder(plan_id: int, reminder_key: str) -> bool:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        try:
            con.execute(
                "INSERT INTO craft_reminders(plan_id, reminder_key, message_id, created_at, deleted_at) VALUES(?, ?, NULL, ?, NULL)",
                (plan_id, reminder_key, now),
            )
            con.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def craft_set_reminder_message(plan_id: int, reminder_key: str, message_id: int) -> None:
    with _db_lock, connect() as con:
        con.execute(
            "UPDATE craft_reminders SET message_id = ? WHERE plan_id = ? AND reminder_key = ?",
            (message_id, plan_id, reminder_key),
        )
        con.commit()


def craft_release_reminder(plan_id: int, reminder_key: str) -> None:
    with _db_lock, connect() as con:
        con.execute(
            "DELETE FROM craft_reminders WHERE plan_id = ? AND reminder_key = ? AND message_id IS NULL",
            (plan_id, reminder_key),
        )
        con.commit()


def craft_open_reminder_messages(plan_id: int) -> list[int]:
    with _db_lock, connect() as con:
        rows = con.execute(
            "SELECT message_id FROM craft_reminders WHERE plan_id = ? AND message_id IS NOT NULL AND deleted_at IS NULL",
            (plan_id,),
        ).fetchall()
    return [int(row["message_id"]) for row in rows]


def craft_mark_reminders_deleted(plan_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            "UPDATE craft_reminders SET deleted_at = ? WHERE plan_id = ? AND deleted_at IS NULL",
            (now, plan_id),
        )
        con.commit()


def craft_mark_reminder_deleted(plan_id: int, message_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE craft_reminders SET deleted_at = ?
            WHERE plan_id = ? AND message_id = ? AND deleted_at IS NULL
            """,
            (now, plan_id, message_id),
        )
        con.commit()


def craft_pending_events(limit: int = 50) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT e.*, p.thread_id, p.channel_id
            FROM craft_events AS e
            JOIN craft_plans AS p ON p.id = e.plan_id
            WHERE e.thread_message_id IS NULL AND p.thread_id IS NOT NULL
            ORDER BY e.id ASC LIMIT ?
            """,
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    return [dict(row) for row in rows]


def craft_mark_event_sent(event_id: int, thread_message_id: int) -> None:
    with _db_lock, connect() as con:
        con.execute(
            "UPDATE craft_events SET thread_message_id = ? WHERE id = ?",
            (thread_message_id, event_id),
        )
        con.commit()


def craft_set_final_output(
    *,
    guild_id: int,
    plan_id: int,
    product_quantity: int,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    if product_quantity < 0:
        raise ValueError("craft_bad_output")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            "SELECT * FROM craft_plans WHERE id = ? AND guild_id = ?",
            (plan_id, guild_id),
        ).fetchone()
        if plan is None or str(plan["stage"]) != "awaiting_output":
            con.rollback()
            raise ValueError("craft_not_awaiting_output")
        if product_quantity > int(plan["attempts_completed"]):
            con.rollback()
            raise ValueError("craft_output_too_large")
        if product_quantity == 0:
            stage = "completed"
            completed_at = now
        else:
            stage = "listing"
            completed_at = None
        con.execute(
            """
            UPDATE craft_plans
            SET final_product_qty = ?, product_stock = ?, stage = ?,
                completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (int(product_quantity), int(product_quantity), stage, completed_at, now, plan_id),
        )
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="final_output",
            actor_id=actor_id,
            actor_display=actor_display,
            details={
                "product_quantity": int(product_quantity),
                "attempts_completed": int(plan["attempts_completed"]),
                "next_stage": stage,
            },
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="craft_final_output",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Указан итог плана #{plan_id}: {int(product_quantity)} шт.",
            payload={
                "plan_id": plan_id,
                "craft_event_id": event_id,
                "previous_product_stock": int(plan["product_stock"] or 0),
                "previous_final_product_qty": plan["final_product_qty"],
                "previous_stage": str(plan["stage"]),
                "previous_completed_at": plan["completed_at"],
            },
            now=now,
        )
        result = _craft_plan_from_con(con, plan_id)
        con.commit()
    result["action_id"] = action_id
    return result


def craft_set_estimated_price(
    *,
    guild_id: int,
    plan_id: int,
    unit_price: int,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    if unit_price <= 0:
        raise ValueError("craft_bad_price")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            "SELECT * FROM craft_plans WHERE id = ? AND guild_id = ?",
            (plan_id, guild_id),
        ).fetchone()
        if plan is None or str(plan["stage"]) not in {"listing", "selling"}:
            con.rollback()
            raise ValueError("craft_not_listing")
        con.execute(
            "UPDATE craft_plans SET estimated_unit_price = ?, updated_at = ? WHERE id = ?",
            (int(unit_price), now, plan_id),
        )
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="estimated_price",
            actor_id=actor_id,
            actor_display=actor_display,
            details={"unit_price": int(unit_price)},
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="craft_estimated_price",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Изменена примерная цена плана #{plan_id}: {int(unit_price)}",
            payload={
                "plan_id": plan_id,
                "craft_event_id": event_id,
                "previous_price": plan["estimated_unit_price"],
            },
            now=now,
        )
        result = _craft_plan_from_con(con, plan_id)
        con.commit()
    result["action_id"] = action_id
    return result


def craft_add_market_listing(
    *,
    guild_id: int,
    plan_id: int,
    quantity: int,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    if quantity <= 0:
        raise ValueError("craft_bad_listing")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            "SELECT * FROM craft_plans WHERE id = ? AND guild_id = ?",
            (plan_id, guild_id),
        ).fetchone()
        if plan is None or str(plan["stage"]) != "listing":
            con.rollback()
            raise ValueError("craft_not_listing")
        if plan["estimated_unit_price"] is None:
            con.rollback()
            raise ValueError("craft_price_required")
        remaining = int(plan["final_product_qty"] or 0) - int(plan["market_listed_qty"])
        if quantity > remaining:
            con.rollback()
            raise ValueError("craft_listing_too_large")
        listing_cur = con.execute(
            """
            INSERT INTO craft_market_listings(
                plan_id, actor_id, actor_display, quantity,
                estimated_unit_price, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (plan_id, actor_id, actor_display, int(quantity), plan["estimated_unit_price"], now),
        )
        listing_id = int(listing_cur.lastrowid)
        new_total = int(plan["market_listed_qty"]) + int(quantity)
        new_stage = "selling" if new_total >= int(plan["final_product_qty"] or 0) else "listing"
        con.execute(
            "UPDATE craft_plans SET market_listed_qty = ?, stage = ?, updated_at = ? WHERE id = ?",
            (new_total, new_stage, now, plan_id),
        )
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="market_listing",
            actor_id=actor_id,
            actor_display=actor_display,
            details={
                "quantity": int(quantity),
                "listed_total": new_total,
                "remaining_to_list": max(0, int(plan["final_product_qty"] or 0) - new_total),
                "estimated_unit_price": plan["estimated_unit_price"],
                "next_stage": new_stage,
            },
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="craft_market_listing",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Выставлено на маркет по плану #{plan_id}: {int(quantity)} шт.",
            payload={
                "plan_id": plan_id,
                "listing_id": listing_id,
                "craft_event_id": event_id,
                "previous_stage": str(plan["stage"]),
            },
            now=now,
        )
        result = _craft_plan_from_con(con, plan_id)
        con.commit()
    result["action_id"] = action_id
    return result


def craft_add_sale(
    *,
    guild_id: int,
    plan_id: int,
    quantity: int,
    total_amount: int,
    actor_id: int,
    actor_display: str | None,
) -> dict[str, Any]:
    if quantity <= 0 or total_amount <= 0:
        raise ValueError("craft_bad_sale")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        plan = con.execute(
            "SELECT * FROM craft_plans WHERE id = ? AND guild_id = ?",
            (plan_id, guild_id),
        ).fetchone()
        if plan is None or str(plan["stage"]) != "selling":
            con.rollback()
            raise ValueError("craft_not_selling")
        remaining = int(plan["final_product_qty"] or 0) - int(plan["sold_qty"])
        if quantity > remaining:
            con.rollback()
            raise ValueError("craft_sale_too_large")
        sale_cur = con.execute(
            """
            INSERT INTO craft_sales(
                plan_id, actor_id, actor_display, quantity, total_amount, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (plan_id, actor_id, actor_display, int(quantity), int(total_amount), now),
        )
        sale_id = int(sale_cur.lastrowid)
        sold_total = int(plan["sold_qty"]) + int(quantity)
        revenue_total = int(plan["total_revenue"]) + int(total_amount)
        product_stock = max(0, int(plan["final_product_qty"] or 0) - sold_total)
        completed = sold_total >= int(plan["final_product_qty"] or 0)
        new_stage = "completed" if completed else "selling"
        con.execute(
            """
            UPDATE craft_plans
            SET sold_qty = ?, total_revenue = ?, product_stock = ?, stage = ?,
                completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (sold_total, revenue_total, product_stock, new_stage, now if completed else None, now, plan_id),
        )
        event_id = _craft_add_event(
            con,
            plan_id=plan_id,
            guild_id=guild_id,
            event_kind="sale",
            actor_id=actor_id,
            actor_display=actor_display,
            details={
                "quantity": int(quantity),
                "total_amount": int(total_amount),
                "average_unit_price": float(total_amount) / int(quantity),
                "sold_total": sold_total,
                "remaining_to_sell": max(0, int(plan["final_product_qty"] or 0) - sold_total),
                "revenue_total": revenue_total,
                "completed": completed,
            },
            now=now,
        )
        action_id = _record_bot_action(
            con,
            guild_id=guild_id,
            actor_id=actor_id,
            actor_display=actor_display,
            module="craft",
            action_kind="craft_sale",
            target_type="craft_plan",
            target_id=plan_id,
            summary=f"Продажа по плану #{plan_id}: {int(quantity)} шт. за {int(total_amount)}",
            payload={
                "plan_id": plan_id,
                "sale_id": sale_id,
                "craft_event_id": event_id,
                "previous_stage": str(plan["stage"]),
                "previous_completed_at": plan["completed_at"],
            },
            now=now,
        )
        result = _craft_plan_from_con(con, plan_id)
        con.commit()
    result["action_id"] = action_id
    return result


def craft_completion_candidates(guild_id: int) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT id FROM craft_plans
            WHERE guild_id = ? AND stage = 'completed' AND completion_message_id IS NULL
            ORDER BY id
            """,
            (guild_id,),
        ).fetchall()
        return [plan for row in rows if (plan := _craft_plan_from_con(con, int(row["id"]))) is not None]


def craft_set_completion_message(plan_id: int, message_id: int) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            "UPDATE craft_plans SET completion_message_id = ?, updated_at = ? WHERE id = ?",
            (message_id, now, plan_id),
        )
        con.commit()


def craft_stats(guild_id: int, days: int = 30) -> dict[str, Any]:
    period_days = max(1, min(int(days), 3650))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)).isoformat()
    with _db_lock, connect() as con:
        plans = con.execute(
            "SELECT * FROM craft_plans WHERE guild_id = ? AND created_at >= ? ORDER BY id",
            (guild_id, cutoff),
        ).fetchall()
        plan_ids = [int(row["id"]) for row in plans]
        active_count = sum(1 for row in plans if str(row["stage"]) in CRAFT_ACTIVE_STAGES)
        completed_count = sum(1 for row in plans if str(row["stage"]) == "completed")
        cancelled_count = sum(1 for row in plans if str(row["stage"]) == "cancelled")
        attempts_planned = sum(int(row["attempts_total"] or 0) for row in plans)
        attempts_completed = sum(int(row["attempts_completed"] or 0) for row in plans)
        output_total = sum(int(row["final_product_qty"] or 0) for row in plans)
        sold_total = sum(int(row["sold_qty"] or 0) for row in plans)
        revenue = sum(int(row["total_revenue"] or 0) for row in plans)

        if plan_ids:
            placeholders = ",".join("?" for _ in plan_ids)
            purchases = int(
                con.execute(
                    f"SELECT COALESCE(SUM(total_cost), 0) AS n FROM craft_purchases WHERE undone_at IS NULL AND plan_id IN ({placeholders})",
                    plan_ids,
                ).fetchone()["n"]
                or 0
            )
            batch_row = con.execute(
                f"""
                SELECT COUNT(*) AS batches, COALESCE(SUM(quantity), 0) AS units,
                       AVG(CASE WHEN completed_at IS NOT NULL
                           THEN MAX(0, (julianday(completed_at) - julianday(due_at)) * 86400)
                           ELSE NULL END) AS average_delay_seconds
                FROM craft_batches
                WHERE undone_at IS NULL AND plan_id IN ({placeholders})
                """,
                plan_ids,
            ).fetchone()
            craft_fees = int(
                con.execute(
                    f"""
                    SELECT COALESCE(SUM(e.amount), 0) AS n
                    FROM finance_events e
                    JOIN craft_plans p ON e.captcha_digest = ('craft:' || p.id)
                    WHERE p.id IN ({placeholders})
                      AND e.event_kind = 'withdraw'
                      AND NOT EXISTS (SELECT 1 FROM finance_events u WHERE u.reversed_event_id = e.id)
                    """,
                    plan_ids,
                ).fetchone()["n"]
                or 0
            )
            contributor_rows = con.execute(
                f"""
                SELECT actor_id, MAX(actor_display) AS actor_display,
                       SUM(purchases) AS purchases, SUM(batches) AS batches, SUM(sales) AS sales
                FROM (
                    SELECT actor_id, actor_display, COUNT(*) AS purchases, 0 AS batches, 0 AS sales
                    FROM craft_purchases WHERE undone_at IS NULL AND plan_id IN ({placeholders}) GROUP BY actor_id
                    UNION ALL
                    SELECT started_by_id, started_by_display, 0, COUNT(*), 0
                    FROM craft_batches WHERE undone_at IS NULL AND plan_id IN ({placeholders}) GROUP BY started_by_id
                    UNION ALL
                    SELECT actor_id, actor_display, 0, 0, COUNT(*)
                    FROM craft_sales WHERE undone_at IS NULL AND plan_id IN ({placeholders}) GROUP BY actor_id
                ) x
                GROUP BY actor_id
                ORDER BY (SUM(purchases) + SUM(batches) + SUM(sales)) DESC, actor_id
                LIMIT 8
                """,
                [*plan_ids, *plan_ids, *plan_ids],
            ).fetchall()
        else:
            purchases = 0
            craft_fees = 0
            batch_row = {"batches": 0, "units": 0, "average_delay_seconds": None}
            contributor_rows = []

        recipe_rows = con.execute(
            """
            SELECT product_name_snapshot AS product_name, COUNT(*) AS plans,
                   SUM(attempts_completed) AS attempts_completed,
                   SUM(COALESCE(final_product_qty, 0)) AS output,
                   SUM(total_revenue) AS revenue
            FROM craft_plans
            WHERE guild_id = ? AND created_at >= ? AND stage != 'cancelled'
            GROUP BY product_name_snapshot
            ORDER BY revenue DESC, attempts_completed DESC LIMIT 8
            """,
            (guild_id, cutoff),
        ).fetchall()
    success_rate = (output_total / attempts_completed * 100) if attempts_completed else 0.0
    return {
        "days": period_days,
        "cutoff": cutoff,
        "plan_count": len(plans),
        "active_count": active_count,
        "completed_count": completed_count,
        "cancelled_count": cancelled_count,
        "attempts_planned": attempts_planned,
        "attempts_completed": attempts_completed,
        "output_total": output_total,
        "success_rate": success_rate,
        "sold_total": sold_total,
        "revenue": revenue,
        "purchase_cost": purchases,
        "craft_fees": craft_fees,
        "profit": revenue - purchases - craft_fees,
        "batch_count": int(batch_row["batches"] or 0),
        "batch_units": int(batch_row["units"] or 0),
        "average_delay_seconds": (
            None if batch_row["average_delay_seconds"] is None else float(batch_row["average_delay_seconds"])
        ),
        "recipes": [dict(row) for row in recipe_rows],
        "contributors": [dict(row) for row in contributor_rows],
    }


def _finance_reverse_event_in_con(
    con: sqlite3.Connection,
    *,
    guild_id: int,
    event_id: int,
    undone_by_id: int,
    undone_by_display: str | None,
    reason: str,
    channel_id: int | None,
    log_channel_id: int,
    admin_user_id: int,
    now: str,
) -> int | None:
    target = con.execute(
        """
        SELECT e.* FROM finance_events AS e
        WHERE e.guild_id = ? AND e.id = ?
          AND e.event_kind IN ('daily', 'interim', 'deposit', 'withdraw')
          AND NOT EXISTS (
              SELECT 1 FROM finance_events AS u WHERE u.reversed_event_id = e.id
          )
        """,
        (guild_id, event_id),
    ).fetchone()
    if target is None:
        return None
    active_rows = con.execute(
        """
        SELECT e.* FROM finance_events AS e
        WHERE e.guild_id = ?
          AND e.event_kind IN ('daily', 'interim', 'deposit', 'withdraw')
          AND NOT EXISTS (
              SELECT 1 FROM finance_events AS u WHERE u.reversed_event_id = e.id
          )
        ORDER BY e.id ASC
        """,
        (guild_id,),
    ).fetchall()
    balance_before = _finance_replay_balance(active_rows)
    balance_after = _finance_replay_balance(active_rows, omit_event_id=event_id)
    undo_delta = (
        balance_after - balance_before
        if balance_before is not None and balance_after is not None
        else None
    )
    cur = con.execute(
        """
        INSERT INTO finance_events(
            guild_id, event_kind, report_date, prompt_id, reversed_event_id,
            amount, delta, balance_before, balance_after, reason,
            captcha_digest, game_code, actor_id, actor_display,
            channel_id, message_id, created_at
        )
        VALUES(?, 'undo', NULL, NULL, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL, ?)
        """,
        (
            guild_id,
            event_id,
            int(target["amount"]),
            undo_delta,
            balance_before,
            balance_after,
            reason,
            undone_by_id,
            undone_by_display,
            channel_id,
            now,
        ),
    )
    undo_id = int(cur.lastrowid)
    _finance_enqueue_notifications(
        con,
        event_id=undo_id,
        log_channel_id=log_channel_id,
        admin_user_id=admin_user_id,
        now=now,
    )
    return undo_id


def _assert_tvrs_generic_restore_mutable(
    con: sqlite3.Connection,
    payload: dict[str, Any],
) -> None:
    """Fence universal undo from an unfinished consensus lifecycle."""

    table = str(payload.get("table") or "")
    if table not in {"tvrs_bills", "tvrs_votes", "tvrs_live_results"}:
        return
    row_id = int(payload.get("row_id"))
    current = con.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    before_raw = payload.get("before")
    before = dict(before_raw) if isinstance(before_raw, dict) else None
    candidate: sqlite3.Row | dict[str, Any] | None = current or before
    try:
        if table == "tvrs_bills":
            if candidate is not None:
                _tvrs_assert_bill_admin_mutable(con, candidate)  # type: ignore[arg-type]
            elif _tvrs_bill_referenced_by_active_consensus(con, row_id):
                raise ValueError("bill_locked_by_active_consensus")
            return
        if table == "tvrs_live_results":
            if candidate is not None:
                _tvrs_assert_result_admin_mutable(con, candidate)  # type: ignore[arg-type]
            return
        if candidate is not None and _tvrs_bill_referenced_by_active_consensus(
            con,
            int(candidate["bill_id"]),
            int(candidate["guild_id"]),
        ):
            raise ValueError("vote_locked_by_active_consensus")
    except ValueError as exc:
        if str(exc) in {
            "bill_locked_by_active_consensus",
            "result_locked_by_active_consensus",
            "vote_locked_by_active_consensus",
        }:
            raise ValueError("bot_action_locked_by_active_consensus") from exc
        raise


def _restore_generic_row(con: sqlite3.Connection, payload: dict[str, Any]) -> None:
    allowed_tables = {
        "bureau_announcements",
        "sgl_cases",
        "sgl_case_events",
        "sgl_receipts",
        "client_profiles",
        "lawyer_profiles",
        "tvrs_bills",
        "tvrs_votes",
        "tvrs_live_results",
    }
    table = str(payload.get("table") or "")
    if table not in allowed_tables:
        raise ValueError("bot_action_unsupported")
    _assert_tvrs_generic_restore_mutable(con, payload)
    before = payload.get("before")
    primary_key = str(payload.get("primary_key") or "id")
    if primary_key != "id":
        raise ValueError("bot_action_unsupported")
    row_id = int(payload.get("row_id"))
    columns = _table_columns(con, table)
    if before is None:
        con.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        return
    clean = {key: value for key, value in dict(before).items() if key in columns}
    existing = con.execute(f"SELECT id FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if existing is None:
        names = list(clean.keys())
        placeholders = ", ".join("?" for _ in names)
        con.execute(
            f"INSERT INTO {table}({', '.join(names)}) VALUES({placeholders})",
            [clean[name] for name in names],
        )
    else:
        names = [name for name in clean.keys() if name != "id"]
        if not names:
            return
        assignments = ", ".join(f"{name} = ?" for name in names)
        con.execute(
            f"UPDATE {table} SET {assignments} WHERE id = ?",
            [*[clean[name] for name in names], row_id],
        )


def _restore_generic_rows(con: sqlite3.Connection, payload: dict[str, Any]) -> None:
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("bot_action_unsupported")
    # Parent records are intentionally restored first. SQLite installations with
    # foreign-key enforcement enabled can then safely accept their child rows.
    priority = {"sgl_cases": 0, "tvrs_bills": 0, "client_profiles": 0, "lawyer_profiles": 0}
    ordered = sorted(rows, key=lambda item: priority.get(str(dict(item).get("table") or ""), 1))
    # Validate the entire batch before its first write. The surrounding undo
    # transaction is still the final safety net, but preflight keeps protected
    # consensus state untouched even temporarily.
    for row_payload in ordered:
        if not isinstance(row_payload, dict):
            raise ValueError("bot_action_unsupported")
        _assert_tvrs_generic_restore_mutable(con, row_payload)
    for row_payload in ordered:
        if not isinstance(row_payload, dict):
            raise ValueError("bot_action_unsupported")
        _restore_generic_row(con, row_payload)


def bot_undo_action(
    *,
    guild_id: int,
    target_actor_id: int,
    undone_by_id: int,
    undone_by_display: str | None,
    reason: str | None,
    log_channel_id: int,
    admin_user_id: int,
    channel_id: int | None,
    action_id: int | None = None,
) -> dict[str, Any] | None:
    """Undo a recorded bot action without deleting its audit trail.

    Craft actions are compensated in-place. Finance actions use the immutable
    finance ledger. Other modules may register a safe generic row snapshot.
    """
    clean_reason = str(reason or "").strip() or "Отмена действия пользователем"
    with _db_lock:
        with connect() as lookup:
            if action_id is None:
                action_row = lookup.execute(
                    """
                    SELECT * FROM bot_actions
                    WHERE guild_id = ? AND actor_id = ? AND status = 'active'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (guild_id, target_actor_id),
                ).fetchone()
            else:
                action_row = lookup.execute(
                    "SELECT * FROM bot_actions WHERE guild_id = ? AND id = ?",
                    (guild_id, int(action_id)),
                ).fetchone()
        action = _bot_action_dict(action_row)
        if action is None:
            return None
        if str(action["status"]) != "active":
            raise ValueError("bot_action_already_undone")
        if not int(action["reversible"]):
            raise ValueError("bot_action_not_reversible")

        if str(action["module"]) == "finance":
            result = finance_undo_last_action(
                guild_id=guild_id,
                target_actor_id=int(action["actor_id"]),
                undone_by_id=undone_by_id,
                undone_by_display=undone_by_display,
                channel_id=channel_id,
                message_id=None,
                log_channel_id=log_channel_id,
                admin_user_id=admin_user_id,
                target_event_id=int(action["payload"]["finance_event_id"]),
            )
            return {
                "action": bot_get_action(int(action["id"]), guild_id),
                "module": "finance",
                "finance": result,
                "refresh_plan_id": None,
                "completion_message_id": None,
            }

        now = utc_now_iso()
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current_row = con.execute(
                "SELECT * FROM bot_actions WHERE id = ? AND guild_id = ?",
                (int(action["id"]), guild_id),
            ).fetchone()
            if current_row is None or str(current_row["status"]) != "active":
                con.rollback()
                raise ValueError("bot_action_already_undone")
            if action.get("target_id") is not None:
                dependent = con.execute(
                    """
                    SELECT id, summary FROM bot_actions
                    WHERE guild_id = ? AND target_type = ? AND target_id = ?
                      AND status = 'active' AND id > ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (
                        guild_id,
                        str(action["target_type"]),
                        action["target_id"],
                        int(action["id"]),
                    ),
                ).fetchone()
                if dependent is not None:
                    con.rollback()
                    raise ValueError(f"bot_action_has_dependents:{int(dependent['id'])}")

            payload = dict(action.get("payload") or {})
            kind = str(action["action_kind"])
            refresh_plan_id: int | None = None
            completion_message_id: int | None = None
            finance_undo_event_id: int | None = None

            if kind == "recipe_created":
                recipe_id = int(payload["recipe_id"])
                recipe = con.execute("SELECT * FROM craft_recipes WHERE id = ?", (recipe_id,)).fetchone()
                if recipe is None:
                    con.rollback()
                    raise ValueError("craft_recipe_not_found")
                new_version = int(recipe["version"] or 1) + 1
                con.execute(
                    "UPDATE craft_recipes SET active = 0, version = ?, updated_at = ? WHERE id = ?",
                    (new_version, now, recipe_id),
                )
            elif kind == "recipe_updated":
                recipe_id = int(payload["recipe_id"])
                previous = dict(payload["previous"])
                recipe = con.execute("SELECT * FROM craft_recipes WHERE id = ?", (recipe_id,)).fetchone()
                if recipe is None:
                    con.rollback()
                    raise ValueError("craft_recipe_not_found")
                new_version = int(recipe["version"] or 1) + 1
                con.execute(
                    """
                    UPDATE craft_recipes
                    SET product_name = ?, treasury_cost_per_unit = ?, duration_minutes_per_unit = ?,
                        max_batch_size = ?, active = ?, version = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        previous["product_name"],
                        int(previous["treasury_cost_per_unit"]),
                        int(previous["duration_minutes_per_unit"]),
                        int(previous["max_batch_size"]),
                        int(previous["active"]),
                        new_version,
                        now,
                        recipe_id,
                    ),
                )
                con.execute("DELETE FROM craft_recipe_materials WHERE recipe_id = ?", (recipe_id,))
                con.executemany(
                    "INSERT INTO craft_recipe_materials(recipe_id, material_name, quantity_per_unit, created_at) VALUES(?, ?, ?, ?)",
                    [(recipe_id, str(item[0]), int(item[1]), now) for item in previous["materials"]],
                )
            elif kind in {"recipe_enabled", "recipe_disabled"}:
                recipe_id = int(payload["recipe_id"])
                recipe = con.execute("SELECT * FROM craft_recipes WHERE id = ?", (recipe_id,)).fetchone()
                if recipe is None:
                    con.rollback()
                    raise ValueError("craft_recipe_not_found")
                con.execute(
                    "UPDATE craft_recipes SET active = ?, version = ?, updated_at = ? WHERE id = ?",
                    (int(payload["previous_active"]), int(recipe["version"] or 1) + 1, now, recipe_id),
                )
            elif kind == "plan_created":
                plan_id = int(payload["plan_id"])
                con.execute(
                    "UPDATE craft_plans SET stage = 'cancelled', completed_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, plan_id),
                )
                refresh_plan_id = plan_id
            elif kind == "craft_purchase":
                plan_id = int(payload["plan_id"])
                purchase = con.execute(
                    "SELECT * FROM craft_purchases WHERE id = ? AND undone_at IS NULL",
                    (int(payload["purchase_id"]),),
                ).fetchone()
                if purchase is None:
                    con.rollback()
                    raise ValueError("bot_action_already_undone")
                con.execute(
                    """
                    UPDATE craft_plan_materials
                    SET stock_quantity = MAX(0, stock_quantity - ?),
                        purchased_quantity = MAX(0, purchased_quantity - ?),
                        spent_total = MAX(0, spent_total - ?), updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        int(purchase["quantity"]),
                        int(purchase["quantity"]),
                        int(purchase["total_cost"]),
                        now,
                        int(purchase["plan_material_id"]),
                    ),
                )
                con.execute(
                    "UPDATE craft_purchases SET undone_at = ?, undone_by_id = ?, undo_reason = ? WHERE id = ?",
                    (now, undone_by_id, clean_reason, int(purchase["id"])),
                )
                con.execute(
                    "UPDATE craft_plans SET stage = ?, updated_at = ? WHERE id = ?",
                    (str(payload.get("stage_before") or "procurement"), now, plan_id),
                )
                refresh_plan_id = plan_id
            elif kind == "craft_inventory_check":
                plan_id = int(payload["plan_id"])
                check = con.execute(
                    "SELECT * FROM craft_inventory_checks WHERE id = ? AND undone_at IS NULL",
                    (int(payload["inventory_check_id"]),),
                ).fetchone()
                if check is None:
                    con.rollback()
                    raise ValueError("bot_action_already_undone")
                previous_materials = json.loads(str(check["previous_materials_json"] or "{}"))
                for material_id, quantity in previous_materials.items():
                    con.execute(
                        "UPDATE craft_plan_materials SET stock_quantity = ?, updated_at = ? WHERE id = ? AND plan_id = ?",
                        (int(quantity), now, int(material_id), plan_id),
                    )
                con.execute(
                    """
                    UPDATE craft_plans SET product_stock = ?, stage = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        int(check["previous_product_quantity"] or 0),
                        str(check["previous_stage"] or "procurement"),
                        now,
                        plan_id,
                    ),
                )
                con.execute(
                    "UPDATE craft_inventory_checks SET undone_at = ?, undone_by_id = ?, undo_reason = ? WHERE id = ?",
                    (now, undone_by_id, clean_reason, int(check["id"])),
                )
                refresh_plan_id = plan_id
            elif kind == "craft_batch_started":
                plan_id = int(payload["plan_id"])
                batch = con.execute(
                    "SELECT * FROM craft_batches WHERE id = ? AND undone_at IS NULL",
                    (int(payload["batch_id"]),),
                ).fetchone()
                if batch is None:
                    con.rollback()
                    raise ValueError("bot_action_already_undone")
                quantity = int(batch["quantity"])
                materials = con.execute(
                    "SELECT * FROM craft_plan_materials WHERE plan_id = ?",
                    (plan_id,),
                ).fetchall()
                for material in materials:
                    con.execute(
                        "UPDATE craft_plan_materials SET stock_quantity = stock_quantity + ?, updated_at = ? WHERE id = ?",
                        (int(material["quantity_per_unit"]) * quantity, now, int(material["id"])),
                    )
                was_completed = str(batch["status"]) == "completed"
                plan_row = con.execute("SELECT completion_message_id FROM craft_plans WHERE id = ?", (plan_id,)).fetchone()
                completion_message_id = plan_row["completion_message_id"] if plan_row else None
                con.execute(
                    """
                    UPDATE craft_plans
                    SET attempts_queued = MAX(0, attempts_queued - ?),
                        attempts_completed = MAX(0, attempts_completed - ?),
                        product_stock = MAX(0, product_stock - ?),
                        stage = ?, completed_at = NULL,
                        completion_message_id = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        quantity,
                        quantity if was_completed else 0,
                        quantity if was_completed else 0,
                        str(payload.get("stage_before") or "crafting"),
                        now,
                        plan_id,
                    ),
                )
                con.execute(
                    "UPDATE craft_batches SET status = 'cancelled', undone_at = ?, undone_by_id = ?, undo_reason = ? WHERE id = ?",
                    (now, undone_by_id, clean_reason, int(batch["id"])),
                )
                if batch["finance_event_id"] is not None:
                    finance_undo_event_id = _finance_reverse_event_in_con(
                        con,
                        guild_id=guild_id,
                        event_id=int(batch["finance_event_id"]),
                        undone_by_id=undone_by_id,
                        undone_by_display=undone_by_display,
                        reason=f"Отмена цикла крафта #{plan_id}, партия #{batch['id']}: {clean_reason}",
                        channel_id=channel_id,
                        log_channel_id=int(payload.get("log_channel_id") or log_channel_id),
                        admin_user_id=int(payload.get("admin_user_id") or admin_user_id),
                        now=now,
                    )
                refresh_plan_id = plan_id
            elif kind == "craft_final_output":
                plan_id = int(payload["plan_id"])
                plan = con.execute("SELECT * FROM craft_plans WHERE id = ?", (plan_id,)).fetchone()
                completion_message_id = plan["completion_message_id"] if plan else None
                con.execute(
                    """
                    UPDATE craft_plans
                    SET product_stock = ?, final_product_qty = ?, stage = ?, completed_at = ?,
                        completion_message_id = NULL, updated_at = ? WHERE id = ?
                    """,
                    (
                        int(payload.get("previous_product_stock") or 0),
                        payload.get("previous_final_product_qty"),
                        str(payload.get("previous_stage") or "awaiting_output"),
                        payload.get("previous_completed_at"),
                        now,
                        plan_id,
                    ),
                )
                refresh_plan_id = plan_id
            elif kind == "craft_estimated_price":
                plan_id = int(payload["plan_id"])
                con.execute(
                    "UPDATE craft_plans SET estimated_unit_price = ?, updated_at = ? WHERE id = ?",
                    (payload.get("previous_price"), now, plan_id),
                )
                refresh_plan_id = plan_id
            elif kind == "craft_market_listing":
                plan_id = int(payload["plan_id"])
                listing = con.execute(
                    "SELECT * FROM craft_market_listings WHERE id = ? AND undone_at IS NULL",
                    (int(payload["listing_id"]),),
                ).fetchone()
                if listing is None:
                    con.rollback()
                    raise ValueError("bot_action_already_undone")
                con.execute(
                    "UPDATE craft_market_listings SET undone_at = ?, undone_by_id = ?, undo_reason = ? WHERE id = ?",
                    (now, undone_by_id, clean_reason, int(listing["id"])),
                )
                con.execute(
                    """
                    UPDATE craft_plans SET market_listed_qty = MAX(0, market_listed_qty - ?),
                        stage = ?, updated_at = ? WHERE id = ?
                    """,
                    (int(listing["quantity"]), str(payload.get("previous_stage") or "listing"), now, plan_id),
                )
                refresh_plan_id = plan_id
            elif kind == "craft_sale":
                plan_id = int(payload["plan_id"])
                sale = con.execute(
                    "SELECT * FROM craft_sales WHERE id = ? AND undone_at IS NULL",
                    (int(payload["sale_id"]),),
                ).fetchone()
                if sale is None:
                    con.rollback()
                    raise ValueError("bot_action_already_undone")
                plan = con.execute("SELECT * FROM craft_plans WHERE id = ?", (plan_id,)).fetchone()
                completion_message_id = plan["completion_message_id"] if plan else None
                con.execute(
                    "UPDATE craft_sales SET undone_at = ?, undone_by_id = ?, undo_reason = ? WHERE id = ?",
                    (now, undone_by_id, clean_reason, int(sale["id"])),
                )
                con.execute(
                    """
                    UPDATE craft_plans
                    SET sold_qty = MAX(0, sold_qty - ?), total_revenue = MAX(0, total_revenue - ?),
                        product_stock = product_stock + ?, stage = ?, completed_at = ?,
                        completion_message_id = NULL, updated_at = ? WHERE id = ?
                    """,
                    (
                        int(sale["quantity"]),
                        int(sale["total_amount"]),
                        int(sale["quantity"]),
                        str(payload.get("previous_stage") or "selling"),
                        payload.get("previous_completed_at"),
                        now,
                        plan_id,
                    ),
                )
                refresh_plan_id = plan_id
            elif kind == "generic_row_restore":
                _restore_generic_row(con, payload)
            elif kind == "generic_rows_restore":
                _restore_generic_rows(con, payload)
            else:
                con.rollback()
                raise ValueError("bot_action_unsupported")

            if kind in {"recipe_created", "recipe_updated", "recipe_enabled", "recipe_disabled"}:
                recipe_id = int(payload["recipe_id"])
                restored = _craft_recipe_from_con(con, recipe_id)
                if restored is not None:
                    restored_materials = [
                        [item["material_name"], int(item["quantity_per_unit"])]
                        for item in restored["materials"]
                    ]
                    con.execute(
                        """
                        INSERT OR IGNORE INTO craft_recipe_versions(
                            recipe_id, version, product_name, treasury_cost_per_unit,
                            duration_minutes_per_unit, max_batch_size, materials_json,
                            active, changed_by_id, changed_by_display, change_kind, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'undo_restore', ?)
                        """,
                        (
                            recipe_id,
                            int(restored["version"]),
                            restored["product_name"],
                            int(restored["treasury_cost_per_unit"]),
                            int(restored["duration_minutes_per_unit"]),
                            int(restored["max_batch_size"]),
                            json.dumps(restored_materials, ensure_ascii=False),
                            int(restored["active"]),
                            undone_by_id,
                            undone_by_display,
                            now,
                        ),
                    )

            if refresh_plan_id is not None:
                _craft_add_event(
                    con,
                    plan_id=refresh_plan_id,
                    guild_id=guild_id,
                    event_kind="action_undone",
                    actor_id=undone_by_id,
                    actor_display=undone_by_display,
                    details={
                        "action_id": int(action["id"]),
                        "original_action": kind,
                        "reason": clean_reason,
                    },
                    now=now,
                )
            con.execute(
                """
                UPDATE bot_actions
                SET status = 'undone', undone_by_id = ?, undone_by_display = ?,
                    undone_at = ?, undo_reason = ?
                WHERE id = ? AND status = 'active'
                """,
                (undone_by_id, undone_by_display, now, clean_reason, int(action["id"])),
            )
            con.commit()
        return {
            "action": bot_get_action(int(action["id"]), guild_id),
            "module": str(action["module"]),
            "refresh_plan_id": refresh_plan_id,
            "completion_message_id": completion_message_id,
            "finance_undo_event_id": finance_undo_event_id,
        }


def _market_optional_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _market_row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    try:
        metadata = json.loads(str(result.get("metadata_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    result["metadata"] = metadata if isinstance(metadata, dict) else {}
    result["external_id"] = str(result.get("external_id") or result.get("item_id") or "")
    return result


def market_replace_snapshot(
    *,
    server_id: str,
    category: str,
    server_name: str,
    source_updated_at: str,
    period_days: int | None,
    items: Iterable[dict[str, Any]],
    fetched_at: str | None = None,
    history_retention_days: int = 400,
) -> dict[str, Any]:
    """Atomically replace the current catalog and preserve one history row per source snapshot."""
    clean_server_id = str(server_id or "").strip().upper()
    clean_category = str(category or "").strip().lower()
    clean_source_updated_at = str(source_updated_at or "").strip()
    if not clean_server_id or not clean_category or not clean_source_updated_at:
        raise ValueError("market_snapshot_scope_required")
    now = str(fetched_at or utc_now_iso())
    prepared: list[tuple[Any, ...]] = []
    seen_ids: set[int] = set()
    seen_external_ids: set[str] = set()
    for raw in items:
        try:
            item_id = int(raw.get("item_id"))
        except (TypeError, ValueError):
            continue
        item_name = str(raw.get("item_name") or "").strip()
        external_id = str(raw.get("external_id") or item_id).strip()
        if item_id < 0 or not item_name or not external_id:
            continue
        if item_id in seen_ids or external_id in seen_external_ids:
            raise ValueError("market_snapshot_duplicate_key")
        seen_ids.add(item_id)
        seen_external_ids.add(external_id)
        normalized_name = str(raw.get("normalized_name") or item_name.casefold()).strip()
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        prepared.append(
            (
                clean_server_id,
                clean_category,
                item_id,
                external_id,
                item_name,
                normalized_name,
                metadata_json,
                _market_optional_non_negative_int(raw.get("total_count")) or 0,
                _market_optional_non_negative_int(raw.get("sold_count")) or 0,
                _market_optional_non_negative_int(raw.get("average_price")),
                _market_optional_non_negative_int(raw.get("min_price")),
                _market_optional_non_negative_int(raw.get("max_price")),
                clean_source_updated_at,
                now,
                now,
                now,
            )
        )
    if not prepared:
        raise ValueError("market_snapshot_empty")

    with _db_lock, connect() as con:
        con.execute(
            "UPDATE market_items SET active = 0, updated_at = ? WHERE server_id = ? AND category = ?",
            (now, clean_server_id, clean_category),
        )
        con.executemany(
            """
            INSERT INTO market_items(
                server_id, category, item_id, external_id, item_name, normalized_name, metadata_json,
                total_count, sold_count, average_price, min_price, max_price,
                source_updated_at, fetched_at, active, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(server_id, category, item_id) DO UPDATE SET
                external_id = excluded.external_id,
                item_name = excluded.item_name,
                normalized_name = excluded.normalized_name,
                metadata_json = excluded.metadata_json,
                total_count = excluded.total_count,
                sold_count = excluded.sold_count,
                average_price = excluded.average_price,
                min_price = excluded.min_price,
                max_price = excluded.max_price,
                source_updated_at = excluded.source_updated_at,
                fetched_at = excluded.fetched_at,
                active = 1,
                updated_at = excluded.updated_at
            """,
            prepared,
        )
        con.executemany(
            """
            INSERT INTO market_item_history(
                server_id, category, item_id, external_id, item_name, metadata_json, total_count, sold_count,
                average_price, min_price, max_price, source_updated_at, fetched_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, category, item_id, source_updated_at) DO UPDATE SET
                external_id = excluded.external_id,
                item_name = excluded.item_name,
                metadata_json = excluded.metadata_json,
                total_count = excluded.total_count,
                sold_count = excluded.sold_count,
                average_price = excluded.average_price,
                min_price = excluded.min_price,
                max_price = excluded.max_price,
                fetched_at = excluded.fetched_at
            """,
            [
                (
                    row[0], row[1], row[2], row[3], row[4], row[6], row[7], row[8],
                    row[9], row[10], row[11], row[12], row[13],
                )
                for row in prepared
            ],
        )
        con.execute(
            """
            INSERT INTO market_catalogs(
                server_id, category, server_name, record_count, source_updated_at,
                period_days, last_attempt_at, last_success_at, last_error,
                consecutive_failures, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)
            ON CONFLICT(server_id, category) DO UPDATE SET
                server_name = excluded.server_name,
                record_count = excluded.record_count,
                source_updated_at = excluded.source_updated_at,
                period_days = excluded.period_days,
                last_attempt_at = excluded.last_attempt_at,
                last_success_at = excluded.last_success_at,
                last_error = NULL,
                consecutive_failures = 0,
                updated_at = excluded.updated_at
            """,
            (
                clean_server_id,
                clean_category,
                str(server_name or "").strip(),
                len(prepared),
                clean_source_updated_at,
                _market_optional_non_negative_int(period_days),
                now,
                now,
                now,
                now,
            ),
        )
        if history_retention_days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=int(history_retention_days))).isoformat()
            con.execute("DELETE FROM market_item_history WHERE fetched_at < ?", (cutoff,))
            con.execute(
                """
                DELETE FROM market_alert_notifications
                WHERE status IN ('delivered', 'failed', 'cancelled') AND created_at < ?
                """,
                (cutoff,),
            )
    return market_catalog_status(clean_server_id, clean_category)


def market_record_sync_error(server_id: str, category: str, error: str) -> dict[str, Any]:
    clean_server_id = str(server_id or "").strip().upper()
    clean_category = str(category or "").strip().lower()
    if not clean_server_id or not clean_category:
        raise ValueError("market_sync_scope_required")
    now = utc_now_iso()
    clean_error = " ".join(str(error or "unknown").split())[:500]
    with _db_lock, connect() as con:
        con.execute(
            """
            INSERT INTO market_catalogs(
                server_id, category, server_name, record_count, last_attempt_at,
                last_error, consecutive_failures, created_at, updated_at
            ) VALUES(?, ?, '', 0, ?, ?, 1, ?, ?)
            ON CONFLICT(server_id, category) DO UPDATE SET
                last_attempt_at = excluded.last_attempt_at,
                last_error = excluded.last_error,
                consecutive_failures = market_catalogs.consecutive_failures + 1,
                updated_at = excluded.updated_at
            """,
            (clean_server_id, clean_category, now, clean_error, now, now),
        )
    return market_catalog_status(clean_server_id, clean_category)


def market_catalog_status(server_id: str, category: str = "items") -> dict[str, Any]:
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM market_catalogs WHERE server_id = ? AND category = ?",
            (str(server_id).strip().upper(), str(category).strip().lower()),
        ).fetchone()
    return dict(row) if row is not None else {}


def market_list_items(
    server_id: str,
    category: str = "items",
    *,
    active_only: bool = True,
    limit: int = 10000,
) -> list[dict[str, Any]]:
    clauses = ["server_id = ?", "category = ?"]
    params: list[Any] = [str(server_id).strip().upper(), str(category).strip().lower()]
    if active_only:
        clauses.append("active = 1")
    params.append(max(1, min(int(limit), 10000)))
    with _db_lock, connect() as con:
        rows = con.execute(
            f"SELECT * FROM market_items WHERE {' AND '.join(clauses)} ORDER BY item_name COLLATE NOCASE LIMIT ?",
            params,
        ).fetchall()
    return [parsed for row in rows if (parsed := _market_row_dict(row)) is not None]


def market_get_item(server_id: str, item_id: int, category: str = "items") -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT * FROM market_items
            WHERE server_id = ? AND category = ? AND item_id = ? AND active = 1
            """,
            (str(server_id).strip().upper(), str(category).strip().lower(), int(item_id)),
        ).fetchone()
    return _market_row_dict(row)


def market_popular_items(server_id: str, category: str = "items", limit: int = 10) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM market_items
            WHERE server_id = ? AND category = ? AND active = 1
            ORDER BY sold_count DESC, total_count DESC, item_name COLLATE NOCASE
            LIMIT ?
            """,
            (
                str(server_id).strip().upper(),
                str(category).strip().lower(),
                max(1, min(int(limit), 25)),
            ),
        ).fetchall()
    return [parsed for row in rows if (parsed := _market_row_dict(row)) is not None]


def market_item_history(
    server_id: str,
    item_id: int,
    category: str = "items",
    limit: int = 30,
) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM market_item_history
            WHERE server_id = ? AND category = ? AND item_id = ?
            ORDER BY source_updated_at DESC
            LIMIT ?
            """,
            (
                str(server_id).strip().upper(),
                str(category).strip().lower(),
                int(item_id),
                max(1, min(int(limit), 365)),
            ),
        ).fetchall()
    return [parsed for row in rows if (parsed := _market_row_dict(row)) is not None]


def market_upsert_alert(
    *,
    discord_user_id: int,
    user_display: str | None,
    guild_id: int | None,
    server_id: str,
    category: str,
    item_id: int,
    target_price: int,
    min_quantity: int,
    current_source_updated_at: str | None,
    max_alerts_per_user: int = 20,
) -> dict[str, Any]:
    """Create or replace one personal, one-shot alert for an item."""
    clean_user_id = int(discord_user_id)
    clean_item_id = int(item_id)
    clean_target_price = int(target_price)
    clean_min_quantity = int(min_quantity)
    clean_server_id = str(server_id or "").strip().upper()
    clean_category = str(category or "").strip().lower()
    if clean_user_id <= 0 or clean_item_id < 0 or not clean_server_id or not clean_category:
        raise ValueError("market_alert_scope_required")
    if clean_target_price <= 0 or clean_min_quantity <= 0:
        raise ValueError("market_alert_thresholds_must_be_positive")
    if clean_target_price > 10**15 or clean_min_quantity > 10**15:
        raise ValueError("market_alert_thresholds_too_large")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        item = con.execute(
            """
            SELECT item_id FROM market_items
            WHERE server_id = ? AND category = ? AND item_id = ? AND active = 1
            """,
            (clean_server_id, clean_category, clean_item_id),
        ).fetchone()
        if item is None:
            raise ValueError("market_alert_item_not_found")
        existing = con.execute(
            """
            SELECT id FROM market_alerts
            WHERE discord_user_id = ? AND server_id = ? AND category = ? AND item_id = ?
            """,
            (clean_user_id, clean_server_id, clean_category, clean_item_id),
        ).fetchone()
        if existing is None:
            count = con.execute(
                "SELECT COUNT(*) AS amount FROM market_alerts WHERE discord_user_id = ?",
                (clean_user_id,),
            ).fetchone()
            if int(count["amount"] or 0) >= max(1, int(max_alerts_per_user)):
                raise ValueError("market_alert_limit_reached")
        con.execute(
            """
            INSERT INTO market_alerts(
                discord_user_id, user_display, guild_id, server_id, category, item_id,
                target_price, min_quantity, status, last_evaluated_source_at,
                triggered_at, last_delivery_error, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, NULL, ?, ?)
            ON CONFLICT(discord_user_id, server_id, category, item_id) DO UPDATE SET
                user_display = excluded.user_display,
                guild_id = excluded.guild_id,
                target_price = excluded.target_price,
                min_quantity = excluded.min_quantity,
                status = 'active',
                last_evaluated_source_at = excluded.last_evaluated_source_at,
                triggered_at = NULL,
                last_delivery_error = NULL,
                updated_at = excluded.updated_at
            """,
            (
                clean_user_id,
                str(user_display or "").strip()[:200] or None,
                int(guild_id) if guild_id else None,
                clean_server_id,
                clean_category,
                clean_item_id,
                clean_target_price,
                clean_min_quantity,
                str(current_source_updated_at or "").strip() or None,
                now,
                now,
            ),
        )
        con.execute(
            """
            UPDATE market_alert_notifications
            SET status = 'cancelled', updated_at = ?
            WHERE alert_id = (
                SELECT id FROM market_alerts
                WHERE discord_user_id = ? AND server_id = ? AND category = ? AND item_id = ?
            ) AND status IN ('pending', 'retry')
            """,
            (now, clean_user_id, clean_server_id, clean_category, clean_item_id),
        )
        row = con.execute(
            """
            SELECT a.*, i.external_id, i.item_name, i.metadata_json, i.average_price,
                   i.min_price, i.total_count, i.sold_count,
                   i.source_updated_at AS item_source_updated_at
            FROM market_alerts a
            JOIN market_items i
              ON i.server_id = a.server_id AND i.category = a.category AND i.item_id = a.item_id
            WHERE a.discord_user_id = ? AND a.server_id = ? AND a.category = ? AND a.item_id = ?
            """,
            (clean_user_id, clean_server_id, clean_category, clean_item_id),
        ).fetchone()
    parsed = _market_row_dict(row)
    if parsed is None:
        raise RuntimeError("market_alert_not_saved")
    return parsed


def market_get_alert(
    discord_user_id: int,
    server_id: str,
    item_id: int,
    category: str = "items",
) -> dict[str, Any] | None:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT a.*, i.external_id, i.item_name, i.metadata_json, i.average_price,
                   i.min_price, i.total_count, i.sold_count,
                   i.source_updated_at AS item_source_updated_at
            FROM market_alerts a
            LEFT JOIN market_items i
              ON i.server_id = a.server_id AND i.category = a.category AND i.item_id = a.item_id
            WHERE a.discord_user_id = ? AND a.server_id = ? AND a.category = ? AND a.item_id = ?
            """,
            (
                int(discord_user_id),
                str(server_id).strip().upper(),
                str(category).strip().lower(),
                int(item_id),
            ),
        ).fetchone()
    return _market_row_dict(row)


def market_list_user_alerts(discord_user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT a.*, i.external_id, i.item_name, i.metadata_json, i.average_price,
                   i.min_price, i.total_count, i.sold_count,
                   i.source_updated_at AS item_source_updated_at
            FROM market_alerts a
            LEFT JOIN market_items i
              ON i.server_id = a.server_id AND i.category = a.category AND i.item_id = a.item_id
            WHERE a.discord_user_id = ?
            ORDER BY
                CASE a.status WHEN 'active' THEN 0 WHEN 'notifying' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END,
                a.updated_at DESC
            LIMIT ?
            """,
            (int(discord_user_id), max(1, min(int(limit), 25))),
        ).fetchall()
    return [parsed for row in rows if (parsed := _market_row_dict(row)) is not None]


def market_alert_stats(server_id: str, category: str = "items") -> dict[str, int]:
    with _db_lock, connect() as con:
        row = con.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) AS active,
                COALESCE(SUM(CASE WHEN status = 'notifying' THEN 1 ELSE 0 END), 0) AS notifying,
                COALESCE(SUM(CASE WHEN status = 'triggered' THEN 1 ELSE 0 END), 0) AS triggered,
                COALESCE(SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END), 0) AS paused
            FROM market_alerts
            WHERE server_id = ? AND category = ?
            """,
            (str(server_id).strip().upper(), str(category).strip().lower()),
        ).fetchone()
        pending = con.execute(
            """
            SELECT COUNT(*) AS amount
            FROM market_alert_notifications n
            JOIN market_alerts a ON a.id = n.alert_id
            WHERE a.server_id = ? AND a.category = ? AND n.status IN ('pending', 'retry')
            """,
            (str(server_id).strip().upper(), str(category).strip().lower()),
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "active": int(row["active"] or 0),
        "notifying": int(row["notifying"] or 0),
        "triggered": int(row["triggered"] or 0),
        "paused": int(row["paused"] or 0),
        "pending_notifications": int(pending["amount"] or 0),
    }


def market_set_alert_status(
    discord_user_id: int,
    alert_id: int,
    status: str,
    *,
    current_source_updated_at: str | None = None,
) -> dict[str, Any] | None:
    clean_status = str(status or "").strip().lower()
    if clean_status not in {"active", "paused"}:
        raise ValueError("market_alert_status_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        if clean_status == "active":
            con.execute(
                """
                UPDATE market_alerts
                SET status = 'active', last_evaluated_source_at = ?, triggered_at = NULL,
                    last_delivery_error = NULL, updated_at = ?
                WHERE id = ? AND discord_user_id = ?
                """,
                (
                    str(current_source_updated_at or "").strip() or None,
                    now,
                    int(alert_id),
                    int(discord_user_id),
                ),
            )
        else:
            con.execute(
                """
                UPDATE market_alerts SET status = 'paused', updated_at = ?
                WHERE id = ? AND discord_user_id = ?
                """,
                (now, int(alert_id), int(discord_user_id)),
            )
            con.execute(
                """
                UPDATE market_alert_notifications
                SET status = 'cancelled', updated_at = ?
                WHERE alert_id = ? AND status IN ('pending', 'retry')
                """,
                (now, int(alert_id)),
            )
        row = con.execute(
            "SELECT * FROM market_alerts WHERE id = ? AND discord_user_id = ?",
            (int(alert_id), int(discord_user_id)),
        ).fetchone()
    return dict(row) if row is not None else None


def market_delete_alert(discord_user_id: int, alert_id: int) -> bool:
    with _db_lock, connect() as con:
        cursor = con.execute(
            "DELETE FROM market_alerts WHERE id = ? AND discord_user_id = ?",
            (int(alert_id), int(discord_user_id)),
        )
    return cursor.rowcount > 0


def market_evaluate_alerts(
    server_id: str,
    source_updated_at: str,
    category: str = "items",
) -> int:
    """Evaluate active alerts once per source snapshot and enqueue matching DMs."""
    clean_server_id = str(server_id or "").strip().upper()
    clean_category = str(category or "").strip().lower()
    clean_source = str(source_updated_at or "").strip()
    if not clean_server_id or not clean_category or not clean_source:
        return 0
    now = utc_now_iso()
    enqueued = 0
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT a.*, i.item_name, i.total_count, i.average_price, i.min_price
            FROM market_alerts a
            JOIN market_items i
              ON i.server_id = a.server_id AND i.category = a.category AND i.item_id = a.item_id
            WHERE a.server_id = ? AND a.category = ? AND a.status = 'active'
              AND i.active = 1 AND i.source_updated_at = ?
              AND COALESCE(a.last_evaluated_source_at, '') <> ?
            """,
            (clean_server_id, clean_category, clean_source, clean_source),
        ).fetchall()
        for row in rows:
            minimum = _market_optional_non_negative_int(row["min_price"])
            average = _market_optional_non_negative_int(row["average_price"])
            observed_price = minimum if minimum and minimum > 0 else average if average and average > 0 else None
            observed_quantity = _market_optional_non_negative_int(row["total_count"]) or 0
            matches = (
                observed_price is not None
                and observed_price <= int(row["target_price"])
                and observed_quantity >= int(row["min_quantity"])
            )
            con.execute(
                """
                UPDATE market_alerts
                SET last_evaluated_source_at = ?, last_observed_price = ?,
                    last_observed_quantity = ?, updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (clean_source, observed_price, observed_quantity, now, int(row["id"])),
            )
            if not matches:
                continue
            cursor = con.execute(
                """
                INSERT OR IGNORE INTO market_alert_notifications(
                    alert_id, source_updated_at, item_name, observed_price, observed_quantity,
                    status, attempt_count, next_attempt_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    int(row["id"]),
                    clean_source,
                    str(row["item_name"] or f"Предмет #{int(row['item_id'])}"),
                    int(observed_price),
                    observed_quantity,
                    now,
                    now,
                    now,
                ),
            )
            if cursor.rowcount > 0:
                con.execute(
                    "UPDATE market_alerts SET status = 'notifying', updated_at = ? WHERE id = ?",
                    (now, int(row["id"])),
                )
                enqueued += 1
    return enqueued


def market_pending_alert_notifications(limit: int = 25) -> list[dict[str, Any]]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT n.*, a.discord_user_id, a.server_id, a.category, a.item_id,
                   a.target_price, a.min_quantity, i.external_id, i.metadata_json
            FROM market_alert_notifications n
            JOIN market_alerts a ON a.id = n.alert_id
            LEFT JOIN market_items i
              ON i.server_id = a.server_id AND i.category = a.category AND i.item_id = a.item_id
            WHERE n.status IN ('pending', 'retry') AND n.next_attempt_at <= ?
              AND a.status = 'notifying'
            ORDER BY n.next_attempt_at, n.id
            LIMIT ?
            """,
            (now, max(1, min(int(limit), 100))),
        ).fetchall()
    return [parsed for row in rows if (parsed := _market_row_dict(row)) is not None]


def market_mark_alert_delivery(
    notification_id: int,
    *,
    delivered: bool,
    dm_message_id: int | None = None,
    error: str | None = None,
    max_attempts: int = 3,
    retry_seconds: int = 600,
) -> dict[str, Any] | None:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    clean_error = " ".join(str(error or "delivery_failed").split())[:500]
    with _db_lock, connect() as con:
        row = con.execute(
            "SELECT * FROM market_alert_notifications WHERE id = ?",
            (int(notification_id),),
        ).fetchone()
        if row is None:
            return None
        if str(row["status"]) not in {"pending", "retry"}:
            return dict(row)
        alert_id = int(row["alert_id"])
        if delivered:
            con.execute(
                """
                UPDATE market_alert_notifications
                SET status = 'delivered', attempt_count = attempt_count + 1,
                    dm_message_id = ?, last_error = NULL, delivered_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(dm_message_id) if dm_message_id else None, now, now, int(notification_id)),
            )
            con.execute(
                """
                UPDATE market_alerts
                SET status = 'triggered', triggered_at = ?, last_delivery_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, now, alert_id),
            )
        else:
            attempt_count = int(row["attempt_count"] or 0) + 1
            permanently_failed = attempt_count >= max(1, int(max_attempts))
            next_attempt = (now_dt + timedelta(seconds=max(60, int(retry_seconds)))).isoformat()
            con.execute(
                """
                UPDATE market_alert_notifications
                SET status = ?, attempt_count = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    "failed" if permanently_failed else "retry",
                    attempt_count,
                    next_attempt,
                    clean_error,
                    now,
                    int(notification_id),
                ),
            )
            con.execute(
                """
                UPDATE market_alerts
                SET status = ?, last_delivery_error = ?, updated_at = ?
                WHERE id = ?
                """,
                ("paused" if permanently_failed else "notifying", clean_error, now, alert_id),
            )
        result = con.execute(
            "SELECT * FROM market_alert_notifications WHERE id = ?",
            (int(notification_id),),
        ).fetchone()
    return dict(result) if result is not None else None
