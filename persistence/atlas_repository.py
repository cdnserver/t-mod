"""Durable multi-tenant storage for T-Mod Atlas."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from persistence.core import _db_lock, connect, connect_readonly, utc_now_iso


_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_ROLES = frozenset({"owner", "administrator", "editor", "member", "viewer"})


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
    ):
        if key in item:
            item[key.removesuffix("_json")] = _decoded(item.pop(key), fallback)
    return item


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
) -> dict[str, Any]:
    clean_title = str(title or "").strip()[:180]
    clean_content = str(content or "").strip()[:250000]
    if not clean_title or len(clean_content) < 20:
        raise ValueError("atlas_knowledge_invalid")
    clean_kind = str(source_kind or "memo").strip().lower()
    if clean_kind not in {"document", "forum", "memo", "regulation", "manual", "url"}:
        clean_kind = "memo"
    checksum = hashlib.sha256(clean_content.encode("utf-8")).hexdigest()
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
                organization_id, title, source_kind, source_url, content_text,
                checksum, created_by_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(organization_id, checksum) DO UPDATE SET
                title = excluded.title,
                source_url = excluded.source_url,
                status = 'pending',
                last_error = NULL,
                updated_at = excluded.updated_at
            """,
            (
                int(organization_id),
                clean_title,
                clean_kind,
                str(source_url or "").strip()[:1000] or None,
                clean_content,
                checksum,
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


def atlas_dashboard(guild_id: int, user_id: int, display_name: str) -> dict[str, Any]:
    atlas_seed_templates()
    personal = atlas_ensure_personal_space(guild_id, user_id, display_name)
    spaces = atlas_user_spaces(guild_id, user_id)
    selected = spaces[0] if spaces else personal["organization"]
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
    }


__all__ = [name for name in globals() if name.startswith("atlas_")]
