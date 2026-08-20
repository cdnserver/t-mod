"""Live SGL correspondence shared by Discord gateway handlers and the web API."""

from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path
from typing import Any, Iterable

import discord

from persistence import sgl_repository as storage
from persistence.core import SGLCase


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default


SGL_WEB_MESSAGE_LIMIT = max(1, min(_env_int("SGL_WEB_MESSAGE_LIMIT", 2_000), 4_000))
SGL_WEB_ATTACHMENT_LIMIT = max(
    1_024,
    min(_env_int("SGL_WEB_ATTACHMENT_LIMIT_BYTES", 8 * 1024 * 1024), 25 * 1024 * 1024),
)
SGL_WEB_ATTACHMENT_COUNT = max(1, min(_env_int("SGL_WEB_ATTACHMENT_COUNT", 10), 10))
_SQLITE_SNOWFLAKE_MAX = 9_223_372_036_854_775_807


def case_participant_ids(case: SGLCase) -> set[int]:
    return {
        int(user_id)
        for user_id in (case.client_id, case.lead_lawyer_id, case.secretary_id)
        if user_id is not None and int(user_id) > 0
    }


def can_view_case(case: SGLCase, user_id: int, *, manager: bool) -> bool:
    return bool(manager) or int(user_id) in case_participant_ids(case)


def can_write_case(case: SGLCase, user_id: int, *, manager: bool) -> bool:
    if not can_view_case(case, user_id, manager=manager):
        return False
    return case.archived_at is None and case.status not in {"closed", "reserved", "error"}


def _author_avatar(author: Any) -> str | None:
    avatar = getattr(author, "display_avatar", None)
    value = getattr(avatar, "url", None)
    return str(value) if value else None


