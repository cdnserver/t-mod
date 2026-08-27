"""Durable multi-tenant storage for T-Mod Atlas."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from persistence.atlas_document_fields import normalize_document_schema, prepare_document_content

from modules.atlas_catalog import (
    atlas_catalog as _base_atlas_catalog,
    atlas_normalize_knowledge_scope,
)
from modules.atlas_taxonomy import atlas_classify_knowledge, atlas_taxonomy_catalog
from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_ROLES = frozenset({"owner", "administrator", "editor", "member", "viewer"})
_CATALOG_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
_TIMELINE_KINDS = frozenset(
    {"incident", "activity", "decision", "document", "communication", "note", "system"}
)
_TIMELINE_STATUSES = frozenset({"open", "active", "resolved", "archived"})
_TIMELINE_IMPORTANCE = frozenset({"routine", "important", "critical"})
_ENTITY_RELATIONS = frozenset(
    {
        "related_to", "supports", "contradicts", "caused_by", "produced",
        "mentions", "responds_to", "belongs_to",
    }
)
_ENTITY_TABLES = {
    "organization": ("atlas_organizations", "id"),
    "document": ("atlas_documents", "id"),
    "timeline_event": ("atlas_timeline_events", "id"),
    "knowledge_source": ("atlas_knowledge_sources", "id"),
    "ai_thread": ("atlas_ai_threads", "id"),
    "forum_feed": ("atlas_forum_feeds", "id"),
    "media_asset": ("atlas_media_assets", "id"),
    "media_segment": ("atlas_media_segments", "id"),
    "case": ("atlas_cases", "id"),
    "case_claim": ("atlas_case_claims", "id"),
    "case_evidence": ("atlas_case_evidence", "id"),
}
_UNSET = object()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decoded(value: Any, fallback: Any) -> Any:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return parsed


def _slug(value: str, user_id: int) -> str:
    selected = _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-")
    return (selected[:36] or f"space-{int(user_id)}")


def _timeline_time(value: str | None) -> str:
    selected = str(value or "").strip()
    if not selected:
        return utc_now_iso()
    try:
        parsed = datetime.fromisoformat(selected.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("atlas_timeline_time_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _entity_identity(value: str | int) -> str:
    selected = str(value or "").strip()
    if not selected or not selected.isdigit() or int(selected) <= 0:
        raise ValueError("atlas_entity_id_invalid")
    return str(int(selected))


def _atlas_entity_exists(
    con: Any,
    organization_id: int,
    entity_type: str,
    entity_id: str | int,
    *,
    actor_user_id: int | None = None,
) -> bool:
    clean_type = str(entity_type or "").strip().lower()
    table = _ENTITY_TABLES.get(clean_type)
    if table is None:
        raise ValueError("atlas_entity_type_invalid")
    clean_id = _entity_identity(entity_id)
    if clean_type == "organization":
        row = con.execute(
            "SELECT 1 FROM atlas_organizations WHERE id = ? AND id = ?",
            (int(clean_id), int(organization_id)),
        ).fetchone()
    elif clean_type == "ai_thread" and actor_user_id is not None:
        row = con.execute(
            """
            SELECT 1 FROM atlas_ai_threads
            WHERE id = ? AND organization_id = ? AND user_id = ?
            """,
            (int(clean_id), int(organization_id), int(actor_user_id)),
        ).fetchone()
    elif clean_type == "media_asset" and actor_user_id is not None:
        row = con.execute(
            """
            SELECT 1 FROM atlas_media_assets
            WHERE id = ? AND organization_id = ? AND status != 'deleted'
              AND (visibility_scope = 'workspace' OR owner_user_id = ?)
            """,
            (int(clean_id), int(organization_id), int(actor_user_id)),
        ).fetchone()
    elif clean_type == "media_segment" and actor_user_id is not None:
        row = con.execute(
            """
            SELECT 1 FROM atlas_media_segments s
            JOIN atlas_media_assets a ON a.id = s.asset_id
            WHERE s.id = ? AND s.organization_id = ? AND a.status != 'deleted'
              AND (a.visibility_scope = 'workspace' OR a.owner_user_id = ?)
            """,
            (int(clean_id), int(organization_id), int(actor_user_id)),
        ).fetchone()
    elif clean_type == "case" and actor_user_id is not None:
        row = con.execute(
            """
            SELECT 1 FROM atlas_cases
            WHERE id = ? AND organization_id = ?
              AND (visibility_scope = 'workspace' OR created_by_id = ? OR assigned_to_id = ?)
            """,
            (int(clean_id), int(organization_id), int(actor_user_id), int(actor_user_id)),
        ).fetchone()
    elif clean_type in {"case_claim", "case_evidence"} and actor_user_id is not None:
        child_table = "atlas_case_claims" if clean_type == "case_claim" else "atlas_case_evidence"
        row = con.execute(
            f"""
            SELECT 1 FROM {child_table} child
            JOIN atlas_cases c ON c.id = child.case_id
            WHERE child.id = ? AND child.organization_id = ?
              AND (c.visibility_scope = 'workspace' OR c.created_by_id = ? OR c.assigned_to_id = ?)
            """,
            (int(clean_id), int(organization_id), int(actor_user_id), int(actor_user_id)),
        ).fetchone()
    else:
        table_name, id_column = table
        row = con.execute(
            f"SELECT 1 FROM {table_name} WHERE {id_column} = ? AND organization_id = ?",
            (int(clean_id), int(organization_id)),
        ).fetchone()
    return row is not None


def _knowledge_checksum(
    visibility_scope: str,
    server_code: str,
    faction_code: str,
    content: str,
    *,
    identity: str = "",
) -> str:
    return hashlib.sha256(
        (
            f"{visibility_scope}\0{server_code}\0{faction_code}\0"
            f"{identity}\0{content}"
        ).encode("utf-8")
    ).hexdigest()


def _row(row: Any) -> dict[str, Any]:
    item = dict(row)
    for key, fallback in (
        ("branding_json", {}),
        ("profile_json", {}),
        ("preferences_json", {}),
        ("metadata_json", {}),
        ("schema_json", {}),
        ("fields_json", {}),
        ("citations_json", []),
        ("details_json", {}),
        ("last_stats_json", {}),
    ):
        if key in item:
            item[key.removesuffix("_json")] = _decoded(item.pop(key), fallback)
    if "source_kind" in item and "title" in item:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if not isinstance(metadata.get("taxonomy"), dict):
            metadata["taxonomy"] = atlas_classify_knowledge(
                title=str(item.get("title") or ""),
                content=str(item.get("content_text") or ""),
                source_url=str(item.get("source_url") or "") or None,
                source_kind=str(item.get("source_kind") or "memo"),
            )
        item["metadata"] = metadata
    return item


def atlas_catalog() -> dict[str, Any]:
    """Return the administrator-managed catalog with stable built-in defaults."""

    base = _base_atlas_catalog()
    try:
        with connect_readonly() as con:
            servers = con.execute(
                "SELECT * FROM atlas_servers ORDER BY enabled DESC, COALESCE(number, 9999), label"
            ).fetchall()
            factions = con.execute(
                "SELECT * FROM atlas_factions ORDER BY enabled DESC, label"
            ).fetchall()
    except Exception:  # startup compatibility before the catalog migration
        servers = []
        factions = []
    def merged(
        builtins: list[dict[str, Any]],
        stored: list[Any],
    ) -> list[dict[str, Any]]:
        # A single administrator-created row must not make the rest of the
        # official catalog disappear. Stored rows override built-ins by code;
        # genuinely custom rows are appended and remain fully supported.
        stored_by_code = {
            str(item.get("code") or ""): item
            for row in stored
            if (item := _row(row)).get("code")
        }
        result: list[dict[str, Any]] = []
        known: set[str] = set()
        for item in builtins:
            code = str(item.get("code") or "")
            result.append({**item, **stored_by_code.get(code, {})})
            known.add(code)
        result.extend(
            item
            for code, item in stored_by_code.items()
            if code not in known
        )
        return result

    return {
        **base,
        **atlas_taxonomy_catalog(),
        "servers": merged(base["servers"], servers),
        "factions": merged(base["factions"], factions),
    }


def atlas_normalize_scope(server_code: str, faction_code: str) -> tuple[str, str]:
    selected_server = str(server_code or "").strip().lower()
    selected_faction = str(faction_code or "").strip().lower()
    catalog = atlas_catalog()
    valid_servers = {
        str(item["code"])
        for item in catalog["servers"]
        if bool(item.get("enabled", True))
    }
    valid_factions = {
        str(item["code"])
        for item in catalog["factions"]
        if bool(item.get("enabled", True))
    }
    if selected_server not in valid_servers:
        raise ValueError("atlas_server_invalid")
    if selected_faction not in valid_factions:
        raise ValueError("atlas_faction_invalid")
    return selected_server, selected_faction


def atlas_overlay_characters(guild_id: int, user_id: int) -> list[dict[str, Any]]:
    """Return only characters owned by the authenticated T-Mod account.

    Faction bindings are deliberately separate from the public profile: a user
    can keep a profile character private while still using it locally in the
    desktop overlay. Ownership is always proven by the database join.
    """

    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT pc.id, pc.nickname, pc.static_id, pc.position, pc.is_public,
                   b.server_code, b.faction_code, b.rank_name, b.is_selected,
                   b.voice_reply_enabled, b.screen_context_enabled,
                   b.assignment_status, b.verified_at, b.updated_at AS binding_updated_at
            FROM profile_characters pc
            LEFT JOIN atlas_character_bindings b
              ON b.guild_id = pc.guild_id
             AND b.user_id = pc.user_id
             AND b.character_id = pc.id
            WHERE pc.guild_id = ? AND pc.user_id = ?
            ORDER BY pc.position, pc.id
            """,
            (int(guild_id), int(user_id)),
        ).fetchall()
    catalog = atlas_catalog()
    server_labels = {
        str(item.get("code") or ""): str(item.get("label") or item.get("name") or "")
        for item in catalog["servers"]
    }
    faction_labels = {
        str(item.get("code") or ""): str(item.get("label") or item.get("name") or "")
        for item in catalog["factions"]
    }
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        bound = bool(item.get("server_code") and item.get("faction_code"))
        result.append(
            {
                "id": int(item["id"]),
                "nickname": str(item["nickname"]),
                "static_id": str(item["static_id"]),
                "position": int(item["position"]),
                "profile_public": bool(item["is_public"]),
                "identity_verified": True,
                "bound": bound,
                "selected": bool(item.get("is_selected")),
                "server_code": str(item.get("server_code") or ""),
                "server_label": server_labels.get(str(item.get("server_code") or ""), ""),
                "faction_code": str(item.get("faction_code") or ""),
                "faction_label": faction_labels.get(str(item.get("faction_code") or ""), ""),
                "faction_name": faction_labels.get(str(item.get("faction_code") or ""), ""),
                "rank": str(item.get("rank_name") or ""),
                "voice_reply_enabled": bool(
                    1 if item.get("voice_reply_enabled") is None else item["voice_reply_enabled"]
                ),
                "screen_context_enabled": bool(item.get("screen_context_enabled")),
                "assignment_status": str(item.get("assignment_status") or "unbound"),
                "verified_at": item.get("verified_at"),
            }
        )
    return result


