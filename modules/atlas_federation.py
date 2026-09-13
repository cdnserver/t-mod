"""Stable scope rules for a multi-project Atlas installation.

Atlas has one platform, but legal truth is not shared blindly.  The helpers in
this module are intentionally dependency-free so that ingestion, retrieval,
dataset exports and future workers use one identical vocabulary.
"""

from __future__ import annotations


ATLAS_DEFAULT_PROJECT_CODE = "majestic-rp"
ATLAS_FEDERATION_SCOPES = (
    "platform",
    "project",
    "server",
    "faction",
    "workspace",
)

_LEGACY_TO_FEDERATION = {
    # Older Atlas only had one project. Its "global" meant "shared in this
    # installation", not "publish this material to every future customer".
    # Preserve that boundary by upgrading it to project scope. Platform-wide
    # rules are an explicit new administrative choice.
    "global": "project",
    "server": "server",
    "faction": "faction",
    "workspace": "workspace",
}
_FEDERATION_TO_LEGACY = {
    "platform": "global",
    "project": "global",
    "server": "server",
    "faction": "faction",
    "workspace": "workspace",
}


def atlas_normalize_federation_scope(
    value: str | None,
    *,
    legacy_visibility_scope: str | None = None,
) -> str:
    """Normalise the new scope, safely deriving it for old Atlas records."""

    selected = str(value or "").strip().lower()
    if not selected:
        selected = _LEGACY_TO_FEDERATION.get(
            str(legacy_visibility_scope or "workspace").strip().lower(),
            "workspace",
        )
    if selected not in ATLAS_FEDERATION_SCOPES:
        raise ValueError("atlas_federation_scope_invalid")
    return selected


def atlas_legacy_visibility_scope(federation_scope: str | None) -> str:
    """Keep legacy consumers compatible while the canonical scope is upgraded."""

    selected = atlas_normalize_federation_scope(federation_scope)
    return _FEDERATION_TO_LEGACY[selected]


def atlas_federation_scope_catalog() -> list[dict[str, str]]:
    return [
        {
            "code": "platform",
            "label": "Общая база правил",
            "description": "Материал одинаково применим во всех проектах Atlas.",
        },
        {
            "code": "project",
            "label": "Только проект",
            "description": "Общая норма одного проекта, без доступа другим проектам.",
        },
        {
            "code": "server",
            "label": "Только сервер",
            "description": "Законы и правила конкретного игрового сервера.",
        },
        {
            "code": "faction",
            "label": "Только фракция",
            "description": "Устав и внутренние материалы фракции на сервере.",
        },
        {
            "code": "workspace",
            "label": "Только пространство",
            "description": "Частные материалы одной команды Atlas.",
        },
    ]


__all__ = [
    "ATLAS_DEFAULT_PROJECT_CODE",
    "ATLAS_FEDERATION_SCOPES",
    "atlas_federation_scope_catalog",
    "atlas_legacy_visibility_scope",
    "atlas_normalize_federation_scope",
]
