"""Isolated Discord simulator driven by the production consensus coordinator.

The simulator uses the same state mutations, guards and result calculation as
the live workflow.  Its repository and registry are deliberately in-memory, so
training can never alter real bills, plenary numbers, votes or DM deliveries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from uuid import uuid4

import discord

from modules.consensus_core import (
    ConsensusStateError,
    LiveConsensusSession,
    LiveParticipant,
    LiveResult,
    calculate_consensus,
    clean_stage_name,
    session_to_snapshot,
)
from modules.consensus_service import ConsensusActor, ConsensusCoordinator
from modules.consensus_v3 import CONSENSUS_ENGINE_VERSION, consensus_progress_text


SIMULATION_COLOR = 0x9B59B6
SIMULATION_FAKE_SPECS = (
    (-101, "Фейк-сопредседатель Марта", "chair", "second"),
    (-102, "Фейк-сопредседатель Лев", "chair", "third"),
    (-201, "Фейк-сенатор Алекс", "senator", None),
    (-202, "Фейк-сенатор Ника", "senator", None),
    (-203, "Фейк-сенатор Роман", "senator", None),
)
SIMULATION_SCENARIOS = {
    "accepted": "Поддержка",
    "mixed": "Спорный расклад",
    "rejected": "Отклонение",
}


class InMemoryConsensusRepository:
    """Small production-protocol adapter with no database or outbox access."""

    def __init__(self) -> None:
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []

    def _persist(
        self,
        session: LiveConsensusSession,
        event_type: str,
        *,
        expected_revision: int | None = None,
        actor: ConsensusActor | None = None,
        stage_from: str | None = None,
        details: dict[str, Any] | None = None,
        deliveries: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        current = self.snapshots.get(session.session_key)
        current_revision = int((current or {}).get("revision") or 0)
        if expected_revision is not None and current_revision != int(expected_revision):
            raise ConsensusStateError(
                "Симуляция изменилась в другой операции. Обновите учебную панель."
            )
        revision = current_revision + 1
        snapshot = session_to_snapshot(session)
        snapshot["revision"] = revision
        self.snapshots[session.session_key] = snapshot
        self.events.append(
            {
                "event_type": str(event_type),
                "revision": revision,
                "stage_from": stage_from,
                "stage_to": str(session.stage),
                "actor_id": actor.user_id if actor else None,
                "details": dict(details or {}),
                "delivery_count": len(tuple(deliveries)),
            }
        )
        return snapshot

    def save(
        self,
        session: LiveConsensusSession,
        event_type: str,
        *,
        actor: ConsensusActor | None = None,
        stage_from: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._persist(
            session,
            event_type,
            actor=actor,
            stage_from=stage_from,
            details=details,
        )

    def active_snapshots(self, guild_id: int | None = None) -> list[dict[str, Any]]:
        return [
            dict(snapshot)
            for snapshot in self.snapshots.values()
            if guild_id is None or int(snapshot.get("guild_id") or 0) == int(guild_id)
        ]

    def save_with_deliveries(
        self,
        session: LiveConsensusSession,
        event_type: str,
        *,
        actor: ConsensusActor | None,
        stage_from: str | None,
        details: dict[str, Any] | None,
        deliveries: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._persist(
            session,
            event_type,
            actor=actor,
            stage_from=stage_from,
            details=details,
            deliveries=deliveries,
        )

    def commit_begin_bill(
        self,
        session: LiveConsensusSession,
        *,
        expected_revision: int,
        bill_id: int,
        event_type: str,
        actor: ConsensusActor,
        details: dict[str, Any],
        deliveries: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        snapshot = self._persist(
            session,
            event_type,
            expected_revision=expected_revision,
            actor=actor,
            details={**details, "bill_id": int(bill_id)},
            deliveries=deliveries,
        )
        return {"session": snapshot}

    def commit_finalization(
        self,
        session: LiveConsensusSession,
        result: LiveResult,
        *,
        expected_revision: int,
        bill_status: str,
        result_summary: str,
        event_type: str,
        actor: ConsensusActor | None,
        details: dict[str, Any],
        deliveries: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        snapshot = self._persist(
            session,
            event_type,
            expected_revision=expected_revision,
            actor=actor,
            details={
                **details,
                "bill_id": int(result.bill_id),
                "bill_status": str(bill_status),
                "result_summary": str(result_summary),
            },
            deliveries=deliveries,
        )
        return {"session": snapshot}

    def commit_finish(
        self,
        session: LiveConsensusSession,
        *,
        expected_revision: int,
        current_bill_id: int | None,
        advance_plenary: bool,
        event_type: str,
        actor: ConsensusActor,
        details: dict[str, Any],
        deliveries: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        snapshot = self._persist(
            session,
            event_type,
            expected_revision=expected_revision,
            actor=actor,
            details={
                **details,
                "current_bill_id": current_bill_id,
                "advance_plenary": bool(advance_plenary),
            },
            deliveries=deliveries,
        )
        return {"session": snapshot}


@dataclass(slots=True)
class ConsensusSimulation:
    guild_id: int
    leader_id: int
    leader_display: str
    bill_number: int = 900
    session: LiveConsensusSession = field(init=False)
    events: list[str] = field(default_factory=list)
    repository: InMemoryConsensusRepository = field(
        default_factory=InMemoryConsensusRepository,
        init=False,
    )
    coordinator: ConsensusCoordinator = field(init=False)
    control_message: discord.Message | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        participants = {
            self.leader_id: LiveParticipant(
                user_id=self.leader_id,
                display_name=self.leader_display,
                mention=f"<@{self.leader_id}>",
                kind="chair",
                permanent=True,
                confirmed=True,
                voting_block="first",
            )
        }
        for user_id, display_name, kind, voting_block in SIMULATION_FAKE_SPECS:
            participants[user_id] = LiveParticipant(
                user_id=user_id,
                display_name=display_name,
                mention=display_name,
                kind=kind,  # type: ignore[arg-type]
                voting_block=voting_block,  # type: ignore[arg-type]
            )
        self.coordinator = ConsensusCoordinator(self.repository)
        self.session = LiveConsensusSession(
            session_key=(
                f"simulation:{self.guild_id}:{self.leader_id}:"
                f"{uuid4().hex[:10]}"
            ),
            guild_id=self.guild_id,
            channel_id=0,
            leader_id=self.leader_id,
            leader_display=self.leader_display,
            plenary_number=0,
            participants=participants,
            engine_version=CONSENSUS_ENGINE_VERSION,
        )
        self.coordinator.save(
            self.session,
            "simulation_opened",
            actor=self.actor,
        )
        self._record("Симуляция открыта; ведущий подтверждён автоматически")

    @property
    def finished(self) -> bool:
        return self.session.finished

    @property
    def actor(self) -> ConsensusActor:
        return ConsensusActor(self.leader_id, self.leader_display)

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
            "decision_category": "ordinary",
        }

    def queue_bills(self, limit: int = 3) -> list[dict[str, object]]:
        if self.finished:
            return []
        start = self.bill_number + (
            0 if self.session.stage == "registration" else 1
        )
        return [
            {
                "id": 900_000 + number,
                "bill_number": number,
                "title": f"Учебный законопроект №{number}",
                "status": "queued",
                "author_id": self.leader_id,
                "author_display": "Симулятор T-Mod",
                "decision_category": "ordinary",
                "created_at": None,
                "source_url": None,
            }
            for number in range(start, start + max(0, int(limit)))
        ]

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
        self.coordinator.confirm_participant(
            self.session,
            participant.user_id,
            actor=ConsensusActor(participant.user_id, participant.display_name),
        )
        self._record(f"{participant.display_name} подтвердил участие")

    def confirm_all(self) -> None:
        self._require_stage("registration")
        for participant in self.session.participants.values():
            self.coordinator.confirm_participant(
                self.session,
                participant.user_id,
                actor=ConsensusActor(participant.user_id, participant.display_name),
            )
        self._record("Все фейковые участники подтвердили участие")

    def begin_voting(self) -> None:
        self.coordinator.present_bill_atomically(
            self.session,
            self._bill(),
            actor=self.actor,
            deliveries=(),
        )
        self._record(f"Представлен проект №{self.bill_number}; воут закрыт")

    def open_voting(self) -> None:
        self.coordinator.open_voting(
            self.session,
            actor=self.actor,
            deliveries=(),
        )
        self._record(f"Открыт воут по проекту №{self.bill_number}")

    def cast_leader_vote(self, vote: str) -> None:
        should_finalize = self.coordinator.cast_vote(
            self.session,
            self.leader_id,
            vote,
            actor=self.actor,
        )
        vote_label = {
            "yes": "За",
            "no": "Против",
            "abstain": "Воздержался",
        }.get(vote, vote)
        self._record(f"Ведущий проголосовал: {vote_label}")
        if should_finalize:
            self.finalize(forced=False)

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
        should_finalize = self.coordinator.cast_vote(
            self.session,
            participant.user_id,
            vote,
            actor=ConsensusActor(participant.user_id, participant.display_name),
        )
        self._record(
            f"{participant.display_name}: {'За' if vote == 'yes' else 'Против'}"
        )
        if should_finalize:
            self.finalize(forced=False)

    def apply_fake_scenario(self, scenario: str) -> None:
        self._require_stage("voting")
        if scenario not in SIMULATION_SCENARIOS:
            raise ConsensusStateError("Неизвестный учебный сценарий.")
        senators = [
            participant
            for participant in self.session.confirmed_participants()
            if participant.kind == "senator"
        ]
        should_finalize = False
        for participant in self.session.confirmed_participants():
            if participant.user_id == self.leader_id:
                continue
            if scenario == "accepted":
                vote = "yes"
            elif scenario == "rejected":
                vote = "no"
            elif participant.voting_block == "second":
                vote = "no"
            elif participant.voting_block == "third":
                vote = "yes"
            else:
                vote = "no" if senators.index(participant) == 1 else "yes"
            should_finalize = self.coordinator.cast_vote(
                self.session,
                participant.user_id,
                vote,
                actor=ConsensusActor(participant.user_id, participant.display_name),
            )
        self._record(f"Применён сценарий «{SIMULATION_SCENARIOS[scenario]}»")
        if should_finalize:
            self.finalize(forced=False)

    def request_discussion(self) -> None:
        initiator = self.session.participants[self.leader_id]
        self.coordinator.request_discussion(self.session, initiator)
        self._record("Запрошена учебная дискуссия")

    def choose_discussion(self, discussion_type: str) -> None:
        self.coordinator.begin_discussion(
            self.session,
            discussion_type,
            channel_id=None,
            allowed_user_ids=(
                participant.user_id
                for participant in self.session.confirmed_participants()
            ),
        )
        self._record(f"Начата дискуссия: {discussion_type.lower()}")

    def end_discussion(self) -> None:
        self.coordinator.end_discussion(self.session, actor=self.actor)
        self._record("Дискуссия завершена; прежние голоса сохранены")

    def pause(self) -> None:
        self.coordinator.pause(
            self.session,
            "Учебная пауза ведущего",
            automatic=False,
            actor=self.actor,
        )
        self._record("Симуляция поставлена на паузу")

    def resume(self) -> None:
        self.coordinator.resume(self.session, actor=self.actor)
        self._record("Симуляция продолжена без проверки голосового канала")

    def set_timer(self, seconds: int = 300) -> None:
        clean_seconds = max(0, int(seconds))
        self.coordinator.set_timer(
            self.session,
            seconds=clean_seconds,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=clean_seconds),
            actor=self.actor,
        )
        self._record(f"Учебный таймер установлен на {clean_seconds // 60} мин.")

    def finalize(self, *, forced: bool = True) -> LiveResult:
        bill = dict(self.session.current_bill or {})
        if not bill:
            raise ConsensusStateError("В симуляции нет текущего проекта.")
        self.coordinator.claim_finalization(
            self.session,
            kind="vote",
            actor=self.actor if forced else None,
            forced=forced,
        )
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
            decision_category=str(calculation["decision_category"]),
            required_percent=float(calculation["required_percent"]),
            opposed_percent=float(calculation["opposed_percent"]),
            block_votes=dict(calculation["block_votes"]),
        )
        self.coordinator.complete_result_atomically(
            self.session,
            result,
            bill_status=result.status,
            result_summary=(
                f"{result.status} • общий консенсус {result.overall_percent}%"
            ),
            event_type="vote_finalized_manually" if forced else "vote_finalized",
            actor=self.actor if forced else None,
            deliveries=(),
        )
        self._record(
            f"Результат: {'принят' if result.status == 'accepted' else 'отклонён'} "
            f"({result.overall_percent}%)"
        )
        return result

    def timer_expired(self) -> LiveResult:
        return self.finalize(forced=False)

    def veto(self) -> LiveResult:
        bill = dict(self.session.current_bill or {})
        if not bill:
            raise ConsensusStateError("В симуляции нет текущего проекта.")
        self.coordinator.claim_finalization(
            self.session,
            kind="veto",
            actor=self.actor,
            veto_authorized=True,
        )
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
            resolved_by_id=self.leader_id,
            resolved_by_display=self.leader_display,
        )
        self.coordinator.complete_result_atomically(
            self.session,
            result,
            bill_status="vetoed",
            result_summary="Применено учебное право вето",
            event_type="veto_applied",
            actor=self.actor,
            deliveries=(),
        )
        self._record("Учебное вето применено")
        return result

    def oral_result(self, status: str) -> LiveResult:
        clean_status = str(status).strip().lower()
        if clean_status not in {"accepted", "rejected"}:
            raise ConsensusStateError("Неизвестный устный итог.")
        bill = dict(self.session.current_bill or {})
        if not bill:
            raise ConsensusStateError("В симуляции нет текущего проекта.")
        note = "Учебное устное решение ведущего"
        self.coordinator.claim_finalization(
            self.session,
            kind="oral",
            actor=self.actor,
            oral_authorized=True,
            action_details={"oral_status": clean_status, "oral_note": note},
        )
        result = LiveResult(
            bill_id=int(bill["id"]),
            bill_number=int(bill["bill_number"]),
            title=str(bill["title"]),
            status=clean_status,
            internal_percent=0.0,
            overall_percent=0.0,
            internal_active=False,
            votes=dict(self.session.votes),
            resolution_method="oral",
            resolution_note=note,
            resolved_by_id=self.leader_id,
            resolved_by_display=self.leader_display,
        )
        self.coordinator.complete_result_atomically(
            self.session,
            result,
            bill_status=clean_status,
            result_summary=f"Учебное устное решение: {clean_status}",
            event_type="oral_result_recorded",
            actor=self.actor,
            deliveries=(),
        )
        self._record(
            "Устно зафиксировано: "
            f"{'принято' if clean_status == 'accepted' else 'отклонено'}"
        )
        return result

    def next_bill(self) -> None:
        self.bill_number += 1
        self.coordinator.present_bill_atomically(
            self.session,
            self._bill(),
            actor=self.actor,
            deliveries=(),
        )
        self._record(f"Открыт следующий учебный проект №{self.bill_number}")

    def finish(self) -> None:
        if self.session.finished:
            return
        self.coordinator.finish_atomically(
            self.session,
            actor=self.actor,
            deliveries=(),
        )
        self._record("Учебное заседание завершено")


_simulations: dict[int, ConsensusSimulation] = {}


def register_consensus_simulation(
    simulation: ConsensusSimulation,
) -> ConsensusSimulation | None:
    previous = _simulations.get(int(simulation.guild_id))
    _simulations[int(simulation.guild_id)] = simulation
    return previous


def get_consensus_simulation(guild_id: int) -> ConsensusSimulation | None:
    return _simulations.get(int(guild_id))


def clear_consensus_simulation(
    guild_id: int,
    *,
    session_key: str | None = None,
) -> ConsensusSimulation | None:
    simulation = _simulations.get(int(guild_id))
    if simulation is None:
        return None
    if session_key is not None and simulation.session.session_key != str(session_key):
        return None
    return _simulations.pop(int(guild_id), None)


def _participant_lines(simulation: ConsensusSimulation) -> str:
    lines: list[str] = []
    for participant in simulation.session.participants.values():
        confirmed = "✅" if participant.confirmed else "⏳"
        role = "председатель" if participant.kind == "chair" else "сенатор"
        vote = simulation.session.votes.get(participant.user_id)
        vote_text = {
            "yes": "За",
            "no": "Против",
            "abstain": "Воздержался",
        }.get(vote, "—")
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
            "Тот же координатор, переходы и расчёт, что в рабочем консенсусе, "
            "но с отдельной памятью и без реальных ЛС, базы и проверки войса. "
            "Ход симуляции доступен в веб-панели."
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
    embed.add_field(
        name="Веб-панель",
        value="🟣 режим **«Симуляция»**",
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
    if session.stage in {
        "presentation",
        "voting",
        "discussion_type",
        "discussion",
        "paused",
    } and session.current_bill:
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
    current = get_consensus_simulation(simulation.guild_id)
    if current is not None and current is not simulation:
        await interaction.response.send_message(
            "Эта учебная панель заменена новой симуляцией.",
            ephemeral=True,
        )
        return
    try:
        action()
    except ConsensusStateError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    interaction_message = getattr(interaction, "message", None)
    if interaction_message is not None:
        simulation.control_message = interaction_message
    await interaction.response.edit_message(
        embed=consensus_simulation_embed(simulation),
        view=None if simulation.finished else ConsensusSimulationView(simulation),
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )


async def refresh_consensus_simulation_projection(
    simulation: ConsensusSimulation,
) -> None:
    """Converge the original Discord simulator card after a web action."""

    message = simulation.control_message
    if message is None:
        return
    try:
        await message.edit(
            embed=consensus_simulation_embed(simulation),
            view=None if simulation.finished else ConsensusSimulationView(simulation),
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=False,
                everyone=False,
            ),
        )
    except discord.DiscordException:
        return


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
        elif stage == "presentation":
            self._add(
                "Поставить на воут",
                "🗳️",
                discord.ButtonStyle.success,
                simulation.open_voting,
            )
            self._add("Пауза", "⏸️", discord.ButtonStyle.secondary, simulation.pause)
            self._add("Завершить", "⏹️", discord.ButtonStyle.danger, simulation.finish)
        elif stage == "voting":
            self._add("За", "✅", discord.ButtonStyle.success, lambda: simulation.cast_leader_vote("yes"))
            self._add("Против", "❌", discord.ButtonStyle.danger, lambda: simulation.cast_leader_vote("no"))
            self._add(
                "Воздержаться",
                "➖",
                discord.ButtonStyle.secondary,
                lambda: simulation.cast_leader_vote("abstain"),
            )
            self._add("Ход фейка", "🤖", discord.ButtonStyle.secondary, simulation.cast_next_fake_vote)
            self.add_item(SimulationScenarioSelect(simulation))
            self._add("Дискуссия", "💬", discord.ButtonStyle.secondary, simulation.request_discussion, row=2)
            self._add("Пауза", "⏸️", discord.ButtonStyle.secondary, simulation.pause, row=2)
            self._add("Таймер 5 мин", "⏱️", discord.ButtonStyle.secondary, simulation.set_timer, row=2)
            self._add("Таймер истёк", "⌛", discord.ButtonStyle.secondary, simulation.timer_expired, row=2)
            self._add("Завершить сейчас", "📌", discord.ButtonStyle.primary, simulation.finalize, row=2)
            self._add("Учебное вето", "🛑", discord.ButtonStyle.danger, simulation.veto, row=3)
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
            self._add(
                "Устно: принято",
                "📜",
                discord.ButtonStyle.success,
                lambda: simulation.oral_result("accepted"),
                row=2,
            )
            self._add(
                "Устно: отклонено",
                "📜",
                discord.ButtonStyle.danger,
                lambda: simulation.oral_result("rejected"),
                row=2,
            )
        elif stage == "discussion":
            self._add("Завершить дискуссию", "✅", discord.ButtonStyle.success, simulation.end_discussion)
            self._add("Пауза", "⏸️", discord.ButtonStyle.secondary, simulation.pause)
            self._add("Завершить", "⏹️", discord.ButtonStyle.danger, simulation.finish)
            self._add(
                "Устно: принято",
                "📜",
                discord.ButtonStyle.success,
                lambda: simulation.oral_result("accepted"),
                row=1,
            )
            self._add(
                "Устно: отклонено",
                "📜",
                discord.ButtonStyle.danger,
                lambda: simulation.oral_result("rejected"),
                row=1,
            )
        elif stage == "paused":
            self._add("Продолжить", "▶️", discord.ButtonStyle.success, simulation.resume)
            self._add("Завершить", "⏹️", discord.ButtonStyle.danger, simulation.finish)
            if simulation.session.current_bill is not None:
                self._add(
                    "Устно: принято",
                    "📜",
                    discord.ButtonStyle.success,
                    lambda: simulation.oral_result("accepted"),
                    row=1,
                )
                self._add(
                    "Устно: отклонено",
                    "📜",
                    discord.ButtonStyle.danger,
                    lambda: simulation.oral_result("rejected"),
                    row=1,
                )
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
    register_consensus_simulation(simulation)
    await interaction.response.send_message(
        embed=consensus_simulation_embed(simulation),
        view=ConsensusSimulationView(simulation),
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )
    try:
        simulation.control_message = await interaction.original_response()
    except discord.DiscordException:
        pass


__all__ = [
    "ConsensusSimulation",
    "ConsensusSimulationView",
    "InMemoryConsensusRepository",
    "SimulationScenarioSelect",
    "clear_consensus_simulation",
    "consensus_simulation_embed",
    "get_consensus_simulation",
    "register_consensus_simulation",
    "refresh_consensus_simulation_projection",
    "start_consensus_simulation",
]
