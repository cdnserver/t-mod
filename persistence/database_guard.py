"""Consistent SQLite backups and integrity telemetry for production runtime.

All snapshots use SQLite's online backup API.  Copying ``tmod.db`` directly is
not safe while WAL is active, so launcher, Reactor and the background worker
share this implementation instead of touching database files themselves.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from persistence import core as _core


VALID_BACKUP_KINDS = frozenset({"hourly", "daily", "manual", "pre-update", "startup"})
_guard_lock = threading.RLock()


def _serialized(function):
    def guarded(*args, **kwargs):
        with _guard_lock:
            return function(*args, **kwargs)

    guarded.__name__ = function.__name__
    guarded.__doc__ = function.__doc__
    return guarded


@dataclass(frozen=True, slots=True)
class DatabaseGuardConfig:
    backup_dir: Path
    hourly_retention: int
    daily_retention: int
    manual_retention: int
    update_retention: int
    min_free_bytes: int


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        selected = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        selected = int(default)
    return max(int(minimum), min(int(maximum), selected))


def database_guard_config() -> DatabaseGuardConfig:
    default_dir = _core.DATA_DIR.parent / "backups" / "database"
    return DatabaseGuardConfig(
        backup_dir=Path(os.getenv("TMOD_DB_BACKUP_DIR", str(default_dir))),
        hourly_retention=_env_int("TMOD_DB_HOURLY_RETENTION", 48, minimum=2, maximum=336),
        daily_retention=_env_int("TMOD_DB_DAILY_RETENTION", 30, minimum=2, maximum=365),
        manual_retention=_env_int("TMOD_DB_MANUAL_RETENTION", 20, minimum=2, maximum=100),
        update_retention=_env_int("TMOD_DB_UPDATE_RETENTION", 12, minimum=2, maximum=50),
        min_free_bytes=_env_int(
            "TMOD_DB_MIN_FREE_BYTES",
            512 * 1024 * 1024,
            minimum=64 * 1024 * 1024,
            maximum=100 * 1024 * 1024 * 1024,
        ),
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _state_path(config: DatabaseGuardConfig | None = None) -> Path:
    return (config or database_guard_config()).backup_dir / "guard-state.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(raw_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_kind(kind: str) -> str:
    selected = str(kind or "").strip().lower()
    if selected not in VALID_BACKUP_KINDS:
        raise ValueError("database_backup_kind_invalid")
    return selected


def _integrity_result(
    path: Path,
    *,
    full: bool = False,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    started = _utc_now()
    deadline = (
        time.monotonic() + max(0.1, float(timeout_seconds))
        if timeout_seconds is not None
        else None
    )
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection_timeout = 30.0 if timeout_seconds is None else min(5.0, max(0.1, float(timeout_seconds)))
    with sqlite3.connect(uri, uri=True, timeout=connection_timeout) as con:
        if deadline is not None:
            con.set_progress_handler(
                lambda: 1 if time.monotonic() >= deadline else 0,
                1_000,
            )
        pragma = "PRAGMA integrity_check" if full else "PRAGMA quick_check"
        try:
            rows = [str(row[0]) for row in con.execute(pragma).fetchall()]
        except sqlite3.OperationalError as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("database_integrity_check_timeout") from exc
            raise
    elapsed_ms = round((_utc_now() - started).total_seconds() * 1000, 1)
    ok = rows == ["ok"]
    return {
        "ok": ok,
        "result": "ok" if ok else "; ".join(rows[:8])[:1000],
        "mode": "full" if full else "quick",
        "checked_at": _utc_now().isoformat(),
        "latency_ms": elapsed_ms,
    }


@_serialized
def check_live_database(*, full: bool = False) -> dict[str, Any]:
    """Check the live DB deliberately; never called by a hot request path."""

    config = database_guard_config()
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = _integrity_result(_core.DATABASE_FILE, full=full)
    except Exception as exc:  # health boundary must remain serializable
        report = {
            "ok": False,
            "result": f"{type(exc).__name__}: {str(exc)[:700]}",
            "mode": "full" if full else "quick",
            "checked_at": _utc_now().isoformat(),
            "latency_ms": None,
        }
    state = _read_json(_state_path(config))
    state["last_integrity"] = report
    _atomic_json(_state_path(config), state)
    return report


@_serialized
def create_database_backup(
    kind: str = "manual",
    *,
    note: str | None = None,
    now: datetime | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Create, validate and atomically publish one consistent snapshot."""

    selected_kind = _validate_kind(kind)
    config = database_guard_config()
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(config.backup_dir).free
    source_size = _core.DATABASE_FILE.stat().st_size if _core.DATABASE_FILE.exists() else 0
    required = max(config.min_free_bytes, source_size * 3)
    if free_bytes < required:
        raise OSError("database_backup_insufficient_space")

    created = (now or _utc_now()).astimezone(timezone.utc)
    stamp = created.strftime("%Y%m%dT%H%M%S%fZ")
    final_path = config.backup_dir / f"tmod-{selected_kind}-{stamp}.db"
    temporary = config.backup_dir / f".{final_path.name}.partial"
    temporary.unlink(missing_ok=True)
    deadline = (
        time.monotonic() + max(0.1, float(timeout_seconds))
        if timeout_seconds is not None
        else None
    )

    def backup_progress(_: int, __: int, ___: int) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("database_backup_timeout")

    try:
        with _core._db_lock:
            connection_timeout = (
                30.0
                if timeout_seconds is None
                else min(5.0, max(0.1, float(timeout_seconds)))
            )
            with sqlite3.connect(
                _core.DATABASE_FILE,
                timeout=connection_timeout,
            ) as source:
                with sqlite3.connect(temporary, timeout=connection_timeout) as target:
                    source.backup(
                        target,
                        pages=256,
                        progress=backup_progress,
                        sleep=0.05,
                    )
                    target.execute("PRAGMA journal_mode=DELETE")
        remaining = (
            None
            if deadline is None
            else max(0.1, deadline - time.monotonic())
        )
        integrity = _integrity_result(
            temporary,
            full=selected_kind in {"daily", "manual", "pre-update"},
            timeout_seconds=remaining,
        )
        if not integrity["ok"]:
            raise sqlite3.DatabaseError(
                f"database_backup_integrity_failed:{integrity['result']}"
            )
        os.replace(temporary, final_path)
    finally:
        temporary.unlink(missing_ok=True)

    payload = {
        "name": final_path.name,
        "path": str(final_path),
        "kind": selected_kind,
        "created_at": created.isoformat(),
        "size_bytes": final_path.stat().st_size,
        "source_size_bytes": source_size,
        "integrity": integrity,
        "note": str(note or "").strip()[:500] or None,
    }
    _atomic_json(final_path.with_suffix(".json"), payload)
    state = _read_json(_state_path(config))
    state["last_backup"] = payload
    state["last_success_at"] = created.isoformat()
    _atomic_json(_state_path(config), state)
    prune_database_backups()
    return payload


