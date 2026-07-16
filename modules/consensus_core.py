from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


CONSENSUS_SNAPSHOT_VERSION = 2
CONSENSUS_ACTIVE_STAGES = frozenset(
    {
        "registration",
        "voting",
        "finalizing",
        "discussion_type",
        "discussion",
        "paused",
        "after_result",
    }
)
CONSENSUS_TERMINAL_STAGES = frozenset({"finished", "cancelled"})
CONSENSUS_STAGE_LABELS = {
    "registration": "регистрация",
    "voting": "голосование",
    "finalizing": "фиксация результата",
    "discussion_type": "выбор типа дискуссии",
    "discussion": "дискуссия",
    "paused": "пауза",
    "after_result": "итог проекта",
    "finished": "завершён",
    "cancelled": "отменён",
}
CONSENSUS_TRANSITIONS = {
    "registration": frozenset({"voting", "cancelled", "finished"}),
    "voting": frozenset({"discussion_type", "paused", "finalizing", "cancelled", "finished"}),
    "finalizing": frozenset({"after_result", "cancelled"}),
    "discussion_type": frozenset({"discussion", "voting", "paused", "cancelled", "finished"}),
    "discussion": frozenset({"voting", "paused", "cancelled", "finished"}),
    "paused": frozenset({"voting", "discussion_type", "discussion", "after_result", "cancelled", "finished"}),
    "after_result": frozenset({"voting", "paused", "finished", "cancelled"}),
    "finished": frozenset(),
    "cancelled": frozenset(),
}


class ConsensusStateError(ValueError):
    pass


@dataclass(frozen=True)
class ConsensusRules:
    version: int = 1
    minimum_chairs: int = 2
    minimum_senators: int = 1
    senators_must_be_odd: bool = True
    internal_activation_strictly_above: float = 51.0
    chair_yes_weight: float = 49.0
    internal_consensus_weight: float = 2.0
    acceptance_percent: float = 51.0

    def __post_init__(self) -> None:
        if self.version < 1 or self.minimum_chairs < 0 or self.minimum_senators < 0:
            raise ConsensusStateError("Некорректная версия или состав кворума.")
        if not 0.0 <= self.internal_activation_strictly_above <= 100.0:
            raise ConsensusStateError("Некорректный порог внутреннего консенсуса.")
        if self.chair_yes_weight < 0.0 or self.internal_consensus_weight < 0.0:
            raise ConsensusStateError("Вес голоса не может быть отрицательным.")
        if not 0.0 <= self.acceptance_percent <= 100.0:
            raise ConsensusStateError("Некорректный порог принятия решения.")


DEFAULT_CONSENSUS_RULES = ConsensusRules()


@dataclass
class LiveParticipant:
    user_id: int
    display_name: str
    mention: str
    kind: Literal["chair", "senator"]
    permanent: bool = False
    confirmed: bool = False
    dm_message_id: int | None = None
    dm_failed: bool = False
    vote_message_id: int | None = None
    discussion_message_id: int | None = None


@dataclass
class LiveResult:
    bill_id: int
    bill_number: int
    title: str
    status: str
    internal_percent: float
    overall_percent: float
    internal_active: bool
    votes: dict[int, str]
    veto_by_id: int | None = None
    retry_bill_number: int | None = None


