"""Atlas Forum Engine: complaint discovery, tracking and personal alerts."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any

import discord

from modules.global_log_runtime import emit_global_event
from modules.atlas_forum_sync import (
    AtlasForumManualActionRequired,
    AtlasForumSnapshot,
    AtlasForumSyncError,
    AtlasForumSyncRunner,
)
from modules.technical_log import log_technical_event
from persistence import atlas_forum_engine_repository as storage
from persistence import reactor_repository


_SECTION_LABELS = {
    "open": "На рассмотрении",
    "accepted": "Рассмотрена · принята",
    "rejected": "Рассмотрена · отклонена",
}
_EVENT_TITLES = {
    "discovered": "На вашего персонажа найдена жалоба",
    "reply": "В жалобе появился новый ответ",
    "staff_reply": "Администратор ответил на жалобу",
    "updated": "Материалы жалобы изменились",
    "status_changed": "Статус жалобы изменён",
}


def _flag(name: str, default: bool) -> bool:
    return str(os.getenv(name, str(default))).strip().casefold() in {
        "1", "true", "yes", "on",
    }


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True, slots=True)
class AtlasForumEngineConfig:
    enabled: bool = True
    # The open complaint feed is an alerting surface, not a slow indexing job.
    # Five-second scheduler ticks plus a 45-second feed lease keep discovery
    # inside the promised one-minute window without continuously driving Chrome.
    initial_delay_seconds: int = 15
    scheduler_poll_seconds: int = 5
    hydration_batch_size: int = 50
    hot_hydration_batch_size: int = 4
    full_scan_interval_seconds: int = 43_200

    @classmethod
    def from_env(cls) -> "AtlasForumEngineConfig":
        return cls(
            enabled=_flag("ATLAS_FORUM_ENGINE_ENABLED", True),
            initial_delay_seconds=_bounded_int(
                "ATLAS_FORUM_ENGINE_INITIAL_DELAY_SECONDS", 15, 2, 900
            ),
            scheduler_poll_seconds=_bounded_int(
                "ATLAS_FORUM_ENGINE_POLL_SECONDS", 5, 2, 60
            ),
            hydration_batch_size=_bounded_int(
                "ATLAS_FORUM_ENGINE_HYDRATION_BATCH", 50, 5, 150
            ),
            hot_hydration_batch_size=_bounded_int(
                "ATLAS_FORUM_ENGINE_HOT_HYDRATION_BATCH", 4, 1, 12
            ),
            full_scan_interval_seconds=_bounded_int(
                "ATLAS_FORUM_ENGINE_FULL_SCAN_SECONDS", 43_200, 1800, 604_800
            ),
        )


def _snapshot_fingerprint(snapshot: AtlasForumSnapshot) -> str:
    if snapshot.posts:
        text = "\n\0\n".join(
            f"{post.index}|{post.author or ''}|{post.author_role or ''}|"
            f"{post.posted_at or ''}|{post.content}"
            for post in snapshot.posts
        )
    else:
        text = f"{snapshot.title}\n\0\n{snapshot.content}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _matches_character(text: str, static_id: str) -> bool:
    clean = re.sub(r"\s+", "", str(static_id or ""))
    if not clean:
        return False
    if clean.isdigit():
        return bool(re.search(rf"(?<!\d){re.escape(clean)}(?!\d)", text))
    return clean.casefold() in re.sub(r"\s+", "", text).casefold()


def _matched_characters(
    snapshot: AtlasForumSnapshot,
    characters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    # Only the complaint title and initial statement identify the target.
    # Later replies can mention unrelated statics and must not subscribe them.
    identity_text = f"{snapshot.title}\n{snapshot.content}"
    return [
        character
        for character in characters
        if _matches_character(identity_text, str(character.get("static_id") or ""))
    ]


class AtlasForumEngineRunner:
    def __init__(
        self,
        bot: discord.Client,
        guild_id: int,
        forum: AtlasForumSyncRunner,
        *,
        config: AtlasForumEngineConfig | None = None,
    ) -> None:
        self.bot = bot
        self.guild_id = int(guild_id)
        self.forum = forum
        self.config = config or AtlasForumEngineConfig.from_env()
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._closed = False
        self._force_once = False
        self._character_revision = ""

    def trigger(self) -> bool:
        if self._closed or not self.config.enabled:
            return False
        self._force_once = True
        self._wake.set()
        return True

    async def _technical_log(
        self,
        title: str,
        details: str,
        *,
        level: str = "info",
        dedupe_key: str,
        exception: BaseException | None = None,
    ) -> None:
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            return
        await log_technical_event(
            self.bot,
            guild,
            title=title,
            details=details,
            level=level,
            dedupe_key=dedupe_key,
            cooldown_seconds=300,
            exception=exception,
            component="atlas-forum-engine",
        )

    async def _process_snapshots(
        self,
        snapshots: tuple[AtlasForumSnapshot, ...],
        *,
        characters: list[dict[str, Any]],
        notify_by_url: dict[str, bool],
    ) -> dict[str, int]:
        summary = {"hydrated": 0, "matched": 0, "events": 0}
        for snapshot in snapshots:
            complaint = await asyncio.to_thread(
                storage.complaint_by_thread,
                self.guild_id,
                "majestic-rp",
                "phoenix-15",
                snapshot.url,
            )
            if complaint is None:
                continue
            posts = list(snapshot.posts)
            latest = posts[-1] if posts else None
            matched = _matched_characters(snapshot, characters)
            metadata = complaint.get("metadata") if isinstance(complaint.get("metadata"), dict) else {}
            try:
                listed_post_count = int(metadata.get("reply_count") or 0) + 1
            except (TypeError, ValueError):
                listed_post_count = 0
            result = await asyncio.to_thread(
                storage.record_complaint_snapshot,
                int(complaint["id"]),
                content_fingerprint=_snapshot_fingerprint(snapshot),
                first_post_excerpt=snapshot.content,
                latest_post_excerpt=latest.content if latest else snapshot.content,
                latest_post_author=latest.author if latest else snapshot.author,
                latest_post_role=latest.author_role if latest else None,
                latest_post_at=latest.posted_at if latest else snapshot.source_updated_at,
                latest_post_is_staff=bool(latest and latest.is_staff),
                post_count=max(len(posts) if posts else 1, listed_post_count),
                matched_characters=matched,
                notify=bool(notify_by_url.get(snapshot.url, True)),
            )
            summary["hydrated"] += 1
            summary["matched"] += int(result["matched"])
            summary["events"] += 1 if result.get("event_id") else 0
            if result.get("event_id"):
                emit_global_event(
                    {
                        "source_service": "atlas",
                        "source_type": "forum_engine",
                        "event_type": f"forum_complaint_{result.get('event_kind') or 'updated'}",
                        "severity": "info",
                        "guild_id": self.guild_id,
                        "target_type": "forum_complaint",
                        "target_id": str(complaint["id"]),
                        "summary": f"Atlas Forum Engine: {snapshot.title[:220]}",
                        "details": {
                            "section": complaint.get("section_kind"),
                            "matched_users": sorted({int(item["user_id"]) for item in matched}),
                            "post_count": max(len(posts) if posts else 1, listed_post_count),
                        },
                    }
                )
        return summary

    async def _reconcile_existing_subjects(
        self,
        characters: list[dict[str, Any]],
    ) -> int:
        """Link an added account character to already cached complaints once."""

        revision = hashlib.sha256(
            "\0".join(
                sorted(
                    f"{item.get('user_id')}:{item.get('character_id')}:{item.get('static_id')}"
                    for item in characters
                )
            ).encode("utf-8")
        ).hexdigest()
        if revision == self._character_revision:
            return 0
        identities = await asyncio.to_thread(
            storage.complaint_identities,
            self.guild_id,
            limit=25_000,
        )
        linked = 0
        for complaint in identities:
            identity_text = (
                f"{complaint.get('title') or ''}\n"
                f"{complaint.get('first_post_excerpt') or ''}"
            )
            matched = [
                item
                for item in characters
                if _matches_character(identity_text, str(item.get("static_id") or ""))
            ]
            if matched:
                linked += await asyncio.to_thread(
                    storage.reconcile_complaint_subjects,
                    int(complaint["id"]),
                    matched,
                )
        self._character_revision = revision
        return linked

    async def _scan_feed(
        self,
        feed: dict[str, Any],
        *,
        force: bool,
        characters: list[dict[str, Any]],
    ) -> dict[str, Any]:
        claimed = await asyncio.to_thread(storage.claim_monitor_feed, int(feed["id"]))
        if claimed is None:
            return {"status": "skipped"}
        baseline_exists = bool(claimed.get("baseline_completed_at"))
        # Deep archive traversal is deliberate (`force`) because hundreds of
        # Selenium pages would otherwise block the hot feed and make a fresh
        # complaint arrive late. Normal cycles always inspect the newest pages.
        full_scan = force
        max_pages = int(claimed["full_pages"] if full_scan else claimed["hot_pages"])
        try:
            inventory = await self.forum.fetch_inventory(
                str(claimed["root_url"]), max_pages=max_pages
            )
            changed_urls: list[str] = []
            notify_by_url: dict[str, bool] = {}
            created = changed = status_changes = 0
            for entry in inventory.entries:
                result = await asyncio.to_thread(
                    storage.record_complaint_listing,
                    guild_id=self.guild_id,
                    project_code=str(claimed["project_code"]),
                    server_code=str(claimed["server_code"]),
                    section_kind=str(claimed["section_kind"]),
                    thread_url=entry.url,
                    title=entry.title,
                    author=entry.author,
                    listing_fingerprint=entry.fingerprint,
                    locked=entry.locked,
                    metadata={
                        "last_post_author": entry.last_post_author,
                        "last_post_at": entry.last_post_at,
                        "reply_count": entry.reply_count,
                        "sticky": entry.sticky,
                        "monitor_feed": str(claimed["feed_key"]),
                    },
                    # A forced archive backfill must never flood members with
                    # historical complaints which merely became known today.
                    notify=baseline_exists and not full_scan,
                )
                created += 1 if result["created"] else 0
                changed += 1 if result["listing_changed"] else 0
                status_changes += 1 if result["section_changed"] else 0
                if result["created"] or result["listing_changed"]:
                    changed_urls.append(entry.url)
                    notify_by_url[entry.url] = baseline_exists and not full_scan
            # A deep archive backfill may hydrate a large batch. The hot path is
            # intentionally tiny: fresh URLs are first and one slow historical
            # topic cannot hold the shared browser long enough to miss the next
            # minute-long alert window.
            hydration_limit = (
                self.config.hydration_batch_size
                if full_scan
                else self.config.hot_hydration_batch_size
            )
            backlog = await asyncio.to_thread(
                storage.complaints_needing_hydration,
                self.guild_id,
                limit=hydration_limit,
            )
            fetch_urls = list(dict.fromkeys(changed_urls + backlog))[:hydration_limit]
            snapshots: tuple[AtlasForumSnapshot, ...] = ()
            skipped: tuple[str, ...] = ()
            hydrated = {"hydrated": 0, "matched": 0, "events": 0}
            if fetch_urls:
                snapshots, skipped = await self.forum.fetch_threads(fetch_urls)
                hydrated = await self._process_snapshots(
                    snapshots,
                    characters=characters,
                    notify_by_url=notify_by_url,
                )
            stats = {
                "section": str(claimed["section_kind"]),
                "mode": "full" if full_scan else "hot",
                "listing_pages": inventory.listing_pages,
                "inventory_complete": inventory.inventory_complete,
                "topics": len(inventory.entries),
                "created": created,
                "changed": changed,
                "status_changes": status_changes,
                "hydrated": hydrated["hydrated"],
                "matched": hydrated["matched"],
                "events": hydrated["events"],
                "skipped": len(skipped),
            }
            attention = bool(skipped or (full_scan and not inventory.inventory_complete))
            error = (
                f"Не удалось прочитать тем: {len(skipped)}"
                if skipped else
                "Полный обход достиг ограничения страниц"
                if full_scan and not inventory.inventory_complete else None
            )
            emit_global_event(
                {
                    "source_service": "atlas",
                    "source_type": "forum_engine",
                    "event_type": "forum_complaint_feed_scanned",
                    "severity": "warning" if attention else "info",
                    "guild_id": self.guild_id,
                    "target_type": "forum_feed",
                    "target_id": str(claimed["feed_key"]),
                    "summary": f"Сверён раздел жалоб: {claimed['section_kind']}",
                    "details": stats,
                }
            )
            return await asyncio.to_thread(
                storage.finish_monitor_feed,
                int(claimed["id"]),
                stats=stats,
                error=error,
                attention=attention,
                full_scan=full_scan and inventory.inventory_complete,
                baseline_completed=not baseline_exists,
            )
        except (AtlasForumManualActionRequired, AtlasForumSyncError) as exc:
            await asyncio.to_thread(
                storage.finish_monitor_feed,
                int(claimed["id"]),
                stats={"section": str(claimed["section_kind"]), "phase": "forum_read"},
                error=str(exc),
                attention=True,
                full_scan=False,
            )
            await self._technical_log(
                "Atlas Forum Engine ждёт форум",
                f"Раздел: `{claimed['feed_key']}`. Последняя карта жалоб сохранена.",
                level="warning",
                dedupe_key=f"atlas-forum-engine-attention:{claimed['id']}",
                exception=exc,
            )
            return {"status": "attention", "error": str(exc)}
        except Exception as exc:
            await asyncio.to_thread(
                storage.finish_monitor_feed,
                int(claimed["id"]),
                stats={"section": str(claimed["section_kind"]), "phase": "engine"},
                error=f"{type(exc).__name__}: {exc}",
                attention=False,
                full_scan=False,
            )
            await self._technical_log(
                "Ошибка Atlas Forum Engine",
                f"Раздел: `{claimed['feed_key']}`. Повтор будет выполнен автоматически.",
                level="warning",
                dedupe_key=f"atlas-forum-engine-error:{claimed['id']}:{type(exc).__name__}",
                exception=exc,
            )
            return {"status": "error", "error": type(exc).__name__}

    async def _deliver_one(self, item: dict[str, Any]) -> bool:
        event_kind = str(item.get("event_kind") or "updated")
        title = _EVENT_TITLES.get(event_kind, "Жалоба на форуме обновилась")
        status = _SECTION_LABELS.get(str(item.get("section_kind") or ""), "Статус уточняется")
        character = str(item.get("nickname") or f"Статик {item.get('static_id')}")
        body = (
            f"{character} · `{item.get('static_id')}`\n"
            f"{status}\n{str(item.get('title') or 'Жалоба')[:220]}"
        )
        await asyncio.to_thread(
            reactor_repository.reactor_put_notification,
            guild_id=self.guild_id,
            user_id=int(item["user_id"]),
            severity="warning" if str(item.get("section_kind")) == "open" else "info",
            kind="atlas_forum_complaint",
            title=title,
            body=body,
            route=str(item.get("thread_url") or ""),
            source_key=f"atlas-forum:{item['complaint_id']}",
            dedupe_key=f"atlas-forum-event:{item['event_id']}",
        )
        if not bool(item.get("dm_notifications", True)):
            await asyncio.to_thread(
                storage.suppress_complaint_delivery,
                int(item["id"]),
                reason="discord_dm_notifications_disabled",
            )
            return True
        user = self.bot.get_user(int(item["user_id"]))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(item["user_id"]))
            except discord.DiscordException as exc:
                await asyncio.to_thread(
                    storage.mark_complaint_delivery,
                    int(item["id"]), sent=False, error=f"{type(exc).__name__}: {exc}",
                )
                return False
        embed = discord.Embed(
            title=title,
            description=body,
            url=str(item.get("thread_url") or "") or None,
            color=0xF0B96A if str(item.get("section_kind")) == "open" else 0x6FDDB9,
        )
        excerpt = " ".join(str(item.get("excerpt") or "").split())[:900]
        if excerpt:
            embed.add_field(name="Последнее изменение", value=excerpt, inline=False)
        if item.get("actor"):
            actor = str(item["actor"])
            if item.get("actor_role"):
                actor += f" · {item['actor_role']}"
            embed.set_footer(text=actor[:200])
        try:
            message = await user.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.DiscordException as exc:
            await asyncio.to_thread(
                storage.mark_complaint_delivery,
                int(item["id"]), sent=False, error=f"{type(exc).__name__}: {exc}",
            )
            return False
        await asyncio.to_thread(
            storage.mark_complaint_delivery,
            int(item["id"]), sent=True, dm_message_id=int(message.id),
        )
        emit_global_event(
            {
                "source_service": "atlas",
                "source_type": "forum_engine",
                "event_type": "forum_complaint_notification_sent",
                "severity": "info",
                "guild_id": self.guild_id,
                "message_id": int(message.id),
                "target_type": "forum_complaint",
                "target_id": str(item["complaint_id"]),
                "summary": title,
                "details": {
                    "event_id": int(item["event_id"]),
                    "recipient_user_id": int(item["user_id"]),
                    "static_id": item.get("static_id"),
                },
            }
        )
        return True

    async def deliver_pending(self) -> dict[str, int]:
        rows = await asyncio.to_thread(storage.pending_complaint_deliveries, limit=50)
        sent = failed = 0
        for item in rows:
            if await self._deliver_one(item):
                sent += 1
            else:
                failed += 1
        return {"pending": len(rows), "sent": sent, "failed": failed}

    async def run_once(self, *, force: bool = False) -> dict[str, Any]:
        if not self.config.enabled:
            return {"status": "disabled"}
        async with self._lock:
            await asyncio.to_thread(storage.ensure_default_monitor_feeds, self.guild_id)
            feeds = await asyncio.to_thread(
                storage.due_monitor_feeds, self.guild_id, force=force
            )
            characters = await asyncio.to_thread(storage.list_monitored_characters, self.guild_id)
            reconciled = await self._reconcile_existing_subjects(characters)
            results = []
            for feed in feeds:
                results.append(
                    await self._scan_feed(
                        feed, force=force, characters=characters
                    )
                )
            delivery = await self.deliver_pending()
            return {
                "status": "complete",
                "feeds": len(feeds),
                "characters": len(characters),
                "subjects_reconciled": reconciled,
                "results": results,
                "delivery": delivery,
            }

    async def run(self) -> None:
        if not self.config.enabled:
            return
        await asyncio.to_thread(storage.ensure_default_monitor_feeds, self.guild_id)
        await asyncio.to_thread(storage.recover_interrupted_monitor_feeds, self.guild_id)
        try:
            await asyncio.wait_for(
                self._wake.wait(), timeout=self.config.initial_delay_seconds
            )
        except TimeoutError:
            pass
        self._wake.clear()
        while not self._closed:
            force = self._force_once
            self._force_once = False
            try:
                await self.run_once(force=force)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._technical_log(
                    "Atlas Forum Engine временно остановил проход",
                    "Сохранённые жалобы и уведомления не потеряны; повтор запланирован.\n"
                    f"Причина: `{type(exc).__name__}: {str(exc)[:600]}`",
                    level="warning",
                    dedupe_key=f"atlas-forum-engine-loop:{type(exc).__name__}",
                    exception=exc,
                )
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.config.scheduler_poll_seconds
                )
            except TimeoutError:
                pass
            self._wake.clear()

    async def close(self) -> None:
        self._closed = True
        self._wake.set()


__all__ = ["AtlasForumEngineConfig", "AtlasForumEngineRunner"]
