"""Deterministic recovery plan for stored and live consensus failures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from modules.consensus_core import LiveConsensusSession
from modules.consensus_health import ConsensusHealthReport


RecoveryPriority = Literal["info", "warning", "critical"]


@dataclass(frozen=True, slots=True)
class ConsensusOperationalState:
    leader_present: bool | None = None
    leader_in_voice: bool | None = None
    voice_channel_available: bool | None = None
    quorum_ready: bool | None = None
    quorum_reason: str | None = None
    discussion_channel_available: bool | None = None
    timer_task_running: bool | None = None
    public_channel_available: bool | None = None
    host_control_bound: bool | None = None
    missing_member_ids: tuple[int, ...] = ()
    missing_control_user_ids: tuple[int, ...] = ()
    delivery_counts: tuple[tuple[str, int], ...] = ()
    dead_delivery_errors: tuple[str, ...] = ()

    def delivery_count(self, status: str) -> int:
        return sum(count for name, count in self.delivery_counts if name == str(status))


@dataclass(frozen=True, slots=True)
class ConsensusRecoveryAction:
    code: str
    priority: RecoveryPriority
    automatic: bool
    message: str


@dataclass(frozen=True, slots=True)
class ConsensusRecoveryPlan:
    actions: tuple[ConsensusRecoveryAction, ...]

    @property
    def automatic(self) -> tuple[ConsensusRecoveryAction, ...]:
        return tuple(item for item in self.actions if item.automatic)

    @property
    def manual(self) -> tuple[ConsensusRecoveryAction, ...]:
        return tuple(item for item in self.actions if not item.automatic)

    @property
    def critical(self) -> tuple[ConsensusRecoveryAction, ...]:
        return tuple(item for item in self.actions if item.priority == "critical")


_SAFE_REPAIR_CODES = frozenset(
    {
        "timer_outside_voting",
        "partial_timer_state",
        "stale_votes",
        "stale_discussion_state",
        "orphan_pending_action",
        "stale_pause_state",
    }
)


def build_consensus_recovery_plan(
    session: LiveConsensusSession,
    health: ConsensusHealthReport,
    operational: ConsensusOperationalState | None = None,
) -> ConsensusRecoveryPlan:
    """Map every observable failure to an explicit safe response."""

    live = operational or ConsensusOperationalState()
    actions: list[ConsensusRecoveryAction] = []

    def add(code: str, priority: RecoveryPriority, automatic: bool, message: str) -> None:
        if not any(item.code == code for item in actions):
            actions.append(ConsensusRecoveryAction(code, priority, automatic, message))

    critical_codes = {item.code for item in health.critical}
    warning_codes = {item.code for item in health.warnings}
    if critical_codes:
        add(
            "manual_integrity_review",
            "critical",
            False,
            "Не продолжать спорную транзакцию автоматически; председателю сверить состояние и журнал.",
        )

    safe_repair_codes = warning_codes.intersection(_SAFE_REPAIR_CODES)
    if (
        "orphan_pending_action" in safe_repair_codes
        and session.stage not in {"registration", "presentation", "after_result"}
    ):
        safe_repair_codes.remove("orphan_pending_action")
    if safe_repair_codes:
        add(
            "repair_safe_invariants",
            "warning",
            True,
            "Удалить однозначно устаревшие таймеры, голоса и служебные поля одной журналируемой записью.",
        )

    if session.stage == "finalizing" and not critical_codes:
        add(
            "retry_finalization",
            "warning",
            True,
            "Повторить атомарную фиксацию сохранённого решения без создания второго результата.",
        )

    if session.stage == "voting" and session.timer_deadline is not None and live.timer_task_running is False:
        add(
            "restore_timer",
            "warning",
            True,
            "Восстановить фоновый таймер из сохранённого срока.",
        )

    quorum_stage = session.stage in {
        "presentation",
        "voting",
        "discussion_type",
        "discussion",
        "paused",
    }
    if quorum_stage and (
        live.voice_channel_available is False or live.quorum_ready is False
    ):
        add(
            "pause_for_quorum",
            "critical",
            True,
            "Остановить активное голосование до возвращения голосового кворума.",
        )
    elif session.stage == "paused" and session.pause_is_automatic and live.quorum_ready is True:
        add(
            "resume_after_quorum",
            "info",
            True,
            "Возобновить автоматически приостановленный этап после восстановления кворума.",
        )

    if live.leader_present is False:
        add(
            "replace_missing_leader",
            "critical",
            False,
            "Передать ведение подтверждённому председателю или безопасно закрыть заседание.",
        )
    elif live.leader_in_voice is False and quorum_stage:
        add(
            "leader_return_or_takeover",
            "critical",
            False,
            "Ведущему вернуться в голосовой канал либо передать ведение присутствующему председателю.",
        )

    if session.stage == "discussion" and live.discussion_channel_available is False:
        add(
            "end_missing_discussion",
            "warning",
            False,
            "Административно завершить дискуссию с исчезнувшим каналом и заново проверить кворум.",
        )

    if live.public_channel_available is False:
        add(
            "restore_public_channel",
            "critical",
            False,
            "Восстановить канал заседания или безопасно закрыть сессию; публичную карточку разместить негде.",
        )

    if live.host_control_bound is False:
        add(
            "reopen_host_controls",
            "info",
            False,
            "Ведущему заново открыть `/tvrs`; состояние хранится в базе, старое меню не требуется.",
        )

    if live.missing_member_ids:
        add(
            "missing_roster_members",
            "warning",
            False,
            "Не менять замороженный состав: восстановить доступ участников либо завершить решение предусмотренным способом.",
        )

    if live.missing_control_user_ids:
        add(
            "rebuild_missing_controls",
            "warning",
            True,
            "Пересоздать отсутствующие личные панели только для затронутых участников.",
        )

    if live.delivery_count("dead"):
        add(
            "requeue_dead_deliveries",
            "warning",
            True,
            "Повторить только актуальные мёртвые уведомления; личные панели пересоздать новой защищённой версией.",
        )

    if "dm_delivery_failures" in warning_codes:
        add(
            "rebuild_private_controls",
            "warning",
            True,
            "Пересоздать личные панели, сохранив публичную карточку резервным способом голосования.",
        )

    if not actions:
        add(
            "monitor",
            "info",
            True,
            "Состояние согласовано; продолжать обычный контроль таймера, кворума и доставок.",
        )
    return ConsensusRecoveryPlan(tuple(actions))


__all__ = [
    "ConsensusOperationalState",
    "ConsensusRecoveryAction",
    "ConsensusRecoveryPlan",
    "build_consensus_recovery_plan",
]