def atlas_set_overlay_character(
    guild_id: int,
    user_id: int,
    character_id: int,
    *,
    server_code: str,
    faction_code: str,
    rank: str | None = None,
    voice_reply_enabled: bool = True,
    screen_context_enabled: bool = False,
) -> dict[str, Any]:
    """Bind and select an owned character for the low-latency field mode."""

    clean_server, clean_faction = atlas_normalize_scope(server_code, faction_code)
    clean_rank = None if rank is None else " ".join(str(rank or "").split())[:100]
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        owned = con.execute(
            """
            SELECT 1 FROM profile_characters
            WHERE id = ? AND guild_id = ? AND user_id = ?
            """,
            (int(character_id), int(guild_id), int(user_id)),
        ).fetchone()
        if owned is None:
            con.rollback()
            raise ValueError("atlas_overlay_character_not_owned")
        if clean_rank is None:
            existing = con.execute(
                """
                SELECT rank_name FROM atlas_character_bindings
                WHERE guild_id = ? AND user_id = ? AND character_id = ?
                """,
                (int(guild_id), int(user_id), int(character_id)),
            ).fetchone()
            clean_rank = str(existing["rank_name"] or "") if existing else ""
        con.execute(
            """
            UPDATE atlas_character_bindings SET is_selected = 0, updated_at = ?
            WHERE guild_id = ? AND user_id = ? AND is_selected = 1
            """,
            (now, int(guild_id), int(user_id)),
        )
        con.execute(
            """
            INSERT INTO atlas_character_bindings(
                guild_id, user_id, character_id, server_code, faction_code,
                rank_name, is_selected, voice_reply_enabled,
                screen_context_enabled, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, character_id) DO UPDATE SET
                server_code = excluded.server_code,
                faction_code = excluded.faction_code,
                rank_name = excluded.rank_name,
                is_selected = 1,
                voice_reply_enabled = excluded.voice_reply_enabled,
                screen_context_enabled = excluded.screen_context_enabled,
                assignment_status = CASE
                    WHEN atlas_character_bindings.assignment_status = 'verified'
                    THEN 'verified' ELSE 'self_reported' END,
                updated_at = excluded.updated_at
            """,
            (
                int(guild_id), int(user_id), int(character_id), clean_server,
                clean_faction, clean_rank, int(bool(voice_reply_enabled)),
                int(bool(screen_context_enabled)), now, now,
            ),
        )
        con.commit()
    return atlas_overlay_context(guild_id, user_id, character_id=character_id)


def atlas_overlay_context(
    guild_id: int,
    user_id: int,
    *,
    character_id: int | None = None,
) -> dict[str, Any]:
    characters = atlas_overlay_characters(guild_id, user_id)
    if character_id is not None:
        selected = next(
            (item for item in characters if int(item["id"]) == int(character_id)),
            None,
        )
        if selected is None:
            raise ValueError("atlas_overlay_character_not_owned")
    else:
        selected = next((item for item in characters if item["selected"]), None)
    return {
        "characters": characters,
        "selected_character": selected,
        "catalog": atlas_catalog(),
    }


def atlas_upsert_server(
    actor_user_id: int,
    *,
    code: str,
    name: str,
    number: int | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    clean_code = str(code or "").strip().lower()
    clean_name = " ".join(str(name or "").split())[:100]
    if not _CATALOG_CODE_RE.fullmatch(clean_code) or len(clean_name) < 2:
        raise ValueError("atlas_server_invalid")
    clean_number = int(number) if number not in {None, ""} else None
    label = f"{clean_name} ({clean_number})" if clean_number is not None else clean_name
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            INSERT INTO atlas_servers(
                code, name, number, label, enabled, created_by_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name, number = excluded.number, label = excluded.label,
                enabled = excluded.enabled, updated_at = excluded.updated_at
            """,
            (
                clean_code, clean_name, clean_number, label, int(bool(enabled)),
                int(actor_user_id), now, now,
            ),
        )
        row = con.execute("SELECT * FROM atlas_servers WHERE code = ?", (clean_code,)).fetchone()
        con.commit()
    return _row(row)


def atlas_upsert_faction(
    actor_user_id: int,
    *,
    code: str,
    name: str,
    short_name: str,
    enabled: bool = True,
) -> dict[str, Any]:
    clean_code = str(code or "").strip().lower()
    clean_name = " ".join(str(name or "").split())[:140]
    clean_short = " ".join(str(short_name or "").split()).upper()[:24]
    if (
        not _CATALOG_CODE_RE.fullmatch(clean_code)
        or len(clean_name) < 2
        or len(clean_short) < 2
    ):
        raise ValueError("atlas_faction_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            INSERT INTO atlas_factions(
                code, name, short_name, label, enabled, created_by_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name, short_name = excluded.short_name,
                label = excluded.label, enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (
                clean_code, clean_name, clean_short, clean_short, int(bool(enabled)),
                int(actor_user_id), now, now,
            ),
        )
        row = con.execute("SELECT * FROM atlas_factions WHERE code = ?", (clean_code,)).fetchone()
        con.commit()
    return _row(row)


def atlas_create_organization(
    guild_id: int,
    actor_user_id: int,
    *,
    name: str,
    owner_user_id: int,
    slug: str | None = None,
    kind: str = "government",
    description: str | None = None,
    server_code: str = "phoenix-15",
    faction_code: str = "lspd",
) -> dict[str, Any]:
    clean_name = " ".join(str(name or "").split())[:120]
    if len(clean_name) < 2 or int(owner_user_id) <= 0:
        raise ValueError("atlas_organization_invalid")
    clean_kind = str(kind or "government").strip().lower()
    if clean_kind not in {"government", "bureau", "project", "personal"}:
        raise ValueError("atlas_organization_kind_invalid")
    clean_server, clean_faction = atlas_normalize_scope(server_code, faction_code)
    clean_slug = _slug(slug or clean_name, int(owner_user_id))
    branding = {
        "server_code": clean_server,
        "faction_code": clean_faction,
        "managed": True,
    }
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            INSERT INTO atlas_organizations(
                guild_id, slug, name, kind, owner_user_id, description,
                branding_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, slug) DO UPDATE SET
                name = excluded.name, kind = excluded.kind,
                owner_user_id = excluded.owner_user_id,
                description = excluded.description,
                branding_json = excluded.branding_json,
                status = 'active', updated_at = excluded.updated_at
            """,
            (
                int(guild_id), clean_slug, clean_name, clean_kind,
                int(owner_user_id), str(description or "").strip()[:500] or None,
                _json(branding), now, now,
            ),
        )
        organization = con.execute(
            "SELECT * FROM atlas_organizations WHERE guild_id = ? AND slug = ?",
            (int(guild_id), clean_slug),
        ).fetchone()
        organization_id = int(organization["id"])
        con.execute(
            """
            INSERT INTO atlas_memberships(
                organization_id, guild_id, user_id, display_name, role, status,
                profile_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 'owner', 'active', ?, ?, ?)
            ON CONFLICT(organization_id, user_id) DO UPDATE SET
                role = 'owner', status = 'active', profile_json = excluded.profile_json,
                updated_at = excluded.updated_at
            """,
            (
                organization_id, int(guild_id), int(owner_user_id),
                f"Discord {int(owner_user_id)}", _json(branding), now, now,
            ),
        )
        con.execute(
            """
            INSERT INTO atlas_audit_events(
                organization_id, actor_user_id, event_type, target_type,
                target_id, summary, details_json, created_at
            ) VALUES(?, ?, 'organization_configured', 'organization', ?, ?, ?, ?)
            """,
            (
                organization_id, int(actor_user_id), str(organization_id),
                f"Настроено пространство «{clean_name}»", _json(branding), now,
            ),
        )
        con.commit()
    return _row(organization)


def atlas_ensure_personal_space(
    guild_id: int,
    user_id: int,
    display_name: str,
) -> dict[str, Any]:
    """Create the safe personal sandbox used before joining an organization."""

    now = utc_now_iso()
    clean_name = str(display_name or f"Участник {int(user_id)}").strip()[:100]
    slug = f"personal-{int(user_id)}"
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            INSERT INTO atlas_organizations(
                guild_id, slug, name, kind, owner_user_id, description,
                created_at, updated_at
            ) VALUES(?, ?, ?, 'personal', ?, ?, ?, ?)
            ON CONFLICT(guild_id, slug) DO UPDATE SET
                name = excluded.name,
                updated_at = excluded.updated_at
            """,
            (
                int(guild_id),
                slug,
                f"Пространство {clean_name}"[:120],
                int(user_id),
                "Личный контур знакомства с возможностями Atlas.",
                now,
                now,
            ),
        )
        organization = con.execute(
            "SELECT * FROM atlas_organizations WHERE guild_id = ? AND slug = ?",
            (int(guild_id), slug),
        ).fetchone()
        organization_id = int(organization["id"])
        con.execute(
            """
            INSERT INTO atlas_memberships(
                organization_id, guild_id, user_id, display_name, role, status,
                last_seen_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 'owner', 'active', ?, ?, ?)
            ON CONFLICT(organization_id, user_id) DO UPDATE SET
                display_name = excluded.display_name,
                last_seen_at = excluded.last_seen_at,
                updated_at = excluded.updated_at
            """,
            (
                organization_id,
                int(guild_id),
                int(user_id),
                clean_name,
                now,
                now,
                now,
            ),
        )
        membership = con.execute(
            """
            SELECT * FROM atlas_memberships
            WHERE organization_id = ? AND user_id = ?
            """,
            (organization_id, int(user_id)),
        ).fetchone()
        con.commit()
    return {"organization": _row(organization), "membership": _row(membership)}


def atlas_ensure_system_space(guild_id: int) -> dict[str, Any]:
    """Return the non-user workspace that owns canonical shared imports."""

    now = utc_now_iso()
    slug = "atlas-system-library"
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            INSERT INTO atlas_organizations(
                guild_id, slug, name, kind, owner_user_id, description,
                created_at, updated_at
            ) VALUES(?, ?, 'Системная библиотека Atlas', 'project', 0, ?, ?, ?)
            ON CONFLICT(guild_id, slug) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (
                int(guild_id),
                slug,
                "Проверенные общие источники и автоматические синхронизации.",
                now,
                now,
            ),
        )
        organization = con.execute(
            "SELECT * FROM atlas_organizations WHERE guild_id = ? AND slug = ?",
            (int(guild_id), slug),
        ).fetchone()
        con.commit()
    return _row(organization)


def atlas_user_spaces(guild_id: int, user_id: int) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT o.*, m.role, m.status AS membership_status,
                   m.onboarding_step, m.profile_json, m.preferences_json
            FROM atlas_memberships m
            JOIN atlas_organizations o ON o.id = m.organization_id
            WHERE m.guild_id = ? AND m.user_id = ? AND m.status != 'suspended'
              AND o.status = 'active'
            ORDER BY CASE o.kind WHEN 'personal' THEN 1 ELSE 0 END,
                     CASE m.role WHEN 'owner' THEN 0 WHEN 'administrator' THEN 1 ELSE 2 END,
                     o.name
            """,
            (int(guild_id), int(user_id)),
        ).fetchall()
    return [_row(row) for row in rows]


