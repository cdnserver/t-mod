"""Durable multi-tenant storage for T-Mod Atlas."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from modules.atlas_catalog import (
    atlas_catalog as _base_atlas_catalog,
    atlas_normalize_knowledge_scope,
)
from modules.atlas_taxonomy import atlas_classify_knowledge, atlas_taxonomy_catalog
from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_ROLES = frozenset({"owner", "administrator", "editor", "member", "viewer"})
_CATALOG_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


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
    return {
        **base,
        **atlas_taxonomy_catalog(),
        "servers": [_row(row) for row in servers] or base["servers"],
        "factions": [_row(row) for row in factions] or base["factions"],
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
            {"fields": ["recipient", "subject", "body", "author"]},
            "Кому: {{recipient}}\nТема: {{subject}}\n\n{{body}}\n\n{{author}}",
        ),
        (
            "incident-report",
            "Рапорт о происшествии",
            "Рапорты",
            "Единый формат фиксации события и принятых мер.",
            {"fields": ["date", "location", "participants", "facts", "actions"]},
            "Дата: {{date}}\nМесто: {{location}}\nУчастники: {{participants}}\n\nОбстоятельства:\n{{facts}}\n\nПринятые меры:\n{{actions}}",
        ),
        (
            "forum-publication",
            "Публикация для форума",
            "Форум",
            "Структурированная публикация с проверкой фактов и вложений.",
            {"fields": ["title", "summary", "content", "attachments"]},
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
    return [_row(row) for row in rows]


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
    clean_fields = {
        str(key)[:80]: str(value).strip()[:12000]
        for key, value in dict(fields or {}).items()
    }
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
                SELECT id FROM atlas_document_templates
                WHERE id = ? AND status = 'active'
                  AND (organization_id IS NULL OR organization_id = ?)
                """,
                (int(template_id), int(organization_id)),
            ).fetchone()
            if template is None:
                raise ValueError("atlas_document_template_forbidden")
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
                str(rendered_text or "")[:100000],
                now,
                now,
            ),
        )
        document_id = int(cursor.lastrowid)
        con.execute(
            """
            INSERT INTO atlas_audit_events(
                organization_id, actor_user_id, event_type, target_type,
                target_id, summary, created_at
            ) VALUES(?, ?, 'document_created', 'document', ?, ?, ?)
            """,
            (int(organization_id), int(user_id), str(document_id), f"Создан документ «{clean_title}»", now),
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


def atlas_mark_knowledge_indexed(
    source_id: int,
    *,
    point_id: str | None,
    error: str | None = None,
) -> None:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        con.execute(
            """
            UPDATE atlas_knowledge_sources
            SET status = ?, qdrant_point_id = COALESCE(?, qdrant_point_id),
                indexed_at = CASE WHEN ? IS NULL THEN ? ELSE indexed_at END,
                last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                "failed" if error else "indexed",
                point_id,
                error,
                now,
                str(error or "")[:2000] or None,
                now,
                int(source_id),
            ),
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
        con.execute(
            """
            UPDATE atlas_forum_feeds
            SET status = ?, last_success_at = CASE WHEN ? IS NULL THEN ? ELSE last_success_at END,
                next_sync_at = ?, last_error = ?, last_stats_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                error,
                now,
                next_sync,
                str(error or "")[:4000] or None,
                _json(stats or {}),
                now,
                int(feed_id),
            ),
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


def atlas_create_thread(organization_id: int, user_id: int, title: str) -> int:
    now = utc_now_iso()
    with _db_lock, connect() as con:
        cursor = con.execute(
            """
            INSERT INTO atlas_ai_threads(organization_id, user_id, title, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (int(organization_id), int(user_id), str(title or "Новый диалог")[:120], now, now),
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
                SELECT * FROM atlas_ai_messages
                WHERE thread_id = ? ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (int(thread_id), max(1, min(500, int(limit)))),
        ).fetchall()
    return {"thread": _row(thread), "messages": [_row(row) for row in rows]}


def atlas_recent_chat_memory(
    organization_id: int,
    user_id: int,
    *,
    exclude_thread_id: int | None = None,
    limit: int = 80,
    max_chars: int = 14_000,
) -> list[dict[str, Any]]:
    """Build a bounded, private continuity window from the user's other chats."""

    params: list[Any] = [int(organization_id), int(user_id)]
    exclusion = ""
    if exclude_thread_id is not None:
        exclusion = "AND t.id != ?"
        params.append(int(exclude_thread_id))
    params.append(max(1, min(100, int(limit))))
    with connect_readonly() as con:
        rows = con.execute(
            f"""
            WITH ranked AS (
                SELECT m.*, t.title AS thread_title,
                       ROW_NUMBER() OVER (
                           PARTITION BY m.thread_id ORDER BY m.id DESC
                       ) AS message_rank
                FROM atlas_ai_messages m
                JOIN atlas_ai_threads t ON t.id = m.thread_id
                WHERE t.organization_id = ? AND t.user_id = ?
                  AND t.status = 'active' AND m.role IN ('user', 'assistant')
                  {exclusion}
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
              (SELECT COUNT(*) FROM atlas_ai_threads WHERE organization_id = ? AND status = 'active') AS threads
            """,
            (organization_id, organization_id, organization_id, organization_id),
        ).fetchone()
    return {
        "organization": selected,
        "membership": membership,
        "spaces": spaces,
        "counts": dict(counts),
        "templates": atlas_templates(organization_id),
        "documents": atlas_documents(organization_id, limit=12),
        "threads": atlas_threads(organization_id, user_id, limit=60),
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
