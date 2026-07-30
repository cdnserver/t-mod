#!/usr/bin/env python3
"""Expand Git history into one Gource node per file change."""

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

        sequence += 1
        source = Path(path)
        filename = safe_component(source.name)
        parent = source.parent.as_posix()
        if parent == ".":
            parent = "_root"

        # Every node is a real file-change event, grouped by directory and file.
        event_name = f"{commit}-{sequence:03d}.{activity}"
        event_path = f"/{parent}/{filename}/activity/{event_name}"
        rows.append(f"{timestamp}|{safe_component(author)}|A|{event_path}")

    output.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} file-change events to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
