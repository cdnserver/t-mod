"""Discord-independent T-Mod Desktop bootstrap projection."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from modules.consensus_web_auth import ConsensusWebPrincipal
from persistence import atlas_repository as atlas_storage
from persistence import global_ban_repository as global_ban_storage
from persistence import profile_repository as profile_storage
from persistence import reactor_repository as reactor_storage
from persistence import web_auth_repository as web_auth_storage
from persistence import access_projection_repository as access_projection_storage


BLACKBIRD_PRIVATE_EDITION = "blackbird"
LEGACY_LUMEN_PRIVATE_EDITION = "lumen"
PRIVATE_DESKTOP_EDITIONS = {
    BLACKBIRD_PRIVATE_EDITION,
    LEGACY_LUMEN_PRIVATE_EDITION,
}
LEGACY_LUMEN_DEFAULT_OWNER_ID = 902235631952998410


@dataclass(frozen=True, slots=True)
class DesktopBootstrapError(Exception):
    status: int
    error: str
    message: str


def desktop_edition(headers: Mapping[str, str]) -> str:
    value = str(headers.get("X-TMod-Desktop-Edition") or "tmod").strip().lower()
    return value if value in PRIVATE_DESKTOP_EDITIONS else "tmod"


def private_desktop_owner_ids() -> set[int]:
    configured = str(os.getenv("TMOD_LUMEN_OWNER_IDS") or "").strip()
    if not configured:
        return {LEGACY_LUMEN_DEFAULT_OWNER_ID}
    return {
        int(value)
        for value in re.split(r"[\s,;]+", configured)
        if value.isdigit() and int(value) > 0
    }


def desktop_version_key(value: str) -> tuple[int, int, int, int, str, int]:
    match = re.fullmatch(
        r"v?(\d+)\.(\d+)\.(\d+)(?:[-.]?([a-zA-Z]+)(?:[.-]?(\d+))?)?",
        str(value or "").strip(),
    )
    if not match:
        return (-1, -1, -1, -1, "", -1)
    major, minor, patch = (int(match.group(index)) for index in (1, 2, 3))
    label = str(match.group(4) or "").lower()
    revision = int(match.group(5) or 0)
    return (major, minor, patch, 1 if not label else 0, label, revision)


def desktop_update_policy(headers: Mapping[str, str], *, edition: str | None = None) -> dict[str, Any]:
    edition = edition or desktop_edition(headers)
    prefix = "TMOD_BLACKBIRD" if edition == BLACKBIRD_PRIVATE_EDITION else "TMOD_DESKTOP"
    minimum = str(os.getenv(f"{prefix}_MIN_VERSION") or "").strip()
    latest = str(os.getenv(f"{prefix}_LATEST_VERSION") or minimum).strip()
    current = str(headers.get("X-TMod-Desktop-Version") or "").strip()
    required = bool(minimum) and desktop_version_key(current) < desktop_version_key(minimum)
    product_name = "Blackbird" if edition == BLACKBIRD_PRIVATE_EDITION else "T-Mod Desktop"
    return {
        "required": required,
        "minimum_version": minimum or None,
        "latest_version": latest or None,
        "current_version": current or None,
        "release_url": str(
            os.getenv(f"{prefix}_RELEASE_URL")
            or (
                "https://github.com/cdnserver/blackbird-releases/releases"
                if edition == BLACKBIRD_PRIVATE_EDITION
                else "https://github.com/cdnserver/t-mod-releases/releases/latest"
            )
        ).strip(),
        "message": (
            f"Для продолжения установите обязательное обновление {product_name}."
            if required else f"Установлена поддерживаемая версия {product_name}."
        ),
    }


async def build_desktop_bootstrap_payload(
    *,
    headers: Mapping[str, str],
    principal: ConsensusWebPrincipal,
    guild_id: int,
) -> dict[str, Any]:
    edition = desktop_edition(headers)
    # Blackbird distribution is controlled by the publisher, not a server-side
    # owner allowlist. Every authenticated T-Mod account may use an installed copy.
    if edition == LEGACY_LUMEN_PRIVATE_EDITION and int(principal.user_id) not in private_desktop_owner_ids():
        raise DesktopBootstrapError(
            status=403,
            error=f"{edition}_private_access_required",
            message="Эта редакция доступна только владельцу.",
        )

    installation = await asyncio.to_thread(
        global_ban_storage.bind_desktop_installation,
        int(guild_id),
        int(principal.user_id),
        str(headers.get("X-TMod-Install-Token") or ""),
        platform=str(headers.get("X-TMod-Desktop-Platform") or ""),
        app_version=str(headers.get("X-TMod-Desktop-Version") or ""),
        device_fingerprint=str(headers.get("X-TMod-Device-Fingerprint") or ""),
    )
    client_update = desktop_update_policy(headers, edition=edition)
    update_required = bool(client_update["required"])
    guild_member = bool(principal.guild_member)
    fellowship_member = bool(principal.fellowship_member)
    administrator = bool(principal.administrator)

    grants, overlay_context = await asyncio.gather(
        asyncio.to_thread(
            web_auth_storage.web_section_grants,
            int(guild_id),
            int(principal.user_id),
        ),
        asyncio.to_thread(
            atlas_storage.atlas_overlay_context,
            int(guild_id),
            int(principal.user_id),
        ),
    )
    notification_payload = await asyncio.to_thread(
        reactor_storage.reactor_list_notifications,
        int(guild_id),
        int(principal.user_id),
        limit=100,
    )
    preferred_name = ""
    avatar_url = str(getattr(getattr(principal.member, "display_avatar", None), "url", "")) or None
    if avatar_url is None:
        avatar_url = await asyncio.to_thread(access_projection_storage.get_web_avatar_url, guild_id, principal.user_id)
    if fellowship_member:
        profile_snapshot = await asyncio.to_thread(
            profile_storage.get_profile_snapshot,
            int(guild_id),
            int(principal.user_id),
        )
        profile, _ = profile_snapshot
        preferred_name = str(getattr(profile, "preferred_name", "") or "").strip()

    granted_sections = sorted(
        {
            str(row.get("section") or "").strip().lower()
            for row in grants
            if str(row.get("section") or "").strip()
        }
    )
    admin_access = administrator or bool(set(granted_sections) - {"atlas_ai"})
    ovr_access = administrator or "ovr" in granted_sections
    atlas_access = administrator or "atlas_ai" in granted_sections

    def service(
        service_id: str,
        title: str,
        url: str,
        *,
        enabled: bool = True,
        reason: str | None = None,
    ) -> dict[str, Any]:
        service_enabled = bool(enabled) and not update_required
        return {
            "id": service_id,
            "title": title,
            "url": url,
            "enabled": service_enabled,
            "reason": (
                "Сначала установите обязательное обновление T-Mod Desktop."
                if update_required
                else reason if not enabled else None
            ),
        }

    member_reason = "Доступ открывается участникам Товарищества."
    return {
        "protocol_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "client": {
            "edition": edition,
            "private": edition in PRIVATE_DESKTOP_EDITIONS,
            "title": (
                "BLACKBIRD — Технологии Товарищества"
                if edition == BLACKBIRD_PRIVATE_EDITION
                else "LUMEN — Технологии Товарищества"
                if edition == LEGACY_LUMEN_PRIVATE_EDITION
                else "T-Mod Desktop"
            ),
        },
        "client_update": client_update,
        "viewer": {
            "id": int(principal.user_id),
            "id_exact": str(principal.user_id),
            "name": preferred_name or str(principal.display_name),
            "display_name": str(principal.display_name),
            "avatar_url": avatar_url,
            "account_tier": str(principal.account_tier),
            "guild_member": guild_member,
            "fellowship_member": fellowship_member,
            "administrator": administrator,
            "sections": granted_sections,
        },
        "device": installation or {"trusted": False},
        "services": [
            service("reactor", "Мой Reactor", "https://home.tvr.lat/", enabled=fellowship_member, reason=member_reason),
            service("consensus", "Consensus", "https://consensus.tvr.lat/"),
            service("atlas", "Atlas", "https://dash.tvr.lat/", enabled=atlas_access, reason="Доступ к Atlas AI выдаётся администраторами."),
            service("sgl", "SGL", "https://sgl.tvr.lat/sgl"),
            service("ovr", "ОВР", "https://ovr.tvr.lat/ovr", enabled=ovr_access, reason="Портал открывается после ручной выдачи доступа."),
            service("games", "T-Mod Games", "https://home.tvr.lat/games", enabled=fellowship_member, reason=member_reason),
            service("tasks", "Общие задачи", "https://consensus.tvr.lat/tasks", enabled=fellowship_member, reason=member_reason),
            service("admin", "Ядерный Reactor", "https://reactor.tvr.lat/admin", enabled=admin_access, reason="Нужен административный или секционный доступ."),
        ],
        "notifications": notification_payload,
        "atlas_overlay": {
            "allowed": atlas_access,
            "characters": overlay_context["characters"],
            "selected_character": overlay_context["selected_character"],
            "catalog": overlay_context["catalog"],
            "default_hotkey": "Ctrl+Shift+Space",
            "endpoints": {
                "context": "https://dash.tvr.lat/api/atlas/overlay/context",
                "transcribe": "https://dash.tvr.lat/api/atlas/overlay/transcribe",
                "stream": "https://dash.tvr.lat/api/atlas/chat/stream",
                "tts_voices": "https://dash.tvr.lat/api/atlas/overlay/tts/voices",
                "tts_preview": "https://dash.tvr.lat/api/atlas/overlay/tts/preview",
                "tts_synthesize": "https://dash.tvr.lat/api/atlas/overlay/tts/synthesize",
            },
            "capabilities": {
                "push_to_talk": True,
                "spoken_reply": True,
                "ai_voice": "system_fallback",
                "screen_context": "consent_gated",
            },
        },
    }


__all__ = [
    "DesktopBootstrapError",
    "PRIVATE_DESKTOP_EDITIONS",
    "build_desktop_bootstrap_payload",
    "desktop_edition",
    "desktop_update_policy",
    "desktop_version_key",
    "private_desktop_owner_ids",
]
