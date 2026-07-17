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

from modules.consensus_core import ConsensusRules, LiveConsensusSession, LiveParticipant, LiveResult
from modules.consensus_runtime import active_consensus_snapshot
from modules.delivery_outbox import DeliveryReceipt, OutboxMessage
from modules.tvrs_config import TVRS_MATERIALS_CHANNEL_ID
from modules.tvrs_embeds import build_bill_embed, build_final_summary_embed, build_result_embed


TVRS_RESULT_TOPIC = "tvrs.consensus.result.v1"
TVRS_RETRY_BILL_TOPIC = "tvrs.consensus.retry-bill.v1"
TVRS_BILL_PUBLICATION_TOPIC = "tvrs.bill.publication.v1"
TVRS_CONTROL_DM_TOPIC = "tvrs.consensus.control-dm.v1"
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
        "veto_by_id": int(result.veto_by_id) if result.veto_by_id else None,
        "retry_bill_number": int(result.retry_bill_number) if result.retry_bill_number else None,
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
    if clean_phase not in {"registration", "voting"}:
        raise ValueError("tvrs_control_delivery_phase_invalid")
    selected_bill_id = int(bill_id or (session.current_bill or {}).get("id") or 0)
    if clean_phase == "voting" and selected_bill_id <= 0:
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
        discriminator = (
            str(generation).strip()
            if generation is not None and str(generation).strip()
            else (str(selected_bill_id) if clean_phase == "voting" else "initial")
        )
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
                    "phase": clean_phase,
                    "bill_id": selected_bill_id or None,
                    "user_id": int(participant.user_id),
                },
                "max_attempts": 12,
            }
        )
    return jobs


def build_session_summary_deliveries(session: LiveConsensusSession) -> list[dict[str, Any]]:
    common = {
        "payload_version": 1,
        "guild_id": int(session.guild_id),
        "session_key": str(session.session_key),
        "channel_id": int(session.channel_id),
        "leader_id": int(session.leader_id),
        "leader_display": str(session.leader_display),
        "plenary_number": int(session.plenary_number),
        "participants": [_participant_payload(item) for item in session.confirmed_participants()],
        "rules": _rules_payload(session.rules),
        "results": [_result_payload(item) for item in session.results],
    }
    prefix = f"consensus:{session.session_key}:summary"
    deliveries = [
        {
            "topic": TVRS_SESSION_SUMMARY_TOPIC,
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
                "topic": TVRS_SESSION_SUMMARY_TOPIC,
                "dedupe_key": f"{prefix}:dm:{participant.user_id}",
                "payload": {
                    **common,
                    "destination": "participant_dm",
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
        veto_by_id=(int(raw_result["veto_by_id"]) if raw_result.get("veto_by_id") else None),
        retry_bill_number=(
            int(raw_result["retry_bill_number"]) if raw_result.get("retry_bill_number") else None
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
    if guild_id <= 0 or not session_key or result_bill_id <= 0:
        return False

    snapshot = active_consensus_snapshot(guild_id)
    if snapshot is None:
        snapshots = await asyncio.to_thread(storage.tvrs_consensus_active_sessions, guild_id)
        snapshot = next(
            (item for item in snapshots if str(item.get("session_key") or "") == session_key),
            None,
        )
    if snapshot is None or str(snapshot.get("session_key") or "") != session_key:
        return False
    current_bill_id = int(dict(snapshot.get("current_bill") or {}).get("id") or 0)
    return current_bill_id > 0 and current_bill_id != result_bill_id


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
                await existing.edit(content=str(payload.get("content") or ""), embed=embed, view=None)
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
                veto_by_id=(int(item["veto_by_id"]) if item.get("veto_by_id") else None),
                retry_bill_number=(
                    int(item["retry_bill_number"]) if item.get("retry_bill_number") else None
                ),
            )
        )
    return session


def make_session_summary_delivery_handler(bot: Any):
    async def deliver(message: OutboxMessage) -> DeliveryReceipt:
        payload = message.payload
        session = _render_summary_session(payload)
        for result in session.results:
            persisted = await asyncio.to_thread(
                storage.tvrs_live_result_for_bill,
                session.session_key,
                result.bill_id,
            )
            if persisted is None:
                return DeliveryReceipt()
        embed = build_final_summary_embed(session)
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
