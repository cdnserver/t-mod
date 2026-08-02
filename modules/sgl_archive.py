"""Safe Discord transcript archiving and temporary restoration for SGL cases."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import discord
from discord import app_commands
from discord.ext import commands

from localization import safe_command_description, safe_command_name
from modules.technical_log import log_technical_event
from persistence import bureau_context as storage


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on", "да"}


SGBUREAU_CATEGORY_ID = _env_int("SGBUREAU_CATEGORY_ID", 1500836076120576000)
SGBUREAU_COMMAND_CHANNEL_ID = _env_int(
    "SGBUREAU_COMMAND_CHANNEL_ID", 1500837640067485717
)
SGBUREAU_STAFF_ROLE_ID = _env_int("SGBUREAU_STAFF_ROLE_ID", 1500488424191295518)
SGBUREAU_ARCHIVE_CATEGORY_ID = _env_int(
    "SGBUREAU_ARCHIVE_CATEGORY_ID", 1505657602774798447
)
SGBUREAU_ARCHIVE_RETENTION_DAYS = max(
    1, _env_int("SGBUREAU_ARCHIVE_RETENTION_DAYS", 14)
)
SGBUREAU_ARCHIVE_INITIAL_PURGE = _env_bool(
    "SGBUREAU_ARCHIVE_INITIAL_PURGE", True
)
SGBUREAU_ARCHIVE_MAINTENANCE_SECONDS = max(
    300, _env_int("SGBUREAU_ARCHIVE_MAINTENANCE_SECONDS", 3600)
)
SGBUREAU_RESTORE_TTL_HOURS = max(1, _env_int("SGBUREAU_RESTORE_TTL_HOURS", 24))
SGBUREAU_ARCHIVE_COLOR = 0xD9D9D9

SG_RESTORE_COMMAND_NAME = safe_command_name(
    "sgbureau.commands.sg_restore_name", "sg_restore"
)
SG_RESTORE_COMMAND_DESCRIPTION = safe_command_description(
    "sgbureau.commands.sg_restore_description", "Restore an archived SGL case"
)

_CASE_NUMBER_RE = re.compile(r"(?<!\d)(\d{1,9})(?!\d)")
_MAINTENANCE_TASKS: dict[int, asyncio.Task[None]] = {}


def parse_case_number_from_channel(name: str, topic: str | None = None) -> int | None:
    for value in (name, topic or ""):
        match = _CASE_NUMBER_RE.search(str(value))
        if match:
            number = int(match.group(1))
            if number > 0:
                return number
    return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def archive_due(archived_at: str | None, *, now: datetime | None = None) -> bool:
    archived = _parse_datetime(archived_at)
    if archived is None:
        return False
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return current - archived >= timedelta(days=SGBUREAU_ARCHIVE_RETENTION_DAYS)


def _safe_filename(value: str, fallback: str) -> str:
    name = Path(str(value or "")).name.strip()
    name = re.sub(r"[^\w.()\[\] -]+", "_", name, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name).strip("._")
    return (name or fallback)[:120]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_has_content(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _replace_file(source: Path, target: Path) -> None:
    source.replace(target)


def _attachment_file_state(path: Path) -> tuple[Path, int]:
    return path.resolve(), path.stat().st_size


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _json_list(value: str) -> list[dict[str, Any]]:
    try:
        result = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


def _json_dict(value: str) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _is_bureau_staff(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or any(
        role.id == SGBUREAU_STAFF_ROLE_ID for role in member.roles
    )


async def _resolve_category(
    guild: discord.Guild, bot: commands.Bot, category_id: int
) -> discord.CategoryChannel | None:
    channel = guild.get_channel(category_id)
    if isinstance(channel, discord.CategoryChannel):
        return channel
    try:
        fetched = await bot.fetch_channel(category_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return fetched if isinstance(fetched, discord.CategoryChannel) else None


def _serialise_overwrites(channel: discord.TextChannel) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        result.append(
            {
                "target_id": int(target.id),
                "target_type": "role" if isinstance(target, discord.Role) else "member",
                "allow": int(allow.value),
                "deny": int(deny.value),
            }
        )
    return result


def _serialise_components(message: discord.Message) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for component in getattr(message, "components", []) or []:
        to_dict = getattr(component, "to_dict", None)
        if callable(to_dict):
            try:
                payload = to_dict()
                if isinstance(payload, dict):
                    result.append(payload)
            except Exception:
                continue
    return result


async def _archive_attachment(
    attachment: discord.Attachment,
    *,
    message_id: int,
    directory: Path,
) -> dict[str, Any]:
    clean_name = _safe_filename(attachment.filename, f"attachment-{attachment.id}")
    filename = f"{int(message_id)}-{int(attachment.id)}-{clean_name}"
    target = directory / filename
    expected_size = int(attachment.size or 0)
    download_variant = "existing"

    # Discord's media proxy may transcode images, so its byte count can differ
    # from Attachment.size and some assets return 415 through the proxy. The
    # original CDN URL is the canonical source; the proxy is only a fallback
    # for older assets whose original URL is no longer available.
    if not await asyncio.to_thread(_file_has_content, target):
        temporary = target.with_suffix(target.suffix + ".part")
        failures: list[str] = []
        for use_cached, variant in ((False, "original_cdn"), (True, "media_proxy")):
            await asyncio.to_thread(temporary.unlink, missing_ok=True)
            try:
                await attachment.save(temporary, use_cached=use_cached)
                if not await asyncio.to_thread(_file_has_content, temporary):
                    raise IOError("empty_attachment_response")
                await asyncio.to_thread(_replace_file, temporary, target)
                download_variant = variant
                break
            except Exception as exc:
                await asyncio.to_thread(temporary.unlink, missing_ok=True)
                failures.append(f"{variant}:{type(exc).__name__}:{str(exc)[:300]}")
        else:
            raise IOError(
                f"attachment_download_failed:{attachment.id}:" + " | ".join(failures)
            )

    root = (await asyncio.to_thread(storage.sgl_archive_root)).resolve()
    resolved, stored_size = await asyncio.to_thread(_attachment_file_state, target)
    if root not in resolved.parents:
        raise ValueError("sgl_archive_attachment_path_escape")
    sha256 = await asyncio.to_thread(_hash_file, target)
    return {
        "id": int(attachment.id),
        "filename": str(attachment.filename),
        "content_type": attachment.content_type,
        "description": getattr(attachment, "description", None),
        "size": int(stored_size),
        "discord_reported_size": expected_size,
        "download_variant": download_variant,
        "spoiler": bool(attachment.is_spoiler()),
        "local_path": resolved.relative_to(root).as_posix(),
        "sha256": sha256,
    }


async def _capture_message(
    message: discord.Message,
    *,
    container_type: str,
    container_name: str,
    directory: Path,
) -> dict[str, Any]:
    attachments = [
        await _archive_attachment(
            attachment, message_id=message.id, directory=directory
        )
        for attachment in message.attachments
    ]
    avatar = getattr(message.author, "display_avatar", None)
    reference = getattr(message, "reference", None)
    return {
        "original_message_id": int(message.id),
        "container_id": int(message.channel.id),
        "container_type": str(container_type),
        "container_name": str(container_name),
        "author_id": getattr(message.author, "id", None),
        "author_name": str(getattr(message.author, "name", "") or ""),
        "author_display": str(
            getattr(message.author, "display_name", None)
            or getattr(message.author, "name", "")
            or "Неизвестный автор"
        ),
        "author_avatar_url": str(getattr(avatar, "url", "") or "") or None,
        "author_is_bot": bool(getattr(message.author, "bot", False)),
        "content": str(message.content or ""),
        "embeds": [embed.to_dict() for embed in message.embeds],
        "attachments": attachments,
        "stickers": [
            {
                "id": int(sticker.id),
                "name": str(sticker.name),
                "format": str(getattr(sticker, "format", "")),
                "url": str(getattr(sticker, "url", "") or ""),
            }
            for sticker in getattr(message, "stickers", []) or []
        ],
        "reactions": [
            {
                "emoji": str(reaction.emoji),
                "count": int(reaction.count),
                "me": bool(reaction.me),
            }
            for reaction in message.reactions
        ],
        "components": _serialise_components(message),
        "reference_message_id": getattr(reference, "message_id", None),
        "created_at": message.created_at.astimezone(timezone.utc).isoformat(),
        "edited_at": (
            message.edited_at.astimezone(timezone.utc).isoformat()
            if message.edited_at
            else None
        ),
        "pinned": bool(message.pinned),
    }


async def _discover_threads(
    channel: discord.TextChannel,
) -> tuple[list[discord.Thread], list[str]]:
    threads: dict[int, discord.Thread] = {
        thread.id: thread for thread in channel.threads
    }
    warnings: list[str] = []
    requests = ({}, {"private": True, "joined": False})
    for options in requests:
        try:
            async for thread in channel.archived_threads(limit=None, **options):
                threads[thread.id] = thread
        except (discord.Forbidden, discord.HTTPException) as exc:
            kind = "private" if options else "public"
            raise RuntimeError(
                f"{kind}_archived_threads_unavailable:{type(exc).__name__}:{exc}"
            ) from exc
    return sorted(threads.values(), key=lambda item: item.id), warnings


async def _ensure_complete_history_permissions(channel: discord.TextChannel) -> None:
    guild = channel.guild
    me = guild.me
    if me is None:
        raise PermissionError("sgl_archive_bot_member_unavailable")
    permissions = channel.permissions_for(me)
    if not permissions.manage_threads and permissions.manage_channels:
        overwrite = channel.overwrites_for(me)
        overwrite.view_channel = True
        overwrite.read_message_history = True
        overwrite.manage_threads = True
        await channel.set_permissions(
            me,
            overwrite=overwrite,
            reason="SGL transcript capture requires complete thread history",
        )
        permissions = channel.permissions_for(me)
    if not permissions.view_channel or not permissions.read_message_history:
        raise PermissionError("sgl_archive_missing_read_history_permission")
    if not permissions.manage_threads:
        raise PermissionError("sgl_archive_missing_manage_threads_permission")


async def capture_case_channel(
    channel: discord.TextChannel,
    *,
    case: storage.SGLCase | None,
    case_number: int,
) -> storage.SGLCaseArchive:
    guild = channel.guild
    await _ensure_complete_history_permissions(channel)

    started_at = datetime.now(timezone.utc).isoformat()
    directory = await asyncio.to_thread(
        storage.sgl_archive_case_directory,
        guild.id,
        case_number,
    )
    messages: list[dict[str, Any]] = []
    seen_message_ids: set[int] = set()
    containers: list[dict[str, Any]] = [
        {"id": channel.id, "type": "channel", "name": channel.name}
    ]

    async for message in channel.history(limit=None, oldest_first=True):
        if message.id in seen_message_ids:
            continue
        seen_message_ids.add(message.id)
        messages.append(
            await _capture_message(
                message,
                container_type="channel",
                container_name=channel.name,
                directory=directory,
            )
        )

    threads, warnings = await _discover_threads(channel)
    for thread in threads:
        containers.append({"id": thread.id, "type": "thread", "name": thread.name})
        async for message in thread.history(limit=None, oldest_first=True):
            # A thread starter can also appear in the parent channel history.
            # Discord message snowflakes are globally unique, so archive it once.
            if message.id in seen_message_ids:
                continue
            seen_message_ids.add(message.id)
            messages.append(
                await _capture_message(
                    message,
                    container_type="thread",
                    container_name=thread.name,
                    directory=directory,
                )
            )

    metadata = {
        "format_version": 1,
        "guild_name": guild.name,
        "channel_nsfw": bool(channel.nsfw),
        "channel_slowmode_delay": int(channel.slowmode_delay),
        "channel_position": int(channel.position),
        "permission_overwrites": _serialise_overwrites(channel),
        "containers": containers,
        "capture_warnings": warnings,
    }
    archive = await asyncio.to_thread(
        storage.save_sgl_case_archive_snapshot,
        guild_id=guild.id,
        case_id=case.id if case else None,
        case_number=case_number,
        original_channel_id=channel.id,
        original_channel_name=channel.name,
        original_topic=channel.topic,
        original_category_id=channel.category_id,
        metadata=metadata,
        messages=messages,
        snapshot_started_at=started_at,
    )
    manifest = {
        "archive": {
            "guild_id": guild.id,
            "case_id": case.id if case else None,
            "case_number": case_number,
            "original_channel_id": channel.id,
            "original_channel_name": channel.name,
            "original_topic": channel.topic,
            "captured_at": archive.snapshot_completed_at,
        },
        "metadata": metadata,
        "messages": messages,
    }
    await asyncio.to_thread(_write_json_atomic, directory / "manifest.json", manifest)
    await verify_archive_files(archive, messages=messages)
    return archive


def validate_archive_files(
    archive: storage.SGLCaseArchive,
    *,
    messages: Iterable[dict[str, Any]] | None = None,
) -> None:
    root = storage.sgl_archive_root().resolve()
    source_messages = list(messages) if messages is not None else [
        {"attachments": _json_list(item.attachments_json)}
        for item in storage.list_sgl_archive_messages(archive.id)
    ]
    if len(source_messages) != archive.message_count:
        raise ValueError(
            f"sgl_archive_message_count_mismatch:{len(source_messages)}!={archive.message_count}"
        )
    attachment_count = 0
    total_bytes = 0
    for message in source_messages:
        for attachment in message.get("attachments") or []:
            attachment_count += 1
            relative = Path(str(attachment.get("local_path") or ""))
            path = (root / relative).resolve()
            if root not in path.parents or not path.is_file():
                raise FileNotFoundError(f"sgl_archive_attachment_missing:{relative}")
            size = int(attachment.get("size") or 0)
            if path.stat().st_size != size:
                raise ValueError(f"sgl_archive_attachment_size_mismatch:{relative}")
            total_bytes += size
    if attachment_count != archive.attachment_count:
        raise ValueError("sgl_archive_attachment_count_mismatch")
    if total_bytes != archive.total_bytes:
        raise ValueError("sgl_archive_total_bytes_mismatch")


async def verify_archive_files(
    archive: storage.SGLCaseArchive,
    *,
    messages: Iterable[dict[str, Any]] | None = None,
) -> None:
    """Verify structure synchronously and hashes without blocking Discord heartbeats."""

    if messages is not None:
        source_messages = list(messages)
    else:
        stored_messages = await asyncio.to_thread(
            storage.list_sgl_archive_messages,
            archive.id,
        )
        source_messages = [
            {"attachments": _json_list(item.attachments_json)}
            for item in stored_messages
        ]
    validate_archive_files(archive, messages=source_messages)
    root = (await asyncio.to_thread(storage.sgl_archive_root)).resolve()
    for message in source_messages:
        for attachment in message.get("attachments") or []:
            expected = str(attachment.get("sha256") or "")
            if not expected:
                raise ValueError("sgl_archive_attachment_hash_missing")
            path = (root / str(attachment.get("local_path") or "")).resolve()
            actual = await asyncio.to_thread(_hash_file, path)
            if actual != expected:
                raise ValueError(
                    f"sgl_archive_attachment_hash_mismatch:{attachment.get('local_path')}"
                )


async def snapshot_and_delete_case_channel(
    bot: commands.Bot,
    channel: discord.TextChannel,
    *,
    case: storage.SGLCase | None,
    case_number: int,
) -> storage.SGLCaseArchive:
    archive = await asyncio.to_thread(
        storage.get_sgl_case_archive_by_source,
        channel.guild.id,
        channel.id,
    )
    try:
        if archive is None or archive.status not in {"ready", "sealed"}:
            archive = await capture_case_channel(
                channel, case=case, case_number=case_number
            )
        else:
            await verify_archive_files(archive)
        if archive.source_deleted_at is not None:
            raise ValueError("sgl_archive_source_already_deleted_but_channel_exists")

        try:
            await channel.delete(
                reason=(
                    f"SGL case {case_number:03d}: transcript saved; "
                    f"Discord archive retention expired"
                )
            )
        except discord.NotFound:
            pass
        sealed = await asyncio.to_thread(
            storage.mark_sgl_archive_source_deleted,
            archive.id,
        )
        if sealed is None:
            raise RuntimeError("sgl_archive_seal_failed")
        return sealed
    except Exception as exc:
        if archive is not None:
            await asyncio.to_thread(
                storage.mark_sgl_archive_error,
                archive.id,
                f"{type(exc).__name__}: {exc}",
            )
        await log_technical_event(
            bot,
            channel.guild,
            title="Ошибка архивации кейса SGL",
            details=(
                f"Кейс: **{case_number:03d}**\n"
                f"Канал: `{channel.name}` (`{channel.id}`)\n"
                f"Канал не удалён. Ошибка: `{type(exc).__name__}: {str(exc)[:1200]}`"
            ),
            dedupe_key=f"sgl-archive:{channel.id}:{type(exc).__name__}",
            cooldown_seconds=900,
        )
        raise


async def _reconcile_deleted_sources(bot: commands.Bot, guild: discord.Guild) -> None:
    archives = await asyncio.to_thread(
        storage.list_sgl_archives_pending_source_reconcile,
        guild.id,
    )
    for archive in archives:
        cached = guild.get_channel(archive.original_channel_id)
        if cached is not None:
            continue
        try:
            await bot.fetch_channel(archive.original_channel_id)
        except discord.NotFound:
            await asyncio.to_thread(
                storage.mark_sgl_archive_source_deleted,
                archive.id,
            )
        except (discord.Forbidden, discord.HTTPException):
            # A permission problem or a transient Discord failure is not proof
            # that the source channel was deleted.
            continue


async def _purge_expired_restorations(bot: commands.Bot, guild: discord.Guild) -> None:
    from modules.sgl_archive_restore import restoration_is_running

    restorations = await asyncio.to_thread(
        storage.list_expired_sgl_archive_restorations,
    )
    for restoration in restorations:
        if restoration.guild_id != guild.id:
            continue
        if restoration_is_running(restoration.id):
            continue
        channel = guild.get_channel(restoration.restored_channel_id)
        try:
            if isinstance(channel, discord.TextChannel):
                await channel.delete(reason="SGL restored transcript access window expired")
            elif channel is None:
                try:
                    fetched = await bot.fetch_channel(restoration.restored_channel_id)
                    if isinstance(fetched, discord.TextChannel):
                        await fetched.delete(
                            reason="SGL restored transcript access window expired"
                        )
                except discord.NotFound:
                    pass
            await asyncio.to_thread(
                storage.mark_sgl_archive_restoration_deleted,
                restoration.id,
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await log_technical_event(
                bot,
                guild,
                title="Не удалось убрать восстановленный кейс SGL",
                details=(
                    f"Канал: `{restoration.restored_channel_id}`\n"
                    f"Ошибка: `{type(exc).__name__}: {str(exc)[:1000]}`"
                ),
                dedupe_key=f"sgl-restore-cleanup:{restoration.restored_channel_id}",
                cooldown_seconds=1800,
            )


async def run_sgl_archive_maintenance_once(bot: commands.Bot) -> None:
    for guild in bot.guilds:
        await _reconcile_deleted_sources(bot, guild)
        await _purge_expired_restorations(bot, guild)
        category = await _resolve_category(guild, bot, SGBUREAU_ARCHIVE_CATEGORY_ID)
        if category is None:
            await log_technical_event(
                bot,
                guild,
                title="Категория архива SGL недоступна",
                details=f"Ожидалась категория `{SGBUREAU_ARCHIVE_CATEGORY_ID}`.",
                dedupe_key="sgl-archive-category-missing",
                cooldown_seconds=3600,
            )
            continue

        backfill_complete = await asyncio.to_thread(
            storage.is_sgl_archive_backfill_complete,
            guild.id,
            category.id,
        )
        initial = SGBUREAU_ARCHIVE_INITIAL_PURGE and not backfill_complete
        failures = 0
        deleted = 0
        for channel in list(category.text_channels):
            if await asyncio.to_thread(
                storage.get_active_sgl_restoration_by_channel,
                guild.id,
                channel.id,
            ):
                continue
            case = await asyncio.to_thread(
                storage.get_sgl_case_by_channel,
                guild.id,
                channel.id,
            )
            if not initial and (case is None or not archive_due(case.archived_at)):
                continue
            case_number = (
                case.case_number
                if case is not None
                else parse_case_number_from_channel(channel.name, channel.topic)
            )
            if case_number is None:
                failures += 1
                await log_technical_event(
                    bot,
                    guild,
                    title="Не распознан номер архивного кейса SGL",
                    details=(
                        f"Канал: `{channel.name}` (`{channel.id}`). "
                        "Он оставлен на месте; добавьте номер кейса в название."
                    ),
                    dedupe_key=f"sgl-archive-no-number:{channel.id}",
                    cooldown_seconds=3600,
                )
                continue
            try:
                await snapshot_and_delete_case_channel(
                    bot, channel, case=case, case_number=case_number
                )
                deleted += 1
                await asyncio.sleep(0.5)
            except Exception:
                failures += 1

        if initial and failures == 0:
            await asyncio.to_thread(
                storage.mark_sgl_archive_backfill_complete,
                guild.id,
                category.id,
            )
            await log_technical_event(
                bot,
                guild,
                title="Первичный архив кейсов SGL завершён",
                details=(
                    f"Сохранено и удалено Discord-каналов: **{deleted}**.\n"
                    "Дальше каналы будут очищаться через "
                    f"**{SGBUREAU_ARCHIVE_RETENTION_DAYS} дней**."
                ),
                level="info",
                dedupe_key=f"sgl-archive-backfill-complete:{category.id}",
                cooldown_seconds=86400,
            )


async def _maintenance_loop(bot: commands.Bot) -> None:
    try:
        while not bot.is_closed():
            try:
                await run_sgl_archive_maintenance_once(bot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                for guild in bot.guilds:
                    await log_technical_event(
                        bot,
                        guild,
                        title="Сбой обслуживания архива SGL",
                        details=f"`{type(exc).__name__}: {str(exc)[:1500]}`",
                        dedupe_key=f"sgl-archive-worker:{type(exc).__name__}",
                        cooldown_seconds=1800,
                    )
            await asyncio.sleep(SGBUREAU_ARCHIVE_MAINTENANCE_SECONDS)
    finally:
        _MAINTENANCE_TASKS.pop(id(bot), None)


def _start_maintenance(bot: commands.Bot) -> None:
    key = id(bot)
    task = _MAINTENANCE_TASKS.get(key)
    if task is None or task.done():
        _MAINTENANCE_TASKS[key] = bot.loop.create_task(_maintenance_loop(bot))


def setup_sgl_archive(
    bot: commands.Bot,
    remember_command_activity: Callable[[discord.Interaction, str, str], None],
) -> None:
    @bot.tree.command(
        name=SG_RESTORE_COMMAND_NAME, description=SG_RESTORE_COMMAND_DESCRIPTION
    )
    @app_commands.guild_only()
    @app_commands.rename(case_number="case")
    @app_commands.describe(case_number="Номер архивного кейса SGL")
    async def sg_restore(
        interaction: discord.Interaction,
        case_number: app_commands.Range[int, 1, 999999999],
    ) -> None:
        remember_command_activity(interaction, "command_sg_restore", "/sg_restore")
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Команда доступна только на сервере.", ephemeral=True
            )
            return
        if interaction.channel_id != SGBUREAU_COMMAND_CHANNEL_ID:
            await interaction.response.send_message(
                f"Используйте команду в командном центре SGL: <#{SGBUREAU_COMMAND_CHANNEL_ID}>.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not _is_bureau_staff(interaction.user):
            await interaction.response.send_message(
                "Восстанавливать кейсы могут только сотрудники SGL.", ephemeral=True
            )
            return

        archive = await asyncio.to_thread(
            storage.get_sgl_case_archive,
            interaction.guild.id,
            int(case_number),
        )
        if archive is None or archive.source_deleted_at is None:
            await interaction.response.send_message(
                f"Постоянный архив кейса **{int(case_number):03d}** пока не найден.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            from modules.sgl_archive_restore import start_case_restoration

            channel, created = await start_case_restoration(
                bot, interaction.guild, archive, interaction.user
            )
            if created:
                text = (
                    f"Кейс **{archive.case_number:03d}** создан: <#{channel.id}>. "
                    "Сообщения восстанавливаются в фоне. Канал доступен "
                    f"{SGBUREAU_RESTORE_TTL_HOURS} ч."
                )
            else:
                text = (
                    f"Кейс **{archive.case_number:03d}** уже восстановлен: "
                    f"<#{channel.id}>."
                )
            await interaction.followup.send(
                text,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            await interaction.followup.send(
                "Не удалось создать временный канал. "
                f"Архив не изменён. Ошибка: `{type(exc).__name__}: {str(exc)[:500]}`",
                ephemeral=True,
            )

    async def handle_sgl_archive_ready() -> None:
        _start_maintenance(bot)

    bot.add_listener(handle_sgl_archive_ready, "on_ready")


__all__ = [
    "SGBUREAU_ARCHIVE_RETENTION_DAYS",
    "SG_RESTORE_COMMAND_NAME",
    "parse_case_number_from_channel",
    "archive_due",
    "capture_case_channel",
    "validate_archive_files",
    "verify_archive_files",
    "snapshot_and_delete_case_channel",
    "run_sgl_archive_maintenance_once",
    "setup_sgl_archive",
]
