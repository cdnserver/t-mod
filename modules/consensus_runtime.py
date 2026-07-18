"""Process runtime for live consensus sessions.

The registry is deliberately independent from Discord views. Other modules may
read a stable snapshot without importing the TVRS UI and creating a cycle.
"""

from __future__ import annotations

import asyncio
from typing import Any

from modules.consensus_core import LiveConsensusSession, clean_stage_name
from modules.consensus_repository import StorageConsensusRepository
from modules.consensus_service import (
    ConsensusActor,
    ConsensusCoordinator,
    ConsensusSessionRegistry,
)


repository = StorageConsensusRepository()
coordinator = ConsensusCoordinator(repository)
registry = ConsensusSessionRegistry()
active_sessions = registry.sessions
restored_guilds: set[int] = set()


def active_consensus_snapshot(guild_id: int) -> dict[str, Any] | None:
    session = registry.get(guild_id)
    if session is None:
        return None
    bill = session.current_bill or {}
    return {
        "guild_id": session.guild_id,
        "session_key": session.session_key,
        "engine_version": session.engine_version,
        "plenary_number": session.plenary_number,
        "stage": session.stage,
        "stage_label": clean_stage_name(session.stage),
        "leader_id": session.leader_id,
        "leader_display": session.leader_display,
        "confirmed_count": len(session.confirmed_participants()),
        "participant_count": len(session.participants),
        "timer_deadline": session.timer_deadline.isoformat() if session.timer_deadline else None,
        "discussion_channel_id": session.discussion_channel_id,
        "current_bill": (
            {
                "id": int(bill.get("id") or 0),
                "bill_number": int(bill.get("bill_number") or 0),
                "title": str(bill.get("title") or ""),
            }
            if bill
            else None
        ),
    }


def session_lock(guild_id: int) -> asyncio.Lock:
    return registry.lock(guild_id)


def persist_session(
    session: LiveConsensusSession,
    event_type: str,
    *,
    actor_id: int | None = None,
    actor_display: str | None = None,
    stage_from: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    coordinator.save(
        session,
        event_type,
        actor=ConsensusActor(actor_id, actor_display),
        stage_from=stage_from,
        details=details,
    )
