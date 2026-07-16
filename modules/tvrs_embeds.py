"""Discord presentation for TVRS entities without interaction workflows."""

from __future__ import annotations

from typing import Any

import discord

from modules.consensus_core import LiveConsensusSession, LiveResult
from modules.tvrs_config import TVRS_EMBED_COLOR
from modules.tvrs_formatting import (
    clip_text,
    format_bill_number,
    format_dt,
    materials_text,
    now_local,
    progress_bar,
    result_status_text,
    role_label,
    ru_ordinal,
    vote_label,
)


def build_paused_embed(session: LiveConsensusSession) -> discord.Embed:
    embed = discord.Embed(
        title=f"Консенсус приостановлен — {ru_ordinal(session.plenary_number)}",
        description=session.paused_reason or "Консенсус временно приостановлен.",
        color=0xD6B46A,
        timestamp=now_local(),
    )
    embed.add_field(name="Ведущий", value=f"<@{session.leader_id}>", inline=True)
    embed.add_field(name="Предыдущий этап", value=f"`{session.previous_stage or 'unknown'}`", inline=True)
    if session.current_bill:
        embed.add_field(
            name="Текущий законопроект",
            value=(
                f"`{format_bill_number(int(session.current_bill.get('bill_number') or 0))}` • "
                f"{session.current_bill.get('title', '')[:200]}"
            ),
            inline=False,
        )
    embed.add_field(
        name="Действия",
        value=(
            "Можно продолжить после восстановления кворума или завершить консенсус. "
            "При завершении текущий законопроект возвращается в очередь."
        ),
        inline=False,
    )
    return embed


def build_discussion_embed(session: LiveConsensusSession) -> discord.Embed:
    bill = session.current_bill or {}
    embed = discord.Embed(
        title=f"Дискуссия по законопроекту {format_bill_number(int(bill.get('bill_number', 0) or 0))}",
        description=str(bill.get("title") or ""),
        color=0x8FAADC,
        timestamp=now_local(),
    )
    embed.add_field(name="Тип дискуссии", value=session.discussion_type or "выбирается", inline=True)
    embed.add_field(
        name="Инициатор",
        value=f"<@{session.discussion_initiator_id}>" if session.discussion_initiator_id else "не указан",
        inline=True,
    )
    if session.discussion_channel_id:
        embed.add_field(name="Канал дискуссии", value=f"<#{session.discussion_channel_id}>", inline=False)
    embed.add_field(
        name="Порядок",
        value=(
            "На время дискуссии голосование скрыто. Участники отвечают в ЛС на сообщение бота, "
            "а бот переносит материалы в канал дискуссии."
        ),
        inline=False,
    )
    return embed


def bill_attempt_text(bill: dict[str, Any]) -> str:
    attempt = int(bill.get("attempt") or 1)
    return f"Попытка консенсуса: {attempt}/3" if attempt > 1 else "Первичное рассмотрение"


def build_bill_embed(guild: discord.Guild, bill: Any) -> discord.Embed:
    del guild  # Reserved for future guild-specific presentation rules.
    if isinstance(bill, dict):
        number = format_bill_number(int(bill["bill_number"]))
        title = str(bill.get("title") or "")
        summary = str(bill.get("summary") or "")
        materials = bill.get("materials")
        author = bill.get("author_display") or str(bill.get("author_id"))
        author_id = int(bill.get("author_id") or 0)
        attempt = bill_attempt_text(bill)
    else:
        number = format_bill_number(bill.bill_number)
        title = bill.title
        summary = bill.summary
        materials = bill.materials
        author = bill.author_display or str(bill.author_id)
        author_id = bill.author_id
        attempt = "Первичное рассмотрение"
    embed = discord.Embed(
        title=f"В процессе консенсуса {number}",
        description=(
            f"{summary}\n\nПредседатель передает директиву по проведению консенсуса Товариществу."
        ),
        color=TVRS_EMBED_COLOR,
        timestamp=now_local(),
    )
    embed.add_field(name="Законопроект", value=title[:1024], inline=False)
    embed.add_field(name="Материалы", value=materials_text(materials), inline=False)
    embed.add_field(name="Рассмотрение", value=attempt, inline=True)
    embed.set_footer(text=f"Автор: {author} ({author_id}) • {format_dt()}")
    return embed


