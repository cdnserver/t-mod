"""Stable server and faction catalog shared by Atlas storage and web surfaces."""

from __future__ import annotations

from typing import Any


_ATLAS_SERVERS = (
    {
        "code": "phoenix-15",
        "name": "Phoenix",
        "number": 15,
        "label": "Phoenix (15)",
        "enabled": True,
    },
)
_ATLAS_FACTIONS = (
    {
        "code": "lspd",
        "name": "Los Santos Police Department",
        "short_name": "LSPD",
        "label": "LSPD",
        "enabled": True,
    },
    {
        "code": "gov",
        "name": "Government",
        "short_name": "GOV",
        "label": "GOV",
        "enabled": True,
    },
)


def atlas_catalog() -> dict[str, Any]:
    return {
        "servers": [dict(item) for item in _ATLAS_SERVERS],
        "factions": [dict(item) for item in _ATLAS_FACTIONS],
        "defaults": {"server_code": "phoenix-15", "faction_code": "lspd"},
    }


def atlas_normalize_scope(server_code: str, faction_code: str) -> tuple[str, str]:
    selected_server = str(server_code or "").strip().lower()
    selected_faction = str(faction_code or "").strip().lower()
    if selected_server not in {str(item["code"]) for item in _ATLAS_SERVERS}:
        raise ValueError("atlas_server_invalid")
    if selected_faction not in {str(item["code"]) for item in _ATLAS_FACTIONS}:
        raise ValueError("atlas_faction_invalid")
    return selected_server, selected_faction


__all__ = ["atlas_catalog", "atlas_normalize_scope"]
