from __future__ import annotations

import asyncio
import re
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any

import discord
from discord.ext import commands

from modules.consensus_core import (
    ConsensusStateError,
    LiveConsensusSession,
    LiveParticipant,
)
from modules.consensus_runtime import (
    active_sessions as _active_sessions,
    coordinator as _consensus,
    session_lock as consensus_session_lock,
)
from modules.consensus_service import ConsensusActor
from modules.operations_runtime import wake_operations_worker
from modules.tvrs_config import (
    TVRS_CHAIR_ROLE_ID,
    TVRS_CONSENSUS_VOICE_CHANNEL_ID,
    TVRS_DISCUSSION_CATEGORY_ID,
    TVRS_SENATOR_ROLE_ID,
)
from modules.tvrs_embeds import (
    build_discussion_embed,
    build_result_embed,
)
from modules.tvrs_formatting import (
    format_bill_number,
    format_timer,
    now_local,
    role_label,
)

from modules.tvrs_presentation import (
    build_dm_vote_embed,
    consensus_bill_id,
    consensus_generation_matches,
    edit_session_host_message,
    edit_vote_dm_to_result,
)
from modules.tvrs_consensus_views import TVRSAfterResultView, TVRSVoteView

async def update_host_vote_message(*args, **kwargs):
    from modules.tvrs_control import update_host_vote_message as _implementation
    return await _implementation(*args, **kwargs)


async def update_public_consensus_card(*args, **kwargs):
    from modules.tvrs_control import update_public_consensus_card as _implementation

    return await _implementation(*args, **kwargs)


async def finalize_current_vote(*args, **kwargs):
    from modules.tvrs_decision import finalize_current_vote as _implementation
    return await _implementation(*args, **kwargs)

async def cancel_vote_timer(session: LiveConsensusSession) -> None:
    task = session.timer_task
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
    session.timer_task = None
    session.timer_deadline = None
    session.timer_seconds = None


async def set_vote_timer(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    seconds: int,
    *,
    expected_bill_id: int | None = None,
) -> None:
    async with consensus_session_lock(session.guild_id):
        if not consensus_generation_matches(
            session,
            stage="voting",
            bill_id=expected_bill_id,
        ) or session.current_bill is None:
            raise ConsensusStateError("Таймер можно установить только во время голосования.")
        previous_task = session.timer_task
        with _consensus.mutation(session):
            session.timer_seconds = int(seconds)
            session.timer_deadline = datetime.now(timezone.utc) + timedelta(seconds=int(seconds))
            await asyncio.to_thread(
                _consensus.save,
                session,
                "timer_set",
                actor=ConsensusActor(session.leader_id, session.leader_display),
                details={"seconds": int(seconds)},
            )
        if previous_task and previous_task is not asyncio.current_task() and not previous_task.done():
            previous_task.cancel()
        session.timer_task = None
        schedule_vote_timer_task(
            bot,
            guild,
            session,
            int(seconds),
            bill_id=consensus_bill_id(session),
        )

    await update_all_vote_dms(
        guild,
        session,
        content=f"Установлен таймер голосования: {format_timer(seconds)}.",
    )
    await update_host_vote_message(bot, guild, session)


def schedule_vote_timer_task(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    seconds: int,
    *,
    bill_id: int | None = None,
) -> None:
    seconds = max(0, int(seconds))
    expected_bill_id = int(bill_id if bill_id is not None else consensus_bill_id(session))

    async def runner() -> None:
        try:
            await asyncio.sleep(seconds)
            current = _active_sessions.get(guild.id)
            if (
                current is session
                and consensus_generation_matches(
                    session,
                    stage="voting",
                    bill_id=expected_bill_id,
                )
            ):
                await finalize_current_vote(
                    bot,
                    guild,
                    session,
                    forced=True,
                    expected_bill_id=expected_bill_id,
                )
        except asyncio.CancelledError:
            return
        except Exception:
            traceback.print_exc()

    session.timer_task = asyncio.create_task(runner())


