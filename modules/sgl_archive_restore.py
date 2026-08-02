"""Read-only replay of durable SGL case transcripts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord.ext import commands

from modules.sgl_archive import (
    SGBUREAU_ARCHIVE_CATEGORY_ID,
    SGBUREAU_ARCHIVE_COLOR,
    SGBUREAU_RESTORE_TTL_HOURS,
    SGBUREAU_STAFF_ROLE_ID,
    _json_dict,
    _json_list,
    _parse_datetime,
    _resolve_category,
    verify_archive_files,
)
from modules.technical_log import log_technical_event
from persistence import bureau_context as storage


_RESTORE_TASKS: dict[int, asyncio.Task[None]] = {}


def restoration_is_running(restoration_id: int) -> bool:
    task = _RESTORE_TASKS.get(int(restoration_id))
    return task is not None and not task.done()


async def _restore_overwrites(
    guild: discord.Guild, archive: storage.SGLCaseArchive
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    metadata = _json_dict(archive.metadata_json)
    result: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
    for item in metadata.get("permission_overwrites", []):
        if not isinstance(item, dict):
            continue
        target_id = int(item.get("target_id") or 0)
        target: discord.abc.Snowflake | None
        if item.get("target_type") == "role":
            target = guild.get_role(target_id)
        else:
            target = guild.get_member(target_id)
            if target is None and target_id:
                try:
                    target = await guild.fetch_member(target_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    target = None
        if target is None:
            continue
        overwrite = discord.PermissionOverwrite.from_pair(
            discord.Permissions(int(item.get("allow") or 0)),
            discord.Permissions(int(item.get("deny") or 0)),
        )
        overwrite.send_messages = False
        overwrite.add_reactions = False
        overwrite.create_public_threads = False
        overwrite.create_private_threads = False
        overwrite.send_messages_in_threads = False
        result[target] = overwrite

    everyone = result.get(guild.default_role, discord.PermissionOverwrite())
    everyone.view_channel = False
    everyone.send_messages = False
    result[guild.default_role] = everyone

    staff = guild.get_role(SGBUREAU_STAFF_ROLE_ID)
    if staff is not None:
        existing = result.get(staff, discord.PermissionOverwrite())
        existing.view_channel = True
        existing.read_message_history = True
        existing.send_messages = False
        result[staff] = existing
    if guild.me is not None:
        result[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=True,
            embed_links=True,
            attach_files=True,
            manage_channels=True,
            manage_webhooks=True,
            manage_threads=True,
            create_public_threads=True,
            send_messages_in_threads=True,
        )
    return result


def _restore_channel_name(case_number: int) -> str:
    return f"{int(case_number):03d}-📦-восстановлен"


def _restore_header(
    archive: storage.SGLCaseArchive, restoration: storage.SGLArchiveRestoration
) -> discord.Embed:
    expires = _parse_datetime(restoration.expires_at)
    timestamp = int(expires.timestamp()) if expires else 0
    embed = discord.Embed(
        title=f"📦 Восстановленный архив кейса {archive.case_number:03d}",
        description=(
            "Это неизменяемая копия удалённого канала. Упоминания отключены, "
            "старые кнопки не активируются.\n\n"
            f"**Исходный канал:** `{archive.original_channel_name}`\n"
            f"**Сообщений:** {archive.message_count}\n"
            f"**Вложений:** {archive.attachment_count}\n"
            f"**Временный канал исчезнет:** <t:{timestamp}:R>"
        ),
        color=SGBUREAU_ARCHIVE_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="T-Mod • защищённый архив SGL")
    return embed


def _message_username(message: storage.SGLArchiveMessage) -> str:
    created = _parse_datetime(message.created_at)
    stamp = created.strftime("%d.%m.%Y %H:%M UTC") if created else "архив"
    display = message.author_display or message.author_name or "Неизвестный автор"
    return f"{display} • {stamp}"[:80]


def _message_notes(message: storage.SGLArchiveMessage) -> str:
    notes: list[str] = []
    reactions = _json_list(message.reactions_json)
    if reactions:
        notes.append(
            "Реакции: "
            + "  ".join(
                f"{item.get('emoji', '?')} ×{int(item.get('count') or 0)}"
                for item in reactions
            )
        )
    stickers = _json_list(message.stickers_json)
    if stickers:
        notes.append(
            "Стикеры: "
            + ", ".join(str(item.get("name") or item.get("id")) for item in stickers)
        )
    if message.reference_message_id:
        notes.append(f"Ответ на исходное сообщение `{message.reference_message_id}`")
    if _json_list(message.components_json):
        notes.append("Интерактивные элементы сохранены в снимке и отключены")
    return "\n".join(notes)


def _attachment_batches(
    attachments: list[dict[str, Any]], *, max_bytes: int
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    skipped: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_size = 0
    for attachment in attachments:
        size = int(attachment.get("size") or 0)
        if size > max_bytes:
            skipped.append(attachment)
            continue
        if current and (len(current) >= 10 or current_size + size > max_bytes):
            batches.append(current)
            current = []
            current_size = 0
        current.append(attachment)
        current_size += size
    if current:
        batches.append(current)
    return batches, skipped


def _discord_files(batch: list[dict[str, Any]]) -> list[discord.File]:
    root = storage.sgl_archive_root().resolve()
    files: list[discord.File] = []
    try:
        for attachment in batch:
            path = (root / str(attachment.get("local_path") or "")).resolve()
            if root not in path.parents or not path.is_file():
                raise FileNotFoundError(path)
            files.append(
                discord.File(
                    path,
                    filename=str(attachment.get("filename") or path.name),
                    spoiler=bool(attachment.get("spoiler")),
                    description=attachment.get("description"),
                )
            )
        return files
    except Exception:
        for file in files:
            file.close()
        raise


async def _send_restored_payload(
    *,
    webhook: discord.Webhook | None,
    target: discord.TextChannel | discord.Thread,
    message: storage.SGLArchiveMessage,
    guild: discord.Guild,
) -> None:
    embeds: list[discord.Embed] = []
    for payload in _json_list(message.embeds_json)[:10]:
        try:
            embeds.append(discord.Embed.from_dict(payload))
        except Exception:
            continue
    attachments = _json_list(message.attachments_json)
    batches, skipped = _attachment_batches(
        attachments, max_bytes=max(1, int(guild.filesize_limit))
    )
    if not batches:
        batches = [[]]
    notes = _message_notes(message)
    if skipped:
        skipped_names = ", ".join(
            str(item.get("filename") or "file") for item in skipped
        )
        notes = (notes + "\n" if notes else "") + (
            "Слишком большие для повторной загрузки файлы сохранены на сервере: "
            + skipped_names
        )

    username = _message_username(message)
    content = message.content or None
    avatar_url = message.author_avatar_url
    for index, batch in enumerate(batches):
        files = _discord_files(batch)
        try:
            first = index == 0
            send_content = content if first else "Вложения к предыдущему сообщению"
            send_embeds = embeds if first else []
            if first and not send_content and not send_embeds and not files:
                send_content = "\u200b"
            if webhook is not None:
                kwargs: dict[str, Any] = {
                    "content": send_content,
                    "username": username,
                    "avatar_url": avatar_url,
                    "embeds": send_embeds,
                    "files": files,
                    "allowed_mentions": discord.AllowedMentions.none(),
                    "wait": True,
                }
                if isinstance(target, discord.Thread):
                    kwargs["thread"] = target
                await webhook.send(**kwargs)
            else:
                prefix = f"**{username}**"
                if send_content:
                    combined = f"{prefix}\n{send_content}"
                    if len(combined) <= 2000:
                        await target.send(
                            combined,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    else:
                        await target.send(
                            prefix,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        await target.send(
                            send_content,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                elif not send_embeds and not files:
                    await target.send(
                        prefix,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                if send_embeds or files:
                    await target.send(
                        embeds=send_embeds,
                        files=files,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
        finally:
            for file in files:
                file.close()

    if notes:
        await target.send(
            f"-# {notes[:1900]}", allowed_mentions=discord.AllowedMentions.none()
        )


async def _complete_restoration(
    bot: commands.Bot,
    guild: discord.Guild,
    channel: discord.TextChannel,
    archive: storage.SGLCaseArchive,
    restoration: storage.SGLArchiveRestoration,
) -> None:
    webhook: discord.Webhook | None = None
    try:
        messages = await asyncio.to_thread(
            storage.list_sgl_archive_messages,
            archive.id,
        )
        await verify_archive_files(archive)
        try:
            webhook = await channel.create_webhook(
                name="T-Mod • восстановление SGL",
                reason=f"Restore SGL case {archive.case_number:03d}",
            )
        except (discord.Forbidden, discord.HTTPException):
            webhook = None

        thread_targets: dict[int, discord.Thread | discord.TextChannel] = {}
        for message in messages:
            target: discord.TextChannel | discord.Thread = channel
            if message.container_type == "thread":
                cached = thread_targets.get(message.container_id)
                if cached is None:
                    try:
                        cached = await channel.create_thread(
                            name=(message.container_name or "архивная-ветка")[:100],
                            type=discord.ChannelType.public_thread,
                            reason=f"Restore thread for SGL case {archive.case_number:03d}",
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        cached = channel
                        await channel.send(
                            f"### 🧵 Ветка: {message.container_name}",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    thread_targets[message.container_id] = cached
                target = cached
            await _send_restored_payload(
                webhook=webhook, target=target, message=message, guild=guild
            )

        await asyncio.to_thread(
            storage.mark_sgl_archive_restoration_complete,
            restoration.id,
        )
        await channel.send(
            embed=discord.Embed(
                title="✅ Восстановление завершено",
                description=(
                    f"Воспроизведено сообщений: **{archive.message_count}**. "
                    "Исходный снимок остаётся в постоянном хранилище."
                ),
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception as exc:
        await asyncio.to_thread(
            storage.mark_sgl_archive_restoration_error,
            restoration.id,
            f"{type(exc).__name__}: {exc}",
        )
        try:
            await channel.send(
                embed=discord.Embed(
                    title="🔴 Восстановление прервано",
                    description=(
                        "Архив не повреждён. Ошибка передана в технический журнал; "
                        "канал можно удалить и повторить команду."
                    ),
                    color=discord.Color.red(),
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass
        await log_technical_event(
            bot,
            guild,
            title="Ошибка восстановления кейса SGL",
            details=(
                f"Кейс: **{archive.case_number:03d}**\n"
                f"Канал: <#{channel.id}>\n"
                f"Ошибка: `{type(exc).__name__}: {str(exc)[:1500]}`"
            ),
            dedupe_key=f"sgl-restore:{archive.id}:{type(exc).__name__}",
            cooldown_seconds=900,
        )
    finally:
        _RESTORE_TASKS.pop(restoration.id, None)
        if webhook is not None:
            try:
                await webhook.delete(reason="SGL transcript restoration finished")
            except discord.HTTPException:
                pass


async def start_case_restoration(
    bot: commands.Bot,
    guild: discord.Guild,
    archive: storage.SGLCaseArchive,
    actor: discord.Member,
) -> tuple[discord.TextChannel, bool]:
    active = await asyncio.to_thread(
        storage.get_active_sgl_archive_restoration,
        archive.id,
    )
    if active is not None:
        existing = guild.get_channel(active.restored_channel_id)
        if isinstance(existing, discord.TextChannel):
            running = _RESTORE_TASKS.get(active.id)
            if active.status == "complete" or (running is not None and not running.done()):
                return existing, False
            # The process was restarted while replaying. A partially replayed
            # channel is discarded so a retry never mixes duplicate messages.
            try:
                await existing.delete(reason="Replace interrupted SGL restoration")
            except discord.NotFound:
                pass
            await asyncio.to_thread(
                storage.mark_sgl_archive_restoration_deleted,
                active.id,
            )
        else:
            await asyncio.to_thread(
                storage.mark_sgl_archive_restoration_deleted,
                active.id,
            )

    category = await _resolve_category(guild, bot, SGBUREAU_ARCHIVE_CATEGORY_ID)
    if category is None:
        raise RuntimeError("sgl_archive_category_missing")
    overwrites = await _restore_overwrites(guild, archive)
    channel = await guild.create_text_channel(
        name=_restore_channel_name(archive.case_number),
        category=category,
        overwrites=overwrites,
        topic=(
            f"RESTORED_SGL_ARCHIVE:{archive.id} • case {archive.case_number:03d} • "
            f"read-only temporary transcript"
        )[:1024],
        reason=f"Restore archived SGL case {archive.case_number:03d} by {actor}",
    )
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=SGBUREAU_RESTORE_TTL_HOURS)
    ).isoformat()
    try:
        restoration = await asyncio.to_thread(
            storage.create_sgl_archive_restoration,
            archive_id=archive.id,
            guild_id=guild.id,
            restored_channel_id=channel.id,
            restored_by_id=actor.id,
            restored_by_display=actor.display_name,
            expires_at=expires_at,
        )
    except Exception:
        await channel.delete(reason="Failed to persist SGL restoration")
        raise
    if restoration.restored_channel_id != channel.id:
        await channel.delete(reason="Duplicate SGL restoration request")
        existing = guild.get_channel(restoration.restored_channel_id)
        if not isinstance(existing, discord.TextChannel):
            raise RuntimeError("sgl_archive_restoration_race")
        return existing, False

    try:
        await channel.send(
            embed=_restore_header(archive, restoration),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception as exc:
        await asyncio.to_thread(
            storage.mark_sgl_archive_restoration_error,
            restoration.id,
            f"header_failed:{type(exc).__name__}:{exc}",
        )
        try:
            await channel.delete(reason="SGL restoration could not start")
        finally:
            await asyncio.to_thread(
                storage.mark_sgl_archive_restoration_deleted,
                restoration.id,
            )
        raise
    task = bot.loop.create_task(
        _complete_restoration(bot, guild, channel, archive, restoration)
    )
    _RESTORE_TASKS[restoration.id] = task
    return channel, True


__all__ = [
    "restoration_is_running",
    "start_case_restoration",
]
