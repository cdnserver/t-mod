"""PostgreSQL backup, integrity and restore implementation for T-Mod."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from persistence.postgres_compat import (
    postgres_safe_target,
    postgres_settings,
    raw_postgres_connection,
)


VALID_KINDS = {"hourly", "daily", "manual", "pre-update", "startup"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _backup_dir() -> Path:
    return Path(os.getenv("TMOD_DB_BACKUP_DIR", "/app/persistent/backups/database"))


def _state_path() -> Path:
    return _backup_dir() / "guard-state.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _command(binary: str, *arguments: str) -> tuple[list[str], dict[str, str]]:
    settings = postgres_settings()
    environment = dict(os.environ)
    libpq_environment = {
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
        "target_session_attrs": "PGTARGETSESSIONATTRS",
        "connect_timeout": "PGCONNECT_TIMEOUT",
        "options": "PGOPTIONS",
    }
    for setting, environment_name in libpq_environment.items():
        if settings.get(setting) not in (None, ""):
            environment[environment_name] = str(settings[setting])
    if "conninfo" in settings:
        return [binary, "--dbname", str(settings["conninfo"]), *arguments], environment
    environment["PGPASSWORD"] = str(settings.get("password") or "")
    return [
        binary,
        "--host",
        str(settings["host"]),
        "--port",
        str(settings["port"]),
        "--username",
        str(settings["user"]),
        "--dbname",
        str(settings["dbname"]),
        *arguments,
    ], environment


def _database_size() -> int:
    with raw_postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_database_size(current_database())")
            return int(cursor.fetchone()[0])


def check_live_database(*, full: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with raw_postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                    """
                )
                table_count = int(cursor.fetchone()[0])
                if table_count <= 0:
                    raise RuntimeError("postgres_schema_empty")
                if full:
                    # Read every user table. This catches inaccessible/corrupt
                    # relations without holding long table locks.
                    cursor.execute(
                        """
                        SELECT quote_ident(table_name) FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                        ORDER BY table_name
                        """
                    )
                    tables = [str(row[0]) for row in cursor.fetchall()]
                    for table in tables:
                        cursor.execute(f"SELECT 1 FROM {table} LIMIT 1")
        report = {
            "ok": True,
            "result": "ok",
            "mode": "full" if full else "quick",
            "table_count": table_count,
            "checked_at": _now().isoformat(),
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }
    except Exception as exc:
        report = {
            "ok": False,
            "result": f"{type(exc).__name__}: {str(exc)[:700]}",
            "mode": "full" if full else "quick",
            "checked_at": _now().isoformat(),
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }
    state = _read_json(_state_path())
    state["last_integrity"] = report
    _write_json(_state_path(), state)
    return report


def create_database_backup(
    kind: str = "manual",
    *,
    note: str | None = None,
    now: datetime | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    selected = str(kind).strip().lower()
    if selected not in VALID_KINDS:
        raise ValueError("database_backup_kind_invalid")
    directory = _backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    source_size = _database_size()
    free = shutil.disk_usage(directory).free
    minimum = int(os.getenv("TMOD_DB_MIN_FREE_BYTES", str(512 * 1024 * 1024)))
    if free < max(minimum, source_size * 2):
        raise OSError("database_backup_insufficient_space")
    created = (now or _now()).astimezone(timezone.utc)
    stamp = created.strftime("%Y%m%dT%H%M%S%fZ")
    final = directory / f"tmod-{selected}-{stamp}.dump"
    temporary = directory / f".{final.name}.partial"
    command, environment = _command(
        "pg_dump",
        "--format=custom",
        "--compress=6",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(temporary),
    )
    lock_connection = raw_postgres_connection(autocommit=True)
    try:
        locked = lock_connection.execute(
            "SELECT pg_try_advisory_lock(hashtext('tmod-database-backup'))"
        ).fetchone()[0]
        if not locked:
            raise RuntimeError("database_backup_already_running")
        result = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"pg_dump_failed:{result.stderr[-700:]}")
        verification = subprocess.run(
            ["pg_restore", "--list", str(temporary)],
            capture_output=True,
            text=True,
            timeout=max(10.0, float(timeout_seconds or 300)),
            check=False,
        )
        if verification.returncode or not verification.stdout.strip():
            raise RuntimeError(f"pg_dump_validation_failed:{verification.stderr[-700:]}")
        os.replace(temporary, final)
    finally:
        temporary.unlink(missing_ok=True)
        try:
            lock_connection.execute(
                "SELECT pg_advisory_unlock(hashtext('tmod-database-backup'))"
            )
        except Exception:
            pass
        finally:
            lock_connection.close()
    payload = {
        "name": final.name,
        "path": str(final),
        "kind": selected,
        "backend": "postgresql",
        "created_at": created.isoformat(),
        "size_bytes": final.stat().st_size,
        "source_size_bytes": source_size,
        "database_target": postgres_safe_target(),
        "integrity": {"ok": True, "result": "pg_restore_list_ok"},
        "note": str(note or "").strip()[:500] or None,
    }
    _write_json(final.with_suffix(".json"), payload)
    state = _read_json(_state_path())
    state["last_backup"] = payload
    state["last_success_at"] = created.isoformat()
    _write_json(_state_path(), state)
    prune_database_backups()
    return payload


