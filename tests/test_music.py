import unittest
import wave
import time
import threading
from array import array
from io import BytesIO
from math import pi, sin
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import davey
import requests

from modules.music_audio import failure_tone_pcm, listening_tone_pcm
from modules.music_config import MUSIC_PANEL_REFRESH_SECONDS
from modules.music_domain import (
    MusicInputError,
    MusicTrack,
    parse_voice_command,
    prepare_youtube_input,
    is_wake_word_candidate,
    split_wake_word,
)
from modules.music_providers import (
    OpenRouterTranscriber,
    YoutubeResolver,
    discord_pcm_to_stt_wav,
    pcm_to_wav,
)
from modules.music_progress import (
    pause_playback_clock,
    playback_elapsed_seconds,
    reset_playback_clock,
    resume_playback_clock,
    start_playback_clock,
)
from modules.music_public_panel import MusicPublicPanelService
from modules.music_public_views import PublicMusicPanelView
from modules.music_runtime import (
    MusicGuildSession,
    MusicManager,
    MusicRuntimeError,
    PCMBytesSource,
    SignalMixerSource,
    SpeechSegment,
    SpeechSegmenter,
    TModVoiceSink,
)
from modules.music_setup import _install_voice_recv_disconnect_guard
from modules.music_speech import SpeechWorkQueue
from modules.music_stt_audio import prepare_discord_pcm_for_stt
from modules.music_views import MusicPanelView, build_music_embed


def _tone_pcm(
    *,
    duration: float = 0.5,
    amplitude: int = 6_000,
    frequency: float = 220.0,
) -> bytes:
    samples = array("h")
    for index in range(round(48_000 * duration)):
        value = round(amplitude * sin(2 * pi * frequency * index / 48_000))
        samples.extend((value, value))
    return samples.tobytes()


ROOT = Path(__file__).resolve().parents[1]


