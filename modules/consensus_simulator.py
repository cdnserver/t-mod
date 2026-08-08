"""Isolated Discord simulator driven by the production consensus coordinator.

The simulator uses the same state mutations, guards and result calculation as
the live workflow.  Its repository and registry are deliberately in-memory, so
training can never alter real bills, plenary numbers, votes or DM deliveries.
"""

from __future__ import annotations

import asyncio
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
    session_to_snapshot,
)
from modules.consensus_service import ConsensusActor, ConsensusCoordinator
from modules.consensus_v3 import CONSENSUS_ENGINE_VERSION
from modules.tvrs_config import (
    TVRS_COCHAIR_IDS,
    TVRS_PERMANENT_CHAIR_ID,
    TVRS_TIMER_OPTIONS,
)
from modules.tvrs_embeds import build_final_summary_embed, build_result_embed
from modules.tvrs_formatting import role_label
from modules.tvrs_presentation import (
    build_dm_vote_embed,
    build_live_vote_embed,
    build_registration_embed,
    participant_kind,
)


SIMULATION_COLOR = 0x9B59B6
SIMULATION_FAKE_CHAIR_SPECS = (
    (-100, "Фейк-сопредседатель Ирина", "chair", "first"),
    (-101, "Фейк-сопредседатель Марта", "chair", "second"),
    (-102, "Фейк-сопредседатель Лев", "chair", "third"),
)
SIMULATION_FAKE_SENATOR_SPECS = (
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
    invited_participants: tuple[LiveParticipant, ...] = ()
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
        real_roster = bool(self.invited_participants)
        leader_block = (
            ("first", "second", "third")[TVRS_COCHAIR_IDS.index(self.leader_id)]
            if self.leader_id in TVRS_COCHAIR_IDS[:3]
            else ("first" if not real_roster else None)
        )
        participants = {
            self.leader_id: LiveParticipant(
                user_id=self.leader_id,
                display_name=self.leader_display,
                mention=f"<@{self.leader_id}>",
                kind="chair",
                permanent=(
                    self.leader_id == TVRS_PERMANENT_CHAIR_ID
                    if real_roster
                    else True
                ),
                confirmed=True,
                voting_block=leader_block,
            )
        }
        for source in self.invited_participants:
            if int(source.user_id) == self.leader_id:
                continue
            participants[int(source.user_id)] = LiveParticipant(
                user_id=int(source.user_id),
                display_name=str(source.display_name),
                mention=str(source.mention),
                kind=source.kind,
                permanent=bool(source.permanent),
                voting_block=source.voting_block,
            )

        # A mixed simulation keeps the canonical six-seat training roster.
        # Selected real users occupy their natural chair block or one of the
        # internal-consensus seats; every unoccupied seat remains controllable
        # by the host as a fake participant.
        occupied_blocks = {
            participant.voting_block
            for participant in participants.values()
            if participant.voting_block in {"first", "second", "third"}
        }
        for user_id, display_name, kind, voting_block in SIMULATION_FAKE_CHAIR_SPECS:
            if voting_block in occupied_blocks:
                continue
            participants[user_id] = LiveParticipant(
                user_id=user_id,
                display_name=display_name,
                mention=display_name,
                kind=kind,  # type: ignore[arg-type]
                permanent=voting_block == "third",
                voting_block=voting_block,  # type: ignore[arg-type]
            )

        real_internal_seats = sum(
            participant.user_id > 0 and participant.voting_block is None
            for participant in participants.values()
        )
        fake_internal_seats = max(
            0,
            len(SIMULATION_FAKE_SENATOR_SPECS) - real_internal_seats,
        )
        for user_id, display_name, kind, voting_block in (
            SIMULATION_FAKE_SENATOR_SPECS[:fake_internal_seats]
        ):
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
        self._record(
            "Симуляция открыта; ведущий подтверждён автоматически"
            if not real_roster
            else "Точный тестовый контур открыт; сенаторам направлены приглашения"
        )

    @property
    def finished(self) -> bool:
        return self.session.finished

    @property
    def actor(self) -> ConsensusActor:
        return ConsensusActor(self.leader_id, self.leader_display)

    @property
    def has_real_roster(self) -> bool:
        return bool(self.invited_participants)

    @property
    def has_fake_roster(self) -> bool:
        return any(user_id < 0 for user_id in self.session.participants)

    @property
    def invited_user_ids(self) -> set[int]:
        return {
            participant.user_id
            for participant in self.session.participants.values()
            if participant.user_id > 0 and participant.user_id != self.leader_id
        }

    def participant_actor(self, user_id: int) -> ConsensusActor:
        participant = self.session.participants.get(int(user_id))
        if participant is None:
            raise ConsensusStateError("Пользователь не входит в тестовый состав.")
        return ConsensusActor(participant.user_id, participant.display_name)

    def confirm_participant(self, user_id: int) -> None:
        self._require_stage("registration")
        participant = self.session.participants.get(int(user_id))
        if participant is None:
            raise ConsensusStateError("Пользователь не входит в тестовый состав.")
        self.coordinator.confirm_participant(
            self.session,
            participant.user_id,
            actor=self.participant_actor(participant.user_id),
        )
        self._record(f"{participant.display_name} подтвердил участие")

    def cast_participant_vote(self, user_id: int, vote: str) -> bool:
        participant = self.session.participants.get(int(user_id))
        if participant is None or not participant.confirmed:
            raise ConsensusStateError("Вы не зарегистрированы в этой симуляции.")
        should_finalize = self.coordinator.cast_vote(
            self.session,
            participant.user_id,
            vote,
            actor=self.participant_actor(participant.user_id),
        )
        vote_label = {
            "yes": "За",
            "no": "Против",
            "abstain": "Воздержался",
        }.get(vote, vote)
        self._record(f"{participant.display_name}: {vote_label}")
        if should_finalize:
            self.finalize(forced=False)
        return should_finalize

    def queue_text(self, limit: int = 5) -> str:
        rows = self.queue_bills(limit)
        return "\n".join(
            f"`{index:02d}` №`{int(row['bill_number']):03d}` — **{row['title']}**"
            for index, row in enumerate(rows, 1)
        ) or "Очередь пуста."

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
                if item.user_id < 0 and not item.confirmed
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
            if participant.user_id >= 0:
                continue
            self.coordinator.confirm_participant(
                self.session,
                participant.user_id,
                actor=ConsensusActor(participant.user_id, participant.display_name),
            )
        self._record("Все фейковые участники подтвердили участие")

    def resend_invitations(self) -> None:
        self._require_stage("registration")
        self.coordinator.save(
            self.session,
            "registration_invitations_retried",
            actor=self.actor,
        )
        self._record("Приглашения участникам отправлены повторно")

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
        self.cast_participant_vote(self.leader_id, vote)

    def cast_next_fake_vote(self) -> None:
        self._require_stage("voting")
        candidates = [
            participant
            for participant in self.session.confirmed_participants()
            if participant.user_id < 0
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
        fake_participants = [
            participant
            for participant in self.session.confirmed_participants()
            if participant.user_id < 0
        ]
        if not fake_participants:
            raise ConsensusStateError("В составе нет фейковых участников.")
        fake_senators = [
            participant
            for participant in fake_participants
            if participant.kind == "senator"
        ]
        should_finalize = False
        for participant in fake_participants:
            if scenario == "accepted":
                vote = "yes"
            elif scenario == "rejected":
                vote = "no"
            elif participant.voting_block == "first":
                vote = "yes"
            elif participant.voting_block == "second":
                vote = "no"
            elif participant.voting_block == "third":
                vote = "yes"
            else:
                vote = "no" if fake_senators.index(participant) == 1 else "yes"
            should_finalize = self.coordinator.cast_vote(
                self.session,
                participant.user_id,
                vote,
                actor=ConsensusActor(participant.user_id, participant.display_name),
            )
        self._record(f"Применён сценарий «{SIMULATION_SCENARIOS[scenario]}»")
        if should_finalize:
            self.finalize(forced=False)

    def request_discussion(self, user_id: int | None = None) -> None:
        initiator = self.session.participants[int(user_id or self.leader_id)]
        self.coordinator.request_discussion(self.session, initiator)
        self._record(f"{initiator.display_name} запросил дискуссию")

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

    def veto(self, user_id: int | None = None) -> LiveResult:
        veto_user_id = int(user_id or self.leader_id)
        participant = self.session.participants.get(veto_user_id)
        if participant is None or not participant.permanent:
            raise ConsensusStateError(
                "Право вето доступно только постоянному председателю."
            )
        veto_actor = self.participant_actor(veto_user_id)
        bill = dict(self.session.current_bill or {})
        if not bill:
            raise ConsensusStateError("В симуляции нет текущего проекта.")
        self.coordinator.claim_finalization(
            self.session,
            kind="veto",
            actor=veto_actor,
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
            veto_by_id=veto_user_id,
            resolved_by_id=veto_user_id,
            resolved_by_display=participant.display_name,
        )
        self.coordinator.complete_result_atomically(
            self.session,
            result,
            bill_status="vetoed",
            result_summary="Применено учебное право вето",
            event_type="veto_applied",
            actor=veto_actor,
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


def consensus_simulation_embed(simulation: ConsensusSimulation) -> discord.Embed:
    """Render the production card against an isolated in-memory session."""

    session = simulation.session
    if session.finished:
        embed = build_final_summary_embed(session)
    elif session.stage == "registration":
        embed = build_registration_embed(
            session,
            queue_override=simulation.queue_text(8),
            voice_override="🧪 не проверяется в изолированном тесте",
            description_override=(
                "Это изолированный пульт ведущего. Выбранные участники подтверждают "
                "тестовое участие в личном сообщении T-Mod. Ведущий зарегистрирован "
                "автоматически."
            ),
        )
    elif session.stage == "after_result" and session.results:
        embed = build_result_embed(session.results[-1], session)
    else:
        embed = build_live_vote_embed(
            session,
            queue_override=simulation.queue_text(5),
        )
    embed.color = discord.Color(SIMULATION_COLOR)
    embed.set_author(
        name="ИЗОЛИРОВАННЫЙ ТЕСТ • интерфейс и правила рабочего консенсуса"
    )
    return embed


def _simulation_registration_dm_embed(
    simulation: ConsensusSimulation,
    participant: LiveParticipant,
) -> discord.Embed:
    embed = discord.Embed(
        title="Пленарный консенсус Товарищества",
        description=(
            f"Ведущий: <@{simulation.leader_id}>\n"
            f"Роль: **{role_label(participant)}**"
        ),
        color=SIMULATION_COLOR,
    )
    embed.add_field(
        name="Очередь законопроектов",
        value=simulation.queue_text(10),
        inline=False,
    )
    embed.set_author(name="ИЗОЛИРОВАННЫЙ ТЕСТ • результат не попадёт в базу")
    return embed


def _simulation_participant_embed(
    simulation: ConsensusSimulation,
    participant: LiveParticipant,
) -> discord.Embed:
    session = simulation.session
    if session.stage == "registration":
        return _simulation_registration_dm_embed(simulation, participant)
    if session.finished:
        embed = build_final_summary_embed(session, compact=True)
    elif session.stage == "after_result" and session.results:
        embed = build_result_embed(session.results[-1], session)
    else:
        embed = build_dm_vote_embed(
            session,
            participant,
            queue_override=simulation.queue_text(4),
        )
    embed.color = discord.Color(SIMULATION_COLOR)
    embed.set_author(name="ИЗОЛИРОВАННЫЙ ТЕСТ • интерфейс рабочего консенсуса")
    return embed


async def _resolve_simulation_member(
    guild: discord.Guild,
    user_id: int,
) -> discord.Member | None:
    member = guild.get_member(int(user_id))
    if member is not None:
        return member
    try:
        return await guild.fetch_member(int(user_id))
    except discord.DiscordException:
        return None


async def _refresh_simulation_participant_dms(
    simulation: ConsensusSimulation,
    guild: discord.Guild,
) -> None:
    """Converge the same canonical DM as the production control delivery."""

    semaphore = asyncio.Semaphore(4)

    async def refresh_one(participant: LiveParticipant) -> None:
        if participant.user_id == simulation.leader_id or participant.user_id <= 0:
            return
        async with semaphore:
            member = await _resolve_simulation_member(guild, participant.user_id)
            if member is None:
                participant.dm_failed = True
                return
            view: discord.ui.View | None = None
            if simulation.session.stage == "registration" and not participant.confirmed:
                view = SimulationParticipantView(simulation, participant.user_id)
            elif simulation.session.stage == "voting":
                view = SimulationParticipantView(simulation, participant.user_id)
            content = (
                "Подтвердите участие в тестовом консенсусе."
                if simulation.session.stage == "registration" and not participant.confirmed
                else "Участие подтверждено. Ожидайте начала рассмотрения."
                if simulation.session.stage == "registration"
                else "Законопроект представлен. Голосование откроет ведущий."
                if simulation.session.stage == "presentation"
                else None
            )
            embed = _simulation_participant_embed(simulation, participant)
            message_id = int(
                participant.vote_message_id or participant.dm_message_id or 0
            )
            for attempt in range(3):
                try:
                    dm_channel = member.dm_channel or await member.create_dm()
                    message = (
                        await dm_channel.fetch_message(message_id)
                        if message_id
                        else None
                    )
                    if message is None:
                        message = await member.send(
                            content=content,
                            embed=embed,
                            view=view,
                        )
                    else:
                        await message.edit(content=content, embed=embed, view=view)
                    participant.dm_message_id = int(message.id)
                    participant.vote_message_id = int(message.id)
                    participant.vote_bill_id = int(
                        (simulation.session.current_bill or {}).get("id") or 0
                    ) or None
                    participant.dm_failed = False
                    return
                except discord.NotFound:
                    message_id = 0
                except discord.DiscordException:
                    if attempt >= 2:
                        participant.dm_failed = True
                        return
                    await asyncio.sleep(0.35 * (attempt + 1))

    await asyncio.gather(
        *(refresh_one(item) for item in simulation.session.participants.values())
    )


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
        content="🧪 **ИЗОЛИРОВАННЫЙ ТЕСТ** · действия не изменяют рабочий консенсус",
        embed=consensus_simulation_embed(simulation),
        view=None if simulation.finished else ConsensusSimulationView(simulation),
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )
    guild = interaction.client.get_guild(simulation.guild_id)
    if guild is not None:
        await _refresh_simulation_participant_dms(simulation, guild)


async def refresh_consensus_simulation_projection(
    simulation: ConsensusSimulation,
    *,
    guild: discord.Guild | None = None,
) -> None:
    """Converge the original Discord simulator card after a web action."""

    message = simulation.control_message
    if message is not None:
        try:
            await message.edit(
                content=(
                    "🧪 **ИЗОЛИРОВАННЫЙ ТЕСТ** · действия не изменяют рабочий консенсус"
                ),
                embed=consensus_simulation_embed(simulation),
                view=None if simulation.finished else ConsensusSimulationView(simulation),
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                ),
            )
        except discord.DiscordException:
            pass
        if guild is None:
            guild = getattr(message, "guild", None)
    if guild is not None:
        await _refresh_simulation_participant_dms(simulation, guild)


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


class SimulationDiscussionTypeView(discord.ui.View):
    def __init__(self, simulation: ConsensusSimulation, user_id: int) -> None:
        super().__init__(timeout=300)
        self.simulation = simulation
        self.user_id = int(user_id)
        self.bill_id = int((simulation.session.current_bill or {}).get("id") or 0)
        for label in ("Правовая", "Фактическая", "Процедурная", "Иная"):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            button.callback = self._callback(label)  # type: ignore[assignment]
            self.add_item(button)

    def _callback(self, label: str):
        async def callback(interaction: discord.Interaction) -> None:
            current = get_consensus_simulation(self.simulation.guild_id)
            if (
                current is not self.simulation
                or interaction.user.id != self.user_id
                or self.simulation.session.stage != "discussion_type"
                or int((self.simulation.session.current_bill or {}).get("id") or 0)
                != self.bill_id
            ):
                await interaction.response.send_message(
                    "Это меню относится к уже завершённому этапу теста.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer()
            try:
                self.simulation.choose_discussion(label)
            except ConsensusStateError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            guild = interaction.client.get_guild(self.simulation.guild_id)
            await refresh_consensus_simulation_projection(
                self.simulation,
                guild=guild,
            )
            await interaction.followup.send(
                f"Дискуссия типа **{label}** начата в тестовом контуре.",
                ephemeral=True,
            )

        return callback


class SimulationVetoConfirmView(discord.ui.View):
    def __init__(self, simulation: ConsensusSimulation, user_id: int) -> None:
        super().__init__(timeout=90)
        self.simulation = simulation
        self.user_id = int(user_id)
        self.bill_id = int((simulation.session.current_bill or {}).get("id") or 0)

    @discord.ui.button(label="Подтвердить вето", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        current_bill_id = int(
            (self.simulation.session.current_bill or {}).get("id") or 0
        )
        if (
            interaction.user.id != self.user_id
            or get_consensus_simulation(self.simulation.guild_id) is not self.simulation
            or self.simulation.session.stage != "voting"
            or current_bill_id != self.bill_id
        ):
            await interaction.response.send_message(
                "Подтверждение устарело.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        try:
            self.simulation.veto(self.user_id)
        except ConsensusStateError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        guild = interaction.client.get_guild(self.simulation.guild_id)
        await refresh_consensus_simulation_projection(self.simulation, guild=guild)
        await interaction.edit_original_response(content="Учебное вето применено.", view=None)


class SimulationParticipantView(discord.ui.View):
    """Participant controls with the same stage and generation fences as live."""

    def __init__(self, simulation: ConsensusSimulation, user_id: int) -> None:
        super().__init__(timeout=3600)
        self.simulation = simulation
        self.user_id = int(user_id)
        self.session_key = simulation.session.session_key
        self.bill_id = int((simulation.session.current_bill or {}).get("id") or 0)
        participant = simulation.session.participants.get(self.user_id)
        if simulation.session.stage == "registration":
            self._add("Подтвердить участие", discord.ButtonStyle.success, self._confirm)
        elif simulation.session.stage == "voting" and participant is not None:
            self._add("За", discord.ButtonStyle.success, lambda i: self._vote(i, "yes"))
            self._add("Против", discord.ButtonStyle.danger, lambda i: self._vote(i, "no"))
            self._add(
                "Воздержаться",
                discord.ButtonStyle.secondary,
                lambda i: self._vote(i, "abstain"),
            )
            if not simulation.session.discussion_initiator_id:
                self._add("Дискуссия", discord.ButtonStyle.secondary, self._discussion)
            if participant.permanent:
                self._add("Вето!", discord.ButtonStyle.danger, self._veto)

    def _add(self, label: str, style: discord.ButtonStyle, callback: Any) -> None:
        button = discord.ui.Button(label=label, style=style)
        button.callback = callback
        self.add_item(button)

    def _current(self, *, stage: str) -> bool:
        return bool(
            get_consensus_simulation(self.simulation.guild_id) is self.simulation
            and self.simulation.session.session_key == self.session_key
            and self.simulation.session.stage == stage
            and (
                stage == "registration"
                or int((self.simulation.session.current_bill or {}).get("id") or 0)
                == self.bill_id
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Эта кнопка не для вас.", ephemeral=True)
            return False
        return True

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if not self._current(stage="registration"):
            await interaction.response.send_message("Регистрация уже завершена.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            self.simulation.confirm_participant(self.user_id)
        except ConsensusStateError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        guild = interaction.client.get_guild(self.simulation.guild_id)
        await refresh_consensus_simulation_projection(self.simulation, guild=guild)

    async def _vote(self, interaction: discord.Interaction, vote: str) -> None:
        if not self._current(stage="voting"):
            await interaction.response.send_message(
                "Эта кнопка относится к уже завершённому проекту.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        try:
            self.simulation.cast_participant_vote(self.user_id, vote)
        except ConsensusStateError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        guild = interaction.client.get_guild(self.simulation.guild_id)
        await refresh_consensus_simulation_projection(self.simulation, guild=guild)

    async def _discussion(self, interaction: discord.Interaction) -> None:
        if not self._current(stage="voting"):
            await interaction.response.send_message("Голосование уже изменилось.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            self.simulation.request_discussion(self.user_id)
        except ConsensusStateError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        guild = interaction.client.get_guild(self.simulation.guild_id)
        await refresh_consensus_simulation_projection(self.simulation, guild=guild)
        await interaction.followup.send(
            "Выберите тип дискуссии.",
            view=SimulationDiscussionTypeView(self.simulation, self.user_id),
            ephemeral=True,
        )

    async def _veto(self, interaction: discord.Interaction) -> None:
        if not self._current(stage="voting"):
            await interaction.response.send_message("Голосование уже изменилось.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Подтвердите применение права вето в изолированном тесте.",
            view=SimulationVetoConfirmView(self.simulation, self.user_id),
            ephemeral=True,
        )


class ConsensusSimulationView(discord.ui.View):
    def __init__(self, simulation: ConsensusSimulation) -> None:
        super().__init__(timeout=3600)
        self.simulation = simulation
        stage = simulation.session.stage
        if stage == "registration":
            if simulation.has_real_roster:
                self._add(
                    "Повторить приглашения",
                    "📨",
                    discord.ButtonStyle.secondary,
                    simulation.resend_invitations,
                )
            if simulation.has_fake_roster:
                self._add("Следующее подтверждение", "✅", discord.ButtonStyle.secondary, simulation.confirm_next)
                self._add("Подтвердить фейков", "👥", discord.ButtonStyle.success, simulation.confirm_all)
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
            if simulation.has_fake_roster:
                self._add("Ход фейка", "🤖", discord.ButtonStyle.secondary, simulation.cast_next_fake_vote)
                self.add_item(SimulationScenarioSelect(simulation))
            self._add("Дискуссия", "💬", discord.ButtonStyle.secondary, simulation.request_discussion, row=2)
            self._add("Пауза", "⏸️", discord.ButtonStyle.secondary, simulation.pause, row=2)
            self._add("Завершить голосование", "📌", discord.ButtonStyle.secondary, simulation.finalize, row=2)
            for label, seconds in TVRS_TIMER_OPTIONS:
                self._add(
                    f"Таймер {label}",
                    "⏱️",
                    discord.ButtonStyle.secondary,
                    lambda selected=seconds: simulation.set_timer(selected),
                    row=3,
                )
            if simulation.session.participants[simulation.leader_id].permanent:
                self._add("Вето!", "🛑", discord.ButtonStyle.danger, simulation.veto, row=4)
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


def _simulation_participant_from_member(member: discord.Member) -> LiveParticipant | None:
    kind = participant_kind(member)
    if kind is None or member.bot:
        return None
    voting_block = (
        ("first", "second", "third")[TVRS_COCHAIR_IDS.index(member.id)]
        if member.id in TVRS_COCHAIR_IDS[:3]
        else None
    )
    return LiveParticipant(
        user_id=member.id,
        display_name=member.display_name,
        mention=member.mention,
        kind=kind,  # type: ignore[arg-type]
        permanent=member.id == TVRS_PERMANENT_CHAIR_ID,
        voting_block=voting_block,  # type: ignore[arg-type]
    )


class SimulationRosterSelect(discord.ui.UserSelect):
    def __init__(self, setup_view: "ConsensusSimulationSetupView") -> None:
        self.setup_view = setup_view
        super().__init__(
            placeholder="Выберите настоящих участников (можно не выбирать)",
            min_values=0,
            max_values=20,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.setup_view.interaction_check(interaction):
            return
        selected: list[LiveParticipant] = []
        rejected: list[str] = []
        for value in self.values:
            member = value if isinstance(value, discord.Member) else None
            if member is None and interaction.guild is not None:
                member = interaction.guild.get_member(int(value.id))
            if member is None or member.id == self.setup_view.leader_id:
                continue
            participant = _simulation_participant_from_member(member)
            if participant is None:
                rejected.append(getattr(value, "display_name", str(value)))
            else:
                selected.append(participant)
        self.setup_view.selected = {
            participant.user_id: participant for participant in selected
        }
        chosen = ", ".join(item.mention for item in selected) or "никого"
        warning = (
            "\nНе включены (нет роли консенсуса): " + ", ".join(rejected)
            if rejected
            else ""
        )
        await interaction.response.edit_message(
            content=(
                f"**Тестовый состав:** {chosen}{warning}\n"
                "Выбранным участникам придёт настоящее тестовое ЛС, а все "
                "оставшиеся места симулятор заполнит фейковыми участниками."
            ),
            view=self.setup_view,
        )


class ConsensusSimulationSetupView(discord.ui.View):
    def __init__(self, leader_id: int) -> None:
        super().__init__(timeout=600)
        self.leader_id = int(leader_id)
        self.selected: dict[int, LiveParticipant] = {}
        self.add_item(SimulationRosterSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.leader_id:
            await interaction.response.send_message(
                "Настроить этот тест может только его ведущий.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Запустить точную симуляцию",
        emoji="🧪",
        style=discord.ButtonStyle.success,
        row=1,
    )
    async def launch(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "Симуляция запускается только в канале сервера.",
                ephemeral=True,
            )
            return
        current = get_consensus_simulation(interaction.guild.id)
        if current is not None and not current.finished:
            await interaction.response.send_message(
                "На сервере уже идёт симуляция. Завершите её перед запуском новой.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        simulation = ConsensusSimulation(
            guild_id=interaction.guild.id,
            leader_id=interaction.user.id,
            leader_display=getattr(
                interaction.user,
                "display_name",
                str(interaction.user),
            ),
            invited_participants=tuple(self.selected.values()),
        )
        register_consensus_simulation(simulation)
        message = await interaction.channel.send(
            content=(
                "🧪 **ИЗОЛИРОВАННЫЙ ТЕСТ** · интерфейс, стадии и расчёт "
                "совпадают с рабочим консенсусом; база не изменяется"
            ),
            embed=consensus_simulation_embed(simulation),
            view=ConsensusSimulationView(simulation),
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=False,
                everyone=False,
            ),
        )
        simulation.control_message = message
        await _refresh_simulation_participant_dms(simulation, interaction.guild)
        unavailable = [
            participant.mention
            for participant in simulation.session.participants.values()
            if participant.dm_failed
        ]
        suffix = (
            " ЛС недоступны: " + ", ".join(unavailable)
            if unavailable
            else " Все приглашения доставлены."
        )
        await interaction.edit_original_response(
            content=f"Точная симуляция запущена.{suffix}",
            view=None,
        )


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
    await interaction.response.send_message(
        content=(
            "Выберите любых реальных сенаторов и председателей для теста — "
            "остальные места автоматически займут фейковые участники. Симулятор "
            "использует производственный координатор, правила 25/25/25/25 и те же "
            "личные панели, но хранит сессию отдельно от рабочей базы."
        ),
        view=ConsensusSimulationSetupView(interaction.user.id),
        ephemeral=True,
    )


__all__ = [
    "ConsensusSimulation",
    "ConsensusSimulationSetupView",
    "ConsensusSimulationView",
    "InMemoryConsensusRepository",
    "SimulationScenarioSelect",
    "SimulationParticipantView",
    "clear_consensus_simulation",
    "consensus_simulation_embed",
    "get_consensus_simulation",
    "register_consensus_simulation",
    "refresh_consensus_simulation_projection",
    "start_consensus_simulation",
]
