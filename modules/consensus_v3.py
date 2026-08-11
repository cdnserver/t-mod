"""Product contract for the third consensus interface.

This module is intentionally pure: it knows nothing about Discord, SQLite or
delivery transports.  Both the production portal and the simulator use these
decisions, so access labels and preflight rules cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from modules.consensus_core import (
    CURRENT_CONSENSUS_ENGINE_VERSION,
    LEGACY_CONSENSUS_ENGINE_VERSION,
    ConsensusRules,
    LiveConsensusSession,
    LiveParticipant,
)


CONSENSUS_ENGINE_VERSION = CURRENT_CONSENSUS_ENGINE_VERSION

ConsensusEntryAction = Literal[
    "prepare",
    "manage",
    "participate",
    "observe",
    "unavailable",
]


@dataclass(frozen=True, slots=True)
class ConsensusAccess:
    primary_action: ConsensusEntryAction
    primary_label: str
    can_prepare: bool
    can_manage: bool
    can_participate: bool
    can_observe: bool
    active_leader_id: int | None = None


def resolve_consensus_access(
    session: LiveConsensusSession | None,
    *,
    user_id: int,
    has_chair_access: bool,
) -> ConsensusAccess:
    """Resolve one unambiguous primary action for the consensus entry point."""

    if session is None or session.finished:
        if has_chair_access:
            return ConsensusAccess(
                primary_action="prepare",
                primary_label="Проверить готовность",
                can_prepare=True,
                can_manage=False,
                can_participate=False,
                can_observe=False,
            )
        return ConsensusAccess(
            primary_action="unavailable",
            primary_label="Нет активного заседания",
            can_prepare=False,
            can_manage=False,
            can_participate=False,
            can_observe=False,
        )

    is_leader = int(user_id) == int(session.leader_id)
    is_participant = int(user_id) in session.participants
    if is_leader:
        action: ConsensusEntryAction = "manage"
        label = "Управлять заседанием"
    elif is_participant:
        action = "participate"
        label = "Открыть личный пульт"
    else:
        action = "observe"
        label = "Наблюдать за заседанием"
    return ConsensusAccess(
        primary_action=action,
        primary_label=label,
        can_prepare=False,
        can_manage=is_leader,
        can_participate=is_participant,
        can_observe=True,
        active_leader_id=int(session.leader_id),
    )


@dataclass(frozen=True, slots=True)
class ConsensusPreflight:
    queue_count: int
    chair_count: int
    senator_count: int
    leader_present: bool
    voice_error: str | None
    rules: ConsensusRules

    @property
    def quorum_ready(self) -> bool:
        if self.rules.version >= 3:
            return (
                self.chair_count + self.senator_count
                >= self.rules.minimum_participants
            )
        enough_chairs = self.chair_count >= self.rules.minimum_chairs
        enough_senators = self.senator_count >= self.rules.minimum_senators
        valid_parity = (
            not self.rules.senators_must_be_odd
            or self.senator_count % 2 == 1
        )
        return enough_chairs and enough_senators and valid_parity

    @property
    def blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if self.queue_count <= 0:
            blockers.append("В очереди нет законопроектов.")
        if self.voice_error:
            blockers.append(str(self.voice_error))
        elif not self.quorum_ready:
            blockers.append("Состав голосового канала не образует кворум.")
        if not self.leader_present:
            blockers.append("Будущий ведущий не находится в голосовом канале.")
        return tuple(dict.fromkeys(blockers))

    @property
    def can_open_registration(self) -> bool:
        return not self.blockers


def evaluate_consensus_preflight(
    participants: Iterable[LiveParticipant],
    *,
    queue_count: int,
    leader_id: int,
    rules: ConsensusRules,
    voice_error: str | None = None,
) -> ConsensusPreflight:
    items = list(participants)
    return ConsensusPreflight(
        queue_count=max(0, int(queue_count)),
        chair_count=sum(1 for item in items if item.kind == "chair"),
        senator_count=sum(1 for item in items if item.kind == "senator"),
        leader_present=int(leader_id) in {int(item.user_id) for item in items},
        voice_error=str(voice_error) if voice_error else None,
        rules=rules,
    )


def consensus_step_index(stage: str) -> int:
    return {
        "registration": 1,
        "presentation": 2,
        "voting": 2,
        "discussion_type": 2,
        "discussion": 2,
        "paused": 2,
        "finalizing": 3,
        "after_result": 3,
        "finished": 4,
        "cancelled": 4,
    }.get(str(stage), 0)


def consensus_progress_text(stage: str) -> str:
    current = consensus_step_index(stage)
    labels = ("Регистрация", "Рассмотрение", "Результат", "Протокол")
    return "  →  ".join(
        f"**{index}. {label}**" if index == current else f"{index}. {label}"
        for index, label in enumerate(labels, start=1)
    )


__all__ = [
    "CONSENSUS_ENGINE_VERSION",
    "LEGACY_CONSENSUS_ENGINE_VERSION",
    "ConsensusAccess",
    "ConsensusPreflight",
    "consensus_progress_text",
    "consensus_step_index",
    "evaluate_consensus_preflight",
    "resolve_consensus_access",
]
