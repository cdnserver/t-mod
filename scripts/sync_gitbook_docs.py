#!/usr/bin/env python3
"""Import every published TXSG GitBook page into the repository.

The published site is only used for the initial migration and drift checks.
After native GitBook Git Sync is enabled, GitHub and GitBook synchronize directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

SITE_URL = "https://txsg.gitbook.io/txsg-docs"
INDEX_URL = f"{SITE_URL}/llms.txt"
AGENT_APPENDIX = "\n---\n\n# Agent Instructions"
PAGE_URL_PATTERN = re.compile(r"https://txsg\.gitbook\.io/txsg-docs/[^)\s]+\.md")
PUBLISHED_HEADER_PATTERN = re.compile(r"^> For the complete documentation index,.*?\n\n", re.DOTALL)
FILE_LINK_PATTERN = re.compile(r"file:///\d+/([^)#]+\.md)")
BROKEN_LINK_PATTERN = re.compile(r"broken://pages/([a-f0-9]+)")
BROKEN_LINK_TARGETS = {
    "68f60ac647a7810c201d37f90a6737237a381c55": PurePosixPath("tvrs/finance.md"),
    "399642652a0d8325fb8d57811ca94121d01d7637": PurePosixPath("tvrs/craft.md"),
    "4683fcf42ffe5a0483bf153e44c42e2c26f9af16": PurePosixPath("tvrs/audit.md"),
    "43a6431c6647abc9d39f3719e4bf5c37cb43b15f": PurePosixPath("tvrs/bills.md"),
    "3ff17bfdcdf55e283ad3b4385e02c443abdbd288": PurePosixPath("glossary.md"),
    "144d1eb61e66062055021313ac25105556e3b690": PurePosixPath("consensus/decision-rules.md"),
    "8e67bb66205c349d270214e387c5f1234c37260c": PurePosixPath("consensus/participant.md"),
    "cc2d0decfe9ad029422f645f9f0523bc0aef7735": PurePosixPath("consensus/chair.md"),
    "f0aec188dff92b85697bf63f02f4fa782210ab74": PurePosixPath("sgl/payments-and-contracts.md"),
}
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "gitbook"


def fetch_text(url: str, attempts: int = 3) -> str:
    request = Request(url, headers={"User-Agent": "T-Mod GitBook importer/1.0"})
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS host from the public index
                return response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError) as exc:
            if attempt == attempts:
                raise RuntimeError(f"Не удалось загрузить {url}: {exc}") from exc
            time.sleep(attempt)
    raise AssertionError("unreachable")


def strip_gitbook_appendix(content: str) -> str:
    content = content.split(AGENT_APPENDIX, 1)[0]
    content = PUBLISHED_HEADER_PATTERN.sub("", content, count=1)
    return content.rstrip() + "\n"


def remote_path(url: str) -> PurePosixPath:
    path = unquote(urlparse(url).path)
    prefix = "/txsg-docs/"
    if not path.startswith(prefix) or not path.endswith(".md"):
        raise ValueError(f"Неожиданный адрес страницы: {url}")
    relative = PurePosixPath(path[len(prefix) :])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Небезопасный путь страницы: {url}")
    return relative


def local_path_for(remote: PurePosixPath) -> PurePosixPath:
    if remote == PurePosixPath("readme.md"):
        return PurePosixPath("README.md")
    if remote == PurePosixPath("summary.md"):
        return PurePosixPath("SUMMARY.md")
    return remote


def parse_page_urls(index: str) -> list[str]:
    urls = list(dict.fromkeys(PAGE_URL_PATTERN.findall(strip_gitbook_appendix(index))))
    if not urls:
        raise RuntimeError("В llms.txt не найдено ни одной Markdown-страницы")
    return urls


def rewrite_internal_links(
    content: str,
    current_local: PurePosixPath,
    path_map: dict[PurePosixPath, PurePosixPath],
) -> str:
    current_dir = str(current_local.parent)

    def relative_link(target: PurePosixPath) -> str:
        return os.path.relpath(str(target), start=current_dir).replace("\\", "/")

    for remote, target in sorted(path_map.items(), key=lambda item: len(str(item[0])), reverse=True):
        relative = relative_link(target)
        source_path = str(remote)
        content = content.replace(f"{SITE_URL}/{source_path}", relative)
        content = content.replace(f"/txsg-docs/{source_path}", relative)

    def replace_file_link(match: re.Match[str]) -> str:
        remote = PurePosixPath(match.group(1))
        if remote == PurePosixPath("help/troubleshooting.md"):
            remote = PurePosixPath("troubleshooting.md")
        target = path_map.get(remote, local_path_for(remote))
        return relative_link(target)

    def replace_broken_link(match: re.Match[str]) -> str:
        target = BROKEN_LINK_TARGETS.get(match.group(1))
        return relative_link(target) if target is not None else match.group(0)

    content = FILE_LINK_PATTERN.sub(replace_file_link, content)
    content = BROKEN_LINK_PATTERN.sub(replace_broken_link, content)
    return content


def build_snapshot() -> tuple[dict[PurePosixPath, str], dict[str, object]]:
    index = fetch_text(INDEX_URL)
    urls = parse_page_urls(index)
    path_map = {remote_path(url): local_path_for(remote_path(url)) for url in urls}
    pages: dict[PurePosixPath, str] = {}
    sources: dict[str, str] = {}
    for url in urls:
        remote = remote_path(url)
        local = path_map[remote]
        content = rewrite_internal_links(strip_gitbook_appendix(fetch_text(url)), local, path_map)
        pages[local] = content
        sources[str(local)] = url

    manifest_pages = {
        str(path): {
            "source": sources[str(path)],
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
        for path, content in sorted(pages.items(), key=lambda item: str(item[0]))
    }
    manifest: dict[str, object] = {
        "source_index": INDEX_URL,
        "page_count": len(pages),
        "pages": manifest_pages,
    }
    return pages, manifest


def expected_files(pages: dict[PurePosixPath, str], manifest: dict[str, object]) -> dict[PurePosixPath, str]:
    files = dict(pages)
    files[PurePosixPath(".source-manifest.json")] = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    return files


def write_snapshot(output: Path, files: dict[PurePosixPath, str]) -> None:
    for relative, content in files.items():
        destination = output.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    print(f"Выгружено страниц: {len(files) - 1}. Папка: {output}")


def check_snapshot(output: Path, files: dict[PurePosixPath, str]) -> int:
    missing: list[str] = []
    changed: list[str] = []
    for relative, expected in files.items():
        destination = output.joinpath(*relative.parts)
        if not destination.exists():
            missing.append(str(relative))
        elif destination.read_text(encoding="utf-8") != expected:
            changed.append(str(relative))
    if missing:
        print("Отсутствуют: " + ", ".join(missing))
    if changed:
        print("Отличаются от опубликованной версии: " + ", ".join(changed))
    if missing or changed:
        return 1
    print(f"Локальная копия совпадает с опубликованным GitBook ({len(files) - 1} страниц).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Импорт опубликованной документации TXSG из GitBook")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Папка локальной копии")
    parser.add_argument("--write", action="store_true", help="Записать опубликованные страницы на диск")
    args = parser.parse_args()

    pages, manifest = build_snapshot()
    files = expected_files(pages, manifest)
    output = args.output.resolve()
    if args.write:
        write_snapshot(output, files)
        return 0
    return check_snapshot(output, files)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
