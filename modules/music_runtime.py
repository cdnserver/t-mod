"""Discord voice runtime for T-Mod Music.

The queue is intentionally ephemeral. Provider calls and Discord audio threads
never mutate it directly; every transition returns to the bot event loop.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import discord
from discord.ext import commands

from modules.music_config import (
    MUSIC_DEFAULT_VOLUME,
    MUSIC_ENABLED,
    MUSIC_IDLE_DISCONNECT_SECONDS,
    MUSIC_QUEUE_LIMIT,
    MUSIC_SEARCH_CONCURRENCY,
    MUSIC_SEARCH_COOLDOWN_SECONDS,
    MUSIC_STT_QUEUE_LIMIT,
    MUSIC_STT_TIMEOUT_SECONDS,
    MUSIC_VOICE_CONTROL_ENABLED,
    MUSIC_WAKE_TIMEOUT_SECONDS,
)
from modules.music_audio import (
    PCMBytesSource,
    SignalMixerSource,
    SpeechSegment,
    SpeechSegmenter,
    TModVoiceSink,
    listening_tone_pcm,
    voice_recv,
)
from modules.music_domain import (
    MusicTrack,
    VoiceCommand,
    parse_voice_command,
    split_wake_word,
)
from modules.music_providers import OpenRouterTranscriber, YoutubeResolver
from modules.music_status import publish_music_status


class MusicRuntimeError(RuntimeError):
    pass


@dataclass(slots=True)
class MusicGuildSession:
    guild_id: int
    voice_channel_id: int
    text_channel_id: int
    connected_by_id: int
    connected_by_display: str
    queue: deque[MusicTrack] = field(default_factory=deque)
    current: MusicTrack | None = None
    volume: float = MUSIC_DEFAULT_VOLUME
    generation: int = 0
    source: SignalMixerSource | None = None
    last_notice: str | None = None
    last_error: str | None = None
    status_message_id: int | None = None
    status_message_obj: Any | None = None
    voice_users: set[int] = field(default_factory=set)
    one_shot_voice_users: set[int] = field(default_factory=set)
    armed_until: dict[int, float] = field(default_factory=dict)
    segmenter: SpeechSegmenter = field(default_factory=SpeechSegmenter)
    speech_queue: asyncio.Queue[SpeechSegment] | None = None
    speech_sweeper_task: asyncio.Task | None = None
    speech_worker_task: asyncio.Task | None = None
    idle_disconnect_task: asyncio.Task | None = None
    receiver_restart_count: int = 0
    receiver_last_restart_at: float = 0.0
    sink: Any | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    status_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MusicManager:
    def __init__(
        self,
        bot: commands.Bot,
        *,
        resolver: YoutubeResolver | None = None,
        transcriber: OpenRouterTranscriber | None = None,
    ) -> None:
        self.bot = bot
        self.resolver = resolver or YoutubeResolver()
        self.transcriber = transcriber or OpenRouterTranscriber()
        self.sessions: dict[int, MusicGuildSession] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._resolve_semaphore = asyncio.Semaphore(MUSIC_SEARCH_CONCURRENCY)
        self._last_search_at: dict[tuple[int, int], float] = {}

    @property
    def voice_receive_available(self) -> bool:
        return bool(voice_recv is not None and MUSIC_VOICE_CONTROL_ENABLED)

    def get(self, guild_id: int) -> MusicGuildSession | None:
        return self.sessions.get(int(guild_id))

    def _voice_client(self, guild_id: int) -> discord.VoiceClient | None:
        guild = self.bot.get_guild(int(guild_id))
        voice_client = getattr(guild, "voice_client", None) if guild else None
        return voice_client if isinstance(voice_client, discord.VoiceClient) else None

    @staticmethod
    def member_voice_channel(member: discord.Member):
        state = getattr(member, "voice", None)
        return getattr(state, "channel", None)

    def require_same_voice(
        self,
        member: discord.Member,
        session: MusicGuildSession,
    ) -> None:
        channel = self.member_voice_channel(member)
        if channel is None or int(channel.id) != int(session.voice_channel_id):
            raise MusicRuntimeError(
                "Сначала зайдите в тот же голосовой канал, где находится T-Mod."
            )

    async def connect(
        self,
        member: discord.Member,
        text_channel_id: int,
    ) -> MusicGuildSession:
        if not MUSIC_ENABLED:
            raise MusicRuntimeError("Музыкальный модуль отключён в конфигурации.")
        channel = self.member_voice_channel(member)
        if channel is None or not hasattr(channel, "connect"):
            raise MusicRuntimeError("Сначала зайдите в голосовой канал.")
        self._loop = asyncio.get_running_loop()
        existing = self._voice_client(member.guild.id)
        if existing is not None and existing.is_connected():
            existing_channel_id = int(getattr(existing.channel, "id", 0) or 0)
            if existing_channel_id != int(channel.id):
                raise MusicRuntimeError(
                    f"T-Mod уже работает в <#{existing_channel_id}>. "
                    "Отключите его там перед переносом."
                )
        else:
            connect_kwargs: dict[str, Any] = {
                "self_deaf": not self.voice_receive_available,
            }
            if voice_recv is not None:
                connect_kwargs["cls"] = voice_recv.VoiceRecvClient
            try:
                existing = await channel.connect(**connect_kwargs)
            except discord.DiscordException as exc:
                if "cls" not in connect_kwargs:
                    raise MusicRuntimeError(
                        f"Не удалось подключиться к голосовому каналу: {str(exc)[:500]}"
                    ) from exc
                # Voice receive is an experimental adapter. If Discord changes
                # its receive protocol, regular music must still remain usable.
                stale_client = getattr(member.guild, "voice_client", None)
                if stale_client is not None:
                    try:
                        await stale_client.disconnect(force=True)
                    except discord.DiscordException:
                        pass
                try:
                    existing = await channel.connect(self_deaf=True)
                except discord.DiscordException as fallback_exc:
                    raise MusicRuntimeError(
                        "Не удалось подключиться к голосовому каналу: "
                        f"{str(fallback_exc)[:500]}"
                    ) from fallback_exc
        session = self.sessions.get(member.guild.id)
        if session is None:
            session = MusicGuildSession(
                guild_id=member.guild.id,
                voice_channel_id=int(channel.id),
                text_channel_id=int(text_channel_id),
                connected_by_id=member.id,
                connected_by_display=member.display_name,
            )
            self.sessions[member.guild.id] = session
        else:
            session.voice_channel_id = int(channel.id)
        if session.idle_disconnect_task and not session.idle_disconnect_task.done():
            session.idle_disconnect_task.cancel()
            session.idle_disconnect_task = None
        session.last_error = None
        session.last_notice = f"Подключено к каналу «{channel.name}»."
        await self.publish_status(session)
        return session

    async def disconnect(
        self,
        member: discord.Member,
        *,
        automatic: bool = False,
    ) -> None:
        session = self.get(member.guild.id)
        if session is None:
            raise MusicRuntimeError("T-Mod сейчас не подключён к музыке.")
        if not automatic:
            self.require_same_voice(member, session)
            if (
                member.id != session.connected_by_id
                and not member.guild_permissions.manage_guild
                and not member.guild_permissions.administrator
            ):
                raise MusicRuntimeError(
                    "Отключить бота может подключивший его участник или управляющий сервером."
                )
        await self._disable_voice_runtime(session)
        voice_client = self._voice_client(session.guild_id)
        async with session.lock:
            session.queue.clear()
            session.current = None
            session.source = None
            session.generation += 1
        if voice_client is not None:
            self._stop_playing(voice_client)
            try:
                await voice_client.disconnect(force=True)
            except discord.DiscordException:
                pass
        session.last_notice = (
            "Отключено автоматически: в голосовом канале не осталось участников."
            if automatic
            else "T-Mod отключён от голосового канала."
        )
        await self.publish_status(session, disconnected=True)
        self.sessions.pop(session.guild_id, None)

    async def enqueue(
        self,
        member: discord.Member,
        value: str,
    ) -> MusicTrack:
        session = self.get(member.guild.id)
        if session is None:
            raise MusicRuntimeError("Сначала подключите T-Mod к голосовому каналу.")
        self.require_same_voice(member, session)
        async with session.lock:
            if len(session.queue) >= MUSIC_QUEUE_LIMIT:
                raise MusicRuntimeError(
                    f"Очередь заполнена: максимум {MUSIC_QUEUE_LIMIT} композиций."
                )
        now = time.monotonic()
        search_key = (int(member.guild.id), int(member.id))
        wait_seconds = MUSIC_SEARCH_COOLDOWN_SECONDS - (
            now - self._last_search_at.get(search_key, 0.0)
        )
        if wait_seconds > 0:
            raise MusicRuntimeError(
                f"Подождите ещё {max(1, math.ceil(wait_seconds))} сек. перед новым поиском."
            )
        self._last_search_at[search_key] = now
        async with self._resolve_semaphore:
            track = await asyncio.to_thread(
                self.resolver.resolve,
                value,
                requester_id=member.id,
                requester_display=member.display_name,
            )
        async with session.lock:
            if len(session.queue) >= MUSIC_QUEUE_LIMIT:
                raise MusicRuntimeError(
                    f"Очередь заполнена: максимум {MUSIC_QUEUE_LIMIT} композиций."
                )
            session.queue.append(track)
            session.last_notice = f"Добавлено: {track.title}"
            session.last_error = None
            should_start = session.current is None
        if should_start:
            await self.start_next(session)
        else:
            await self.publish_status(session)
        return track

    async def start_next(self, session: MusicGuildSession) -> None:
        async with session.lock:
            if session.current is not None or not session.queue:
                return
            track = session.queue.popleft()
            session.current = track
            session.generation += 1
            generation = session.generation
            session.last_notice = f"Подготовка потока: {track.title}"
            session.last_error = None
        await self.publish_status(session)
        try:
            stream_url = await asyncio.to_thread(self.resolver.stream_url, track)
            raw_source = discord.FFmpegPCMAudio(
                stream_url,
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options="-vn -loglevel warning",
            )
            source = SignalMixerSource(
                discord.PCMVolumeTransformer(raw_source, volume=session.volume)
            )
        except Exception as exc:
            async with session.lock:
                if session.generation == generation:
                    session.current = None
                    session.source = None
                    session.last_error = (
                        f"Не удалось запустить «{track.title}»: {str(exc)[:500]}"
                    )
            await self.publish_status(session)
            await self.start_next(session)
            return
        voice_client = self._voice_client(session.guild_id)
        async with session.lock:
            valid = (
                session.generation == generation
                and session.current == track
                and voice_client is not None
                and voice_client.is_connected()
            )
            if valid:
                session.source = source
                session.last_notice = f"Сейчас играет: {track.title}"
        if not valid or voice_client is None:
            source.cleanup()
            return
        loop = asyncio.get_running_loop()

        def after_playback(error: Exception | None) -> None:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self._on_track_finished(session.guild_id, generation, error)
                )
            )

        try:
            voice_client.play(source, after=after_playback)
        except (discord.ClientException, TypeError) as exc:
            source.cleanup()
            async with session.lock:
                if session.generation == generation:
                    session.current = None
                    session.source = None
                    session.last_error = f"Discord не запустил аудио: {str(exc)[:500]}"
            await self.publish_status(session)
            await self.start_next(session)
            return
        await self.publish_status(session)

    async def _on_track_finished(
        self,
        guild_id: int,
        generation: int,
        error: Exception | None,
    ) -> None:
        session = self.get(guild_id)
        if session is None:
            return
        async with session.lock:
            if session.generation != int(generation):
                return
            finished = session.current
            session.current = None
            session.source = None
            if error is not None:
                session.last_error = f"Ошибка аудиопотока: {str(error)[:500]}"
            elif finished is not None:
                session.last_notice = f"Завершено: {finished.title}"
        await self.publish_status(session)
        await self.start_next(session)

    @staticmethod
    def _stop_playing(voice_client: discord.VoiceClient) -> None:
        stop_playing = getattr(voice_client, "stop_playing", None)
        if callable(stop_playing):
            stop_playing()
        else:
            voice_client.stop()

    async def pause(self, member: discord.Member) -> None:
        session = self._controlled_session(member)
        voice_client = self._voice_client(member.guild.id)
        if voice_client is None or not voice_client.is_playing():
            raise MusicRuntimeError("Сейчас нечего ставить на паузу.")
        voice_client.pause()
        session.last_notice = "Воспроизведение приостановлено."
        await self.publish_status(session)

    async def resume(self, member: discord.Member) -> None:
        session = self._controlled_session(member)
        voice_client = self._voice_client(member.guild.id)
        if voice_client is None or not voice_client.is_paused():
            raise MusicRuntimeError("Музыка сейчас не стоит на паузе.")
        voice_client.resume()
        session.last_notice = "Воспроизведение продолжено."
        await self.publish_status(session)

    async def skip(self, member: discord.Member) -> None:
        session = self._controlled_session(member)
        voice_client = self._voice_client(member.guild.id)
        if session.current is None or voice_client is None:
            raise MusicRuntimeError("Сейчас нет композиции, которую можно пропустить.")
        session.last_notice = f"Пропущено: {session.current.title}"
        self._stop_playing(voice_client)
        await self.publish_status(session)

    async def stop(self, member: discord.Member) -> None:
        session = self._controlled_session(member)
        voice_client = self._voice_client(member.guild.id)
        async with session.lock:
            session.queue.clear()
            session.current = None
            session.source = None
            session.generation += 1
            session.last_notice = "Воспроизведение остановлено, очередь очищена."
        if voice_client is not None:
            self._stop_playing(voice_client)
        await self.publish_status(session)

    async def set_volume(self, member: discord.Member, percent: int) -> int:
        session = self._controlled_session(member)
        clean = max(5, min(100, int(percent)))
        session.volume = clean / 100
        if session.source is not None:
            session.source.volume = session.volume
        session.last_notice = f"Громкость: {clean}%."
        await self.publish_status(session)
        return clean

    def _controlled_session(self, member: discord.Member) -> MusicGuildSession:
        session = self.get(member.guild.id)
        if session is None:
            raise MusicRuntimeError("T-Mod сейчас не подключён к музыке.")
        self.require_same_voice(member, session)
        return session

    async def toggle_voice_control(self, member: discord.Member) -> bool:
        session = self._controlled_session(member)
        if not self.voice_receive_available:
            raise MusicRuntimeError(
                "Приём голосовых команд недоступен: проверьте voice-receive в контейнере."
            )
        if not self.transcriber.configured:
            raise MusicRuntimeError(
                "Для голосового управления не настроен OPENROUTER_API_KEY."
            )
        was_listening = member.id in session.voice_users
        was_one_shot = member.id in session.one_shot_voice_users
        enabled = not was_listening or was_one_shot
        if enabled:
            session.voice_users.add(member.id)
            session.one_shot_voice_users.discard(member.id)
            session.last_notice = (
                f"{member.display_name} включил голосовое управление для себя. "
                "Скажите «Банан», «Сборщик риса» или «Т-Мод» либо нажмите «Голосовая команда»."
            )
            try:
                await self._ensure_voice_runtime(session)
            except Exception:
                if not was_listening:
                    session.voice_users.discard(member.id)
                if was_one_shot:
                    session.one_shot_voice_users.add(member.id)
                raise
        else:
            session.voice_users.discard(member.id)
            session.one_shot_voice_users.discard(member.id)
            session.armed_until.pop(member.id, None)
            session.segmenter.discard(member.id)
            session.last_notice = (
                f"{member.display_name} отключил голосовое управление для себя."
            )
            if not session.voice_users:
                await self._disable_voice_runtime(session)
        await self.publish_status(session)
        return enabled

    async def arm_voice_command(self, member: discord.Member) -> None:
        """Listen for one command without requiring the wake word."""

        session = self._controlled_session(member)
        if not self.voice_receive_available:
            raise MusicRuntimeError(
                "Приём голосовых команд недоступен: проверьте voice-receive в контейнере."
            )
        if not self.transcriber.configured:
            raise MusicRuntimeError(
                "Для голосового управления не настроен OPENROUTER_API_KEY."
            )
        temporary = member.id not in session.voice_users
        session.voice_users.add(member.id)
        if temporary:
            session.one_shot_voice_users.add(member.id)
        session.armed_until[member.id] = time.monotonic() + MUSIC_WAKE_TIMEOUT_SECONDS
        try:
            await self._ensure_voice_runtime(session)
        except Exception:
            session.armed_until.pop(member.id, None)
            if temporary:
                session.voice_users.discard(member.id)
                session.one_shot_voice_users.discard(member.id)
            raise
        session.last_error = None
        session.last_notice = (
            f"{member.display_name}: слушаю одну команду без ключевой фразы."
        )
        await self.play_listening_signal(session)
        await self.publish_status(session)

    async def _ensure_voice_runtime(self, session: MusicGuildSession) -> None:
        voice_client = self._voice_client(session.guild_id)
        if (
            voice_client is None
            or voice_recv is None
            or not isinstance(
                voice_client,
                voice_recv.VoiceRecvClient,
            )
        ):
            raise MusicRuntimeError(
                "Голосовой клиент подключён без поддержки приёма. Переподключите T-Mod."
            )
        if session.speech_queue is None:
            session.speech_queue = asyncio.Queue(maxsize=MUSIC_STT_QUEUE_LIMIT)
        if session.speech_worker_task is None or session.speech_worker_task.done():
            session.speech_worker_task = asyncio.create_task(
                self._speech_worker(session),
                name=f"music-stt:{session.guild_id}",
            )
        if session.speech_sweeper_task is None or session.speech_sweeper_task.done():
            session.speech_sweeper_task = asyncio.create_task(
                self._speech_sweeper(session),
                name=f"music-speech-sweeper:{session.guild_id}",
            )
        if not voice_client.is_listening():
            session.sink = TModVoiceSink(self, session.guild_id)
            loop = asyncio.get_running_loop()

            def after_receiver(error: Exception | None) -> None:
                if error is None:
                    return
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        self._recover_voice_receiver(session.guild_id, error)
                    )
                )

            voice_client.listen(session.sink, after=after_receiver)

    async def _recover_voice_receiver(
        self,
        guild_id: int,
        error: Exception,
    ) -> None:
        session = self.get(guild_id)
        if session is None or not session.voice_users:
            return
        now = time.monotonic()
        if now - session.receiver_last_restart_at > 60:
            session.receiver_restart_count = 0
        session.receiver_restart_count += 1
        session.receiver_last_restart_at = now
        if session.receiver_restart_count > 3:
            session.last_error = (
                "Голосовой приём остановлен после трёх сбоев. "
                "Переподключите T-Mod; кнопки музыки продолжают работать."
            )
            await self.publish_status(session)
            return
        await asyncio.sleep(min(4.0, 2 ** (session.receiver_restart_count - 1)))
        current = self.get(guild_id)
        if current is not session or not session.voice_users:
            return
        try:
            await self._ensure_voice_runtime(session)
        except Exception as recovery_error:
            session.last_error = (
                f"Не удалось восстановить голосовой приём: {str(recovery_error)[:500]}"
            )
        else:
            session.last_notice = (
                "Голосовой приём автоматически восстановлен после сбоя "
                f"{type(error).__name__}."
            )
            session.last_error = None
        await self.publish_status(session)

    async def _disable_voice_runtime(self, session: MusicGuildSession) -> None:
        voice_client = self._voice_client(session.guild_id)
        if voice_client is not None:
            is_listening = getattr(voice_client, "is_listening", None)
            stop_listening = getattr(voice_client, "stop_listening", None)
            if callable(is_listening) and is_listening() and callable(stop_listening):
                stop_listening()
        current_task = asyncio.current_task()
        for task in (session.speech_sweeper_task, session.speech_worker_task):
            if task is not None and task is not current_task and not task.done():
                task.cancel()
        session.speech_sweeper_task = None
        session.speech_worker_task = None
        session.speech_queue = None
        session.sink = None
        session.receiver_restart_count = 0
        session.receiver_last_restart_at = 0.0
        session.voice_users.clear()
        session.one_shot_voice_users.clear()
        session.armed_until.clear()
        session.segmenter.clear()

    async def _release_one_shot_user(
        self,
        session: MusicGuildSession,
        user_id: int,
    ) -> bool:
        if user_id not in session.one_shot_voice_users:
            return False
        session.one_shot_voice_users.discard(user_id)
        session.voice_users.discard(user_id)
        session.armed_until.pop(user_id, None)
        session.segmenter.discard(user_id)
        if not session.voice_users:
            await self._disable_voice_runtime(session)
        return True

    def accept_voice_packet(self, guild_id: int, user_id: int, pcm: bytes) -> None:
        session = self.get(guild_id)
        loop = self._loop
        if session is None or loop is None or user_id not in session.voice_users:
            return
        completed = session.segmenter.accept(user_id, pcm)
        for segment in completed:
            loop.call_soon_threadsafe(self._enqueue_speech_segment, guild_id, segment)

    def _enqueue_speech_segment(self, guild_id: int, segment: SpeechSegment) -> None:
        session = self.get(guild_id)
        if (
            session is None
            or segment.user_id not in session.voice_users
            or session.speech_queue is None
            or session.speech_queue.full()
        ):
            return
        if segment.user_id in session.one_shot_voice_users:
            session.armed_until[segment.user_id] = (
                time.monotonic() + MUSIC_STT_TIMEOUT_SECONDS + 5
            )
        session.speech_queue.put_nowait(segment)

    async def _speech_sweeper(self, session: MusicGuildSession) -> None:
        try:
            while session.voice_users:
                await asyncio.sleep(0.25)
                for segment in session.segmenter.drain_ready():
                    self._enqueue_speech_segment(session.guild_id, segment)
                if await self._expire_armed_commands(session, time.monotonic()):
                    return
        except asyncio.CancelledError:
            return

    async def _expire_armed_commands(
        self,
        session: MusicGuildSession,
        now: float,
    ) -> bool:
        """Expire both one-shot and persistent users' command windows."""

        expired = [
            user_id
            for user_id, deadline in session.armed_until.items()
            if deadline < now
        ]
        if not expired:
            return False
        for user_id in expired:
            session.armed_until.pop(user_id, None)
            session.segmenter.discard(user_id)
            await self._release_one_shot_user(session, user_id)
        session.last_notice = "Ожидание голосовой команды завершено."
        await self.publish_status(session)
        return not session.voice_users

    async def _speech_worker(self, session: MusicGuildSession) -> None:
        try:
            while session.voice_users and session.speech_queue is not None:
                segment = await session.speech_queue.get()
                if segment.user_id not in session.voice_users:
                    continue
                try:
                    transcript = await asyncio.to_thread(
                        self.transcriber.transcribe_pcm,
                        segment.pcm,
                    )
                except Exception as exc:
                    session.last_error = f"Голосовое управление: {str(exc)[:500]}"
                    await self._release_one_shot_user(
                        session,
                        segment.user_id,
                    )
                    await self.publish_status(session)
                    continue
                await self._handle_transcript(session, segment.user_id, transcript)
        except asyncio.CancelledError:
            return

    async def _handle_transcript(
        self,
        session: MusicGuildSession,
        user_id: int,
        transcript: str,
    ) -> None:
        guild = self.bot.get_guild(session.guild_id)
        member = guild.get_member(int(user_id)) if guild else None
        if member is None or user_id not in session.voice_users:
            return
        try:
            self.require_same_voice(member, session)
        except MusicRuntimeError:
            return
        now = time.monotonic()
        woke, remainder = split_wake_word(transcript)
        armed = session.armed_until.get(user_id, 0) >= now
        command: VoiceCommand | None = None
        if woke:
            session.armed_until[user_id] = now + MUSIC_WAKE_TIMEOUT_SECONDS
            command = parse_voice_command(remainder) if remainder else None
            await self.play_listening_signal(session)
        elif armed:
            command = parse_voice_command(transcript)
        if command is None:
            return
        session.armed_until.pop(user_id, None)
        await self._release_one_shot_user(session, user_id)
        session.last_error = None
        try:
            await self.execute_voice_command(member, command)
        except Exception as exc:
            session.last_error = f"Голосовая команда не выполнена: {str(exc)[:500]}"
            await self.publish_status(session)

    async def execute_voice_command(
        self,
        member: discord.Member,
        command: VoiceCommand,
    ) -> None:
        session = self._controlled_session(member)
        action = command.action
        if action == "play" and command.query:
            await self.enqueue(member, command.query)
        elif action == "pause":
            await self.pause(member)
        elif action == "resume":
            await self.resume(member)
        elif action == "skip":
            await self.skip(member)
        elif action == "stop":
            await self.stop(member)
        elif action == "disconnect":
            await self.disconnect(member)
        elif action == "volume_up":
            await self.set_volume(member, round(session.volume * 100) + 10)
        elif action == "volume_down":
            await self.set_volume(member, round(session.volume * 100) - 10)
        elif action == "volume_set" and command.volume_percent is not None:
            await self.set_volume(member, command.volume_percent)
        elif action == "queue":
            session.last_notice = f"В очереди композиций: {len(session.queue)}."
            await self.publish_status(session)
        elif action == "now":
            session.last_notice = (
                f"Сейчас играет: {session.current.title}"
                if session.current
                else "Сейчас музыка не воспроизводится."
            )
            await self.publish_status(session)

    async def play_listening_signal(self, session: MusicGuildSession) -> None:
        voice_client = self._voice_client(session.guild_id)
        if voice_client is None or not voice_client.is_connected():
            return
        if session.source is not None and (
            voice_client.is_playing() or voice_client.is_paused()
        ):
            was_paused = voice_client.is_paused()
            session.source.trigger_signal()
            if was_paused:
                voice_client.resume()
            await asyncio.sleep(0.24)
            if was_paused and voice_client.is_playing():
                voice_client.pause()
            return
        try:
            voice_client.play(PCMBytesSource(listening_tone_pcm()))
        except discord.ClientException:
            return
        await asyncio.sleep(0.22)

    async def publish_status(
        self,
        session: MusicGuildSession,
        *,
        disconnected: bool = False,
    ) -> None:
        await publish_music_status(self, session, disconnected=disconnected)

    async def handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        session = self.get(member.guild.id)
        if session is None:
            return
        before_channel_id = int(getattr(before.channel, "id", 0) or 0)
        after_channel_id = int(getattr(after.channel, "id", 0) or 0)
        if self.bot.user is not None and member.id == self.bot.user.id:
            if before_channel_id != session.voice_channel_id:
                return
            if after.channel is None:
                await self._disable_voice_runtime(session)
                session.last_notice = "Голосовое подключение завершено Discord."
                await self.publish_status(session, disconnected=True)
                self.sessions.pop(session.guild_id, None)
                return
            session.voice_channel_id = after_channel_id
            session.last_notice = f"T-Mod перемещён в канал «{after.channel.name}»."
            await self.publish_status(session)
            return
        touched = {
            before_channel_id,
            after_channel_id,
        }
        if session.voice_channel_id not in touched:
            return
        if (
            member.id in session.voice_users
            and before_channel_id == session.voice_channel_id
            and after_channel_id != session.voice_channel_id
        ):
            session.voice_users.discard(member.id)
            session.one_shot_voice_users.discard(member.id)
            session.armed_until.pop(member.id, None)
            session.segmenter.discard(member.id)
            if not session.voice_users:
                await self._disable_voice_runtime(session)
            session.last_notice = f"Голосовое управление {member.display_name} отключено после выхода из канала."
            await self.publish_status(session)
        channel = member.guild.get_channel(session.voice_channel_id)
        humans = [item for item in getattr(channel, "members", ()) if not item.bot]
        if humans:
            if session.idle_disconnect_task and not session.idle_disconnect_task.done():
                session.idle_disconnect_task.cancel()
                session.idle_disconnect_task = None
            return
        if session.idle_disconnect_task and not session.idle_disconnect_task.done():
            return

        async def disconnect_when_empty() -> None:
            try:
                await asyncio.sleep(MUSIC_IDLE_DISCONNECT_SECONDS)
                current = self.get(member.guild.id)
                voice_channel = member.guild.get_channel(session.voice_channel_id)
                remaining = [
                    item
                    for item in getattr(voice_channel, "members", ())
                    if not item.bot
                ]
                if current is session and not remaining:
                    await self.disconnect(member, automatic=True)
            except asyncio.CancelledError:
                return
            except Exception:
                return

        session.idle_disconnect_task = asyncio.create_task(
            disconnect_when_empty(),
            name=f"music-idle-disconnect:{session.guild_id}",
        )


__all__ = [
    "MusicGuildSession",
    "MusicManager",
    "MusicRuntimeError",
    "PCMBytesSource",
    "SignalMixerSource",
    "SpeechSegment",
    "SpeechSegmenter",
    "TModVoiceSink",
]