@dataclass
class LiveConsensusSession:
    session_key: str
    guild_id: int
    channel_id: int
    leader_id: int
    leader_display: str
    plenary_number: int
    participants: dict[int, LiveParticipant]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    host_message_id: int | None = None
    host_message_obj: object | None = None
    stage: str = "registration"
    current_bill: dict[str, Any] | None = None
    votes: dict[int, str] = field(default_factory=dict)
    results: list[LiveResult] = field(default_factory=list)
    finished: bool = False
    timer_task: asyncio.Task[Any] | None = None
    timer_deadline: datetime | None = None
    timer_seconds: int | None = None
    previous_stage: str | None = None
    paused_reason: str | None = None
    pause_is_automatic: bool = False
    discussion_channel_id: int | None = None
    discussion_initiator_id: int | None = None
    discussion_type: str | None = None
    discussion_allowed_user_ids: set[int] = field(default_factory=set)
    discussion_note_message_id: int | None = None
    pending_action: dict[str, Any] | None = None
    revision: int = 0
    rules: ConsensusRules = field(default_factory=ConsensusRules)

    def confirmed_participants(self) -> list[LiveParticipant]:
        return [participant for participant in self.participants.values() if participant.confirmed]

    def confirmed_chairs(self) -> list[LiveParticipant]:
        return [participant for participant in self.confirmed_participants() if participant.kind == "chair"]

    def confirmed_senators(self) -> list[LiveParticipant]:
        return [participant for participant in self.confirmed_participants() if participant.kind == "senator"]

    def quorum_ready(self) -> bool:
        chairs = len(self.confirmed_chairs())
        senators = len(self.confirmed_senators())
        enough_chairs = chairs >= self.rules.minimum_chairs
        enough_senators = senators >= self.rules.minimum_senators
        valid_parity = not self.rules.senators_must_be_odd or senators % 2 == 1
        return enough_chairs and enough_senators and valid_parity

    def all_voted(self) -> bool:
        participant_ids = {participant.user_id for participant in self.confirmed_participants()}
        return bool(participant_ids) and participant_ids.issubset(self.votes)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def clean_stage_name(stage: str) -> str:
    return CONSENSUS_STAGE_LABELS.get(str(stage), str(stage))


def transition_session(session: LiveConsensusSession, target_stage: str) -> tuple[str, str]:
    current = str(session.stage)
    target = str(target_stage)
    if current == target:
        return current, target
    if current not in CONSENSUS_TRANSITIONS:
        raise ConsensusStateError(f"Неизвестный этап консенсуса: {current}")
    if target not in CONSENSUS_TRANSITIONS[current]:
        raise ConsensusStateError(f"Недопустимый переход консенсуса: {current} → {target}")
    session.stage = target
    session.finished = target in CONSENSUS_TERMINAL_STAGES
    return current, target


def calculate_consensus(session: LiveConsensusSession) -> dict[str, Any]:
    senators = [participant for participant in session.confirmed_participants() if participant.kind == "senator"]
    chairs = [participant for participant in session.confirmed_participants() if participant.kind == "chair"]
    senator_votes = [session.votes[participant.user_id] for participant in senators if participant.user_id in session.votes]
    senator_yes = sum(1 for vote in senator_votes if vote == "yes")
    internal_percent = (senator_yes / len(senator_votes) * 100.0) if senator_votes else 0.0
    rules = session.rules
    internal_active = internal_percent > rules.internal_activation_strictly_above
    chair_yes = sum(1 for participant in chairs if session.votes.get(participant.user_id) == "yes")
    overall = min(
        100.0,
        chair_yes * rules.chair_yes_weight + (rules.internal_consensus_weight if internal_active else 0.0),
    )
    return {
        "senators": senators,
        "chairs": chairs,
        "senator_votes": senator_votes,
        "senator_yes": senator_yes,
        "internal_percent": round(internal_percent, 2),
        "internal_active": internal_active,
        "chair_yes": chair_yes,
        "overall_percent": round(overall, 2),
        "accepted": overall >= rules.acceptance_percent,
        "rules_version": rules.version,
        "acceptance_percent": rules.acceptance_percent,
    }


def _participant_payload(participant: LiveParticipant) -> dict[str, Any]:
    return {
        "user_id": int(participant.user_id),
        "display_name": str(participant.display_name),
        "mention": str(participant.mention),
        "kind": str(participant.kind),
        "permanent": bool(participant.permanent),
        "confirmed": bool(participant.confirmed),
        "dm_message_id": participant.dm_message_id,
        "dm_failed": bool(participant.dm_failed),
        "vote_message_id": participant.vote_message_id,
        "discussion_message_id": participant.discussion_message_id,
    }


def _result_payload(result: LiveResult) -> dict[str, Any]:
    return {
        "bill_id": int(result.bill_id),
        "bill_number": int(result.bill_number),
        "title": str(result.title),
        "status": str(result.status),
        "internal_percent": float(result.internal_percent),
        "overall_percent": float(result.overall_percent),
        "internal_active": bool(result.internal_active),
        "votes": {str(user_id): str(vote) for user_id, vote in result.votes.items()},
        "veto_by_id": result.veto_by_id,
        "retry_bill_number": result.retry_bill_number,
    }


