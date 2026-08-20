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

            CREATE TABLE IF NOT EXISTS runtime_error_inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                component TEXT NOT NULL,
                level TEXT NOT NULL,
                exception_type TEXT,
                details TEXT NOT NULL,
                traceback_text TEXT,
                environment TEXT NOT NULL,
                release TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1,
                pending INTEGER NOT NULL DEFAULT 1,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                last_error TEXT,
                github_issue_number INTEGER,
                github_issue_url TEXT,
                last_published_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_runtime_error_inbox_pending
            ON runtime_error_inbox(pending, next_attempt_at, last_published_at);

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
                preferred_name TEXT,
                directory_completed_at TEXT,
                directory_required INTEGER NOT NULL DEFAULT 0,
                onboarding_prompted_at TEXT,
                onboarding_completed_at TEXT,
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

            CREATE TABLE IF NOT EXISTS member_onboarding_reminders (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                last_attempt_at TEXT NOT NULL,
                last_sent_at TEXT,
                sent_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id),
                FOREIGN KEY (guild_id, user_id)
                    REFERENCES member_profiles(guild_id, user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_member_onboarding_reminders_attempt
            ON member_onboarding_reminders(guild_id, last_attempt_at);

            CREATE TABLE IF NOT EXISTS web_credentials (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                login_key TEXT NOT NULL,
                login_display TEXT NOT NULL,
                pin_hash TEXT NOT NULL,
                session_version INTEGER NOT NULL DEFAULT 1,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until INTEGER NOT NULL DEFAULT 0,
                reset_required INTEGER NOT NULL DEFAULT 0,
                last_login_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id),
                UNIQUE (guild_id, login_key)
            );

            CREATE INDEX IF NOT EXISTS idx_web_credentials_login
            ON web_credentials(guild_id, login_key);

            CREATE TABLE IF NOT EXISTS web_section_grants (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                section TEXT NOT NULL,
                granted_by_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, section)
            );

            CREATE INDEX IF NOT EXISTS idx_web_section_grants_user
            ON web_section_grants(guild_id, user_id);

            CREATE TABLE IF NOT EXISTS global_bans (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                reason TEXT NOT NULL,
                issued_by_id INTEGER NOT NULL,
                issued_by_display TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                revoked_by_id INTEGER,
                revoked_by_display TEXT,
                revoked_at TEXT,
                discord_state TEXT NOT NULL DEFAULT 'pending'
                    CHECK(discord_state IN ('pending', 'banned', 'unbanned', 'failed')),
                discord_error TEXT,
                revision INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_global_bans_active
            ON global_bans(guild_id, active, issued_at DESC);

            CREATE TABLE IF NOT EXISTS global_ban_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('issued', 'revoked', 'discord_sync')),
                actor_id INTEGER NOT NULL,
                actor_display TEXT NOT NULL,
                reason TEXT,
                discord_state TEXT,
                discord_error TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_global_ban_events_subject
            ON global_ban_events(guild_id, user_id, id DESC);

            CREATE TABLE IF NOT EXISTS reactor_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info'
                    CHECK(severity IN ('info', 'success', 'warning', 'critical')),
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                route TEXT,
                source_key TEXT,
                dedupe_key TEXT NOT NULL,
                read_at TEXT,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(guild_id, user_id, dedupe_key)
            );

            CREATE INDEX IF NOT EXISTS idx_reactor_notifications_inbox
            ON reactor_notifications(guild_id, user_id, read_at, id DESC);

            CREATE TABLE IF NOT EXISTS atlas_servers (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                number INTEGER,
                label TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_by_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS atlas_factions (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                short_name TEXT NOT NULL,
                label TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_by_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS atlas_organizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                slug TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'project'
                    CHECK(kind IN ('government', 'bureau', 'project', 'personal')),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'suspended', 'archived')),
                owner_user_id INTEGER NOT NULL,
                description TEXT,
                branding_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(guild_id, slug)
            );

            CREATE TABLE IF NOT EXISTS atlas_memberships (
                organization_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member'
                    CHECK(role IN ('owner', 'administrator', 'editor', 'member', 'viewer')),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('invited', 'active', 'suspended')),
                onboarding_step INTEGER NOT NULL DEFAULT 0
                    CHECK(onboarding_step BETWEEN 0 AND 4),
                profile_json TEXT NOT NULL DEFAULT '{}',
                preferences_json TEXT NOT NULL DEFAULT '{}',
                last_seen_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(organization_id, user_id),
                FOREIGN KEY(organization_id) REFERENCES atlas_organizations(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_atlas_memberships_user
            ON atlas_memberships(guild_id, user_id, status);

            CREATE TABLE IF NOT EXISTS atlas_knowledge_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER NOT NULL,
                server_code TEXT NOT NULL DEFAULT 'phoenix-15',
                faction_code TEXT NOT NULL DEFAULT 'lspd',
                visibility_scope TEXT NOT NULL DEFAULT 'workspace'
                    CHECK(visibility_scope IN ('global', 'server', 'faction', 'workspace')),
                title TEXT NOT NULL,
                source_kind TEXT NOT NULL DEFAULT 'memo'
                    CHECK(source_kind IN ('document', 'forum', 'memo', 'regulation', 'manual', 'url')),
                source_url TEXT,
                content_text TEXT NOT NULL,
                checksum TEXT NOT NULL,
                qdrant_point_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'indexed', 'failed', 'archived')),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                original_filename TEXT,
                created_by_id INTEGER NOT NULL,
                indexed_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(organization_id, checksum),
                FOREIGN KEY(organization_id) REFERENCES atlas_organizations(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_atlas_knowledge_status
            ON atlas_knowledge_sources(organization_id, status, id DESC);

            CREATE TABLE IF NOT EXISTS atlas_knowledge_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                title TEXT NOT NULL,
                content_text TEXT NOT NULL,
                checksum TEXT NOT NULL,
                source_url TEXT,
                captured_at TEXT NOT NULL,
                UNIQUE(source_id, revision),
                FOREIGN KEY(source_id) REFERENCES atlas_knowledge_sources(id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS atlas_forum_feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                organization_id INTEGER NOT NULL,
                feed_key TEXT NOT NULL,
                root_url TEXT NOT NULL,
                server_code TEXT NOT NULL DEFAULT 'phoenix-15',
                faction_code TEXT NOT NULL DEFAULT 'lspd',
                visibility_scope TEXT NOT NULL DEFAULT 'server'
                    CHECK(visibility_scope IN ('global', 'server', 'faction', 'workspace')),
                interval_seconds INTEGER NOT NULL DEFAULT 43200,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'running', 'ok', 'attention', 'error', 'disabled')),
                last_started_at TEXT,
                last_success_at TEXT,
                next_sync_at TEXT,
                last_error TEXT,
                last_stats_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(guild_id, feed_key),
                FOREIGN KEY(organization_id) REFERENCES atlas_organizations(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_atlas_forum_feeds_due
            ON atlas_forum_feeds(status, next_sync_at, id);

            CREATE TABLE IF NOT EXISTS atlas_document_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT,
                schema_json TEXT NOT NULL DEFAULT '{}',
                template_text TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('draft', 'active', 'archived')),
                created_by_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(organization_id, code, version),
                FOREIGN KEY(organization_id) REFERENCES atlas_organizations(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_atlas_templates_catalog
            ON atlas_document_templates(organization_id, status, category, name);

            CREATE TABLE IF NOT EXISTS atlas_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER NOT NULL,
                template_id INTEGER,
                author_user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK(status IN ('draft', 'review', 'approved', 'published', 'archived')),
                fields_json TEXT NOT NULL DEFAULT '{}',
                rendered_text TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 1,
                reviewed_by_id INTEGER,
                approved_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(organization_id) REFERENCES atlas_organizations(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(template_id) REFERENCES atlas_document_templates(id)
                    ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_atlas_documents_workspace
            ON atlas_documents(organization_id, status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS atlas_ai_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                agent_id TEXT NOT NULL DEFAULT 'atlas-tvr-a',
                title TEXT NOT NULL DEFAULT 'Новый диалог',
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(organization_id) REFERENCES atlas_organizations(id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS atlas_ai_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                content_text TEXT NOT NULL,
                citations_json TEXT NOT NULL DEFAULT '[]',
                model TEXT,
                latency_ms INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES atlas_ai_threads(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_atlas_ai_messages_thread
            ON atlas_ai_messages(thread_id, id);

            CREATE TABLE IF NOT EXISTS atlas_ai_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                rating TEXT NOT NULL CHECK(rating IN ('good', 'bad')),
                comment_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(message_id, user_id),
                FOREIGN KEY(organization_id) REFERENCES atlas_organizations(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(thread_id) REFERENCES atlas_ai_threads(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(message_id) REFERENCES atlas_ai_messages(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_atlas_ai_feedback_review
            ON atlas_ai_feedback(organization_id, rating, updated_at DESC);

            CREATE TABLE IF NOT EXISTS atlas_discord_threads (
                discord_thread_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                parent_channel_id INTEGER NOT NULL,
                organization_id INTEGER NOT NULL,
                atlas_thread_id INTEGER NOT NULL UNIQUE,
                owner_user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'closed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(organization_id) REFERENCES atlas_organizations(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(atlas_thread_id) REFERENCES atlas_ai_threads(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_atlas_discord_threads_owner
            ON atlas_discord_threads(guild_id, owner_user_id, status);

            CREATE TABLE IF NOT EXISTS atlas_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER NOT NULL,
                actor_user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                target_type TEXT,
                target_id TEXT,
                summary TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(organization_id) REFERENCES atlas_organizations(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_atlas_audit_timeline
            ON atlas_audit_events(organization_id, id DESC);

            CREATE TABLE IF NOT EXISTS game_matches (
                id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                game_type TEXT NOT NULL
                    CHECK(game_type IN ('chess', 'backgammon')),
                mode TEXT NOT NULL
                    CHECK(mode IN ('bot', 'friend')),
                host_user_id INTEGER NOT NULL,
                host_display TEXT NOT NULL,
                guest_user_id INTEGER,
                guest_display TEXT,
                host_side TEXT NOT NULL
                    CHECK(host_side IN ('white', 'black')),
                bot_level INTEGER NOT NULL DEFAULT 1
                    CHECK(bot_level BETWEEN 1 AND 3),
                status TEXT NOT NULL DEFAULT 'waiting'
                    CHECK(status IN ('waiting', 'active', 'finished', 'cancelled')),
                turn_side TEXT NOT NULL DEFAULT 'white'
                    CHECK(turn_side IN ('white', 'black')),
                state_json TEXT NOT NULL,
                result TEXT,
                winner_side TEXT CHECK(winner_side IN ('white', 'black')),
                version INTEGER NOT NULL DEFAULT 1,
                last_action_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_game_matches_member
            ON game_matches(guild_id, host_user_id, guest_user_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS game_match_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id TEXT NOT NULL,
                actor_user_id INTEGER,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(match_id) REFERENCES game_matches(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_game_match_events_timeline
            ON game_match_events(match_id, id DESC);

            CREATE TABLE IF NOT EXISTS reactor_preferences (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                surface TEXT NOT NULL,
                layout_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, user_id, surface)
            );

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

            -- The live SGL workspace is deliberately separate from a durable
            -- archive snapshot.  These rows are the canonical projection of
            -- messages exchanged while a case channel is still active:
            -- Discord messages arrive through gateway listeners, while web
            -- messages are written immediately after Discord accepts them.
            CREATE TABLE IF NOT EXISTS sgl_case_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                case_number INTEGER NOT NULL,
                origin TEXT NOT NULL,
                discord_message_id INTEGER,
                author_id INTEGER,
                author_display TEXT NOT NULL DEFAULT '',
                author_avatar_url TEXT,
                author_is_bot INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL DEFAULT '',
                attachments_json TEXT NOT NULL DEFAULT '[]',
                reply_to_discord_message_id INTEGER,
                created_at TEXT NOT NULL,
                edited_at TEXT,
                deleted_at TEXT,
                UNIQUE(guild_id, discord_message_id),
                FOREIGN KEY (case_id) REFERENCES sgl_cases(id) ON DELETE CASCADE
            );

            -- A publication is intentionally a durable, reviewable object.
            -- A forum post is an external side effect, so the title/body and
            -- state are saved before a browser is allowed to submit anything.
            CREATE TABLE IF NOT EXISTS sgl_case_forum_publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                case_number INTEGER NOT NULL,
                target_url TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                forum_url TEXT,
                created_by_id INTEGER,
                created_by_display TEXT,
                reviewed_by_id INTEGER,
                reviewed_by_display TEXT,
                reviewed_at TEXT,
                published_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (case_id) REFERENCES sgl_cases(id) ON DELETE CASCADE
            );

            -- Atlas answers are a durable part of a case file, rather than an
            -- ephemeral chat window.  The original prompt is retained for
            -- auditability; citations remain structured for a later export.
            CREATE TABLE IF NOT EXISTS sgl_case_ai_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                case_number INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'analysis',
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                citations_json TEXT NOT NULL DEFAULT '[]',
                agent_id TEXT NOT NULL DEFAULT 'atlas-claims',
                response_mode TEXT NOT NULL DEFAULT 'balanced',
                created_by_id INTEGER,
                created_by_display TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (case_id) REFERENCES sgl_cases(id) ON DELETE CASCADE
            );

            -- Decisions and handoffs are deliberately kept out of public case
            -- correspondence.  They form a small, durable staff-only journal:
            -- the note body is immutable after creation, while its read /
            -- acknowledgement lifecycle preserves who picked it up and when.
            CREATE TABLE IF NOT EXISTS sgl_case_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                case_number INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'decision'
                    CHECK(kind IN ('decision', 'handoff', 'risk', 'note')),
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK(status IN ('open', 'read', 'acknowledged', 'superseded')),
                target_user_id INTEGER,
                target_display TEXT,
                created_by_id INTEGER,
                created_by_display TEXT,
                created_at TEXT NOT NULL,
                read_at TEXT,
                read_by_id INTEGER,
                read_by_display TEXT,
                acknowledged_at TEXT,
                acknowledged_by_id INTEGER,
                acknowledged_by_display TEXT,
                updated_by_id INTEGER,
                updated_by_display TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (case_id) REFERENCES sgl_cases(id) ON DELETE CASCADE
            );

            -- Each published claim gets one monitored forum projection.  A
            -- fingerprint lets the bot alert about substantive changes without
            -- repeatedly notifying about an unchanged topic.
            CREATE TABLE IF NOT EXISTS sgl_case_forum_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                publication_id INTEGER NOT NULL UNIQUE,
                case_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                case_number INTEGER NOT NULL,
                forum_url TEXT NOT NULL,
                thread_title TEXT,
                thread_excerpt TEXT,
                content_fingerprint TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                last_checked_at TEXT,
                last_changed_at TEXT,
                last_notified_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (publication_id) REFERENCES sgl_case_forum_publications(id) ON DELETE CASCADE,
                FOREIGN KEY (case_id) REFERENCES sgl_cases(id) ON DELETE CASCADE
            );

            -- Tasks and notifications make the web workspace an operational
            -- surface, not merely a mirror of Discord.  They deliberately
            -- attach to a case where possible, while retaining a guild-level
            -- projection for watch failures and other bureau-wide signals.
            CREATE TABLE IF NOT EXISTS sgl_case_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                case_number INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                priority TEXT NOT NULL DEFAULT 'normal'
                    CHECK(priority IN ('critical', 'high', 'normal', 'low')),
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK(status IN ('open', 'done', 'cancelled')),
                owner_id INTEGER,
                owner_display TEXT,
                due_at TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                completed_at TEXT,
                completed_by_id INTEGER,
                completed_by_display TEXT,
                created_by_id INTEGER,
                created_by_display TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (case_id) REFERENCES sgl_cases(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS sgl_case_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                case_id INTEGER,
                case_number INTEGER,
                kind TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info'
                    CHECK(severity IN ('info', 'success', 'warning', 'critical')),
                title TEXT NOT NULL,
                body TEXT,
                tab TEXT,
                source TEXT NOT NULL DEFAULT 'system',
                external_status TEXT NOT NULL DEFAULT 'not_applicable'
                    CHECK(external_status IN ('not_applicable', 'pending', 'sent', 'failed')),
                dedupe_key TEXT,
                acknowledged_at TEXT,
                acknowledged_by_id INTEGER,
                acknowledged_by_display TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(guild_id, dedupe_key),
                FOREIGN KEY (case_id) REFERENCES sgl_cases(id) ON DELETE CASCADE
            );

            -- Keep compact immutable Forum Watch snapshots.  The current
            -- observation is ideal for monitoring, but a case operator also
            -- needs to see what changed without scraping the forum again.
            CREATE TABLE IF NOT EXISTS sgl_case_forum_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                publication_id INTEGER NOT NULL,
                case_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                case_number INTEGER NOT NULL,
                snapshot_kind TEXT NOT NULL,
                thread_title TEXT,
                thread_excerpt TEXT,
                content_fingerprint TEXT,
                captured_at TEXT NOT NULL,
                FOREIGN KEY (publication_id) REFERENCES sgl_case_forum_publications(id) ON DELETE CASCADE,
                FOREIGN KEY (case_id) REFERENCES sgl_cases(id) ON DELETE CASCADE
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
                execution_blocks_json TEXT NOT NULL DEFAULT '[]',
                moderated_by_id INTEGER,
                moderated_by_display TEXT,
                moderated_at TEXT,
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
                execution_blocks_json TEXT NOT NULL DEFAULT '[]',
                ai_model TEXT,
                ai_revision INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1,
                submitted_bill_id INTEGER,
                moderation_status TEXT NOT NULL DEFAULT 'draft',
                moderation_round INTEGER NOT NULL DEFAULT 0,
                moderation_note TEXT,
                moderator_id INTEGER,
                moderator_display TEXT,
                submitted_at TEXT,
                reviewed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tvrs_bill_workspaces_author
            ON tvrs_bill_workspaces(guild_id, author_id, status, updated_at DESC);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_tvrs_bill_workspaces_one_open
            ON tvrs_bill_workspaces(guild_id, author_id)
            WHERE status IN ('draft', 'review', 'changes_requested');

            CREATE TABLE IF NOT EXISTS tvrs_bill_moderation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                workspace_id INTEGER NOT NULL,
                round INTEGER NOT NULL DEFAULT 1,
                action TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                actor_display TEXT,
                note TEXT,
                workspace_revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tvrs_bill_moderation_events_workspace
            ON tvrs_bill_moderation_events(workspace_id, id ASC);

            CREATE TABLE IF NOT EXISTS tvrs_legislation_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                bill_id INTEGER,
                workspace_id INTEGER,
                source_block_id TEXT,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'todo',
                priority TEXT NOT NULL DEFAULT 'normal',
                assignee_id INTEGER,
                assignee_display TEXT,
                due_at TEXT,
                created_by_id INTEGER NOT NULL,
                created_by_display TEXT,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_tvrs_legislation_tasks_board
            ON tvrs_legislation_tasks(guild_id, status, priority, due_at, id);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_tvrs_legislation_tasks_block
            ON tvrs_legislation_tasks(bill_id, source_block_id)
            WHERE bill_id IS NOT NULL AND source_block_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS ovr_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                case_number INTEGER NOT NULL,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                static_id TEXT NOT NULL,
                discord_text TEXT NOT NULL,
                discord_user_id INTEGER,
                forum_url TEXT,
                additional_info TEXT,
                case_kind TEXT NOT NULL DEFAULT 'admission',
                priority TEXT NOT NULL DEFAULT 'normal',
                classification TEXT NOT NULL DEFAULT 'restricted',
                objective TEXT,
                executive_summary TEXT,
                hypothesis TEXT,
                aliases TEXT,
                affiliations TEXT,
                nowa_links TEXT,
                findings TEXT,
                risk_level TEXT NOT NULL DEFAULT 'unrated',
                status TEXT NOT NULL DEFAULT 'new',
                decision TEXT,
                decision_reason TEXT,
                assigned_to_id INTEGER,
                assigned_to_display TEXT,
                created_by_id INTEGER NOT NULL,
                created_by_display TEXT,
                due_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                decided_at TEXT,
                UNIQUE(guild_id, case_number)
            );

            CREATE INDEX IF NOT EXISTS idx_ovr_cases_board
            ON ovr_cases(guild_id, status, due_at, case_number DESC);

            CREATE TABLE IF NOT EXISTS ovr_case_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                case_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                actor_display TEXT,
                action TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ovr_case_events_case
            ON ovr_case_events(case_id, id ASC);

            CREATE TABLE IF NOT EXISTS ovr_case_materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                case_id INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'document',
                title TEXT NOT NULL,
                content TEXT,
                source_url TEXT,
                reliability TEXT NOT NULL DEFAULT 'unrated',
                status TEXT NOT NULL DEFAULT 'new',
                active INTEGER NOT NULL DEFAULT 1,
                created_by_id INTEGER NOT NULL,
                created_by_display TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ovr_case_materials_case
            ON ovr_case_materials(case_id, active, id DESC);

            CREATE TABLE IF NOT EXISTS ovr_case_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                case_id INTEGER NOT NULL,
                person_name TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                static_id TEXT,
                discord_text TEXT,
                details TEXT,
                confidence TEXT NOT NULL DEFAULT 'unrated',
                active INTEGER NOT NULL DEFAULT 1,
                created_by_id INTEGER NOT NULL,
                created_by_display TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ovr_case_relations_case
            ON ovr_case_relations(case_id, active, id DESC);

            CREATE TABLE IF NOT EXISTS ovr_case_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                case_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'todo',
                priority TEXT NOT NULL DEFAULT 'normal',
                assignee_id INTEGER,
                assignee_display TEXT,
                due_at TEXT,
                created_by_id INTEGER NOT NULL,
                created_by_display TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ovr_case_tasks_case
            ON ovr_case_tasks(case_id, status, priority, id DESC);

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

            CREATE TABLE IF NOT EXISTS tvrs_consensus_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                plenary_number INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                invitation_text TEXT NOT NULL DEFAULT '',
                scheduled_for TEXT NOT NULL,
                initial_scheduled_for TEXT,
                time_shift_minutes INTEGER NOT NULL DEFAULT 0,
                last_rescheduled_at TEXT,
                duration_minutes INTEGER NOT NULL DEFAULT 90,
                voice_channel_id INTEGER NOT NULL,
                created_by_id INTEGER NOT NULL,
                created_by_display TEXT,
                discord_event_id INTEGER,
                invitation_broadcast_id INTEGER,
                status TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK(status IN ('scheduled', 'started', 'completed', 'cancelled')),
                started_session_key TEXT,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                cancelled_at TEXT,
                started_at TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_tvrs_consensus_schedule_active
            ON tvrs_consensus_schedules(guild_id)
            WHERE status = 'scheduled';

            CREATE INDEX IF NOT EXISTS idx_tvrs_consensus_schedule_timeline
            ON tvrs_consensus_schedules(guild_id, scheduled_for DESC, id DESC);

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

        for column, definition in {
            "case_kind": "TEXT NOT NULL DEFAULT 'admission'",
            "priority": "TEXT NOT NULL DEFAULT 'normal'",
            "classification": "TEXT NOT NULL DEFAULT 'restricted'",
            "objective": "TEXT",
            "executive_summary": "TEXT",
            "hypothesis": "TEXT",
            "aliases": "TEXT",
            "affiliations": "TEXT",
        }.items():
            _add_column_if_missing(con, "ovr_cases", column, definition)

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
            "web_credentials",
            "reset_required",
            "INTEGER NOT NULL DEFAULT 0",
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
            "execution_blocks_json": "TEXT NOT NULL DEFAULT '[]'",
            "moderated_by_id": "INTEGER",
            "moderated_by_display": "TEXT",
            "moderated_at": "TEXT",
        }.items():
            _add_column_if_missing(con, "tvrs_bills", column, definition)

        for column, definition in {
            "execution_blocks_json": "TEXT NOT NULL DEFAULT '[]'",
            "moderation_status": "TEXT NOT NULL DEFAULT 'draft'",
            "moderation_round": "INTEGER NOT NULL DEFAULT 0",
            "moderation_note": "TEXT",
            "moderator_id": "INTEGER",
            "moderator_display": "TEXT",
            "submitted_at": "TEXT",
            "reviewed_at": "TEXT",
        }.items():
            _add_column_if_missing(con, "tvrs_bill_workspaces", column, definition)
        con.execute(
            """
            UPDATE tvrs_bill_workspaces
            SET moderation_status = CASE
                WHEN status = 'submitted' THEN 'approved'
                WHEN status = 'cancelled' THEN 'cancelled'
                WHEN moderation_status IS NULL OR moderation_status = '' THEN 'draft'
                ELSE moderation_status
            END
            WHERE moderation_status IS NULL
               OR moderation_status = ''
               OR (status IN ('submitted', 'cancelled') AND moderation_status = 'draft')
            """
        )
        con.execute("DROP INDEX IF EXISTS idx_tvrs_bill_workspaces_one_open")
        con.execute(
            """
            CREATE UNIQUE INDEX idx_tvrs_bill_workspaces_one_open
            ON tvrs_bill_workspaces(guild_id, author_id)
            WHERE status IN ('draft', 'review', 'changes_requested')
            """
        )
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS tvrs_bill_moderation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                workspace_id INTEGER NOT NULL,
                round INTEGER NOT NULL DEFAULT 1,
                action TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                actor_display TEXT,
                note TEXT,
                workspace_revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tvrs_bill_moderation_queue
            ON tvrs_bill_workspaces(guild_id, moderation_status, submitted_at, id);
            CREATE INDEX IF NOT EXISTS idx_tvrs_bill_moderation_events_workspace
            ON tvrs_bill_moderation_events(workspace_id, id ASC);

            CREATE TABLE IF NOT EXISTS tvrs_legislation_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                bill_id INTEGER,
                workspace_id INTEGER,
                source_block_id TEXT,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'todo',
                priority TEXT NOT NULL DEFAULT 'normal',
                assignee_id INTEGER,
                assignee_display TEXT,
                due_at TEXT,
                created_by_id INTEGER NOT NULL,
                created_by_display TEXT,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tvrs_legislation_tasks_board
            ON tvrs_legislation_tasks(guild_id, status, priority, due_at, id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tvrs_legislation_tasks_block
            ON tvrs_legislation_tasks(bill_id, source_block_id)
            WHERE bill_id IS NOT NULL AND source_block_id IS NOT NULL;
            """
        )

        for column, definition in {
            "initial_scheduled_for": "TEXT",
            "time_shift_minutes": "INTEGER NOT NULL DEFAULT 0",
            "last_rescheduled_at": "TEXT",
        }.items():
            _add_column_if_missing(con, "tvrs_consensus_schedules", column, definition)
        con.execute(
            """
            UPDATE tvrs_consensus_schedules
            SET initial_scheduled_for = scheduled_for
            WHERE initial_scheduled_for IS NULL OR initial_scheduled_for = ''
            """
        )
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
            "preferred_name": "TEXT",
            "directory_completed_at": "TEXT",
            "directory_required": "INTEGER NOT NULL DEFAULT 0",
            "onboarding_prompted_at": "TEXT",
            "onboarding_completed_at": "TEXT",
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
        _add_column_if_missing(
            con,
            "atlas_knowledge_sources",
            "server_code",
            "TEXT NOT NULL DEFAULT 'phoenix-15'",
        )
        _add_column_if_missing(
            con,
            "atlas_knowledge_sources",
            "faction_code",
            "TEXT NOT NULL DEFAULT 'lspd'",
        )
        atlas_scope_missing = not any(
            str(row["name"]) == "visibility_scope"
            for row in con.execute("PRAGMA table_info(atlas_knowledge_sources)").fetchall()
        )
        _add_column_if_missing(
            con,
            "atlas_knowledge_sources",
            "visibility_scope",
            "TEXT NOT NULL DEFAULT 'workspace'",
        )
        _add_column_if_missing(con, "atlas_knowledge_sources", "original_filename", "TEXT")
        _add_column_if_missing(
            con,
            "atlas_ai_threads",
            "agent_id",
            "TEXT NOT NULL DEFAULT 'atlas-tvr-a'",
        )
        if atlas_scope_missing:
            # Existing material remains private. Marking it pending replaces
            # old Qdrant payloads with the new access-scope token on startup.
            con.execute(
                """
                UPDATE atlas_knowledge_sources
                SET status = CASE WHEN status = 'archived' THEN status ELSE 'pending' END,
                    qdrant_point_id = NULL,
                    last_error = NULL
                """
            )

        atlas_taxonomy_migration = "migration:atlas-taxonomy:2026-08-09-v1"
        if con.execute(
            "SELECT 1 FROM meta WHERE key = ?",
            (atlas_taxonomy_migration,),
        ).fetchone() is None:
            # The Qdrant index is derived data. Rebuild payload metadata while
            # preserving every canonical source and revision in SQLite.
            con.execute(
                """
                UPDATE atlas_knowledge_sources
                SET status = CASE WHEN status = 'archived' THEN status ELSE 'pending' END,
                    qdrant_point_id = NULL,
                    last_error = NULL
                """
            )
            set_meta(con, atlas_taxonomy_migration, utc_now_iso())

        atlas_title_index_migration = "migration:atlas-title-index:2026-08-09-v1"
        if con.execute(
            "SELECT 1 FROM meta WHERE key = ?",
            (atlas_title_index_migration,),
        ).fetchone() is None:
            # Search embeddings now include the document identity. Rebuild only
            # derived Qdrant points; canonical texts and revisions stay intact.
            con.execute(
                """
                UPDATE atlas_knowledge_sources
                SET status = CASE WHEN status = 'archived' THEN status ELSE 'pending' END,
                    qdrant_point_id = NULL,
                    last_error = NULL
                """
            )
            set_meta(con, atlas_title_index_migration, utc_now_iso())

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

            CREATE INDEX IF NOT EXISTS idx_atlas_knowledge_scope
            ON atlas_knowledge_sources(
                organization_id, server_code, faction_code, status, id DESC
            );

            CREATE INDEX IF NOT EXISTS idx_atlas_knowledge_visibility
            ON atlas_knowledge_sources(
                visibility_scope, server_code, faction_code, organization_id,
                status, id DESC
            );

            CREATE INDEX IF NOT EXISTS idx_atlas_knowledge_revisions_source
            ON atlas_knowledge_revisions(source_id, revision DESC);

            CREATE INDEX IF NOT EXISTS idx_atlas_knowledge_forum_url
            ON atlas_knowledge_sources(organization_id, source_kind, source_url, id DESC);

            CREATE INDEX IF NOT EXISTS idx_activity_events_guild_at
            ON activity_events(guild_id, at DESC, id DESC);

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

            CREATE INDEX IF NOT EXISTS idx_sgl_case_messages_case
            ON sgl_case_messages(case_id, id DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_messages_discord
            ON sgl_case_messages(guild_id, discord_message_id);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_forum_publications_case
            ON sgl_case_forum_publications(case_id, id DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_forum_publications_status
            ON sgl_case_forum_publications(guild_id, status, id ASC);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_ai_notes_case
            ON sgl_case_ai_notes(case_id, id DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_decisions_case
            ON sgl_case_decisions(case_id, status, id DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_decisions_target
            ON sgl_case_decisions(guild_id, target_user_id, status, id DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_forum_observations_guild
            ON sgl_case_forum_observations(guild_id, status, updated_at DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_tasks_open
            ON sgl_case_tasks(guild_id, status, due_at, priority, id DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_tasks_case
            ON sgl_case_tasks(case_id, status, id DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_notifications_inbox
            ON sgl_case_notifications(guild_id, acknowledged_at, id DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_notifications_case
            ON sgl_case_notifications(case_id, acknowledged_at, id DESC);

            CREATE INDEX IF NOT EXISTS idx_sgl_case_forum_snapshots_case
            ON sgl_case_forum_snapshots(case_id, id DESC);

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

            CREATE INDEX IF NOT EXISTS idx_finance_events_guild_created
            ON finance_events(guild_id, created_at DESC, id DESC);

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

            CREATE INDEX IF NOT EXISTS idx_bot_actions_guild_created
            ON bot_actions(guild_id, created_at DESC, id DESC);

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

            CREATE INDEX IF NOT EXISTS idx_craft_events_guild_created
            ON craft_events(guild_id, created_at DESC, id DESC);

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

        catalog_now = utc_now_iso()
        con.execute(
            """
            INSERT OR IGNORE INTO atlas_servers(
                code, name, number, label, created_at, updated_at
            ) VALUES('phoenix-15', 'Phoenix', 15, 'Phoenix (15)', ?, ?)
            """,
            (catalog_now, catalog_now),
        )
        for code, name, short_name in (
            ('lspd', 'Los Santos Police Department', 'LSPD'),
            ('gov', 'Government', 'GOV'),
        ):
            con.execute(
                """
                INSERT OR IGNORE INTO atlas_factions(
                    code, name, short_name, label, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (code, name, short_name, short_name, catalog_now, catalog_now),
            )

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


def _apply_consensus_v2_reset_in_connection(
    con: sqlite3.Connection,
    reset_id: str,
    *,
    destructive: bool = False,
) -> dict[str, int | str]:
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
    if (
        not destructive
        and str(reset_id) == "2026-07-16-clean-consensus-v2"
        and any(counts.values())
    ):
        # A missing migration marker is not proof that current consensus data
        # is legacy.  The meta table can be repaired or partially lost while
        # all domain tables remain valid.  Startup must therefore fail safe:
        # preserve existing decisions and recreate the marker.  The explicit
        # maintenance API below (or a deliberately new migration id) is the
        # only path allowed to erase this data.
        summary: dict[str, int | str] = {
            "status": "preserved_existing_data",
            **counts,
        }
        set_meta(con, meta_key, json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return summary
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
        result = _apply_consensus_v2_reset_in_connection(
            con,
            reset_id,
            destructive=True,
        )
        con.commit()
        return result


# ---------------- Durable delivery outbox ----------------

__all__ = ['_backup_before_consensus_reset', '_consensus_result_dedup_meta_key', '_backup_before_consensus_result_dedup', 'init_db', '_consensus_reset_meta_key', '_apply_consensus_result_dedup_in_connection', '_apply_consensus_v2_reset_in_connection', 'tvrs_apply_consensus_v2_reset']