def list_database_backups(*, limit: int = 50) -> list[dict[str, Any]]:
    directory = _backup_dir()
    items: list[dict[str, Any]] = []
    for pattern in ("tmod-*.dump", "tmod-*.db"):
        for path in directory.glob(pattern):
            metadata = _read_json(path.with_suffix(".json"))
            if not metadata:
                metadata = {
                    "name": path.name,
                    "path": str(path),
                    "kind": "legacy" if path.suffix == ".db" else "unknown",
                    "backend": "sqlite" if path.suffix == ".db" else "postgresql",
                    "created_at": datetime.fromtimestamp(
                        path.stat().st_mtime, timezone.utc
                    ).isoformat(),
                    "size_bytes": path.stat().st_size,
                }
            metadata["exists"] = path.exists()
            items.append(metadata)
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return items[: max(1, min(200, int(limit)))]


def prune_database_backups() -> dict[str, int]:
    limits = {
        "hourly": int(os.getenv("TMOD_DB_HOURLY_RETENTION", "48")),
        "daily": int(os.getenv("TMOD_DB_DAILY_RETENTION", "30")),
        "manual": int(os.getenv("TMOD_DB_MANUAL_RETENTION", "20")),
        "pre-update": int(os.getenv("TMOD_DB_UPDATE_RETENTION", "12")),
        "startup": int(os.getenv("TMOD_DB_UPDATE_RETENTION", "12")),
        "legacy": 50,
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in list_database_backups(limit=200):
        grouped.setdefault(str(item.get("kind") or "unknown"), []).append(item)
    removed = 0
    directory = _backup_dir().resolve()
    for kind, items in grouped.items():
        for item in items[limits.get(kind, 5) :]:
            path = Path(str(item.get("path") or "")).resolve()
            if path.parent != directory or not path.name.startswith("tmod-"):
                continue
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)
            removed += 1
    return {"removed": removed, "remaining": len(list_database_backups(limit=200))}


def ensure_startup_recovery_point(
    *, note: str | None = None, now: datetime | None = None
) -> dict[str, Any]:
    current = (now or _now()).astimezone(timezone.utc)
    reuse = int(os.getenv("TMOD_DB_STARTUP_REUSE_MINUTES", "720"))
    current_target = postgres_safe_target()
    for item in list_database_backups(limit=20):
        if item.get("backend") != "postgresql" or not (item.get("integrity") or {}).get("ok"):
            continue
        # Never reuse a recovery point created for another PostgreSQL node.
        # This matters during localization/failover where the backup directory
        # survives but POSTGRES_HOST changes underneath it.
        if item.get("database_target") != current_target:
            continue
        try:
            created = datetime.fromisoformat(str(item["created_at"]))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        if timedelta(0) <= current - created <= timedelta(minutes=reuse):
            return {**item, "reused": True, "reuse_reason": "fresh_validated_backup"}
    result = create_database_backup(
        "startup",
        note=note,
        now=current,
        timeout_seconds=float(os.getenv("TMOD_DB_STARTUP_BACKUP_TIMEOUT_SECONDS", "300")),
    )
    return {**result, "reused": False}


