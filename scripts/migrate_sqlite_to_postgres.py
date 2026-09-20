#!/usr/bin/env python3
"""Idempotent, all-or-nothing import of the production SQLite DB into PostgreSQL."""

from __future__ import annotations

import argparse
import base64
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
              AND table_name NOT IN (
                  'tmod_platform_migrations',
                  'tmod_platform_migration_quarantine'
              )
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


def _postgres_column_types(
    connection: Any, table: str
) -> dict[str, tuple[str, bool]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return {
            str(name): (str(data_type), str(nullable).upper() == "YES")
            for name, data_type, nullable in cursor.fetchall()
        }


def _clean_value(value: Any) -> Any:
    # PostgreSQL correctly rejects NUL in text; old Discord payloads can contain
    # it. SQLite allowed it, so preserve the surrounding text losslessly.
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


def _coerce_value(value: Any, data_type: str, nullable: bool) -> Any:
    if value is None:
        if not nullable:
            raise ValueError("required_value_missing")
        return None
    selected = str(data_type).lower()
    if selected in {"bigint", "integer", "smallint"}:
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"invalid_integer:{value!r}")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid_integer:{value!r}") from exc
    if selected in {"double precision", "real", "numeric", "decimal"}:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid_number:{value!r}") from exc
    if selected == "boolean":
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"invalid_boolean:{value!r}")
        return bool(value)
    if selected == "bytea":
        if isinstance(value, memoryview):
            return value.tobytes()
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        raise ValueError(f"invalid_binary:{type(value).__name__}")
    return _clean_value(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return {
            "type": "bytes",
            "base64": base64.b64encode(bytes(value)).decode("ascii"),
        }
    return str(value)


def migrate(sqlite_path: Path, *, force: bool = False) -> dict[str, Any]:
    if not postgres_enabled():
        raise RuntimeError("postgres_backend_not_enabled")

    pg = raw_postgres_connection()
    migration_lock_acquired = False
    try:
        # Compose, the interactive launcher and the update watcher can all ask
        # for the idempotent migration job.  PostgreSQL DDL is transactional,
        # but two simultaneous schema initializers can still deadlock while
        # acquiring relation locks in a different order.  A session-scoped
        # advisory lock makes the complete migration a single-writer job.  It
        # is released automatically even if this process is killed.
        with pg.cursor() as cursor:
            cursor.execute("SET statement_timeout = 0")
            cursor.execute(
                "SELECT pg_advisory_lock(hashtext(%s))",
                ("tmod-platform-migration-v1",),
            )
        pg.commit()
        migration_lock_acquired = True

        # Build the current schema before importing legacy rows. A second init
        # after import applies data migrations still pending in SQLite.
        storage.init_db()
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
                """
                CREATE TABLE IF NOT EXISTS tmod_platform_migration_quarantine (
                    id BIGSERIAL PRIMARY KEY,
                    migration_key TEXT NOT NULL,
                    source_table TEXT NOT NULL,
                    source_row_number BIGINT NOT NULL,
                    row_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL
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
                    allowed_seed_tables = {"atlas_projects", "atlas_servers", "atlas_factions", "meta"}
                    unexpected = sorted(set(occupied) - allowed_seed_tables)
                    if unexpected:
                        raise RuntimeError(
                            "postgres_target_not_empty:" + ",".join(unexpected[:12])
                        )

            counts: dict[str, int] = {}
            source_counts: dict[str, int] = {}
            quarantined: list[tuple[str, int, str, str]] = []
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
                    cursor.execute(
                        "TRUNCATE TABLE tmod_platform_migration_quarantine RESTART IDENTITY"
                    )

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
                        column_types = _postgres_column_types(pg, table)
                        quoted = ", ".join(f'"{name}"' for name in columns)
                        source = sqlite_connection.execute(
                            f'SELECT {quoted} FROM "{table}"'
                        )
                        imported = 0
                        source_row_number = 0
                        with cursor.copy(
                            f'COPY "{table}" ({quoted}) FROM STDIN'
                        ) as copy:
                            while True:
                                rows = source.fetchmany(1000)
                                if not rows:
                                    break
                                for row in rows:
                                    source_row_number += 1
                                    try:
                                        values = tuple(
                                            _coerce_value(
                                                row[name],
                                                column_types[name][0],
                                                column_types[name][1],
                                            )
                                            for name in columns
                                        )
                                    except (TypeError, ValueError) as exc:
                                        quarantined.append(
                                            (
                                                table,
                                                source_row_number,
                                                json.dumps(
                                                    {name: row[name] for name in columns},
                                                    ensure_ascii=False,
                                                    default=_json_default,
                                                ),
                                                f"{type(exc).__name__}: {exc}"[:1000],
                                            )
                                        )
                                        continue
                                    copy.write_row(values)
                                    imported += 1
                        counts[table] = imported
                        source_counts[table] = source_row_number

                    completed_at = datetime.now(timezone.utc).isoformat()
                    if quarantined:
                        cursor.executemany(
                            """
                            INSERT INTO tmod_platform_migration_quarantine(
                                migration_key, source_table, source_row_number,
                                row_json, error, created_at
                            ) VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            [
                                (
                                    MIGRATION_ID,
                                    table,
                                    row_number,
                                    row_json,
                                    error,
                                    completed_at,
                                )
                                for table, row_number, row_json, error in quarantined
                            ],
                        )

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

                    details = {
                        "status": "migrated",
                        "migration": MIGRATION_ID,
                        "sqlite_path": str(sqlite_path),
                        "sqlite_size_bytes": sqlite_path.stat().st_size,
                        "table_count": len(counts),
                        "row_count": sum(counts.values()),
                        "source_row_count": sum(source_counts.values()),
                        "quarantined_row_count": len(quarantined),
                        "quarantined_tables": sorted(
                            {table for table, _, _, _ in quarantined}
                        ),
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

        # Keep the migration advisory lock through the post-import schema
        # upgrades and validation. Otherwise a second migration job could
        # acquire the lock and enter its first init while this job is still in
        # its final init, recreating the same DDL deadlock at a later stage.
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
    finally:
        if migration_lock_acquired:
            try:
                pg.rollback()
                with pg.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))",
                        ("tmod-platform-migration-v1",),
                    )
                pg.commit()
            except Exception:
                # Closing the PostgreSQL session below always releases a
                # session-scoped advisory lock.
                pass
        pg.close()


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
