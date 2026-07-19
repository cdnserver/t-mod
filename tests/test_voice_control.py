import sqlite3
import tempfile
import unittest
from array import array
from concurrent.futures import ThreadPoolExecutor
from math import pi, sin
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import storage
from modules.music_domain import parse_voice_command
from modules.profile import ProfileMicrophoneView, profile_microphone_embed
from modules.voice_control_audio import (
    apply_pcm_gain,
    prepare_discord_pcm_for_stt,
)
from modules.voice_control_domain import (
    LocalTranscript,
    VoiceDomainSpec,
    assess_microphone,
    decide_local_transcript,
)
from modules.voice_control_service import VoiceControlPlatform
from modules.voice_control_local import LocalVoiceRecognizer


ROOT = Path(__file__).resolve().parents[1]


def _tone_pcm(
    *,
    duration: float = 0.6,
    amplitude: int = 4_000,
    frequency: float = 220.0,
) -> bytes:
    samples = array("h")
    for index in range(round(48_000 * duration)):
        value = round(amplitude * sin(2 * pi * frequency * index / 48_000))
        samples.extend((value, value))
    return samples.tobytes()


class VoiceStorageMixin:
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "voice-control-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()


class VoiceControlDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.domain = VoiceDomainSpec(
            "music",
            parse_voice_command,
            frozenset(
                {
                    "pause",
                    "resume",
                    "skip",
                    "stop",
                    "disconnect",
                    "volume_up",
                }
            ),
            frozenset({"stop", "disconnect"}),
        )

    def decide(self, text: str, confidence: float, *, armed: bool = False):
        return decide_local_transcript(
            LocalTranscript(text, confidence, 120, "local/tiny"),
            armed=armed,
            domain=self.domain,
            fast_confidence=0.58,
            cautious_confidence=0.76,
            reject_confidence=0.66,
        )

    def test_local_wake_and_short_controls_take_fast_path(self) -> None:
        wake = self.decide("Банан", 0.8)
        pause = self.decide("пауза", 0.7, armed=True)

        self.assertEqual((wake.outcome, wake.reason), ("accept", "wake"))
        self.assertEqual(pause.outcome, "accept")
        self.assertEqual(pause.intent.action, "pause")

    def test_cautious_and_complex_commands_require_cloud_when_uncertain(self) -> None:
        weak_stop = self.decide("банан стоп", 0.7)
        strong_stop = self.decide("банан стоп", 0.9)
        play = self.decide("банан включи группу кино", 0.95)

        self.assertEqual(weak_stop.outcome, "fallback")
        self.assertEqual(strong_stop.outcome, "accept")
        self.assertEqual(play.outcome, "fallback")

    def test_confident_background_speech_never_spends_cloud_request(self) -> None:
        decision = self.decide("обсудим проект после обеда", 0.9)

        self.assertEqual(decision.outcome, "reject")
        self.assertEqual(decision.reason, "background_speech")

    def test_microphone_assessment_distinguishes_silence_and_signal(self) -> None:
        silent = assess_microphone(prepare_discord_pcm_for_stt(b"\x00" * 192_000))
        silence = b"\x00" * round(192_000 * 0.2)
        signal = assess_microphone(
            prepare_discord_pcm_for_stt(silence + _tone_pcm() + silence)
        )

        self.assertEqual(silent.quality_key, "silent")
        self.assertEqual(silent.quality_score, 0)
        self.assertGreater(signal.quality_score, 40)
        self.assertGreater(signal.snr_db, 0)

    def test_calibrated_gain_is_clamped_and_does_not_change_shape(self) -> None:
        pcm = _tone_pcm(amplitude=100)
        boosted = apply_pcm_gain(pcm, 20)

        self.assertEqual(len(boosted), len(pcm))
        self.assertNotEqual(boosted, pcm)
        prepared = prepare_discord_pcm_for_stt(boosted)
        self.assertTrue(prepared.has_speech)
        self.assertGreater(prepared.raw_rms, 100)


class VoiceControlStorageTests(VoiceStorageMixin, unittest.TestCase):
    def assessment(self):
        return assess_microphone(prepare_discord_pcm_for_stt(_tone_pcm(amplitude=650)))

    def test_calibration_and_metrics_round_trip_without_audio_or_transcript(self) -> None:
        saved = storage.save_microphone_calibration(10, 100, self.assessment())
        storage.record_voice_recognition(
            10,
            100,
            success=True,
            latency_ms=420,
            engine="local/tiny",
        )
        current = storage.record_voice_recognition(
            10,
            100,
            success=False,
            latency_ms=800,
            engine="cloud/fallback",
        )

        self.assertEqual(saved.guild_id, 10)
        self.assertEqual(current.commands_total, 2)
        self.assertEqual(current.failures_total, 1)
        self.assertEqual(current.average_latency_ms, 610)
        self.assertEqual(current.last_engine, "cloud/fallback")
        with sqlite3.connect(storage.DATABASE_FILE) as con:
            columns = {
                row[1]
                for row in con.execute("PRAGMA table_info(voice_user_profiles)")
            }
        self.assertNotIn("audio", columns)
        self.assertNotIn("transcript", columns)

    def test_metric_updates_are_atomic_under_concurrency(self) -> None:
        def record(_: int) -> None:
            storage.record_voice_recognition(
                10,
                100,
                success=True,
                latency_ms=100,
                engine="local/tiny",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(record, range(80)))

        profile = storage.get_voice_user_profile(10, 100)
        self.assertEqual(profile.commands_total, 80)
        self.assertEqual(profile.average_latency_ms, 100)

    def test_reset_removes_calibration_but_preserves_operational_metrics(self) -> None:
        storage.save_microphone_calibration(10, 100, self.assessment())
        storage.record_voice_recognition(
            10,
            100,
            success=True,
            latency_ms=100,
            engine="local/tiny",
        )

        reset = storage.reset_microphone_calibration(10, 100)

        self.assertIsNone(reset.calibrated_at)
        self.assertEqual(reset.input_gain, 1.0)
        self.assertEqual(reset.commands_total, 1)


