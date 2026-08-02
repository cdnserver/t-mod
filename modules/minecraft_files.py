"""Sandboxed file, plugin, backup, and log management for Minecraft."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4


_MANAGER_DIRS = frozenset({".tmod-backups", ".tmod-trash", ".tmod-upload"})
_TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".conf",
        ".ini",
        ".json",
        ".json5",
        ".lang",
        ".log",
        ".md",
        ".properties",
        ".sk",
        ".toml",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_BLOCKED_SUFFIXES = frozenset({".key", ".p12", ".pfx", ".pem"})
_SECRET_PROPERTY_KEYS = frozenset({"rcon.password"})
_REDACTED_VALUE = "<скрыто T-Mod>"
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_SAFE_UPLOAD_SUFFIXES = frozenset(
    {
        *_TEXT_SUFFIXES,
        ".dat",
        ".jar",
        ".mca",
        ".mcr",
        ".nbt",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".zip",
    }
)


class MinecraftFilesError(RuntimeError):
    """A safe, user-facing file-manager failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class MinecraftFilesConfig:
    root: Path
    max_upload_bytes: int
    max_edit_bytes: int


def _bounded_env_bytes(name: str, default_mib: int, maximum_mib: int) -> int:
    try:
        selected = int(str(os.getenv(name, default_mib)).strip())
    except (TypeError, ValueError):
        selected = default_mib
    return max(1, min(maximum_mib, selected)) * 1024 * 1024


def minecraft_files_config() -> MinecraftFilesConfig:
    return MinecraftFilesConfig(
        root=Path(
            os.getenv("MINECRAFT_DATA_DIR", "/app/persistent/minecraft")
        ).expanduser(),
        max_upload_bytes=_bounded_env_bytes("MINECRAFT_UPLOAD_MAX_MIB", 128, 512),
        max_edit_bytes=_bounded_env_bytes("MINECRAFT_EDIT_MAX_MIB", 2, 8),
    )


def _utc_iso(timestamp: float | None = None) -> str:
    value = datetime.fromtimestamp(
        timestamp if timestamp is not None else datetime.now().timestamp(),
        tz=timezone.utc,
    )
    return value.isoformat()


def _ensure_root(config: MinecraftFilesConfig, *, create: bool = False) -> Path:
    root = config.root.resolve(strict=False)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise MinecraftFilesError(
            "minecraft_data_unavailable",
            "Папка Minecraft ещё не создана или недоступна контейнеру T-Mod.",
        )
    return root


def _safe_path(
    config: MinecraftFilesConfig,
    raw_path: Any,
    *,
    allow_root: bool = True,
    must_exist: bool = False,
) -> Path:
    root = _ensure_root(config)
    text = str(raw_path or "").strip().replace("\\", "/").strip("/")
    if not text:
        if allow_root:
            return root
        raise MinecraftFilesError("minecraft_path_required", "Выберите файл или папку.")
    pure = PurePosixPath(text)
    if pure.is_absolute() or any(
        part in {"", ".", ".."}
        or len(part) > 180
        or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
        for part in pure.parts
    ):
        raise MinecraftFilesError(
            "minecraft_path_invalid", "Некорректный путь Minecraft."
        )
    if any(part in _MANAGER_DIRS for part in pure.parts):
        raise MinecraftFilesError(
            "minecraft_path_protected",
            "Служебная область T-Mod недоступна из файлового менеджера.",
        )
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise MinecraftFilesError(
                "minecraft_symlink_forbidden",
                "Символические ссылки недоступны из веб-панели.",
            )
    resolved = current.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MinecraftFilesError(
            "minecraft_path_outside_root",
            "Путь выходит за пределы папки Minecraft.",
        ) from exc
    if must_exist and not resolved.exists():
        raise MinecraftFilesError(
            "minecraft_path_missing", "Файл или папка не найдены."
        )
    return resolved


def _relative(config: MinecraftFilesConfig, path: Path) -> str:
    return path.relative_to(config.root.resolve(strict=False)).as_posix()


