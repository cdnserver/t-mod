import unittest
import wave
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from modules.music_domain import (
    MusicInputError,
    MusicTrack,
    parse_voice_command,
    prepare_youtube_input,
    split_wake_word,
)
from modules.music_providers import OpenRouterTranscriber, YoutubeResolver, pcm_to_wav
from modules.music_runtime import (
    MusicGuildSession,
    MusicManager,
    MusicRuntimeError,
    PCMBytesSource,
    SignalMixerSource,
    SpeechSegmenter,
    TModVoiceSink,
)
from modules.music_views import MusicPanelView, build_music_embed


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
        for transcript in ("Т-Мод", "т мод", "тимод", "ти мод", "T Mod"):
            with self.subTest(transcript=transcript):
                woke, remainder = split_wake_word(transcript)
                self.assertTrue(woke)
                self.assertEqual(remainder, "")

        woke, remainder = split_wake_word("Т-Мод, включи Кино Группа крови")
        self.assertTrue(woke)
        command = parse_voice_command(remainder)
        self.assertEqual(command.action, "play")
        self.assertEqual(command.query, "кино группа крови")
        self.assertEqual(parse_voice_command("поставь на паузу").action, "pause")
        self.assertEqual(parse_voice_command("играй дальше").action, "resume")
        self.assertEqual(parse_voice_command("играй").action, "resume")
        self.assertEqual(parse_voice_command("следующая").action, "skip")
        self.assertEqual(parse_voice_command("громкость 150").volume_percent, 100)
        self.assertIsNone(parse_voice_command("давайте обсудим проект"))


class MusicProviderTests(unittest.TestCase):
    def test_pcm_is_wrapped_as_standard_discord_wav(self) -> None:
        pcm = b"\x01\x00\x02\x00" * 480
        encoded = pcm_to_wav(pcm)
        with wave.open(BytesIO(encoded), "rb") as wav:
            self.assertEqual(wav.getframerate(), 48_000)
            self.assertEqual(wav.getnchannels(), 2)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.readframes(wav.getnframes()), pcm)

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
        transcriber = OpenRouterTranscriber()
        transcriber.api_key = "test-key"
        response = SimpleNamespace(
            status_code=200,
            headers={},
            json=lambda: {"text": "Т-Мод"},
            text='{"text":"Т-Мод"}',
        )
        pcm = b"\x00\x00\x00\x00" * 480
        with patch("modules.music_providers.requests.post", return_value=response) as request:
            result = transcriber.transcribe_pcm(pcm)
        self.assertEqual(result, "Т-Мод")
        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload["language"], "ru")
        self.assertEqual(payload["input_audio"]["format"], "wav")
        self.assertNotIn("test-key", str(payload))


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
        self.assertIn("Dropped corrupt Discord voice packet", captured.output[0])


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

    async def test_personal_panel_has_complete_controls_without_connection(self) -> None:
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

    async def test_one_shot_button_runs_command_without_wake_word_then_opts_out(self) -> None:
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
            await self.manager._recover_voice_receiver(77, RuntimeError("router failed"))

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
        self.bot.get_channel = lambda channel_id: channel if channel_id in {200, 201} else None

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
