#!/usr/bin/env python3
"""Expand Git history into a denser Gource activity stream.

Each file change is emitted together with touches for its parent directories.
That makes the visualization feel more alive: the tree keeps reacting at the
file level, but the directory structure also gets motion and points.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def safe_component(value: str) -> str:
    return value.replace("|", "¦").replace("/", "∕")


def ancestry(path: Path) -> list[str]:
    if str(path) in {"", "."}:
        return []
    parts = list(path.parts)
    nodes: list[str] = []
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        nodes.append(current.as_posix())
    return nodes


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} OUTPUT", file=sys.stderr)
        return 2

    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)

    raw = git(
        "log",
        "--all",
        "--reverse",
        "--date-order",
        "--format=@@@%ct|%an|%h",
        "--name-status",
        "--find-renames",
    )

    timestamp = ""
    author = ""
    commit = ""
    sequence = 0
    rows: list[str] = []

    for line in raw.splitlines():
        if line.startswith("@@@"):
            timestamp, author, commit = line[3:].split("|", 2)
            sequence = 0
            continue
        if not line or not timestamp:
            continue

        fields = line.split("\t")
        status = fields[0][0]
        if status == "R" and len(fields) >= 3:
            path = fields[2]
            activity = "renamed"
        elif len(fields) >= 2:
            path = fields[1]
            activity = {
                "A": "added",
                "D": "deleted",
                "M": "modified",
                "T": "changed",
            }.get(status, "changed")
        else:
            continue

        source = Path(path)
        filename = safe_component(source.name)
        parent = source.parent.as_posix()
        if parent == ".":
            parent = "_root"

        sequence += 1

        # Every file event is also reflected through its directory chain so that
        # the map shows movement at multiple levels instead of only the leaf node.
        file_event_name = f"{commit}-{sequence:03d}.{activity}"
        file_event_path = f"/{parent}/{filename}/activity/{file_event_name}"
        rows.append(f"{timestamp}|{safe_component(author)}|A|{file_event_path}")

        for depth, directory in enumerate(ancestry(source.parent), start=1):
            sequence += 1
            dir_path = Path(directory)
            dir_name = safe_component(dir_path.name or "_root")
            dir_parent = Path(directory).parent.as_posix()
            if dir_parent == ".":
                dir_parent = "_root"
            dir_event_name = f"{commit}-{sequence:03d}.dir{depth}.{activity}"
            dir_event_path = f"/{dir_parent}/{dir_name}/activity/{dir_event_name}"
            rows.append(f"{timestamp}|{safe_component(author)}|M|{dir_event_path}")

    output.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} file-change events to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
