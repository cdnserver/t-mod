#!/usr/bin/env python3
"""Idempotent, all-or-nothing import of the production SQLite DB into PostgreSQL."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import storage  # noqa: E402
from persistence.postgres_compat import (  # noqa: E402
    postgres_enabled,
    raw_postgres_connection,
)


MIGRATION_ID = "sqlite-to-postgresql-v1"


def _sqlite_tables(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
    ]


def _postgres_tables(connection: Any) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
              AND table_name <> 'tmod_platform_migrations'
            ORDER BY table_name
            """
        )
        return [str(row[0]) for row in cursor.fetchall()]


def _postgres_columns(connection: Any, table: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return [str(row[0]) for row in cursor.fetchall()]


def _clean_value(value: Any) -> Any:
    # PostgreSQL correctly rejects NUL in text; old Discord payloads can contain
    # it. SQLite allowed it, so preserve the surrounding text losslessly.
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


def migrate(sqlite_path: Path, *, force: bool = False) -> dict[str, Any]:
    if not postgres_enabled():
        raise RuntimeError("postgres_backend_not_enabled")

    # Build the current schema before importing legacy rows. A second init after
    # import applies data migrations that were still pending in SQLite.
    storage.init_db()
    pg = raw_postgres_connection()
    try:
        with pg.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tmod_platform_migrations (
                    key TEXT PRIMARY KEY,
                    details_json TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                "SELECT details_json FROM tmod_platform_migrations WHERE key = %s",
                (MIGRATION_ID,),
            )
            existing = cursor.fetchone()
        pg.commit()
        if existing and not force:
            return {"status": "already_migrated", "migration": MIGRATION_ID}

        if not sqlite_path.is_file() or sqlite_path.stat().st_size == 0:
            details = {
                "status": "fresh_postgresql",
                "migration": MIGRATION_ID,
                "sqlite_path": str(sqlite_path),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            with pg.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO tmod_platform_migrations(key, details_json, completed_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT(key) DO UPDATE SET
                        details_json = excluded.details_json,
                        completed_at = excluded.completed_at
                    """,
                    (MIGRATION_ID, json.dumps(details), details["completed_at"]),
                )
            pg.commit()
            return details

        sqlite_connection = sqlite3.connect(
            f"file:{sqlite_path.resolve().as_posix()}?mode=ro", uri=True, timeout=30
        )
        sqlite_connection.row_factory = sqlite3.Row
        try:
            integrity = sqlite_connection.execute("PRAGMA quick_check").fetchone()[0]
            if str(integrity) != "ok":
                raise sqlite3.DatabaseError(f"sqlite_integrity_failed:{integrity}")
            source_tables = _sqlite_tables(sqlite_connection)
            target_tables = _postgres_tables(pg)
            shared_tables = [table for table in source_tables if table in target_tables]
            if not shared_tables:
                raise RuntimeError("sqlite_migration_no_shared_tables")

            if not force:
                with pg.cursor() as cursor:
                    occupied = []
                    for table in target_tables:
                        cursor.execute(f'SELECT EXISTS(SELECT 1 FROM "{table}" LIMIT 1)')
                        if cursor.fetchone()[0]:
                            occupied.append(table)
                    # init_db can create reference rows. They are safe to replace;
                    # user-owned rows in any other table indicate an unsafe retry.
                    allowed_seed_tables = {"atlas_servers", "atlas_factions", "meta"}
                    unexpected = sorted(set(occupied) - allowed_seed_tables)
                    if unexpected:
                        raise RuntimeError(
                            "postgres_target_not_empty:" + ",".join(unexpected[:12])
                        )

            counts: dict[str, int] = {}
            # Metadata inspection above starts an implicit psycopg transaction.
            # Close it before the atomic import; otherwise ``transaction()`` is
            # only a nested savepoint and connection.close() would roll the
            # successfully copied data back.
            pg.commit()
            with pg.transaction():
                with pg.cursor() as cursor:
                    cursor.execute("SET LOCAL session_replication_role = replica")
                    for table in reversed(target_tables):
                        cursor.execute(f'TRUNCATE TABLE "{table}" CASCADE')

                    for table in shared_tables:
                        sqlite_columns = [
                            str(row[1])
                            for row in sqlite_connection.execute(
                                f'PRAGMA table_info("{table}")'
                            ).fetchall()
                        ]
                        pg_columns = set(_postgres_columns(pg, table))
                        columns = [name for name in sqlite_columns if name in pg_columns]
                        if not columns:
                            continue
                        quoted = ", ".join(f'"{name}"' for name in columns)
                        source = sqlite_connection.execute(
                            f'SELECT {quoted} FROM "{table}"'
                        )
                        imported = 0
                        with cursor.copy(
                            f'COPY "{table}" ({quoted}) FROM STDIN'
                        ) as copy:
                            while True:
                                rows = source.fetchmany(1000)
                                if not rows:
                                    break
                                for row in rows:
                                    copy.write_row(
                                        tuple(_clean_value(row[name]) for name in columns)
                                    )
                                    imported += 1
                        counts[table] = imported

                    # Move every BIGSERIAL sequence past imported IDs.
                    cursor.execute(
                        """
                        SELECT table_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND column_name = 'id'
                        ORDER BY table_name
                        """
                    )
                    id_tables = [str(row[0]) for row in cursor.fetchall()]
                    for table in id_tables:
                        cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", (table,))
                        sequence = cursor.fetchone()[0]
                        if not sequence:
                            continue
                        cursor.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"')
                        maximum = int(cursor.fetchone()[0] or 0)
                        cursor.execute(
                            "SELECT setval(%s, %s, %s)",
                            (sequence, max(1, maximum), maximum > 0),
                        )

                    # Verify every imported table before the transaction can commit.
                    for table, source_count in counts.items():
                        cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                        target_count = int(cursor.fetchone()[0])
                        if target_count != source_count:
                            raise RuntimeError(
                                f"sqlite_migration_count_mismatch:{table}:"
                                f"{source_count}!={target_count}"
                            )

                    completed_at = datetime.now(timezone.utc).isoformat()
                    details = {
                        "status": "migrated",
                        "migration": MIGRATION_ID,
                        "sqlite_path": str(sqlite_path),
                        "sqlite_size_bytes": sqlite_path.stat().st_size,
                        "table_count": len(counts),
                        "row_count": sum(counts.values()),
                        "completed_at": completed_at,
                    }
                    cursor.execute(
                        """
                        INSERT INTO tmod_platform_migrations(key, details_json, completed_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT(key) DO UPDATE SET
                            details_json = excluded.details_json,
                            completed_at = excluded.completed_at
                        """,
                        (MIGRATION_ID, json.dumps(details), completed_at),
                    )
        finally:
            sqlite_connection.close()
    finally:
        pg.close()

    # Apply data migrations against the newly imported rows and validate access
    # through the same adapter used by every runtime process.
    storage.init_db()
    with storage.connect_readonly() as connection:
        connection.execute("SELECT 1").fetchone()
    if os.getenv("TMOD_MIGRATION_BACKUP_REQUIRED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        from persistence.database_guard import create_database_backup

        details["recovery_point"] = create_database_backup(
            "startup",
            note="Validated PostgreSQL recovery point after SQLite import",
            timeout_seconds=900,
        )["path"]
    return details


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sqlite",
        default=os.getenv("DATABASE_FILE", "/app/persistent/data/tmod.db"),
    )
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args()
    try:
        result = migrate(Path(arguments.sqlite), force=arguments.force)
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