def _clean_filename(value: Any) -> str:
    name = str(value or "").strip()
    windows_stem = name.split(".", 1)[0].upper()
    if (
        not name
        or len(name) > 180
        or name in {".", ".."}
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 or char in '<>:"|?*' for char in name)
        or name.endswith((" ", "."))
        or windows_stem in _WINDOWS_RESERVED_NAMES
    ):
        raise MinecraftFilesError("minecraft_name_invalid", "Некорректное имя файла.")
    if name in _MANAGER_DIRS:
        raise MinecraftFilesError(
            "minecraft_name_protected", "Это имя зарезервировано T-Mod."
        )
    return name


def _is_sensitive(path: Path) -> bool:
    lowered = path.name.lower()
    return (
        path.suffix.lower() in _BLOCKED_SUFFIXES
        or lowered in {".env", "secrets.json", "secrets.yml", "secrets.yaml"}
        or "private-key" in lowered
    )


def _is_editable(config: MinecraftFilesConfig, path: Path) -> bool:
    try:
        return (
            path.is_file()
            and not path.is_symlink()
            and not _is_sensitive(path)
            and path.suffix.lower() in _TEXT_SUFFIXES
            and path.stat().st_size <= config.max_edit_bytes
        )
    except OSError:
        return False


def _entry(config: MinecraftFilesConfig, path: Path) -> dict[str, Any]:
    stat = path.stat()
    relative = _relative(config, path)
    is_directory = path.is_dir()
    is_server_properties = relative.lower() == "server.properties"
    return {
        "name": path.name,
        "path": relative,
        "kind": "directory" if is_directory else "file",
        "size": None if is_directory else int(stat.st_size),
        "modified_at": _utc_iso(stat.st_mtime),
        "editable": False if is_directory else _is_editable(config, path),
        "downloadable": bool(
            not is_directory and not _is_sensitive(path) and not is_server_properties
        ),
        "plugin": relative.startswith("plugins/")
        and (path.name.endswith(".jar") or path.name.endswith(".jar.disabled")),
    }


def minecraft_storage_overview() -> dict[str, Any]:
    config = minecraft_files_config()
    try:
        root = _ensure_root(config)
    except MinecraftFilesError as exc:
        return {
            "available": False,
            "root": str(config.root),
            "error": str(exc),
            "max_upload_bytes": config.max_upload_bytes,
        }
    usage = shutil.disk_usage(root)
    return {
        "available": True,
        "root": str(root),
        "max_upload_bytes": config.max_upload_bytes,
        "max_edit_bytes": config.max_edit_bytes,
        "disk": {
            "total": int(usage.total),
            "used": int(usage.used),
            "free": int(usage.free),
        },
        "plugins": len(minecraft_list_plugins(config=config)),
        "backups": len(minecraft_list_backups(config=config)),
        "trash": len(minecraft_list_trash(config=config)),
    }


def minecraft_list_directory(
    relative_path: Any = "",
    *,
    config: MinecraftFilesConfig | None = None,
) -> dict[str, Any]:
    selected = config or minecraft_files_config()
    directory = _safe_path(selected, relative_path, must_exist=True)
    if not directory.is_dir():
        raise MinecraftFilesError(
            "minecraft_not_directory", "Выбранный путь не является папкой."
        )
    entries: list[dict[str, Any]] = []
    try:
        children = list(directory.iterdir())
    except OSError as exc:
        raise MinecraftFilesError(
            "minecraft_directory_unreadable", "Не удалось прочитать папку."
        ) from exc
    for child in children:
        if child.name in _MANAGER_DIRS or child.is_symlink():
            continue
        try:
            entries.append(_entry(selected, child))
        except OSError:
            continue
    entries.sort(
        key=lambda item: (item["kind"] != "directory", str(item["name"]).lower())
    )
    relative = (
        _relative(selected, directory) if directory != selected.root.resolve() else ""
    )
    parent = str(PurePosixPath(relative).parent)
    if parent == ".":
        parent = ""
    return {
        "path": relative,
        "parent": parent,
        "entries": entries[:1000],
        "truncated": len(entries) > 1000,
    }


