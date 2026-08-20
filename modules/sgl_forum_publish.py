"""Reviewed, normal-browser publication of SGL claim drafts to Majestic Forum.

This module deliberately does *not* automate captchas, login forms or browser
fingerprints.  It reuses the already configured Atlas Selenium session, pauses
for manual action when the forum asks for it, and only submits a draft after a
manager explicitly calls the publication endpoint.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from modules.atlas_forum_sync import (
    AtlasForumBrowser,
    AtlasForumManualActionRequired,
    AtlasForumSyncConfig,
    AtlasForumSyncError,
    forum_interstitial_kind,
)
from persistence.core import SGLCase


class SGLForumPublishError(RuntimeError):
    """A publication could not be submitted safely."""


class SGLForumPublishAttentionRequired(SGLForumPublishError):
    """A human must log in, grant permission, or complete forum verification."""


def _flag(name: str, default: bool = False) -> bool:
    return str(os.getenv(name, str(default))).strip().lower() in {"1", "true", "yes", "on"}


def _clean(value: Any, *, limit: int) -> str:
    return re.sub(r"\r\n?", "\n", str(value or "")).strip()[:limit]


@dataclass(frozen=True, slots=True)
class SGLForumPublishConfig:
    enabled: bool
    selenium_url: str
    root_url: str
    cookie_file: str
    challenge_wait_seconds: int = 45

    @classmethod
    def from_env(cls) -> "SGLForumPublishConfig":
        # Disabled until an operator explicitly connects an authenticated
        # browser. Drafting remains available, so no claim data is lost.
        try:
            challenge_wait_seconds = int(
                os.getenv("SGL_FORUM_CHALLENGE_WAIT_SECONDS", "45")
            )
        except (TypeError, ValueError):
            challenge_wait_seconds = 45
        return cls(
            enabled=_flag("SGL_FORUM_PUBLISH_ENABLED"),
            selenium_url=str(
                os.getenv("SGL_FORUM_SELENIUM_URL")
                or os.getenv("ATLAS_FORUM_SELENIUM_URL")
                or "http://atlas-forum-browser:4444/wd/hub"
            ).strip(),
            root_url=str(
                os.getenv("SGL_FORUM_ROOT_URL")
                or "https://forum.majestic-rp.ru/forums/"
            ).strip(),
            cookie_file=str(
                os.getenv("SGL_FORUM_COOKIE_FILE")
                or os.getenv("ATLAS_FORUM_COOKIE_FILE")
                or "/app/persistent/data/atlas-forum-cookies.json"
            ).strip(),
            challenge_wait_seconds=max(10, min(180, challenge_wait_seconds)),
        )

    def as_atlas_config(self) -> AtlasForumSyncConfig:
        return AtlasForumSyncConfig(
            enabled=self.enabled,
            selenium_url=self.selenium_url,
            root_url=self.root_url,
            cookie_file=self.cookie_file,
            feed_key="sgl-forum-publication",
            server_code="",
            faction_code="",
            visibility_scope="private",
            interval_seconds=86_400,
            initial_delay_seconds=5,
            page_delay_seconds=1.0,
            challenge_wait_seconds=self.challenge_wait_seconds,
            max_listing_pages=1,
            max_threads=1,
        )


def validate_forum_target(value: Any, config: SGLForumPublishConfig | None = None) -> str:
    """Permit only the configured forum host and a forum-section URL."""

    selected = config or SGLForumPublishConfig.from_env()
    target = str(value or "").strip()
    root = urlsplit(selected.root_url)
    parsed = urlsplit(target)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.netloc.casefold() != root.netloc.casefold()
        or not parsed.path.startswith("/forums/")
    ):
        raise ValueError("sgl_forum_target_invalid")
    return urlunsplit(("https", parsed.netloc.casefold(), parsed.path.rstrip("/") + "/", "", ""))


def claim_draft_for_case(case: SGLCase) -> tuple[str, str]:
    """Create an editable BBCode draft from the factual SGL case record."""

    number = f"{int(case.case_number):03d}"
    client = _clean(case.client_nick or case.client_display or "Не указан", limit=160)
    representative = _clean(case.lead_lawyer_display or "Не указан", limit=160)
    request_type = _clean(case.request_type or "Исковое заявление", limit=160)
    situation = _clean(case.situation_text or "Обстоятельства будут дополнены представителем.", limit=8_000)
    passport = _clean(case.passport_url or "Не указана", limit=1_000)
    static_id = _clean(case.static_id or "Не указан", limit=100)
    phone = _clean(case.phone or "Не указан", limit=100)
    body = "\n".join(
        (
            "[CENTER][B][SIZE=5]ИСКОВОЕ ЗАЯВЛЕНИЕ[/SIZE][/B][/CENTER]",
            "",
            f"[B]Вид обращения:[/B] {request_type}",
            f"[B]Номер дела SGL:[/B] {number}",
            f"[B]Заявитель:[/B] {client}",
            f"[B]Представитель:[/B] {representative}",
            f"[B]Статический ID:[/B] {static_id}",
            f"[B]Контакт:[/B] {phone}",
            f"[B]Документ:[/B] {passport}",
            "",
            "[B]Обстоятельства дела[/B]",
            situation,
            "",
            "[B]Просительная часть[/B]",
            "Прошу рассмотреть изложенные обстоятельства и принять решение в пределах компетенции суда.",
            "",
            "[I]Черновик подготовлен SGL Bureau. Перед публикацией сотрудник обязан проверить факты, ссылки и требования.[/I]",
        )
    )
    return f"{request_type} · SGL №{number}", body


class SGLForumPublisher:
    """Submit exactly one reviewed draft through the forum's ordinary UI."""

    def __init__(self, config: SGLForumPublishConfig | None = None) -> None:
        self.config = config or SGLForumPublishConfig.from_env()
        self._browser = AtlasForumBrowser(self.config.as_atlas_config())

    @staticmethod
    def _find_first(driver: Any, selectors: tuple[str, ...]) -> Any | None:
        from selenium.webdriver.common.by import By

        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                try:
                    if element.is_displayed() and element.is_enabled():
                        return element
                except Exception:
                    continue
        return None

    @staticmethod
    def _check_interstitial(driver: Any) -> None:
        kind = forum_interstitial_kind(str(driver.page_source or ""))
        if kind in {"login", "manual", "javascript", "access"}:
            messages = {
                "login": "Форум ожидает авторизацию в браузерной сессии.",
                "manual": "Форум запросил ручное подтверждение в браузере.",
                "javascript": "JavaScript-проверка форума ещё не завершилась.",
                "access": "У авторизованного форумного аккаунта нет доступа к разделу.",
            }
            raise SGLForumPublishAttentionRequired(messages[kind])

    def publish(self, *, target_url: Any, title: Any, body: Any) -> str:
        if not self.config.enabled:
            raise SGLForumPublishError("sgl_forum_publish_disabled")
        target = validate_forum_target(target_url, self.config)
        headline = _clean(title, limit=180)
        copy = _clean(body, limit=20_000)
        if not headline:
            raise SGLForumPublishError("sgl_forum_title_invalid")
        if not copy:
            raise SGLForumPublishError("sgl_forum_body_invalid")

        try:
            # _load follows the same cookie, challenge and manual-attention
            # rules as Atlas knowledge import. No HTTP form replay is used.
            self._browser._load(target)
            driver = self._browser._connect()
            self._check_interstitial(driver)
            create = self._find_first(
                driver,
                (
                    "a[href*='post-thread']",
                    "a[href*='create-thread']",
                    "a.button--cta[href*='thread']",
                ),
            )
            if create is None:
                raise SGLForumPublishAttentionRequired(
                    "Кнопка создания темы недоступна: проверьте права аккаунта и раздел форума."
                )
            driver.execute_script("arguments[0].click();", create)
            title_input = self._find_first(
                driver, ("input[name='title']", "input[name='thread_title']")
            )
            editor = self._find_first(
                driver,
                (
                    "textarea[name='message']",
                    "textarea[name='message_html']",
                    ".fr-element[contenteditable='true']",
                    "[contenteditable='true'][data-xf-init]",
                ),
            )
            if title_input is None or editor is None:
                self._check_interstitial(driver)
                raise SGLForumPublishAttentionRequired(
                    "Форум не открыл форму темы. Проверьте доступ к созданию исков."
                )
            title_input.clear()
            title_input.send_keys(headline)
            editor.click()
            editor.send_keys(copy)
            submit = self._find_first(
                driver,
                (
                    "form button[type='submit']",
                    "button[type='submit'][name='submit']",
                    ".formSubmitRow button.button--primary",
                ),
            )
            if submit is None:
                raise SGLForumPublishAttentionRequired(
                    "Не найдена кнопка публикации темы. Проверьте форму в браузере."
                )
            driver.execute_script("arguments[0].click();", submit)
            # The forum normally redirects synchronously after submitting a
            # XenForo form. A short browser-side ready check avoids guessing a
            # URL while keeping a request bounded by the driver's timeout.
            from selenium.webdriver.support.ui import WebDriverWait

            WebDriverWait(driver, 20).until(
                lambda active: "/threads/" in str(active.current_url or "")
            )
            self._check_interstitial(driver)
            published = str(driver.current_url or "").strip()
            parsed = urlsplit(published)
            if (
                parsed.scheme != "https"
                or parsed.netloc.casefold() != urlsplit(self.config.root_url).netloc.casefold()
                or not parsed.path.startswith("/threads/")
            ):
                raise SGLForumPublishError("sgl_forum_publish_result_invalid")
            self._browser.checkpoint_authentication()
            return urlunsplit(("https", parsed.netloc.casefold(), parsed.path.rstrip("/") + "/", "", ""))
        except AtlasForumManualActionRequired as exc:
            raise SGLForumPublishAttentionRequired(str(exc)) from exc
        except SGLForumPublishError:
            raise
        except AtlasForumSyncError as exc:
            raise SGLForumPublishError(str(exc)) from exc
        except Exception as exc:
            raise SGLForumPublishError(
                f"sgl_forum_publish_failed:{type(exc).__name__}"
            ) from exc
        finally:
            self._browser.close()


__all__ = [
    "SGLForumPublishAttentionRequired",
    "SGLForumPublishConfig",
    "SGLForumPublishError",
    "SGLForumPublisher",
    "claim_draft_for_case",
    "validate_forum_target",
]
