"""Discord adapter for durable consensus-result delivery.

Payloads contain semantic data only.  No view or Discord object is persisted,
which keeps the outbox portable across restarts and library upgrades.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any

import discord
from persistence import tvrs_repository as storage

from modules.async_safety import consensus_projection_lock
from modules.consensus_core import ConsensusRules, LiveConsensusSession, LiveParticipant, LiveResult
from modules.consensus_runtime import registry as _consensus_registry
from modules.delivery_outbox import DeliveryDeferred, DeliveryReceipt, OutboxMessage
from modules.profile_notifications import evaluate_profile_notification
from modules.tvrs_config import TVRS_MATERIALS_CHANNEL_ID
from modules.tvrs_embeds import build_bill_embed, build_final_summary_embed, build_result_embed


TVRS_RESULT_TOPIC = "tvrs.consensus.result.v1"
TVRS_RETRY_BILL_TOPIC = "tvrs.consensus.retry-bill.v1"
TVRS_BILL_PUBLICATION_TOPIC = "tvrs.bill.publication.v1"
TVRS_CONTROL_DM_TOPIC = "tvrs.consensus.control-dm.v1"
TVRS_CONTROL_NOTICE_TOPIC = "tvrs.consensus.control-notice.v1"
TVRS_NOTICE_DELETE_TOPIC = "tvrs.consensus.notice-delete.v1"
TVRS_PHASE_ANNOUNCEMENT_TOPIC = "tvrs.consensus.phase-announcement.v1"
TVRS_DISCUSSION_INVITE_TOPIC = "tvrs.consensus.discussion-invite.v1"
TVRS_SESSION_SUMMARY_TOPIC = "tvrs.consensus.session-summary.v1"


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
        "source_channel_id": result.source_channel_id,
        "source_message_id": result.source_message_id,
        "decision_category": str(result.decision_category),
        "required_percent": float(result.required_percent),
        "opposed_percent": float(result.opposed_percent),
        "block_votes": dict(result.block_votes),
        "veto_by_id": int(result.veto_by_id) if result.veto_by_id else None,
        "retry_bill_number": int(result.retry_bill_number) if result.retry_bill_number else None,
        "resolution_method": str(result.resolution_method),
        "resolution_note": result.resolution_note,
        "resolved_by_id": int(result.resolved_by_id) if result.resolved_by_id else None,
        "resolved_by_display": result.resolved_by_display,
    }


def _participant_payload(participant: LiveParticipant) -> dict[str, Any]:
    return {
        "user_id": int(participant.user_id),
        "display_name": str(participant.display_name),
        "mention": str(participant.mention),
        "kind": str(participant.kind),
        "permanent": bool(participant.permanent),
        "confirmed": bool(participant.confirmed),
        "vote_message_id": int(participant.vote_message_id) if participant.vote_message_id else None,
        "vote_bill_id": int(participant.vote_bill_id) if participant.vote_bill_id else None,
        "voting_block": participant.voting_block,
    }


def _rules_payload(rules: ConsensusRules) -> dict[str, Any]:
    return {
        "version": int(rules.version),
        "minimum_chairs": int(rules.minimum_chairs),
        "minimum_senators": int(rules.minimum_senators),
        "senators_must_be_odd": bool(rules.senators_must_be_odd),
        "internal_activation_strictly_above": float(rules.internal_activation_strictly_above),
        "chair_yes_weight": float(rules.chair_yes_weight),
        "internal_consensus_weight": float(rules.internal_consensus_weight),
        "acceptance_percent": float(rules.acceptance_percent),
        "minimum_participants": int(rules.minimum_participants),
        "internal_quorum_strictly_above": float(
            rules.internal_quorum_strictly_above
        ),
        "heavy_acceptance_percent": float(rules.heavy_acceptance_percent),
        "unanimous_acceptance_percent": float(
            rules.unanimous_acceptance_percent
        ),
    }


def build_result_deliveries(
    session: LiveConsensusSession,
    result: LiveResult,
    *,
    participant_content: str,
) -> list[dict[str, Any]]:
    common = {
        "payload_version": 1,
        "guild_id": int(session.guild_id),
        "session_key": str(session.session_key),
        "engine_version": int(session.engine_version),
        "channel_id": int(session.channel_id),
        "leader_id": int(session.leader_id),
        "leader_display": str(session.leader_display),
        "plenary_number": int(session.plenary_number),
        "participants": [_participant_payload(item) for item in session.confirmed_participants()],
        "rules": _rules_payload(session.rules),
        "result": _result_payload(result),
    }
    prefix = f"consensus:{session.session_key}:{result.bill_id}"
    deliveries = [
        {
            "topic": TVRS_RESULT_TOPIC,
            "dedupe_key": f"{prefix}:public",
            "payload": {**common, "destination": "public"},
            "max_attempts": 12,
        }
    ]
    for participant in session.confirmed_participants():
        if participant.user_id == session.leader_id:
            continue
        deliveries.append(
            {
                "topic": TVRS_RESULT_TOPIC,
                "dedupe_key": f"{prefix}:dm:{participant.user_id}",
                "payload": {
                    **common,
                    "destination": "participant_dm",
                    "destination_user_id": int(participant.user_id),
                    "destination_message_id": (
                        int(participant.vote_message_id) if participant.vote_message_id else None
                    ),
                    "content": str(participant_content),
                },
                "max_attempts": 12,
            }
        )
    return deliveries


def build_control_dm_deliveries(
    session: LiveConsensusSession,
    *,
    phase: str,
    bill_id: int | None = None,
    generation: str | None = None,
) -> list[dict[str, Any]]:
    """Create deterministic registration/vote DM intents."""

    clean_phase = str(phase).strip().lower()
    if clean_phase not in {"registration", "presentation", "voting"}:
        raise ValueError("tvrs_control_delivery_phase_invalid")
    selected_bill_id = int(bill_id or (session.current_bill or {}).get("id") or 0)
    if clean_phase in {"presentation", "voting"} and selected_bill_id <= 0:
        raise ValueError("tvrs_control_delivery_bill_required")
    participants = (
        [item for item in session.participants.values() if not item.confirmed]
        if clean_phase == "registration"
        else session.confirmed_participants()
    )
    jobs: list[dict[str, Any]] = []
    for participant in participants:
        if participant.user_id == session.leader_id:
            continue
        clean_generation = str(generation or "").strip()
        if clean_phase in {"presentation", "voting"}:
            # A recovery label may repeat on every restart, while a bill must
            # never reuse another bill's idempotency key.
            discriminator = str(selected_bill_id)
            if clean_generation:
                discriminator = f"{selected_bill_id}:{clean_generation}"
        else:
            discriminator = clean_generation or "initial"
        jobs.append(
            {
                "topic": TVRS_CONTROL_DM_TOPIC,
                "dedupe_key": (
                    f"consensus:{session.session_key}:control:{clean_phase}:"
                    f"{discriminator}:{participant.user_id}"
                ),
                "payload": {
                    "payload_version": 1,
                    "guild_id": int(session.guild_id),
                    "session_key": str(session.session_key),
                    "engine_version": int(session.engine_version),
                    "phase": clean_phase,
                    "bill_id": selected_bill_id or None,
                    "user_id": int(participant.user_id),
                },
                # Discord outages and global rate limits can last longer than a
                # couple of minutes.  Interactive controls keep retrying for a
                # useful window; permanent DM failures are classified by the
                # handler and stop immediately.
                "max_attempts": 120,
                "priority": 200,
                "supersede_key": (
                    f"consensus:{session.session_key}:control:{participant.user_id}"
                ),
            }
        )
    return jobs


def build_control_notice_deliveries(
    session: LiveConsensusSession,
    *,
    bill_id: int | None = None,
) -> list[dict[str, Any]]:
    """Create one unread DM notice per participant and bill.

    The reusable control panel and the unread notification intentionally live
    in different outbox jobs.  A temporary notice failure must never turn a
    successfully delivered voting panel into a failed delivery.
    """

    selected_bill_id = int(bill_id or (session.current_bill or {}).get("id") or 0)
    if selected_bill_id <= 0:
        raise ValueError("tvrs_control_notice_bill_required")
    jobs: list[dict[str, Any]] = []
    for participant in session.confirmed_participants():
        if participant.user_id == session.leader_id:
            continue
        jobs.append(
            {
                "topic": TVRS_CONTROL_NOTICE_TOPIC,
                "dedupe_key": (
                    f"consensus:{session.session_key}:bill:{selected_bill_id}:"
                    f"notice:{participant.user_id}"
                ),
                "payload": {
                    "payload_version": 1,
                    "guild_id": int(session.guild_id),
                    "session_key": str(session.session_key),
                    "engine_version": int(session.engine_version),
                    "bill_id": selected_bill_id,
                    "user_id": int(participant.user_id),
                },
                "max_attempts": 60,
                "priority": 150,
                "supersede_key": (
                    f"consensus:{session.session_key}:notice:{participant.user_id}"
                ),
            }
        )
    return jobs


def build_phase_announcement_delivery(
    session: LiveConsensusSession,
    *,
    phase: str,
    bill_id: int | None = None,
) -> dict[str, Any]:
    """Build one server-side call to action for a whole consensus phase.

    This is the durable fallback for members whose direct messages are closed.
    It creates one aggregate message, not one public message per participant.
    """

    clean_phase = str(phase).strip().lower()
    if clean_phase not in {"registration", "presentation", "voting"}:
        raise ValueError("tvrs_phase_announcement_phase_invalid")
    selected_bill_id = int(bill_id or (session.current_bill or {}).get("id") or 0)
    if clean_phase in {"presentation", "voting"} and selected_bill_id <= 0:
        raise ValueError("tvrs_phase_announcement_bill_required")
    participants = (
        [item for item in session.participants.values() if not item.confirmed]
        if clean_phase == "registration"
        else session.confirmed_participants()
    )
    user_ids = [
        int(item.user_id)
        for item in participants
        if int(item.user_id) != int(session.leader_id)
    ]
    discriminator = (
        str(selected_bill_id)
        if clean_phase in {"presentation", "voting"}
        else "initial"
    )
    bill = dict(session.current_bill or {})
    return {
        "topic": TVRS_PHASE_ANNOUNCEMENT_TOPIC,
        "dedupe_key": (
            f"consensus:{session.session_key}:phase:{clean_phase}:{discriminator}"
        ),
        "payload": {
            "payload_version": 1,
            "guild_id": int(session.guild_id),
            "session_key": str(session.session_key),
            "engine_version": int(session.engine_version),
            "channel_id": int(session.channel_id),
            "phase": clean_phase,
            "bill_id": selected_bill_id or None,
            "bill_number": int(bill.get("bill_number") or 0) or None,
            "bill_title": str(bill.get("title") or "")[:300],
            "user_ids": user_ids,
        },
        "max_attempts": 60,
        "priority": 250,
        "supersede_key": f"consensus:{session.session_key}:phase-announcement",
    }


def build_discussion_invite_deliveries(
    session: LiveConsensusSession,
    *,
    channel_id: int,
    discussion_type: str,
    allowed_user_ids: set[int] | list[int] | tuple[int, ...],
) -> list[dict[str, Any]]:
    """Create durable, independently retryable discussion invitations."""

    bill_id = int((session.current_bill or {}).get("id") or 0)
    selected_channel_id = int(channel_id)
    if bill_id <= 0 or selected_channel_id <= 0:
        raise ValueError("tvrs_discussion_invite_identity_required")
    jobs: list[dict[str, Any]] = []
    for user_id in sorted({int(item) for item in allowed_user_ids if int(item) > 0}):
        jobs.append(
            {
                "topic": TVRS_DISCUSSION_INVITE_TOPIC,
                "dedupe_key": (
                    f"consensus:{session.session_key}:bill:{bill_id}:"
                    f"discussion-invite:{user_id}"
                ),
                "payload": {
                    "payload_version": 1,
                    "guild_id": int(session.guild_id),
                    "session_key": str(session.session_key),
                    "engine_version": int(session.engine_version),
                    "bill_id": bill_id,
                    "channel_id": selected_channel_id,
                    "discussion_type": str(discussion_type).strip()[:80],
                    "user_id": user_id,
                },
                "max_attempts": 120,
                "priority": 170,
                "supersede_key": (
                    f"consensus:{session.session_key}:discussion-invite:{user_id}"
                ),
            }
        )
    return jobs


def build_session_summary_deliveries(session: LiveConsensusSession) -> list[dict[str, Any]]:
    result_payloads = [_result_payload(item) for item in session.results]
    common = {
        "payload_version": 1,
        "guild_id": int(session.guild_id),
        "session_key": str(session.session_key),
        "engine_version": int(session.engine_version),
        "channel_id": int(session.channel_id),
        "leader_id": int(session.leader_id),
        "leader_display": str(session.leader_display),
        "plenary_number": int(session.plenary_number),
        "participants": [_participant_payload(item) for item in session.confirmed_participants()],
        "rules": _rules_payload(session.rules),
        "result_bill_ids": [int(item.bill_id) for item in session.results],
        "summary_counts": {
            "total": len(session.results),
            "accepted": sum(item.status == "accepted" for item in session.results),
            "rejected": sum(item.status == "rejected" for item in session.results),
            "vetoed": sum(item.status == "vetoed" for item in session.results),
        },
    }
    prefix = f"consensus:{session.session_key}:summary"
    pages: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 0
    for item in result_payloads:
        estimated = (
            len(str(item.get("title") or "")[:140])
            + len(str(item.get("source_channel_id") or ""))
            + len(str(item.get("source_message_id") or ""))
            + 100
        )
        if current and (current_size + estimated > 3200 or len(current) >= 20):
            pages.append(current)
            current = []
            current_size = 0
        current.append(item)
        current_size += estimated
    if current or not pages:
        pages.append(current)

    page_count = len(pages)
    deliveries: list[dict[str, Any]] = []
    for page_number, page_results in enumerate(pages, start=1):
        deliveries.append({
            "topic": TVRS_SESSION_SUMMARY_TOPIC,
            "dedupe_key": (
                f"{prefix}:public"
                if page_number == 1
                else f"{prefix}:public:{page_number}"
            ),
            "payload": {
                **common,
                "destination": "public",
                "results": page_results,
                "page_number": page_number,
                "page_count": page_count,
            },
            "max_attempts": 12,
        })
    for participant in session.confirmed_participants():
        if participant.user_id == session.leader_id:
            continue
        deliveries.append(
            {
                "topic": TVRS_SESSION_SUMMARY_TOPIC,
                "dedupe_key": f"{prefix}:dm:{participant.user_id}",
                "payload": {
                    **common,
                    "destination": "participant_dm",
                    "results": [],
                    "compact": True,
                    "destination_user_id": int(participant.user_id),
                    "content": "Пленарный консенсус завершён.",
                },
                "max_attempts": 12,
            }
        )
    return deliveries


def build_retry_bill_delivery(
    session: LiveConsensusSession,
    result: LiveResult,
    retry_bill: dict[str, Any],
) -> dict[str, Any]:
    bill = {
        key: retry_bill.get(key)
        for key in (
            "id",
            "guild_id",
            "bill_number",
            "channel_id",
            "message_id",
            "author_id",
            "author_display",
            "title",
            "summary",
            "materials",
            "decision_category",
            "implementation_plan",
            "leadership_actions",
            "editor_workspace_id",
            "status",
            "original_bill_id",
            "attempt",
        )
    }
    return {
        "topic": TVRS_RETRY_BILL_TOPIC,
        "dedupe_key": f"consensus:{session.session_key}:{result.bill_id}:retry-bill:{int(retry_bill['id'])}",
        "payload": {
            "payload_version": 1,
            "guild_id": int(session.guild_id),
            "session_key": str(session.session_key),
            "channel_id": int(TVRS_MATERIALS_CHANNEL_ID),
            "bill": bill,
        },
        "max_attempts": 12,
    }


def _render_objects(payload: dict[str, Any]) -> tuple[LiveConsensusSession, LiveResult]:
    if int(payload.get("payload_version") or 0) != 1:
        raise ValueError("tvrs_delivery_payload_version_unsupported")
    raw_rules = dict(payload.get("rules") or {})
    rules = ConsensusRules(**raw_rules)
    participants: dict[int, LiveParticipant] = {}
    for raw in payload.get("participants") or []:
        item = dict(raw)
        participant = LiveParticipant(
            user_id=int(item["user_id"]),
            display_name=str(item.get("display_name") or item["user_id"]),
            mention=str(item.get("mention") or f"<@{int(item['user_id'])}>"),
            kind=str(item.get("kind") or "senator"),  # type: ignore[arg-type]
            permanent=bool(item.get("permanent")),
            confirmed=bool(item.get("confirmed", True)),
            vote_message_id=(int(item["vote_message_id"]) if item.get("vote_message_id") else None),
            vote_bill_id=(int(item["vote_bill_id"]) if item.get("vote_bill_id") else None),
            voting_block=(
                str(item["voting_block"])
                if item.get("voting_block") in {"first", "second", "third"}
                else None
            ),  # type: ignore[arg-type]
        )
        participants[participant.user_id] = participant
    session = LiveConsensusSession(
        session_key=str(payload["session_key"]),
        guild_id=int(payload["guild_id"]),
        channel_id=int(payload["channel_id"]),
        leader_id=int(payload["leader_id"]),
        leader_display=str(payload.get("leader_display") or payload["leader_id"]),
        plenary_number=int(payload["plenary_number"]),
        participants=participants,
        engine_version=int(payload.get("engine_version") or 2),
        created_at=datetime.now(timezone.utc),
        stage="after_result",
        rules=rules,
    )
    raw_result = dict(payload.get("result") or {})
    result = LiveResult(
        bill_id=int(raw_result["bill_id"]),
        bill_number=int(raw_result["bill_number"]),
        title=str(raw_result.get("title") or ""),
        status=str(raw_result.get("status") or "rejected"),
        internal_percent=float(raw_result.get("internal_percent") or 0.0),
        overall_percent=float(raw_result.get("overall_percent") or 0.0),
        internal_active=bool(raw_result.get("internal_active")),
        votes={int(user_id): str(vote) for user_id, vote in dict(raw_result.get("votes") or {}).items()},
        source_channel_id=(
            int(raw_result["source_channel_id"])
            if raw_result.get("source_channel_id")
            else None
        ),
        source_message_id=(
            int(raw_result["source_message_id"])
            if raw_result.get("source_message_id")
            else None
        ),
        decision_category=str(raw_result.get("decision_category") or "ordinary"),
        required_percent=float(raw_result.get("required_percent") or 50.0),
        opposed_percent=float(raw_result.get("opposed_percent") or 0.0),
        block_votes={
            str(key): str(value)
            for key, value in dict(raw_result.get("block_votes") or {}).items()
        },
        veto_by_id=(int(raw_result["veto_by_id"]) if raw_result.get("veto_by_id") else None),
        retry_bill_number=(
            int(raw_result["retry_bill_number"]) if raw_result.get("retry_bill_number") else None
        ),
        resolution_method=str(raw_result.get("resolution_method") or "vote"),  # type: ignore[arg-type]
        resolution_note=(str(raw_result["resolution_note"]) if raw_result.get("resolution_note") else None),
        resolved_by_id=(int(raw_result["resolved_by_id"]) if raw_result.get("resolved_by_id") else None),
        resolved_by_display=(
            str(raw_result["resolved_by_display"]) if raw_result.get("resolved_by_display") else None
        ),
    )
    return session, result


def delivery_marker(dedupe_key: str) -> str:
    digest = hashlib.sha256(str(dedupe_key).encode("utf-8")).hexdigest()[:12]
    return f"TVRS • результат зафиксирован • ref {digest}"


async def find_delivery_marker(channel: Any, marker: str) -> Any | None:
    history = getattr(channel, "history", None)
    if history is None:
        return None
    async for candidate in history(limit=50):
        if getattr(getattr(candidate, "author", None), "bot", False) is not True:
            continue
        for embed in getattr(candidate, "embeds", ()):
            footer = getattr(getattr(embed, "footer", None), "text", None)
            if footer == marker:
                return candidate
    return None


async def result_control_message_was_reused(payload: dict[str, Any]) -> bool:
    """Return whether a later bill now owns the participant control message.

    Result deliveries may be delayed for minutes or replayed from the dead-letter
    queue.  A participant's saved message can meanwhile become the control panel
    for the next bill.  Editing that ID would remove the newer voting buttons.
    Runtime state is preferred, while the durable snapshot covers startup before
    the in-memory registry has been restored.
    """

    guild_id = int(payload.get("guild_id") or 0)
    session_key = str(payload.get("session_key") or "")
    result_bill_id = int(dict(payload.get("result") or {}).get("bill_id") or 0)
    user_id = int(payload.get("destination_user_id") or 0)
    if guild_id <= 0 or not session_key or result_bill_id <= 0:
        return False

    current = _consensus_registry.find(session_key)
    if current is not None and int(current.guild_id) == guild_id:
        current_bill_id = int(dict(current.current_bill or {}).get("id") or 0)
        participant = current.participants.get(user_id)
        panel_bill_id = int(participant.vote_bill_id or 0) if participant else 0
        return (
            current_bill_id > 0 and current_bill_id != result_bill_id
        ) or (
            panel_bill_id > 0 and panel_bill_id != result_bill_id
        )

    snapshots = await asyncio.to_thread(storage.tvrs_consensus_active_sessions, guild_id)
    snapshot = next(
        (item for item in snapshots if str(item.get("session_key") or "") == session_key),
        None,
    )
    if snapshot is None or str(snapshot.get("session_key") or "") != session_key:
        return False
    current_bill_id = int(dict(snapshot.get("current_bill") or {}).get("id") or 0)
    panel_bill_id = 0
    for raw_participant in snapshot.get("participants") or []:
        participant = dict(raw_participant)
        if int(participant.get("user_id") or 0) == user_id:
            panel_bill_id = int(participant.get("vote_bill_id") or 0)
            break
    return (
        current_bill_id > 0 and current_bill_id != result_bill_id
    ) or (
        panel_bill_id > 0 and panel_bill_id != result_bill_id
    )


def make_result_delivery_handler(bot: Any):
    async def deliver(message: OutboxMessage) -> DeliveryReceipt:
        payload = message.payload
        session, result = _render_objects(payload)
        persisted_result = await asyncio.to_thread(
            storage.tvrs_live_result_for_bill,
            session.session_key,
            result.bill_id,
        )
        if persisted_result is None:
            return DeliveryReceipt()
        embed = build_result_embed(result, session)
        marker = delivery_marker(message.dedupe_key)
        embed.set_footer(text=marker)
        destination = str(payload.get("destination") or "")
        guild = bot.get_guild(session.guild_id)
        if guild is None:
            raise RuntimeError(f"tvrs_delivery_guild_unavailable:{session.guild_id}")

        if destination == "public":
            channel = guild.get_channel(session.channel_id) or bot.get_channel(session.channel_id)
            if channel is None:
                channel = await bot.fetch_channel(session.channel_id)
            if not hasattr(channel, "send"):
                raise RuntimeError(f"tvrs_delivery_channel_unavailable:{session.channel_id}")
            previous = await find_delivery_marker(channel, marker)
            if previous is not None:
                return DeliveryReceipt(message_id=int(previous.id))
            sent = await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            return DeliveryReceipt(message_id=int(sent.id))

        if destination != "participant_dm":
            raise ValueError(f"tvrs_delivery_destination_invalid:{destination}")
        user_id = int(payload.get("destination_user_id") or 0)
        async with consensus_projection_lock(session.session_key, user_id):
            member = guild.get_member(user_id)
            if member is None:
                member = await guild.fetch_member(user_id)
            dm_channel = member.dm_channel or await member.create_dm()
            existing_message_id = int(payload.get("destination_message_id") or 0)
            if existing_message_id and await result_control_message_was_reused(payload):
                existing_message_id = 0
            if existing_message_id:
                try:
                    existing = await dm_channel.fetch_message(existing_message_id)
                except discord.NotFound:
                    existing = None
                if existing is not None:
                    # Ownership can change while Discord resolves the message.
                    # Fence again immediately before removing its controls.
                    if not await result_control_message_was_reused(payload):
                        await existing.edit(
                            content=str(payload.get("content") or ""),
                            embed=embed,
                            view=None,
                        )
                        return DeliveryReceipt(message_id=int(existing.id))
            previous = await find_delivery_marker(dm_channel, marker)
            if previous is not None:
                return DeliveryReceipt(message_id=int(previous.id))
            sent = await member.send(content=str(payload.get("content") or ""), embed=embed)
            return DeliveryReceipt(message_id=int(sent.id))

    return deliver


def make_retry_bill_delivery_handler(bot: Any):
    async def deliver(message: OutboxMessage) -> DeliveryReceipt:
        payload = message.payload
        if int(payload.get("payload_version") or 0) != 1:
            raise ValueError("tvrs_retry_delivery_payload_version_unsupported")
        guild_id = int(payload.get("guild_id") or 0)
        channel_id = int(payload.get("channel_id") or TVRS_MATERIALS_CHANNEL_ID)
        bill = dict(payload.get("bill") or {})
        bill_id = int(bill.get("id") or 0)
        if guild_id <= 0 or bill_id <= 0:
            raise ValueError("tvrs_retry_delivery_identity_invalid")
        current_bill = await asyncio.to_thread(storage.tvrs_get_bill_dict_by_id, bill_id)
        if current_bill is None:
            # An administrator may delete a bill after the session is finished.
            # A dead-letter replay must not resurrect its public card.
            return DeliveryReceipt()
        guild = bot.get_guild(guild_id)
        if guild is None:
            raise RuntimeError(f"tvrs_delivery_guild_unavailable:{guild_id}")
        channel = guild.get_channel(channel_id) or bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        if not hasattr(channel, "send"):
            raise RuntimeError(f"tvrs_delivery_channel_unavailable:{channel_id}")

        existing_message_id = int((current_bill or {}).get("message_id") or bill.get("message_id") or 0)
        marker = delivery_marker(message.dedupe_key)
        embed = build_bill_embed(guild, current_bill or bill)
        embed.set_footer(text=marker)
        if existing_message_id and hasattr(channel, "fetch_message"):
            try:
                existing = await channel.fetch_message(existing_message_id)
            except discord.NotFound:
                existing = None
            if existing is not None:
                await existing.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                if str(payload.get("publication_kind") or "") == "initial":
                    await asyncio.to_thread(
                        storage.tvrs_complete_bill_publication,
                        bill_id,
                        int(existing.id),
                        channel_id,
                    )
                return DeliveryReceipt(message_id=int(existing.id))
        previous = await find_delivery_marker(channel, marker)
        if previous is not None:
            if str(payload.get("publication_kind") or "") == "initial":
                await asyncio.to_thread(
                    storage.tvrs_complete_bill_publication,
                    bill_id,
                    int(previous.id),
                    channel_id,
                )
            else:
                await asyncio.to_thread(storage.tvrs_set_bill_message, bill_id, int(previous.id), channel_id)
            return DeliveryReceipt(message_id=int(previous.id))
        sent = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        if str(payload.get("publication_kind") or "") == "initial":
            await asyncio.to_thread(
                storage.tvrs_complete_bill_publication,
                bill_id,
                int(sent.id),
                channel_id,
            )
        else:
            await asyncio.to_thread(storage.tvrs_set_bill_message, bill_id, int(sent.id), channel_id)
        return DeliveryReceipt(message_id=int(sent.id))

    return deliver


def _render_summary_session(payload: dict[str, Any]) -> LiveConsensusSession:
    if int(payload.get("payload_version") or 0) != 1:
        raise ValueError("tvrs_summary_payload_version_unsupported")
    participants: dict[int, LiveParticipant] = {}
    for raw in payload.get("participants") or []:
        item = dict(raw)
        participant = LiveParticipant(
            user_id=int(item["user_id"]),
            display_name=str(item.get("display_name") or item["user_id"]),
            mention=str(item.get("mention") or f"<@{int(item['user_id'])}>"),
            kind=str(item.get("kind") or "senator"),  # type: ignore[arg-type]
            permanent=bool(item.get("permanent")),
            confirmed=bool(item.get("confirmed", True)),
            vote_message_id=(int(item["vote_message_id"]) if item.get("vote_message_id") else None),
            vote_bill_id=(int(item["vote_bill_id"]) if item.get("vote_bill_id") else None),
            voting_block=(
                str(item["voting_block"])
                if item.get("voting_block") in {"first", "second", "third"}
                else None
            ),  # type: ignore[arg-type]
        )
        participants[participant.user_id] = participant
    session = LiveConsensusSession(
        session_key=str(payload["session_key"]),
        guild_id=int(payload["guild_id"]),
        channel_id=int(payload["channel_id"]),
        leader_id=int(payload["leader_id"]),
        leader_display=str(payload.get("leader_display") or payload["leader_id"]),
        plenary_number=int(payload["plenary_number"]),
        participants=participants,
        engine_version=int(payload.get("engine_version") or 2),
        stage="finished",
        finished=True,
        rules=ConsensusRules(**dict(payload.get("rules") or {})),
    )
    for raw in payload.get("results") or []:
        item = dict(raw)
        session.results.append(
            LiveResult(
                bill_id=int(item["bill_id"]),
                bill_number=int(item["bill_number"]),
                title=str(item.get("title") or ""),
                status=str(item.get("status") or "rejected"),
                internal_percent=float(item.get("internal_percent") or 0.0),
                overall_percent=float(item.get("overall_percent") or 0.0),
                internal_active=bool(item.get("internal_active")),
                votes={int(key): str(value) for key, value in dict(item.get("votes") or {}).items()},
                source_channel_id=(
                    int(item["source_channel_id"])
                    if item.get("source_channel_id")
                    else None
                ),
                source_message_id=(
                    int(item["source_message_id"])
                    if item.get("source_message_id")
                    else None
                ),
                decision_category=str(item.get("decision_category") or "ordinary"),
                required_percent=float(item.get("required_percent") or 50.0),
                opposed_percent=float(item.get("opposed_percent") or 0.0),
                block_votes={
                    str(key): str(value)
                    for key, value in dict(item.get("block_votes") or {}).items()
                },
                veto_by_id=(int(item["veto_by_id"]) if item.get("veto_by_id") else None),
                retry_bill_number=(
                    int(item["retry_bill_number"]) if item.get("retry_bill_number") else None
                ),
                resolution_method=str(item.get("resolution_method") or "vote"),  # type: ignore[arg-type]
                resolution_note=(str(item["resolution_note"]) if item.get("resolution_note") else None),
                resolved_by_id=(int(item["resolved_by_id"]) if item.get("resolved_by_id") else None),
                resolved_by_display=(
                    str(item["resolved_by_display"]) if item.get("resolved_by_display") else None
                ),
            )
        )
    return session


def make_session_summary_delivery_handler(bot: Any):
    async def deliver(message: OutboxMessage) -> DeliveryReceipt:
        payload = message.payload
        session = _render_summary_session(payload)
        result_bill_ids = [
            int(item)
            for item in payload.get("result_bill_ids") or []
            if int(item) > 0
        ]
        if not result_bill_ids:
            result_bill_ids = [int(result.bill_id) for result in session.results]
        for result_bill_id in result_bill_ids:
            persisted = await asyncio.to_thread(
                storage.tvrs_live_result_for_bill,
                session.session_key,
                result_bill_id,
            )
            if persisted is None:
                return DeliveryReceipt()
        embed = build_final_summary_embed(
            session,
            page_number=int(payload.get("page_number") or 1),
            page_count=int(payload.get("page_count") or 1),
            compact=bool(payload.get("compact")),
            summary_counts={
                str(key): int(value)
                for key, value in dict(payload.get("summary_counts") or {}).items()
            },
        )
        marker = delivery_marker(message.dedupe_key)
        embed.set_footer(text=marker)
        guild = bot.get_guild(session.guild_id)
        if guild is None:
            raise RuntimeError(f"tvrs_delivery_guild_unavailable:{session.guild_id}")
        destination = str(payload.get("destination") or "")
        if destination == "public":
            channel = guild.get_channel(session.channel_id) or bot.get_channel(session.channel_id)
            if channel is None:
                channel = await bot.fetch_channel(session.channel_id)
            if not hasattr(channel, "send"):
                raise RuntimeError(f"tvrs_delivery_channel_unavailable:{session.channel_id}")
            previous = await find_delivery_marker(channel, marker)
            if previous is not None:
                return DeliveryReceipt(message_id=int(previous.id))
            sent = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return DeliveryReceipt(message_id=int(sent.id))
        if destination != "participant_dm":
            raise ValueError(f"tvrs_summary_destination_invalid:{destination}")
        user_id = int(payload.get("destination_user_id") or 0)
        decision = await asyncio.to_thread(
            evaluate_profile_notification,
            session.guild_id,
            user_id,
            "consensus",
        )
        if not decision.allowed:
            if decision.resume_at is not None:
                raise DeliveryDeferred(decision.resume_at, "profile_quiet_hours")
            return DeliveryReceipt()
        member = guild.get_member(user_id)
        if member is None:
            member = await guild.fetch_member(user_id)
        dm_channel = member.dm_channel or await member.create_dm()
        previous = await find_delivery_marker(dm_channel, marker)
        if previous is not None:
            return DeliveryReceipt(message_id=int(previous.id))
        sent = await member.send(content=str(payload.get("content") or ""), embed=embed)
        return DeliveryReceipt(message_id=int(sent.id))

    return deliver
