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
_NETWORK_ROUTE_ERROR_MARKERS = (
    "net::err_connection_refused",
    "net::err_connection_timed_out",
    "connecttimeout",
    "connection timed out",
    "connection refused",
    "network is unreachable",
    "no route to host",
    "net::err_name_not_resolved",
)


def _is_network_route_failure(error: object) -> bool:
    value = str(error or "").casefold()
    return any(marker in value for marker in _NETWORK_ROUTE_ERROR_MARKERS)


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
    # Five-second scheduler ticks plus a 30-second feed lease keep discovery
    # inside the promised one-minute window without continuously driving Chrome.
    initial_delay_seconds: int = 15
    scheduler_poll_seconds: int = 5
    hydration_batch_size: int = 50
    hot_hydration_batch_size: int = 4
    archive_listing_batch_pages: int = 2
    archive_hydration_batch_size: int = 3
    full_scan_interval_seconds: int = 43_200
    # Retrying an upstream TCP rejection can extend an anti-DDoS cooldown.
    # One failed feed therefore pauses every Majestic complaint feed together.
    network_cooldown_seconds: int = 21_600

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
            archive_listing_batch_pages=_bounded_int(
                "ATLAS_FORUM_ENGINE_ARCHIVE_PAGE_BATCH", 2, 1, 20
            ),
            archive_hydration_batch_size=_bounded_int(
                "ATLAS_FORUM_ENGINE_ARCHIVE_HYDRATION_BATCH", 3, 1, 20
            ),
            full_scan_interval_seconds=_bounded_int(
                "ATLAS_FORUM_ENGINE_FULL_SCAN_SECONDS", 43_200, 1800, 604_800
            ),
            network_cooldown_seconds=_bounded_int(
                "ATLAS_FORUM_NETWORK_COOLDOWN_SECONDS", 21_600, 3600, 86_400
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


_TARGET_STATIC_RE = re.compile(
    r"(?:статическ(?:ий|ого)\s*)?(?:#\s*)?id\s*(?:#\s*)?"
    r"(?:нарушителя|игрока|ответчика)\s*[:#№-]*\s*(\d{1,12})",
    re.IGNORECASE,
)
_REPORTER_STATIC_PATTERNS = (
    re.compile(
        r"(?:ваш|мой)\s+(?:статическ\w*\s+)?#?\s*"
        r"(?:id|айди|статик)\s*[:#№-]*\s*(\d{1,12})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:статическ\w*\s+)?#?\s*(?:id|айди|статик)\s*"
        r"(?:заявителя|автора(?:\s+жалобы)?)\s*[:#№-]*\s*(\d{1,12})",
        re.IGNORECASE,
    ),
)


def _complaint_target_static_ids(content: str) -> set[str]:
    """Extract only the accused player's IDs from a structured complaint."""

    return {match.group(1) for match in _TARGET_STATIC_RE.finditer(str(content or ""))}


def _complaint_participant_statics(title: str, content: str) -> dict[str, set[str]]:
    body = str(content or "")
    targets = _complaint_target_static_ids(body)
    if not targets:
        # Legacy topics often encode the accused static only in their title.
        targets = set(re.findall(r"(?<!\d)(\d{3,12})(?!\d)", str(title or "")))
    reporters = {
        match.group(1)
        for pattern in _REPORTER_STATIC_PATTERNS
        for match in pattern.finditer(body)
    }
    return {"reporter": reporters, "target": targets}


def _match_complaint_characters(
    title: str,
    content: str,
    characters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    explicit_targets = _complaint_target_static_ids(content)
    if explicit_targets:
        return [
            character
            for character in characters
            if re.sub(r"\s+", "", str(character.get("static_id") or ""))
            in explicit_targets
        ]
    # Older free-form templates commonly put the accused static in the title.
    # Prefer it over the body, which can mention witnesses and the reporter.
    title_matches = [
        character
        for character in characters
        if _matches_character(title, str(character.get("static_id") or ""))
    ]
    if title_matches:
        return title_matches
    return [
        character
        for character in characters
        # Very short statics are indistinguishable from dates, timecodes and
        # rule numbers in legacy free-form archives. They are matched only by
        # the explicit accused field or title above; guessing here would send
        # another player's complaint to the wrong person.
        if len(re.sub(r"\s+", "", str(character.get("static_id") or ""))) >= 4
        and _matches_character(content, str(character.get("static_id") or ""))
    ]


def _matched_characters(
    snapshot: AtlasForumSnapshot,
    characters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return _match_complaint_characters(snapshot.title, snapshot.content, characters)


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
                posts=(
                    {
                        "index": post.index,
                        "author": post.author,
                        "author_role": post.author_role,
                        "posted_at": post.posted_at,
                        "content": post.content,
                        "is_staff": post.is_staff,
                    }
                    for post in posts
                ),
                participant_statics=_complaint_participant_statics(
                    snapshot.title,
                    snapshot.content,
                ),
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
            matched = _match_complaint_characters(
                str(complaint.get("title") or ""),
                str(complaint.get("first_post_excerpt") or ""),
                characters,
            )
            # Reconciliation is authoritative even when nothing matches: that
            # empty result removes false subjects created by older parsers.
            linked += await asyncio.to_thread(
                storage.reconcile_complaint_subjects,
                int(complaint["id"]),
                matched,
            )
        self._character_revision = revision
        return linked

    async def _record_inventory(
        self,
        claimed: dict[str, Any],
        inventory: Any,
        *,
        notify: bool,
    ) -> dict[str, Any]:
        changed_urls: list[str] = []
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
                notify=notify,
            )
            created += 1 if result["created"] else 0
            changed += 1 if result["listing_changed"] else 0
            status_changes += 1 if result["section_changed"] else 0
            if result["created"] or result["listing_changed"]:
                changed_urls.append(entry.url)
        return {
            "created": created,
            "changed": changed,
            "status_changes": status_changes,
            "changed_urls": changed_urls,
        }

    async def _scan_feed(
        self,
        feed: dict[str, Any],
        *,
        archive: bool,
        characters: list[dict[str, Any]],
    ) -> dict[str, Any]:
        claimed = await asyncio.to_thread(storage.claim_monitor_feed, int(feed["id"]))
        if claimed is None:
            return {"status": "skipped"}
        baseline_exists = bool(claimed.get("baseline_completed_at"))
        try:
            # The first page is the alerting lane and is always read first. A
            # user pressing “check now” never starts a multi-hour archive job.
            hot_inventory = await self.forum.fetch_inventory(
                str(claimed["root_url"]),
                max_pages=int(claimed["hot_pages"]),
            )
            hot = await self._record_inventory(
                claimed,
                hot_inventory,
                notify=baseline_exists,
            )

            archive_inventory = None
            archive_ran = False
            archive_pages = 0
            archive_topics = 0
            archive_result = {
                "created": 0,
                "changed": 0,
                "status_changes": 0,
                "changed_urls": [],
            }
            backfill_state = claimed
            if archive and not claimed.get("backfill_completed_at"):
                cursor_url = str(
                    claimed.get("backfill_cursor_url") or claimed["root_url"]
                )
                archive_ran = True
                next_url = None
                if cursor_url == str(claimed["root_url"]):
                    # Page one was already read by the alert lane above. Count
                    # it once, then continue from its next page. Apart from
                    # saving a browser roundtrip this prevents an archive
                    # baseline from overriding a genuine new-topic alert.
                    archive_pages = hot_inventory.listing_pages
                    archive_topics = len(hot_inventory.entries)
                    next_url = hot_inventory.next_url
                    remaining = max(0, self.config.archive_listing_batch_pages - 1)
                    if next_url and remaining:
                        archive_inventory = await self.forum.fetch_inventory(
                            next_url,
                            max_pages=remaining,
                        )
                else:
                    archive_inventory = await self.forum.fetch_inventory(
                        cursor_url,
                        max_pages=self.config.archive_listing_batch_pages,
                    )
                if archive_inventory is not None:
                    archive_result = await self._record_inventory(
                        claimed,
                        archive_inventory,
                        notify=False,
                    )
                    archive_pages += archive_inventory.listing_pages
                    archive_topics += len(archive_inventory.entries)
                    next_url = archive_inventory.next_url
                backfill_state = await asyncio.to_thread(
                    storage.advance_monitor_backfill,
                    int(claimed["id"]),
                    next_url=next_url,
                    pages_scanned=archive_pages,
                    topics_seen=archive_topics,
                )

            changed_urls = list(
                dict.fromkeys(hot["changed_urls"] + archive_result["changed_urls"])
            )
            notify_by_url = {
                url: baseline_exists for url in hot["changed_urls"]
            }
            for url in archive_result["changed_urls"]:
                notify_by_url.setdefault(url, False)
            hydration_limit = self.config.hot_hydration_batch_size
            if archive_ran:
                hydration_limit += self.config.archive_hydration_batch_size
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
                if skipped:
                    await asyncio.to_thread(
                        storage.mark_complaint_hydration_failed,
                        self.guild_id,
                        skipped,
                        error="forum_thread_read_failed",
                    )
            created = int(hot["created"]) + int(archive_result["created"])
            changed = int(hot["changed"]) + int(archive_result["changed"])
            status_changes = int(hot["status_changes"]) + int(
                archive_result["status_changes"]
            )
            stats = {
                "section": str(claimed["section_kind"]),
                "mode": "hot+archive" if archive_ran else "hot",
                "listing_pages": hot_inventory.listing_pages,
                "topics": len(hot_inventory.entries),
                "created": created,
                "changed": changed,
                "status_changes": status_changes,
                "hydrated": hydrated["hydrated"],
                "matched": hydrated["matched"],
                "events": hydrated["events"],
                "skipped": len(skipped),
                "archive_pages": archive_pages,
                "archive_topics": archive_topics,
                "archive_pages_total": int(
                    backfill_state.get("backfill_pages_scanned") or 0
                ),
                "archive_topics_total": int(
                    backfill_state.get("backfill_topics_seen") or 0
                ),
                "archive_complete": bool(
                    backfill_state.get("backfill_completed_at")
                ),
            }
            emit_global_event(
                {
                    "source_service": "atlas",
                    "source_type": "forum_engine",
                    "event_type": "forum_complaint_feed_scanned",
                    "severity": "warning" if skipped else "info",
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
                error=None,
                attention=False,
                full_scan=bool(
                    archive_ran
                    and backfill_state.get("backfill_completed_at")
                ),
                baseline_completed=not baseline_exists,
            )
        except (AtlasForumManualActionRequired, AtlasForumSyncError) as exc:
            network_route_failure = _is_network_route_failure(exc)
            finished = await asyncio.to_thread(
                storage.finish_monitor_feed,
                int(claimed["id"]),
                stats={"section": str(claimed["section_kind"]), "phase": "forum_read"},
                error=str(exc),
                attention=True,
                full_scan=False,
            )
            if network_route_failure:
                await asyncio.to_thread(
                    storage.defer_monitor_feeds_for_network_cooldown,
                    self.guild_id,
                    seconds=self.config.network_cooldown_seconds,
                    error=str(exc),
                )
            failure_count = int(finished.get("failure_count") or 0)
            if failure_count in {1, 5, 20}:
                manual = isinstance(exc, AtlasForumManualActionRequired)
                await self._technical_log(
                    (
                        "Atlas Forum Engine требует подтверждения форума"
                        if manual
                        else "Atlas Forum Engine временно не видит браузер форума"
                    ),
                    f"Раздел: `{claimed['feed_key']}`. Сохранённая карта цела; "
                    "повтор выполняется автоматически.\n"
                    f"Причина: `{str(exc)[:700]}`",
                    level="warning",
                    dedupe_key=(
                        f"atlas-forum-engine-read:{claimed['id']}:{failure_count}"
                    ),
                    exception=exc,
                )
            return {
                "status": "attention",
                "error": str(exc),
                "network_cooldown": network_route_failure,
            }
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
            archive_candidates = [
                item for item in feeds if not item.get("backfill_completed_at")
            ]
            archive_feed_id = None
            if archive_candidates:
                selected_archive = min(
                    archive_candidates,
                    key=lambda item: (
                        str(item.get("last_backfill_at") or ""),
                        int(item["id"]),
                    ),
                )
                archive_feed_id = int(selected_archive["id"])
            for feed in feeds:
                result = await self._scan_feed(
                    feed,
                    archive=int(feed["id"]) == archive_feed_id,
                    characters=characters,
                )
                results.append(result)
                if result.get("network_cooldown"):
                    break
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
