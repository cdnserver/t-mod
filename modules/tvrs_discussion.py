from __future__ import annotations

import asyncio
import re
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any

import discord
from discord.ext import commands

from modules.async_safety import run_blocking_cancellation_safe
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
from modules.delivery_runtime import wake_delivery_worker
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
from modules.tvrs_delivery import build_discussion_invite_deliveries
from modules.tvrs_formatting import (
    format_bill_number,
    format_timer,
    now_local,
    role_label,
)

from modules.tvrs_presentation import (
    consensus_bill_id,
    consensus_generation_matches,
    edit_session_host_message,
)
from modules.tvrs_consensus_views import TVRSAfterResultView

async def update_host_vote_message(*args, **kwargs):
    from modules.tvrs_control import update_host_vote_message as _implementation
    return await _implementation(*args, **kwargs)


async def update_public_consensus_card(*args, **kwargs):
    from modules.tvrs_control import update_public_consensus_card as _implementation

    return await _implementation(*args, **kwargs)


async def finalize_current_vote(*args, **kwargs):
    from modules.tvrs_decision import finalize_current_vote as _implementation
    return await _implementation(*args, **kwargs)

async def _cancel_runtime_vote_timer(session: LiveConsensusSession) -> None:
    """Detach the in-process task without changing persisted timer fields."""

    task = session.timer_task
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
    session.timer_task = None


async def cancel_vote_timer(session: LiveConsensusSession) -> None:
    """Stop both runtime and durable parts of an already-committed timer."""

    await _cancel_runtime_vote_timer(session)
    session.timer_deadline = None
    session.timer_seconds = None
    session.timer_added_seconds = 0
    session.timer_last_added_seconds = None
    session.timer_last_adjusted_at = None


