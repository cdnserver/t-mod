#!/usr/bin/env python3
"""Fetch the private GitHub runtime-error inbox into a reviewable Markdown file."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_VERSION = "2026-03-10"
TITLE_PREFIX = "[T-Mod runtime]"


def _resolve_token(explicit: str) -> str:
    token = explicit.strip() or os.getenv("ERROR_INBOX_GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _fetch_issues(repository: str, token: str, state: str) -> list[dict[str, Any]]:
    query = urlencode({"state": state, "per_page": 100, "sort": "updated", "direction": "desc"})
    request = Request(
        f"https://api.github.com/repos/{repository}/issues?{query}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "T-Mod-Error-Inbox-Fetch/1",
            "X-GitHub-Api-Version": API_VERSION,
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read(2_000_000).decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"GitHub вернул HTTP {exc.code}") from exc
    except (OSError, URLError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Не удалось получить GitHub Issues: {type(exc).__name__}") from exc
    if not isinstance(payload, list):
        raise RuntimeError("GitHub вернул неожиданный формат")
    return [
        issue
        for issue in payload
        if isinstance(issue, dict)
        and "pull_request" not in issue
        and str(issue.get("title") or "").startswith(TITLE_PREFIX)
    ]


def _markdown(repository: str, issues: list[dict[str, Any]]) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    lines = [
        "# T-Mod Runtime Error Inbox",
        "",
        f"Репозиторий: `{repository}`  ",
        f"Получено: `{generated_at}`  ",
        f"Открытых отчётов: **{len(issues)}**",
        "",
    ]
    if not issues:
        lines.append("Открытых runtime-ошибок нет.")
        return "\n".join(lines) + "\n"
    for issue in issues:
        number = int(issue.get("number") or 0)
        title = str(issue.get("title") or "Без названия").removeprefix(TITLE_PREFIX).strip()
        url = str(issue.get("html_url") or "")
        updated_at = str(issue.get("updated_at") or "")
        body = str(issue.get("body") or "Нет описания.").strip()
        lines.extend(
            (
                f"## [#{number} · {title}]({url})",
                "",
                f"Обновлено: `{updated_at}`",
                "",
                body,
                "",
                "---",
                "",
            )
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository",
        default=os.getenv("ERROR_INBOX_GITHUB_REPOSITORY", "cdnserver/t-mod"),
    )
    parser.add_argument("--token", default="", help="Не рекомендуется: используйте gh auth или ENV")
    parser.add_argument("--state", choices=("open", "closed", "all"), default="open")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    token = _resolve_token(args.token)
    if not token:
        print(
            "Нет GitHub-токена. Выполните `gh auth login` или задайте "
            "ERROR_INBOX_GITHUB_TOKEN.",
            file=sys.stderr,
        )
        return 2
    try:
        issues = _fetch_issues(str(args.repository), token, str(args.state))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    report = _markdown(str(args.repository), issues)
    if args.output is None:
        sys.stdout.write(report)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"Сохранено: {args.output} ({len(issues)} отчётов)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
