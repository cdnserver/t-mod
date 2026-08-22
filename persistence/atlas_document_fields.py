"""Safe structured fields and deterministic rendering for Atlas documents."""

from __future__ import annotations

import re
from typing import Any


_FIELD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_PLACEHOLDER = re.compile(r"{{\s*([a-z][a-z0-9_]{0,79})\s*}}", re.IGNORECASE)
_LONG_FIELDS = frozenset({"body", "content", "facts", "actions", "summary", "participants"})


def normalize_document_schema(schema: Any) -> dict[str, Any]:
    """Return a stable public schema while accepting legacy string field lists."""

    raw_schema = schema if isinstance(schema, dict) else {}
    raw_fields = raw_schema.get("fields") if isinstance(raw_schema.get("fields"), list) else []
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_fields[:40]:
        if isinstance(raw, str):
            key = raw.strip().lower()
            definition: dict[str, Any] = {"key": key}
        elif isinstance(raw, dict):
            definition = dict(raw)
            key = str(definition.get("key") or definition.get("name") or "").strip().lower()
        else:
            continue
        if not _FIELD_KEY.fullmatch(key) or key in seen:
            continue
        seen.add(key)
        field_type = str(definition.get("type") or ("textarea" if key in _LONG_FIELDS else "text")).lower()
        if field_type not in {"text", "textarea", "date", "number", "select"}:
            field_type = "text"
        raw_options = definition.get("options") if isinstance(definition.get("options"), list) else []
        options = [str(item).strip()[:120] for item in raw_options if str(item).strip()][:30]
        if field_type == "select" and not options:
            field_type = "text"
        fields.append(
            {
                "key": key,
                "label": str(definition.get("label") or key.replace("_", " ").capitalize()).strip()[:120],
                "type": field_type,
                "required": bool(definition.get("required", True)),
                "placeholder": str(definition.get("placeholder") or "").strip()[:240],
                "options": options,
            }
        )
    return {**raw_schema, "fields": fields}


def prepare_document_content(
    schema: Any,
    template_text: str,
    fields: dict[str, Any],
    *,
    fallback_text: str = "",
    allow_legacy_content: bool = False,
) -> tuple[dict[str, str], str]:
    """Validate fields and render a template without evaluating user input."""

    normalized_schema = normalize_document_schema(schema)
    definitions = normalized_schema["fields"]
    raw_fields = fields if isinstance(fields, dict) else {}
    clean_fields = {
        str(key)[:80]: str(value if value is not None else "").strip()[:12000]
        for key, value in raw_fields.items()
    }
    schema_keys = {item["key"] for item in definitions}
    legacy = allow_legacy_content and bool(clean_fields.get("content")) and not (schema_keys & clean_fields.keys())
    if not legacy:
        missing = [item["key"] for item in definitions if item["required"] and not clean_fields.get(item["key"])]
        if missing:
            raise ValueError(f"atlas_document_fields_required:{','.join(missing)}")
        clean_fields = {item["key"]: clean_fields.get(item["key"], "") for item in definitions}

    source = str(template_text or "")[:100000]
    if source and not legacy:
        rendered = _PLACEHOLDER.sub(lambda match: clean_fields.get(match.group(1).lower(), "—") or "—", source)
    else:
        rendered = str(fallback_text or clean_fields.get("content") or "")[:100000]
    return clean_fields, rendered[:100000]