def session_to_snapshot(session: LiveConsensusSession) -> dict[str, Any]:
    return {
        "snapshot_version": CONSENSUS_SNAPSHOT_VERSION,
        "session_key": str(session.session_key),
        "guild_id": int(session.guild_id),
        "channel_id": int(session.channel_id),
        "leader_id": int(session.leader_id),
        "leader_display": str(session.leader_display),
        "plenary_number": int(session.plenary_number),
        "created_at": session.created_at.astimezone(timezone.utc).isoformat(),
        "host_message_id": session.host_message_id,
        "stage": str(session.stage),
        "current_bill": dict(session.current_bill) if session.current_bill else None,
        "votes": {str(user_id): str(vote) for user_id, vote in session.votes.items()},
        "results": [_result_payload(result) for result in session.results],
        "finished": bool(session.finished),
        "timer_deadline": session.timer_deadline.astimezone(timezone.utc).isoformat() if session.timer_deadline else None,
        "timer_seconds": session.timer_seconds,
        "previous_stage": session.previous_stage,
        "paused_reason": session.paused_reason,
        "pause_is_automatic": bool(session.pause_is_automatic),
        "discussion_channel_id": session.discussion_channel_id,
        "discussion_initiator_id": session.discussion_initiator_id,
        "discussion_type": session.discussion_type,
        "discussion_allowed_user_ids": sorted(int(user_id) for user_id in session.discussion_allowed_user_ids),
        "discussion_note_message_id": session.discussion_note_message_id,
        "pending_action": dict(session.pending_action) if session.pending_action else None,
        "revision": int(session.revision),
        "rules": {
            "version": int(session.rules.version),
            "minimum_chairs": int(session.rules.minimum_chairs),
            "minimum_senators": int(session.rules.minimum_senators),
            "senators_must_be_odd": bool(session.rules.senators_must_be_odd),
            "internal_activation_strictly_above": float(session.rules.internal_activation_strictly_above),
            "chair_yes_weight": float(session.rules.chair_yes_weight),
            "internal_consensus_weight": float(session.rules.internal_consensus_weight),
            "acceptance_percent": float(session.rules.acceptance_percent),
        },
        "participants": [_participant_payload(participant) for participant in session.participants.values()],
    }


