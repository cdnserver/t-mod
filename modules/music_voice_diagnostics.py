"""Music voice-gateway adapter for shared microphone diagnostics."""

from __future__ import annotations

import asyncio
from typing import Any

from modules.music_runtime_errors import MusicRuntimeError
from modules.voice_control_config import VOICE_DIAGNOSTIC_TIMEOUT_SECONDS
from modules.voice_control_service import VoiceDiagnosticResult


async def run_microphone_diagnostic(
    manager: Any,
    member: Any,
    text_channel_id: int,
) -> VoiceDiagnosticResult:
    session = manager.get(member.guild.id)
    connected_for_diagnostic = session is None
    if session is None:
        session = await manager.connect(member, text_channel_id)
    else:
        manager.require_same_voice(member, session)
    temporary_listener = member.id not in session.voice_users
    future = manager.voice_control.begin_diagnostic(member.guild.id, member.id)
    session.voice_users.add(member.id)
    session.diagnostic_users.add(member.id)
    try:
        await manager._ensure_voice_runtime(session)
        session.last_error = None
        session.last_notice = (
            f"{member.display_name}: диагностика микрофона — ожидаю контрольную фразу."
        )
        await manager.play_listening_signal(session)
        await manager.publish_status(session)
        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=VOICE_DIAGNOSTIC_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise MusicRuntimeError(
                "Не услышал контрольную фразу. Повторите проверку и говорите сразу после сигнала."
            ) from exc
    finally:
        manager.voice_control.cancel_diagnostic(member.guild.id, member.id)
        session.diagnostic_users.discard(member.id)
        if temporary_listener:
            session.voice_users.discard(member.id)
            session.segmenter.discard(member.id)
        if not session.voice_users:
            await manager._disable_voice_runtime(session)
        current = manager.get(member.guild.id)
        if (
            connected_for_diagnostic
            and current is session
            and session.current is None
            and not session.queue
        ):
            try:
                await manager.disconnect(member)
            except MusicRuntimeError:
                pass


__all__ = ["run_microphone_diagnostic"]