def atlas_update_onboarding(
    organization_id: int,
    user_id: int,
    *,
    step: int,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_step = max(0, min(4, int(step)))
    clean_profile = {
        str(key)[:50]: str(value).strip()[:500]
        for key, value in dict(profile or {}).items()
        if str(value).strip()
    }
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE atlas_memberships
            SET onboarding_step = MAX(onboarding_step, ?),
                profile_json = CASE WHEN ? = '{}' THEN profile_json ELSE ? END,
                updated_at = ?, last_seen_at = ?
            WHERE organization_id = ? AND user_id = ? AND status = 'active'
            """,
            (
                selected_step,
                _json(clean_profile),
                _json(clean_profile),
                now,
                now,
                int(organization_id),
                int(user_id),
            ),
        )
        row = con.execute(
            """
            SELECT * FROM atlas_memberships
            WHERE organization_id = ? AND user_id = ?
            """,
            (int(organization_id), int(user_id)),
        ).fetchone()
        con.commit()
    if row is None:
        raise ValueError("atlas_membership_missing")
    return _row(row)


def atlas_seed_templates() -> None:
    now = utc_now_iso()
    templates = (
        (
            "official-memo",
            "Служебная записка",
            "Внутренние документы",
            "Краткое официальное обращение внутри структуры.",
            {"fields": [
                {"key": "recipient", "label": "Кому", "placeholder": "Должность, подразделение или имя"},
                {"key": "subject", "label": "Тема", "placeholder": "Краткий предмет обращения"},
                {"key": "body", "label": "Содержание", "type": "textarea", "placeholder": "Факты, предложение и ожидаемое действие"},
                {"key": "author", "label": "Автор", "placeholder": "Имя и должность"},
            ]},
            "Кому: {{recipient}}\nТема: {{subject}}\n\n{{body}}\n\n{{author}}",
        ),
        (
            "incident-report",
            "Рапорт о происшествии",
            "Рапорты",
            "Единый формат фиксации события и принятых мер.",
            {"fields": [
                {"key": "date", "label": "Дата события", "type": "date"},
                {"key": "location", "label": "Место", "placeholder": "Где произошло событие"},
                {"key": "participants", "label": "Участники", "type": "textarea", "placeholder": "Имена, должности и идентификаторы"},
                {"key": "facts", "label": "Установленные обстоятельства", "type": "textarea"},
                {"key": "actions", "label": "Принятые меры", "type": "textarea"},
            ]},
            "Дата: {{date}}\nМесто: {{location}}\nУчастники: {{participants}}\n\nОбстоятельства:\n{{facts}}\n\nПринятые меры:\n{{actions}}",
        ),
        (
            "forum-publication",
            "Публикация для форума",
            "Форум",
            "Структурированная публикация с проверкой фактов и вложений.",
            {"fields": [
                {"key": "title", "label": "Заголовок публикации"},
                {"key": "summary", "label": "Краткое введение", "type": "textarea"},
                {"key": "content", "label": "Основной текст", "type": "textarea"},
                {"key": "attachments", "label": "Вложения", "type": "textarea", "required": False, "placeholder": "Ссылки или перечень приложений"},
            ]},
            "[CENTER][B]{{title}}[/B][/CENTER]\n\n{{summary}}\n\n{{content}}\n\nВложения: {{attachments}}",
        ),
    )
    with _db_lock, connect() as con:
        for code, name, category, description, schema, body in templates:
            con.execute(
                """
                INSERT INTO atlas_document_templates(
                    organization_id, code, name, category, description,
                    schema_json, template_text, version, status,
                    created_by_id, created_at, updated_at
                )
                SELECT NULL, ?, ?, ?, ?, ?, ?, 1, 'active', 0, ?, ?
                WHERE NOT EXISTS(
                    SELECT 1 FROM atlas_document_templates
                    WHERE organization_id IS NULL AND code = ? AND version = 1
                )
                """,
                (
                    code,
                    name,
                    category,
                    description,
                    _json(schema),
                    body,
                    now,
                    now,
                    code,
                ),
            )
            con.execute(
                """
                UPDATE atlas_document_templates
                SET name = ?, category = ?, description = ?, schema_json = ?,
                    template_text = ?, updated_at = ?
                WHERE organization_id IS NULL AND code = ? AND version = 1
                """,
                (name, category, description, _json(schema), body, now, code),
            )
        con.commit()


def atlas_templates(organization_id: int) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT * FROM atlas_document_templates
            WHERE status = 'active' AND (organization_id IS NULL OR organization_id = ?)
            ORDER BY category, name, version DESC
            """,
            (int(organization_id),),
        ).fetchall()
    items = [_row(row) for row in rows]
    for item in items:
        item["schema"] = normalize_document_schema(item.get("schema"))
    return items


def atlas_documents(organization_id: int, *, limit: int = 30) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT d.*, t.name AS template_name, t.category AS template_category
            FROM atlas_documents d
            LEFT JOIN atlas_document_templates t ON t.id = d.template_id
            WHERE d.organization_id = ?
            ORDER BY d.updated_at DESC, d.id DESC LIMIT ?
            """,
            (int(organization_id), max(1, min(200, int(limit)))),
        ).fetchall()
    return [_row(row) for row in rows]


def atlas_create_document(
    organization_id: int,
    user_id: int,
    *,
    title: str,
    template_id: int | None,
    fields: dict[str, Any],
    rendered_text: str = "",
) -> dict[str, Any]:
    clean_title = str(title or "").strip()[:180]
    if not clean_title:
        raise ValueError("atlas_document_title_required")
    clean_fields: dict[str, str]
    clean_text = str(rendered_text or "")[:100000]
    now = utc_now_iso()
    with _db_lock, connect() as con:
        membership = con.execute(
            """
            SELECT role FROM atlas_memberships
            WHERE organization_id = ? AND user_id = ? AND status = 'active'
            """,
            (int(organization_id), int(user_id)),
        ).fetchone()
        if membership is None or str(membership["role"]) == "viewer":
            raise ValueError("atlas_document_forbidden")
        if template_id:
            template = con.execute(
                """
                SELECT id, schema_json, template_text FROM atlas_document_templates
                WHERE id = ? AND status = 'active'
                  AND (organization_id IS NULL OR organization_id = ?)
                """,
                (int(template_id), int(organization_id)),
            ).fetchone()
            if template is None:
                raise ValueError("atlas_document_template_forbidden")
            clean_fields, clean_text = prepare_document_content(
                _decoded(template["schema_json"], {}),
                str(template["template_text"] or ""),
                fields,
                fallback_text=clean_text,
            )
        else:
            clean_fields, clean_text = prepare_document_content(
                {}, "", fields, fallback_text=clean_text
            )
        cursor = con.execute(
            """
            INSERT INTO atlas_documents(
                organization_id, template_id, author_user_id, title,
                fields_json, rendered_text, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(organization_id),
                int(template_id) if template_id else None,
                int(user_id),
                clean_title,
                _json(clean_fields),
                clean_text,
                now,
                now,
            ),
        )
        document_id = int(cursor.lastrowid)
        revision_checksum = hashlib.sha256(
            _json(
                {
                    "title": clean_title,
                    "fields": clean_fields,
                    "rendered_text": clean_text,
                }
            ).encode("utf-8")
        ).hexdigest()
        con.execute(
            """
            INSERT INTO atlas_document_revisions(
                organization_id, document_id, revision, created_by_id,
                title, fields_json, rendered_text, change_summary,
                checksum_sha256, created_at
            ) VALUES(?, ?, 1, ?, ?, ?, ?, 'Первый черновик', ?, ?)
            """,
            (
                int(organization_id), document_id, int(user_id), clean_title,
                _json(clean_fields), clean_text,
                revision_checksum, now,
            ),
        )
        con.execute(
            """
            INSERT INTO atlas_audit_events(
                organization_id, actor_user_id, event_type, target_type,
                target_id, summary, created_at
            ) VALUES(?, ?, 'document_created', 'document', ?, ?, ?)
            """,
            (int(organization_id), int(user_id), str(document_id), f"Создан документ «{clean_title}»", now),
        )
        con.execute(
            """
            INSERT INTO atlas_timeline_events(
                organization_id, actor_user_id, event_kind, title, summary,
                status, importance, occurred_at, source_type, source_id,
                dedupe_key, created_at, updated_at
            ) VALUES(?, ?, 'document', ?, ?, 'active', 'routine', ?,
                     'document', ?, ?, ?, ?)
            ON CONFLICT(organization_id, dedupe_key) DO NOTHING
            """,
            (
                int(organization_id), int(user_id), f"Создан документ «{clean_title}»",
                "Черновик появился в рабочем пространстве Atlas.", now,
                str(document_id), f"document-created:{document_id}", now, now,
            ),
        )
        row = con.execute("SELECT * FROM atlas_documents WHERE id = ?", (document_id,)).fetchone()
        con.commit()
    return _row(row)


