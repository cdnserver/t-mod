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

    def as_int(value: object) -> int | None:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            return None

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
    current_bill_id = as_int((session.current_bill or {}).get("id"))
    current_bill_number = as_int((session.current_bill or {}).get("bill_number"))
    if session.current_bill is not None and (
        current_bill_id is None
        or current_bill_id <= 0
        or current_bill_number is None
        or current_bill_number <= 0
    ):
        add(
            "bill_identity_missing",
            "critical",
            "Текущий законопроект не содержит устойчивого идентификатора или номера.",
            "Не создавать доставки и результат; сверить проект с очередью SQLite.",
        )
    if session.stage in {"registration", "after_result"} and session.current_bill is not None:
        add(
            "bill_leaked_between_stages",
            "critical",
            "Законопроект привязан к этапу, на котором его быть не должно.",
            "Не продолжать автоматически; сверить статус проекта и журнал транзакций.",
        )

    if session.stage == "paused":
        resumable_stages = {"voting", "discussion_type", "discussion", "after_result"}
        if session.previous_stage not in resumable_stages:
            add(
                "pause_target_invalid",
                "critical",
                "Пауза не содержит корректного этапа для продолжения.",
                "Председателю выбрать безопасное завершение либо сверить последний переход в журнале.",
            )
        paused_needs_bill = session.previous_stage in {"voting", "discussion_type", "discussion"}
        if paused_needs_bill and session.current_bill is None:
            add(
                "paused_bill_missing",
                "critical",
                "Пауза голосования потеряла текущий законопроект.",
                "Не возобновлять автоматически; сверить проект и журнал переходов.",
            )
        if session.previous_stage == "after_result" and session.current_bill is not None:
            add(
                "paused_bill_leaked",
                "critical",
                "Межпроектная пауза ошибочно содержит активный законопроект.",
                "Не строить панели голосования; сверить статус проекта в очереди.",
            )

    confirmed_ids = {item.user_id for item in session.confirmed_participants()}
    unknown_votes = set(session.votes) - confirmed_ids
    invalid_votes = {
        user_id
        for user_id, vote in session.votes.items()
        if vote not in {"yes", "no", "abstain"}
    }
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

    result_ids = [as_int(item.bill_id) for item in session.results]
    valid_result_ids = [item for item in result_ids if item is not None]
    if len(valid_result_ids) != len(set(valid_result_ids)):
        add(
            "duplicate_results",
            "critical",
            "Один законопроект присутствует в итогах несколько раз.",
            "Не публиковать сводку; сверить идемпотентную транзакцию результата.",
        )
    for result in session.results:
        result_bill_id = as_int(result.bill_id)
        result_bill_number = as_int(result.bill_number)
        if (
            result_bill_id is None
            or result_bill_id <= 0
            or result_bill_number is None
            or result_bill_number <= 0
        ):
            add(
                "result_identity_invalid",
                "critical",
                "Один из результатов не содержит корректный номер или идентификатор проекта.",
                "Не публиковать сводку; сверить результат с законопроектом в SQLite.",
            )
        if result.status not in {"accepted", "rejected", "vetoed"}:
            add(
                "result_status_invalid",
                "critical",
                "Один из результатов содержит неизвестный статус.",
                "Не продолжать автоматически; сверить атомарную запись результата.",
            )
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
        if as_int(pending.get("bill_id")) != current_bill_id:
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

    if session.stage != "paused" and (
        session.paused_reason is not None or session.pause_is_automatic
    ):
        add(
            "stale_pause_state",
            "warning",
            "После возобновления остались параметры старой паузы.",
            "Очистить служебные параметры одной журналируемой операцией.",
        )

    discussion_fields = bool(
        session.discussion_channel_id
        or session.discussion_initiator_id
        or session.discussion_type
        or session.discussion_allowed_user_ids
        or session.discussion_note_message_id
        or any(item.discussion_message_id for item in session.participants.values())
    )
    if session.stage == "discussion" and not session.discussion_type:
        add(
            "discussion_type_missing",
            "critical",
            "Активная дискуссия не имеет типа.",
            "Вернуть этап к выбору типа или завершить дискуссию административно.",
        )
    if session.stage == "discussion" and not session.discussion_initiator_id:
        add(
            "discussion_initiator_missing",
            "critical",
            "Активная дискуссия не содержит инициатора.",
            "Завершить дискуссию административно и заново проверить кворум.",
        )
    unknown_discussion_users = set(session.discussion_allowed_user_ids) - set(session.participants)
    if unknown_discussion_users:
        add(
            "discussion_users_outside_roster",
            "critical",
            "В дискуссии найдены получатели вне замороженного состава.",
            "Не отправлять приглашения посторонним; завершить или восстановить дискуссию.",
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
