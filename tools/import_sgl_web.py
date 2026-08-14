#!/usr/bin/env python3
"""Import only the reviewed SGL frontend from its isolated private repository."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "web" / "sgl"
DEFAULT_REPOSITORY = "https://github.com/cdnserver/t-mod-sgl-web.git"
REQUIRED_FILES = frozenset({"index.html", "style.css", "app.js", "favicon.svg"})
FORBIDDEN_NAMES = frozenset({".env", "id_rsa", "id_ed25519", "credentials.json"})
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 40 * 1024 * 1024
MAX_FILES = 300


class SGLWebImportError(RuntimeError):
    pass


def _run(*args: str, cwd: Path = ROOT, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise SGLWebImportError(f"{' '.join(args[:2])}: {detail[-1200:]}")
    return str(result.stdout or "").strip()


def _ensure_target_clean() -> None:
    dirty = _run("git", "status", "--porcelain", "--", "web/sgl", capture=True)
    if dirty:
        raise SGLWebImportError(
            "В web/sgl есть незакоммиченные изменения. Сначала сохраните или отмените их."
        )


def _extract_product(archive: Path, destination: Path) -> Path:
    product = destination / "web" / "sgl"
    count = 0
    total = 0
    with tarfile.open(archive, "r:") as bundle:
        for member in bundle.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise SGLWebImportError(f"Небезопасный путь в архиве: {member.name}")
            if member.isdir() and path.parts in {("web",), ("web", "sgl")}:
                continue
            if path.parts[:2] != ("web", "sgl"):
                raise SGLWebImportError(f"Архив вышел за границу web/sgl: {member.name}")
            if member.issym() or member.islnk():
                raise SGLWebImportError(f"Ссылки запрещены: {member.name}")
            if member.isdir():
                continue
            if not member.isfile():
                raise SGLWebImportError(f"Неподдерживаемый объект: {member.name}")
            if path.name.casefold() in FORBIDDEN_NAMES or path.name.startswith(".env"):
                raise SGLWebImportError(f"Запрещённый файл: {member.name}")
            if member.size > MAX_FILE_BYTES:
                raise SGLWebImportError(f"Файл больше 10 МБ: {member.name}")
            count += 1
            total += member.size
            if count > MAX_FILES or total > MAX_TOTAL_BYTES:
                raise SGLWebImportError("SGL-пакет превышает безопасный размер")
            source = bundle.extractfile(member)
            if source is None:
                raise SGLWebImportError(f"Не удалось прочитать {member.name}")
            output = destination.joinpath(*path.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("wb") as stream:
                shutil.copyfileobj(source, stream)
    missing = [name for name in REQUIRED_FILES if not (product / name).is_file()]
    if missing:
        raise SGLWebImportError(f"В пакете нет обязательных файлов: {', '.join(sorted(missing))}")
    return product


def import_sgl_web(repository: str, reference: str, *, dry_run: bool = False) -> str:
    _ensure_target_clean()
    with tempfile.TemporaryDirectory(prefix="tmod-sgl-import-") as temporary:
        workspace = Path(temporary)
        repository_copy = workspace / "repository"
        archive = workspace / "sgl-web.tar"
        _run("git", "init", "--quiet", str(repository_copy), cwd=workspace)
        _run(
            "git", "fetch", "--no-tags", "--depth=1", repository, reference,
            cwd=repository_copy,
        )
        commit = _run("git", "rev-parse", "FETCH_HEAD", cwd=repository_copy, capture=True)
        _run(
            "git", "archive", "--format=tar", "-o", str(archive), commit, "web/sgl",
            cwd=repository_copy,
        )
        product = _extract_product(archive, workspace / "unpacked")
        if dry_run:
            return commit
        if TARGET.exists():
            shutil.rmtree(TARGET)
        shutil.copytree(product, TARGET)
        (TARGET / ".source.json").write_text(
            json.dumps(
                {
                    "repository": repository,
                    "commit": commit,
                    "imported_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return commit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", help="Полный commit SHA, tag или ветка SGL Web")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        commit = import_sgl_web(args.repository, args.reference, dry_run=args.dry_run)
    except SGLWebImportError as exc:
        parser.error(str(exc))
    print(f"SGL Web {'проверен' if args.dry_run else 'импортирован'}: {commit}")
    if not args.dry_run:
        print("Проверьте git diff, запустите тесты и только затем создайте commit в T-Mod.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
