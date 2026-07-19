"""Composition adapter between T-Mod Music and the shared Voice Control core."""

from __future__ import annotations

from typing import Any

from modules.music_domain import parse_voice_command
from modules.music_providers import OpenRouterTranscriber
from modules.voice_control_service import VoiceControlPlatform, get_voice_control


def configure_music_voice_control(
    bot: Any,
    *,
    transcriber: Any | None,
    voice_control: VoiceControlPlatform | None,
) -> VoiceControlPlatform:
    existing = get_voice_control(bot)
    cloud = transcriber or (
        existing.cloud_transcriber if existing is not None else OpenRouterTranscriber()
    )
    platform = voice_control or existing or VoiceControlPlatform(
        bot,
        cloud_transcriber=cloud,
    )
    platform.register_domain(
        "music",
        parse_voice_command,
        fast_actions={
            "pause",
            "resume",
            "skip",
            "stop",
            "disconnect",
            "queue",
            "now",
            "volume_up",
            "volume_down",
            "volume_set",
        },
        cautious_actions={"stop", "disconnect"},
    )
    return platform


__all__ = ["configure_music_voice_control"]
