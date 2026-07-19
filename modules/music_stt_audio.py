"""Compatibility facade for the shared Voice Control audio pipeline."""

from modules.voice_control_audio import *  # noqa: F403
from modules.voice_control_audio import __all__ as _voice_control_audio_exports

__all__ = _voice_control_audio_exports