def atlas_add_knowledge(
    organization_id: int,
    user_id: int,
    *,
    title: str,
    content: str,
    source_kind: str = "memo",
    source_url: str | None = None,
    server_code: str = "phoenix-15",
    faction_code: str = "lspd",
    visibility_scope: str = "workspace",
    original_filename: str | None = None,
    knowledge_domain: str | None = None,
    corpus_kind: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_title = str(title or "").strip()[:180]
    clean_content = str(content or "").strip()[:250000]
    if not clean_title or len(clean_content) < 20:
        raise ValueError("atlas_knowledge_invalid")
    clean_kind = str(source_kind or "memo").strip().lower()
    if clean_kind not in {"document", "forum", "memo", "regulation", "manual", "url"}:
        clean_kind = "memo"
    clean_server, clean_faction = atlas_normalize_scope(server_code, faction_code)
    clean_visibility = atlas_normalize_knowledge_scope(visibility_scope)
    clean_filename = str(original_filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1][:240] or None
    taxonomy = atlas_classify_knowledge(
        title=clean_title,
        content=clean_content,
        source_url=source_url,
        source_kind=clean_kind,
        domain_hint=knowledge_domain,
        corpus_hint=corpus_kind,
    )
    clean_metadata = {**dict(metadata or {}), "taxonomy": taxonomy}
    checksum = _knowledge_checksum(
        clean_visibility,
        clean_server,
        clean_faction,
        clean_content,
    )
    now = utc_now_iso()
    with _db_lock, connect() as con:
        membership = con.execute(
            """
            SELECT role FROM atlas_memberships
            WHERE organization_id = ? AND user_id = ? AND status = 'active'
            """,
            (int(organization_id), int(user_id)),
        ).fetchone()
        if membership is None or str(membership["role"]) not in {"owner", "administrator", "editor"}:
            raise ValueError("atlas_knowledge_forbidden")
        con.execute(
            """
            INSERT INTO atlas_knowledge_sources(
                organization_id, server_code, faction_code, visibility_scope,
                title, source_kind,
                source_url, content_text, checksum, original_filename,
                metadata_json, created_by_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(organization_id, checksum) DO UPDATE SET
                title = excluded.title,
                source_url = excluded.source_url,
                original_filename = excluded.original_filename,
                metadata_json = excluded.metadata_json,
                status = 'pending',
                last_error = NULL,
                updated_at = excluded.updated_at
            """,
            (
                int(organization_id),
                clean_server,
                clean_faction,
                clean_visibility,
                clean_title,
                clean_kind,
                str(source_url or "").strip()[:1000] or None,
                clean_content,
                checksum,
                clean_filename,
                _json(clean_metadata),
                int(user_id),
                now,
                now,
            ),
        )
        row = con.execute(
            """
            SELECT * FROM atlas_knowledge_sources
            WHERE organization_id = ? AND checksum = ?
            """,
            (int(organization_id), checksum),
        ).fetchone()
        con.execute(
            """
            INSERT INTO atlas_audit_events(
                organization_id, actor_user_id, event_type, target_type,
                target_id, summary, created_at
            ) VALUES(?, ?, 'knowledge_queued', 'knowledge', ?, ?, ?)
            """,
            (
                int(organization_id),
                int(user_id),
                str(row["id"]),
                f"Источник «{clean_title}» поставлен на индексацию",
                now,
            ),
        )
        con.execute(
            """
            INSERT INTO atlas_timeline_events(
                organization_id, actor_user_id, event_kind, title, summary,
                status, importance, occurred_at, source_type, source_id,
                dedupe_key, created_at, updated_at
            ) VALUES(?, ?, 'system', ?, ?, 'active', 'routine', ?,
                     'knowledge_source', ?, ?, ?, ?)
            ON CONFLICT(organization_id, dedupe_key) DO NOTHING
            """,
            (
                int(organization_id), int(user_id), f"Добавлен источник «{clean_title}»",
                "Материал поставлен на индексацию и станет частью разрешённой памяти Atlas.",
                now, str(row["id"]), f"knowledge-created:{int(row['id'])}", now, now,
            ),
        )
        con.commit()
    return _row(row)


def atlas_knowledge_sources(
    organization_id: int,
    *,
    server_code: str | None = None,
    faction_code: str | None = None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    clauses = ["status != 'archived'"]
    params: list[Any] = []
    if server_code is not None or faction_code is not None:
        clean_server, clean_faction = atlas_normalize_scope(
            server_code or "phoenix-15",
            faction_code or "lspd",
        )
        clauses.append(
            """(
                visibility_scope = 'global'
                OR (visibility_scope = 'server' AND server_code = ?)
                OR (visibility_scope = 'faction' AND server_code = ? AND faction_code = ?)
                OR (visibility_scope = 'workspace' AND organization_id = ?
                    AND server_code = ? AND faction_code = ?)
            )"""
        )
        params.extend(
            [
                clean_server,
                clean_server,
                clean_faction,
                int(organization_id),
                clean_server,
                clean_faction,
            ]
        )
    else:
        clauses.append("organization_id = ?")
        params.append(int(organization_id))
    params.append(max(1, min(200, int(limit))))
    with connect_readonly() as con:
        rows = con.execute(
            f"""
            SELECT id, organization_id, server_code, faction_code,
                   visibility_scope, title,
                   source_kind, source_url, checksum, status, original_filename,
                   metadata_json, created_by_id, indexed_at, last_error,
                   created_at, updated_at
            FROM atlas_knowledge_sources
            WHERE {' AND '.join(clauses)}
            ORDER BY id DESC LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_row(row) for row in rows]


def atlas_knowledge_source(source_id: int) -> dict[str, Any] | None:
    """Return one canonical source, including its text, for background work."""

    with connect_readonly() as con:
        row = con.execute(
            "SELECT * FROM atlas_knowledge_sources WHERE id = ?",
            (int(source_id),),
        ).fetchone()
    return _row(row) if row is not None else None


def atlas_searchable_knowledge_sources(
    organization_id: int,
    *,
    server_code: str = "phoenix-15",
    faction_code: str = "lspd",
    limit: int = 300,
) -> list[dict[str, Any]]:
    """Return accessible canonical text for the local half of hybrid search."""

    clean_server, clean_faction = atlas_normalize_scope(server_code, faction_code)
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT * FROM atlas_knowledge_sources
            WHERE status != 'archived'
              AND length(trim(content_text)) >= 20
              AND (
                visibility_scope = 'global'
                OR (visibility_scope = 'server' AND server_code = ?)
                OR (visibility_scope = 'faction' AND server_code = ? AND faction_code = ?)
                OR (visibility_scope = 'workspace' AND organization_id = ?
                    AND server_code = ? AND faction_code = ?)
              )
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (
                clean_server,
                clean_server,
                clean_faction,
                int(organization_id),
                clean_server,
                clean_faction,
                max(1, min(1_000, int(limit))),
            ),
        ).fetchall()
    return [_row(row) for row in rows]


def atlas_indexable_knowledge_sources(*, limit: int = 500) -> list[dict[str, Any]]:
    """Return canonical source text for rebuilding the derived search index."""

    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT * FROM atlas_knowledge_sources
            WHERE status != 'archived'
              AND length(trim(content_text)) >= 20
            ORDER BY id ASC
            LIMIT ?
            """,
            (max(1, min(2_000, int(limit))),),
        ).fetchall()
    return [_row(row) for row in rows]


def atlas_archive_duplicate_forum_sources() -> dict[str, int]:
    """Archive obsolete /unread copies while retaining the richest canonical source."""

    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT id, organization_id, source_url, content_text, updated_at
            FROM atlas_knowledge_sources
            WHERE status != 'archived' AND source_url IS NOT NULL
              AND source_url LIKE 'https://forum.majestic-rp.ru/threads/%'
            ORDER BY id ASC
            """
        ).fetchall()
        groups: dict[tuple[int, str], list[Any]] = {}
        for row in rows:
            url = re.sub(
                r"/(?:unread|latest)/?$",
                "/",
                str(row["source_url"] or "").strip(),
                flags=re.IGNORECASE,
            )
            groups.setdefault((int(row["organization_id"]), url), []).append(row)
        archived = 0
        for group in groups.values():
            if len(group) < 2:
                continue
            keep = max(
                group,
                key=lambda row: (
                    len(str(row["content_text"] or "")),
                    not bool(re.search(r"/(?:unread|latest)/?$", str(row["source_url"] or ""), re.I)),
                    str(row["updated_at"] or ""),
                    int(row["id"]),
                ),
            )
            duplicate_ids = [int(row["id"]) for row in group if int(row["id"]) != int(keep["id"])]
            if not duplicate_ids:
                continue
            placeholders = ",".join("?" for _ in duplicate_ids)
            con.execute(
                f"UPDATE atlas_knowledge_sources SET status = 'archived', updated_at = ? "
                f"WHERE id IN ({placeholders})",
                (utc_now_iso(), *duplicate_ids),
            )
            archived += len(duplicate_ids)
        con.commit()
    return {"groups": sum(len(group) > 1 for group in groups.values()), "archived": archived}


def atlas_mark_knowledge_indexed(
    source_id: int,
    *,
    point_id: str | None,
    error: str | None = None,
) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        indexed_at_assignment = "indexed_at = ?" if error is None else "indexed_at = indexed_at"
        params: list[Any] = [
            "failed" if error else "indexed",
            point_id,
        ]
        if error is None:
            params.append(now)
        params.extend(
            (
                str(error or "")[:2000] or None,
                now,
                int(source_id),
            )
        )
        con.execute(
            f"""
            UPDATE atlas_knowledge_sources
            SET status = ?, qdrant_point_id = COALESCE(?, qdrant_point_id),
                {indexed_at_assignment},
                last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            params,
        )
        con.commit()


def atlas_ensure_forum_feed(
    guild_id: int,
    *,
    feed_key: str,
    root_url: str,
    server_code: str = "phoenix-15",
    faction_code: str = "lspd",
    visibility_scope: str = "server",
    interval_seconds: int = 43_200,
) -> dict[str, Any]:
    organization = atlas_ensure_system_space(guild_id)
    clean_server, clean_faction = atlas_normalize_scope(server_code, faction_code)
    clean_visibility = atlas_normalize_knowledge_scope(visibility_scope)
    clean_key = str(feed_key or "").strip().lower()[:80]
    clean_url = str(root_url or "").strip()[:2000]
    if not clean_key or not clean_url.startswith(("https://", "http://")):
        raise ValueError("atlas_forum_feed_invalid")
    interval = max(3600, min(604_800, int(interval_seconds)))
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            INSERT INTO atlas_forum_feeds(
                guild_id, organization_id, feed_key, root_url, server_code,
                faction_code, visibility_scope, interval_seconds,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, feed_key) DO UPDATE SET
                organization_id = excluded.organization_id,
                root_url = excluded.root_url,
                server_code = excluded.server_code,
                faction_code = excluded.faction_code,
                visibility_scope = excluded.visibility_scope,
                interval_seconds = excluded.interval_seconds,
                status = CASE
                    WHEN atlas_forum_feeds.status = 'disabled' THEN 'pending'
                    ELSE atlas_forum_feeds.status
                END,
                updated_at = excluded.updated_at
            """,
            (
                int(guild_id),
                int(organization["id"]),
                clean_key,
                clean_url,
                clean_server,
                clean_faction,
                clean_visibility,
                interval,
                now,
                now,
            ),
        )
        row = con.execute(
            "SELECT * FROM atlas_forum_feeds WHERE guild_id = ? AND feed_key = ?",
            (int(guild_id), clean_key),
        ).fetchone()
        con.commit()
    return _row(row)


def atlas_forum_sync_started(feed_id: int) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE atlas_forum_feeds
            SET status = 'running', last_started_at = ?, last_error = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, int(feed_id)),
        )
        row = con.execute(
            "SELECT * FROM atlas_forum_feeds WHERE id = ?",
            (int(feed_id),),
        ).fetchone()
        con.commit()
    if row is None:
        raise ValueError("atlas_forum_feed_missing")
    return _row(row)


def atlas_forum_sync_finished(
    feed_id: int,
    *,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
    attention: bool = False,
) -> dict[str, Any]:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with _db_lock, connect() as con:
        feed = con.execute(
            "SELECT * FROM atlas_forum_feeds WHERE id = ?",
            (int(feed_id),),
        ).fetchone()
        if feed is None:
            raise ValueError("atlas_forum_feed_missing")
        next_sync = (
            now_dt + timedelta(seconds=max(3600, int(feed["interval_seconds"])))
        ).isoformat()
        status = "attention" if attention else ("error" if error else "ok")
        success_assignment = (
            "last_success_at = last_success_at" if error else "last_success_at = ?"
        )
        params: list[Any] = [status]
        if not error:
            params.append(now)
        params.extend(
            (
                next_sync,
                str(error or "")[:4000] or None,
                _json(stats or {}),
                now,
                int(feed_id),
            )
        )
        con.execute(
            f"""
            UPDATE atlas_forum_feeds
            SET status = ?, {success_assignment},
                next_sync_at = ?, last_error = ?, last_stats_json = ?, updated_at = ?
            WHERE id = ?
            """,
            params,
        )
        row = con.execute(
            "SELECT * FROM atlas_forum_feeds WHERE id = ?",
            (int(feed_id),),
        ).fetchone()
        con.commit()
    return _row(row)


def atlas_forum_sync_status(guild_id: int) -> dict[str, Any] | None:
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT * FROM atlas_forum_feeds
            WHERE guild_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (int(guild_id),),
        ).fetchone()
    return _row(row) if row is not None else None


def atlas_upsert_synced_knowledge(
    organization_id: int,
    *,
    title: str,
    content: str,
    source_url: str,
    server_code: str,
    faction_code: str,
    visibility_scope: str,
    feed_key: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_title = str(title or "").strip()[:180]
    clean_content = str(content or "").strip()[:250000]
    clean_url = str(source_url or "").strip()[:2000]
    if not clean_title or len(clean_content) < 20 or not clean_url.startswith(("https://", "http://")):
        raise ValueError("atlas_synced_knowledge_invalid")
    clean_server, clean_faction = atlas_normalize_scope(server_code, faction_code)
    clean_visibility = atlas_normalize_knowledge_scope(visibility_scope)
    checksum = _knowledge_checksum(
        clean_visibility,
        clean_server,
        clean_faction,
        clean_content,
        identity=clean_url,
    )
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            """
            SELECT * FROM atlas_knowledge_sources
            WHERE organization_id = ? AND source_kind = 'forum' AND source_url = ?
            ORDER BY id DESC LIMIT 1
            """,
            (int(organization_id), clean_url),
        ).fetchone()
        current_metadata = _decoded(existing["metadata_json"], {}) if existing else {}
        current_revision = max(1, int(current_metadata.get("revision") or 1))
        taxonomy = atlas_classify_knowledge(
            title=clean_title,
            content=clean_content,
            source_url=clean_url,
            source_kind="forum",
            domain_hint=str(dict(metadata or {}).get("knowledge_domain") or "") or None,
            corpus_hint=str(dict(metadata or {}).get("corpus_kind") or "") or None,
        )
        merged_metadata = {
            **current_metadata,
            **dict(metadata or {}),
            "taxonomy": taxonomy,
            "sync_feed": str(feed_key)[:80],
            "last_seen_at": now,
            "missing_runs": 0,
            "missing_from_feed": False,
            "revision": current_revision,
        }
        created = existing is None
        changed = (
            created
            or str(existing["checksum"]) != checksum
            or str(existing["title"]) != clean_title
        )
        if existing is None:
            cursor = con.execute(
                """
                INSERT INTO atlas_knowledge_sources(
                    organization_id, server_code, faction_code, visibility_scope,
                    title, source_kind, source_url, content_text, checksum,
                    status, metadata_json, created_by_id, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'forum', ?, ?, ?, 'pending', ?, 0, ?, ?)
                """,
                (
                    int(organization_id), clean_server, clean_faction,
                    clean_visibility, clean_title, clean_url, clean_content,
                    checksum, _json(merged_metadata), now, now,
                ),
            )
            source_id = int(cursor.lastrowid)
        else:
            source_id = int(existing["id"])
            if changed:
                con.execute(
                    """
                    INSERT OR IGNORE INTO atlas_knowledge_revisions(
                        source_id, revision, title, content_text, checksum,
                        source_url, captured_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        current_revision,
                        str(existing["title"]),
                        str(existing["content_text"]),
                        str(existing["checksum"]),
                        str(existing["source_url"] or "") or None,
                        now,
                    ),
                )
                merged_metadata["revision"] = current_revision + 1
                con.execute(
                    """
                    UPDATE atlas_knowledge_sources
                    SET server_code = ?, faction_code = ?, visibility_scope = ?,
                        title = ?, content_text = ?, checksum = ?, status = 'pending',
                        qdrant_point_id = NULL, indexed_at = NULL, last_error = NULL,
                        metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        clean_server, clean_faction, clean_visibility, clean_title,
                        clean_content, checksum, _json(merged_metadata), now, source_id,
                    ),
                )
            else:
                con.execute(
                    """
                    UPDATE atlas_knowledge_sources
                    SET title = ?, metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (clean_title, _json(merged_metadata), now, source_id),
                )
        if changed:
            con.execute(
                """
                INSERT INTO atlas_audit_events(
                    organization_id, actor_user_id, event_type, target_type,
                    target_id, summary, details_json, created_at
                ) VALUES(?, 0, 'forum_source_updated', 'knowledge', ?, ?, ?, ?)
                """,
                (
                    int(organization_id),
                    str(source_id),
                    f"Atlas обновил материал форума «{clean_title}»",
                    _json({"source_url": clean_url, "created": created}),
                    now,
                ),
            )
            revision = int(merged_metadata.get("revision") or 1)
            con.execute(
                """
                INSERT INTO atlas_timeline_events(
                    organization_id, actor_user_id, event_kind, title, summary,
                    status, importance, occurred_at, source_type, source_id,
                    metadata_json, dedupe_key, created_at, updated_at
                ) VALUES(?, 0, 'system', ?, ?, 'resolved', 'routine', ?,
                         'knowledge_source', ?, ?, ?, ?, ?)
                ON CONFLICT(organization_id, dedupe_key) DO NOTHING
                """,
                (
                    int(organization_id),
                    ("Получен материал форума" if created else "Обновлён материал форума")
                    + f" «{clean_title}»",
                    "Atlas сохранил новую проверяемую редакцию источника.",
                    now,
                    str(source_id),
                    _json({"source_url": clean_url, "revision": revision}),
                    f"forum-source:{source_id}:revision:{revision}",
                    now,
                    now,
                ),
            )
        row = con.execute(
            "SELECT * FROM atlas_knowledge_sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        con.commit()
    return {"source": _row(row), "created": created, "changed": changed}


def atlas_mark_forum_sources_seen(
    organization_id: int,
    *,
    feed_key: str,
    seen_urls: list[str] | tuple[str, ...] | set[str],
) -> int:
    """Mark missing pages for review without deleting or de-indexing them."""

    seen = {str(item).strip() for item in seen_urls if str(item).strip()}
    changed = 0
    now = utc_now_iso()
    with _db_lock, connect() as con:
        rows = con.execute(
            """
            SELECT * FROM atlas_knowledge_sources
            WHERE organization_id = ? AND source_kind = 'forum'
            """,
            (int(organization_id),),
        ).fetchall()
        for row in rows:
            details = _decoded(row["metadata_json"], {})
            if str(details.get("sync_feed") or "") != str(feed_key):
                continue
            url = str(row["source_url"] or "")
            missing = url not in seen
            runs = int(details.get("missing_runs") or 0) + 1 if missing else 0
            previous = bool(details.get("missing_from_feed"))
            details.update(
                {
                    "missing_runs": runs,
                    "missing_from_feed": missing,
                    "last_inventory_at": now,
                }
            )
            if missing != previous:
                changed += 1
            con.execute(
                "UPDATE atlas_knowledge_sources SET metadata_json = ?, updated_at = ? WHERE id = ?",
                (_json(details), now, int(row["id"])),
            )
        con.commit()
    return changed


def atlas_knowledge_revisions(source_id: int) -> list[dict[str, Any]]:
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT * FROM atlas_knowledge_revisions
            WHERE source_id = ? ORDER BY revision DESC
            """,
            (int(source_id),),
        ).fetchall()
    return [_row(row) for row in rows]


def atlas_create_thread(
    organization_id: int,
    user_id: int,
    title: str,
    *,
    agent_id: str = "atlas-tvr-a",
) -> int:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            INSERT INTO atlas_ai_threads(
                organization_id, user_id, agent_id, title, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                int(organization_id), int(user_id), str(agent_id or "atlas-tvr-a")[:80],
                str(title or "Новый диалог")[:120], now, now,
            ),
        )
        con.commit()
        return int(cursor.lastrowid)


def atlas_add_message(
    thread_id: int,
    role: str,
    content: str,
    *,
    citations: list[dict[str, Any]] | None = None,
    model: str | None = None,
    latency_ms: int | None = None,
) -> int:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            INSERT INTO atlas_ai_messages(
                thread_id, role, content_text, citations_json, model,
                latency_ms, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(thread_id),
                str(role),
                str(content)[:100000],
                _json(citations or []),
                str(model or "")[:160] or None,
                int(latency_ms) if latency_ms is not None else None,
                now,
            ),
        )
        con.execute(
            "UPDATE atlas_ai_threads SET updated_at = ? WHERE id = ?",
            (now, int(thread_id)),
        )
        con.commit()
        return int(cursor.lastrowid)


def atlas_set_message_feedback(
    organization_id: int,
    user_id: int,
    message_id: int,
    rating: str,
    *,
    comment: str | None = None,
) -> dict[str, Any]:
    selected = str(rating or "").strip().lower()
    if selected not in {"good", "bad"}:
        raise ValueError("atlas_feedback_rating_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        message = con.execute(
            """
            SELECT m.id, m.thread_id FROM atlas_ai_messages m
            JOIN atlas_ai_threads t ON t.id = m.thread_id
            WHERE m.id = ? AND m.role = 'assistant'
              AND t.organization_id = ? AND t.user_id = ? AND t.status = 'active'
            """,
            (int(message_id), int(organization_id), int(user_id)),
        ).fetchone()
        if message is None:
            raise ValueError("atlas_feedback_message_not_found")
        con.execute(
            """
            INSERT INTO atlas_ai_feedback(
                organization_id, thread_id, message_id, user_id, rating,
                comment_text, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id, user_id) DO UPDATE SET
                rating = excluded.rating,
                comment_text = excluded.comment_text,
                updated_at = excluded.updated_at
            """,
            (
                int(organization_id), int(message["thread_id"]), int(message_id),
                int(user_id), selected, str(comment or "").strip()[:2000] or None,
                now, now,
            ),
        )
        row = con.execute(
            "SELECT * FROM atlas_ai_feedback WHERE message_id = ? AND user_id = ?",
            (int(message_id), int(user_id)),
        ).fetchone()
        con.commit()
    return _row(row)


def atlas_bind_discord_thread(
    *,
    discord_thread_id: int,
    guild_id: int,
    parent_channel_id: int,
    organization_id: int,
    atlas_thread_id: int,
    owner_user_id: int,
) -> dict[str, Any]:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            INSERT INTO atlas_discord_threads(
                discord_thread_id, guild_id, parent_channel_id, organization_id,
                atlas_thread_id, owner_user_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_thread_id) DO UPDATE SET
                status = 'active', updated_at = excluded.updated_at
            """,
            (
                int(discord_thread_id), int(guild_id), int(parent_channel_id),
                int(organization_id), int(atlas_thread_id), int(owner_user_id), now, now,
            ),
        )
        row = con.execute(
            "SELECT * FROM atlas_discord_threads WHERE discord_thread_id = ?",
            (int(discord_thread_id),),
        ).fetchone()
        con.commit()
    return _row(row)


