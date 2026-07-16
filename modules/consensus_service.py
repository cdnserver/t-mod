from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Protocol

from modules.consensus_core import (
    ConsensusStateError,
    LiveConsensusSession,
    LiveParticipant,
    LiveResult,
    calculate_consensus,
    session_from_snapshot,
    session_to_snapshot,
    transition_session,
)


VALID_VOTES = frozenset({"yes", "no"})


@dataclass(frozen=True)
class ConsensusActor:
    user_id: int | None = None
    display_name: str | None = None


class ConsensusRepository(Protocol):
    def save(
        self,
        session: LiveConsensusSession,
        event_type: str,
        *,
        actor: ConsensusActor | None = None,
        stage_from: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def active_snapshots(self, guild_id: int | None = None) -> list[dict[str, Any]]: ...

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
    ) -> dict[str, Any]: ...

    def save_with_deliveries(
        self,
        session: LiveConsensusSession,
        event_type: str,
        *,
        actor: ConsensusActor | None,
        stage_from: str | None,
        details: dict[str, Any] | None,
        deliveries: Iterable[dict[str, Any]],
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...


class ConsensusSessionRegistry:
    """In-process cache. The database remains the source of truth."""

    def __init__(self) -> None:
        self.sessions: dict[int, LiveConsensusSession] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(int(guild_id), asyncio.Lock())

    def get(self, guild_id: int) -> LiveConsensusSession | None:
        session = self.sessions.get(int(guild_id))
        return session if session is not None and not session.finished else None

    def find(self, session_key: str) -> LiveConsensusSession | None:
        key = str(session_key)
        return next((session for session in self.sessions.values() if session.session_key == key), None)

    def add(self, session: LiveConsensusSession) -> None:
        current = self.get(session.guild_id)
        if current is not None and current.session_key != session.session_key:
            raise ConsensusStateError("На сервере уже идёт другой консенсус.")
        self.sessions[int(session.guild_id)] = session

    def remove(self, guild_id: int, *, session_key: str | None = None) -> LiveConsensusSession | None:
        current = self.sessions.get(int(guild_id))
        if current is None or (session_key is not None and current.session_key != session_key):
            return None
        self._locks.pop(int(guild_id), None)
        return self.sessions.pop(int(guild_id), None)

    def restore(self, snapshots: Iterable[dict[str, Any]]) -> list[LiveConsensusSession]:
        restored: list[LiveConsensusSession] = []
        for snapshot in snapshots:
            session = session_from_snapshot(snapshot)
            if session.finished:
                continue
            self.add(session)
            restored.append(session)
        return restored


class ConsensusCoordinator:
    """The only place where persistent consensus state should be mutated."""

    def __init__(self, repository: ConsensusRepository) -> None:
        self.repository = repository

    @staticmethod
    def _restore_checkpoint(session: LiveConsensusSession, snapshot: dict[str, Any]) -> None:
        restored = session_from_snapshot(snapshot)
        runtime_message = session.host_message_obj
        runtime_timer = session.timer_task
        for field_name, value in vars(restored).items():
            if field_name not in {"host_message_obj", "timer_task"}:
                setattr(session, field_name, value)
        session.host_message_obj = runtime_message
        session.timer_task = runtime_timer

    @contextmanager
    def mutation(self, session: LiveConsensusSession) -> Iterator[None]:
        """Roll live state back if its persistence write fails."""

        checkpoint = session_to_snapshot(session)
        try:
            yield
        except BaseException:
            self._restore_checkpoint(session, checkpoint)
            raise

    def save(
        self,
        session: LiveConsensusSession,
        event_type: str,
        *,
        actor: ConsensusActor | None = None,
        stage_from: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = self.repository.save(
            session,
            event_type,
            actor=actor,
            stage_from=stage_from,
            details=details,
        )
        session.revision = int(row.get("revision") or session.revision)
        return row

    def save_with_deliveries(
        self,
        session: LiveConsensusSession,
        event_type: str,
        *,
        actor: ConsensusActor | None = None,
        stage_from: str | None = None,
        details: dict[str, Any] | None = None,
        deliveries: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        row = self.repository.save_with_deliveries(
            session,
            event_type,
            actor=actor,
            stage_from=stage_from,
            details=details,
            deliveries=deliveries,
        )
        session.revision = int(row.get("revision") or session.revision)
        return row

    def transition(
        self,
        session: LiveConsensusSession,
        target_stage: str,
        event_type: str,
        *,
        actor: ConsensusActor | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        was_finished = session.finished
        previous, _ = transition_session(session, target_stage)
        try:
            self.save(session, event_type, actor=actor, stage_from=previous, details=details)
        except Exception:
            session.stage = previous
            session.finished = was_finished
            raise

    def confirm_participant(
        self,
        session: LiveConsensusSession,
        user_id: int,
        *,
        actor: ConsensusActor | None = None,
    ) -> bool:
        if session.stage != "registration":
            raise ConsensusStateError("Регистрация на это заседание уже завершена.")
        participant = session.participants.get(int(user_id))
        if participant is None:
            raise ConsensusStateError("Пользователь не входит в состав этого консенсуса.")
        changed = not participant.confirmed or participant.dm_failed
        with self.mutation(session):
            participant.confirmed = True
            participant.dm_failed = False
            if changed:
                self.save(session, "participant_confirmed", actor=actor, details={"user_id": int(user_id)})
        return changed

    def bind_host_message(self, session: LiveConsensusSession, message_id: int | None, event_type: str) -> None:
        with self.mutation(session):
            session.host_message_id = int(message_id) if message_id else None
            self.save(session, event_type, details={"message_id": session.host_message_id})

    def cast_vote(
        self,
        session: LiveConsensusSession,
        user_id: int,
        vote: str,
        *,
        actor: ConsensusActor | None = None,
    ) -> bool:
        clean_vote = str(vote).strip().lower()
        if clean_vote not in VALID_VOTES:
            raise ConsensusStateError("Неизвестный вариант голоса.")
        if session.stage != "voting" or session.current_bill is None:
            raise ConsensusStateError("Голосование уже завершено или временно недоступно.")
        participant = session.participants.get(int(user_id))
        if participant is None or not participant.confirmed:
            raise ConsensusStateError("Участник не зарегистрирован в этом голосовании.")
        previous_vote = session.votes.get(int(user_id))
        with self.mutation(session):
            session.votes[int(user_id)] = clean_vote
            if previous_vote != clean_vote:
                self.save(
                    session,
                    "vote_cast",
                    actor=actor,
                    details={
                        "vote": clean_vote,
                        "previous_vote": previous_vote,
                        "bill_id": int(session.current_bill.get("id") or 0),
                    },
                )
        return session.all_voted()

    def begin_bill(self, session: LiveConsensusSession, bill: dict[str, Any], *, actor: ConsensusActor) -> None:
        if session.stage not in {"registration", "after_result"}:
            raise ConsensusStateError("Нельзя открыть следующий проект на текущем этапе.")
        if session.current_bill is not None:
            raise ConsensusStateError("Предыдущий проект ещё не закрыт.")
        if not session.quorum_ready():
            raise ConsensusStateError("Подтверждённый кворум не набран.")
        with self.mutation(session):
            session.current_bill = dict(bill)
            session.votes.clear()
            session.pending_action = None
            session.discussion_channel_id = None
            session.discussion_initiator_id = None
            session.discussion_type = None
            session.discussion_allowed_user_ids.clear()
            self.transition(
                session,
                "voting",
                "bill_voting_started",
                actor=actor,
                details={
                    "bill_id": int(bill.get("id") or 0),
                    "bill_number": int(bill.get("bill_number") or 0),
                },
            )

    def begin_bill_atomically(
        self,
        session: LiveConsensusSession,
        bill: dict[str, Any],
        *,
        actor: ConsensusActor,
        deliveries: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        if session.stage not in {"registration", "after_result"}:
            raise ConsensusStateError("Нельзя открыть следующий проект на текущем этапе.")
        if session.current_bill is not None:
            raise ConsensusStateError("Предыдущий проект ещё не закрыт.")
        if not session.quorum_ready():
            raise ConsensusStateError("Подтверждённый кворум не набран.")

        candidate = session_from_snapshot(session_to_snapshot(session))
        candidate.current_bill = dict(bill)
        candidate.votes.clear()
        candidate.pending_action = None
        candidate.discussion_channel_id = None
        candidate.discussion_initiator_id = None
        candidate.discussion_type = None
        candidate.discussion_allowed_user_ids.clear()
        previous, _ = transition_session(candidate, "voting")
        details = {
            "bill_id": int(bill.get("id") or 0),
            "bill_number": int(bill.get("bill_number") or 0),
        }
        receipt = self.repository.commit_begin_bill(
            candidate,
            expected_revision=int(session.revision),
            bill_id=int(bill.get("id") or 0),
            event_type="bill_voting_started",
            actor=actor,
            details={**details, "stage_from": previous},
            deliveries=deliveries,
        )
        persisted = dict(receipt.get("session") or {})
        candidate.revision = int(persisted.get("revision") or candidate.revision + 1)
        self._restore_checkpoint(session, session_to_snapshot(candidate))
        return receipt

    def claim_finalization(
        self,
        session: LiveConsensusSession,
        *,
        kind: str,
        actor: ConsensusActor | None,
        forced: bool = False,
        veto_authorized: bool = False,
    ) -> dict[str, Any]:
        if session.stage == "finalizing" and session.pending_action:
            if session.pending_action.get("kind") != kind:
                raise ConsensusStateError("Голосование уже фиксируется другим способом.")
            return dict(session.pending_action)
        if session.stage != "voting" or session.current_bill is None:
            raise ConsensusStateError("Текущее голосование уже закрыто.")
        if kind not in {"vote", "veto"}:
            raise ConsensusStateError("Неизвестный способ завершения голосования.")
        if kind == "veto" and (actor is None or actor.user_id is None or not veto_authorized):
            raise ConsensusStateError("Право вето не подтверждено для этого участника.")
        with self.mutation(session):
            session.pending_action = {
                "kind": kind,
                "forced": bool(forced),
                "actor_id": actor.user_id if actor else None,
                "actor_display": actor.display_name if actor else None,
                "bill_id": int(session.current_bill.get("id") or 0),
                "claimed_at": datetime.now(timezone.utc).isoformat(),
            }
            self.transition(
                session,
                "finalizing",
                "vote_finalization_claimed" if kind == "vote" else "veto_claimed",
                actor=actor,
                details=dict(session.pending_action),
            )
        return dict(session.pending_action)

    def complete_result(
        self,
        session: LiveConsensusSession,
        result: LiveResult,
        *,
        event_type: str,
        actor: ConsensusActor | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.mutation(session):
            previous, event_details = self._apply_result(session, result, details=details)
            self.save(session, event_type, actor=actor, stage_from=previous, details=event_details)

    def _apply_result(
        self,
        session: LiveConsensusSession,
        result: LiveResult,
        *,
        details: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if session.stage != "finalizing":
            raise ConsensusStateError("Результат можно зафиксировать только после блокировки голосования.")
        if session.current_bill is None or int(session.current_bill.get("id") or 0) != int(result.bill_id):
            raise ConsensusStateError("Фиксируется результат другого законопроекта.")
        if not any(existing.bill_id == result.bill_id for existing in session.results):
            session.results.append(result)
        session.current_bill = None
        session.votes.clear()
        session.pending_action = None
        session.timer_deadline = None
        session.timer_seconds = None
        event_details = {
            "bill_id": result.bill_id,
            "bill_number": result.bill_number,
            "status": result.status,
            "overall_percent": result.overall_percent,
        }
        event_details.update(details or {})
        previous, _ = transition_session(session, "after_result")
        return previous, event_details

    def complete_result_atomically(
        self,
        session: LiveConsensusSession,
        result: LiveResult,
        *,
        bill_status: str,
        result_summary: str,
        event_type: str,
        actor: ConsensusActor | None = None,
        details: dict[str, Any] | None = None,
        deliveries: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """Persist result, state transition, event and deliveries in one commit.

        A snapshot round-trip creates a candidate without copying runtime-only
        asyncio tasks or Discord message objects.  The live object is updated
        only after SQLite confirms the commit.
        """

        candidate = session_from_snapshot(session_to_snapshot(session))
        candidate.revision = int(session.revision)
        _, event_details = self._apply_result(candidate, result, details=details)
        receipt = self.repository.commit_finalization(
            candidate,
            result,
            expected_revision=int(session.revision),
            bill_status=bill_status,
            result_summary=result_summary,
            event_type=event_type,
            actor=actor,
            details=event_details,
            deliveries=deliveries,
        )
        persisted = dict(receipt.get("session") or {})
        candidate.revision = int(persisted.get("revision") or candidate.revision + 1)

        session.stage = candidate.stage
        session.current_bill = candidate.current_bill
        session.votes = candidate.votes
        session.results = candidate.results
        session.pending_action = candidate.pending_action
        session.timer_deadline = candidate.timer_deadline
        session.timer_seconds = candidate.timer_seconds
        session.finished = candidate.finished
        session.revision = candidate.revision
        return receipt

    def request_discussion(self, session: LiveConsensusSession, initiator: LiveParticipant) -> None:
        if session.stage != "voting" or session.current_bill is None:
            raise ConsensusStateError("Дискуссию можно начать только во время голосования.")
        if initiator.kind != "senator" or not initiator.confirmed:
            raise ConsensusStateError("Дискуссию может инициировать только зарегистрированный сенатор.")
        if session.discussion_initiator_id is not None:
            raise ConsensusStateError("Дискуссия по этому проекту уже инициирована.")
        with self.mutation(session):
            session.previous_stage = "voting"
            session.discussion_initiator_id = initiator.user_id
            self.transition(
                session,
                "discussion_type",
                "discussion_requested",
                actor=ConsensusActor(initiator.user_id, initiator.display_name),
            )

    def begin_discussion(
        self,
        session: LiveConsensusSession,
        discussion_type: str,
        *,
        channel_id: int | None,
        allowed_user_ids: Iterable[int],
    ) -> None:
        if session.stage != "discussion_type":
            raise ConsensusStateError("Тип дискуссии сейчас выбрать нельзя.")
        with self.mutation(session):
            session.discussion_type = str(discussion_type).strip()[:80]
            session.discussion_channel_id = int(channel_id) if channel_id else None
            session.discussion_allowed_user_ids = {int(user_id) for user_id in allowed_user_ids}
            self.transition(
                session,
                "discussion",
                "discussion_started",
                details={"type": session.discussion_type, "channel_id": session.discussion_channel_id},
            )

    def end_discussion(self, session: LiveConsensusSession, *, actor: ConsensusActor) -> None:
        if session.stage not in {"discussion_type", "discussion"}:
            raise ConsensusStateError("Активной дискуссии сейчас нет.")
        with self.mutation(session):
            session.discussion_type = None
            session.discussion_initiator_id = None
            session.discussion_allowed_user_ids.clear()
            for participant in session.participants.values():
                participant.discussion_message_id = None
            self.transition(session, "voting", "discussion_finished", actor=actor)

    def pause(
        self,
        session: LiveConsensusSession,
        reason: str,
        *,
        automatic: bool,
        actor: ConsensusActor | None,
    ) -> None:
        if session.stage == "paused":
            return
        if session.stage not in {"voting", "discussion_type", "discussion", "after_result"}:
            raise ConsensusStateError("На текущем этапе консенсус нельзя поставить на паузу.")
        with self.mutation(session):
            session.previous_stage = session.stage
            session.paused_reason = str(reason).strip()[:1000]
            session.pause_is_automatic = bool(automatic)
            self.transition(
                session,
                "paused",
                "session_paused_automatically" if automatic else "session_paused",
                actor=actor,
                details={"reason": session.paused_reason},
            )

    def resume(self, session: LiveConsensusSession, *, actor: ConsensusActor) -> str:
        if session.stage != "paused":
            raise ConsensusStateError("Консенсус не находится на паузе.")
        target = session.previous_stage or ("voting" if session.current_bill else "after_result")
        if target == "paused" or target == "finalizing":
            target = "voting" if session.current_bill else "after_result"
        with self.mutation(session):
            session.paused_reason = None
            session.pause_is_automatic = False
            self.transition(session, target, "session_resumed", actor=actor)
        return target

    def finish(self, session: LiveConsensusSession, *, actor: ConsensusActor, cancelled: bool = False) -> None:
        target = "cancelled" if cancelled else "finished"
        with self.mutation(session):
            session.pending_action = None
            session.timer_deadline = None
            session.timer_seconds = None
            self.transition(
                session,
                target,
                "session_cancelled" if cancelled else "session_finished",
                actor=actor,
                details={"result_count": len(session.results)},
            )

    def finish_atomically(
        self,
        session: LiveConsensusSession,
        *,
        actor: ConsensusActor,
        cancelled: bool = False,
        deliveries: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """Close a session, requeue its bill and advance numbering in one commit."""

        if session.finished or session.stage in {"finished", "cancelled"}:
            raise ConsensusStateError("Консенсус уже завершён.")
        if session.stage == "finalizing":
            raise ConsensusStateError("Сначала необходимо завершить фиксацию текущего решения.")
        candidate = session_from_snapshot(session_to_snapshot(session))
        current_bill_id = (
            int(candidate.current_bill.get("id") or 0)
            if candidate.current_bill is not None
            else None
        )
        candidate.current_bill = None
        candidate.votes.clear()
        candidate.pending_action = None
        candidate.timer_deadline = None
        candidate.timer_seconds = None
        target = "cancelled" if cancelled else "finished"
        transition_session(candidate, target)
        details = {"result_count": len(candidate.results), "requeued_bill_id": current_bill_id}
        receipt = self.repository.commit_finish(
            candidate,
            expected_revision=int(session.revision),
            current_bill_id=current_bill_id,
            advance_plenary=not cancelled,
            event_type="session_cancelled" if cancelled else "session_finished",
            actor=actor,
            details=details,
            deliveries=deliveries,
        )
        persisted = dict(receipt.get("session") or {})
        candidate.revision = int(persisted.get("revision") or candidate.revision + 1)
        self._restore_checkpoint(session, session_to_snapshot(candidate))
        return receipt

    @staticmethod
    def calculate(session: LiveConsensusSession) -> dict[str, Any]:
        return calculate_consensus(session)

    @staticmethod
    def snapshot(session: LiveConsensusSession) -> dict[str, Any]:
        return session_to_snapshot(session)
