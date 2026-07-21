"""Isolated Discord simulator for the current consensus lifecycle.

The simulator deliberately has no storage or consensus-runtime imports. It
exercises the production rules and stage machine entirely in memory, so a
training session cannot touch real bills, plenary numbers, votes or outbox
deliveries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import discord

from modules.consensus_core import (
    ConsensusStateError,
    LiveConsensusSession,
    LiveParticipant,
    LiveResult,
    calculate_consensus,
    clean_stage_name,
    transition_session,
)
from modules.consensus_v3 import CONSENSUS_ENGINE_VERSION, consensus_progress_text


SIMULATION_COLOR = 0x9B59B6
SIMULATION_FAKE_SPECS = (
    (-101, "Фейк-председатель Марта", "chair"),
    (-201, "Фейк-сенатор Алекс", "senator"),
    (-202, "Фейк-сенатор Ника", "senator"),
    (-203, "Фейк-сенатор Роман", "senator"),
)
SIMULATION_SCENARIOS = {
    "accepted": "Поддержка",
    "mixed": "Спорный расклад",
    "rejected": "Отклонение",
}


@dataclass(slots=True)
class ConsensusSimulation:
    guild_id: int
    leader_id: int
    leader_display: str
    bill_number: int = 900
    session: LiveConsensusSession = field(init=False)
    events: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        participants = {
            self.leader_id: LiveParticipant(
                user_id=self.leader_id,
                display_name=self.leader_display,
                mention=f"<@{self.leader_id}>",
                kind="chair",
                permanent=True,
                confirmed=True,
            )
        }
        for user_id, display_name, kind in SIMULATION_FAKE_SPECS:
            participants[user_id] = LiveParticipant(
                user_id=user_id,
                display_name=display_name,
                mention=display_name,
                kind=kind,  # type: ignore[arg-type]
            )
        self.session = LiveConsensusSession(
            session_key=f"simulation:{self.guild_id}:{self.leader_id}",
            guild_id=self.guild_id,
            channel_id=0,
            leader_id=self.leader_id,
            leader_display=self.leader_display,
            plenary_number=0,
            participants=participants,
            engine_version=CONSENSUS_ENGINE_VERSION,
        )
        self._record("Симуляция открыта; ведущий подтверждён автоматически")

    @property
    def finished(self) -> bool:
        return self.session.finished

    def _record(self, text: str) -> None:
        self.events.append(str(text))
        del self.events[:-6]

    def _require_stage(self, *stages: str) -> None:
        if self.session.stage not in stages:
            raise ConsensusStateError("Это действие недоступно на текущем этапе симуляции.")

    def _bill(self) -> dict[str, object]:
        return {
            "id": 900_000 + self.bill_number,
            "bill_number": self.bill_number,
            "title": f"Учебный законопроект №{self.bill_number}",
            "summary": (
                "Тестовый проект для проверки регистрации, голосования, дискуссии, "
                "паузы и фиксации результата."
            ),
            "materials": "Учебные материалы отсутствуют.",
        }

    def confirm_next(self) -> None:
        self._require_stage("registration")
        participant = next(
            (
                item
                for item in self.session.participants.values()
                if item.user_id != self.leader_id and not item.confirmed
            ),
            None,
        )
        if participant is None:
            raise ConsensusStateError("Все фейковые участники уже подтвердились.")
        participant.confirmed = True
        self._record(f"{participant.display_name} подтвердил участие")

    def confirm_all(self) -> None:
        self._require_stage("registration")
        for participant in self.session.participants.values():
            participant.confirmed = True
        self._record("Все фейковые участники подтвердили участие")

    def begin_voting(self) -> None:
        self._require_stage("registration")
        if not self.session.quorum_ready():
            raise ConsensusStateError("Сначала соберите учебный кворум.")
        self.session.current_bill = self._bill()
        self.session.votes.clear()
        transition_session(self.session, "voting")
        self._record(f"Начато голосование по проекту №{self.bill_number}")

    def cast_leader_vote(self, vote: str) -> None:
        self._require_stage("voting")
        if vote not in {"yes", "no"}:
            raise ConsensusStateError("Неизвестный вариант голоса.")
        self.session.votes[self.leader_id] = vote
        self._record(f"Ведущий проголосовал: {'За' if vote == 'yes' else 'Против'}")

    def cast_next_fake_vote(self) -> None:
        self._require_stage("voting")
        candidates = [
            participant
            for participant in self.session.confirmed_participants()
            if participant.user_id != self.leader_id
            and participant.user_id not in self.session.votes
        ]
        if not candidates:
            raise ConsensusStateError("Все подтверждённые фейковые участники уже проголосовали.")
        participant = candidates[0]
        vote = "no" if participant.kind == "chair" else "yes"
        self.session.votes[participant.user_id] = vote
        self._record(
            f"{participant.display_name}: {'За' if vote == 'yes' else 'Против'}"
        )

    def apply_fake_scenario(self, scenario: str) -> None:
        self._require_stage("voting")
        if scenario not in SIMULATION_SCENARIOS:
            raise ConsensusStateError("Неизвестный учебный сценарий.")
        senators = [
            participant
            for participant in self.session.confirmed_participants()
            if participant.kind == "senator"
        ]
        for participant in self.session.confirmed_participants():
            if participant.user_id == self.leader_id:
                continue
            if scenario == "accepted":
                vote = "yes"
            elif scenario == "rejected":
                vote = "no"
            elif participant.kind == "chair":
                vote = "no"
            else:
                vote = "no" if senators.index(participant) == 1 else "yes"
            self.session.votes[participant.user_id] = vote
        self._record(f"Применён сценарий «{SIMULATION_SCENARIOS[scenario]}»")

    def request_discussion(self) -> None:
        self._require_stage("voting")
        self.session.previous_stage = "voting"
        transition_session(self.session, "discussion_type")
        self._record("Запрошена учебная дискуссия")

    def choose_discussion(self, discussion_type: str) -> None:
        self._require_stage("discussion_type")
        self.session.discussion_type = str(discussion_type)
        transition_session(self.session, "discussion")
        self._record(f"Начата дискуссия: {discussion_type.lower()}")

    def end_discussion(self) -> None:
        self._require_stage("discussion")
        transition_session(self.session, "voting")
        self.session.discussion_type = None
        self._record("Дискуссия завершена; прежние голоса сохранены")

    def pause(self) -> None:
        self._require_stage("voting", "discussion_type", "discussion", "after_result")
        self.session.previous_stage = self.session.stage
        self.session.paused_reason = "Учебная пауза ведущего"
        transition_session(self.session, "paused")
        self._record("Симуляция поставлена на паузу")

    def resume(self) -> None:
        self._require_stage("paused")
        target = self.session.previous_stage or "voting"
        if target not in {"voting", "discussion_type", "discussion", "after_result"}:
            target = "voting"
        transition_session(self.session, target)
        self.session.paused_reason = None
        self._record("Симуляция продолжена без проверки голосового канала")

    def finalize(self) -> LiveResult:
        self._require_stage("voting")
        bill = dict(self.session.current_bill or {})
        if not bill:
            raise ConsensusStateError("В симуляции нет текущего проекта.")
        transition_session(self.session, "finalizing")
        calculation = calculate_consensus(self.session)
        result = LiveResult(
            bill_id=int(bill["id"]),
            bill_number=int(bill["bill_number"]),
            title=str(bill["title"]),
            status="accepted" if calculation["accepted"] else "rejected",
            internal_percent=float(calculation["internal_percent"]),
            overall_percent=float(calculation["overall_percent"]),
            internal_active=bool(calculation["internal_active"]),
            votes=dict(self.session.votes),
        )
        self.session.results.append(result)
        self.session.current_bill = None
        self.session.votes.clear()
        transition_session(self.session, "after_result")
        self._record(
            f"Результат: {'принят' if result.status == 'accepted' else 'отклонён'} "
            f"({result.overall_percent}%)"
        )
        return result

    def veto(self) -> LiveResult:
        self._require_stage("voting")
        bill = dict(self.session.current_bill or {})
        if not bill:
            raise ConsensusStateError("В симуляции нет текущего проекта.")
        transition_session(self.session, "finalizing")
        result = LiveResult(
            bill_id=int(bill["id"]),
            bill_number=int(bill["bill_number"]),
            title=str(bill["title"]),
            status="vetoed",
            internal_percent=0.0,
            overall_percent=0.0,
            internal_active=False,
            resolution_method="veto",
            votes=dict(self.session.votes),
            veto_by_id=self.leader_id,
        )
        self.session.results.append(result)
        self.session.current_bill = None
        self.session.votes.clear()
        transition_session(self.session, "after_result")
        self._record("Учебное вето применено")
        return result

    def next_bill(self) -> None:
        self._require_stage("after_result")
        self.bill_number += 1
        self.session.current_bill = self._bill()
        self.session.votes.clear()
        self.session.pending_action = None
        transition_session(self.session, "voting")
        self._record(f"Открыт следующий учебный проект №{self.bill_number}")

    def finish(self) -> None:
        if self.session.finished:
            return
        if self.session.stage == "finalizing":
            raise ConsensusStateError("Сначала завершите фиксацию учебного результата.")
        self.session.current_bill = None
        self.session.votes.clear()
        transition_session(self.session, "finished")
        self._record("Учебное заседание завершено")


def _participant_lines(simulation: ConsensusSimulation) -> str:
    lines: list[str] = []
    for participant in simulation.session.participants.values():
        confirmed = "✅" if participant.confirmed else "⏳"
        role = "председатель" if participant.kind == "chair" else "сенатор"
        vote = simulation.session.votes.get(participant.user_id)
        vote_text = "За" if vote == "yes" else "Против" if vote == "no" else "—"
        display = (
            f"<@{participant.user_id}>"
            if participant.user_id == simulation.leader_id
            else participant.display_name
        )
        lines.append(f"{confirmed} **{display}** · `{role}` · голос: **{vote_text}**")
    return "\n".join(lines)


def consensus_simulation_embed(simulation: ConsensusSimulation) -> discord.Embed:
    session = simulation.session
    embed = discord.Embed(
        title=f"🧪 Симулятор Consensus V{simulation.session.engine_version}",
        description=(
            "Полностью изолированный учебный контур. Фейковые участники, голоса и проекты "
            "существуют только в этой карточке и никогда не попадают в рабочую базу."
        ),
        color=SIMULATION_COLOR,
    )
    embed.add_field(
        name="Этап",
        value=f"**{clean_stage_name(session.stage)}**",
        inline=True,
    )
    embed.add_field(
        name="Маршрут",
        value=consensus_progress_text(session.stage),
        inline=False,
    )
    embed.add_field(
        name="Учебный кворум",
        value="✅ набран" if session.quorum_ready() else "⏳ ожидается",
        inline=True,
    )
    embed.add_field(
        name="Проверка войса",
        value="🚫 отключена",
        inline=True,
    )
    embed.add_field(name="Участники", value=_participant_lines(simulation)[:1024], inline=False)

    if session.current_bill is not None:
        bill = session.current_bill
        embed.add_field(
            name=f"Проект №{int(bill.get('bill_number') or 0)}",
            value=f"**{str(bill.get('title') or 'Учебный проект')}**\n{str(bill.get('summary') or '')}"[:1024],
            inline=False,
        )
    if session.stage in {"voting", "discussion_type", "discussion", "paused"} and session.current_bill:
        calculation = calculate_consensus(session)
        embed.add_field(
            name="Текущий расчёт",
            value=(
                f"Сенат: **{calculation['internal_percent']}%** · "
                f"общий результат: **{calculation['overall_percent']}%** · "
                f"{'✅ проходит' if calculation['accepted'] else '❌ пока не проходит'}"
            ),
            inline=False,
        )
    if session.results:
        result = session.results[-1]
        status = {
            "accepted": "✅ принят",
            "rejected": "❌ отклонён",
            "vetoed": "🛑 вето",
        }.get(result.status, result.status)
        embed.add_field(
            name=f"Последний результат · №{result.bill_number}",
            value=f"**{status}** · общий результат **{result.overall_percent}%**",
            inline=False,
        )
    if simulation.events:
        embed.add_field(
            name="Ход симуляции",
            value="\n".join(f"• {event}" for event in simulation.events)[-1024:],
            inline=False,
        )
    embed.set_footer(
        text=(
            f"T-Mod • Consensus V{session.engine_version} • учебный контур • "
            "сбрасывается при перезапуске"
        )
    )
    return embed


async def _apply_simulation_action(
    interaction: discord.Interaction,
    simulation: ConsensusSimulation,
    action: Callable[[], object],
) -> None:
    try:
        action()
    except ConsensusStateError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    await interaction.response.edit_message(
        embed=consensus_simulation_embed(simulation),
        view=None if simulation.finished else ConsensusSimulationView(simulation),
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )


class SimulationScenarioSelect(discord.ui.Select):
    def __init__(self, simulation: ConsensusSimulation) -> None:
        self.simulation = simulation
        options = [
            discord.SelectOption(
                label=label,
                value=value,
                description=(
                    "Все фейки голосуют за"
                    if value == "accepted"
                    else "Председатель против, сенаторы разделены"
                    if value == "mixed"
                    else "Все фейки голосуют против"
                ),
            )
            for value, label in SIMULATION_SCENARIOS.items()
        ]
        super().__init__(placeholder="Сценарий голосов фейков", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _apply_simulation_action(
            interaction,
            self.simulation,
            lambda: self.simulation.apply_fake_scenario(self.values[0]),
        )


class ConsensusSimulationView(discord.ui.View):
    def __init__(self, simulation: ConsensusSimulation) -> None:
        super().__init__(timeout=3600)
        self.simulation = simulation
        stage = simulation.session.stage
        if stage == "registration":
            self._add("Следующее подтверждение", "✅", discord.ButtonStyle.secondary, simulation.confirm_next)
            self._add("Подтвердить всех", "👥", discord.ButtonStyle.success, simulation.confirm_all)
            self._add(
                "Начать голосование",
                "🗳️",
                discord.ButtonStyle.primary,
                simulation.begin_voting,
                disabled=not simulation.session.quorum_ready(),
            )
            self._add("Завершить", "⏹️", discord.ButtonStyle.danger, simulation.finish)
        elif stage == "voting":
            self._add("За", "✅", discord.ButtonStyle.success, lambda: simulation.cast_leader_vote("yes"))
            self._add("Против", "❌", discord.ButtonStyle.danger, lambda: simulation.cast_leader_vote("no"))
            self._add("Ход фейка", "🤖", discord.ButtonStyle.secondary, simulation.cast_next_fake_vote)
            self.add_item(SimulationScenarioSelect(simulation))
            self._add("Дискуссия", "💬", discord.ButtonStyle.secondary, simulation.request_discussion, row=2)
            self._add("Пауза", "⏸️", discord.ButtonStyle.secondary, simulation.pause, row=2)
            self._add("Таймер истёк", "⏱️", discord.ButtonStyle.secondary, simulation.finalize, row=2)
            self._add("Завершить сейчас", "📌", discord.ButtonStyle.primary, simulation.finalize, row=2)
            self._add("Учебное вето", "🛑", discord.ButtonStyle.danger, simulation.veto, row=2)
        elif stage == "discussion_type":
            for label in ("Правовая", "Фактическая", "Процедурная", "Иная"):
                self._add(
                    label,
                    "💬",
                    discord.ButtonStyle.secondary,
                    lambda selected=label: simulation.choose_discussion(selected),
                )
            self._add("Пауза", "⏸️", discord.ButtonStyle.secondary, simulation.pause, row=1)
            self._add("Завершить", "⏹️", discord.ButtonStyle.danger, simulation.finish, row=1)
        elif stage == "discussion":
            self._add("Завершить дискуссию", "✅", discord.ButtonStyle.success, simulation.end_discussion)
            self._add("Пауза", "⏸️", discord.ButtonStyle.secondary, simulation.pause)
            self._add("Завершить", "⏹️", discord.ButtonStyle.danger, simulation.finish)
        elif stage == "paused":
            self._add("Продолжить", "▶️", discord.ButtonStyle.success, simulation.resume)
            self._add("Завершить", "⏹️", discord.ButtonStyle.danger, simulation.finish)
        elif stage == "after_result":
            self._add("Следующий проект", "➡️", discord.ButtonStyle.success, simulation.next_bill)
            self._add("Завершить заседание", "🏁", discord.ButtonStyle.secondary, simulation.finish)

    def _add(
        self,
        label: str,
        emoji: str,
        style: discord.ButtonStyle,
        action: Callable[[], object],
        *,
        row: int = 0,
        disabled: bool = False,
    ) -> None:
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=style,
            row=row,
            disabled=disabled,
        )

        async def callback(interaction: discord.Interaction) -> None:
            await _apply_simulation_action(interaction, self.simulation, action)

        button.callback = callback
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.simulation.leader_id:
            await interaction.response.send_message(
                "Этой симуляцией управляет другой ведущий.",
                ephemeral=True,
            )
            return False
        return True


async def start_consensus_simulation(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Симулятор работает только на сервере.",
            ephemeral=True,
        )
        return
    permissions = getattr(interaction.user, "guild_permissions", None)
    if not permissions or not permissions.administrator:
        await interaction.response.send_message(
            "Симулятор консенсуса доступен только администратору.",
            ephemeral=True,
        )
        return
    simulation = ConsensusSimulation(
        guild_id=interaction.guild.id,
        leader_id=interaction.user.id,
        leader_display=getattr(interaction.user, "display_name", str(interaction.user)),
    )
    await interaction.response.send_message(
        embed=consensus_simulation_embed(simulation),
        view=ConsensusSimulationView(simulation),
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )


__all__ = [
    "ConsensusSimulation",
    "ConsensusSimulationView",
    "SimulationScenarioSelect",
    "consensus_simulation_embed",
    "start_consensus_simulation",
]
