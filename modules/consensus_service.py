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
    validate_consensus_roster,
)


VALID_VOTES = frozenset({"yes", "no", "abstain"})


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
    def _validated_bill(
        session: LiveConsensusSession,
        bill: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(bill, dict):
            raise ConsensusStateError("Законопроект имеет некорректный формат.")
        try:
            bill_id = int(bill.get("id") or 0)
            bill_number = int(bill.get("bill_number") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ConsensusStateError("Законопроект содержит некорректный номер.") from exc
        if bill_id <= 0 or bill_number <= 0:
            raise ConsensusStateError("У законопроекта отсутствует устойчивый номер.")
        if any(int(result.bill_id) == bill_id for result in session.results):
            raise ConsensusStateError("Этот законопроект уже рассмотрен в текущем заседании.")
        return dict(bill)

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

    def repair_safe_invariants(
        self,
        session: LiveConsensusSession,
        *,
        actor: ConsensusActor | None = None,
    ) -> tuple[str, ...]:
        """Repair only stale fields whose intended value is unambiguous.

        This deliberately refuses to infer a missing bill, rewrite a roster or
        touch a finalization claim.  Those situations require a chair decision;
        background recovery may only remove leftovers that cannot belong to the
        current stage.
        """

        repaired: list[str] = []
        with self.mutation(session):
            partial_timer = (session.timer_deadline is None) != (session.timer_seconds is None)
            if session.stage != "voting" and (
                session.timer_deadline is not None or session.timer_seconds is not None
            ):
                session.timer_deadline = None
                session.timer_seconds = None
                session.timer_added_seconds = 0
                session.timer_last_added_seconds = None
                session.timer_last_adjusted_at = None
                repaired.append("timer_outside_voting")
            elif partial_timer:
                session.timer_deadline = None
                session.timer_seconds = None
                session.timer_added_seconds = 0
                session.timer_last_added_seconds = None
                session.timer_last_adjusted_at = None
                repaired.append("partial_timer_state")

            if (
                session.stage in {"registration", "presentation", "after_result"}
                and session.current_bill is None
                and session.votes
            ):
                session.votes.clear()
                repaired.append("stale_votes")

            if session.stage not in {"discussion", "discussion_type", "paused", "finalizing"}:
                discussion_fields = bool(
                    session.discussion_channel_id
                    or session.discussion_initiator_id
                    or session.discussion_type
                    or session.discussion_allowed_user_ids
                    or session.discussion_note_message_id
                    or any(item.discussion_message_id for item in session.participants.values())
                )
                if discussion_fields:
                    session.discussion_channel_id = None
                    session.discussion_initiator_id = None
                    session.discussion_type = None
                    session.discussion_allowed_user_ids.clear()
                    session.discussion_note_message_id = None
                    for participant in session.participants.values():
                        participant.discussion_message_id = None
                    repaired.append("stale_discussion_state")

            if (
                session.stage in {"registration", "presentation", "after_result"}
                and session.current_bill is None
                and session.pending_action is not None
            ):
                session.pending_action = None
                repaired.append("orphan_pending_action")

            if session.stage != "paused" and (
                session.paused_reason is not None or session.pause_is_automatic
            ):
                session.paused_reason = None
                session.pause_is_automatic = False
                repaired.append("stale_pause_state")

            if repaired:
                self.save(
                    session,
                    "session_invariants_repaired",
                    actor=actor,
                    details={"repairs": repaired},
                )
        return tuple(repaired)

    def cast_vote(
        self,
        session: LiveConsensusSession,
        user_id: int,
        vote: str,
        *,
        actor: ConsensusActor | None = None,
    ) -> bool:
        validate_consensus_roster(session)
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

    def set_timer(
        self,
        session: LiveConsensusSession,
        *,
        seconds: int,
        deadline: datetime,
        actor: ConsensusActor,
        added_seconds: int | None = None,
    ) -> None:
        """Persist a voting deadline as one rollback-safe state mutation."""

        validate_consensus_roster(session)
        if session.stage != "voting" or session.current_bill is None:
            raise ConsensusStateError("Таймер можно установить только во время голосования.")
        clean_seconds = max(0, int(seconds))
        clean_deadline = deadline.astimezone(timezone.utc)
        previous_deadline = session.timer_deadline
        previous_seconds = max(0, int(session.timer_seconds or 0))
        inferred_added = (
            max(0, clean_seconds - previous_seconds)
            if previous_deadline is not None
            and previous_deadline > datetime.now(timezone.utc)
            and clean_deadline > previous_deadline
            else 0
        )
        clean_added = (
            inferred_added
            if added_seconds is None
            else max(0, int(added_seconds))
        )
        with self.mutation(session):
            session.timer_seconds = clean_seconds
            session.timer_deadline = clean_deadline
            if clean_added:
                session.timer_added_seconds = max(
                    0,
                    int(session.timer_added_seconds),
                ) + clean_added
                session.timer_last_added_seconds = clean_added
                session.timer_last_adjusted_at = datetime.now(timezone.utc)
            else:
                session.timer_added_seconds = 0
                session.timer_last_added_seconds = None
                session.timer_last_adjusted_at = None
            self.save(
                session,
                "timer_extended" if clean_added else "timer_set",
                actor=actor,
                details={
                    "seconds": clean_seconds,
                    "added_seconds": clean_added,
                    "deadline": clean_deadline.isoformat(),
                },
            )

    def begin_bill(self, session: LiveConsensusSession, bill: dict[str, Any], *, actor: ConsensusActor) -> None:
        validate_consensus_roster(session)
        if session.stage not in {"registration", "after_result"}:
            raise ConsensusStateError("Нельзя открыть следующий проект на текущем этапе.")
        if session.current_bill is not None:
            raise ConsensusStateError("Предыдущий проект ещё не закрыт.")
        if not session.quorum_ready():
            raise ConsensusStateError("Подтверждённый кворум не набран.")
        clean_bill = self._validated_bill(session, bill)
        with self.mutation(session):
            session.current_bill = clean_bill
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
        validate_consensus_roster(session)
        if session.stage not in {"registration", "after_result"}:
            raise ConsensusStateError("Нельзя открыть следующий проект на текущем этапе.")
        if session.current_bill is not None:
            raise ConsensusStateError("Предыдущий проект ещё не закрыт.")
        if not session.quorum_ready():
            raise ConsensusStateError("Подтверждённый кворум не набран.")

        clean_bill = self._validated_bill(session, bill)
        candidate = session_from_snapshot(session_to_snapshot(session))
        candidate.current_bill = clean_bill
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

    def present_bill_atomically(
        self,
        session: LiveConsensusSession,
        bill: dict[str, Any],
        *,
        actor: ConsensusActor,
        deliveries: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        """Bind the next bill without opening voting controls."""

        validate_consensus_roster(session)
        if session.stage not in {"registration", "after_result"}:
            raise ConsensusStateError("Нельзя представить следующий проект на текущем этапе.")
        if session.current_bill is not None:
            raise ConsensusStateError("Предыдущий проект ещё не закрыт.")
        if not session.quorum_ready():
            raise ConsensusStateError("Подтверждённый кворум не набран.")

        clean_bill = self._validated_bill(session, bill)
        candidate = session_from_snapshot(session_to_snapshot(session))
        candidate.current_bill = clean_bill
        candidate.votes.clear()
        candidate.pending_action = None
        candidate.discussion_channel_id = None
        candidate.discussion_initiator_id = None
        candidate.discussion_type = None
        candidate.discussion_allowed_user_ids.clear()
        previous, _ = transition_session(candidate, "presentation")
        details = {
            "bill_id": int(bill.get("id") or 0),
            "bill_number": int(bill.get("bill_number") or 0),
        }
        receipt = self.repository.commit_begin_bill(
            candidate,
            expected_revision=int(session.revision),
            bill_id=int(bill.get("id") or 0),
            event_type="bill_presented",
            actor=actor,
            details={**details, "stage_from": previous},
            deliveries=deliveries,
        )
        persisted = dict(receipt.get("session") or {})
        candidate.revision = int(persisted.get("revision") or candidate.revision + 1)
        self._restore_checkpoint(session, session_to_snapshot(candidate))
        return receipt

    def open_voting(
        self,
        session: LiveConsensusSession,
        *,
        actor: ConsensusActor,
        deliveries: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically open voting and publish all voting controls."""

        validate_consensus_roster(session)
        if session.stage != "presentation" or session.current_bill is None:
            raise ConsensusStateError("Законопроект сейчас нельзя поставить на воут.")
        with self.mutation(session):
            previous, _ = transition_session(session, "voting")
            return self.save_with_deliveries(
                session,
                "bill_voting_opened",
                actor=actor,
                stage_from=previous,
                details={
                    "bill_id": int(session.current_bill.get("id") or 0),
                    "bill_number": int(session.current_bill.get("bill_number") or 0),
                },
                deliveries=deliveries,
            )

    def claim_finalization(
        self,
        session: LiveConsensusSession,
        *,
        kind: str,
        actor: ConsensusActor | None,
        forced: bool = False,
        veto_authorized: bool = False,
        oral_authorized: bool = False,
        action_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        validate_consensus_roster(session)
        if session.stage == "finalizing" and session.pending_action:
            if session.pending_action.get("kind") != kind:
                raise ConsensusStateError("Голосование уже фиксируется другим способом.")
            return dict(session.pending_action)
        allowed_stages = {"voting"}
        if kind == "oral":
            allowed_stages.update({"paused", "discussion_type", "discussion"})
        if session.stage not in allowed_stages or session.current_bill is None:
            raise ConsensusStateError("Текущее голосование уже закрыто.")
        if kind not in {"vote", "veto", "oral"}:
            raise ConsensusStateError("Неизвестный способ завершения голосования.")
        if kind == "veto" and (actor is None or actor.user_id is None or not veto_authorized):
            raise ConsensusStateError("Право вето не подтверждено для этого участника.")
        if kind == "oral" and (actor is None or actor.user_id is None or not oral_authorized):
            raise ConsensusStateError("Устное решение может зафиксировать только уполномоченный председатель.")
        clean_action_details = dict(action_details or {})
        protected_keys = {"kind", "forced", "actor_id", "actor_display", "bill_id", "claimed_at"}
        if protected_keys.intersection(clean_action_details):
            raise ConsensusStateError("Служебные поля фиксации нельзя переопределить.")
        if kind == "oral":
            if str(clean_action_details.get("oral_status") or "") not in {"accepted", "rejected"}:
                raise ConsensusStateError("Для устного решения не указан корректный итог.")
            if len(str(clean_action_details.get("oral_note") or "").strip()) < 3:
                raise ConsensusStateError("Для устного решения необходимо основание.")
        with self.mutation(session):
            session.pending_action = {
                "kind": kind,
                "forced": bool(forced),
                "actor_id": actor.user_id if actor else None,
                "actor_display": actor.display_name if actor else None,
                "bill_id": int(session.current_bill.get("id") or 0),
                "claimed_at": datetime.now(timezone.utc).isoformat(),
                **clean_action_details,
            }
            if kind == "oral":
                session.discussion_channel_id = None
                session.discussion_initiator_id = None
                session.discussion_type = None
                session.discussion_allowed_user_ids.clear()
                session.discussion_note_message_id = None
                for participant in session.participants.values():
                    participant.discussion_message_id = None
            # A timer only belongs to the active voting stage.  Clear its
            # durable representation in the same transaction as the stage
            # transition; the asyncio task is cancelled by the runtime only
            # after this write succeeds.
            session.timer_deadline = None
            session.timer_seconds = None
            session.timer_added_seconds = 0
            session.timer_last_added_seconds = None
            session.timer_last_adjusted_at = None
            event_type = {
                "vote": "vote_finalization_claimed",
                "veto": "veto_claimed",
                "oral": "oral_result_claimed",
            }[kind]
            self.transition(
                session,
                "finalizing",
                event_type,
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
        validate_consensus_roster(session)
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
        session.timer_added_seconds = 0
        session.timer_last_added_seconds = None
        session.timer_last_adjusted_at = None
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
        session.timer_added_seconds = candidate.timer_added_seconds
        session.timer_last_added_seconds = candidate.timer_last_added_seconds
        session.timer_last_adjusted_at = candidate.timer_last_adjusted_at
        session.finished = candidate.finished
        session.revision = candidate.revision
        return receipt

    def request_discussion(self, session: LiveConsensusSession, initiator: LiveParticipant) -> None:
        validate_consensus_roster(session)
        if session.stage != "voting" or session.current_bill is None:
            raise ConsensusStateError("Дискуссию можно начать только во время голосования.")
        if not initiator.confirmed:
            raise ConsensusStateError(
                "Дискуссию может инициировать только зарегистрированный участник."
            )
        if session.discussion_initiator_id is not None:
            raise ConsensusStateError("Дискуссия по этому проекту уже инициирована.")
        with self.mutation(session):
            session.previous_stage = "voting"
            session.discussion_initiator_id = initiator.user_id
            # Do not leave a restorable voting timer attached to the
            # discussion-selection stage.  This mutation is rolled back with
            # the stage when persistence fails.
            session.timer_deadline = None
            session.timer_seconds = None
            session.timer_added_seconds = 0
            session.timer_last_added_seconds = None
            session.timer_last_adjusted_at = None
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
        deliveries: Iterable[dict[str, Any]] = (),
    ) -> None:
        validate_consensus_roster(session)
        if session.stage != "discussion_type":
            raise ConsensusStateError("Тип дискуссии сейчас выбрать нельзя.")
        clean_type = str(discussion_type).strip()[:80]
        if not clean_type:
            raise ConsensusStateError("Укажите тип дискуссии.")
        clean_allowed = {int(user_id) for user_id in allowed_user_ids}
        confirmed_ids = {item.user_id for item in session.confirmed_participants()}
        if not clean_allowed.issubset(confirmed_ids):
            raise ConsensusStateError(
                "В дискуссию нельзя добавить участника вне подтверждённого состава."
            )
        delivery_jobs = tuple(deliveries)
        with self.mutation(session):
            session.discussion_type = clean_type
            session.discussion_channel_id = int(channel_id) if channel_id else None
            session.discussion_allowed_user_ids = clean_allowed
            previous, _ = transition_session(session, "discussion")
            details = {
                "type": session.discussion_type,
                "channel_id": session.discussion_channel_id,
            }
            if delivery_jobs:
                self.save_with_deliveries(
                    session,
                    "discussion_started",
                    stage_from=previous,
                    details=details,
                    deliveries=delivery_jobs,
                )
            else:
                self.save(
                    session,
                    "discussion_started",
                    stage_from=previous,
                    details=details,
                )

    def end_discussion(self, session: LiveConsensusSession, *, actor: ConsensusActor) -> None:
        validate_consensus_roster(session)
        if session.stage not in {"discussion_type", "discussion"}:
            raise ConsensusStateError("Активной дискуссии сейчас нет.")
        with self.mutation(session):
            session.discussion_channel_id = None
            session.discussion_type = None
            session.discussion_initiator_id = None
            session.discussion_allowed_user_ids.clear()
            session.discussion_note_message_id = None
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
        if session.stage not in {
            "presentation",
            "voting",
            "discussion_type",
            "discussion",
            "after_result",
        }:
            raise ConsensusStateError("На текущем этапе консенсус нельзя поставить на паузу.")
        with self.mutation(session):
            session.previous_stage = session.stage
            session.paused_reason = str(reason).strip()[:1000]
            session.pause_is_automatic = bool(automatic)
            # Pausing and stopping the durable timer are one state change.
            # The runtime task must remain alive until this save succeeds so
            # a database outage cannot silently lose the deadline.
            session.timer_deadline = None
            session.timer_seconds = None
            session.timer_added_seconds = 0
            session.timer_last_added_seconds = None
            session.timer_last_adjusted_at = None
            self.transition(
                session,
                "paused",
                "session_paused_automatically" if automatic else "session_paused",
                actor=actor,
                details={"reason": session.paused_reason},
            )

    def resume(self, session: LiveConsensusSession, *, actor: ConsensusActor) -> str:
        validate_consensus_roster(session)
        if session.stage != "paused":
            raise ConsensusStateError("Консенсус не находится на паузе.")
        target = str(session.previous_stage or "")
        allowed_targets = {
            "presentation",
            "voting",
            "discussion_type",
            "discussion",
            "after_result",
        }
        if target not in allowed_targets:
            raise ConsensusStateError(
                "Не удалось определить этап для продолжения. Откройте аварийное восстановление."
            )
        if target in {
            "presentation",
            "voting",
            "discussion_type",
            "discussion",
        } and session.current_bill is None:
            raise ConsensusStateError(
                "Нельзя возобновить голосование: текущий законопроект отсутствует."
            )
        if target == "after_result" and session.current_bill is not None:
            raise ConsensusStateError(
                "Нельзя возобновить межпроектный этап: найден незавершённый законопроект."
            )
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
            session.timer_added_seconds = 0
            session.timer_last_added_seconds = None
            session.timer_last_adjusted_at = None
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
        reason: str | None = None,
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
        candidate.timer_added_seconds = 0
        candidate.timer_last_added_seconds = None
        candidate.timer_last_adjusted_at = None
        target = "cancelled" if cancelled else "finished"
        transition_session(candidate, target)
        details = {
            "result_count": len(candidate.results),
            "requeued_bill_id": current_bill_id,
            "reason": str(reason or "").strip()[:1000] or None,
            "administrative": bool(actor.user_id != session.leader_id),
        }
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

    def transfer_leadership(
        self,
        session: LiveConsensusSession,
        *,
        new_leader_id: int,
        new_leader_display: str,
        actor: ConsensusActor,
    ) -> bool:
        """Transfer only operational leadership; the participant roster is immutable.

        A takeover is deliberately limited to an already confirmed chair.  This
        prevents an administrative recovery action from silently changing the
        quorum or adding a new vote to a running consensus.
        """

        if session.finished:
            raise ConsensusStateError("Завершённому консенсусу нельзя сменить ведущего.")
        participant = session.participants.get(int(new_leader_id))
        if participant is None or not participant.confirmed or participant.kind != "chair":
            raise ConsensusStateError(
                "Новым ведущим может стать только подтверждённый председатель из состава заседания."
            )
        if int(session.leader_id) == int(new_leader_id):
            return False
        previous_id = int(session.leader_id)
        previous_display = str(session.leader_display)
        with self.mutation(session):
            session.leader_id = int(new_leader_id)
            session.leader_display = str(new_leader_display).strip()[:200] or str(new_leader_id)
            session.host_message_id = None
            session.host_message_obj = None
            self.save(
                session,
                "leadership_transferred",
                actor=actor,
                details={
                    "previous_leader_id": previous_id,
                    "previous_leader_display": previous_display,
                    "new_leader_id": int(new_leader_id),
                    "new_leader_display": session.leader_display,
                    "stage": str(session.stage),
                },
            )
        return True

    @staticmethod
    def calculate(session: LiveConsensusSession) -> dict[str, Any]:
        return calculate_consensus(session)

    @staticmethod
    def snapshot(session: LiveConsensusSession) -> dict[str, Any]:
        return session_to_snapshot(session)