def atlas_discord_thread(discord_thread_id: int) -> dict[str, Any] | None:
    with connect_readonly() as con:
        row = con.execute(
            """
            SELECT * FROM atlas_discord_threads
            WHERE discord_thread_id = ? AND status = 'active'
            """,
            (int(discord_thread_id),),
        ).fetchone()
    return _row(row) if row is not None else None


def atlas_record_event(
    organization_id: int,
    actor_user_id: int,
    event_type: str,
    summary: str,
    *,
    target_type: str | None = None,
    target_id: str | int | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            INSERT INTO atlas_audit_events(
                organization_id, actor_user_id, event_type, target_type,
                target_id, summary, details_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(organization_id), int(actor_user_id), str(event_type)[:80],
                str(target_type)[:80] if target_type else None,
                str(target_id)[:160] if target_id is not None else None,
                str(summary)[:500], _json(details or {}), utc_now_iso(),
            ),
        )
        con.commit()
        return int(cursor.lastrowid)


def atlas_create_timeline_event(
    organization_id: int,
    actor_user_id: int,
    *,
    title: str,
    summary: str = "",
    event_kind: str = "activity",
    status: str = "open",
    importance: str = "routine",
    occurred_at: str | None = None,
    source_type: str | None = None,
    source_id: str | int | None = None,
    metadata: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
) -> dict[str, Any]:
    """Persist one user-visible moment in the workspace continuity timeline."""

    clean_title = " ".join(str(title or "").split())[:180]
    clean_summary = str(summary or "").strip()[:12_000]
    clean_kind = str(event_kind or "activity").strip().lower()
    clean_status = str(status or "open").strip().lower()
    clean_importance = str(importance or "routine").strip().lower()
    if len(clean_title) < 2:
        raise ValueError("atlas_timeline_title_required")
    if clean_kind not in _TIMELINE_KINDS:
        raise ValueError("atlas_timeline_kind_invalid")
    if clean_status not in _TIMELINE_STATUSES:
        raise ValueError("atlas_timeline_status_invalid")
    if clean_importance not in _TIMELINE_IMPORTANCE:
        raise ValueError("atlas_timeline_importance_invalid")
    clean_source_type = str(source_type or "").strip().lower()[:80] or None
    clean_source_id = _entity_identity(source_id) if source_id is not None else None
    if bool(clean_source_type) != bool(clean_source_id):
        raise ValueError("atlas_timeline_source_incomplete")
    clean_dedupe = str(dedupe_key or "").strip()[:160] or None
    moment = _timeline_time(occurred_at)
    now = utc_now_iso()
    with _db_lock, connect() as con:
        membership = con.execute(
            """
            SELECT role FROM atlas_memberships
            WHERE organization_id = ? AND user_id = ? AND status = 'active'
            """,
            (int(organization_id), int(actor_user_id)),
        ).fetchone()
        if membership is None or str(membership["role"]) == "viewer":
            raise ValueError("atlas_timeline_forbidden")
        if clean_source_type and not _atlas_entity_exists(
            con,
            int(organization_id),
            clean_source_type,
            clean_source_id or "",
            actor_user_id=int(actor_user_id),
        ):
            raise ValueError("atlas_timeline_source_not_found")
        con.execute(
            """
            INSERT INTO atlas_timeline_events(
                organization_id, actor_user_id, event_kind, title, summary,
                status, importance, occurred_at, source_type, source_id,
                metadata_json, dedupe_key, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(organization_id, dedupe_key) DO NOTHING
            """,
            (
                int(organization_id), int(actor_user_id), clean_kind, clean_title,
                clean_summary, clean_status, clean_importance, moment,
                clean_source_type, clean_source_id, _json(dict(metadata or {})),
                clean_dedupe, now, now,
            ),
        )
        if clean_dedupe:
            row = con.execute(
                """
                SELECT * FROM atlas_timeline_events
                WHERE organization_id = ? AND dedupe_key = ?
                """,
                (int(organization_id), clean_dedupe),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM atlas_timeline_events WHERE id = last_insert_rowid()"
            ).fetchone()
        con.commit()
    if row is None:
        raise ValueError("atlas_timeline_create_failed")
    return _row(row)