async def set_vote_timer(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    seconds: int,
    *,
    expected_bill_id: int | None = None,
    expected_revision: int | None = None,
) -> None:
    clean_seconds = max(0, int(seconds))
    async with consensus_session_lock(session.guild_id):
        if (
            expected_revision is not None
            and int(session.revision) != int(expected_revision)
        ):
            raise ConsensusStateError(
                "Состояние голосования уже изменилось. Обновите пульт."
            )
        if not consensus_generation_matches(
            session,
            stage="voting",
            bill_id=expected_bill_id,
        ) or session.current_bill is None:
            raise ConsensusStateError("Таймер можно установить только во время голосования.")
        previous_task = session.timer_task
        now = datetime.now(timezone.utc)
        previous_deadline = session.timer_deadline
        is_extension = bool(previous_deadline and previous_deadline > now)
        if is_extension:
            deadline = previous_deadline + timedelta(seconds=clean_seconds)
            total_seconds = max(clean_seconds, int(session.timer_seconds or 0) + clean_seconds)
            runtime_seconds = max(0, int((deadline - now).total_seconds()))
        else:
            deadline = now + timedelta(seconds=clean_seconds)
            total_seconds = clean_seconds
            runtime_seconds = clean_seconds
        try:
            await run_blocking_cancellation_safe(
                _consensus.set_timer,
                session,
                seconds=total_seconds,
                deadline=deadline,
                actor=ConsensusActor(session.leader_id, session.leader_display),
            )
        finally:
            # The helper can re-raise cancellation after the blocking commit.
            # Complete the runtime half whenever that commit visibly won;
            # otherwise a new durable deadline would have no matching task.
            if session.timer_deadline == deadline and session.timer_seconds == total_seconds:
                if (
                    previous_task
                    and previous_task is not asyncio.current_task()
                    and not previous_task.done()
                ):
                    previous_task.cancel()
                session.timer_task = None
                schedule_vote_timer_task(
                    bot,
                    guild,
                    session,
                    runtime_seconds,
                    bill_id=consensus_bill_id(session),
                )

    await update_all_vote_dms(
        guild,
        session,
        content=(
            f"К таймеру добавлено {format_timer(clean_seconds)}. "
            f"Новое оставшееся время: {format_timer(runtime_seconds)}."
            if is_extension
            else f"Установлен таймер голосования: {format_timer(clean_seconds)}."
        ),
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
    # Discord I/O is projected through the durable outbox.  Keeping this
    # compatibility entrypoint lets lifecycle callers request a refresh
    # without blocking a state transition on every participant's DM.
    from modules.tvrs_control import enqueue_current_control_projection

    await enqueue_current_control_projection(guild, session, content=content)


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
            raise ConsensusStateError(
                "Голосование уже перешло к другому этапу. Обновите личный пульт."
            )
        if session.discussion_initiator_id:
            raise ConsensusStateError("Дискуссия по этому проекту уже инициирована.")
        try:
            await run_blocking_cancellation_safe(
                _consensus.request_discussion,
                session,
                initiator,
            )
        finally:
            # The helper deliberately delays cancellation until the database
            # write finishes, then re-raises it.  A committed transition still
            # needs its obsolete runtime timer detached in that path.
            if (
                session.stage == "discussion_type"
                and session.discussion_initiator_id == initiator.user_id
                and session.timer_deadline is None
            ):
                await _cancel_runtime_vote_timer(session)
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
    expected_revision: int | None = None,
) -> None:
    if (
        expected_revision is not None
        and int(session.revision) != int(expected_revision)
    ):
        raise ConsensusStateError(
            "Состояние консенсуса уже изменилось. Обновите пульт."
        )
    if session.current_bill is None or not consensus_generation_matches(
        session,
        stage="discussion_type",
        bill_id=expected_bill_id,
    ):
        raise ConsensusStateError(
            "Выбор типа дискуссии уже устарел. Обновите пульт консенсуса."
        )
    category = guild.get_channel(TVRS_DISCUSSION_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        try:
            fetched = await bot.fetch_channel(TVRS_DISCUSSION_CATEGORY_ID)  # type: ignore[attr-defined]
            category = fetched if isinstance(fetched, discord.CategoryChannel) else None
        except discord.DiscordException as exc:
            raise ConsensusStateError(
                "Не удалось открыть категорию дискуссий. Состояние консенсуса не изменено; попробуйте ещё раз."
            ) from exc
    if not isinstance(category, discord.CategoryChannel):
        raise ConsensusStateError(
            "Категория дискуссий не найдена или недоступна. Состояние консенсуса не изменено."
        )
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
    try:
        created = await guild.create_text_channel(
            channel_name,
            category=category,
            overwrites=overwrites,
            reason="TVRS live consensus discussion",
        )
    except discord.DiscordException as exc:
        raise ConsensusStateError(
            "Discord временно не дал создать канал дискуссии. Консенсус остался на выборе типа; повторите попытку."
        ) from exc
    if created is None:
        raise ConsensusStateError(
            "Канал дискуссии не был создан. Консенсус остался на выборе типа; повторите попытку."
        )
    allowed: set[int] = {p.user_id for p in session.confirmed_participants() if p.kind == "senator"}
    non_leader_chair = next((p for p in session.confirmed_participants() if p.kind == "chair" and p.user_id != session.leader_id), None)
    if non_leader_chair:
        allowed.add(non_leader_chair.user_id)
    activation_committed = False
    try:
        async with consensus_session_lock(session.guild_id):
            if (
                expected_revision is not None
                and int(session.revision) != int(expected_revision)
            ):
                should_activate = False
            elif session.current_bill is None or not consensus_generation_matches(
                session,
                stage="discussion_type",
                bill_id=expected_bill_id,
            ):
                should_activate = False
            else:
                should_activate = True
                deliveries = build_discussion_invite_deliveries(
                    session,
                    channel_id=int(created.id),
                    discussion_type=discussion_type,
                    allowed_user_ids=allowed,
                )
                try:
                    await run_blocking_cancellation_safe(
                        _consensus.begin_discussion,
                        session,
                        discussion_type,
                        channel_id=int(created.id),
                        allowed_user_ids=allowed,
                        deliveries=deliveries,
                    )
                finally:
                    activation_committed = (
                        session.stage == "discussion"
                        and session.discussion_channel_id == int(created.id)
                    )
    except BaseException:
        if not activation_committed:
            try:
                await created.delete(reason="TVRS discussion activation failed")
            except discord.DiscordException:
                pass
        else:
            # Invitations were committed atomically with the stage.  Wake the
            # outbox even when the caller itself was cancelled immediately
            # after the blocking commit.
            wake_delivery_worker()
            wake_operations_worker()
        raise
    if not should_activate:
        try:
            await created.delete(reason="TVRS discussion state changed before activation")
        except discord.DiscordException:
            pass
        raise ConsensusStateError(
            "Пока создавался канал, этап консенсуса изменился. Обновите пульт; "
            "лишний канал уже удалён."
        )
    wake_delivery_worker()
    wake_operations_worker()
    if created:
        try:
            await created.send(embed=build_discussion_embed(session), allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.DiscordException:
            pass
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
    actor: ConsensusActor | None = None,
    expected_stage: str | None = None,
    expected_bill_id: int | None = None,
    expected_revision: int | None = None,
) -> None:
    if expected_stage is not None and not consensus_generation_matches(
        session,
        stage=expected_stage,
        bill_id=expected_bill_id,
    ):
        return
    if session.stage not in {"discussion_type", "discussion"}:
        return
    closed_channel_id: int | None = None
    async with consensus_session_lock(session.guild_id):
        if (
            expected_revision is not None
            and int(session.revision) != int(expected_revision)
        ):
            raise ConsensusStateError(
                "Состояние дискуссии уже изменилось. Обновите пульт."
            )
        if expected_stage is not None and not consensus_generation_matches(
            session,
            stage=expected_stage,
            bill_id=expected_bill_id,
        ):
            return
        if session.stage not in {"discussion_type", "discussion"}:
            return
        closed_channel_id = int(session.discussion_channel_id or 0) or None
        await run_blocking_cancellation_safe(
            _consensus.end_discussion,
            session,
            actor=actor or ConsensusActor(session.leader_id, session.leader_display),
        )
    wake_operations_worker()
    if closed_channel_id:
        channel = guild.get_channel(closed_channel_id)
        if hasattr(channel, "send"):
            try:
                await channel.send(
                    "Дискуссия завершена ведущим. "
                    "Голосование возвращено в активный режим."
                )  # type: ignore[attr-defined]
            except discord.DiscordException:
                pass
    await check_realtime_quorum(bot, guild, session)
    content = (
        "Дискуссия завершена, но голосование приостановлено до восстановления кворума."
        if session.stage == "paused"
        else "Дискуссия завершена. Голосование снова открыто."
    )
    await update_all_vote_dms(guild, session, content=content)
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
    expected_revision: int | None = None,
) -> None:
    async with consensus_session_lock(session.guild_id):
        if (
            expected_revision is not None
            and int(session.revision) != int(expected_revision)
        ):
            raise ConsensusStateError(
                "Состояние заседания уже изменилось. Обновите пульт."
            )
        if expected_stage is not None and not consensus_generation_matches(
            session,
            stage=expected_stage,
            bill_id=expected_bill_id,
        ):
            return
        if session.stage == "paused":
            return
        if automatic:
            quorum_ready, current_reason = session_voice_quorum_ready(guild, session)
            if quorum_ready:
                return
            reason = current_reason
        try:
            await run_blocking_cancellation_safe(
                _consensus.pause,
                session,
                reason,
                automatic=automatic,
                actor=None
                if automatic
                else ConsensusActor(session.leader_id, session.leader_display),
            )
        finally:
            # See request_discussion(): runtime cancellation follows, never
            # precedes, the durable transition, including cancellation after
            # a successful blocking commit.
            if session.stage == "paused" and session.timer_deadline is None:
                await _cancel_runtime_vote_timer(session)
    wake_operations_worker()
    await update_all_vote_dms(guild, session, content=reason)
    await update_host_vote_message(bot, guild, session)