def _etag(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _redact_properties(raw: str) -> tuple[str, list[str]]:
    redacted: list[str] = []
    output: list[str] = []
    for line in raw.splitlines(keepends=True):
        plain = line.rstrip("\r\n")
        separator = (
            "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        )
        if "=" in plain and not plain.lstrip().startswith(("#", "!")):
            key, _ = plain.split("=", 1)
            clean_key = key.strip().lower()
            if clean_key in _SECRET_PROPERTY_KEYS:
                output.append(f"{key}={_REDACTED_VALUE}{separator}")
                redacted.append(clean_key)
                continue
        output.append(line)
    return "".join(output), redacted


def _preserve_secret_properties(current: str, proposed: str) -> str:
    secrets: dict[str, str] = {}
    for line in current.splitlines():
        if "=" not in line or line.lstrip().startswith(("#", "!")):
            continue
        key, value = line.split("=", 1)
        if key.strip().lower() in _SECRET_PROPERTY_KEYS:
            secrets[key.strip().lower()] = value
    seen: set[str] = set()
    output: list[str] = []
    for line in proposed.splitlines():
        if "=" in line and not line.lstrip().startswith(("#", "!")):
            key, _ = line.split("=", 1)
            clean_key = key.strip().lower()
            if clean_key in _SECRET_PROPERTY_KEYS:
                if clean_key in secrets:
                    output.append(f"{key}={secrets[clean_key]}")
                    seen.add(clean_key)
                continue
        output.append(line)
    for key, value in secrets.items():
        if key not in seen:
            output.append(f"{key}={value}")
    suffix = "\n" if proposed.endswith(("\n", "\r\n")) else ""
    return "\n".join(output) + suffix


def minecraft_read_text(
    relative_path: Any,
    *,
    config: MinecraftFilesConfig | None = None,
) -> dict[str, Any]:
    selected = config or minecraft_files_config()
    path = _safe_path(selected, relative_path, allow_root=False, must_exist=True)
    if not _is_editable(selected, path):
        raise MinecraftFilesError(
            "minecraft_file_not_editable",
            "Этот файл нельзя безопасно редактировать в браузере.",
        )
    try:
        raw = path.read_bytes()
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MinecraftFilesError(
            "minecraft_file_encoding",
            "Редактор поддерживает только текстовые UTF-8 файлы.",
        ) from exc
    except OSError as exc:
        raise MinecraftFilesError(
            "minecraft_file_unreadable", "Не удалось прочитать файл."
        ) from exc
    redacted: list[str] = []
    if _relative(selected, path).lower() == "server.properties":
        content, redacted = _redact_properties(content)
    return {
        **_entry(selected, path),
        "content": content,
        "etag": _etag(raw),
        "redacted_keys": redacted,
    }


def _save_revision(config: MinecraftFilesConfig, path: Path, raw: bytes) -> None:
    revisions = config.root.resolve() / ".tmod-backups" / "edits"
    revisions.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    digest = hashlib.sha256(_relative(config, path).encode()).hexdigest()[:10]
    (revisions / f"{stamp}-{digest}-{path.name}").write_bytes(raw)


def minecraft_write_text(
    relative_path: Any,
    content: Any,
    *,
    expected_etag: str | None,
    config: MinecraftFilesConfig | None = None,
) -> dict[str, Any]:
    selected = config or minecraft_files_config()
    path = _safe_path(selected, relative_path, allow_root=False)
    if path.suffix.lower() not in _TEXT_SUFFIXES or _is_sensitive(path):
        raise MinecraftFilesError(
            "minecraft_file_not_editable",
            "Для этого типа файла веб-редактор отключён.",
        )
    if not path.parent.is_dir():
        raise MinecraftFilesError(
            "minecraft_parent_missing", "Родительская папка не найдена."
        )
    proposed = str(content or "")
    encoded = proposed.encode("utf-8")
    if len(encoded) > selected.max_edit_bytes:
        raise MinecraftFilesError(
            "minecraft_edit_too_large", "Файл слишком большой для редактора."
        )
    current = b""
    if path.exists():
        if not path.is_file() or path.is_symlink():
            raise MinecraftFilesError(
                "minecraft_file_not_editable", "Выбранный объект не является файлом."
            )
        if path.stat().st_size > selected.max_edit_bytes:
            raise MinecraftFilesError(
                "minecraft_edit_too_large",
                "Файл слишком большой для редактора.",
            )
        current = path.read_bytes()
        if not expected_etag:
            raise MinecraftFilesError(
                "minecraft_etag_required",
                "Обновите файл в редакторе перед сохранением.",
            )
        if expected_etag and _etag(current) != str(expected_etag):
            raise MinecraftFilesError(
                "minecraft_file_changed",
                "Файл уже изменился на сервере. Обновите редактор перед сохранением.",
            )
        if _relative(selected, path).lower() == "server.properties":
            try:
                current_text = current.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MinecraftFilesError(
                    "minecraft_file_encoding",
                    "Редактор поддерживает только текстовые UTF-8 файлы.",
                ) from exc
            proposed = _preserve_secret_properties(current_text, proposed)
            encoded = proposed.encode("utf-8")
        _save_revision(selected, path, current)
    elif _relative(selected, path).lower() == "server.properties":
        proposed = _preserve_secret_properties("", proposed)
        encoded = proposed.encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=".tmod-edit-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        path.chmod(0o644)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return minecraft_read_text(_relative(selected, path), config=selected)


def minecraft_make_directory(
    parent_path: Any,
    name: Any,
    *,
    config: MinecraftFilesConfig | None = None,
) -> dict[str, Any]:
    selected = config or minecraft_files_config()
    parent = _safe_path(selected, parent_path, must_exist=True)
    if not parent.is_dir():
        raise MinecraftFilesError(
            "minecraft_not_directory", "Родительский путь не является папкой."
        )
    target = _safe_path(
        selected, f"{_relative(selected, parent)}/{_clean_filename(name)}"
    )
    try:
        target.mkdir()
        target.chmod(0o755)
    except FileExistsError as exc:
        raise MinecraftFilesError(
            "minecraft_path_exists", "Такой файл или папка уже существует."
        ) from exc
    return _entry(selected, target)


def minecraft_rename_path(
    relative_path: Any,
    new_name: Any,
    *,
    config: MinecraftFilesConfig | None = None,
) -> dict[str, Any]:
    selected = config or minecraft_files_config()
    source = _safe_path(selected, relative_path, allow_root=False, must_exist=True)
    target = source.with_name(_clean_filename(new_name))
    _safe_path(selected, _relative(selected, target), allow_root=False)
    if target.exists():
        raise MinecraftFilesError(
            "minecraft_path_exists", "Объект с таким именем уже существует."
        )
    source.rename(target)
    return _entry(selected, target)


def minecraft_trash_path(
    relative_path: Any,
    *,
    config: MinecraftFilesConfig | None = None,
) -> dict[str, Any]:
    selected = config or minecraft_files_config()
    source = _safe_path(selected, relative_path, allow_root=False, must_exist=True)
    trash_id = (
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:8]}"
    )
    bucket = selected.root.resolve() / ".tmod-trash" / trash_id
    bucket.mkdir(parents=True, exist_ok=False)
    metadata = {
        "id": trash_id,
        "original_path": _relative(selected, source),
        "name": source.name,
        "kind": "directory" if source.is_dir() else "file",
        "deleted_at": _utc_iso(),
    }
    (bucket / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    shutil.move(str(source), str(bucket / "payload"))
    return metadata


def minecraft_list_trash(
    *,
    config: MinecraftFilesConfig | None = None,
) -> list[dict[str, Any]]:
    selected = config or minecraft_files_config()
    root = _ensure_root(selected) / ".tmod-trash"
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for bucket in root.iterdir():
        try:
            metadata = json.loads(
                (bucket / "metadata.json").read_text(encoding="utf-8")
            )
            if isinstance(metadata, dict) and (bucket / "payload").exists():
                items.append(metadata)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return sorted(
        items, key=lambda item: str(item.get("deleted_at") or ""), reverse=True
    )


def minecraft_restore_trash(
    trash_id: Any,
    *,
    config: MinecraftFilesConfig | None = None,
) -> dict[str, Any]:
    selected = config or minecraft_files_config()
    clean_id = str(trash_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{20,80}", clean_id):
        raise MinecraftFilesError(
            "minecraft_trash_invalid", "Некорректная запись корзины."
        )
    bucket = _ensure_root(selected) / ".tmod-trash" / clean_id
    try:
        metadata = json.loads((bucket / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MinecraftFilesError(
            "minecraft_trash_missing", "Запись корзины не найдена."
        ) from exc
    destination = _safe_path(selected, metadata.get("original_path"), allow_root=False)
    if destination.exists():
        raise MinecraftFilesError(
            "minecraft_restore_conflict",
            "По исходному пути уже существует файл или папка.",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(bucket / "payload"), str(destination))
    shutil.rmtree(bucket, ignore_errors=True)
    return _entry(selected, destination)


def minecraft_prepare_upload(
    *,
    config: MinecraftFilesConfig | None = None,
) -> Path:
    selected = config or minecraft_files_config()
    upload_root = _ensure_root(selected) / ".tmod-upload"
    upload_root.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="upload-", dir=upload_root)
    os.close(fd)
    return Path(name)


def minecraft_repair_plugin_permissions(
    *,
    config: MinecraftFilesConfig | None = None,
) -> int:
    """Make panel-uploaded JARs readable by the non-root Paper process."""

    selected = config or minecraft_files_config()
    plugins = _ensure_root(selected) / "plugins"
    if not plugins.is_dir():
        return 0
    plugins.chmod(0o755)
    repaired = 0
    for path in plugins.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        lower = path.name.lower()
        if not (lower.endswith(".jar") or lower.endswith(".jar.disabled")):
            continue
        if (path.stat().st_mode & 0o777) != 0o644:
            path.chmod(0o644)
            repaired += 1
    return repaired


def minecraft_finish_upload(
    directory_path: Any,
    filename: Any,
    temporary_path: Path,
    size: int,
    *,
    overwrite: bool = False,
    config: MinecraftFilesConfig | None = None,
) -> dict[str, Any]:
    selected = config or minecraft_files_config()
    directory = _safe_path(selected, directory_path, must_exist=True)
    if not directory.is_dir():
        raise MinecraftFilesError(
            "minecraft_not_directory", "Папка загрузки не найдена."
        )
    name = _clean_filename(filename)
    suffix = Path(name).suffix.lower()
    if suffix not in _SAFE_UPLOAD_SUFFIXES:
        raise MinecraftFilesError(
            "minecraft_upload_type_forbidden",
            "Этот тип файла нельзя загружать в Minecraft через веб-панель.",
        )
    if int(size) <= 0 or int(size) > selected.max_upload_bytes:
        raise MinecraftFilesError(
            "minecraft_upload_too_large", "Файл превышает лимит загрузки."
        )
    try:
        actual_size = temporary_path.stat().st_size
    except OSError as exc:
        raise MinecraftFilesError(
            "minecraft_upload_missing", "Временный файл загрузки не найден."
        ) from exc
    if actual_size != int(size):
        raise MinecraftFilesError(
            "minecraft_upload_size_mismatch",
            "Размер загруженного файла изменился во время обработки.",
        )
    relative_directory = _relative(selected, directory)
    if suffix == ".jar" and relative_directory != "plugins":
        raise MinecraftFilesError(
            "minecraft_plugin_directory_required",
            "JAR-плагины можно загружать только в папку plugins.",
        )
    target = directory / name
    _safe_path(selected, _relative(selected, target), allow_root=False)
    if target.exists() and not overwrite:
        raise MinecraftFilesError(
            "minecraft_upload_conflict",
            "Файл уже существует. Включите замену или переименуйте загрузку.",
        )
    if target.exists():
        minecraft_trash_path(_relative(selected, target), config=selected)
    os.replace(temporary_path, target)
    # mkstemp deliberately creates 0600 files.  Paper runs as a non-root user,
    # so an uploaded JAR/config must be made readable after the atomic move.
    target.chmod(0o644)
    return _entry(selected, target)


def minecraft_download_path(
    relative_path: Any,
    *,
    config: MinecraftFilesConfig | None = None,
) -> Path:
    selected = config or minecraft_files_config()
    path = _safe_path(selected, relative_path, allow_root=False, must_exist=True)
    if (
        not path.is_file()
        or _is_sensitive(path)
        or _relative(selected, path).lower() == "server.properties"
    ):
        raise MinecraftFilesError(
            "minecraft_download_forbidden",
            "Этот файл нельзя скачать через веб-панель.",
        )
    return path


def minecraft_list_plugins(
    *,
    config: MinecraftFilesConfig | None = None,
) -> list[dict[str, Any]]:
    selected = config or minecraft_files_config()
    minecraft_repair_plugin_permissions(config=selected)
    plugins = _ensure_root(selected) / "plugins"
    if not plugins.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in plugins.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        enabled = path.name.lower().endswith(".jar")
        disabled = path.name.lower().endswith(".jar.disabled")
        if not enabled and not disabled:
            continue
        item = _entry(selected, path)
        item["enabled"] = enabled
        item["display_name"] = path.name[:-13] if disabled else path.stem
        items.append(item)
    return sorted(items, key=lambda item: str(item["display_name"]).lower())


def minecraft_set_plugin_state(
    relative_path: Any,
    enabled: bool,
    *,
    config: MinecraftFilesConfig | None = None,
) -> dict[str, Any]:
    selected = config or minecraft_files_config()
    source = _safe_path(selected, relative_path, allow_root=False, must_exist=True)
    if source.parent != selected.root.resolve() / "plugins" or not source.is_file():
        raise MinecraftFilesError(
            "minecraft_plugin_invalid", "Выбранный файл не является плагином."
        )
    source.chmod(0o644)
    lower = source.name.lower()
    if enabled and lower.endswith(".jar.disabled"):
        target = source.with_name(source.name[:-9])
    elif not enabled and lower.endswith(".jar"):
        target = source.with_name(f"{source.name}.disabled")
    else:
        return {**_entry(selected, source), "enabled": lower.endswith(".jar")}
    if target.exists():
        raise MinecraftFilesError(
            "minecraft_plugin_conflict", "Файл плагина с таким именем уже существует."
        )
    source.rename(target)
    target.chmod(0o644)
    return {**_entry(selected, target), "enabled": bool(enabled)}


def _backup_root(config: MinecraftFilesConfig) -> Path:
    root = _ensure_root(config) / ".tmod-backups" / "full"
    root.mkdir(parents=True, exist_ok=True)
    return root


def minecraft_create_backup(
    label: Any = "manual",
    *,
    config: MinecraftFilesConfig | None = None,
) -> dict[str, Any]:
    selected = config or minecraft_files_config()
    root = _ensure_root(selected)
    clean_label = (
        re.sub(
            r"[^\w.-]+", "-", str(label or "manual").strip(), flags=re.UNICODE
        ).strip("-.")[:50]
        or "manual"
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = _backup_root(selected) / f"{stamp}-{clean_label}-{uuid4().hex[:6]}.zip"
    temporary = backup.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4
        ) as archive:
            for path in root.rglob("*"):
                relative = path.relative_to(root)
                if (
                    not relative.parts
                    or relative.parts[0] in _MANAGER_DIRS
                    or path.is_symlink()
                ):
                    continue
                if relative.parts[0] in {"cache", "logs"}:
                    continue
                if path.is_dir():
                    continue
                if relative.as_posix().lower() == "server.properties":
                    raw = path.read_text(encoding="utf-8")
                    sanitized, _ = _redact_properties(raw)
                    archive.writestr(relative.as_posix(), sanitized.encode("utf-8"))
                else:
                    archive.write(path, relative.as_posix())
        os.replace(temporary, backup)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return _backup_entry(backup)


def _backup_entry(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "id": path.name,
        "name": path.stem,
        "size": int(stat.st_size),
        "created_at": _utc_iso(stat.st_mtime),
    }


def minecraft_list_backups(
    *,
    config: MinecraftFilesConfig | None = None,
) -> list[dict[str, Any]]:
    selected = config or minecraft_files_config()
    root = _ensure_root(selected) / ".tmod-backups" / "full"
    if not root.is_dir():
        return []
    return sorted(
        (_backup_entry(path) for path in root.glob("*.zip") if path.is_file()),
        key=lambda item: str(item["created_at"]),
        reverse=True,
    )


def minecraft_backup_path(
    backup_id: Any,
    *,
    config: MinecraftFilesConfig | None = None,
) -> Path:
    selected = config or minecraft_files_config()
    name = _clean_filename(backup_id)
    if not name.endswith(".zip"):
        raise MinecraftFilesError(
            "minecraft_backup_invalid", "Некорректная резервная копия."
        )
    path = _backup_root(selected) / name
    if not path.is_file():
        raise MinecraftFilesError(
            "minecraft_backup_missing", "Резервная копия не найдена."
        )
    return path


def minecraft_delete_backup(
    backup_id: Any,
    *,
    config: MinecraftFilesConfig | None = None,
) -> dict[str, Any]:
    selected = config or minecraft_files_config()
    path = minecraft_backup_path(backup_id, config=selected)
    entry = _backup_entry(path)
    path.unlink()
    return entry


def minecraft_restore_backup(
    backup_id: Any,
    *,
    config: MinecraftFilesConfig | None = None,
) -> dict[str, Any]:
    selected = config or minecraft_files_config()
    root = _ensure_root(selected)
    backup = minecraft_backup_path(backup_id, config=selected)
    restored = 0
    with zipfile.ZipFile(backup, "r") as archive:
        for info in archive.infolist():
            pure = PurePosixPath(info.filename)
            if pure.is_absolute() or any(
                part in {"", ".", ".."} for part in pure.parts
            ):
                raise MinecraftFilesError(
                    "minecraft_backup_unsafe", "Архив содержит небезопасный путь."
                )
            if pure.parts[0] in _MANAGER_DIRS or info.is_dir():
                continue
            target = _safe_path(selected, pure.as_posix(), allow_root=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            if pure.as_posix().lower() == "server.properties":
                current = target.read_text(encoding="utf-8") if target.is_file() else ""
                proposed = archive.read(info).decode("utf-8")
                target.write_text(
                    _preserve_secret_properties(current, proposed),
                    encoding="utf-8",
                )
            else:
                with (
                    archive.open(info, "r") as source,
                    target.open("wb") as destination,
                ):
                    shutil.copyfileobj(source, destination)
            restored += 1
    return {"id": backup.name, "restored_files": restored, "root": str(root)}


def minecraft_tail_log(
    lines: int = 250,
    *,
    config: MinecraftFilesConfig | None = None,
) -> dict[str, Any]:
    selected = config or minecraft_files_config()
    path = _safe_path(selected, "logs/latest.log", allow_root=False)
    if not path.is_file():
        return {"available": False, "lines": [], "path": "logs/latest.log"}
    selected_lines = max(20, min(int(lines), 1000))
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 512 * 1024))
        raw = handle.read()
    content = raw.decode("utf-8", errors="replace").splitlines()[-selected_lines:]
    return {
        "available": True,
        "path": "logs/latest.log",
        "size": int(path.stat().st_size),
        "modified_at": _utc_iso(path.stat().st_mtime),
        "lines": content,
    }


__all__ = [
    "MinecraftFilesConfig",
    "MinecraftFilesError",
    "minecraft_backup_path",
    "minecraft_create_backup",
    "minecraft_delete_backup",
    "minecraft_download_path",
    "minecraft_files_config",
    "minecraft_finish_upload",
    "minecraft_list_backups",
    "minecraft_list_directory",
    "minecraft_list_plugins",
    "minecraft_list_trash",
    "minecraft_make_directory",
    "minecraft_prepare_upload",
    "minecraft_read_text",
    "minecraft_repair_plugin_permissions",
    "minecraft_rename_path",
    "minecraft_restore_backup",
    "minecraft_restore_trash",
    "minecraft_set_plugin_state",
    "minecraft_storage_overview",
    "minecraft_tail_log",
    "minecraft_trash_path",
    "minecraft_write_text",
]