async def update_all_vote_dms(guild: discord.Guild, session: LiveConsensusSession, content: str | None = None) -> None:
    expected_stage = str(session.stage)
    expected_bill_id = consensus_bill_id(session)
    if expected_stage not in {"voting", "paused", "discussion_type", "discussion"}:
        return
    receipts: dict[int, tuple[int | None, bool]] = {}
    for p in list(session.confirmed_participants()):
        if not consensus_generation_matches(
            session,
            stage=expected_stage,
            bill_id=expected_bill_id,
        ):
            return
        if p.user_id == session.leader_id:
            continue
        member = guild.get_member(p.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(p.user_id)
            except discord.DiscordException:
                receipts[p.user_id] = (None, True)
                continue
        embed = build_dm_vote_embed(session, p)
        view = TVRSVoteView(
            session.session_key,
            p.user_id,
            bill_id=expected_bill_id,
        )
        if p.vote_message_id:
            try:
                dm_channel = member.dm_channel or await member.create_dm()
                msg = await dm_channel.fetch_message(p.vote_message_id)
                await msg.edit(content=content, embed=embed, view=view)
                receipts[p.user_id] = (int(msg.id), False)
                continue
            except discord.DiscordException:
                pass
        try:
            dm = await member.send(content=content, embed=embed, view=view)
            receipts[p.user_id] = (int(dm.id), False)
        except discord.DiscordException:
            receipts[p.user_id] = (None, True)
    async with consensus_session_lock(session.guild_id):
        current = _active_sessions.get(session.guild_id)
        if current is not session or not consensus_generation_matches(
            session,
            stage=expected_stage,
            bill_id=expected_bill_id,
        ):
            return
        with _consensus.mutation(session):
            for user_id, (message_id, failed) in receipts.items():
                participant = session.participants.get(user_id)
                if participant is None:
                    continue
                if message_id is not None:
                    participant.vote_message_id = message_id
                    participant.vote_bill_id = expected_bill_id or None
                    participant.dm_failed = False
                elif failed:
                    participant.dm_failed = True
            await asyncio.to_thread(
                _consensus.save,
                session,
                "vote_messages_refreshed",
                details={
                    "bill_id": expected_bill_id or None,
                    "updated_count": sum(
                        1 for message_id, _ in receipts.values() if message_id is not None
                    ),
                    "failed_count": sum(1 for _, failed in receipts.values() if failed),
                },
            )


async def request_discussion(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    initiator: LiveParticipant,
    *,
    expected_bill_id: int | None = None,
) -> None:
    async with consensus_session_lock(session.guild_id):
        if not consensus_generation_matches(
            session,
            stage="voting",
            bill_id=expected_bill_id,
        ) or session.current_bill is None:
            return
        if session.discussion_initiator_id:
            return
        await cancel_vote_timer(session)
        await asyncio.to_thread(_consensus.request_discussion, session, initiator)
    wake_operations_worker()
    await update_all_vote_dms(guild, session, content=f"<@{initiator.user_id}> инициировал дискуссию. Голосование временно приостановлено.")
    await update_host_vote_message(bot, guild, session)


async def start_discussion_channel(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    discussion_type: str,
    *,
    expected_bill_id: int | None = None,
) -> None:
    if session.current_bill is None or not consensus_generation_matches(
        session,
        stage="discussion_type",
        bill_id=expected_bill_id,
    ):
        return
    category = guild.get_channel(TVRS_DISCUSSION_CATEGORY_ID)
    if category is None:
        try:
            fetched = await bot.fetch_channel(TVRS_DISCUSSION_CATEGORY_ID)  # type: ignore[attr-defined]
            category = fetched if isinstance(fetched, discord.CategoryChannel) else None
        except discord.DiscordException:
            category = None
    overwrites: dict[Any, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True) if guild.me else discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    senator_role = guild.get_role(TVRS_SENATOR_ROLE_ID)
    chair_role = guild.get_role(TVRS_CHAIR_ROLE_ID)
    if senator_role:
        overwrites[senator_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
    if chair_role:
        overwrites[chair_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
    number = format_bill_number(int(session.current_bill.get("bill_number") or 0))
    safe_title = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ_-]+", "-", str(session.current_bill.get("title") or "project")).strip("-")[:35]
    channel_name = f"дискуссия-{number}-{safe_title}"[:90]
    created = None
    try:
        created = await guild.create_text_channel(channel_name, category=category if isinstance(category, discord.CategoryChannel) else None, overwrites=overwrites, reason="TVRS live consensus discussion")
    except discord.DiscordException:
        created = None
    allowed: set[int] = {p.user_id for p in session.confirmed_participants() if p.kind == "senator"}
    non_leader_chair = next((p for p in session.confirmed_participants() if p.kind == "chair" and p.user_id != session.leader_id), None)
    if non_leader_chair:
        allowed.add(non_leader_chair.user_id)
    async with consensus_session_lock(session.guild_id):
        if session.current_bill is None or not consensus_generation_matches(
            session,
            stage="discussion_type",
            bill_id=expected_bill_id,
        ):
            if created is not None:
                try:
                    await created.delete(reason="TVRS discussion state changed before activation")
                except discord.DiscordException:
                    pass
            return
        await asyncio.to_thread(
            _consensus.begin_discussion,
            session,
            discussion_type,
            channel_id=created.id if created else None,
            allowed_user_ids=allowed,
        )
    wake_operations_worker()
    if created:
        try:
            await created.send(embed=build_discussion_embed(session), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.DiscordException:
            pass
    for p in session.confirmed_participants():
        if session.stage != "discussion":
            break
        if p.user_id not in allowed:
            continue
        member = guild.get_member(p.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(p.user_id)
            except discord.DiscordException:
                continue
        try:
            msg = await member.send(
                content="Дискуссия начата. Ответьте именно на это сообщение, чтобы бот перенес вашу позицию и материалы в канал дискуссии.",
                embed=build_discussion_embed(session),
            )
            p.discussion_message_id = msg.id
        except discord.DiscordException:
            p.dm_failed = True
    async with consensus_session_lock(session.guild_id):
        if session.stage == "discussion":
            await asyncio.to_thread(_consensus.save, session, "discussion_invitations_sent")
    await update_all_vote_dms(guild, session, content="Дискуссия начата. Голосование временно скрыто.")
    await update_host_vote_message(bot, guild, session)


async def forward_discussion_message(bot: commands.Bot, message: discord.Message) -> bool:
    if message.guild is not None or message.author.bot:
        return False
    for session in list(_active_sessions.values()):
        if session.finished or session.stage != "discussion" or not session.discussion_channel_id:
            continue
        if message.author.id not in session.discussion_allowed_user_ids:
            continue
        p = session.participants.get(message.author.id)
        if not p:
            continue
        ref_id = getattr(getattr(message, "reference", None), "message_id", None)
        if p.discussion_message_id and ref_id and int(ref_id) != int(p.discussion_message_id):
            continue
        if p.discussion_message_id and not ref_id:
            # Accept free DM while discussion is active; mobile clients often forget reply references.
            pass
        channel = bot.get_channel(session.discussion_channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(session.discussion_channel_id)
            except discord.DiscordException:
                channel = None
        if not hasattr(channel, "send"):
            return False
        embed = discord.Embed(
            title=f"Материал дискуссии от {p.display_name}",
            description=(message.content or "Без текста")[:3900],
            color=0x8FAADC,
            timestamp=now_local(),
        )
        embed.add_field(name="Роль", value=role_label(p), inline=True)
        if session.current_bill:
            embed.add_field(name="Законопроект", value=format_bill_number(int(session.current_bill.get("bill_number") or 0)), inline=True)
        files = []
        for att in message.attachments[:5]:
            try:
                files.append(await att.to_file())
            except discord.DiscordException:
                pass
        try:
            await channel.send(embed=embed, files=files, allowed_mentions=discord.AllowedMentions.none())  # type: ignore[attr-defined]
            try:
                await message.add_reaction("✅")
            except discord.DiscordException:
                pass
            return True
        except discord.DiscordException:
            return False
    return False


async def end_discussion(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    *,
    expected_stage: str | None = None,
    expected_bill_id: int | None = None,
) -> None:
    if expected_stage is not None and not consensus_generation_matches(
        session,
        stage=expected_stage,
        bill_id=expected_bill_id,
    ):
        return
    if session.stage not in {"discussion_type", "discussion"}:
        return
    if session.discussion_channel_id:
        channel = guild.get_channel(session.discussion_channel_id)
        if hasattr(channel, "send"):
            try:
                await channel.send("Дискуссия завершена ведущим. Голосование возвращено в активный режим.")  # type: ignore[attr-defined]
            except discord.DiscordException:
                pass
    async with consensus_session_lock(session.guild_id):
        if expected_stage is not None and not consensus_generation_matches(
            session,
            stage=expected_stage,
            bill_id=expected_bill_id,
        ):
            return
        if session.stage not in {"discussion_type", "discussion"}:
            return
        await asyncio.to_thread(
            _consensus.end_discussion,
            session,
            actor=ConsensusActor(session.leader_id, session.leader_display),
        )
    wake_operations_worker()
    await update_all_vote_dms(guild, session, content="Дискуссия завершена. Голосование снова открыто.")
    await update_host_vote_message(bot, guild, session)


async def pause_session(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    reason: str,
    automatic: bool = False,
    *,
    expected_stage: str | None = None,
    expected_bill_id: int | None = None,
) -> None:
    async with consensus_session_lock(session.guild_id):
        if expected_stage is not None and not consensus_generation_matches(
            session,
            stage=expected_stage,
            bill_id=expected_bill_id,
        ):
            return
        if session.stage == "paused":
            return
        await cancel_vote_timer(session)
        await asyncio.to_thread(
            _consensus.pause,
            session,
            reason,
            automatic=automatic,
            actor=None if automatic else ConsensusActor(session.leader_id, session.leader_display),
        )
    wake_operations_worker()
    await update_all_vote_dms(guild, session, content=reason)
    await update_host_vote_message(bot, guild, session)


def session_voice_quorum_ready(guild: discord.Guild, session: LiveConsensusSession) -> tuple[bool, str]:
    channel = guild.get_channel(TVRS_CONSENSUS_VOICE_CHANNEL_ID)
    if not isinstance(channel, discord.VoiceChannel):
        return False, "Голосовой канал консенсуса не найден."
    voice_ids = {m.id for m in channel.members if not m.bot}
    confirmed = [p for p in session.confirmed_participants() if p.user_id in voice_ids]
    chairs = [p for p in confirmed if p.kind == "chair"]
    senators = [p for p in confirmed if p.kind == "senator"]
    rules = session.rules
    ok = (
        len(chairs) >= rules.minimum_chairs
        and len(senators) >= rules.minimum_senators
        and (not rules.senators_must_be_odd or len(senators) % 2 == 1)
    )
    if ok:
        return True, f"Кворум сохранен: председатели `{len(chairs)}`, сенаторы `{len(senators)}`."
    return False, f"Кворум утрачен: председатели `{len(chairs)}`, сенаторы `{len(senators)}`. Консенсус приостановлен."


async def check_realtime_quorum(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession) -> None:
    if session.finished or session.stage in {"registration", "finalizing"}:
        return
    ok, reason = session_voice_quorum_ready(guild, session)
    if not ok and session.stage != "paused":
        await pause_session(bot, guild, session, reason, automatic=True)


async def resume_session(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    *,
    expected_stage: str | None = None,
    expected_bill_id: int | None = None,
) -> None:
    quorum_failed = False
    async with consensus_session_lock(session.guild_id):
        if expected_stage is not None and not consensus_generation_matches(
            session,
            stage=expected_stage,
            bill_id=expected_bill_id,
        ):
            return
        if session.stage != "paused":
            return
        ok, _ = session_voice_quorum_ready(guild, session)
        if not ok:
            quorum_failed = True
            target = ""
        else:
            target = await asyncio.to_thread(
                _consensus.resume,
                session,
                actor=ConsensusActor(session.leader_id, session.leader_display),
            )
    if quorum_failed:
        await update_host_vote_message(bot, guild, session)
        return
    if target in {"discussion_type", "discussion"}:
        content = "Кворум восстановлен. Дискуссия продолжается."
    elif target == "after_result":
        content = "Кворум восстановлен. Можно переходить к следующему проекту."
    else:
        content = "Кворум восстановлен. Голосование продолжается."
    wake_operations_worker()
    if target == "after_result" and session.results:
        embed = build_result_embed(session.results[-1], session)
        for participant in session.confirmed_participants():
            await edit_vote_dm_to_result(guild, session, participant, embed, content=content)
        await edit_session_host_message(
            session,
            embed=embed,
            view=TVRSAfterResultView(session.session_key),
        )
        await update_public_consensus_card(bot, guild, session)
        return
    await update_all_vote_dms(guild, session, content=content)
    await update_host_vote_message(bot, guild, session)

__all__ = ['cancel_vote_timer', 'set_vote_timer', 'schedule_vote_timer_task', 'update_all_vote_dms', 'request_discussion', 'start_discussion_channel', 'forward_discussion_message', 'end_discussion', 'pause_session', 'session_voice_quorum_ready', 'check_realtime_quorum', 'resume_session']
