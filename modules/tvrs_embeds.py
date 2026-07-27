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
        implementation_plan = bill.get("implementation_plan")
        leadership_actions = bill.get("leadership_actions")
    else:
        number = format_bill_number(bill.bill_number)
        title = bill.title
        summary = bill.summary
        materials = bill.materials
        author = bill.author_display or str(bill.author_id)
        author_id = bill.author_id
        attempt = "Первичное рассмотрение"
        implementation_plan = getattr(bill, "implementation_plan", None)
        leadership_actions = getattr(bill, "leadership_actions", None)
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
    if implementation_plan:
        embed.add_field(
            name="После принятия",
            value=clip_text(implementation_plan, 1000),
            inline=False,
        )
    if leadership_actions:
        embed.add_field(
            name="Действия руководства",
            value=clip_text(leadership_actions, 1000),
            inline=False,
        )
    embed.set_footer(text=f"Автор: {author} ({author_id}) • {format_dt()}")
    return embed


def build_result_embed(result: LiveResult, session: LiveConsensusSession) -> discord.Embed:
    color = 0x7FD17F if result.status == "accepted" else (0xD6B46A if result.status == "vetoed" else 0xD67F7F)
    icon = "✅" if result.status == "accepted" else ("🟨" if result.status == "vetoed" else "❌")
    required_percent = (
        float(result.required_percent)
        if float(result.required_percent or 0.0) > 0
        else float(session.rules.acceptance_percent)
    )
    embed = discord.Embed(
        title=(
            f"{icon} Итог устного решения • №{format_bill_number(result.bill_number)}"
            if result.resolution_method == "oral"
            else f"{icon} Итог голосования • №{format_bill_number(result.bill_number)}"
        ),
        description=f"**{clip_text(result.title, 240)}**\n\nРешение: **{result_status_text(result.status)}**",
        color=color,
        timestamp=now_local(),
    )
    if result.resolution_method == "oral":
        recorder = f"<@{result.resolved_by_id}>" if result.resolved_by_id else "не указан"
        embed.add_field(
            name="🗣️ Способ фиксации",
            value=f"Решение принято устно и внесено в систему председателем {recorder}.",
            inline=False,
        )
        if result.resolution_note:
            embed.add_field(
                name="📝 Основание",
                value=clip_text(result.resolution_note, 1000),
                inline=False,
            )
    else:
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
                f"против `{result.opposed_percent}%` • "
                f"порог принятия `{required_percent}%`"
            ),
            inline=True,
        )
        if result.block_votes:
            block_labels = {
                "first": "Первый сопредседатель",
                "second": "Второй сопредседатель",
                "third": "Третий сопредседатель",
                "consensus": "Консенсус Товарищества",
            }
            block_states = {
                "yes": "✅ за",
                "no": "❌ против",
                "abstain": "⚪ воздержался",
                "inactive": "➖ неактивен",
            }
            block_lines = [
                f"**{block_labels[key]} · 25%** — "
                f"{block_states.get(result.block_votes.get(key), '➖ неактивен')}"
                for key in ("first", "second", "third", "consensus")
            ]
            embed.add_field(
                name="🧩 Четыре блока",
                value="\n".join(block_lines),
                inline=False,
            )
    if result.veto_by_id:
        value = f"<@{result.veto_by_id}>"
        if result.retry_bill_number:
            value += f"\nПовторное рассмотрение: №`{format_bill_number(result.retry_bill_number)}`"
        embed.add_field(name="🛑 Право вето", value=value, inline=False)
    if result.resolution_method != "oral":
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
    embed.set_footer(
        text="TVRS • устное решение зафиксировано"
        if result.resolution_method == "oral"
        else "TVRS • результат зафиксирован"
    )
    return embed


def _result_source_url(result: LiveResult, guild_id: int) -> str | None:
    if not result.source_channel_id or not result.source_message_id:
        return None
    return (
        f"https://discord.com/channels/{int(guild_id)}/"
        f"{int(result.source_channel_id)}/{int(result.source_message_id)}"
    )


def _summary_result_line(result: LiveResult, guild_id: int) -> str:
    mark = "✅" if result.status == "accepted" else ("🟨" if result.status == "vetoed" else "❌")
    suffix = (
        "устное решение"
        if result.resolution_method == "oral"
        else f"общий `{result.overall_percent}%`"
    )
    title = discord.utils.escape_markdown(clip_text(result.title, 140))
    source_url = _result_source_url(result, guild_id)
    rendered_title = f"[{title}]({source_url})" if source_url else f"**{title}**"
    return (
        f"{mark} №`{format_bill_number(result.bill_number)}` • "
        f"{rendered_title} — {result_status_text(result.status)} • {suffix}"
    )


def _summary_result_fields(session: LiveConsensusSession) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for result in session.results:
        line = _summary_result_line(result, session.guild_id)
        extra = len(line) + (1 if current else 0)
        if current and current_size + extra > 1000:
            chunks.append("\n".join(current))
            current = []
            current_size = 0
        current.append(line)
        current_size += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks


def build_final_summary_embed(
    session: LiveConsensusSession,
    *,
    page_number: int = 1,
    page_count: int = 1,
    compact: bool = False,
    summary_counts: dict[str, int] | None = None,
) -> discord.Embed:
    counts = dict(summary_counts or {})
    total = int(counts.get("total", len(session.results)))
    accepted = int(
        counts.get(
            "accepted",
            sum(result.status == "accepted" for result in session.results),
        )
    )
    rejected = int(
        counts.get(
            "rejected",
            sum(result.status == "rejected" for result in session.results),
        )
    )
    vetoed = int(
        counts.get(
            "vetoed",
            sum(result.status == "vetoed" for result in session.results),
        )
    )
    embed = discord.Embed(
        title=(
            f"🏛️ Проведен {ru_ordinal(session.plenary_number)} пленарный консенсус Товарищества"
            if page_number <= 1
            else (
                f"🏛️ Итоги {ru_ordinal(session.plenary_number)} пленарного консенсуса "
                f"· продолжение"
            )
        ),
        description=(
            f"Рассмотрено проектов: **{total}** • принято: **{accepted}** • "
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
    if page_number <= 1:
        participants = [
            f"{participant.mention} — `{role_label(participant)}`"
            for participant in sorted(
                session.confirmed_participants(),
                key=lambda item: (item.kind != "chair", item.display_name.lower()),
            )
        ]
        embed.add_field(
            name="Состав",
            value=clip_text("\n".join(participants), 1000, "Нет участников."),
            inline=False,
        )
    if compact:
        embed.add_field(
            name="Официальный протокол",
            value="Полный перечень законопроектов опубликован в канале заседания.",
            inline=False,
        )
    elif session.results:
        for index, value in enumerate(_summary_result_fields(session), start=1):
            embed.add_field(
                name=(
                    "Рассмотренные законопроекты"
                    if index == 1
                    else "Рассмотренные законопроекты · продолжение"
                ),
                value=value,
                inline=False,
            )
    else:
        embed.add_field(name="Рассмотренные законопроекты", value="Законопроекты не рассматривались.", inline=False)
    page_text = f" • страница {page_number}/{page_count}" if page_count > 1 else ""
    embed.set_footer(text=f"TVRS • официальный итог пленарного консенсуса{page_text}")
    return embed
