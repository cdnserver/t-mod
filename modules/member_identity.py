"""Member-facing identity rules shared by Discord and the web Reactor."""

from __future__ import annotations

from typing import Any

from modules.tvrs_config import TVRS_CHAIR_ROLE_ID
from persistence.profile_repository import (
    normalize_profile_nickname,
    normalize_profile_preferred_name,
    normalize_profile_static,
)


DISCORD_NICKNAME_MAX_LENGTH = 32


def fellowship_discord_nickname(
    character_nickname: str,
    static_id: str,
    preferred_name: str,
) -> str:
    """Return ``S. Goodman | 263345 | Иван`` for a complete identity."""

    character = normalize_profile_nickname(character_nickname)
    parts = character.split()
    if len(parts) < 2 or not parts[0][0].isalpha():
        raise ValueError("profile_character_full_name_required")
    static = normalize_profile_static(static_id)
    preferred = normalize_profile_preferred_name(preferred_name)
    compact_character = f"{parts[0][0].upper()}. {' '.join(parts[1:])}"
    nickname = f"{compact_character} | {static} | {preferred}"
    if len(nickname) > DISCORD_NICKNAME_MAX_LENGTH:
        raise ValueError("profile_discord_nickname_too_long")
    return nickname


def nickname_change_exempt(member: Any) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    if bool(getattr(permissions, "administrator", False)):
        return True
    return any(
        int(getattr(role, "id", 0) or 0) == int(TVRS_CHAIR_ROLE_ID)
        for role in getattr(member, "roles", ())
    )


__all__ = [
    "DISCORD_NICKNAME_MAX_LENGTH",
    "fellowship_discord_nickname",
    "nickname_change_exempt",
]