def build_result_embed(result: LiveResult, session: LiveConsensusSession) -> discord.Embed:
    color = 0x7FD17F if result.status == "accepted" else (0xD6B46A if result.status == "vetoed" else 0xD67F7F)
    icon = "✅" if result.status == "accepted" else ("🟨" if result.status == "vetoed" else "❌")
    embed = discord.Embed(
        title=f"{icon} Итог голосования • №{format_bill_number(result.bill_number)}",
        description=f"**{clip_text(result.title, 240)}**\n\nРешение: **{result_status_text(result.status)}**",
        color=color,
        timestamp=now_local(),
    )
    embed.add_field(
        name="🏛️ Внутренний консенсус",
        value=(
            f"`{result.internal_percent}%` {progress_bar(result.internal_percent, 10)}\n"
            f"{'✅ активирован' if result.internal_active else '❌ не активирован'}"
        ),
        inline=True,
    )
    embed.add_field(
        name="🌐 Общий консенсус",
        value=(
            f"`{result.overall_percent}%` {progress_bar(result.overall_percent, 10)}\n"
            f"порог принятия `{session.rules.acceptance_percent}%`"
        ),
        inline=True,
    )
    if result.veto_by_id:
        value = f"<@{result.veto_by_id}>"
        if result.retry_bill_number:
            value += f"\nПовторное рассмотрение: №`{format_bill_number(result.retry_bill_number)}`"
        embed.add_field(name="🛑 Право вето", value=value, inline=False)
    lines = [
        f"{participant.mention} · `{role_label(participant)}` · "
        f"**{vote_label(result.votes.get(participant.user_id))}**"
        for participant in sorted(
            session.confirmed_participants(),
            key=lambda item: (item.kind != "chair", item.display_name.lower()),
        )
    ]
    embed.add_field(
        name="🧾 Зафиксированные голоса",
        value=clip_text("\n".join(lines), 1000, "Нет голосов."),
        inline=False,
    )
    embed.set_footer(text="TVRS • результат зафиксирован")
    return embed


def build_final_summary_embed(session: LiveConsensusSession) -> discord.Embed:
    accepted = sum(result.status == "accepted" for result in session.results)
    rejected = sum(result.status == "rejected" for result in session.results)
    vetoed = sum(result.status == "vetoed" for result in session.results)
    embed = discord.Embed(
        title=f"🏛️ Проведен {ru_ordinal(session.plenary_number)} пленарный консенсус Товарищества",
        description=(
            f"Рассмотрено проектов: **{len(session.results)}** • принято: **{accepted}** • "
            f"не принято: **{rejected}** • вето: **{vetoed}**"
        ),
        color=TVRS_EMBED_COLOR,
        timestamp=now_local(),
    )
    chairs = [participant for participant in session.confirmed_participants() if participant.kind == "chair"]
    senators = [participant for participant in session.confirmed_participants() if participant.kind == "senator"]
    embed.add_field(
        name="👥 Участники",
        value=f"Председатели: `{len(chairs)}` • Сенаторы: `{len(senators)}`",
        inline=True,
    )
    embed.add_field(name="👤 Ведущий", value=f"<@{session.leader_id}>", inline=True)
    participants = [
        f"{participant.mention} — `{role_label(participant)}`"
        for participant in sorted(
            session.confirmed_participants(),
            key=lambda item: (item.kind != "chair", item.display_name.lower()),
        )
    ]
    embed.add_field(name="Состав", value=clip_text("\n".join(participants), 1000, "Нет участников."), inline=False)
    if session.results:
        lines = []
        for result in session.results:
            mark = "✅" if result.status == "accepted" else ("🟨" if result.status == "vetoed" else "❌")
            lines.append(
                f"{mark} №`{format_bill_number(result.bill_number)}` • "
                f"**{clip_text(result.title, 80)}** — {result_status_text(result.status)} • "
                f"общий `{result.overall_percent}%`"
            )
        embed.add_field(name="Рассмотренные законопроекты", value=clip_text("\n".join(lines), 1000), inline=False)
    else:
        embed.add_field(name="Рассмотренные законопроекты", value="Законопроекты не рассматривались.", inline=False)
    embed.set_footer(text="TVRS • официальный итог пленарного консенсуса")
    return embed