def session_from_snapshot(snapshot: dict[str, Any]) -> LiveConsensusSession:
    version = int(snapshot.get("snapshot_version") or 0)
    if version != CONSENSUS_SNAPSHOT_VERSION:
        raise ConsensusStateError(f"Неподдерживаемая версия снимка консенсуса: {version}")
    stage = str(snapshot.get("stage") or "")
    if stage not in CONSENSUS_TRANSITIONS:
        raise ConsensusStateError(f"Неизвестный этап в снимке консенсуса: {stage}")
    snapshot_finished = bool(snapshot.get("finished"))
    if snapshot_finished and stage not in CONSENSUS_TERMINAL_STAGES:
        raise ConsensusStateError("Активная сессия ошибочно помечена завершённой.")
    current_bill = snapshot.get("current_bill")
    stages_requiring_bill = {"voting", "finalizing", "discussion_type", "discussion"}
    if stage in stages_requiring_bill and not isinstance(current_bill, dict):
        raise ConsensusStateError(f"Для этапа {stage} отсутствует текущий законопроект.")
    pending_action = snapshot.get("pending_action")
    if stage == "finalizing" and (
        not isinstance(pending_action, dict) or pending_action.get("kind") not in {"vote", "veto"}
    ):
        raise ConsensusStateError("Для фиксации результата отсутствует сохранённое действие.")
    participants: dict[int, LiveParticipant] = {}
    for raw in snapshot.get("participants") or []:
        user_id = int(raw["user_id"])
        kind = str(raw.get("kind") or "")
        if kind not in {"chair", "senator"}:
            raise ConsensusStateError(f"Неизвестная роль участника: {kind}")
        participants[user_id] = LiveParticipant(
            user_id=user_id,
            display_name=str(raw.get("display_name") or user_id),
            mention=str(raw.get("mention") or f"<@{user_id}>"),
            kind=kind,  # type: ignore[arg-type]
            permanent=bool(raw.get("permanent")),
            confirmed=bool(raw.get("confirmed")),
            dm_message_id=int(raw["dm_message_id"]) if raw.get("dm_message_id") else None,
            dm_failed=bool(raw.get("dm_failed")),
            vote_message_id=int(raw["vote_message_id"]) if raw.get("vote_message_id") else None,
            discussion_message_id=int(raw["discussion_message_id"]) if raw.get("discussion_message_id") else None,
        )
    results = [
        LiveResult(
            bill_id=int(raw["bill_id"]),
            bill_number=int(raw["bill_number"]),
            title=str(raw.get("title") or ""),
            status=str(raw.get("status") or ""),
            internal_percent=float(raw.get("internal_percent") or 0.0),
            overall_percent=float(raw.get("overall_percent") or 0.0),
            internal_active=bool(raw.get("internal_active")),
            votes={int(user_id): str(vote) for user_id, vote in (raw.get("votes") or {}).items()},
            veto_by_id=int(raw["veto_by_id"]) if raw.get("veto_by_id") else None,
            retry_bill_number=int(raw["retry_bill_number"]) if raw.get("retry_bill_number") else None,
        )
        for raw in (snapshot.get("results") or [])
    ]
    created_at = parse_datetime(snapshot.get("created_at")) or utc_now()
    raw_rules = snapshot.get("rules") if isinstance(snapshot.get("rules"), dict) else {}
    rules = ConsensusRules(
        version=int(raw_rules.get("version", DEFAULT_CONSENSUS_RULES.version)),
        minimum_chairs=int(raw_rules.get("minimum_chairs", DEFAULT_CONSENSUS_RULES.minimum_chairs)),
        minimum_senators=int(raw_rules.get("minimum_senators", DEFAULT_CONSENSUS_RULES.minimum_senators)),
        senators_must_be_odd=bool(raw_rules.get("senators_must_be_odd", DEFAULT_CONSENSUS_RULES.senators_must_be_odd)),
        internal_activation_strictly_above=float(
            raw_rules.get(
                "internal_activation_strictly_above",
                DEFAULT_CONSENSUS_RULES.internal_activation_strictly_above,
            )
        ),
        chair_yes_weight=float(raw_rules.get("chair_yes_weight", DEFAULT_CONSENSUS_RULES.chair_yes_weight)),
        internal_consensus_weight=float(
            raw_rules.get("internal_consensus_weight", DEFAULT_CONSENSUS_RULES.internal_consensus_weight)
        ),
        acceptance_percent=float(raw_rules.get("acceptance_percent", DEFAULT_CONSENSUS_RULES.acceptance_percent)),
    )
    return LiveConsensusSession(
        session_key=str(snapshot["session_key"]),
        guild_id=int(snapshot["guild_id"]),
        channel_id=int(snapshot["channel_id"]),
        leader_id=int(snapshot["leader_id"]),
        leader_display=str(snapshot.get("leader_display") or snapshot["leader_id"]),
        plenary_number=int(snapshot["plenary_number"]),
        participants=participants,
        created_at=created_at,
        host_message_id=int(snapshot["host_message_id"]) if snapshot.get("host_message_id") else None,
        stage=stage,
        current_bill=dict(current_bill) if isinstance(current_bill, dict) else None,
        votes={int(user_id): str(vote) for user_id, vote in (snapshot.get("votes") or {}).items()},
        results=results,
        finished=snapshot_finished or stage in CONSENSUS_TERMINAL_STAGES,
        timer_deadline=parse_datetime(snapshot.get("timer_deadline")),
        timer_seconds=int(snapshot["timer_seconds"]) if snapshot.get("timer_seconds") else None,
        previous_stage=str(snapshot["previous_stage"]) if snapshot.get("previous_stage") else None,
        paused_reason=str(snapshot["paused_reason"]) if snapshot.get("paused_reason") else None,
        pause_is_automatic=bool(snapshot.get("pause_is_automatic")),
        discussion_channel_id=int(snapshot["discussion_channel_id"]) if snapshot.get("discussion_channel_id") else None,
        discussion_initiator_id=int(snapshot["discussion_initiator_id"]) if snapshot.get("discussion_initiator_id") else None,
        discussion_type=str(snapshot["discussion_type"]) if snapshot.get("discussion_type") else None,
        discussion_allowed_user_ids={int(user_id) for user_id in (snapshot.get("discussion_allowed_user_ids") or [])},
        discussion_note_message_id=int(snapshot["discussion_note_message_id"]) if snapshot.get("discussion_note_message_id") else None,
        pending_action=dict(pending_action) if isinstance(pending_action, dict) else None,
        revision=int(snapshot.get("revision") or 0),
        rules=rules,
    )
