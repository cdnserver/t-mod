from __future__ import annotations

import json
from typing import Any, Iterable

from persistence import tvrs_repository as storage
from modules.consensus_core import LiveConsensusSession, LiveResult, session_to_snapshot
from modules.consensus_service import ConsensusActor


class StorageConsensusRepository:
    """SQLite adapter kept separate from the consensus rules and Discord UI."""

    def save(
        self,
        session: LiveConsensusSession,
        event_type: str,
        *,
        actor: ConsensusActor | None = None,
        stage_from: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return storage.tvrs_consensus_save_session(
            session_to_snapshot(session),
            event_type=event_type,
            actor_id=actor.user_id if actor else None,
            actor_display=actor.display_name if actor else None,
            stage_from=stage_from,
            details=details,
        )

    def active_snapshots(self, guild_id: int | None = None) -> list[dict[str, Any]]:
        return storage.tvrs_consensus_active_sessions(guild_id)

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
        return storage.tvrs_consensus_save_session(
            session_to_snapshot(session),
            event_type=event_type,
            actor_id=actor.user_id if actor else None,
            actor_display=actor.display_name if actor else None,
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
        return storage.tvrs_consensus_commit_begin_bill(
            session_to_snapshot(session),
            expected_revision=expected_revision,
            bill_id=bill_id,
            event_type=event_type,
            actor_id=actor.user_id,
            actor_display=actor.display_name,
            details=details,
            deliveries=deliveries,
        )

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
        return storage.tvrs_consensus_commit_finalization(
            session_to_snapshot(session),
            expected_revision=expected_revision,
            result={
                "bill_id": int(result.bill_id),
                "bill_number": int(result.bill_number),
                "bill_title": str(result.title),
                "status": str(result.status),
                "internal_percent": float(result.internal_percent),
                "overall_percent": float(result.overall_percent),
                "internal_active": bool(result.internal_active),
                "votes_json": json.dumps(
                    {str(user_id): vote for user_id, vote in result.votes.items()},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "source_channel_id": result.source_channel_id,
                "source_message_id": result.source_message_id,
                "decision_category": str(result.decision_category),
                "required_percent": float(result.required_percent),
                "opposed_percent": float(result.opposed_percent),
                "block_votes_json": json.dumps(
                    dict(result.block_votes),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "veto_by_id": result.veto_by_id,
                "veto_by_display": actor.display_name if result.veto_by_id and actor else None,
                "retry_bill_number": result.retry_bill_number,
                "resolution_method": str(result.resolution_method),
                "resolution_note": result.resolution_note,
                "resolved_by_id": result.resolved_by_id,
                "resolved_by_display": result.resolved_by_display,
            },
            bill_status=bill_status,
            result_summary=result_summary,
            event_type=event_type,
            actor_id=actor.user_id if actor else None,
            actor_display=actor.display_name if actor else None,
            details=details,
            deliveries=deliveries,
        )

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
        return storage.tvrs_consensus_commit_finish(
            session_to_snapshot(session),
            expected_revision=expected_revision,
            current_bill_id=current_bill_id,
            advance_plenary=advance_plenary,
            event_type=event_type,
            actor_id=actor.user_id,
            actor_display=actor.display_name,
            details=details,
            deliveries=deliveries,
        )

    def events(self, session_key: str, limit: int = 200) -> list[dict[str, Any]]:
        return storage.tvrs_consensus_events(session_key, limit)

    def quarantine(self, session_key: str, reason: str) -> bool:
        return storage.tvrs_consensus_quarantine_session(session_key, reason)
