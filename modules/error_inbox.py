"""Durable, redacted runtime error delivery to a private GitHub issue inbox."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import traceback
from types import TracebackType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from persistence import error_repository as error_storage


LOGGER = logging.getLogger("tmod.error_inbox")
API_BASE = "https://api.github.com"
API_VERSION = "2026-03-10"
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|token)?\s*)[^\s,;]+"),
    re.compile(r"(?i)((?:token|secret|password|passwd|api[_-]?key|private[_-]?key)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"[A-Za-z0-9_-]{20,30}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,}"),
    re.compile(r"(?i)(https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/)[^\s/]+"),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.DOTALL,
    ),
)
_SENSITIVE_ENV_NAMES = (
    "DISCORD_TOKEN",
    "OPENROUTER_API_KEY",
    "MAJESTIC_API_KEY",
    "CONSENSUS_WEB_TOKEN",
    "ERROR_INBOX_GITHUB_TOKEN",
)


def _env_flag(name: str, default: bool) -> bool:
    fallback = "true" if default else "false"
    return os.getenv(name, fallback).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


@dataclass(frozen=True, slots=True)
class ErrorInboxConfig:
    capture_enabled: bool
    publish_enabled: bool
    repository: str
    token: str
    environment: str
    release: str
    label: str
    interval_seconds: int
    batch_size: int
    comment_cooldown_seconds: int
    dependency_occurrence_threshold: int
    retention_days: int
    request_timeout_seconds: int

    @property
    def publisher_ready(self) -> bool:
        return (
            self.capture_enabled
            and self.publish_enabled
            and bool(self.token)
            and bool(_REPOSITORY_PATTERN.fullmatch(self.repository))
        )


_active_config: ErrorInboxConfig | None = None


def _read_token_file(path: str) -> str:
    clean_path = str(path or "").strip()
    if not clean_path:
        return ""
    try:
        return Path(clean_path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def load_error_inbox_config() -> ErrorInboxConfig:
    token = os.getenv("ERROR_INBOX_GITHUB_TOKEN", "").strip()
    if not token:
        token = _read_token_file(os.getenv("ERROR_INBOX_GITHUB_TOKEN_FILE", ""))
    return ErrorInboxConfig(
        capture_enabled=_env_flag("ERROR_INBOX_CAPTURE_ENABLED", True),
        publish_enabled=_env_flag("ERROR_INBOX_PUBLISH_ENABLED", False),
        repository=os.getenv("ERROR_INBOX_GITHUB_REPOSITORY", "cdnserver/t-mod").strip(),
        token=token,
        environment=os.getenv("TMOD_ENVIRONMENT", "production").strip() or "production",
        release=os.getenv("TMOD_RELEASE", "unknown").strip() or "unknown",
        label=os.getenv("ERROR_INBOX_GITHUB_LABEL", "tmod-runtime").strip(),
        interval_seconds=_env_int("ERROR_INBOX_INTERVAL_SECONDS", 30, minimum=10, maximum=3600),
        batch_size=_env_int("ERROR_INBOX_BATCH_SIZE", 10, minimum=1, maximum=50),
        comment_cooldown_seconds=_env_int(
            "ERROR_INBOX_COMMENT_COOLDOWN_SECONDS",
            1800,
            minimum=60,
            maximum=86400,
        ),
        dependency_occurrence_threshold=_env_int(
            "ERROR_INBOX_DEPENDENCY_THRESHOLD",
            3,
            minimum=1,
            maximum=100,
        ),
        retention_days=_env_int("ERROR_INBOX_RETENTION_DAYS", 90, minimum=7, maximum=730),
        request_timeout_seconds=_env_int("ERROR_INBOX_TIMEOUT_SECONDS", 10, minimum=3, maximum=60),
    )


def sanitize_error_text(value: object, *, limit: int = 12000) -> str:
    text = _ANSI_PATTERN.sub("", str(value or "")).replace("\x00", "")
    for name in _SENSITIVE_ENV_NAMES:
        secret = os.getenv(name, "")
        if len(secret) >= 8:
            text = text.replace(secret, "[REDACTED]")
    for index, pattern in enumerate(_SECRET_PATTERNS):
        if index in {0, 1, 3}:
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    text = text.replace("```", "'''" ).strip()
    return text[: max(1, int(limit))]


@dataclass(frozen=True, slots=True)
class ErrorClassification:
    category: str
    priority: str
    publish: bool
    title: str
    component: str
    fingerprint_hint: str | None
    next_action: str


def _root_exception(exc: BaseException | None) -> BaseException | None:
    current = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        next_error = current.__cause__ or current.__context__
        if not isinstance(next_error, BaseException):
            break
        current = next_error
    return current


def _traceback_signature(exc: BaseException) -> str:
    root = _root_exception(exc) or exc
    frames = traceback.extract_tb(root.__traceback__)
    application_frames = [
        frame
        for frame in frames
        if "/app/" in frame.filename.replace("\\", "/")
        or "/modules/" in frame.filename.replace("\\", "/")
        or "/persistence/" in frame.filename.replace("\\", "/")
    ]
    selected = application_frames[-5:] or frames[-3:]
    # Line numbers change after harmless edits. File + callable still identifies
    # the failing application path without splitting one defect every release.
    stable_frames = [f"{Path(frame.filename).name}:{frame.name}" for frame in selected]
    if stable_frames:
        return "|".join([type(root).__name__, *stable_frames])
    message = re.sub(r"\b\d{4,}\b", "<id>", str(root).lower())[:240]
    return f"{type(root).__name__}:{message}"


def classify_runtime_error(
    *,
    title: str,
    details: str,
    component: str,
    level: str = "error",
    exception: BaseException | None = None,
    exception_type: str | None = None,
) -> ErrorClassification:
    root = _root_exception(exception)
    exception_name = str(exception_type or (type(root).__name__ if root else ""))
    searchable = "\n".join((str(title), str(details), str(component), exception_name)).lower()

    if "unknown interaction" in searchable or "error code: 10062" in searchable:
        return ErrorClassification(
            category="interaction-expired",
            priority="P4",
            publish=False,
            title="Discord-взаимодействие уже завершилось",
            component="discord.interaction",
            fingerprint_hint="noise:discord-interaction-expired",
            next_action=(
                "Issue не требуется: Discord уже закрыл нажатие или форму. "
                "Событие остаётся в техническом журнале, но не засоряет очередь разработки."
            ),
        )

    disconnected_client = exception_name in {
        "ConnectionResetError",
        "ClientConnectionResetError",
        "BrokenPipeError",
    } and any(
        marker in searchable
        for marker in ("connection lost", "closing transport", "aiohttp.server", "broken pipe")
    )
    if disconnected_client:
        return ErrorClassification(
            category="client-disconnect",
            priority="P4",
            publish=False,
            title="Клиент закрыл соединение",
            component="web.client",
            fingerprint_hint="noise:web-client-disconnect",
            next_action=(
                "Issue не требуется: браузер или Discord закрыл транспорт до завершения запроса."
            ),
        )

    network_failure = any(
        marker in searchable
        for marker in (
            "clientconnectorerror",
            "clientconnectordnserror",
            "temporary failure in name resolution",
            "connect call failed",
            "cannot connect to host",
        )
    )
    if network_failure:
        service = "discord" if any(
            marker in searchable for marker in ("discord.com", "discord.client", "discord.gateway")
        ) else "external"
        return ErrorClassification(
            category="dependency",
            priority="P2",
            publish=True,
            title=(
                "Discord временно недоступен"
                if service == "discord"
                else "Внешний сервис временно недоступен"
            ),
            component=f"dependency.{service}",
            fingerprint_hint=f"dependency:{service}:connectivity",
            next_action=(
                "Проверить сеть/DNS и доступность внешнего сервиса. Программный дефект "
                "создаётся только после нескольких повторов одного инцидента."
            ),
        )

    critical = str(level).lower() == "critical"
    return ErrorClassification(
        category="defect",
        priority="P1" if critical else "P2",
        publish=True,
        title=str(title or "Необработанная ошибка"),
        component=str(component or "tmod"),
        fingerprint_hint=None,
        next_action="Воспроизвести по traceback, исправить первопричину и добавить регрессионный тест.",
    )


def error_fingerprint(*, hint: str | None, title: str, component: str, exception: BaseException | None) -> str:
    basis = str(hint or "").strip()
    if not basis and exception is not None:
        basis = _traceback_signature(exception)
    if not basis:
        basis = f"{component}:{title}"
    return hashlib.sha256(basis.encode("utf-8", errors="replace")).hexdigest()


def _record_safely(
    *,
    title: str,
    details: str,
    component: str,
    level: str,
    fingerprint_hint: str | None,
    exception: BaseException | None,
    config: ErrorInboxConfig,
) -> bool:
    if not config.capture_enabled or str(level).lower() not in {"error", "critical"}:
        return False
    try:
        classification = classify_runtime_error(
            title=title,
            details=details,
            component=component,
            level=level,
            exception=exception,
        )
        if not classification.publish:
            return False
        root = _root_exception(exception)
        trace_text = ""
        if exception is not None:
            trace_text = "".join(
                traceback.format_exception(type(exception), exception, exception.__traceback__)
            )
        error_storage.record_runtime_error(
            fingerprint=error_fingerprint(
                hint=classification.fingerprint_hint or fingerprint_hint,
                title=classification.title,
                component=classification.component,
                exception=root,
            ),
            title=sanitize_error_text(classification.title, limit=180),
            component=sanitize_error_text(classification.component, limit=120),
            level=str(level).lower(),
            exception_type=(type(root).__name__ if root is not None else None),
            details=sanitize_error_text(details, limit=12000),
            traceback_text=sanitize_error_text(trace_text, limit=20000) if trace_text else None,
            environment=sanitize_error_text(config.environment, limit=80),
            release=sanitize_error_text(config.release, limit=120),
        )
        return True
    except Exception:
        # The error reporter must never become a new source of application errors.
        return False


async def capture_runtime_event(
    *,
    title: str,
    details: str,
    component: str = "tmod",
    level: str = "error",
    fingerprint_hint: str | None = None,
    exception: BaseException | None = None,
    config: ErrorInboxConfig | None = None,
) -> bool:
    selected = config or _active_config
    if selected is None:
        selected = await asyncio.to_thread(load_error_inbox_config)
    return await asyncio.to_thread(
        _record_safely,
        title=title,
        details=details,
        component=component,
        level=level,
        fingerprint_hint=fingerprint_hint,
        exception=exception,
        config=selected,
    )


def capture_runtime_event_sync(
    *,
    title: str,
    details: str,
    component: str = "tmod",
    level: str = "error",
    fingerprint_hint: str | None = None,
    exception: BaseException | None = None,
    config: ErrorInboxConfig | None = None,
) -> bool:
    return _record_safely(
        title=title,
        details=details,
        component=component,
        level=level,
        fingerprint_hint=fingerprint_hint,
        exception=exception,
        config=config or _active_config or load_error_inbox_config(),
    )


def _record_classification(record: dict[str, Any]) -> ErrorClassification:
    return classify_runtime_error(
        title=str(record.get("title") or "Необработанная ошибка"),
        details=str(record.get("details") or ""),
        component=str(record.get("component") or "tmod"),
        level=str(record.get("level") or "error"),
        exception_type=str(record.get("exception_type") or ""),
    )


def _issue_title(record: dict[str, Any]) -> str:
    classification = _record_classification(record)
    title = f"[T-Mod][{classification.priority}] {classification.title}"
    return title[:240]


def _issue_labels(record: dict[str, Any], base_label: str) -> list[str]:
    classification = _record_classification(record)
    labels = [base_label] if base_label else []
    labels.append(
        "runtime:dependency" if classification.category == "dependency" else "runtime:defect"
    )
    labels.append(
        "priority:high" if classification.priority == "P1" else "priority:normal"
    )
    return list(dict.fromkeys(labels))


def _issue_body(record: dict[str, Any]) -> str:
    details = sanitize_error_text(record.get("details"), limit=12000) or "Нет дополнительных сведений."
    trace_text = sanitize_error_text(record.get("traceback_text"), limit=20000)
    classification = _record_classification(record)
    lines = [
        f"<!-- tmod-runtime:{record['fingerprint']} -->",
        "## Автоматический отчёт T-Mod",
        "",
        f"- **Приоритет:** `{classification.priority}`",
        f"- **Категория:** `{classification.category}`",
        f"- **Компонент:** `{record.get('component') or 'tmod'}`",
        f"- **Уровень:** `{record.get('level') or 'error'}`",
        f"- **Исключение:** `{record.get('exception_type') or 'не указано'}`",
        f"- **Окружение:** `{record.get('environment') or 'unknown'}`",
        f"- **Версия:** `{record.get('release') or 'unknown'}`",
        f"- **Первый случай:** `{record.get('first_seen_at')}`",
        f"- **Последний случай:** `{record.get('last_seen_at')}`",
        f"- **Количество:** `{int(record.get('occurrences') or 1)}`",
        f"- **Fingerprint:** `{record['fingerprint']}`",
        "",
        "### Контекст",
        "```text",
        details,
        "```",
        "",
        "### Следующее действие",
        classification.next_action,
    ]
    if trace_text:
        lines.extend(("", "### Traceback", "```text", trace_text, "```"))
    lines.extend(
        (
            "",
            "> Отчёт очищен от известных токенов и секретов. "
            "Закройте Issue после выпуска исправления; повтор ошибки откроет его снова.",
        )
    )
    return "\n".join(lines)[:60000]


def _recurrence_comment(record: dict[str, Any]) -> str:
    details = sanitize_error_text(record.get("details"), limit=8000) or "Нет дополнительных сведений."
    trace_text = sanitize_error_text(record.get("traceback_text"), limit=12000)
    lines = [
        "## Ошибка повторилась",
        "",
        f"- **Последний случай:** `{record.get('last_seen_at')}`",
        f"- **Количество случаев:** `{int(record.get('occurrences') or 1)}`",
        f"- **Версия:** `{record.get('release') or 'unknown'}`",
        "",
        "```text",
        details,
        "```",
    ]
    if trace_text:
        lines.extend(("", "<details><summary>Последний traceback</summary>", "", "```text", trace_text, "```", "</details>"))
    return "\n".join(lines)[:60000]


class GitHubIssuePublisher:
    def __init__(self, config: ErrorInboxConfig) -> None:
        self.config = config

    def _request(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{API_BASE}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "T-Mod-Error-Inbox/1",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        try:
            with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                body = response.read(1_000_000)
        except HTTPError as exc:
            response_body = exc.read(4000).decode("utf-8", errors="replace")
            raise RuntimeError(
                f"github_http_{exc.code}: {sanitize_error_text(response_body, limit=1200)}"
            ) from exc
        except (OSError, URLError) as exc:
            raise RuntimeError(f"github_transport_error: {type(exc).__name__}") from exc
        try:
            decoded = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("github_invalid_json") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("github_unexpected_response")
        return decoded

    def _create_issue(self, record: dict[str, Any]) -> tuple[int, str]:
        repository = self.config.repository
        payload: dict[str, Any] = {
            "title": _issue_title(record),
            "body": _issue_body(record),
        }
        payload["labels"] = _issue_labels(record, self.config.label)
        try:
            result = self._request("POST", f"/repos/{repository}/issues", payload)
        except RuntimeError as exc:
            if "github_http_422" not in str(exc) or "labels" not in payload:
                raise
            payload.pop("labels", None)
            result = self._request("POST", f"/repos/{repository}/issues", payload)
        number = int(result["number"])
        url = str(result.get("html_url") or f"https://github.com/{repository}/issues/{number}")
        return number, url

    def publish(self, record: dict[str, Any]) -> tuple[int, str]:
        repository = self.config.repository
        issue_number = record.get("github_issue_number")
        if issue_number is None:
            return self._create_issue(record)

        number = int(issue_number)
        update_payload: dict[str, Any] = {
            "state": "open",
            "title": _issue_title(record),
            "body": _issue_body(record),
            "labels": _issue_labels(record, self.config.label),
        }
        try:
            self._request("PATCH", f"/repos/{repository}/issues/{number}", update_payload)
        except RuntimeError as exc:
            # A manually deleted issue must not poison the durable outbox forever.
            if "github_http_404" in str(exc):
                return self._create_issue(record)
            if "github_http_422" in str(exc):
                update_payload.pop("labels", None)
                self._request("PATCH", f"/repos/{repository}/issues/{number}", update_payload)
            else:
                raise
        self._request(
            "POST",
            f"/repos/{repository}/issues/{number}/comments",
            {"body": _recurrence_comment(record)},
        )
        return number, str(
            record.get("github_issue_url") or f"https://github.com/{repository}/issues/{number}"
        )


async def publish_error_inbox_once(config: ErrorInboxConfig | None = None) -> int:
    selected = config or load_error_inbox_config()
    if not selected.publisher_ready:
        return 0
    records = await asyncio.to_thread(
        error_storage.list_runtime_errors_ready,
        limit=min(100, selected.batch_size * 5),
        comment_cooldown_seconds=selected.comment_cooldown_seconds,
    )
    publisher = GitHubIssuePublisher(selected)
    published = 0
    for record in records:
        if published >= selected.batch_size:
            break
        classification = _record_classification(record)
        if (
            classification.category == "dependency"
            and int(record.get("occurrences") or 1)
            < selected.dependency_occurrence_threshold
        ):
            continue
        try:
            number, url = await asyncio.to_thread(publisher.publish, record)
            await asyncio.to_thread(
                error_storage.mark_runtime_error_published,
                str(record["fingerprint"]),
                published_occurrences=int(record.get("occurrences") or 1),
                github_issue_number=number,
                github_issue_url=url,
            )
            published += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await asyncio.to_thread(
                error_storage.mark_runtime_error_failed,
                str(record["fingerprint"]),
                error=sanitize_error_text(exc, limit=1800),
            )
    return published


async def error_inbox_worker(config: ErrorInboxConfig | None = None) -> None:
    selected = config or load_error_inbox_config()
    cycles = 0
    while True:
        try:
            await publish_error_inbox_once(selected)
            cycles += 1
            if cycles == 1 or cycles % 120 == 0:
                await asyncio.to_thread(
                    error_storage.prune_runtime_error_inbox,
                    retention_days=selected.retention_days,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never create a reporting loop when GitHub or SQLite is unavailable.
            pass
        await asyncio.sleep(selected.interval_seconds)


class ErrorInboxLoggingHandler(logging.Handler):
    def __init__(self, loop: asyncio.AbstractEventLoop, config: ErrorInboxConfig) -> None:
        super().__init__(level=logging.ERROR)
        self.loop = loop
        self.config = config

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(LOGGER.name):
            return
        try:
            exception = record.exc_info[1] if record.exc_info else None
            details = self.format(record)
            def schedule() -> None:
                self.loop.create_task(
                    capture_runtime_event(
                        title=f"Ошибка журнала {record.name}",
                        details=details,
                        component=record.name,
                        level="error",
                        # The exception's application traceback is a much more stable
                        # fingerprint than the line where a third-party logger emitted it.
                        fingerprint_hint=None,
                        exception=exception,
                        config=self.config,
                    )
                )

            self.loop.call_soon_threadsafe(schedule)
        except Exception:
            return


_runtime_installed = False
_worker_task: asyncio.Task[None] | None = None
_logging_handler: ErrorInboxLoggingHandler | None = None
_original_sys_excepthook = sys.excepthook
_original_threading_excepthook = threading.excepthook


def setup_error_inbox_runtime(loop: asyncio.AbstractEventLoop) -> None:
    global _active_config, _logging_handler, _runtime_installed, _worker_task
    if _runtime_installed:
        return
    _runtime_installed = True
    config = load_error_inbox_config()
    _active_config = config
    if not config.capture_enabled:
        print("Error Inbox: disabled", flush=True)
        return

    previous_loop_handler = loop.get_exception_handler()

    def loop_exception_handler(active_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exception = context.get("exception")
        message = str(context.get("message") or "Unhandled asyncio exception")
        active_loop.create_task(
            capture_runtime_event(
                title="Необработанная ошибка asyncio",
                details=message,
                component="asyncio",
                fingerprint_hint=None,
                exception=exception if isinstance(exception, BaseException) else None,
                config=config,
            )
        )
        if previous_loop_handler is not None:
            previous_loop_handler(active_loop, context)
        else:
            active_loop.default_exception_handler(context)

    loop.set_exception_handler(loop_exception_handler)

    def process_excepthook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        if exc_value.__traceback__ is None and exc_traceback is not None:
            exc_value = exc_value.with_traceback(exc_traceback)
        capture_runtime_event_sync(
            title="Критическая ошибка процесса",
            details=str(exc_value),
            component="process",
            level="critical",
            exception=exc_value,
            config=config,
        )
        _original_sys_excepthook(exc_type, exc_value, exc_traceback)

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        capture_runtime_event_sync(
            title="Необработанная ошибка потока",
            details=f"Поток: {getattr(args.thread, 'name', 'unknown')}",
            component="threading",
            level="critical",
            exception=args.exc_value,
            config=config,
        )
        _original_threading_excepthook(args)

    sys.excepthook = process_excepthook
    threading.excepthook = thread_excepthook
    _logging_handler = ErrorInboxLoggingHandler(loop, config)
    logging.getLogger().addHandler(_logging_handler)
    _worker_task = loop.create_task(error_inbox_worker(config), name="tmod-error-inbox")

    state = "GitHub publisher ready" if config.publisher_ready else "local spool only"
    print(f"Error Inbox: {state}; repository={config.repository}", flush=True)


__all__ = [
    "ErrorInboxConfig",
    "ErrorInboxLoggingHandler",
    "GitHubIssuePublisher",
    "capture_runtime_event",
    "capture_runtime_event_sync",
    "classify_runtime_error",
    "error_fingerprint",
    "error_inbox_worker",
    "load_error_inbox_config",
    "publish_error_inbox_once",
    "sanitize_error_text",
    "setup_error_inbox_runtime",
]