class VoiceControlServiceTests(VoiceStorageMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.cloud = SimpleNamespace(
            configured=True,
            primary_model="cloud/primary",
            transcribe_pcm=lambda _pcm: "Банан, включи группу Кино",
        )
        self.platform = VoiceControlPlatform(SimpleNamespace(), cloud_transcriber=self.cloud)
        self.platform.register_domain(
            "music",
            parse_voice_command,
            fast_actions={"pause", "resume", "skip", "stop"},
            cautious_actions={"stop"},
        )

    async def test_private_diagnostic_calibrates_and_returns_ephemeral_result(self) -> None:
        self.platform.local.transcribe_pcm = AsyncMock(
            return_value=LocalTranscript("банан включи кино", 0.8, 90, "local/tiny")
        )
        future = self.platform.begin_diagnostic(10, 100)

        consumed = await self.platform.consume_diagnostic(
            10,
            100,
            _tone_pcm(amplitude=700),
        )
        result = await future

        self.assertTrue(consumed)
        self.assertIn("Кино", result.transcript)
        self.assertGreater(result.assessment.quality_score, 0)
        self.assertIsNotNone(result.profile.calibrated_at)
        conditioned = await self.platform.condition_pcm(10, 100, _tone_pcm(amplitude=70))
        self.assertTrue(conditioned)

    async def test_silent_diagnostic_sample_is_ignored_until_real_speech(self) -> None:
        future = self.platform.begin_diagnostic(10, 100)

        consumed = await self.platform.consume_diagnostic(10, 100, b"\x00" * 192_000)

        self.assertTrue(consumed)
        self.assertFalse(future.done())
        self.platform.cancel_diagnostic(10, 100)

    async def test_domain_registry_is_reusable_for_future_modules(self) -> None:
        def custom_parser(text: str):
            return SimpleNamespace(action="approve") if text == "одобри" else None

        self.platform.register_domain(
            "consensus",
            custom_parser,
            fast_actions={"approve"},
        )
        self.platform.local.transcribe_pcm = AsyncMock(
            return_value=LocalTranscript("одобри", 0.9, 80, "local/tiny")
        )

        decision = await self.platform.try_local("consensus", b"pcm", armed=True)

        self.assertEqual(decision.outcome, "accept")
        self.assertEqual(decision.intent.action, "approve")


class VoiceControlLocalLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_eager_warmup_still_starts_model_on_first_phrase(self) -> None:
        recognizer = LocalVoiceRecognizer()
        recognizer.enabled = True
        with (
            patch("modules.voice_control_local.VOICE_LOCAL_STT_WARMUP", False),
            patch.object(LocalVoiceRecognizer, "installed", new_callable=lambda: property(lambda _: True)),
            patch.object(recognizer, "_warmup", new=AsyncMock()),
        ):
            recognizer.start_warmup()
            self.assertIsNone(recognizer._warmup_task)

            result = await recognizer.transcribe_pcm(b"pcm")

            self.assertIsNone(result)
            self.assertIsNotNone(recognizer._warmup_task)
            await recognizer._warmup_task


class VoiceControlProfileUiTests(unittest.TestCase):
    def test_profile_diagnostics_explain_privacy_and_quality(self) -> None:
        member = SimpleNamespace(display_name="Участник")
        profile = SimpleNamespace(
            quality_score=82,
            input_gain=1.25,
            snr_db=18.5,
            calibrated_at="2026-07-20T10:00:00+00:00",
            commands_total=10,
            failures_total=1,
            average_latency_ms=430,
            last_engine="local/tiny",
        )

        embed = profile_microphone_embed(member, profile, local_status="ready")
        rendered = "\n".join(
            [str(embed.description)]
            + [f"{field.name}\n{field.value}" for field in embed.fields]
        )

        self.assertIn("не сохраняются", rendered)
        self.assertIn("82/100", rendered)
        self.assertIn("Банан, включи группу Кино", rendered)
        view = ProfileMicrophoneView(100, SimpleNamespace())
        self.assertEqual(len(view.children), 3)


class VoiceControlDeploymentTests(unittest.TestCase):
    def test_windows_docker_receives_local_voice_defaults_and_persistent_cache(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        example = (ROOT / ".env.persistent.example").read_text(encoding="utf-8")
        migration = (ROOT / "merge_env_windows.ps1").read_text(encoding="utf-8")

        self.assertIn("faster-whisper==1.2.1", requirements)
        self.assertIn("VOICE_LOCAL_STT_ENABLED=true", example)
        self.assertIn("VOICE_MODEL_CACHE_DIR=/app/persistent/models/voice", example)
        self.assertIn("VOICE_LOCAL_STT_WAIT_SECONDS=0.75", example)
        self.assertIn("Added by T-Mod update", migration)


if __name__ == "__main__":
    unittest.main()