def discord_attachments(attachments: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for attachment in list(attachments)[:SGL_WEB_ATTACHMENT_COUNT]:
        result.append(
            {
                "id": int(getattr(attachment, "id", 0) or 0) or None,
                "filename": str(getattr(attachment, "filename", "attachment")),
                "url": str(getattr(attachment, "url", "")),
                "content_type": getattr(attachment, "content_type", None),
                "size": int(getattr(attachment, "size", 0) or 0),
            }
        )
    return result


async def capture_discord_case_message(message: discord.Message) -> dict[str, Any] | None:
    """Persist a human Discord message if it belongs to an active SGL case."""

    if message.guild is None or message.author.bot:
        return None
    case = await asyncio.to_thread(
        storage.get_sgl_case_by_channel, message.guild.id, message.channel.id
    )
    if case is None:
        return None
    reference = getattr(message, "reference", None)
    reply_to = getattr(reference, "message_id", None)
    return await asyncio.to_thread(
        storage.record_sgl_case_message,
        case=case,
        origin="discord",
        discord_message_id=int(message.id),
        author_id=int(message.author.id),
        author_display=str(
            getattr(message.author, "display_name", getattr(message.author, "name", "Участник"))
        ),
        author_avatar_url=_author_avatar(message.author),
        author_is_bot=False,
        content=str(message.content or ""),
        attachments=discord_attachments(message.attachments),
        reply_to_discord_message_id=int(reply_to) if reply_to else None,
        created_at=message.created_at.isoformat(),
        edited_at=message.edited_at.isoformat() if message.edited_at else None,
    )


async def update_discord_case_message(message: discord.Message) -> dict[str, Any] | None:
    if message.guild is None or message.author.bot:
        return None
    return await asyncio.to_thread(
        storage.update_sgl_case_message_from_discord,
        guild_id=int(message.guild.id),
        discord_message_id=int(message.id),
        content=str(message.content or ""),
        attachments=discord_attachments(message.attachments),
        edited_at=message.edited_at.isoformat() if message.edited_at else None,
    )


async def mark_discord_case_message_deleted(
    *, guild_id: int | None, discord_message_id: int
) -> dict[str, Any] | None:
    if guild_id is None:
        return None
    return await asyncio.to_thread(
        storage.mark_sgl_case_message_deleted,
        guild_id=int(guild_id),
        discord_message_id=int(discord_message_id),
    )


def _safe_attachment_name(value: str) -> str:
    candidate = Path(str(value or "attachment")).name.strip().replace("\x00", "")
    return candidate[:180] or "attachment"


async def _case_reply_reference(
    *, case: SGLCase, reply_to_discord_message_id: int | None
) -> discord.MessageReference | None:
    """Return a safe Discord reply reference scoped to one SGL case.

    A transcript lookup is required before Discord receives the reference:
    otherwise a client could submit an arbitrary snowflake and turn a case
    message into a reply to another case's conversation.  The final Discord
    API call still uses ``fail_if_not_exists=True``; a record surviving after
    its source message was removed in Discord is never silently converted to
    an ordinary message.
    """

    if reply_to_discord_message_id is None:
        return None
    try:
        reply_id = int(reply_to_discord_message_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("sgl_case_reply_invalid") from exc
    if reply_id <= 0 or reply_id > _SQLITE_SNOWFLAKE_MAX:
        raise ValueError("sgl_case_reply_invalid")
    target = await asyncio.to_thread(
        storage.get_sgl_case_message_by_discord_id,
        guild_id=int(case.guild_id),
        discord_message_id=reply_id,
    )
    if target is None:
        raise ValueError("sgl_case_reply_not_found")
    if int(target.get("case_id") or 0) != int(case.id):
        raise ValueError("sgl_case_reply_cross_case")
    if target.get("deleted_at"):
        raise ValueError("sgl_case_reply_deleted")
    if case.channel_id is None:  # Defensive: caller already checks this.
        raise ValueError("sgl_case_channel_unavailable")
    return discord.MessageReference(
        message_id=reply_id,
        channel_id=int(case.channel_id),
        guild_id=int(case.guild_id),
        fail_if_not_exists=True,
    )


async def send_web_case_message(
    *,
    bot: discord.Client,
    case: SGLCase,
    author_id: int,
    author_display: str,
    content: str,
    uploads: Iterable[tuple[str, bytes, str | None]] = (),
    reply_to_discord_message_id: int | None = None,
) -> dict[str, Any]:
    """Publish one web-authored message to Discord and store its live transcript row."""

    if case.channel_id is None:
        raise ValueError("sgl_case_channel_unavailable")
    clean_content = str(content or "").strip()
    if len(clean_content) > SGL_WEB_MESSAGE_LIMIT:
        raise ValueError("sgl_case_message_too_long")
    prepared_uploads = list(uploads)
    if len(prepared_uploads) > SGL_WEB_ATTACHMENT_COUNT:
        raise ValueError("sgl_case_attachment_count_exceeded")
    files: list[discord.File] = []
    for filename, data, _content_type in prepared_uploads:
        if len(data) > SGL_WEB_ATTACHMENT_LIMIT:
            raise ValueError("sgl_case_attachment_too_large")
        files.append(discord.File(io.BytesIO(data), filename=_safe_attachment_name(filename)))
    if not clean_content and not files:
        raise ValueError("sgl_case_message_empty")

    reference = await _case_reply_reference(
        case=case,
        reply_to_discord_message_id=reply_to_discord_message_id,
    )

    channel = bot.get_channel(int(case.channel_id))
    if not isinstance(channel, discord.TextChannel):
        try:
            fetched = await bot.fetch_channel(int(case.channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            raise ValueError("sgl_case_channel_unavailable") from exc
        channel = fetched if isinstance(fetched, discord.TextChannel) else None
    if channel is None:
        raise ValueError("sgl_case_channel_unavailable")

    try:
        sent = await channel.send(
            content=clean_content or None,
            files=files or None,
            reference=reference,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        # With a reply reference Discord is the last authority on whether the
        # source still exists.  Never persist a plain replacement message if
        # that source disappeared between transcript validation and send.
        if reference is not None:
            raise ValueError("sgl_case_reply_unavailable") from exc
        raise ValueError("sgl_case_message_delivery_failed") from exc
    return await asyncio.to_thread(
        storage.record_sgl_case_message,
        case=case,
        origin="web",
        discord_message_id=int(sent.id),
        author_id=int(author_id),
        author_display=str(author_display),
        author_avatar_url=None,
        author_is_bot=False,
        content=clean_content,
        attachments=discord_attachments(sent.attachments),
        reply_to_discord_message_id=reply_to_discord_message_id,
        created_at=sent.created_at.isoformat(),
    )


__all__ = [
    "SGL_WEB_ATTACHMENT_COUNT",
    "SGL_WEB_ATTACHMENT_LIMIT",
    "SGL_WEB_MESSAGE_LIMIT",
    "can_view_case",
    "can_write_case",
    "capture_discord_case_message",
    "discord_attachments",
    "mark_discord_case_message_deleted",
    "send_web_case_message",
    "update_discord_case_message",
]
