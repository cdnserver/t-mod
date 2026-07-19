"""Reusable Voice Control v2 composition service."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from modules.voice_control_audio import apply_pcm_gain, prepare_discord_pcm_for_stt
from modules.voice_control_config import (
    VOICE_BACKGROUND_REJECT_CONFIDENCE,
    VOICE_CAUTION_COMMAND_CONFIDENCE,
    VOICE_FAST_COMMAND_CONFIDENCE,
    VOICE_LOCAL_STT_WAIT_SECONDS,
)
from modules.voice_control_domain import (
    LocalVoiceDecision,
    MicrophoneAssessment,
    VoiceDomainSpec,
    assess_microphone,
    decide_local_transcript,
)
from modules.voice_control_local import LocalVoiceRecognizer
from persistence import voice_control_context as repository


log = logging.getLogger(__name__)
DiagnosticGateway = Callable[[Any, int], Awaitable["VoiceDiagnosticResult"]]


@dataclass(frozen=True, slots=True)
class VoiceDiagnosticResult:
    assessment: MicrophoneAssessment
    transcript: str
    latency_ms: int
    engine: str
    local_status: str
    profile: repository.VoiceUserProfile


class VoiceControlPlatform:
    """Shared recognition, calibration and metrics foundation for all features."""

    def __init__(self, bot: Any, *, cloud_transcriber: Any) -> None:
        self.bot = bot
        self.cloud_transcriber = cloud_transcriber
        self.local = LocalVoiceRecognizer()
        self._domains: dict[str, VoiceDomainSpec] = {}
        self._profile_cache: dict[tuple[int, int], repository.VoiceUserProfile | None] = {}
        self._pending_diagnostics: dict[
            tuple[int, int], asyncio.Future[VoiceDiagnosticResult]
        ] = {}
        self._diagnostic_gateway: DiagnosticGateway | None = None
        self._storage_retry_after = 0.0

    def register_domain(
        self,
        name: str,
        parser: Callable[[str], Any | None],
        *,
        fast_actions: set[str] | frozenset[str],
        cautious_actions: set[str] | frozenset[str] = frozenset(),
    ) -> VoiceDomainSpec:
        clean = str(name or "").strip().lower()
        if not clean:
            raise ValueError("voice_domain_name_required")
        spec = VoiceDomainSpec(
            clean,
            parser,
            frozenset(str(action) for action in fast_actions),
            frozenset(str(action) for action in cautious_actions),
        )
        self._domains[clean] = spec
        return spec

    def domain(self, name: str) -> VoiceDomainSpec:
        try:
            return self._domains[str(name).strip().lower()]
        except KeyError as exc:
            raise RuntimeError(f"Voice Control domain is not registered: {name}") from exc

    def start(self) -> None:
        self.local.start_warmup()

    @property
    def local_status(self) -> str:
        return self.local.status

    def set_diagnostic_gateway(self, gateway: DiagnosticGateway) -> None:
        self._diagnostic_gateway = gateway

    async def run_diagnostic(self, member: Any, text_channel_id: int) -> VoiceDiagnosticResult:
        if self._diagnostic_gateway is None:
            raise RuntimeError("Голосовая диагностика ещё не подключена к Discord.")
        return await self._diagnostic_gateway(member, int(text_channel_id))

    async def get_profile(
        self,
        guild_id: int,
        user_id: int,
        *,
        refresh: bool = False,
    ) -> repository.VoiceUserProfile | None:
        key = (int(guild_id), int(user_id))
        if time.monotonic() < self._storage_retry_after:
            return self._profile_cache.get(key)
        if refresh or key not in self._profile_cache:
            try:
                self._profile_cache[key] = await asyncio.to_thread(
                    repository.get_voice_user_profile,
                    *key,
                )
            except Exception:
                # Recognition must stay available during a temporary SQLite
                # outage; calibration is an enhancement, never a dependency.
                log.debug("Voice Control calibration storage is temporarily unavailable")
                self._storage_retry_after = time.monotonic() + 30.0
                self._profile_cache.pop(key, None)
                return None
        return self._profile_cache.get(key)

    async def condition_pcm(self, guild_id: int, user_id: int, pcm: bytes) -> bytes:
        profile = await self.get_profile(guild_id, user_id)
        gain = float(getattr(profile, "input_gain", 1.0) or 1.0)
        return apply_pcm_gain(pcm, gain)

    async def try_local(
        self,
        domain_name: str,
        pcm: bytes,
        *,
        armed: bool,
    ) -> LocalVoiceDecision:
        domain = self.domain(domain_name)
        try:
            transcript = await asyncio.wait_for(
                self.local.transcribe_pcm(pcm),
                timeout=VOICE_LOCAL_STT_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            return LocalVoiceDecision("fallback", None, None, "local_timeout")
        except Exception as exc:
            log.warning("Local Voice Control transcription failed: %s", str(exc)[:300])
            return LocalVoiceDecision("fallback", None, None, "local_error")
        return decide_local_transcript(
            transcript,
            armed=armed,
            domain=domain,
            fast_confidence=VOICE_FAST_COMMAND_CONFIDENCE,
            cautious_confidence=VOICE_CAUTION_COMMAND_CONFIDENCE,
            reject_confidence=VOICE_BACKGROUND_REJECT_CONFIDENCE,
        )

    async def record_recognition(
        self,
        guild_id: int,
        user_id: int,
        *,
        success: bool,
        latency_ms: int,
        engine: str,
    ) -> None:
        if time.monotonic() < self._storage_retry_after:
            return
        try:
            profile = await asyncio.to_thread(
                repository.record_voice_recognition,
                guild_id,
                user_id,
                success=success,
                latency_ms=latency_ms,
                engine=engine,
            )
        except Exception:
            log.debug("Voice Control metrics storage is temporarily unavailable")
            self._storage_retry_after = time.monotonic() + 30.0
            return
        self._profile_cache[(int(guild_id), int(user_id))] = profile

    def begin_diagnostic(
        self,
        guild_id: int,
        user_id: int,
    ) -> asyncio.Future[VoiceDiagnosticResult]:
        key = (int(guild_id), int(user_id))
        current = self._pending_diagnostics.get(key)
        if current is not None and not current.done():
            raise RuntimeError("Проверка микрофона уже запущена.")
        future = asyncio.get_running_loop().create_future()
        self._pending_diagnostics[key] = future
        return future

    def diagnostic_pending(self, guild_id: int, user_id: int) -> bool:
        future = self._pending_diagnostics.get((int(guild_id), int(user_id)))
        return bool(future is not None and not future.done())

    def cancel_diagnostic(self, guild_id: int, user_id: int) -> None:
        key = (int(guild_id), int(user_id))
        future = self._pending_diagnostics.pop(key, None)
        if future is not None and not future.done():
            future.cancel()

    async def consume_diagnostic(
        self,
        guild_id: int,
        user_id: int,
        pcm: bytes,
    ) -> bool:
        key = (int(guild_id), int(user_id))
        future = self._pending_diagnostics.get(key)
        if future is None or future.done():
            return False
        started = time.monotonic()
        prepared = prepare_discord_pcm_for_stt(pcm)
        if not prepared.has_speech:
            return True
        assessment = assess_microphone(prepared)
        local_task = asyncio.create_task(self.local.transcribe_pcm(pcm))
        cloud_task: asyncio.Task[str] | None = None
        if bool(getattr(self.cloud_transcriber, "configured", False)):
            cloud_task = asyncio.create_task(
                asyncio.to_thread(self.cloud_transcriber.transcribe_pcm, pcm)
            )
        local_result, cloud_result = await asyncio.gather(
            local_task,
            cloud_task or asyncio.sleep(0, result=""),
            return_exceptions=True,
        )
        local_text = (
            str(getattr(local_result, "text", "") or "")
            if not isinstance(local_result, BaseException)
            else ""
        )
        cloud_text = (
            str(cloud_result or "")
            if not isinstance(cloud_result, BaseException)
            else ""
        )
        transcript = cloud_text.strip() or local_text.strip()
        engine = (
            str(getattr(self.cloud_transcriber, "primary_model", "cloud"))
            if cloud_text.strip()
            else str(getattr(local_result, "model", "signal-only"))
        )
        profile = await asyncio.to_thread(
            repository.save_microphone_calibration,
            guild_id,
            user_id,
            assessment,
        )
        self._profile_cache[key] = profile
        result = VoiceDiagnosticResult(
            assessment=assessment,
            transcript=transcript,
            latency_ms=round((time.monotonic() - started) * 1000),
            engine=engine,
            local_status=self.local_status,
            profile=profile,
        )
        if not future.done():
            future.set_result(result)
        return True

    async def reset_calibration(
        self,
        guild_id: int,
        user_id: int,
    ) -> repository.VoiceUserProfile | None:
        profile = await asyncio.to_thread(
            repository.reset_microphone_calibration,
            guild_id,
            user_id,
        )
        self._profile_cache[(int(guild_id), int(user_id))] = profile
        return profile


def get_voice_control(client: Any) -> VoiceControlPlatform | None:
    value = getattr(client, "voice_control", None)
    return value if isinstance(value, VoiceControlPlatform) else None


__all__ = [
    "VoiceControlPlatform",
    "VoiceDiagnosticResult",
    "get_voice_control",
]