@_serialized
def ensure_startup_recovery_point(
    *,
    note: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reuse a fresh validated snapshot or create one with a hard time budget."""

    current = (now or _utc_now()).astimezone(timezone.utc)
    reuse_minutes = _env_int(
        "TMOD_DB_STARTUP_REUSE_MINUTES",
        12 * 60,
        minimum=5,
        maximum=24 * 60,
    )
    for item in list_database_backups(limit=20):
        created_at = _parse_timestamp(item.get("created_at"))
        integrity = item.get("integrity") or {}
        path = Path(str(item.get("path") or ""))
        age = current - created_at if created_at is not None else None
        if (
            created_at is not None
            and age is not None
            and timedelta(0) <= age <= timedelta(minutes=reuse_minutes)
            and path.is_file()
            and bool(integrity.get("ok"))
        ):
            return {
                **item,
                "reused": True,
                "reuse_reason": "fresh_validated_backup",
            }

    timeout_seconds = _env_int(
        "TMOD_DB_STARTUP_BACKUP_TIMEOUT_SECONDS",
        20,
        minimum=5,
        maximum=300,
    )
    created = create_database_backup(
        "startup",
        note=note,
        now=current,
        timeout_seconds=float(timeout_seconds),
    )
    return {**created, "reused": False}


def list_database_backups(*, limit: int = 50) -> list[dict[str, Any]]:
    config = database_guard_config()
    if not config.backup_dir.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in config.backup_dir.glob("tmod-*.db"):
        metadata = _read_json(path.with_suffix(".json"))
        if not metadata:
            metadata = {
                "name": path.name,
                "path": str(path),
                "kind": "unknown",
                "created_at": datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
                "size_bytes": path.stat().st_size,
            }
        metadata["exists"] = path.exists()
        items.append(metadata)
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return items[: max(1, min(int(limit), 200))]


@_serialized
def prune_database_backups() -> dict[str, int]:
    config = database_guard_config()
    limits = {
        "hourly": config.hourly_retention,
        "daily": config.daily_retention,
        "manual": config.manual_retention,
        "pre-update": config.update_retention,
        "startup": config.update_retention,
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in list_database_backups(limit=200):
        grouped.setdefault(str(item.get("kind") or "unknown"), []).append(item)
    removed = 0
    for kind, items in grouped.items():
        keep = limits.get(kind, 5)
        for item in items[keep:]:
            path = Path(str(item.get("path") or ""))
            if path.parent != config.backup_dir or not path.name.startswith("tmod-"):
                continue
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)
            removed += 1
    return {"removed": removed, "remaining": len(list_database_backups(limit=200))}


@_serialized
def run_scheduled_database_protection(
    *, now: datetime | None = None
) -> dict[str, Any]:
    """Create due hourly/daily snapshots and periodically validate the source."""

    current = (now or _utc_now()).astimezone(timezone.utc)
    backups = list_database_backups(limit=200)

    def newest(kind: str) -> datetime | None:
        for item in backups:
            if item.get("kind") == kind:
                return _parse_timestamp(item.get("created_at"))
        return None

    created: list[dict[str, Any]] = []
    latest_any = _parse_timestamp(backups[0].get("created_at")) if backups else None
    last_hourly = newest("hourly")
    if (
        (last_hourly is None or current - last_hourly >= timedelta(hours=1))
        and (latest_any is None or current - latest_any >= timedelta(hours=1))
    ):
        created.append(create_database_backup("hourly", now=current))
    last_daily = newest("daily")
    if (
        (last_daily is None or last_daily.date() != current.date())
        and (latest_any is None or latest_any.date() != current.date())
    ):
        created.append(create_database_backup("daily", now=current))

    state = _read_json(_state_path())
    last_check = _parse_timestamp((state.get("last_integrity") or {}).get("checked_at"))
    integrity = state.get("last_integrity")
    if last_check is None or current - last_check >= timedelta(hours=6):
        integrity = check_live_database(full=False)
    return {
        "created": created,
        "integrity": integrity,
        "protection": database_protection_snapshot(),
    }


def database_protection_snapshot() -> dict[str, Any]:
    config = database_guard_config()
    backups = list_database_backups(limit=50)
    state = _read_json(_state_path(config))
    try:
        disk = shutil.disk_usage(config.backup_dir if config.backup_dir.exists() else config.backup_dir.parent)
        free_bytes: int | None = disk.free
        total_bytes: int | None = disk.total
    except OSError:
        free_bytes = None
        total_bytes = None
    latest_by_kind: dict[str, dict[str, Any]] = {}
    for item in backups:
        latest_by_kind.setdefault(str(item.get("kind") or "unknown"), item)
    last_integrity = state.get("last_integrity") or {}
    latest = backups[0] if backups else None
    status = "ok"
    if last_integrity and not bool(last_integrity.get("ok")):
        status = "critical"
    elif latest is None:
        status = "warning"
    elif (_utc_now() - (_parse_timestamp(latest.get("created_at")) or _utc_now())) > timedelta(hours=2):
        status = "warning"
    if free_bytes is not None and free_bytes < config.min_free_bytes:
        status = "critical"
    return {
        "status": status,
        "database_path": str(_core.DATABASE_FILE),
        "database_size_bytes": (
            _core.DATABASE_FILE.stat().st_size if _core.DATABASE_FILE.exists() else None
        ),
        "backup_dir": str(config.backup_dir),
        "backup_count": len(backups),
        "latest": latest,
        "latest_by_kind": latest_by_kind,
        "last_integrity": last_integrity or None,
        "free_bytes": free_bytes,
        "total_bytes": total_bytes,
        "retention": {
            "hourly": config.hourly_retention,
            "daily": config.daily_retention,
            "manual": config.manual_retention,
            "pre_update": config.update_retention,
        },
    }


@_serialized
def restore_database_backup(path: str | Path, *, offline_confirmed: bool = False) -> dict[str, Any]:
    """Restore a validated snapshot.  Caller must stop the bot first."""

    if not offline_confirmed:
        raise PermissionError("database_restore_requires_offline_confirmation")
    config = database_guard_config()
    selected = Path(path).resolve()
    if selected.parent != config.backup_dir.resolve() or not selected.name.startswith("tmod-"):
        raise ValueError("database_backup_path_invalid")
    integrity = _integrity_result(selected, full=True)
    if not integrity["ok"]:
        raise sqlite3.DatabaseError("database_restore_source_corrupt")
    target = _core.DATABASE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".restore.partial")
    shutil.copy2(selected, temporary)
    os.replace(temporary, target)
    target.with_name(target.name + "-wal").unlink(missing_ok=True)
    target.with_name(target.name + "-shm").unlink(missing_ok=True)
    return {
        "ok": True,
        "restored_from": str(selected),
        "database_path": str(target),
        "integrity": integrity,
    }


__all__ = [
    "DatabaseGuardConfig",
    "VALID_BACKUP_KINDS",
    "check_live_database",
    "create_database_backup",
    "database_guard_config",
    "database_protection_snapshot",
    "ensure_startup_recovery_point",
    "list_database_backups",
    "prune_database_backups",
    "restore_database_backup",
    "run_scheduled_database_protection",
]