class MusicDomainTests(unittest.TestCase):
    def test_query_accepts_search_and_only_youtube_urls(self) -> None:
        self.assertEqual(
            prepare_youtube_input("Кино Группа крови"),
            "ytsearch1:Кино Группа крови",
        )
        self.assertEqual(
            prepare_youtube_input("https://youtu.be/abc123"),
            "https://youtu.be/abc123",
        )
        for unsafe in (
            "file:///etc/passwd",
            "http://127.0.0.1/private",
            "https://youtube.com.evil.test/watch?v=1",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(MusicInputError):
                prepare_youtube_input(unsafe)

    def test_wake_word_and_russian_commands_are_deterministic(self) -> None:
        for transcript in (
            "Т-Мод",
            "т мод",
            "тимод",
            "ти мод",
            "T Mod",
            "Сборщик риса",
            "сборщик риса",
            "Банан",
            "банан",
        ):
            with self.subTest(transcript=transcript):
                woke, remainder = split_wake_word(transcript)
                self.assertTrue(woke)
                self.assertEqual(remainder, "")

        woke, remainder = split_wake_word("Т-Мод, включи Кино Группа крови")
        self.assertTrue(woke)
        command = parse_voice_command(remainder)
        self.assertEqual(command.action, "play")
        self.assertEqual(command.query, "кино группа крови")

        woke, remainder = split_wake_word("Сборщик риса, включи Цой")
        self.assertTrue(woke)
        command = parse_voice_command(remainder)
        self.assertEqual(command.action, "play")
        self.assertEqual(command.query, "цой")

        woke, remainder = split_wake_word("Банан, поставь Кино")
        self.assertTrue(woke)
        command = parse_voice_command(remainder)
        self.assertEqual(command.action, "play")
        self.assertEqual(command.query, "кино")
        self.assertEqual(parse_voice_command("поставь на паузу").action, "pause")
        self.assertEqual(parse_voice_command("играй дальше").action, "resume")
        self.assertEqual(parse_voice_command("играй").action, "resume")
        self.assertEqual(parse_voice_command("следующая").action, "skip")
        self.assertEqual(parse_voice_command("громкость 150").volume_percent, 100)
        self.assertIsNone(parse_voice_command("давайте обсудим проект"))

    def test_russian_stt_variants_and_polite_commands_are_understood(self) -> None:
        play_cases = {
            "пожалуйста включить песню Кино": "кино",
            "можешь вруби мне Цоя": "цоя",
            "давай запусти трек Земфира Искала": "земфира искала",
            "поставить пожалуйста Би-2": "би-2",
            "сыграй Король и Шут": "король и шут",
        }
        for transcript, expected in play_cases.items():
            with self.subTest(transcript=transcript):
                command = parse_voice_command(transcript)
                self.assertIsNotNone(command)
                self.assertEqual(command.action, "play")
                self.assertEqual(command.query, expected)
        self.assertEqual(parse_voice_command("остановить").action, "stop")
        self.assertEqual(parse_voice_command("приостановить").action, "pause")
        self.assertEqual(parse_voice_command("продолжить").action, "resume")
        self.assertEqual(parse_voice_command("следущий").action, "skip")
        self.assertEqual(parse_voice_command("переключить").action, "skip")
        self.assertEqual(
            parse_voice_command("громкость пятьдесят процентов").volume_percent,
            50,
        )

    def test_near_wake_word_only_requests_accuracy_check(self) -> None:
        for transcript in ("бонан", "тимад", "сборщик ряса включи Цой"):
            with self.subTest(transcript=transcript):
                self.assertTrue(is_wake_word_candidate(transcript))
        for transcript in ("барабан", "банка", "сборка проекта", "обычный разговор"):
            with self.subTest(transcript=transcript):
                self.assertFalse(is_wake_word_candidate(transcript))


class MusicProviderTests(unittest.TestCase):
    def test_pcm_is_wrapped_as_standard_discord_wav(self) -> None:
        pcm = b"\x01\x00\x02\x00" * 480
        encoded = pcm_to_wav(pcm)
        with wave.open(BytesIO(encoded), "rb") as wav:
            self.assertEqual(wav.getframerate(), 48_000)
            self.assertEqual(wav.getnchannels(), 2)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.readframes(wav.getnframes()), pcm)

    def test_discord_pcm_is_compacted_to_speech_ready_mono(self) -> None:
        pcm = b"\x10\x00\x10\x00" * 48_000
        encoded = discord_pcm_to_stt_wav(pcm)
        with wave.open(BytesIO(encoded), "rb") as wav:
            self.assertEqual(wav.getframerate(), 16_000)
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getnframes(), 16_000)
        self.assertLess(len(encoded), len(pcm) // 5)

    def test_stt_audio_trims_silence_and_normalizes_quiet_speech(self) -> None:
        silence = b"\x00" * round(192_000 * 0.5)
        prepared = prepare_discord_pcm_for_stt(
            silence + _tone_pcm(duration=0.4, amplitude=250) + silence
        )

        self.assertTrue(prepared.has_speech)
        self.assertLess(prepared.output_duration_ms, prepared.input_duration_ms)
        self.assertGreater(prepared.rms, 500)
        self.assertLessEqual(prepared.peak, 30_000)
        with wave.open(BytesIO(prepared.wav), "rb") as wav:
            self.assertEqual(wav.getframerate(), 16_000)
            self.assertEqual(wav.getnchannels(), 1)

    def test_stt_audio_rejects_silence_and_dc_offset(self) -> None:
        silence = prepare_discord_pcm_for_stt(b"\x00" * 192_000)
        dc_offset = prepare_discord_pcm_for_stt(b"\xe8\x03" * 96_000)

        self.assertFalse(silence.has_speech)
        self.assertFalse(dc_offset.has_speech)
        self.assertEqual(silence.wav, b"")

    def test_stt_audio_keeps_and_boosts_a_very_quiet_microphone(self) -> None:
        prepared = prepare_discord_pcm_for_stt(_tone_pcm(duration=0.45, amplitude=90))

        self.assertTrue(prepared.has_speech)
        self.assertGreater(prepared.rms, 250)
        self.assertTrue(prepared.wav)

    def test_youtube_metadata_is_normalized_without_downloading(self) -> None:
        class FakeYDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def extract_info(self, target, *, download):
                self.target = target
                self.download = download
                return {
                    "entries": [
                        {
                            "title": "Композиция",
                            "webpage_url": "https://www.youtube.com/watch?v=abc",
                            "duration": 185,
                            "uploader": "Автор",
                            "thumbnail": "https://i.ytimg.com/example.jpg",
                        }
                    ]
                }

        with patch("modules.music_providers.yt_dlp.YoutubeDL", FakeYDL):
            track = YoutubeResolver().resolve(
                "Композиция",
                requester_id=7,
                requester_display="Слушатель",
            )
        self.assertEqual(track.title, "Композиция")
        self.assertEqual(track.duration_seconds, 185)
        self.assertEqual(track.requested_by_id, 7)

    def test_provider_output_cannot_turn_into_an_arbitrary_fetch(self) -> None:
        class FakeYDL:
            def __init__(self, _options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def extract_info(self, _target, *, download):
                self.assert_download = download
                return {"title": "Подмена", "webpage_url": "http://127.0.0.1/private"}

        with (
            patch("modules.music_providers.yt_dlp.YoutubeDL", FakeYDL),
            self.assertRaises(MusicInputError),
        ):
            YoutubeResolver().resolve(
                "Композиция",
                requester_id=7,
                requester_display="Слушатель",
            )

    def test_proxy_credentials_are_redacted_from_provider_errors(self) -> None:
        proxy = "http://user:secret@proxy.example:8080"
        with patch("modules.music_providers.MUSIC_YTDLP_PROXY", proxy):
            text = YoutubeResolver._safe_provider_error(
                RuntimeError(f"connection through {proxy} failed")
            )
        self.assertNotIn("secret", text)
        self.assertIn("<proxy>", text)

    def test_openrouter_stt_uses_audio_endpoint_without_persisting_audio(self) -> None:
        response = SimpleNamespace(
            status_code=200,
            headers={},
            json=lambda: {"text": "Т-Мод", "usage": {"cost": 0.0001}},
            text='{"text":"Т-Мод"}',
        )
        request = MagicMock(return_value=response)
        transcriber = OpenRouterTranscriber(http=SimpleNamespace(post=request))
        transcriber.api_key = "test-key"

        result = transcriber.transcribe_pcm(_tone_pcm())

        self.assertEqual(result, "Т-Мод")
        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload["input_audio"]["format"], "wav")
        self.assertEqual(payload["model"], transcriber.primary_model)
        self.assertEqual(payload["language"], "ru")
        self.assertNotIn("test-key", str(payload))

    def test_openrouter_stt_keeps_language_hint_for_legacy_whisper(self) -> None:
        response = SimpleNamespace(
            status_code=200,
            headers={},
            json=lambda: {"text": "привет"},
            text='{"text":"привет"}',
        )
        request = MagicMock(return_value=response)
        transcriber = OpenRouterTranscriber(http=SimpleNamespace(post=request))
        transcriber.api_key = "test-key"

        transcriber.transcribe_pcm(
            _tone_pcm(),
            model="openai/whisper-large-v3",
        )

        self.assertEqual(request.call_args.kwargs["json"]["language"], "ru")

    def test_silent_audio_never_spends_an_api_request(self) -> None:
        request = MagicMock()
        transcriber = OpenRouterTranscriber(http=SimpleNamespace(post=request))
        transcriber.api_key = "test-key"

        self.assertEqual(transcriber.transcribe_pcm(b"\x00" * 192_000), "")

        request.assert_not_called()

    def test_invalid_model_enters_cooldown_instead_of_repeating_slow_calls(
        self,
    ) -> None:
        response = SimpleNamespace(
            status_code=404,
            headers={},
            json=lambda: {},
            text="model not found",
        )
        request = MagicMock(return_value=response)
        transcriber = OpenRouterTranscriber(http=SimpleNamespace(post=request))
        transcriber.api_key = "test-key"

        with self.assertRaisesRegex(Exception, "HTTP 404"):
            transcriber.transcribe_pcm(_tone_pcm(), model="missing/model")
        with self.assertRaisesRegex(Exception, "восстанавливается"):
            transcriber.transcribe_pcm(_tone_pcm(), model="missing/model")

        request.assert_called_once()

    def test_rate_limit_retry_wait_is_strictly_bounded(self) -> None:
        limited = SimpleNamespace(
            status_code=429,
            headers={"Retry-After": "99"},
            json=lambda: {},
            text="rate limited",
        )
        success = SimpleNamespace(
            status_code=200,
            headers={},
            json=lambda: {"text": "банан"},
            text='{"text":"банан"}',
        )
        request = MagicMock(side_effect=[limited, success])
        transcriber = OpenRouterTranscriber(http=SimpleNamespace(post=request))
        transcriber.api_key = "test-key"

        with patch("modules.music_providers.time.sleep") as delay:
            result = transcriber.transcribe_pcm(_tone_pcm())

        self.assertEqual(result, "банан")
        delay.assert_called_once_with(1.25)

    def test_read_timeout_falls_back_without_repeating_a_long_request(self) -> None:
        request = MagicMock(side_effect=requests.ReadTimeout("slow provider"))
        transcriber = OpenRouterTranscriber(http=SimpleNamespace(post=request))
        transcriber.api_key = "test-key"

        with self.assertRaisesRegex(Exception, "временно недоступен"):
            transcriber.transcribe_pcm(_tone_pcm())

        request.assert_called_once()


class MusicAudioRuntimeTests(unittest.TestCase):
    def test_speech_segmenter_separates_users_and_silence(self) -> None:
        segmenter = SpeechSegmenter()
        packet = b"\x01\x00\x01\x00" * 15_000
        self.assertEqual(segmenter.accept(1, packet, now=0.0), [])
        self.assertEqual(segmenter.accept(2, packet, now=0.1), [])
        ready = segmenter.drain_ready(now=2.0)
        self.assertEqual({item.user_id for item in ready}, {1, 2})
        self.assertTrue(all(item.pcm == packet for item in ready))

    def test_speech_segmenter_forgets_one_user_without_touching_others(self) -> None:
        segmenter = SpeechSegmenter()
        packet = b"\x01\x00\x01\x00" * 15_000
        segmenter.accept(1, packet, now=0.0)
        segmenter.accept(2, packet, now=0.0)
        segmenter.discard(1)
        ready = segmenter.drain_ready(now=2.0)
        self.assertEqual([item.user_id for item in ready], [2])

    def test_armed_command_uses_shorter_endpoint_without_changing_default(self) -> None:
        packet = b"\x01\x00\x01\x00" * 15_000
        command = SpeechSegmenter()
        background = SpeechSegmenter()
        command.accept(1, packet, now=0.0, silence_seconds=0.42)
        background.accept(1, packet, now=0.0)

        self.assertEqual([item.user_id for item in command.drain_ready(now=0.43)], [1])
        self.assertEqual(background.drain_ready(now=0.43), [])


class MusicProgressTests(unittest.TestCase):
    def test_monotonic_clock_excludes_pause_time_and_resets(self) -> None:
        session = SimpleNamespace(
            track_started_at=None,
            track_paused_at=None,
            track_paused_seconds=0.0,
        )
        start_playback_clock(session, now=100.0)
        pause_playback_clock(session, now=105.0)
        self.assertEqual(playback_elapsed_seconds(session, now=120.0), 5)
        resume_playback_clock(session, now=120.0)
        self.assertEqual(playback_elapsed_seconds(session, now=123.0), 8)
        reset_playback_clock(session)
        self.assertEqual(playback_elapsed_seconds(session, now=200.0), 0)

    def test_pcm_sources_are_non_opus_and_signal_mixes_into_music(self) -> None:
        frame = b"\x00" * 3840
        base = MagicMock(spec=discord.PCMVolumeTransformer)
        base.read.return_value = frame
        base.volume = 0.5
        mixer = SignalMixerSource(base)
        mixer.trigger_signal()
        mixed = mixer.read()
        self.assertEqual(len(mixed), len(frame))
        self.assertNotEqual(mixed, frame)
        self.assertFalse(mixer.is_opus())

        memory = PCMBytesSource(b"\x01\x00" * 10)
        self.assertEqual(len(memory.read()), 3840)
        self.assertEqual(memory.read(), b"")
        self.assertNotEqual(failure_tone_pcm(), listening_tone_pcm())

    def test_corrupt_opus_packet_is_isolated_without_stopping_sink(self) -> None:
        manager = SimpleNamespace(accept_voice_packet=MagicMock())
        bad_decoder = MagicMock()
        opus_error = discord.opus.OpusError.__new__(discord.opus.OpusError)
        Exception.__init__(opus_error, "corrupted stream")
        opus_error.code = -4
        bad_decoder.decode.side_effect = opus_error
        good_decoder = MagicMock()
        good_decoder.decode.return_value = b"\x00" * 3840
        user = SimpleNamespace(id=7, bot=False)
        packet = SimpleNamespace(opus=b"opus-packet")

        with (
            patch(
                "modules.music_audio.discord.opus.Decoder",
                side_effect=[bad_decoder, good_decoder],
            ),
            self.assertLogs("modules.music_audio", level="WARNING") as captured,
        ):
            sink = TModVoiceSink(manager, 77)
            self.assertTrue(sink.wants_opus())
            sink.write(user, packet)
            manager.accept_voice_packet.assert_not_called()
            sink.write(user, packet)

        manager.accept_voice_packet.assert_called_once_with(77, 7, b"\x00" * 3840)
        self.assertIn("stage=opus", captured.output[0])

    def test_dave_is_decrypted_before_opus(self) -> None:
        manager = SimpleNamespace(accept_voice_packet=MagicMock())
        decoder = MagicMock()
        decoder.decode.return_value = b"\x00" * 3840
        dave_session = SimpleNamespace(
            ready=True,
            decrypt=MagicMock(return_value=b"plain-opus"),
        )
        connection = SimpleNamespace(
            dave_protocol_version=1,
            dave_session=dave_session,
        )
        user = SimpleNamespace(id=7, bot=False)

        with patch(
            "modules.music_audio.discord.opus.Decoder",
            return_value=decoder,
        ):
            sink = TModVoiceSink(manager, 77)
            sink._voice_client = SimpleNamespace(_connection=connection)
            sink.write(user, SimpleNamespace(opus=b"encrypted-opus"))

        dave_session.decrypt.assert_called_once_with(
            7,
            davey.MediaType.audio,
            b"encrypted-opus",
        )
        decoder.decode.assert_called_once_with(b"plain-opus", fec=False)
        manager.accept_voice_packet.assert_called_once_with(77, 7, b"\x00" * 3840)

    def test_valid_plaintext_stays_in_compatibility_mode_without_dave_rechecks(
        self,
    ) -> None:
        manager = SimpleNamespace(accept_voice_packet=MagicMock())
        decoder = MagicMock()
        decoder.decode.return_value = b"\x00" * 3840
        dave_session = SimpleNamespace(
            ready=True,
            can_passthrough=MagicMock(return_value=False),
            decrypt=MagicMock(
                side_effect=ValueError(
                    "Failed to decrypt: "
                    "DecryptionFailed(UnencryptedWhenPassthroughDisabled)"
                )
            ),
        )
        connection = SimpleNamespace(
            dave_protocol_version=1,
            dave_session=dave_session,
        )
        user = SimpleNamespace(id=7, bot=False)

        with (
            patch("modules.music_audio.discord.opus.Decoder", return_value=decoder),
            self.assertLogs("modules.music_audio", level="INFO") as captured,
        ):
            sink = TModVoiceSink(manager, 77)
            sink._voice_client = SimpleNamespace(_connection=connection)
            sink.write(user, SimpleNamespace(opus=b"plain-opus"))
            sink.write(user, SimpleNamespace(opus=b"next-plain-opus"))

        self.assertEqual(decoder.decode.call_count, 2)
        dave_session.decrypt.assert_called_once()
        self.assertEqual(manager.accept_voice_packet.call_count, 2)
        self.assertIn("DAVE plaintext compatibility mode enabled", captured.output[0])

    def test_dave_failure_does_not_pass_invalid_audio_to_stt(self) -> None:
        manager = SimpleNamespace(accept_voice_packet=MagicMock())
        decoder = MagicMock()
        opus_error = discord.opus.OpusError.__new__(discord.opus.OpusError)
        Exception.__init__(opus_error, "corrupted stream")
        opus_error.code = -4
        decoder.decode.side_effect = opus_error
        dave_session = SimpleNamespace(
            ready=True,
            can_passthrough=MagicMock(return_value=False),
            decrypt=MagicMock(side_effect=ValueError("user has no decryptor")),
        )
        connection = SimpleNamespace(
            dave_protocol_version=1,
            dave_session=dave_session,
        )
        user = SimpleNamespace(id=7, bot=False)

        with (
            patch("modules.music_audio.discord.opus.Decoder", return_value=decoder),
            self.assertLogs("modules.music_audio", level="WARNING") as captured,
        ):
            sink = TModVoiceSink(manager, 77)
            sink._voice_client = SimpleNamespace(_connection=connection)
            sink.write(user, SimpleNamespace(opus=b"not-opus"))

        manager.accept_voice_packet.assert_not_called()
        self.assertIn("stage=dave", captured.output[0])
        self.assertIn("user has no decryptor", captured.output[0])

    def test_plaintext_transition_cache_switches_back_to_dave_immediately(self) -> None:
        manager = SimpleNamespace(accept_voice_packet=MagicMock())
        opus_error = discord.opus.OpusError.__new__(discord.opus.OpusError)
        Exception.__init__(opus_error, "corrupted stream")
        opus_error.code = -4
        plaintext_decoder = MagicMock()
        plaintext_decoder.decode.side_effect = [b"first-pcm", opus_error]
        encrypted_decoder = MagicMock()
        encrypted_decoder.decode.return_value = b"second-pcm"
        dave_session = SimpleNamespace(
            ready=True,
            can_passthrough=MagicMock(return_value=False),
            decrypt=MagicMock(
                side_effect=[
                    ValueError("unencrypted when passthrough mode was disabled"),
                    b"decrypted-opus",
                ]
            ),
        )
        connection = SimpleNamespace(
            dave_protocol_version=1,
            dave_session=dave_session,
        )
        user = SimpleNamespace(id=7, bot=False)

        with (
            patch(
                "modules.music_audio.discord.opus.Decoder",
                side_effect=[plaintext_decoder, encrypted_decoder],
            ),
            self.assertLogs("modules.music_audio", level="INFO"),
        ):
            sink = TModVoiceSink(manager, 77)
            sink._voice_client = SimpleNamespace(_connection=connection)
            sink.write(user, SimpleNamespace(opus=b"plain-opus"))
            sink.write(user, SimpleNamespace(opus=b"encrypted-opus"))

        self.assertEqual(manager.accept_voice_packet.call_count, 2)
        encrypted_decoder.decode.assert_called_once_with(b"decrypted-opus", fec=False)


class MusicSpeechQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_urgent_command_overtakes_background_speech(self) -> None:
        queue = SpeechWorkQueue(4)
        queue.offer(SpeechSegment(1, b"background"), urgent=False)
        queue.offer(SpeechSegment(2, b"command"), urgent=True)

        urgent = await queue.get()
        self.assertTrue(urgent.urgent)
        self.assertEqual(urgent.segment.user_id, 2)
        queue.done(2)
        background = await queue.get()
        self.assertEqual(background.segment.user_id, 1)

    async def test_two_workers_never_reorder_one_speaker(self) -> None:
        queue = SpeechWorkQueue(4)
        queue.offer(SpeechSegment(1, b"first"), urgent=False)
        queue.offer(SpeechSegment(1, b"second"), urgent=False)
        queue.offer(SpeechSegment(2, b"other"), urgent=False)

        first = await queue.get()
        concurrent = await queue.get()
        self.assertEqual(first.segment.pcm, b"first")
        self.assertEqual(concurrent.segment.user_id, 2)
        queue.done(1)
        second = await queue.get()
        self.assertEqual(second.segment.pcm, b"second")

    async def test_urgent_command_evicts_old_background_when_full(self) -> None:
        queue = SpeechWorkQueue(2)
        queue.offer(SpeechSegment(1, b"old"), urgent=False)
        queue.offer(SpeechSegment(2, b"newer"), urgent=False)

        self.assertTrue(queue.offer(SpeechSegment(3, b"urgent"), urgent=True))

        first = await queue.get()
        queue.done(first.segment.user_id)
        second = await queue.get()
        self.assertEqual(first.segment.pcm, b"urgent")
        self.assertEqual(second.segment.pcm, b"newer")
        self.assertEqual(queue.dropped, 1)

    async def test_stale_background_is_removed_before_capacity_check(self) -> None:
        queue = SpeechWorkQueue(1)
        queue.offer(
            SpeechSegment(1, b"stale"),
            urgent=False,
            now=time.monotonic() - 10,
        )

        self.assertTrue(queue.offer(SpeechSegment(2, b"fresh"), urgent=False))

        item = await queue.get()
        self.assertEqual(item.segment.pcm, b"fresh")
        self.assertEqual(queue.dropped, 1)

    async def test_queue_stays_bounded_during_large_speech_burst(self) -> None:
        queue = SpeechWorkQueue(8)
        for index in range(2_000):
            queue.offer(
                SpeechSegment(index % 25, str(index).encode()),
                urgent=False,
            )

        self.assertLessEqual(len(queue), 8)
        self.assertEqual(queue.dropped, 1_992)
        self.assertTrue(queue.offer(SpeechSegment(99, b"urgent"), urgent=True))
        self.assertLessEqual(len(queue), 8)
        item = await queue.get()
        self.assertEqual(item.segment.pcm, b"urgent")


class MusicDeploymentTests(unittest.TestCase):
    def test_windows_launcher_merges_production_stt_defaults(self) -> None:
        example = (ROOT / ".env.persistent.example").read_text(encoding="utf-8")
        launcher = (ROOT / "run_windows.bat").read_text(encoding="utf-8")
        migration = (ROOT / "merge_env_windows.ps1").read_text(encoding="utf-8")

        self.assertIn("MUSIC_STT_MODEL=qwen/qwen3-asr-flash-2026-02-10", example)
        self.assertIn("qwen/qwen3-asr-flash-2026-02-10", example)
        self.assertIn("MUSIC_STT_HEDGE_DELAY_SECONDS=0.25", example)
        self.assertIn("MUSIC_STT_WORKERS=2", example)
        self.assertIn("merge_env_windows.ps1", launcher)
        self.assertIn("MUSIC_STT_QUEUE_LIMIT=4", migration)
        self.assertIn("MUSIC_STT_QUEUE_LIMIT=8", migration)
        self.assertIn("MUSIC_STT_TIMEOUT_SECONDS=8", migration)

    def test_shared_panel_defaults_to_one_second_reconciliation(self) -> None:
        example = (ROOT / ".env.persistent.example").read_text(encoding="utf-8")

        self.assertEqual(MUSIC_PANEL_REFRESH_SECONDS, 1.0)
        self.assertIn("MUSIC_PANEL_REFRESH_SECONDS=1", example)
        self.assertIn("MUSIC_PANEL_CHANNEL_ID=0", example)


class MusicPublicPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.guild = SimpleNamespace(id=77, voice_client=None)
        self.bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            guilds=[self.guild],
            get_guild=lambda guild_id: self.guild if guild_id == 77 else None,
            get_channel=lambda _channel_id: None,
            is_closed=lambda: False,
        )
        self.manager = MusicManager(self.bot)  # type: ignore[arg-type]
        self.service = MusicPublicPanelService(self.bot, self.manager)  # type: ignore[arg-type]
        self.service._retire_legacy_card = AsyncMock()  # type: ignore[method-assign]
        self.channel = SimpleNamespace(id=300)
        self.message = SimpleNamespace(id=400, channel=self.channel)

    async def test_public_controls_are_persistent_and_not_bound_to_one_user(
        self,
    ) -> None:
        view = PublicMusicPanelView(self.manager)
        custom_ids = [item.custom_id for item in view.children]

        self.assertIsNone(view.timeout)
        self.assertTrue(view.is_persistent())
        self.assertEqual(len(custom_ids), len(set(custom_ids)))
        self.assertTrue(all(custom_ids))
        self.assertIn("tmod_music_public_play", custom_ids)
        self.assertIn("tmod_music_public_voice_command", custom_ids)
        self.assertIn("Голос для меня", {item.label for item in view.children})

    async def test_transient_discord_outage_is_warning_not_runtime_error(self) -> None:
        response = SimpleNamespace(status=522, reason="Connection timed out", headers={})
        failure = discord.HTTPException(response, {"message": "temporary outage"})
        self.service._initialized.add(self.guild.id)
        self.service.refresh_guild = AsyncMock(side_effect=failure)  # type: ignore[method-assign]

        with self.assertLogs("modules.music_public_panel", level="WARNING") as captured:
            await self.service.reconcile_all()

        self.assertTrue(any("temporarily unavailable" in line for line in captured.output))
        self.assertFalse(any(" ERROR:" in line for line in captured.output))

    def test_voice_receiver_ignores_ssrc_after_reader_was_stopped(self) -> None:
        class FakeClient:
            def _remove_ssrc(self, *, user_id: int) -> None:
                raise AssertionError("unpatched receiver method")

        fake_extension = SimpleNamespace(VoiceRecvClient=FakeClient)
        with patch("modules.music_setup.voice_recv", fake_extension):
            self.assertTrue(_install_voice_recv_disconnect_guard())
            client = FakeClient()
            client._id_to_ssrc = {42: 7001}
            client._ssrc_to_id = {7001: 42}
            client._reader = object()

            client._remove_ssrc(user_id=42)

        self.assertEqual(client._id_to_ssrc, {})
        self.assertEqual(client._ssrc_to_id, {})

    async def test_one_second_tick_reuses_single_message_without_noop_patch(
        self,
    ) -> None:
        ensure = AsyncMock(return_value=self.message)
        with (
            patch(
                "modules.music_public_panel.resolve_control_channel",
                new=AsyncMock(return_value=self.channel),
            ),
            patch("modules.music_public_panel.ensure_panel_message", new=ensure),
            patch(
                "modules.music_public_panel.edit_message_with_retry",
                new=AsyncMock(),
            ) as edit,
        ):
            first = await self.service.refresh_guild(self.guild)
            second = await self.service.refresh_guild(self.guild)

        self.assertIs(first, self.message)
        self.assertIs(second, self.message)
        ensure.assert_awaited_once()
        edit.assert_not_awaited()

    async def test_worker_reconciles_on_one_second_cadence(self) -> None:
        self.bot.is_closed = MagicMock(side_effect=[False, True])
        self.service.reconcile_all = AsyncMock()  # type: ignore[method-assign]

        with patch(
            "modules.music_public_panel.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            await self.service._worker()

        self.service.reconcile_all.assert_awaited_once()
        delay = sleep.await_args.args[0]
        self.assertGreaterEqual(delay, 0.9)
        self.assertLessEqual(delay, 1.0)

    async def test_public_queue_response_is_private(self) -> None:
        interaction = SimpleNamespace(
            guild=self.guild,
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await PublicMusicPanelView(self.manager).queue(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            "Очередь пока не создана.", ephemeral=True
        )

    async def test_refresh_updates_shared_card_without_opening_another_menu(
        self,
    ) -> None:
        self.manager.public_panel_service = SimpleNamespace(refresh_guild=AsyncMock())
        interaction = SimpleNamespace(
            guild=self.guild,
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        await PublicMusicPanelView(self.manager).refresh(interaction)

        self.manager.public_panel_service.refresh_guild.assert_awaited_once_with(
            self.guild
        )
        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True, thinking=True
        )
        kwargs = interaction.edit_original_response.await_args.kwargs
        self.assertEqual(kwargs, {"content": "Общая музыкальная панель обновлена."})

    async def test_playback_clock_causes_real_second_by_second_panel_edit(self) -> None:
        voice_client = SimpleNamespace(
            is_connected=lambda: True,
            is_paused=lambda: False,
            is_playing=lambda: True,
        )
        self.manager._voice_client = lambda _guild_id: voice_client  # type: ignore[method-assign]
        session = MusicGuildSession(
            guild_id=77,
            voice_channel_id=100,
            text_channel_id=300,
            connected_by_id=7,
            connected_by_display="Слушатель",
            current=MusicTrack("Кино", "https://youtu.be/test", 120),
        )
        session.track_started_at = 90.0
        self.manager.sessions[77] = session
        ensure = AsyncMock(return_value=self.message)
        edit = AsyncMock(return_value=self.message)

        with (
            patch(
                "modules.music_public_panel.resolve_control_channel",
                new=AsyncMock(return_value=self.channel),
            ),
            patch("modules.music_public_panel.ensure_panel_message", new=ensure),
            patch("modules.music_public_panel.edit_message_with_retry", new=edit),
            patch(
                "modules.music_progress.time.monotonic",
                side_effect=[100.0, 100.0, 101.0, 101.0],
            ),
        ):
            await self.service.refresh_guild(self.guild)
            await self.service.refresh_guild(self.guild)

        ensure.assert_awaited_once()
        edit.assert_awaited_once()
        rendered = edit.await_args.kwargs["embed"]
        self.assertIn("`0:11`", str(rendered.fields[0].value))

    async def test_visible_state_change_edits_canonical_message_once(self) -> None:
        ensure = AsyncMock(return_value=self.message)
        edit = AsyncMock(return_value=self.message)
        with (
            patch(
                "modules.music_public_panel.resolve_control_channel",
                new=AsyncMock(return_value=self.channel),
            ),
            patch("modules.music_public_panel.ensure_panel_message", new=ensure),
            patch("modules.music_public_panel.edit_message_with_retry", new=edit),
        ):
            await self.service.refresh_guild(self.guild)
            self.manager.sessions[77] = MusicGuildSession(
                guild_id=77,
                voice_channel_id=100,
                text_channel_id=300,
                connected_by_id=7,
                connected_by_display="Слушатель",
            )
            await self.service.refresh_guild(self.guild)

        ensure.assert_awaited_once()
        edit.assert_awaited_once()


class MusicManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.member = SimpleNamespace(
            id=7,
            display_name="Слушатель",
            bot=False,
            voice=SimpleNamespace(channel=SimpleNamespace(id=100, name="Музыка")),
            guild_permissions=SimpleNamespace(manage_guild=True, administrator=False),
        )
        self.guild = SimpleNamespace(
            id=77,
            voice_client=None,
            get_member=lambda user_id: self.member if user_id == 7 else None,
            get_channel=lambda _channel_id: None,
        )
        self.member.guild = self.guild
        self.bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            get_guild=lambda guild_id: self.guild if guild_id == 77 else None,
            get_channel=lambda _channel_id: None,
        )
        self.manager = MusicManager(self.bot)  # type: ignore[arg-type]
        self.session = MusicGuildSession(
            guild_id=77,
            voice_channel_id=100,
            text_channel_id=200,
            connected_by_id=7,
            connected_by_display="Слушатель",
        )
        self.manager.sessions[77] = self.session

    async def test_wake_then_command_is_scoped_to_opted_in_user(self) -> None:
        self.session.voice_users.add(7)
        self.manager.play_listening_signal = AsyncMock()  # type: ignore[method-assign]
        self.manager.execute_voice_command = AsyncMock()  # type: ignore[method-assign]

        await self.manager._handle_transcript(self.session, 7, "Т-Мод")
        self.manager.play_listening_signal.assert_awaited_once_with(self.session)
        self.manager.execute_voice_command.assert_not_awaited()

        await self.manager._handle_transcript(self.session, 7, "включи Земфира Искала")
        self.manager.execute_voice_command.assert_awaited_once()
        command = self.manager.execute_voice_command.await_args.args[1]
        self.assertEqual(command.action, "play")
        self.assertEqual(command.query, "земфира искала")

        self.manager.execute_voice_command.reset_mock()
        await self.manager._handle_transcript(self.session, 8, "Т-Мод стоп")
        self.manager.execute_voice_command.assert_not_awaited()

    async def test_voice_intent_delegates_to_existing_player_operations(self) -> None:
        track = MusicTrack("Трек", "https://youtu.be/abc", 100)
        self.manager.enqueue = AsyncMock(return_value=track)  # type: ignore[method-assign]
        self.manager.pause = AsyncMock()  # type: ignore[method-assign]

        await self.manager.execute_voice_command(
            self.member,  # type: ignore[arg-type]
            parse_voice_command("включи трек Кино"),
        )
        self.manager.enqueue.assert_awaited_once_with(self.member, "кино")
        await self.manager.execute_voice_command(
            self.member,  # type: ignore[arg-type]
            parse_voice_command("пауза"),
        )
        self.manager.pause.assert_awaited_once_with(self.member)

    async def test_armed_command_uses_accuracy_fallback_only_when_needed(self) -> None:
        transcriber = SimpleNamespace(
            accuracy_model="openai/gpt-4o-transcribe",
            transcribe_pcm=MagicMock(
                side_effect=lambda _pcm, model=None: (
                    "играй Цой" if model else "неразборчивый разговор"
                )
            ),
        )
        self.manager.transcriber = transcriber  # type: ignore[assignment]
        self.manager.execute_voice_command = AsyncMock()  # type: ignore[method-assign]
        self.session.voice_users.add(7)
        self.session.armed_until[7] = float("inf")

        recognized = await self.manager._process_speech_segment(
            self.session,
            SpeechSegment(7, b"pcm"),
        )

        self.assertTrue(recognized)
        self.assertEqual(transcriber.transcribe_pcm.call_count, 2)
        self.assertEqual(
            transcriber.transcribe_pcm.call_args.kwargs["model"],
            "openai/gpt-4o-transcribe",
        )
        command = self.manager.execute_voice_command.await_args.args[1]
        self.assertEqual(command.action, "play")
        self.assertEqual(command.query, "цой")

    async def test_unarmed_conversation_never_uses_expensive_fallback(self) -> None:
        transcriber = SimpleNamespace(
            accuracy_model="openai/gpt-4o-transcribe",
            transcribe_pcm=MagicMock(return_value="обычный разговор"),
        )
        self.manager.transcriber = transcriber  # type: ignore[assignment]
        self.session.voice_users.add(7)

        recognized = await self.manager._process_speech_segment(
            self.session,
            SpeechSegment(7, b"pcm"),
        )

        self.assertFalse(recognized)
        transcriber.transcribe_pcm.assert_called_once_with(b"pcm")

    async def test_near_wake_word_is_corrected_before_command_execution(self) -> None:
        transcriber = SimpleNamespace(
            accuracy_models=("qwen/qwen3-asr-flash-2026-02-10",),
            transcribe_pcm=MagicMock(
                side_effect=lambda _pcm, model=None: (
                    "банан включи Цой" if model else "бонан включи Цой"
                )
            ),
        )
        self.manager.transcriber = transcriber  # type: ignore[assignment]
        self.manager.execute_voice_command = AsyncMock()  # type: ignore[method-assign]
        self.manager.play_listening_signal = AsyncMock()  # type: ignore[method-assign]
        self.session.voice_users.add(7)

        recognized = await self.manager._process_speech_segment(
            self.session,
            SpeechSegment(7, b"pcm"),
        )

        self.assertTrue(recognized)
        self.assertEqual(transcriber.transcribe_pcm.call_count, 2)
        self.manager.execute_voice_command.assert_awaited_once()
        self.manager.play_listening_signal.assert_awaited_once_with(self.session)
        command = self.manager.execute_voice_command.await_args.args[1]
        self.assertEqual(command.query, "цой")

    async def test_primary_provider_failure_recovers_through_accuracy_model(
        self,
    ) -> None:
        def transcribe(_pcm, model=None):
            if model is None:
                raise RuntimeError("primary unavailable")
            return "банан стоп"

        self.manager.transcriber = SimpleNamespace(
            accuracy_models=("openai/gpt-4o-transcribe",),
            transcribe_pcm=MagicMock(side_effect=transcribe),
        )  # type: ignore[assignment]
        self.manager.execute_voice_command = AsyncMock()  # type: ignore[method-assign]
        self.manager.play_listening_signal = AsyncMock()  # type: ignore[method-assign]
        self.session.voice_users.add(7)

        recognized = await self.manager._process_speech_segment(
            self.session,
            SpeechSegment(7, b"pcm"),
        )

        self.assertTrue(recognized)
        self.manager.execute_voice_command.assert_awaited_once()
        self.assertEqual(
            self.manager.execute_voice_command.await_args.args[1].action,
            "stop",
        )

    async def test_ambiguous_combined_command_does_not_emit_two_signals(self) -> None:
        transcriber = SimpleNamespace(
            accuracy_models=("accurate",),
            transcribe_pcm=MagicMock(
                side_effect=lambda _pcm, model=None: (
                    "банан включи Кино" if model else "банан включать кино"
                )
            ),
        )
        self.manager.transcriber = transcriber  # type: ignore[assignment]
        self.manager.execute_voice_command = AsyncMock()  # type: ignore[method-assign]
        self.manager.play_listening_signal = AsyncMock()  # type: ignore[method-assign]
        self.session.voice_users.add(7)

        await self.manager._process_speech_segment(
            self.session,
            SpeechSegment(7, b"pcm"),
        )

        self.manager.play_listening_signal.assert_awaited_once_with(self.session)
        self.manager.execute_voice_command.assert_awaited_once()

    async def test_fast_actionable_transcript_never_calls_fallback_models(self) -> None:
        transcriber = SimpleNamespace(
            accuracy_models=("accurate-one", "accurate-two"),
            transcribe_pcm=MagicMock(return_value="банан пауза"),
        )
        self.manager.transcriber = transcriber  # type: ignore[assignment]
        self.manager.execute_voice_command = AsyncMock()  # type: ignore[method-assign]
        self.manager.play_listening_signal = AsyncMock()  # type: ignore[method-assign]
        self.session.voice_users.add(7)

        recognized = await self.manager._process_speech_segment(
            self.session,
            SpeechSegment(7, b"pcm"),
        )

        self.assertTrue(recognized)
        transcriber.transcribe_pcm.assert_called_once_with(b"pcm")

    async def test_accuracy_models_really_start_in_parallel(self) -> None:
        rendezvous = threading.Barrier(2)

        def transcribe(_pcm, model=None):
            if model is None:
                return "неразборчивая команда"
            rendezvous.wait(timeout=1)
            return "играй Цой" if model == "accurate-two" else "шум"

        transcriber = SimpleNamespace(
            accuracy_models=("accurate-one", "accurate-two"),
            transcribe_pcm=MagicMock(side_effect=transcribe),
        )
        self.manager.transcriber = transcriber  # type: ignore[assignment]
        self.manager.execute_voice_command = AsyncMock()  # type: ignore[method-assign]
        self.session.voice_users.add(7)
        self.session.armed_until[7] = float("inf")

        recognized = await self.manager._process_speech_segment(
            self.session,
            SpeechSegment(7, b"pcm"),
        )

        self.assertTrue(recognized)
        self.manager.execute_voice_command.assert_awaited_once()

    async def test_slow_primary_is_hedged_before_it_finishes(self) -> None:
        rendezvous = threading.Barrier(2)

        def transcribe(_pcm, model=None):
            rendezvous.wait(timeout=1)
            return "играй Цой" if model else "неразборчивая команда"

        self.manager.transcriber = SimpleNamespace(
            accuracy_models=("fast-backup",),
            transcribe_pcm=MagicMock(side_effect=transcribe),
        )  # type: ignore[assignment]
        self.manager.execute_voice_command = AsyncMock()  # type: ignore[method-assign]
        self.session.voice_users.add(7)
        self.session.armed_until[7] = float("inf")
        started = time.monotonic()

        recognized = await self.manager._process_speech_segment(
            self.session,
            SpeechSegment(7, b"pcm"),
        )

        self.assertTrue(recognized)
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual(self.manager.transcriber.transcribe_pcm.call_count, 2)

    async def test_armed_local_control_can_win_without_waiting_for_cloud(self) -> None:
        def slow_cloud(_pcm, model=None):
            time.sleep(0.4)
            return "пауза"

        self.manager.transcriber = SimpleNamespace(
            accuracy_models=("cloud-backup",),
            transcribe_pcm=MagicMock(side_effect=slow_cloud),
        )  # type: ignore[assignment]
        self.manager.voice_control.try_local = AsyncMock(
            return_value=SimpleNamespace(
                outcome="accept",
                transcript=SimpleNamespace(
                    text="пауза",
                    model="local/tiny",
                ),
            )
        )
        self.manager.voice_control.record_recognition = AsyncMock()
        self.manager.execute_voice_command = AsyncMock()  # type: ignore[method-assign]
        self.session.voice_users.add(7)
        self.session.armed_until[7] = float("inf")
        started = time.monotonic()

        recognized = await self.manager._process_speech_segment(
            self.session,
            SpeechSegment(7, b"pcm"),
        )

        self.assertTrue(recognized)
        self.assertLess(time.monotonic() - started, 0.2)
        command = self.manager.execute_voice_command.await_args.args[1]
        self.assertEqual(command.action, "pause")
        self.manager.voice_control.record_recognition.assert_awaited_once()

    async def test_unrecognized_armed_command_gets_failure_cue_and_retry_window(
        self,
    ) -> None:
        transcriber = SimpleNamespace(
            accuracy_models=("accurate-one", "accurate-two"),
            transcribe_pcm=MagicMock(return_value="неразборчивый разговор"),
        )
        self.manager.transcriber = transcriber  # type: ignore[assignment]
        self.manager.play_listening_signal = AsyncMock()  # type: ignore[method-assign]
        self.manager.publish_status = AsyncMock()  # type: ignore[method-assign]
        self.session.voice_users.add(7)
        self.session.armed_until[7] = float("inf")
        before = time.monotonic()

        recognized = await self.manager._process_speech_segment(
            self.session,
            SpeechSegment(7, b"pcm"),
        )

        self.assertFalse(recognized)
        self.manager.play_listening_signal.assert_awaited_once_with(
            self.session,
            failed=True,
        )
        self.manager.publish_status.assert_awaited_once_with(self.session)
        self.assertGreater(self.session.armed_until[7], before)
        self.assertLess(self.session.armed_until[7], before + 10)

    async def test_personal_panel_has_complete_controls_without_connection(
        self,
    ) -> None:
        self.manager.sessions.clear()
        view = MusicPanelView(self.manager, 7, 77)
        labels = {str(item.label) for item in view.children}
        self.assertTrue(
            {
                "Подключить",
                "Включить",
                "Пауза",
                "Следующая",
                "Стоп",
                "Обновить",
                "Голосовая команда",
            }
            <= labels
        )
        play_button = next(item for item in view.children if item.label == "Включить")
        self.assertTrue(play_button.disabled)
        embed = build_music_embed(self.manager, None)
        self.assertIn("персональная панель", embed.footer.text)

    async def test_voice_command_button_never_keeps_a_stale_listening_label(
        self,
    ) -> None:
        voice_client = SimpleNamespace(
            is_connected=lambda: True,
            is_paused=lambda: False,
            is_playing=lambda: False,
        )
        self.manager._voice_client = lambda _guild_id: voice_client  # type: ignore[method-assign]
        self.session.armed_until[7] = float("inf")

        view = MusicPanelView(self.manager, 7, 77)

        button = next(
            item for item in view.children if item.label == "Голосовая команда"
        )
        self.assertFalse(button.disabled)

    async def test_one_shot_button_runs_command_without_wake_word_then_opts_out(
        self,
    ) -> None:
        self.manager.transcriber.api_key = "test-key"
        self.manager._ensure_voice_runtime = AsyncMock()  # type: ignore[method-assign]
        self.manager.play_listening_signal = AsyncMock()  # type: ignore[method-assign]
        self.manager.publish_status = AsyncMock()  # type: ignore[method-assign]
        self.manager.execute_voice_command = AsyncMock()  # type: ignore[method-assign]

        await self.manager.arm_voice_command(self.member)
        self.assertIn(7, self.session.voice_users)
        self.assertIn(7, self.session.one_shot_voice_users)
        self.manager.play_listening_signal.assert_awaited_once_with(self.session)

        await self.manager._handle_transcript(self.session, 7, "играй Цой")

        self.assertNotIn(7, self.session.voice_users)
        self.assertNotIn(7, self.session.one_shot_voice_users)
        self.manager.execute_voice_command.assert_awaited_once()
        command = self.manager.execute_voice_command.await_args.args[1]
        self.assertEqual(command.action, "play")
        self.assertEqual(command.query, "цой")

    async def test_one_shot_permission_expires_without_a_command(self) -> None:
        self.session.voice_users.add(7)
        self.session.one_shot_voice_users.add(7)
        self.session.armed_until[7] = 0
        self.manager.publish_status = AsyncMock()  # type: ignore[method-assign]

        with patch("modules.music_runtime.asyncio.sleep", new=AsyncMock()):
            await self.manager._speech_sweeper(self.session)

        self.assertNotIn(7, self.session.voice_users)
        self.assertNotIn(7, self.session.one_shot_voice_users)
        self.manager.publish_status.assert_awaited_once_with(self.session)

    async def test_persistent_voice_user_stays_enabled_after_command_window_expires(
        self,
    ) -> None:
        self.session.voice_users.add(7)
        self.session.armed_until[7] = 0
        self.manager.publish_status = AsyncMock()  # type: ignore[method-assign]

        stopped = await self.manager._expire_armed_commands(self.session, 1)

        self.assertFalse(stopped)
        self.assertIn(7, self.session.voice_users)
        self.assertNotIn(7, self.session.armed_until)
        self.assertEqual(
            self.session.last_notice, "Ожидание голосовой команды завершено."
        )
        self.manager.publish_status.assert_awaited_once_with(self.session)

    async def test_youtube_search_has_per_user_cooldown(self) -> None:
        self.session.current = MusicTrack("Играет", "https://youtu.be/current", 100)
        found = MusicTrack("Новый", "https://youtu.be/new", 120)
        self.manager.resolver.resolve = MagicMock(return_value=found)
        await self.manager.enqueue(self.member, "первый запрос")
        with self.assertRaises(MusicRuntimeError):
            await self.manager.enqueue(self.member, "слишком быстрый второй запрос")
        self.manager.resolver.resolve.assert_called_once()

    async def test_voice_opt_in_is_revoked_when_user_leaves_channel(self) -> None:
        self.session.voice_users.add(7)
        packet = b"\x01\x00\x01\x00" * 15_000
        self.session.segmenter.accept(7, packet, now=0.0)
        other_member = SimpleNamespace(bot=False)
        self.guild.get_channel = lambda channel_id: (
            SimpleNamespace(members=[other_member]) if channel_id == 100 else None
        )

        await self.manager.handle_voice_state_update(
            self.member,
            SimpleNamespace(channel=SimpleNamespace(id=100)),
            SimpleNamespace(channel=None),
        )

        self.assertNotIn(7, self.session.voice_users)
        self.assertEqual(self.session.segmenter.drain_ready(now=2.0), [])

    async def test_external_bot_move_updates_controlled_voice_channel(self) -> None:
        bot_member = SimpleNamespace(id=999, guild=self.guild)
        self.manager.publish_status = AsyncMock()  # type: ignore[method-assign]
        destination = SimpleNamespace(id=101, name="Другая музыка")

        await self.manager.handle_voice_state_update(
            bot_member,
            SimpleNamespace(channel=SimpleNamespace(id=100)),
            SimpleNamespace(channel=destination),
        )

        self.assertEqual(self.session.voice_channel_id, 101)
        self.manager.publish_status.assert_awaited_once_with(self.session)

    async def test_failed_receiver_is_restarted_without_touching_music(self) -> None:
        self.session.voice_users.add(7)
        self.session.current = MusicTrack("Играет", "https://youtu.be/current", 100)
        self.manager._ensure_voice_runtime = AsyncMock()  # type: ignore[method-assign]
        self.manager.publish_status = AsyncMock()  # type: ignore[method-assign]

        with patch("modules.music_runtime.asyncio.sleep", new=AsyncMock()):
            await self.manager._recover_voice_receiver(
                77, RuntimeError("router failed")
            )

        self.manager._ensure_voice_runtime.assert_awaited_once_with(self.session)
        self.manager.publish_status.assert_awaited_once_with(self.session)
        self.assertEqual(self.session.current.title, "Играет")
        self.assertIn("автоматически восстановлен", self.session.last_notice)

    async def test_public_status_card_is_reused_from_persistent_receipt(self) -> None:
        message = SimpleNamespace(id=902, edit=AsyncMock())
        channel = SimpleNamespace(
            id=201,
            fetch_message=AsyncMock(return_value=message),
            send=AsyncMock(),
        )
        self.bot.get_channel = lambda channel_id: (
            channel if channel_id in {200, 201} else None
        )

        def stored_value(key: str) -> str | None:
            if key.endswith(":channel_id"):
                return "201"
            if key.endswith(":message_id"):
                return "902"
            return None

        with (
            patch("modules.music_status.get_meta", side_effect=stored_value),
            patch("modules.music_status.set_meta_value") as save_receipt,
        ):
            await self.manager.publish_status(self.session)

        channel.fetch_message.assert_awaited_once_with(902)
        message.edit.assert_awaited_once()
        channel.send.assert_not_awaited()
        save_receipt.assert_not_called()
        self.assertEqual(self.session.text_channel_id, 201)
        self.assertEqual(self.session.status_message_id, 902)


if __name__ == "__main__":
    unittest.main()
