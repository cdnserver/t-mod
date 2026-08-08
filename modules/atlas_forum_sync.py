"""Browser-backed, revision-safe synchronization of public XenForo material.

The browser executes the forum's normal JavaScript. It never attempts to solve
CAPTCHAs, spoof fingerprints or bypass an interstitial: manual verification is
reported as an explicit attention state while the last good Atlas revision
continues serving users.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from lxml import html

from modules.atlas_ai import atlas_index_source
from modules.technical_log import log_technical_event
from persistence import atlas_repository as storage


_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_CHALLENGE_MARKERS = (
    "please turn javascript on",
    "enable javascript and cookies to continue",
    "cdn-cgi/challenge-platform",
    "vddosw3data.js",
)
_MANUAL_MARKERS = (
    "g-recaptcha",
    "hcaptcha",
    "cf-turnstile",
    "подтвердите, что вы человек",
    "verify you are human",
)


class AtlasForumSyncError(RuntimeError):
    pass


class AtlasForumManualActionRequired(AtlasForumSyncError):
    pass


@dataclass(frozen=True, slots=True)
class AtlasForumSyncConfig:
    enabled: bool
    selenium_url: str
    root_url: str
    cookie_file: str
    feed_key: str
    server_code: str
    faction_code: str
    visibility_scope: str
    interval_seconds: int
    initial_delay_seconds: int
    page_delay_seconds: float
    challenge_wait_seconds: int
    max_listing_pages: int
    max_threads: int

    @classmethod
    def from_env(cls) -> "AtlasForumSyncConfig":
        enabled = str(os.getenv("ATLAS_FORUM_SYNC_ENABLED", "true")).strip().lower()
        return cls(
            enabled=enabled in {"1", "true", "yes", "on"},
            selenium_url=str(
                os.getenv(
                    "ATLAS_FORUM_SELENIUM_URL",
                    "http://atlas-forum-browser:4444/wd/hub",
                )
            ).strip(),
            root_url=str(
                os.getenv(
                    "ATLAS_FORUM_ROOT_URL",
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.1213/",
                )
            ).strip(),
            cookie_file=str(
                os.getenv(
                    "ATLAS_FORUM_COOKIE_FILE",
                    "/app/persistent/data/atlas-forum-cookies.json",
                )
            ).strip(),
            feed_key=str(os.getenv("ATLAS_FORUM_FEED_KEY", "majestic-phoenix-laws")).strip(),
            server_code=str(os.getenv("ATLAS_FORUM_SERVER_CODE", "phoenix-15")).strip(),
            faction_code=str(os.getenv("ATLAS_FORUM_FACTION_CODE", "lspd")).strip(),
            visibility_scope=str(os.getenv("ATLAS_FORUM_VISIBILITY_SCOPE", "server")).strip(),
            interval_seconds=max(
                3600,
                int(os.getenv("ATLAS_FORUM_SYNC_INTERVAL_SECONDS", "43200")),
            ),
            initial_delay_seconds=max(
                5,
                int(os.getenv("ATLAS_FORUM_INITIAL_DELAY_SECONDS", "45")),
            ),
            page_delay_seconds=max(
                1.0,
                float(os.getenv("ATLAS_FORUM_PAGE_DELAY_SECONDS", "2.5")),
            ),
            challenge_wait_seconds=max(
                10,
                int(os.getenv("ATLAS_FORUM_CHALLENGE_WAIT_SECONDS", "45")),
            ),
            max_listing_pages=max(
                1,
                min(50, int(os.getenv("ATLAS_FORUM_MAX_LISTING_PAGES", "20"))),
            ),
            max_threads=max(
                1,
                min(500, int(os.getenv("ATLAS_FORUM_MAX_THREADS", "200"))),
            ),
        )


@dataclass(frozen=True, slots=True)
class AtlasForumSnapshot:
    url: str
    title: str
    content: str
    author: str | None = None
    source_updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class AtlasForumScrapeBatch:
    snapshots: tuple[AtlasForumSnapshot, ...]
    inventory_complete: bool


def _clean_text(value: str) -> str:
    lines = []
    for raw in str(value or "").replace("\u00a0", " ").splitlines():
        line = _SPACE_RE.sub(" ", raw).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()


def _canonical_url(base_url: str, href: str) -> str | None:
    absolute = urljoin(base_url, str(href or "").strip())
    base = urlsplit(base_url)
    parsed = urlsplit(absolute)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
        return None
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path, "", ""))


def parse_forum_listing(page_html: str, page_url: str) -> tuple[list[str], str | None]:
    try:
        tree = html.fromstring(str(page_html or ""))
    except (TypeError, ValueError) as exc:
        raise AtlasForumSyncError("atlas_forum_listing_invalid") from exc
    links: list[str] = []
    seen: set[str] = set()
    selectors = (
        "//div[contains(@class,'structItem-title')]//a[contains(@href,'/threads/')]/@href",
        "//a[contains(@class,'PreviewTooltip') and contains(@href,'/threads/')]/@href",
        "//a[contains(@href,'/threads/')]/@href",
    )
    for selector in selectors:
        for href in tree.xpath(selector):
            url = _canonical_url(page_url, str(href))
            if url and "/threads/" in url and url not in seen:
                seen.add(url)
                links.append(url)
        if links:
            break
    next_url = None
    next_candidates = tree.xpath(
        "//a[contains(@class,'pageNav-jump--next') or @rel='next']/@href"
    )
    if next_candidates:
        candidate = _canonical_url(page_url, str(next_candidates[0]))
        if candidate and "/forums/" in candidate:
            next_url = candidate
    return links, next_url


def parse_forum_thread(page_html: str, page_url: str) -> AtlasForumSnapshot:
    try:
        tree = html.fromstring(str(page_html or ""))
    except (TypeError, ValueError) as exc:
        raise AtlasForumSyncError("atlas_forum_thread_invalid") from exc
    title_nodes = tree.xpath("//h1[contains(@class,'p-title-value')]")
    if not title_nodes:
        title_nodes = tree.xpath("//meta[@property='og:title']/@content")
    title = ""
    if title_nodes:
        node = title_nodes[0]
        title = _clean_text(node if isinstance(node, str) else node.text_content())
    body_nodes = tree.xpath(
        "(//article[contains(concat(' ', normalize-space(@class), ' '), ' message--post ')]"
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' message-body ')]"
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' bbWrapper ')])[1]"
    )
    if not body_nodes:
        body_nodes = tree.xpath(
            "(//article[contains(concat(' ', normalize-space(@class), ' '), ' message--post ')]"
            "//*[contains(concat(' ', normalize-space(@class), ' '), ' message-body ')])[1]"
        )
    if not body_nodes:
        body_nodes = tree.xpath(
            "(//*[contains(concat(' ', normalize-space(@class), ' '), ' message-userContent ')]"
            "//*[contains(concat(' ', normalize-space(@class), ' '), ' bbWrapper ')])[1]"
        )
    if not body_nodes:
        body_nodes = tree.xpath(
            "(//*[contains(concat(' ', normalize-space(@class), ' '), ' message-content ')]"
            "//*[contains(concat(' ', normalize-space(@class), ' '), ' bbWrapper ')])[1]"
        )
    if not body_nodes:
        raise AtlasForumSyncError("atlas_forum_thread_body_missing")
    body = body_nodes[0]
    for unwanted in body.xpath(
        ".//script | .//style | .//*[contains(@class,'bbCodeBlock--quote')] "
        "| .//*[contains(@class,'message-signature')]"
    ):
        parent = unwanted.getparent()
        if parent is not None:
            parent.remove(unwanted)
    blocks: list[str] = []
    for node in body.xpath(".//h1 | .//h2 | .//h3 | .//h4 | .//p | .//li | .//blockquote"):
        text = _clean_text(node.text_content())
        if text and (not blocks or text != blocks[-1]):
            blocks.append(text)
    content = "\n\n".join(blocks) if blocks else _clean_text("\n".join(body.itertext()))
    if not title:
        title = content.splitlines()[0][:180] if content else "Материал форума"
    if len(content) < 20:
        raise AtlasForumSyncError("atlas_forum_thread_content_too_short")
    author_nodes = tree.xpath(
        "(//article[contains(@class,'message')]//*[contains(@class,'message-name')]"
        "//*[contains(@class,'username')])[1]"
    )
    time_values = tree.xpath(
        "(//article[contains(@class,'message')]//time[contains(@class,'u-dt')]/@datetime)[1]"
    )
    return AtlasForumSnapshot(
        url=str(page_url),
        title=title[:180],
        content=content[:250000],
        author=_clean_text(author_nodes[0].text_content())[:120] if author_nodes else None,
        source_updated_at=str(time_values[0])[:100] if time_values else None,
    )


def forum_interstitial_kind(page_html: str) -> str | None:
    lowered = str(page_html or "").lower()
    if any(marker in lowered for marker in _MANUAL_MARKERS):
        return "manual"
    if any(marker in lowered for marker in _CHALLENGE_MARKERS):
        return "javascript"
    return None


class AtlasForumBrowser:
    def __init__(self, config: AtlasForumSyncConfig) -> None:
        self.config = config
        self._driver: Any | None = None

    def _grid_root(self) -> str:
        parsed = urlsplit(self.config.selenium_url)
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")

    def _release_orphaned_sessions(self) -> int:
        """Free the dedicated Grid slot left by a previous T-Mod process."""

        try:
            response = requests.post(
                f"{self._grid_root()}/graphql",
                json={"query": "{ sessionsInfo { sessions { id } } }"},
                timeout=4,
            )
            response.raise_for_status()
            sessions = response.json().get("data", {}).get("sessionsInfo", {}).get("sessions", [])
        except (requests.RequestException, TypeError, ValueError, AttributeError):
            return 0
        released = 0
        for item in sessions if isinstance(sessions, list) else []:
            session_id = str(item.get("id") or "") if isinstance(item, dict) else ""
            if not session_id:
                continue
            try:
                result = requests.delete(
                    f"{self._grid_root()}/session/{session_id}",
                    timeout=5,
                )
                if result.status_code < 500:
                    released += 1
            except requests.RequestException:
                continue
        return released

    def _open_driver(self, options: Any) -> Any:
        from selenium import webdriver

        driver = webdriver.Remote(
            command_executor=self.config.selenium_url,
            options=options,
        )
        driver.set_page_load_timeout(60)
        return driver

    def _save_cookies(self, driver: Any) -> int:
        """Persist authentication without persisting Chromium's lock-prone profile."""

        cookie_file = str(self.config.cookie_file or "").strip()
        if not cookie_file:
            return 0
        cookies = driver.get_cookies()
        if not isinstance(cookies, list) or not cookies:
            return 0
        path = Path(cookie_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(cookies, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            # chmod is not supported by every Docker Desktop bind mount.
            pass
        temporary.replace(path)
        return len(cookies)

    def _restore_cookies(self, driver: Any) -> int:
        cookie_file = str(self.config.cookie_file or "").strip()
        if not cookie_file:
            return 0
        path = Path(cookie_file)
        if not path.is_file():
            return 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return 0
        if not isinstance(payload, list):
            return 0
        parsed = urlsplit(self.config.root_url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
        try:
            driver.get(origin)
        except Exception:
            return 0
        restored = 0
        allowed_fields = {
            "name",
            "value",
            "path",
            "domain",
            "secure",
            "httpOnly",
            "expiry",
            "sameSite",
        }
        for raw_cookie in payload:
            if not isinstance(raw_cookie, dict):
                continue
            cookie = {key: value for key, value in raw_cookie.items() if key in allowed_fields}
            if not cookie.get("name") or "value" not in cookie:
                continue
            if "expiry" in cookie:
                try:
                    cookie["expiry"] = int(cookie["expiry"])
                except (TypeError, ValueError):
                    cookie.pop("expiry", None)
            if cookie.get("sameSite") not in {"Strict", "Lax", "None"}:
                cookie.pop("sameSite", None)
            try:
                driver.add_cookie(cookie)
                restored += 1
            except Exception:
                # One obsolete forum cookie must not invalidate the whole session.
                continue
        return restored

    @staticmethod
    def _exception_detail(exc: BaseException) -> str:
        return _SPACE_RE.sub(" ", str(exc or "")).strip()[:350]

    def _connect(self) -> Any:
        if self._driver is not None:
            try:
                _ = self._driver.current_url
                return self._driver
            except Exception:
                self.close()
        try:
            from selenium.webdriver.chrome.options import Options
        except ImportError as exc:
            raise AtlasForumSyncError("selenium_not_installed") from exc
        options = Options()
        options.add_argument("--lang=ru-RU")
        options.add_argument("--window-size=1440,1200")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.set_capability("pageLoadStrategy", "normal")
        options.set_capability("se:name", "T-Mod Atlas forum sync")
        self._release_orphaned_sessions()
        try:
            self._driver = self._open_driver(options)
            self._restore_cookies(self._driver)
            return self._driver
        except Exception:
            self.close()
            time.sleep(1.0)
        try:
            self._release_orphaned_sessions()
            self._driver = self._open_driver(options)
            self._restore_cookies(self._driver)
            return self._driver
        except Exception as exc:
            self.close()
            detail = self._exception_detail(exc)
            raise AtlasForumSyncError(
                f"atlas_forum_browser_unavailable:{type(exc).__name__}"
                f"{f':{detail}' if detail else ''}"
            ) from exc

    def _load(self, url: str) -> str:
        driver = self._connect()
        try:
            driver.get(url)
            deadline = time.monotonic() + self.config.challenge_wait_seconds
            while True:
                source = str(driver.page_source or "")
                kind = forum_interstitial_kind(source)
                if kind is None:
                    return source
                if kind == "manual":
                    visible = bool(
                        driver.execute_script(
                            """
                            const text = (document.body?.innerText || '').toLowerCase();
                            if (text.includes('verify you are human') ||
                                text.includes('подтвердите, что вы человек')) return true;
                            return [...document.querySelectorAll(
                              '.g-recaptcha,.h-captcha,.cf-turnstile,' +
                              'iframe[src*="captcha"],iframe[src*="turnstile"]'
                            )].some((node) => {
                              const box = node.getBoundingClientRect();
                              const style = getComputedStyle(node);
                              return box.width > 2 && box.height > 2 &&
                                style.display !== 'none' && style.visibility !== 'hidden';
                            });
                            """
                        )
                    )
                    if visible:
                        raise AtlasForumManualActionRequired(
                            "Форум запросил ручное подтверждение в локальном Chromium."
                        )
                    return source
                if time.monotonic() >= deadline:
                    raise AtlasForumManualActionRequired(
                        "JavaScript-проверка форума не завершилась автоматически."
                    )
                time.sleep(1.0)
        except AtlasForumManualActionRequired:
            raise
        except Exception as exc:
            self.close()
            raise AtlasForumSyncError(f"atlas_forum_page_failed:{type(exc).__name__}") from exc

    def scrape(self) -> AtlasForumScrapeBatch:
        listing_queue = [self.config.root_url]
        visited_listings: set[str] = set()
        thread_urls: list[str] = []
        thread_seen: set[str] = set()
        hit_thread_limit = False
        while listing_queue and len(visited_listings) < self.config.max_listing_pages:
            listing_url = listing_queue.pop(0)
            if listing_url in visited_listings:
                continue
            links: list[str] = []
            next_url: str | None = None
            for attempt in range(3):
                page = self._load(listing_url)
                links, next_url = parse_forum_listing(page, listing_url)
                if links:
                    break
                if attempt < 2:
                    time.sleep(self.config.page_delay_seconds)
            if not links:
                raise AtlasForumManualActionRequired(
                    "Форум не показал список тем. Откройте локальный Chromium, "
                    "завершите проверку страницы и повторите синхронизацию."
                )
            visited_listings.add(listing_url)
            for link in links:
                if link in thread_seen:
                    continue
                if len(thread_urls) >= self.config.max_threads:
                    hit_thread_limit = True
                    continue
                thread_seen.add(link)
                thread_urls.append(link)
            if next_url and next_url not in visited_listings:
                listing_queue.append(next_url)
            if listing_queue:
                time.sleep(self.config.page_delay_seconds)
        if not thread_urls:
            raise AtlasForumManualActionRequired(
                "Форум не показал темы законодательной базы. Требуется проверка страницы."
            )
        snapshots: list[AtlasForumSnapshot] = []
        for index, thread_url in enumerate(thread_urls):
            page = self._load(thread_url)
            snapshots.append(parse_forum_thread(page, thread_url))
            if index + 1 < len(thread_urls):
                time.sleep(self.config.page_delay_seconds)
        return AtlasForumScrapeBatch(
            snapshots=tuple(snapshots),
            inventory_complete=not hit_thread_limit and not listing_queue,
        )

    def scrape_thread(self, url: str) -> AtlasForumSnapshot:
        """Import one authenticated Majestic forum thread without allowing arbitrary hosts."""

        thread_url = _canonical_url(self.config.root_url, str(url or ""))
        if thread_url is None or "/threads/" not in thread_url:
            raise AtlasForumSyncError("atlas_forum_thread_url_invalid")
        return parse_forum_thread(self._load(thread_url), thread_url)

    def close(self) -> None:
        driver, self._driver = self._driver, None
        if driver is not None:
            try:
                self._save_cookies(driver)
            except Exception:
                pass
            try:
                driver.quit()
            except Exception:
                pass


IndexCallback = Callable[[dict[str, Any]], Awaitable[list[str]]]


class AtlasForumSyncRunner:
    def __init__(
        self,
        bot: Any,
        guild_id: int,
        *,
        config: AtlasForumSyncConfig | None = None,
        browser: AtlasForumBrowser | None = None,
        index_callback: IndexCallback = atlas_index_source,
    ) -> None:
        self.bot = bot
        self.guild_id = int(guild_id)
        self.config = config or AtlasForumSyncConfig.from_env()
        self.browser = browser or AtlasForumBrowser(self.config)
        self.index_callback = index_callback
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._closed = False

    def trigger(self) -> bool:
        if not self.config.enabled or self._closed:
            return False
        self._wake.set()
        return True

    async def fetch_thread(self, url: str) -> AtlasForumSnapshot:
        """Reuse the signed-in browser while serializing it with scheduled sync."""

        if not self.config.enabled or self._closed:
            raise AtlasForumSyncError("atlas_forum_sync_disabled")
        async with self._lock:
            try:
                snapshot = await asyncio.to_thread(self.browser.scrape_thread, url)
            except AtlasForumManualActionRequired:
                raise
            else:
                await asyncio.to_thread(self.browser.close)
                return snapshot

    async def _technical_log(
        self,
        *,
        title: str,
        details: str,
        level: str,
        dedupe_key: str,
        exception: BaseException | None = None,
    ) -> None:
        guild = self.bot.get_guild(self.guild_id) if self.bot is not None else None
        if guild is None:
            return
        await log_technical_event(
            self.bot,
            guild,
            title=title,
            details=details,
            level=level,
            dedupe_key=dedupe_key,
            cooldown_seconds=3600,
            exception=exception,
            component="atlas-forum-sync",
        )

    async def sync_once(self) -> dict[str, Any]:
        async with self._lock:
            feed = await asyncio.to_thread(
                storage.atlas_ensure_forum_feed,
                self.guild_id,
                feed_key=self.config.feed_key,
                root_url=self.config.root_url,
                server_code=self.config.server_code,
                faction_code=self.config.faction_code,
                visibility_scope=self.config.visibility_scope,
                interval_seconds=self.config.interval_seconds,
            )
            await asyncio.to_thread(storage.atlas_forum_sync_started, int(feed["id"]))
            try:
                batch = await asyncio.to_thread(self.browser.scrape)
                snapshots = list(batch.snapshots)
                changed = 0
                created = 0
                indexed = 0
                index_errors = 0
                retried = 0
                seen_urls: list[str] = []
                for snapshot in snapshots:
                    seen_urls.append(snapshot.url)
                    result = await asyncio.to_thread(
                        storage.atlas_upsert_synced_knowledge,
                        int(feed["organization_id"]),
                        title=snapshot.title,
                        content=snapshot.content,
                        source_url=snapshot.url,
                        server_code=self.config.server_code,
                        faction_code=self.config.faction_code,
                        visibility_scope=self.config.visibility_scope,
                        feed_key=self.config.feed_key,
                        metadata={
                            "author": snapshot.author,
                            "source_updated_at": snapshot.source_updated_at,
                        },
                    )
                    if result["created"]:
                        created += 1
                    source = result["source"]
                    needs_index = result["changed"] or source.get("status") != "indexed"
                    if not needs_index:
                        continue
                    if result["changed"]:
                        changed += 1
                    else:
                        retried += 1
                    try:
                        point_ids = await self.index_callback(source)
                        await asyncio.to_thread(
                            storage.atlas_mark_knowledge_indexed,
                            int(source["id"]),
                            point_id=point_ids[0] if point_ids else None,
                        )
                        indexed += 1
                    except Exception as exc:
                        index_errors += 1
                        await asyncio.to_thread(
                            storage.atlas_mark_knowledge_indexed,
                            int(source["id"]),
                            point_id=None,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                missing_changes = 0
                if batch.inventory_complete:
                    missing_changes = await asyncio.to_thread(
                        storage.atlas_mark_forum_sources_seen,
                        int(feed["organization_id"]),
                        feed_key=self.config.feed_key,
                        seen_urls=seen_urls,
                    )
                stats = {
                    "pages": len(snapshots),
                    "created": created,
                    "changed": changed,
                    "indexed": indexed,
                    "index_errors": index_errors,
                    "retried": retried,
                    "missing_changes": missing_changes,
                    "inventory_complete": batch.inventory_complete,
                }
                partial_error = (
                    f"Не удалось переиндексировать источников: {index_errors}"
                    if index_errors
                    else None
                )
                state = await asyncio.to_thread(
                    storage.atlas_forum_sync_finished,
                    int(feed["id"]),
                    stats=stats,
                    error=partial_error,
                    attention=bool(index_errors),
                )
                if changed:
                    await self._technical_log(
                        title="Atlas обновил законодательную базу",
                        details=(
                            f"Phoenix (15): проверено {len(snapshots)}, "
                            f"изменено {changed}, новых {created}."
                        ),
                        level="info",
                        dedupe_key=f"atlas-forum-updated:{state.get('last_success_at')}",
                    )
                await asyncio.to_thread(self.browser.close)
                return state
            except AtlasForumManualActionRequired as exc:
                state = await asyncio.to_thread(
                    storage.atlas_forum_sync_finished,
                    int(feed["id"]),
                    stats={},
                    error=str(exc),
                    attention=True,
                )
                await self._technical_log(
                    title="Atlas ждёт подтверждение форума",
                    details=(
                        f"{exc}\nОткройте на домашнем сервере "
                        "http://127.0.0.1:7900/ и завершите проверку. "
                        "Последняя рабочая редакция продолжает использоваться."
                    ),
                    level="warning",
                    dedupe_key="atlas-forum-manual-action",
                    exception=exc,
                )
                return state
            except Exception as exc:
                await asyncio.to_thread(self.browser.close)
                state = await asyncio.to_thread(
                    storage.atlas_forum_sync_finished,
                    int(feed["id"]),
                    stats={},
                    error=f"{type(exc).__name__}: {exc}",
                )
                await self._technical_log(
                    title="Ошибка синхронизации Atlas с форумом",
                    details=(
                        f"`{type(exc).__name__}`. Сохранённая законодательная база "
                        "не изменена; следующая попытка состоится автоматически."
                    ),
                    level="warning",
                    dedupe_key=f"atlas-forum-sync:{type(exc).__name__}",
                    exception=exc,
                )
                return state

    async def run(self) -> None:
        if not self.config.enabled:
            return
        try:
            initial_wait = self.config.initial_delay_seconds
            current = await asyncio.to_thread(
                storage.atlas_forum_sync_status,
                self.guild_id,
            )
            if current and current.get("next_sync_at"):
                try:
                    due = datetime.fromisoformat(str(current["next_sync_at"]))
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=timezone.utc)
                    remaining = (due - datetime.now(timezone.utc)).total_seconds()
                    if remaining > 0:
                        initial_wait = max(
                            initial_wait,
                            min(self.config.interval_seconds, int(remaining)),
                        )
                except (TypeError, ValueError):
                    pass
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=initial_wait,
                )
            except TimeoutError:
                pass
            self._wake.clear()
            while not self._closed:
                await self.sync_once()
                try:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=self.config.interval_seconds,
                    )
                except TimeoutError:
                    pass
                self._wake.clear()
        except asyncio.CancelledError:
            raise
        finally:
            await asyncio.to_thread(self.browser.close)

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        await asyncio.to_thread(self.browser.close)


__all__ = [
    "AtlasForumBrowser",
    "AtlasForumManualActionRequired",
    "AtlasForumScrapeBatch",
    "AtlasForumSnapshot",
    "AtlasForumSyncConfig",
    "AtlasForumSyncError",
    "AtlasForumSyncRunner",
    "forum_interstitial_kind",
    "parse_forum_listing",
    "parse_forum_thread",
]