def atlas_update_timeline_event(
    organization_id: int,
    actor_user_id: int,
    event_id: int,
    *,
    title: Any = _UNSET,
    summary: Any = _UNSET,
    status: Any = _UNSET,
    importance: Any = _UNSET,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Update a timeline moment with optimistic concurrency protection."""

    assignments: list[str] = []
    params: list[Any] = []
    if title is not _UNSET:
        clean_title = " ".join(str(title or "").split())[:180]
        if len(clean_title) < 2:
            raise ValueError("atlas_timeline_title_required")
        assignments.append("title = ?")
        params.append(clean_title)
    if summary is not _UNSET:
        assignments.append("summary = ?")
        params.append(str(summary or "").strip()[:12_000])
    clean_status: str | None = None
    if status is not _UNSET:
        clean_status = str(status or "").strip().lower()
        if clean_status not in _TIMELINE_STATUSES:
            raise ValueError("atlas_timeline_status_invalid")
        assignments.append("status = ?")
        params.append(clean_status)
    if importance is not _UNSET:
        clean_importance = str(importance or "").strip().lower()
        if clean_importance not in _TIMELINE_IMPORTANCE:
            raise ValueError("atlas_timeline_importance_invalid")
        assignments.append("importance = ?")
        params.append(clean_importance)

    with _db_lock, connect() as con:
        membership = con.execute(
            """
            SELECT role FROM atlas_memberships
            WHERE organization_id = ? AND user_id = ? AND status = 'active'
            """,
            (int(organization_id), int(actor_user_id)),
        ).fetchone()
        if membership is None or str(membership["role"]) == "viewer":
            raise ValueError("atlas_timeline_forbidden")
        current = con.execute(
            "SELECT * FROM atlas_timeline_events WHERE id = ? AND organization_id = ?",
            (int(event_id), int(organization_id)),
        ).fetchone()
        if current is None:
            raise ValueError("atlas_timeline_not_found")
        current_version = int(current["version"] or 1)
        if expected_version is not None and int(expected_version) != current_version:
            raise ValueError("atlas_timeline_version_conflict")
        if not assignments:
            return _row(current)
        now = utc_now_iso()
        if clean_status == "resolved":
            assignments.append("resolved_at = COALESCE(resolved_at, ?)")
            params.append(now)
        elif clean_status is not None:
            assignments.append("resolved_at = NULL")
        assignments.extend(("version = version + 1", "updated_at = ?"))
        params.append(now)
        params.extend((int(event_id), int(organization_id), current_version))
        cursor = con.execute(
            f"""
            UPDATE atlas_timeline_events
            SET {', '.join(assignments)}
            WHERE id = ? AND organization_id = ? AND version = ?
            """,
            tuple(params),
        )
        if int(cursor.rowcount or 0) != 1:
            raise ValueError("atlas_timeline_version_conflict")
        row = con.execute(
            "SELECT * FROM atlas_timeline_events WHERE id = ?",
            (int(event_id),),
        ).fetchone()
        con.commit()
    return _row(row)


def atlas_timeline_page(
    organization_id: int,
    *,
    actor_user_id: int | None = None,
    status: str | None = None,
    limit: int = 80,
    cursor: str | int | None = None,
) -> dict[str, Any]:
    selected_status = str(status or "").strip().lower()
    if selected_status and selected_status not in _TIMELINE_STATUSES:
        raise ValueError("atlas_timeline_status_invalid")
    selected_limit = max(1, min(100, int(limit)))
    params: list[Any] = [int(organization_id)]
    status_filter = ""
    if selected_status:
        status_filter = "AND e.status = ?"
        params.append(selected_status)
    privacy_filter = ""
    if actor_user_id is not None:
        privacy_filter = """
        AND (
          COALESCE(e.source_type, '') NOT IN ('case', 'media_asset')
          OR (e.source_type = 'case' AND EXISTS(
            SELECT 1 FROM atlas_cases c
            WHERE c.id = CAST(e.source_id AS INTEGER)
              AND c.organization_id = e.organization_id
              AND (c.visibility_scope = 'workspace' OR c.created_by_id = ? OR c.assigned_to_id = ?)
          ))
          OR (e.source_type = 'media_asset' AND EXISTS(
            SELECT 1 FROM atlas_media_assets a
            WHERE a.id = CAST(e.source_id AS INTEGER)
              AND a.organization_id = e.organization_id
              AND (a.visibility_scope = 'workspace' OR a.owner_user_id = ?)
          ))
        )
        """
        params.extend((int(actor_user_id), int(actor_user_id), int(actor_user_id)))
    cursor_filter = ""
    with connect_readonly() as con:
        if cursor is not None and str(cursor).strip():
            cursor_id = _entity_identity(cursor)
            cursor_row = con.execute(
                """
                SELECT occurred_at, id FROM atlas_timeline_events
                WHERE organization_id = ? AND id = ?
                """,
                (int(organization_id), int(cursor_id)),
            ).fetchone()
            if cursor_row is None:
                raise ValueError("atlas_timeline_cursor_invalid")
            cursor_filter = "AND (e.occurred_at < ? OR (e.occurred_at = ? AND e.id < ?))"
            params.extend(
                (str(cursor_row["occurred_at"]), str(cursor_row["occurred_at"]), int(cursor_id))
            )
        params.append(selected_limit + 1)
        rows = con.execute(
            f"""
            SELECT e.*, m.display_name AS actor_display_name
            FROM atlas_timeline_events e
            LEFT JOIN atlas_memberships m
              ON m.organization_id = e.organization_id
             AND m.user_id = e.actor_user_id
            WHERE e.organization_id = ? {status_filter} {privacy_filter} {cursor_filter}
            ORDER BY e.occurred_at DESC, e.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    has_more = len(rows) > selected_limit
    items = [_row(row) for row in rows[:selected_limit]]
    return {
        "items": items,
        "next_cursor": str(items[-1]["id"]) if has_more and items else None,
    }


def atlas_timeline_events(
    organization_id: int,
    *,
    actor_user_id: int | None = None,
    status: str | None = None,
    limit: int = 80,
    cursor: str | int | None = None,
) -> list[dict[str, Any]]:
    return atlas_timeline_page(
        organization_id,
        actor_user_id=actor_user_id,
        status=status,
        limit=limit,
        cursor=cursor,
    )["items"]


def atlas_timeline_summary(organization_id: int, *, actor_user_id: int | None = None) -> dict[str, int]:
    privacy_filter = ""
    params: list[Any] = [int(organization_id)]
    if actor_user_id is not None:
        privacy_filter = """
        AND (
          COALESCE(e.source_type, '') NOT IN ('case', 'media_asset')
          OR (e.source_type = 'case' AND EXISTS(
            SELECT 1 FROM atlas_cases c
            WHERE c.id = CAST(e.source_id AS INTEGER)
              AND c.organization_id = e.organization_id
              AND (c.visibility_scope = 'workspace' OR c.created_by_id = ? OR c.assigned_to_id = ?)
          ))
          OR (e.source_type = 'media_asset' AND EXISTS(
            SELECT 1 FROM atlas_media_assets a
            WHERE a.id = CAST(e.source_id AS INTEGER)
              AND a.organization_id = e.organization_id
              AND (a.visibility_scope = 'workspace' OR a.owner_user_id = ?)
          ))
        )
        """
        params.extend((int(actor_user_id), int(actor_user_id), int(actor_user_id)))
    with connect_readonly() as con:
        row = con.execute(
            f"""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN status IN ('open', 'active') THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN status IN ('open', 'active')
                        AND importance IN ('important', 'critical') THEN 1 ELSE 0 END) AS attention,
              SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) AS resolved
            FROM atlas_timeline_events e WHERE e.organization_id = ? {privacy_filter}
            """,
            tuple(params),
        ).fetchone()
    return {key: int(value or 0) for key, value in dict(row).items()}


def atlas_link_entities(
    organization_id: int,
    actor_user_id: int,
    *,
    source_type: str,
    source_id: str | int,
    relation: str,
    target_type: str,
    target_id: str | int,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_source_type = str(source_type or "").strip().lower()[:80]
    clean_source_id = _entity_identity(source_id)
    clean_target_type = str(target_type or "").strip().lower()[:80]
    clean_target_id = _entity_identity(target_id)
    clean_relation = str(relation or "").strip().lower()
    if not all((clean_source_type, clean_source_id, clean_target_type, clean_target_id)):
        raise ValueError("atlas_entity_link_invalid")
    if clean_relation not in _ENTITY_RELATIONS:
        raise ValueError("atlas_entity_relation_invalid")
    now = utc_now_iso()
    with _db_lock, connect() as con:
        membership = con.execute(
            """
            SELECT role FROM atlas_memberships
            WHERE organization_id = ? AND user_id = ? AND status = 'active'
            """,
            (int(organization_id), int(actor_user_id)),
        ).fetchone()
        if membership is None or str(membership["role"]) == "viewer":
            raise ValueError("atlas_entity_link_forbidden")
        if not _atlas_entity_exists(
            con,
            int(organization_id),
            clean_source_type,
            clean_source_id,
            actor_user_id=int(actor_user_id),
        ):
            raise ValueError("atlas_entity_source_not_found")
        if not _atlas_entity_exists(
            con,
            int(organization_id),
            clean_target_type,
            clean_target_id,
            actor_user_id=int(actor_user_id),
        ):
            raise ValueError("atlas_entity_target_not_found")
        con.execute(
            """
            INSERT INTO atlas_entity_links(
                organization_id, source_type, source_id, relation,
                target_type, target_id, created_by_id, metadata_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                organization_id, source_type, source_id,
                relation, target_type, target_id
            ) DO UPDATE SET metadata_json = excluded.metadata_json
            """,
            (
                int(organization_id), clean_source_type, clean_source_id,
                clean_relation, clean_target_type, clean_target_id,
                int(actor_user_id), _json(dict(metadata or {})), now,
            ),
        )
        row = con.execute(
            """
            SELECT * FROM atlas_entity_links
            WHERE organization_id = ? AND source_type = ? AND source_id = ?
              AND relation = ? AND target_type = ? AND target_id = ?
            """,
            (
                int(organization_id), clean_source_type, clean_source_id,
                clean_relation, clean_target_type, clean_target_id,
            ),
        ).fetchone()
        con.commit()
    return _row(row)


def atlas_entity_links(
    organization_id: int,
    entity_type: str,
    entity_id: str | int,
) -> list[dict[str, Any]]:
    clean_type = str(entity_type or "").strip().lower()[:80]
    if clean_type not in _ENTITY_TABLES:
        raise ValueError("atlas_entity_type_invalid")
    clean_id = _entity_identity(entity_id)
    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT * FROM atlas_entity_links
            WHERE organization_id = ?
              AND ((source_type = ? AND source_id = ?)
                OR (target_type = ? AND target_id = ?))
            ORDER BY id DESC
            """,
            (int(organization_id), clean_type, clean_id, clean_type, clean_id),
        ).fetchall()
    return [_row(row) for row in rows]


def atlas_threads(
    organization_id: int,
    user_id: int,
    *,
    limit: int = 60,
) -> list[dict[str, Any]]:
    """List only the viewer's conversations inside the selected workspace."""

    with connect_readonly() as con:
        rows = con.execute(
            """
            SELECT t.*,
                   (SELECT COUNT(*) FROM atlas_ai_messages m
                     WHERE m.thread_id = t.id) AS message_count,
                   (SELECT substr(m.content_text, 1, 240)
                      FROM atlas_ai_messages m
                     WHERE m.thread_id = t.id
                     ORDER BY m.id DESC LIMIT 1) AS preview
            FROM atlas_ai_threads t
            WHERE t.organization_id = ? AND t.user_id = ? AND t.status = 'active'
            ORDER BY t.updated_at DESC, t.id DESC
            LIMIT ?
            """,
            (
                int(organization_id),
                int(user_id),
                max(1, min(200, int(limit))),
            ),
        ).fetchall()
    return [_row(row) for row in rows]


def atlas_thread_messages(
    organization_id: int,
    user_id: int,
    thread_id: int,
    *,
    limit: int = 200,
) -> dict[str, Any]:
    """Return one owned thread and its messages in chronological order."""

    with connect_readonly() as con:
        thread = con.execute(
            """
            SELECT * FROM atlas_ai_threads
            WHERE id = ? AND organization_id = ? AND user_id = ? AND status = 'active'
            """,
            (int(thread_id), int(organization_id), int(user_id)),
        ).fetchone()
        if thread is None:
            raise ValueError("atlas_thread_not_found")
        rows = con.execute(
            """
            SELECT * FROM (
                SELECT m.*, f.rating AS feedback_rating
                FROM atlas_ai_messages m
                LEFT JOIN atlas_ai_feedback f
                  ON f.message_id = m.id AND f.user_id = ?
                WHERE m.thread_id = ? ORDER BY m.id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (int(user_id), int(thread_id), max(1, min(500, int(limit)))),
        ).fetchall()
    return {"thread": _row(thread), "messages": [_row(row) for row in rows]}


def atlas_recent_chat_memory(
    organization_id: int,
    user_id: int,
    *,
    exclude_thread_id: int | None = None,
    agent_id: str | None = None,
    limit: int = 80,
    max_chars: int = 14_000,
) -> list[dict[str, Any]]:
    """Build a bounded, private continuity window from the user's other chats."""

    params: list[Any] = [int(organization_id), int(user_id)]
    exclusion = ""
    if exclude_thread_id is not None:
        exclusion = "AND t.id != ?"
        params.append(int(exclude_thread_id))
    agent_filter = ""
    if agent_id:
        agent_filter = "AND t.agent_id = ?"
        params.append(str(agent_id)[:80])
    params.append(max(1, min(100, int(limit))))
    with connect_readonly() as con:
        rows = con.execute(
            f"""
            WITH ranked AS (
                SELECT m.*, t.title AS thread_title,
                       (SELECT f.rating FROM atlas_ai_feedback f
                         WHERE f.message_id = m.id AND f.user_id = t.user_id
                         LIMIT 1) AS feedback_rating,
                       ROW_NUMBER() OVER (
                           PARTITION BY m.thread_id ORDER BY m.id DESC
                       ) AS message_rank
                FROM atlas_ai_messages m
                JOIN atlas_ai_threads t ON t.id = m.thread_id
                WHERE t.organization_id = ? AND t.user_id = ?
                  AND t.status = 'active' AND m.role IN ('user', 'assistant')
                  AND NOT (
                    m.role = 'assistant' AND EXISTS (
                      SELECT 1 FROM atlas_ai_feedback f
                      WHERE f.message_id = m.id AND f.user_id = t.user_id
                        AND f.rating = 'bad'
                    )
                  )
                  {exclusion}
                  {agent_filter}
            )
            SELECT * FROM ranked
            WHERE message_rank <= 4
            ORDER BY id DESC LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    selected: list[dict[str, Any]] = []
    remaining = max(1_000, min(40_000, int(max_chars)))
    for row in rows:
        item = _row(row)
        content = str(item.get("content_text") or "")[:remaining]
        if not content:
            continue
        item["content_text"] = content
        selected.append(item)
        remaining -= len(content)
        if remaining <= 0:
            break
    selected.reverse()
    return selected


def atlas_dashboard(
    guild_id: int,
    user_id: int,
    display_name: str,
    organization_id: int | None = None,
) -> dict[str, Any]:
    atlas_seed_templates()
    personal = atlas_ensure_personal_space(guild_id, user_id, display_name)
    spaces = atlas_user_spaces(guild_id, user_id)
    selected = next(
        (
            item
            for item in spaces
            if organization_id is not None and int(item["id"]) == int(organization_id)
        ),
        spaces[0] if spaces else personal["organization"],
    )
    organization_id = int(selected["id"])
    membership = next(
        (item for item in spaces if int(item["id"]) == organization_id),
        personal["membership"],
    )
    with connect_readonly() as con:
        counts = con.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM atlas_documents WHERE organization_id = ?) AS documents,
              (SELECT COUNT(*) FROM atlas_knowledge_sources WHERE organization_id = ? AND status = 'indexed') AS knowledge,
              (SELECT COUNT(*) FROM atlas_memberships WHERE organization_id = ? AND status = 'active') AS members,
              (SELECT COUNT(*) FROM atlas_ai_threads WHERE organization_id = ? AND status = 'active') AS threads,
              (SELECT COUNT(*) FROM atlas_timeline_events WHERE organization_id = ?) AS events
            """,
            (
                organization_id, organization_id, organization_id,
                organization_id, organization_id,
            ),
        ).fetchone()
    timeline = atlas_timeline_events(organization_id, actor_user_id=user_id, limit=80)
    timeline_summary = atlas_timeline_summary(organization_id, actor_user_id=user_id)
    safe_counts = dict(counts)
    safe_counts["events"] = int(timeline_summary["total"])
    return {
        "organization": selected,
        "membership": membership,
        "spaces": spaces,
        "counts": safe_counts,
        "templates": atlas_templates(organization_id),
        "documents": atlas_documents(organization_id, limit=12),
        "threads": atlas_threads(organization_id, user_id, limit=60),
        "timeline": timeline,
        "timeline_summary": timeline_summary,
        "knowledge_sources": atlas_knowledge_sources(organization_id, limit=40),
        "catalog": atlas_catalog(),
    }


def atlas_admin_snapshot(guild_id: int) -> dict[str, Any]:
    with connect_readonly() as con:
        totals = con.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM atlas_organizations WHERE guild_id = ?) AS organizations,
              (SELECT COUNT(*) FROM atlas_memberships WHERE guild_id = ? AND status = 'active') AS members,
              (SELECT COUNT(*) FROM atlas_documents d
                 JOIN atlas_organizations o ON o.id = d.organization_id
                WHERE o.guild_id = ?) AS documents,
              (SELECT COUNT(*) FROM atlas_knowledge_sources k
                 JOIN atlas_organizations o ON o.id = k.organization_id
                WHERE o.guild_id = ? AND k.status = 'indexed') AS indexed_sources,
              (SELECT COUNT(*) FROM atlas_knowledge_sources k
                 JOIN atlas_organizations o ON o.id = k.organization_id
                WHERE o.guild_id = ? AND k.status = 'failed') AS failed_sources,
              (SELECT COUNT(*) FROM atlas_ai_messages msg
                 JOIN atlas_ai_threads t ON t.id = msg.thread_id
                 JOIN atlas_organizations o ON o.id = t.organization_id
                WHERE o.guild_id = ? AND msg.role = 'assistant') AS ai_answers
            """,
            (
                int(guild_id), int(guild_id), int(guild_id),
                int(guild_id), int(guild_id), int(guild_id),
            ),
        ).fetchone()
        organizations = con.execute(
            """
            SELECT o.*,
                   (SELECT COUNT(*) FROM atlas_memberships m WHERE m.organization_id = o.id AND m.status = 'active') AS member_count,
                   (SELECT COUNT(*) FROM atlas_documents d WHERE d.organization_id = o.id) AS document_count
            FROM atlas_organizations o
            WHERE o.guild_id = ? ORDER BY o.updated_at DESC LIMIT 20
            """,
            (int(guild_id),),
        ).fetchall()
        recent = con.execute(
            """
            SELECT e.* FROM atlas_audit_events e
            JOIN atlas_organizations o ON o.id = e.organization_id
            WHERE o.guild_id = ? ORDER BY e.id DESC LIMIT 20
            """,
            (int(guild_id),),
        ).fetchall()
    return {
        "totals": dict(totals),
        "organizations": [_row(row) for row in organizations],
        "recent_events": [_row(row) for row in recent],
        "catalog": atlas_catalog(),
    }


__all__ = [name for name in globals() if name.startswith("atlas_")]
