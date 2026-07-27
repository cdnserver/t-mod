from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from persistence import core as _core
from persistence.core import (
    _add_column_if_missing,
    _db_lock,
    connect,
    utc_now_iso,
)
from persistence.activity_repository import set_meta

def _backup_before_consensus_reset() -> Path | None:
    """Create a consistent SQLite backup before the one-time destructive reset."""
    if not _core.DATABASE_FILE.exists() or _core.DATABASE_FILE.stat().st_size == 0:
        return None
    source = sqlite3.connect(_core.DATABASE_FILE, timeout=30)
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
            (_consensus_reset_meta_key(_core.CONSENSUS_V2_RESET_ID),),
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
        backup_dir = _core.DATA_DIR / "backups"
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

    if not _core.DATABASE_FILE.exists() or _core.DATABASE_FILE.stat().st_size == 0:
        return None
    source = sqlite3.connect(_core.DATABASE_FILE, timeout=30)
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
            (_consensus_reset_meta_key(_core.CONSENSUS_V2_RESET_ID),),
        ).fetchone()
        if reset_marker is None:
            return None
        marker = source.execute(
            "SELECT 1 FROM meta WHERE key = ?",
            (_consensus_result_dedup_meta_key(_core.CONSENSUS_RESULT_DEDUP_ID),),
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
        backup_dir = _core.DATA_DIR / "backups"
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

            CREATE TABLE IF NOT EXISTS member_profiles (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'busy', 'away', 'vacation')),
                status_note TEXT,
                visibility TEXT NOT NULL DEFAULT 'members'
                    CHECK(visibility IN ('members', 'private')),
                show_activity INTEGER NOT NULL DEFAULT 1,
                show_availability INTEGER NOT NULL DEFAULT 1,
                show_position INTEGER NOT NULL DEFAULT 1,
                show_characters INTEGER NOT NULL DEFAULT 1,
                show_join_date INTEGER NOT NULL DEFAULT 1,
                show_directory INTEGER NOT NULL DEFAULT 1,
                theme TEXT NOT NULL DEFAULT 'indigo'
                    CHECK(theme IN ('indigo', 'emerald', 'gold', 'rose')),
                primary_character_id INTEGER,
                dm_notifications INTEGER NOT NULL DEFAULT 1,
                dm_market INTEGER NOT NULL DEFAULT 1,
                dm_craft INTEGER NOT NULL DEFAULT 1,
                dm_consensus INTEGER NOT NULL DEFAULT 1,
                dm_finance INTEGER NOT NULL DEFAULT 1,
                dm_system INTEGER NOT NULL DEFAULT 1,
                quiet_hours_enabled INTEGER NOT NULL DEFAULT 0,
                quiet_start_minute INTEGER NOT NULL DEFAULT 0,
                quiet_end_minute INTEGER NOT NULL DEFAULT 480,
                biography TEXT,
                contribution TEXT,
                responsibilities TEXT,
                membership_since TEXT,
                directory_completed_at TEXT,
                directory_required INTEGER NOT NULL DEFAULT 0,
                onboarding_prompted_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS profile_characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                nickname TEXT NOT NULL,
                static_id TEXT NOT NULL,
                position INTEGER NOT NULL CHECK(position BETWEEN 1 AND 3),
                is_public INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (guild_id, static_id),
                UNIQUE (guild_id, user_id, position),
                FOREIGN KEY (guild_id, user_id)
                    REFERENCES member_profiles(guild_id, user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_profile_characters_owner
            ON profile_characters(guild_id, user_id, position);

            CREATE TABLE IF NOT EXISTS voice_user_profiles (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                input_gain REAL NOT NULL DEFAULT 1.0,
                noise_floor INTEGER NOT NULL DEFAULT 0,
                speech_rms INTEGER NOT NULL DEFAULT 0,
                speech_peak INTEGER NOT NULL DEFAULT 0,
                snr_db REAL NOT NULL DEFAULT 0,
                clipping_percent REAL NOT NULL DEFAULT 0,
                quality_score INTEGER NOT NULL DEFAULT 0,
                commands_total INTEGER NOT NULL DEFAULT 0,
                failures_total INTEGER NOT NULL DEFAULT 0,
                latency_samples INTEGER NOT NULL DEFAULT 0,
                latency_total_ms INTEGER NOT NULL DEFAULT 0,
                last_engine TEXT,
                calibrated_at TEXT,
                last_diagnostic_at TEXT,
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

            CREATE TABLE IF NOT EXISTS sgl_case_archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                case_id INTEGER,
                case_number INTEGER NOT NULL,
                original_channel_id INTEGER NOT NULL,
                original_channel_name TEXT NOT NULL,
                original_topic TEXT,
                original_category_id INTEGER,
                status TEXT NOT NULL DEFAULT 'capturing',
                message_count INTEGER NOT NULL DEFAULT 0,
                attachment_count INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                snapshot_started_at TEXT NOT NULL,
                snapshot_completed_at TEXT,
                source_deleted_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(guild_id, case_number),
                FOREIGN KEY (case_id) REFERENCES sgl_cases(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS sgl_case_archive_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                archive_id INTEGER NOT NULL,
                original_message_id INTEGER NOT NULL,
                container_id INTEGER NOT NULL,
                container_type TEXT NOT NULL DEFAULT 'channel',
                container_name TEXT NOT NULL DEFAULT '',
                author_id INTEGER,
                author_name TEXT NOT NULL DEFAULT '',
                author_display TEXT NOT NULL DEFAULT '',
                author_avatar_url TEXT,
                author_is_bot INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL DEFAULT '',
                embeds_json TEXT NOT NULL DEFAULT '[]',
                attachments_json TEXT NOT NULL DEFAULT '[]',
                stickers_json TEXT NOT NULL DEFAULT '[]',
                reactions_json TEXT NOT NULL DEFAULT '[]',
                components_json TEXT NOT NULL DEFAULT '[]',
                reference_message_id INTEGER,
                created_at TEXT NOT NULL,
                edited_at TEXT,
                pinned INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL,
                UNIQUE(archive_id, original_message_id),
                FOREIGN KEY (archive_id) REFERENCES sgl_case_archives(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS sgl_case_archive_restorations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                archive_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                restored_channel_id INTEGER NOT NULL,
                restored_by_id INTEGER NOT NULL,
                restored_by_display TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'restoring',
                restored_at TEXT NOT NULL,
                completed_at TEXT,
                expires_at TEXT NOT NULL,
                deleted_at TEXT,
                last_error TEXT,
                FOREIGN KEY (archive_id) REFERENCES sgl_case_archives(id) ON DELETE CASCADE
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
                decision_category TEXT NOT NULL DEFAULT 'ordinary',
                implementation_plan TEXT,
                leadership_actions TEXT,
                editor_workspace_id INTEGER,
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
                source_channel_id INTEGER,
                source_message_id INTEGER,
                decision_category TEXT NOT NULL DEFAULT 'ordinary',
                required_percent REAL NOT NULL DEFAULT 50,
                opposed_percent REAL NOT NULL DEFAULT 0,
                block_votes_json TEXT,
                veto_by_id INTEGER,
                veto_by_display TEXT,
                resolution_method TEXT NOT NULL DEFAULT 'vote',
                resolution_note TEXT,
                resolved_by_id INTEGER,
                resolved_by_display TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tvrs_bill_workspaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                author_display TEXT,
                parent_channel_id INTEGER,
                thread_id INTEGER,
                panel_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'draft',
                idea TEXT,
                desired_outcome TEXT,
                constraints_text TEXT,
                title TEXT,
                summary TEXT,
                materials TEXT,
                decision_category TEXT NOT NULL DEFAULT 'ordinary',
                implementation_plan TEXT,
                leadership_actions TEXT,
                ai_model TEXT,
                ai_revision INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1,
                submitted_bill_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tvrs_bill_workspaces_author
            ON tvrs_bill_workspaces(guild_id, author_id, status, updated_at DESC);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_tvrs_bill_workspaces_one_open
            ON tvrs_bill_workspaces(guild_id, author_id)
            WHERE status IN ('draft', 'review');

            CREATE TABLE IF NOT EXISTS admin_broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                author_display TEXT,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                link_url TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                recipient_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                queued_at TEXT,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_admin_broadcasts_guild
            ON admin_broadcasts(guild_id, id DESC);

            CREATE TABLE IF NOT EXISTS admin_broadcast_recipients (
                broadcast_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_display TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                reason TEXT,
                outbox_id INTEGER,
                dm_message_id INTEGER,
                available_at TEXT,
                delivered_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (broadcast_id, user_id),
                FOREIGN KEY (broadcast_id) REFERENCES admin_broadcasts(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_admin_broadcast_recipients_status
            ON admin_broadcast_recipients(broadcast_id, status);

            CREATE TABLE IF NOT EXISTS tvrs_consensus_sessions (
                session_key TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                engine_version INTEGER NOT NULL DEFAULT 2,
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
                priority INTEGER NOT NULL DEFAULT 0,
                supersede_key TEXT,
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
        for column, definition in {
            "visibility": "TEXT NOT NULL DEFAULT 'members'",
            "show_activity": "INTEGER NOT NULL DEFAULT 1",
            "show_availability": "INTEGER NOT NULL DEFAULT 1",
            "show_position": "INTEGER NOT NULL DEFAULT 1",
            "show_characters": "INTEGER NOT NULL DEFAULT 1",
            "show_join_date": "INTEGER NOT NULL DEFAULT 1",
            "theme": "TEXT NOT NULL DEFAULT 'indigo'",
            "primary_character_id": "INTEGER",
            "dm_notifications": "INTEGER NOT NULL DEFAULT 1",
            "dm_market": "INTEGER NOT NULL DEFAULT 1",
            "dm_craft": "INTEGER NOT NULL DEFAULT 1",
            "dm_consensus": "INTEGER NOT NULL DEFAULT 1",
            "dm_finance": "INTEGER NOT NULL DEFAULT 1",
            "dm_system": "INTEGER NOT NULL DEFAULT 1",
            "quiet_hours_enabled": "INTEGER NOT NULL DEFAULT 0",
            "quiet_start_minute": "INTEGER NOT NULL DEFAULT 0",
            "quiet_end_minute": "INTEGER NOT NULL DEFAULT 480",
        }.items():
            _add_column_if_missing(con, "member_profiles", column, definition)

        _add_column_if_missing(
            con,
            "tvrs_bill_workspaces",
            "revision",
            "INTEGER NOT NULL DEFAULT 1",
        )
        _add_column_if_missing(
            con,
            "tvrs_bill_workspaces",
            "panel_message_id",
            "INTEGER",
        )
        _add_column_if_missing(
            con,
            "profile_characters",
            "is_public",
            "INTEGER NOT NULL DEFAULT 1",
        )
        _add_column_if_missing(
            con,
            "tvrs_consensus_sessions",
            "engine_version",
            "INTEGER NOT NULL DEFAULT 2",
        )
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
            "decision_category": "TEXT NOT NULL DEFAULT 'ordinary'",
            "implementation_plan": "TEXT",
            "leadership_actions": "TEXT",
            "editor_workspace_id": "INTEGER",
        }.items():
            _add_column_if_missing(con, "tvrs_bills", column, definition)
        con.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tvrs_bills_editor_workspace
            ON tvrs_bills(editor_workspace_id)
            WHERE editor_workspace_id IS NOT NULL
            """
        )

        for column, definition in {
            "voter_display": "TEXT",
        }.items():
            _add_column_if_missing(con, "tvrs_votes", column, definition)

        for column, definition in {
            "resolution_method": "TEXT NOT NULL DEFAULT 'vote'",
            "resolution_note": "TEXT",
            "resolved_by_id": "INTEGER",
            "resolved_by_display": "TEXT",
            "source_channel_id": "INTEGER",
            "source_message_id": "INTEGER",
            "decision_category": "TEXT NOT NULL DEFAULT 'ordinary'",
            "required_percent": "REAL NOT NULL DEFAULT 50",
            "opposed_percent": "REAL NOT NULL DEFAULT 0",
            "block_votes_json": "TEXT",
        }.items():
            _add_column_if_missing(con, "tvrs_live_results", column, definition)
        con.execute(
            "UPDATE tvrs_live_results SET resolution_method = 'veto' "
            "WHERE status = 'vetoed' AND resolution_method = 'vote'"
        )

        for column, definition in {
            "biography": "TEXT",
            "contribution": "TEXT",
            "responsibilities": "TEXT",
            "membership_since": "TEXT",
            "directory_completed_at": "TEXT",
            "directory_required": "INTEGER NOT NULL DEFAULT 0",
            "onboarding_prompted_at": "TEXT",
            "show_directory": "INTEGER NOT NULL DEFAULT 1",
        }.items():
            _add_column_if_missing(con, "member_profiles", column, definition)


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
        _add_column_if_missing(
            con,
            "delivery_outbox",
            "priority",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _add_column_if_missing(con, "delivery_outbox", "supersede_key", "TEXT")
        control_cleanup_migration = "migration:delivery-control-cleanup:2026-07-20-v2"
        control_cleanup_applied = con.execute(
            "SELECT 1 FROM meta WHERE key = ?",
            (control_cleanup_migration,),
        ).fetchone()
        if control_cleanup_applied is None:
            migration_now = utc_now_iso()
            # The first priority migration could revive several generations of
            # the same registration invitation. Stop the entire legacy control
            # backlog. Recovery later reconstructs at most one missing current
            # panel from the durable consensus snapshot.
            con.execute(
                """
                UPDATE delivery_outbox
                SET status = 'cancelled', priority = 100,
                    lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                    last_error = 'superseded_by_control_delivery_v2',
                    payload_json = '{"compacted":true,"reason":"control_delivery_superseded"}',
                    updated_at = ?
                WHERE topic = 'tvrs.consensus.control-dm.v1'
                  AND status IN ('pending', 'retry', 'processing', 'dead')
                """,
                (migration_now,),
            )
            set_meta(con, control_cleanup_migration, migration_now)

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

            CREATE INDEX IF NOT EXISTS idx_sgl_case_archives_source
            ON sgl_case_archives(guild_id, original_channel_id);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_archives_status
            ON sgl_case_archives(guild_id, status, source_deleted_at);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_archive_messages_order
            ON sgl_case_archive_messages(archive_id, position ASC);

            CREATE INDEX IF NOT EXISTS idx_sgl_archive_restorations_active
            ON sgl_case_archive_restorations(guild_id, status, expires_at);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_sgl_archive_one_active_restoration
            ON sgl_case_archive_restorations(archive_id)
            WHERE deleted_at IS NULL AND status IN ('restoring', 'complete');

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

            CREATE INDEX IF NOT EXISTS idx_delivery_outbox_priority_ready
            ON delivery_outbox(status, priority DESC, available_at, lease_until, id ASC);

            CREATE INDEX IF NOT EXISTS idx_delivery_outbox_supersede
            ON delivery_outbox(topic, supersede_key, status, id DESC);

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

        _apply_consensus_v2_reset_in_connection(con, _core.CONSENSUS_V2_RESET_ID)
        _apply_consensus_result_dedup_in_connection(con, _core.CONSENSUS_RESULT_DEDUP_ID)
        set_meta(con, "schema_version", "2026-07-20-control-delivery-v2")
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


def tvrs_apply_consensus_v2_reset(reset_id: str | None = None) -> dict[str, int | str]:
    reset_id = str(reset_id or _core.CONSENSUS_V2_RESET_ID)
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        result = _apply_consensus_v2_reset_in_connection(con, reset_id)
        con.commit()
        return result


# ---------------- Durable delivery outbox ----------------

__all__ = ['_backup_before_consensus_reset', '_consensus_result_dedup_meta_key', '_backup_before_consensus_result_dedup', 'init_db', '_consensus_reset_meta_key', '_apply_consensus_result_dedup_in_connection', '_apply_consensus_v2_reset_in_connection', 'tvrs_apply_consensus_v2_reset']
