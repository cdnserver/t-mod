"""Database-backed projection of the Nuclear Reactor section access.

The projection is shared by the legacy Discord web runtime and the standalone
API.  Access decisions must not depend on a live Discord gateway connection.
"""

from __future__ import annotations

from typing import Any

from persistence import web_auth_repository as web_auth_storage


ADMIN_ONLY_WEB_SECTIONS = frozenset({"security"})


def effective_admin_sections(
    guild_id: int,
    user_id: int,
    *,
    administrator: bool,
) -> list[str]:
    """Return the durable section set visible to one authenticated account."""

    if administrator:
        return sorted(
            web_auth_storage.WEB_GRANTABLE_SECTIONS | ADMIN_ONLY_WEB_SECTIONS
        )
    return sorted(
        {
            str(row.get("section") or "")
            for row in web_auth_storage.web_section_grants(
                int(guild_id), int(user_id)
            )
            if str(row.get("section") or "")
            and str(row.get("section") or "") != "atlas_ai"
        }
    )


def build_admin_access_payload(
    guild_id: int,
    principal: Any,
    *,
    guild_name: str = "Товарищество",
) -> dict[str, Any]:
    sections = effective_admin_sections(
        int(guild_id),
        int(principal.user_id),
        administrator=bool(principal.administrator),
    )
    return {
        "viewer": {
            "id": int(principal.user_id),
            "name": str(principal.display_name),
            "administrator": bool(principal.administrator),
            "csrf_token": str(principal.csrf_token),
            "roles": [
                {
                    "id": int(getattr(role, "id", 0) or 0),
                    "name": str(getattr(role, "name", "") or ""),
                }
                for role in getattr(principal.member, "roles", ())
                if int(getattr(role, "id", 0) or 0) > 0
            ],
        },
        "guild": {
            "id": int(guild_id),
            "name": str(guild_name or "Товарищество"),
        },
        "sections": sections,
        "administrator": bool(principal.administrator),
    }


__all__ = [
    "ADMIN_ONLY_WEB_SECTIONS",
    "build_admin_access_payload",
    "effective_admin_sections",
]