def run_scheduled_database_protection(*, now: datetime | None = None) -> dict[str, Any]:
    current = (now or _now()).astimezone(timezone.utc)
    backups = [item for item in list_database_backups(limit=200) if item.get("backend") == "postgresql"]

    def latest(kind: str) -> datetime | None:
        for item in backups:
            if item.get("kind") == kind:
                try:
                    value = datetime.fromisoformat(str(item["created_at"]))
                    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
                except (KeyError, ValueError):
                    return None
        return None

    created: list[dict[str, Any]] = []
    hourly = latest("hourly")
    if hourly is None or current - hourly >= timedelta(hours=1):
        created.append(create_database_backup("hourly", now=current))
    daily = latest("daily")
    if daily is None or daily.date() != current.date():
        created.append(create_database_backup("daily", now=current))
    state = _read_json(_state_path())
    last = (state.get("last_integrity") or {}).get("checked_at")
    try:
        last_check = datetime.fromisoformat(str(last))
    except (TypeError, ValueError):
        last_check = None
    integrity = state.get("last_integrity")
    if last_check is None or current - last_check >= timedelta(hours=6):
        integrity = check_live_database(full=False)
    return {"created": created, "integrity": integrity, "protection": database_protection_snapshot()}


def database_protection_snapshot() -> dict[str, Any]:
    directory = _backup_dir()
    backups = list_database_backups(limit=50)
    postgres_backups = [item for item in backups if item.get("backend") == "postgresql"]
    state = _read_json(_state_path())
    try:
        disk = shutil.disk_usage(directory if directory.exists() else directory.parent)
        free, total = disk.free, disk.total
    except OSError:
        free, total = None, None
    latest = postgres_backups[0] if postgres_backups else None
    integrity = state.get("last_integrity") or {}
    status = "ok"
    if integrity and not integrity.get("ok"):
        status = "critical"
    elif latest is None:
        status = "warning"
    return {
        "status": status,
        "backend": "postgresql",
        "database_path": postgres_safe_target(),
        "database_size_bytes": _database_size(),
        "backup_dir": str(directory),
        "backup_count": len(postgres_backups),
        "legacy_sqlite_backup_count": len(backups) - len(postgres_backups),
        "latest": latest,
        "last_integrity": integrity or None,
        "free_bytes": free,
        "total_bytes": total,
    }


def restore_database_backup(path: str | Path, *, offline_confirmed: bool = False) -> dict[str, Any]:
    if not offline_confirmed:
        raise PermissionError("database_restore_requires_offline_confirmation")
    selected = Path(path).resolve()
    if selected.parent != _backup_dir().resolve() or selected.suffix != ".dump":
        raise ValueError("database_backup_path_invalid")
    check = subprocess.run(
        ["pg_restore", "--list", str(selected)], capture_output=True, text=True
    )
    if check.returncode:
        raise RuntimeError("database_restore_source_corrupt")
    command, environment = _command(
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        str(selected),
    )
    result = subprocess.run(command, env=environment, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"pg_restore_failed:{result.stderr[-700:]}")
    integrity = check_live_database(full=True)
    if not integrity["ok"]:
        raise RuntimeError("database_restore_integrity_failed")
    return {
        "ok": True,
        "backend": "postgresql",
        "restored_from": str(selected),
        "database_path": postgres_safe_target(),
        "integrity": integrity,
    }


__all__ = [
    "check_live_database",
    "create_database_backup",
    "database_protection_snapshot",
    "ensure_startup_recovery_point",
    "list_database_backups",
    "prune_database_backups",
    "restore_database_backup",
    "run_scheduled_database_protection",
]
