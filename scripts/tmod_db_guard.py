#!/usr/bin/env python3
"""Offline/launcher interface for T-Mod database protection."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from persistence.database_guard import (  # noqa: E402
    check_live_database,
    create_database_backup,
    database_protection_snapshot,
    list_database_backups,
    restore_database_backup,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="T-Mod SQLite protection utility")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup")
    backup.add_argument(
        "--kind",
        default="manual",
        choices=("hourly", "daily", "manual", "pre-update", "startup"),
    )
    backup.add_argument("--note", default="")

    check = subparsers.add_parser("check")
    check.add_argument("--full", action="store_true")

    listing = subparsers.add_parser("list")
    listing.add_argument("--limit", type=int, default=20)

    restore = subparsers.add_parser("restore")
    restore.add_argument("path")
    restore.add_argument("--offline-confirmed", action="store_true")

    subparsers.add_parser("status")
    arguments = parser.parse_args()
    try:
        if arguments.command == "backup":
            payload = create_database_backup(arguments.kind, note=arguments.note)
        elif arguments.command == "check":
            payload = check_live_database(full=arguments.full)
        elif arguments.command == "list":
            payload = {"items": list_database_backups(limit=arguments.limit)}
        elif arguments.command == "restore":
            payload = restore_database_backup(
                arguments.path,
                offline_confirmed=arguments.offline_confirmed,
            )
        else:
            payload = database_protection_snapshot()
    except Exception as exc:  # CLI boundary
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({"ok": True, "result": payload}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
