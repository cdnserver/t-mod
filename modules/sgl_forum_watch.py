"""Safe, durable watching of published SGL claims on the Majestic forum.

The watcher deliberately reuses the ordinary authenticated Selenium browser
already used by Atlas.  It never solves login/captcha challenges, never posts,
and treats a forum topic as read-only evidence.  Its only side effect is a
compact Discord alert in the already protected case channel when a later scan
detects a change.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from typing import Any

import discord

from modules.atlas_forum_sync import (
    AtlasForumBrowser,
    AtlasForumManualActionRequired,
    AtlasForumSyncConfig,
    AtlasForumSyncError,
)
from modules.sgl_forum_publish import SGLForumPublishConfig
from persistence import sgl_repository as storage
from persistence.core import utc_now_iso


def _flag(name: str, default: bool = False) -> bool:
    return str(os.getenv(name, str(default))).strip().lower() in {"1", "true", "yes", "on"}


def _interval() -> int:
    try:
        value = int(str(os.getenv("SGL_FORUM_WATCH_INTERVAL_SECONDS", "900")).strip())
    except (TypeError, ValueError):
        value = 900
    return max(120, min(86_400, value))


@dataclass(frozen=True, slots=True)
class SGLForumWatchConfig:
    enabled: bool
    interval_seconds: int
    selenium_url: str
    root_url: str
    cookie_file: str
    challenge_wait_seconds: int

    @classmethod
    def from_env(cls) -> "SGLForumWatchConfig":
        publication = SGLForumPublishConfig.from_env()
        return cls(
            enabled=_flag("SGL_FORUM_WATCH_ENABLED"),
            interval_seconds=_interval(),
            selenium_url=publication.selenium_url,
            root_url=publication.root_url,
            cookie_file=publication.cookie_file,
            challenge_wait_seconds=publication.challenge_wait_seconds,
        )

    def as_atlas_config(self) -> AtlasForumSyncConfig:
        return AtlasForumSyncConfig(
            enabled=self.enabled,
            selenium_url=self.selenium_url,
            root_url=self.root_url,
            cookie_file=self.cookie_file,
            feed_key="sgl-forum-watch",
            server_code="",
            faction_code="",
            visibility_scope="private",
            interval_seconds=self.interval_seconds,
            initial_delay_seconds=10,
            page_delay_seconds=0.8,
            challenge_wait_seconds=self.challenge_wait_seconds,
            max_listing_pages=1,
            max_threads=1,
        )


class SGLForumWatchError(RuntimeError):
    pass


class SGLForumWatchRunner:
    """Serialize one browser scan and emit only meaningful case alerts."""

    def __init__(
        self,
        bot: discord.Client,
        guild_id: int,
        *,
        config: SGLForumWatchConfig | None = None,
        browser: AtlasForumBrowser | None = None,
    ) -> None:
        self.bot = bot
        self.guild_id = int(guild_id)
        self.config = config or SGLForumWatchConfig.from_env()
        self.browser = browser or AtlasForumBrowser(self.config.as_atlas_config())
        self._lock = asyncio.Lock()
        self._closed = False
        # This is deliberately in-memory operational metadata.  Durable topic
        # state belongs to the observations table; the runner status only
        # answers whether this process is currently checking and when it most
        # recently completed a pass.  Do not place browser/cookie settings in
        # this projection.
        self._last_started_at: str | None = None
        self._last_finished_at: str | None = None
        self._last_summary: dict[str, int | str] | None = None
        self._last_error: str | None = None

    def status_snapshot(self) -> dict[str, Any]:
        """Return a safe, non-secret status projection for manager surfaces."""

        state = (
            "closed" if self._closed
            else ("disabled" if not self.config.enabled else ("checking" if self._lock.locked() else "idle"))
        )
        return {
            "state": state,
            "last_started_at": self._last_started_at,
            "last_finished_at": self._last_finished_at,
            "last_summary": dict(self._last_summary) if self._last_summary is not None else None,
            # Keep this a stable category, never an exception message that
            # could include a third-party URL, session detail, or traceback.
            "last_error": self._last_error,
        }

    @staticmethod
    def _fingerprint(title: str, content: str) -> str:
        canonical = f"{str(title).strip()}\n\0\n{str(content).strip()}".encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    async def _notify(self, publication: dict[str, Any], observation: dict[str, Any]) -> bool:
        channel_id = int(publication.get("channel_id") or 0)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return False
        changed = observation.get("change_type") == "changed"
        header = "⚠️ Форумный иск изменился" if changed else "⚠️ Forum Watch требует внимания"
        title = str(observation.get("thread_title") or publication.get("title") or "Тема иска")[:180]
        excerpt = str(observation.get("thread_excerpt") or observation.get("last_error") or "")[:700]
        lines = [
            header,
            f"SGL №{int(publication.get('case_number') or 0):03d} · {title}",
            str(publication.get("forum_url") or ""),
        ]
        if excerpt:
            lines.append(f"{excerpt}")
        try:
            await channel.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return False
        return True

    async def check_once(self) -> dict[str, int | str]:
        if not self.config.enabled:
            raise SGLForumWatchError("sgl_forum_watch_disabled")
        async with self._lock:
            summary: dict[str, int | str] = {
                "status": "complete", "checked": 0, "changed": 0,
                "alerts": 0, "errors": 0,
            }
            self._last_started_at = utc_now_iso()
            self._last_error = None
            try:
                publications = await asyncio.to_thread(
                    storage.list_sgl_forum_publications_for_guild, self.guild_id, limit=300
                )
                active = [
                    item for item in publications
                    if str(item.get("status") or "") == "published" and item.get("forum_url")
                ]
                for publication in active:
                    try:
                        snapshot = await asyncio.to_thread(
                            self.browser.scrape_thread, str(publication["forum_url"])
                        )
                        observation = await asyncio.to_thread(
                            storage.observe_sgl_case_forum_publication,
                            publication=publication,
                            thread_title=snapshot.title,
                            thread_excerpt=snapshot.content[:4_000],
                            content_fingerprint=self._fingerprint(snapshot.title, snapshot.content),
                        )
                        summary["checked"] = int(summary["checked"]) + 1
                    except (AtlasForumManualActionRequired, AtlasForumSyncError) as exc:
                        observation = await asyncio.to_thread(
                            storage.observe_sgl_case_forum_publication,
                            publication=publication,
                            error=str(exc),
                        )
                        summary["errors"] = int(summary["errors"]) + 1
                    except Exception as exc:  # keep other case topics observable
                        observation = await asyncio.to_thread(
                            storage.observe_sgl_case_forum_publication,
                            publication=publication,
                            error=f"sgl_forum_watch_failed:{type(exc).__name__}",
                        )
                        summary["errors"] = int(summary["errors"]) + 1
                    if observation.get("change_type") == "changed":
                        summary["changed"] = int(summary["changed"]) + 1
                    if observation.get("should_notify"):
                        case = await asyncio.to_thread(
                            storage.get_sgl_case_by_number,
                            self.guild_id,
                            int(publication.get("case_number") or 0),
                        )
                        changed = observation.get("change_type") == "changed"
                        notification = None
                        if case is not None:
                            notification = await asyncio.to_thread(
                                storage.create_sgl_case_notification,
                                guild_id=self.guild_id,
                                case=case,
                                kind="forum_changed" if changed else "forum_watch_error",
                                severity="warning" if changed else "critical",
                                title="Изменился опубликованный иск" if changed else "Forum Watch требует ручного действия",
                                body=str(
                                    observation.get("thread_excerpt")
                                    or observation.get("last_error")
                                    or publication.get("forum_url")
                                    or ""
                                )[:4_000],
                                tab="forum",
                                source="forum_watch",
                                external_status="pending",
                                dedupe_key=(
                                    f"forum:{int(observation['id'])}:"
                                    f"{observation.get('change_type')}:"
                                    f"{observation.get('last_changed_at') or observation.get('updated_at') or ''}"
                                ),
                            )
                        delivered = await self._notify(publication, observation)
                        if notification is not None:
                            await asyncio.to_thread(
                                storage.update_sgl_case_notification_delivery,
                                int(notification["id"]),
                                guild_id=self.guild_id,
                                external_status="sent" if delivered else "failed",
                            )
                        if delivered:
                            await asyncio.to_thread(
                                storage.mark_sgl_forum_observation_notified,
                                int(observation["id"]),
                            )
                            summary["alerts"] = int(summary["alerts"]) + 1
                self._last_summary = dict(summary)
                return summary
            except asyncio.CancelledError:
                self._last_error = "cancelled"
                raise
            except Exception:
                self._last_error = "check_failed"
                raise
            finally:
                self._last_finished_at = utc_now_iso()
                try:
                    await asyncio.to_thread(self.browser.close)
                except Exception:
                    # A later scan can create a fresh browser; report the
                    # failure without leaking low-level browser details.
                    self._last_error = self._last_error or "browser_close_failed"

    async def run_forever(self) -> None:
        if not self.config.enabled:
            return
        while not self._closed:
            try:
                await self.check_once()
            except Exception:
                # A later interval is safer than a tight retry loop around an
                # operator-owned browser session.  Individual failures are
                # already retained by the observation record where possible.
                pass
            try:
                await asyncio.wait_for(
                    asyncio.sleep(self.config.interval_seconds), timeout=self.config.interval_seconds + 5
                )
            except asyncio.CancelledError:
                raise

    async def close(self) -> None:
        self._closed = True
        await asyncio.to_thread(self.browser.close)


__all__ = ["SGLForumWatchConfig", "SGLForumWatchError", "SGLForumWatchRunner"]