def session_voice_quorum_ready(guild: discord.Guild, session: LiveConsensusSession) -> tuple[bool, str]:
    get_channel = getattr(guild, "get_channel", None)
    channel = get_channel(TVRS_CONSENSUS_VOICE_CHANNEL_ID) if callable(get_channel) else None
    if not isinstance(channel, discord.VoiceChannel):
        return False, "Голосовой канал консенсуса не найден."
    voice_ids = {m.id for m in channel.members if not m.bot}
    if int(session.leader_id) not in voice_ids:
        return False, "Ведущий отсутствует в голосовом канале. Консенсус приостановлен."
    confirmed = [p for p in session.confirmed_participants() if p.user_id in voice_ids]
    rules = session.rules
    if rules.version >= 3:
        invited = len(session.participants)
        ok = (
            len(confirmed) >= rules.minimum_participants
            and invited > 0
            and len(confirmed) / invited * 100.0
            > rules.internal_quorum_strictly_above
        )
        if ok:
            return True, (
                f"Кворум сохранён: в войсе `{len(confirmed)}` из `{invited}` участников."
            )
        return False, (
            f"Кворум утрачен: в войсе `{len(confirmed)}` из `{invited}`, "
            f"требуется больше половины и не менее `{rules.minimum_participants}`. "
            "Консенсус приостановлен."
        )

    chairs = [p for p in confirmed if p.kind == "chair"]
    senators = [p for p in confirmed if p.kind == "senator"]
    ok = (
        len(chairs) >= rules.minimum_chairs
        and len(senators) >= rules.minimum_senators
        and (not rules.senators_must_be_odd or len(senators) % 2 == 1)
    )
    if ok:
        return True, f"Кворум сохранен: председатели `{len(chairs)}`, сенаторы `{len(senators)}`."
    return False, f"Кворум утрачен: председатели `{len(chairs)}`, сенаторы `{len(senators)}`. Консенсус приостановлен."


async def check_realtime_quorum(bot: commands.Bot | discord.Client, guild: discord.Guild, session: LiveConsensusSession) -> None:
    if session.finished or session.stage in {"registration", "finalizing", "after_result"}:
        return
    ok, reason = session_voice_quorum_ready(guild, session)
    if not ok and session.stage != "paused":
        await pause_session(bot, guild, session, reason, automatic=True)
    elif ok and session.stage == "paused" and session.pause_is_automatic:
        from modules.consensus_health import assess_consensus_health

        if not assess_consensus_health(session).critical:
            await resume_session(bot, guild, session)


async def resume_session(
    bot: commands.Bot | discord.Client,
    guild: discord.Guild,
    session: LiveConsensusSession,
    *,
    expected_stage: str | None = None,
    expected_bill_id: int | None = None,
    expected_revision: int | None = None,
) -> None:
    quorum_failed = False
    async with consensus_session_lock(session.guild_id):
        if (
            expected_revision is not None
            and int(session.revision) != int(expected_revision)
        ):
            raise ConsensusStateError(
                "Состояние заседания уже изменилось. Обновите пульт."
            )
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
            target = await run_blocking_cancellation_safe(
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
