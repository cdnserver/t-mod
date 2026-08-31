"""Browser-backed, revision-safe synchronization of public XenForo material.

The browser executes the forum's normal JavaScript. It never attempts to solve
CAPTCHAs, spoof fingerprints or bypass an interstitial: manual verification is
reported as an explicit attention state while the last good Atlas revision
continues serving users.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, replace
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
_LOGIN_FORM_MARKERS = (
    'type="password"',
    "type='password'",
    "autocomplete=\"current-password\"",
    "autocomplete='current-password'",
)
_LOGIN_TEXT_MARKERS = (
    "you must be logged in",
    "you must be logged-in",
    "log in or register",
    "войдите или зарегистрируйтесь",
    "вам необходимо войти",
    "необходимо авторизоваться",
)
_ACCESS_DENIED_MARKERS = (
    "you do not have permission to view this page",
    "you do not have permission to perform this action",
    "недостаточно прав для просмотра",
    "у вас нет прав для просмотра",
    "у вас недостаточно прав",
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
    # The default Atlas feed is the Majestic legislative library.  Laws are
    # shared between Majestic servers, so its canonical scope is the project,
    # not one Phoenix faction. Other feeds can override this in the database.
    federation_scope: str = "project"
    knowledge_domain: str | None = "ic"
    corpus_kind: str | None = "law"
    scheduler_poll_seconds: int = 60
    # Extra origins are opt-in. A database row must never be able to turn the
    # forum browser into a general-purpose authenticated web client.
    allowed_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "AtlasForumSyncConfig":
        enabled = str(os.getenv("ATLAS_FORUM_SYNC_ENABLED", "true")).strip().lower()
        root_url = str(
            os.getenv(
                "ATLAS_FORUM_ROOT_URL",
                "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.1213/",
            )
        ).strip()
        configured_origins: list[str] = []
        for raw_origin in str(os.getenv("ATLAS_FORUM_ALLOWED_ORIGINS", "")).split(","):
            parsed = urlsplit(raw_origin.strip())
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
                if origin not in configured_origins:
                    configured_origins.append(origin)
        return cls(
            enabled=enabled in {"1", "true", "yes", "on"},
            selenium_url=str(
                os.getenv(
                    "ATLAS_FORUM_SELENIUM_URL",
                    "http://atlas-forum-browser:4444/wd/hub",
                )
            ).strip(),
            root_url=root_url,
            cookie_file=str(
                os.getenv(
                    "ATLAS_FORUM_COOKIE_FILE",
                    "/app/persistent/data/atlas-forum-cookies.json",
                )
            ).strip(),
            feed_key=str(os.getenv("ATLAS_FORUM_FEED_KEY", "majestic-phoenix-laws")).strip(),
            server_code=str(os.getenv("ATLAS_FORUM_SERVER_CODE", "phoenix-15")).strip(),
            faction_code=str(os.getenv("ATLAS_FORUM_FACTION_CODE", "lspd")).strip(),
            visibility_scope=str(os.getenv("ATLAS_FORUM_VISIBILITY_SCOPE", "global")).strip(),
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
            federation_scope=str(
                os.getenv("ATLAS_FORUM_FEDERATION_SCOPE", "project")
            ).strip(),
            knowledge_domain=str(
                os.getenv("ATLAS_FORUM_KNOWLEDGE_DOMAIN", "ic")
            ).strip()
            or None,
            corpus_kind=str(
                os.getenv("ATLAS_FORUM_CORPUS_KIND", "law")
            ).strip()
            or None,
            scheduler_poll_seconds=max(
                15,
                min(900, int(os.getenv("ATLAS_FORUM_SCHEDULER_POLL_SECONDS", "60"))),
            ),
            allowed_origins=tuple(configured_origins),
        )


@dataclass(frozen=True, slots=True)
class AtlasForumAttachment:
    """A forum-owned attachment discovered in the authoritative first post.

    Discovery deliberately does not download or OCR a file.  The link remains
    tied to the source topic until a bounded background worker can preserve the
    original and present a human-reviewable OCR result.
    """

    url: str
    filename: str
    media_kind: str
    label: str | None = None

    def public(self) -> dict[str, str | None]:
        return {
            "url": self.url,
            "filename": self.filename,
            "media_kind": self.media_kind,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class AtlasForumSnapshot:
    url: str
    title: str
    content: str
    author: str | None = None
    source_updated_at: str | None = None
    attachments: tuple[AtlasForumAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class AtlasForumScrapeBatch:
    snapshots: tuple[AtlasForumSnapshot, ...]
    inventory_complete: bool
    skipped_threads: tuple[str, ...] = ()


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
    path = parsed.path
    thread_match = re.match(r"^(/threads/[^/]+\.\d+)(?:/.*)?$", path, re.IGNORECASE)
    if thread_match:
        path = f"{thread_match.group(1)}/"
    return urlunsplit(("https", parsed.netloc.lower(), path, "", ""))


def _canonical_attachment_url(base_url: str, href: str) -> str | None:
    """Keep only attachment URLs owned by the forum that supplied the topic."""

    absolute = urljoin(base_url, str(href or "").strip())
    base = urlsplit(base_url)
    parsed = urlsplit(absolute)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
        return None
    path = parsed.path or ""
    lowered = path.casefold()
    if "/attachments/" not in lowered and "/data/attachments/" not in lowered:
        return None
    return urlunsplit(("https", parsed.netloc.lower(), path, "", ""))


def _forum_attachments(body: Any, page_url: str) -> tuple[AtlasForumAttachment, ...]:
    """Extract a small, deduplicated inventory without trusting external media."""

    discovered: list[AtlasForumAttachment] = []
    seen: set[str] = set()
    candidates: list[tuple[str, str, str | None]] = []
    for image in body.xpath(".//img[@src or @data-src]"):
        candidates.append(
            (
                str(image.get("data-src") or image.get("src") or ""),
                "image",
                _clean_text(str(image.get("alt") or image.get("title") or ""))[:180] or None,
            )
        )
    for link in body.xpath(".//a[@href]"):
        candidates.append(
            (
                str(link.get("href") or ""),
                "file",
                _clean_text(link.text_content())[:180] or None,
            )
        )
    for href, inferred_kind, label in candidates:
        url = _canonical_attachment_url(page_url, href)
        if not url or url in seen:
            continue
        seen.add(url)
        filename = Path(urlsplit(url).path).name or "forum-attachment"
        suffix = Path(filename).suffix.casefold()
        media_kind = "image" if inferred_kind == "image" or suffix in {
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"
        } else "file"
        discovered.append(
            AtlasForumAttachment(
                url=url,
                filename=filename[:240],
                media_kind=media_kind,
                label=label,
            )
        )
        if len(discovered) >= 16:
            break
    return tuple(discovered)


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
        # XenForo add-ons and newer themes sometimes remove the usual
        # message-content wrapper while preserving the canonical bbWrapper.
        body_nodes = tree.xpath(
            "(//*[contains(concat(' ', normalize-space(@class), ' '), ' bbWrapper ')]"
            "[ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' message ')]])[1]"
        )
    if not body_nodes:
        body_nodes = tree.xpath(
            "(//*[contains(concat(' ', normalize-space(@class), ' '), ' message-body ')])[1]"
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
    # XenForo documents frequently keep articles, tables and numbered clauses
    # in nested div/span nodes rather than p/li elements. Selecting only a few
    # block tags silently reduced whole codes to a handful of list items. Every
    # text node inside the first post is authoritative after quotes/scripts
    # have been removed, so preserve all of them in document order.
    content = _clean_text("\n".join(body.itertext()))
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
        attachments=_forum_attachments(body, str(page_url)),
    )


def forum_interstitial_kind(page_html: str) -> str | None:
    lowered = str(page_html or "").lower()
    # XenForo keeps a hidden login overlay in the DOM even for authenticated
    # visitors.  Real forum content takes precedence over that dormant form.
    try:
        tree = html.fromstring(str(page_html or ""))
        has_forum_content = bool(
            tree.xpath(
                "//div[contains(@class,'structItem-title')]"
                "//a[contains(@href,'/threads/')]"
                " | //*[contains(concat(' ', normalize-space(@class), ' '),"
                " ' message-userContent ')]"
                "//*[contains(concat(' ', normalize-space(@class), ' '),"
                " ' bbWrapper ')]"
                " | //*[contains(concat(' ', normalize-space(@class), ' '),"
                " ' message-body ')]"
                "//*[contains(concat(' ', normalize-space(@class), ' '),"
                " ' bbWrapper ')]"
            )
        )
        if has_forum_content:
            return None
    except (TypeError, ValueError):
        pass
    if any(marker in lowered for marker in _MANUAL_MARKERS):
        return "manual"
    if any(marker in lowered for marker in _ACCESS_DENIED_MARKERS):
        return "access"
    if any(marker in lowered for marker in _LOGIN_TEXT_MARKERS) or (
        "/login" in lowered and any(marker in lowered for marker in _LOGIN_FORM_MARKERS)
    ):
        return "login"
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
        """Persist browser auth without persisting Chromium's lock-prone profile."""

        cookie_file = str(self.config.cookie_file or "").strip()
        if not cookie_file:
            return 0
        cookies = driver.get_cookies()
        if not isinstance(cookies, list) or not cookies:
            return 0
        cookies = sorted(
            (dict(item) for item in cookies if isinstance(item, dict)),
            key=lambda item: (
                str(item.get("domain") or ""),
                str(item.get("path") or ""),
                str(item.get("name") or ""),
            ),
        )
        local_storage: dict[str, str] = {}
        try:
            stored = driver.execute_script(
                "return Object.fromEntries(Object.entries(window.localStorage || {}));"
            )
            if isinstance(stored, dict):
                local_storage = {
                    str(key)[:300]: str(value)[:100_000]
                    for key, value in stored.items()
                }
        except Exception:
            pass
        path = Path(cookie_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        serialized = json.dumps(
            {
                "version": 1,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "cookies": cookies,
                "local_storage": local_storage,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            current = path.read_text(encoding="utf-8")
            current_payload = json.loads(current)
            if isinstance(current_payload, dict):
                current_payload.pop("saved_at", None)
                candidate = json.loads(serialized)
                candidate.pop("saved_at", None)
                if current_payload == candidate:
                    return len(cookies)
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        temporary.write_text(
            serialized,
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
        local_storage: dict[str, Any] = {}
        if isinstance(payload, dict):
            local_storage = (
                payload.get("local_storage")
                if isinstance(payload.get("local_storage"), dict)
                else {}
            )
            payload = payload.get("cookies")
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
        if local_storage:
            try:
                driver.execute_script(
                    "for (const [key, value] of Object.entries(arguments[0])) "
                    "window.localStorage.setItem(key, value);",
                    {str(key): str(value) for key, value in local_storage.items()},
                )
            except Exception:
                pass
        if restored:
            try:
                driver.refresh()
            except Exception:
                pass
        return restored

    @property
    def active(self) -> bool:
        return self._driver is not None

    def checkpoint_authentication(self) -> int:
        driver = self._driver
        if driver is None:
            return 0
        return self._save_cookies(driver)

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
                    try:
                        self._save_cookies(driver)
                    except Exception:
                        pass
                    return source
                if kind == "login":
                    raise AtlasForumManualActionRequired(
                        "Форум ожидает авторизацию в локальном Chromium."
                    )
                if kind == "access":
                    raise AtlasForumManualActionRequired(
                        "Авторизованный аккаунт форума не имеет доступа к этой странице."
                    )
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

    def _scrape_listing(self, root_url: str) -> AtlasForumScrapeBatch:
        listing_queue = [root_url]
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
        skipped_threads: list[str] = []
        for index, thread_url in enumerate(thread_urls):
            try:
                page = self._load(thread_url)
                snapshots.append(parse_forum_thread(page, thread_url))
            except AtlasForumManualActionRequired:
                raise
            except AtlasForumSyncError:
                skipped_threads.append(thread_url)
            if index + 1 < len(thread_urls):
                time.sleep(self.config.page_delay_seconds)
        if not snapshots:
            raise AtlasForumManualActionRequired(
                "Atlas открыл раздел, но не смог прочитать ни одной темы. "
                "Проверьте авторизацию и открытую страницу в локальном Chromium."
            )
        return AtlasForumScrapeBatch(
            snapshots=tuple(snapshots),
            inventory_complete=(
                not hit_thread_limit and not listing_queue and not skipped_threads
            ),
            skipped_threads=tuple(skipped_threads),
        )

    def scrape(self) -> AtlasForumScrapeBatch:
        return self._scrape_listing(self.config.root_url)

    def scrape_listing(self, url: str) -> AtlasForumScrapeBatch:
        """Import every topic from any same-host XenForo listing."""

        listing_url = _canonical_url(self.config.root_url, str(url or ""))
        if listing_url is None or "/forums/" not in listing_url:
            raise AtlasForumSyncError("atlas_forum_listing_url_invalid")
        return self._scrape_listing(listing_url)

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
        default_origin = self._origin(self.config.root_url)
        self._allowed_origins = {
            default_origin,
            *(self._origin(item) for item in self.config.allowed_origins),
        }
        self._browsers: dict[str, AtlasForumBrowser] = {default_origin: self.browser}
        self._auth_checkpoint_tasks: dict[asyncio.Task[None], AtlasForumBrowser] = {}
        self._force_default_once = False

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urlsplit(str(url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AtlasForumSyncError("atlas_forum_feed_url_invalid")
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"

    def _cookie_file_for_origin(self, origin: str) -> str:
        """Keep authenticated sessions separate when Atlas has several forums."""

        configured_origin = self._origin(self.config.root_url)
        if origin == configured_origin or not self.config.cookie_file:
            return self.config.cookie_file
        original = Path(self.config.cookie_file)
        suffix = original.suffix or ".json"
        token = hashlib.sha256(origin.encode("utf-8")).hexdigest()[:12]
        return str(original.with_name(f"{original.stem}-{token}{suffix}"))

    def _browser_for_feed(self, feed: dict[str, Any]) -> AtlasForumBrowser:
        root_url = str(feed.get("root_url") or "").strip()
        origin = self._origin(root_url)
        if origin not in self._allowed_origins:
            raise AtlasForumSyncError("atlas_forum_feed_origin_not_allowed")
        existing = self._browsers.get(origin)
        if existing is not None:
            return existing
        browser_config = replace(
            self.config,
            root_url=root_url,
            cookie_file=self._cookie_file_for_origin(origin),
        )
        created = AtlasForumBrowser(browser_config)
        self._browsers[origin] = created
        return created

    def _start_auth_checkpoint(self, browser: AtlasForumBrowser | None = None) -> None:
        target = browser or self.browser
        checkpoint = getattr(target, "checkpoint_authentication", None)
        if not callable(checkpoint):
            return
        if any(browser is target and not task.done() for task, browser in self._auth_checkpoint_tasks.items()):
            return
        task = asyncio.create_task(
            self._checkpoint_authentication_loop(target),
            name="atlas-forum-auth-checkpoint",
        )
        self._auth_checkpoint_tasks[task] = target
        task.add_done_callback(lambda done: self._auth_checkpoint_tasks.pop(done, None))

    async def _checkpoint_authentication_loop(self, browser: AtlasForumBrowser) -> None:
        """Keep a manual browser session alive and capture login as soon as it changes."""

        while not self._closed and bool(getattr(browser, "active", False)):
            async with self._lock:
                if not bool(getattr(browser, "active", False)):
                    return
                try:
                    await asyncio.to_thread(browser.checkpoint_authentication)
                except Exception:
                    return
            await asyncio.sleep(3)

    def trigger(self) -> bool:
        if not self.config.enabled or self._closed:
            return False
        # The admin button is an explicit request, not merely a request to
        # check whether the regular schedule happens to be due.
        self._force_default_once = True
        self._wake.set()
        return True

    def is_configured_listing_url(self, url: str) -> bool:
        candidate = _canonical_url(self.config.root_url, str(url or ""))
        configured = _canonical_url(self.config.root_url, self.config.root_url)
        return bool(
            candidate
            and configured
            and "/forums/" in candidate
            and candidate.rstrip("/") == configured.rstrip("/")
        )

    def is_forum_listing_url(self, url: str) -> bool:
        candidate = _canonical_url(self.config.root_url, str(url or ""))
        return bool(candidate and "/forums/" in candidate)

    async def fetch_listing(self, url: str) -> AtlasForumScrapeBatch:
        """Serialize an arbitrary same-host listing import with scheduled sync."""

        if not self.config.enabled or self._closed:
            raise AtlasForumSyncError("atlas_forum_sync_disabled")
        async with self._lock:
            try:
                batch = await asyncio.to_thread(self.browser.scrape_listing, url)
            except (AtlasForumManualActionRequired, AtlasForumSyncError):
                if bool(getattr(self.browser, "active", False)):
                    self._start_auth_checkpoint(self.browser)
                raise
            else:
                await asyncio.to_thread(self.browser.close)
                return batch

    async def fetch_thread(self, url: str) -> AtlasForumSnapshot:
        """Reuse the signed-in browser while serializing it with scheduled sync."""

        if not self.config.enabled or self._closed:
            raise AtlasForumSyncError("atlas_forum_sync_disabled")
        async with self._lock:
            try:
                snapshot = await asyncio.to_thread(self.browser.scrape_thread, url)
            except (AtlasForumManualActionRequired, AtlasForumSyncError):
                if bool(getattr(self.browser, "active", False)):
                    self._start_auth_checkpoint(self.browser)
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

    async def _ensure_default_feed(self) -> dict[str, Any]:
        """Seed the configured Majestic feed without resetting its schedule."""

        return await asyncio.to_thread(
            storage.atlas_ensure_forum_feed,
            self.guild_id,
            feed_key=self.config.feed_key,
            root_url=self.config.root_url,
            server_code=self.config.server_code,
            faction_code=self.config.faction_code,
            visibility_scope=self.config.visibility_scope,
            federation_scope=self.config.federation_scope,
            knowledge_domain=self.config.knowledge_domain,
            corpus_kind=self.config.corpus_kind,
            interval_seconds=self.config.interval_seconds,
        )

    async def sync_once(
        self,
        feed: dict[str, Any] | None = None,
        *,
        force: bool = True,
    ) -> dict[str, Any]:
        """Synchronise one claimed feed and preserve its exact corpus profile.

        ``force=True`` is intentionally retained for an explicit administrator
        action and compatibility with the old one-feed runner.  The background
        scheduler always passes ``force=False`` and claims only due feeds.
        """

        async with self._lock:
            configured = feed or await self._ensure_default_feed()
            claimed = await asyncio.to_thread(
                storage.atlas_forum_claim_feed,
                self.guild_id,
                int(configured["id"]),
                force=force,
            )
            if claimed is None:
                return {
                    "id": int(configured["id"]),
                    "feed_key": str(configured.get("feed_key") or ""),
                    "status": "skipped",
                    "last_stats": {"phase": "not_due"},
                }
            active_feed = claimed
            browser: AtlasForumBrowser | None = None
            phase = "feed_policy"
            try:
                browser = self._browser_for_feed(active_feed)
                phase = "forum_read"
                batch = await asyncio.to_thread(
                    browser.scrape_listing,
                    str(active_feed["root_url"]),
                )
                snapshots = list(batch.snapshots)
                phase = "knowledge_index"
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
                        int(active_feed["organization_id"]),
                        title=snapshot.title,
                        content=snapshot.content,
                        source_url=snapshot.url,
                        server_code=str(active_feed["server_code"]),
                        faction_code=str(active_feed["faction_code"]),
                        visibility_scope=str(active_feed["visibility_scope"]),
                        federation_scope=str(active_feed.get("federation_scope") or "") or None,
                        knowledge_domain=str(active_feed.get("knowledge_domain") or "") or None,
                        corpus_kind=str(active_feed.get("corpus_kind") or "") or None,
                        feed_key=str(active_feed["feed_key"]),
                        metadata={
                            "author": snapshot.author,
                            "source_updated_at": snapshot.source_updated_at,
                            "ingestion_origin": "scheduled_forum_feed",
                            "forum_attachments": [
                                attachment.public() for attachment in snapshot.attachments
                            ],
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
                        int(active_feed["organization_id"]),
                        feed_key=str(active_feed["feed_key"]),
                        seen_urls=seen_urls,
                    )
                stats = {
                    "phase": "knowledge_index" if index_errors else "complete",
                    "feed_key": str(active_feed["feed_key"]),
                    "project_code": str(active_feed["project_code"]),
                    "federation_scope": str(active_feed.get("federation_scope") or ""),
                    "knowledge_domain": str(active_feed.get("knowledge_domain") or ""),
                    "corpus_kind": str(active_feed.get("corpus_kind") or ""),
                    "pages": len(snapshots),
                    "skipped": len(batch.skipped_threads),
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
                    int(active_feed["id"]),
                    stats=stats,
                    error=partial_error,
                    attention=bool(index_errors),
                )
                if changed:
                    await self._technical_log(
                        title="Atlas обновил проверяемую базу",
                        details=(
                            f"Контур: `{active_feed['project_code']}` · "
                            f"лента: `{active_feed['feed_key']}`\n"
                            f"Проверено: {len(snapshots)}, изменено: {changed}, новых: {created}."
                        ),
                        level="info",
                        dedupe_key=(
                            f"atlas-forum-updated:{active_feed['id']}:"
                            f"{state.get('last_success_at')}"
                        ),
                    )
                await asyncio.to_thread(browser.close)
                return state
            except AtlasForumManualActionRequired as exc:
                if browser is not None:
                    self._start_auth_checkpoint(browser)
                state = await asyncio.to_thread(
                    storage.atlas_forum_sync_finished,
                    int(active_feed["id"]),
                    stats={"phase": phase, "feed_key": str(active_feed["feed_key"])},
                    error=str(exc),
                    attention=True,
                )
                await self._technical_log(
                    title="Atlas ждёт подтверждение форума",
                    details=(
                        f"Лента: `{active_feed['feed_key']}`\n{exc}\n"
                        "Откройте локальный Chromium Atlas и завершите проверку. "
                        "Последняя рабочая редакция продолжает использоваться."
                    ),
                    level="warning",
                    dedupe_key=f"atlas-forum-manual-action:{active_feed['id']}",
                    exception=exc,
                )
                return state
            except Exception as exc:
                keep_browser_open = browser is not None and phase == "forum_read" and bool(
                    getattr(browser, "active", False)
                )
                if keep_browser_open:
                    self._start_auth_checkpoint(browser)
                elif browser is not None:
                    await asyncio.to_thread(browser.close)
                state = await asyncio.to_thread(
                    storage.atlas_forum_sync_finished,
                    int(active_feed["id"]),
                    stats={"phase": phase, "feed_key": str(active_feed["feed_key"])},
                    error=f"{phase}:{type(exc).__name__}: {exc}",
                    attention=keep_browser_open,
                )
                await self._technical_log(
                    title="Ошибка синхронизации Atlas с форумом",
                    details=(
                        f"Лента: `{active_feed['feed_key']}` · этап: `{phase}`. "
                        f"Ошибка: `{type(exc).__name__}`. "
                        "Сохранённая законодательная база не изменена; следующая попытка состоится автоматически."
                    ),
                    level="warning",
                    dedupe_key=f"atlas-forum-sync:{active_feed['id']}:{type(exc).__name__}",
                    exception=exc,
                )
                return state

    async def run(self) -> None:
        """Run every due feed, not only the legacy environment-defined one."""

        if not self.config.enabled:
            return
        try:
            # Seed once before sleeping so a new installation has a durable
            # feed record immediately, but do not overwrite its next_sync_at.
            try:
                await self._ensure_default_feed()
            except Exception as exc:
                await self._technical_log(
                    title="Не удалось подготовить ленту Atlas",
                    details=f"Ошибка конфигурации: `{type(exc).__name__}`. Повтор будет выполнен автоматически.",
                    level="warning",
                    dedupe_key=f"atlas-forum-seed:{type(exc).__name__}",
                    exception=exc,
                )
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self.config.initial_delay_seconds,
                )
            except TimeoutError:
                pass
            self._wake.clear()
            while not self._closed:
                try:
                    default_feed = await self._ensure_default_feed()
                    due = await asyncio.to_thread(
                        storage.atlas_forum_due_feeds,
                        self.guild_id,
                        limit=8,
                    )
                except Exception as exc:
                    await self._technical_log(
                        title="Планировщик Atlas временно недоступен",
                        details=f"Ошибка: `{type(exc).__name__}`. Повтор будет выполнен автоматически.",
                        level="warning",
                        dedupe_key=f"atlas-forum-scheduler:{type(exc).__name__}",
                        exception=exc,
                    )
                    due = []
                    default_feed = None
                force_default = self._force_default_once
                self._force_default_once = False
                if force_default and default_feed is not None and not any(
                    int(feed["id"]) == int(default_feed["id"]) for feed in due
                ):
                    due.insert(0, default_feed)
                for feed in due:
                    if self._closed:
                        break
                    await self.sync_once(
                        feed,
                        force=bool(force_default and default_feed and int(feed["id"]) == int(default_feed["id"])),
                    )
                # Continue a long backlog promptly, but yield to the event
                # loop so overlay/chat traffic is never starved by indexing.
                if len(due) >= 8 and not self._closed:
                    await asyncio.sleep(0)
                    continue
                try:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=self.config.scheduler_poll_seconds,
                    )
                except TimeoutError:
                    pass
                self._wake.clear()
        except asyncio.CancelledError:
            raise
        finally:
            await self.close()

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        tasks = list(self._auth_checkpoint_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._auth_checkpoint_tasks.clear()
        browsers = list({id(browser): browser for browser in self._browsers.values()}.values())
        for browser in browsers:
            await asyncio.to_thread(browser.close)


__all__ = [
    "AtlasForumBrowser",
    "AtlasForumAttachment",
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
