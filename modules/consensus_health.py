"""Pure lifecycle diagnostics for a live consensus session.

The watchdog, recovery UI and tests consume the same report.  Keeping this
module free of Discord and SQLite makes every diagnosis deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from modules.consensus_core import (
    CONSENSUS_ACTIVE_STAGES,
    CURRENT_CONSENSUS_ENGINE_VERSION,
    LiveConsensusSession,
)


HealthSeverity = Literal["info", "warning", "critical"]


@dataclass(frozen=True, slots=True)
class ConsensusHealthIssue:
    code: str
    severity: HealthSeverity
    message: str
    recovery: str


@dataclass(frozen=True, slots=True)
class ConsensusHealthReport:
    issues: tuple[ConsensusHealthIssue, ...]

    @property
    def critical(self) -> tuple[ConsensusHealthIssue, ...]:
        return tuple(item for item in self.issues if item.severity == "critical")

    @property
    def warnings(self) -> tuple[ConsensusHealthIssue, ...]:
        return tuple(item for item in self.issues if item.severity == "warning")

    @property
    def healthy(self) -> bool:
        return not self.critical


def assess_consensus_health(session: LiveConsensusSession) -> ConsensusHealthReport:
    issues: list[ConsensusHealthIssue] = []

    def add(code: str, severity: HealthSeverity, message: str, recovery: str) -> None:
        issues.append(ConsensusHealthIssue(code, severity, message, recovery))

    if session.finished or session.stage not in CONSENSUS_ACTIVE_STAGES:
        add(
            "terminal_in_active_registry",
            "critical",
            "Завершённая или неизвестная сессия осталась среди активных.",
            "Удалить сессию из оперативного реестра и восстановить публичную карточку.",
        )
    if session.revision < 1:
        add(
            "missing_revision",
            "critical",
            "У сессии отсутствует корректная версия состояния.",
            "Перезагрузить состояние из SQLite; при невозможности поместить снимок в карантин.",
        )
    if session.engine_version < CURRENT_CONSENSUS_ENGINE_VERSION:
        add(
            "legacy_engine",
            "warning",
            "Сессия создана предыдущей версией движка.",
            "Продолжить через совместимое восстановление и не менять правила до завершения.",
        )

    leader = session.participants.get(int(session.leader_id))
    if leader is None or leader.kind != "chair":
        add(
            "leader_outside_roster",
            "critical",
            "Ведущий отсутствует среди председателей состава.",
            "Передать ведение подтверждённому председателю либо закрыть сессию административно.",
        )
    elif not leader.confirmed:
        add(
            "leader_unconfirmed",
            "critical",
            "Ведущий не подтверждён в составе заседания.",
            "Подтвердить ведущего или передать ведение подтверждённому председателю.",
        )

    needs_bill = session.stage in {"voting", "finalizing", "discussion_type", "discussion"}
    if needs_bill and session.current_bill is None:
        add(
            "bill_missing",
            "critical",
            "Текущий этап требует законопроект, но он отсутствует.",
            "Восстановить снимок из SQLite; при повреждении закрыть сессию без изменения проекта.",
        )
    if session.stage in {"registration", "after_result"} and session.current_bill is not None:
        add(
            "bill_leaked_between_stages",
            "critical",
            "Законопроект привязан к этапу, на котором его быть не должно.",
            "Не продолжать автоматически; сверить статус проекта и журнал транзакций.",
        )

    confirmed_ids = {item.user_id for item in session.confirmed_participants()}
    unknown_votes = set(session.votes) - confirmed_ids
    invalid_votes = {user_id for user_id, vote in session.votes.items() if vote not in {"yes", "no"}}
    if unknown_votes:
        add(
            "votes_outside_roster",
            "critical",
            "Найдены голоса пользователей вне подтверждённого состава.",
            "Остановить автоматическую фиксацию и сверить журнал голосов.",
        )
    if invalid_votes:
        add(
            "invalid_vote_value",
            "critical",
            "Найдены голоса с неизвестным значением.",
            "Остановить автоматическую фиксацию и восстановить корректный снимок.",
        )
    if session.stage in {"registration", "after_result"} and session.votes:
        add(
            "stale_votes",
            "warning",
            "Между проектами сохранились голоса предыдущего этапа.",
            "Очистить голоса через безопасное восстановление состояния.",
        )

    result_ids = [int(item.bill_id) for item in session.results]
    if len(result_ids) != len(set(result_ids)):
        add(
            "duplicate_results",
            "critical",
            "Один законопроект присутствует в итогах несколько раз.",
            "Не публиковать сводку; сверить идемпотентную транзакцию результата.",
        )
    for result in session.results:
        if result.resolution_method not in {"vote", "veto", "oral"}:
            add(
                "unknown_resolution_method",
                "critical",
                "Итог содержит неизвестный способ принятия решения.",
                "Сверить запись результата с журналом событий.",
            )
        if result.resolution_method == "oral" and (
            result.status not in {"accepted", "rejected"}
            or not str(result.resolution_note or "").strip()
            or result.resolved_by_id is None
        ):
            add(
                "invalid_oral_result",
                "critical",
                "Устный итог не содержит полного основания или автора фиксации.",
                "Исправить итог через журналируемую административную операцию.",
            )

    pending = dict(session.pending_action or {})
    if session.stage == "finalizing":
        kind = str(pending.get("kind") or "")
        if kind not in {"vote", "veto", "oral"}:
            add(
                "finalization_claim_missing",
                "critical",
                "Фиксация результата не содержит тип операции.",
                "Не создавать новый итог; восстановить сохранённую операцию или отправить снимок в карантин.",
            )
        if int(pending.get("bill_id") or 0) != int((session.current_bill or {}).get("id") or 0):
            add(
                "finalization_bill_mismatch",
                "critical",
                "Фиксация относится не к текущему законопроекту.",
                "Остановить повторы и сверить атомарную транзакцию результата.",
            )
        if kind == "oral" and (
            str(pending.get("oral_status") or "") not in {"accepted", "rejected"}
            or not str(pending.get("oral_note") or "").strip()
        ):
            add(
                "oral_resolution_incomplete",
                "critical",
                "Устное решение сохранено без результата или основания.",
                "Повторно подтвердить устный итог через административный пульт.",
            )
    elif pending:
        add(
            "orphan_pending_action",
            "warning",
            "За пределами фиксации осталось незавершённое действие.",
            "Сверить ревизию с SQLite и очистить устаревшее действие восстановлением.",
        )

    if session.timer_deadline is not None and session.stage != "voting":
        add(
            "timer_outside_voting",
            "warning",
            "Таймер сохранён вне активного голосования.",
            "Остановить устаревший таймер; этап важнее фоновой задачи.",
        )
    if (session.timer_deadline is None) != (session.timer_seconds is None):
        add(
            "partial_timer_state",
            "warning",
            "Таймер сохранён частично.",
            "Пересоздать или очистить таймер одной атомарной операцией.",
        )

    discussion_fields = bool(
        session.discussion_channel_id
        or session.discussion_initiator_id
        or session.discussion_type
        or session.discussion_allowed_user_ids
    )
    if session.stage == "discussion" and not session.discussion_type:
        add(
            "discussion_type_missing",
            "critical",
            "Активная дискуссия не имеет типа.",
            "Вернуть этап к выбору типа или завершить дискуссию административно.",
        )
    if session.stage not in {"discussion", "discussion_type", "paused", "finalizing"} and discussion_fields:
        add(
            "stale_discussion_state",
            "warning",
            "После дискуссии остались служебные параметры.",
            "Очистить параметры при следующем безопасном переходе этапа.",
        )

    dm_failures = sum(item.dm_failed for item in session.participants.values())
    if dm_failures:
        add(
            "dm_delivery_failures",
            "warning",
            f"Личные панели недоступны для участников: {dm_failures}.",
            "Использовать публичную карточку как резерв и повторить доставку после проверки ЛС.",
        )

    return ConsensusHealthReport(tuple(issues))


__all__ = ["ConsensusHealthIssue", "ConsensusHealthReport", "assess_consensus_health"]
