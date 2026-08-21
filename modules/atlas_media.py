"""Local content-addressed storage for Atlas Files + Media."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from persistence import core as persistence_core


class AtlasMediaError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = str(code)
        self.retryable = bool(retryable)


def _bounded_bytes(name: str, default_mib: int, maximum_mib: int) -> int:
    try:
        value = int(str(os.getenv(name, str(default_mib))).strip())
    except (TypeError, ValueError):
        value = default_mib
    return max(1, min(maximum_mib, value)) * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AtlasMediaConfig:
    root: Path
    max_asset_bytes: int
    quota_bytes: int
    max_chunk_bytes: int
    scan_command: str

    @classmethod
    def from_env(cls) -> "AtlasMediaConfig":
        default_root = persistence_core.DATA_DIR.parent / "atlas-media"
        return cls(
            root=Path(os.getenv("ATLAS_MEDIA_DIR", str(default_root))).expanduser(),
            max_asset_bytes=_bounded_bytes("ATLAS_MEDIA_MAX_ASSET_MIB", 2048, 16384),
            quota_bytes=_bounded_bytes("ATLAS_MEDIA_WORKSPACE_QUOTA_MIB", 51200, 524288),
            max_chunk_bytes=_bounded_bytes("ATLAS_MEDIA_CHUNK_MIB", 8, 32),
            scan_command=str(os.getenv("ATLAS_MEDIA_SCAN_COMMAND", "")).strip(),
        )


class AtlasLocalBlobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._lock = threading.RLock()

    def path(self, storage_key: str) -> Path:
        clean = str(storage_key or "").replace("\\", "/").lstrip("/")
        candidate = (self.root / clean).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise AtlasMediaError("atlas_media_path_escape", "Недопустимый путь файла.") from exc
        return candidate

    def append_chunk(self, storage_key: str, *, offset: int, data: bytes) -> int:
        if not data:
            raise AtlasMediaError("atlas_media_chunk_empty", "Получен пустой фрагмент файла.")
        target = self.path(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            current = target.stat().st_size if target.exists() else 0
            expected_end = int(offset) + len(data)
            if current > int(offset):
                if current != expected_end:
                    raise AtlasMediaError(
                        "atlas_media_upload_offset_conflict",
                        "Позиция загрузки изменилась. Продолжите с актуального смещения.",
                    )
                with target.open("rb") as stream:
                    stream.seek(int(offset))
                    if stream.read(len(data)) != data:
                        raise AtlasMediaError(
                            "atlas_media_chunk_conflict",
                            "Повторный фрагмент не совпадает с сохранённым.",
                        )
                return current
            if current != int(offset):
                raise AtlasMediaError(
                    "atlas_media_upload_offset_conflict",
                    "Позиция загрузки изменилась. Продолжите с актуального смещения.",
                )
            mode = "r+b" if target.exists() else "w+b"
            with target.open(mode) as stream:
                stream.seek(int(offset))
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            return expected_end

    def checksum_and_size(self, storage_key: str) -> tuple[str, int]:
        target = self.path(storage_key)
        if not target.is_file():
            raise AtlasMediaError(
                "atlas_media_blob_missing",
                "Загруженный файл временно недоступен.",
                retryable=True,
            )
        digest = hashlib.sha256()
        size = 0
        with target.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def head(self, storage_key: str, size: int = 4096) -> bytes:
        target = self.path(storage_key)
        with target.open("rb") as stream:
            return stream.read(max(64, min(64 * 1024, int(size))))

    def final_key(self, checksum_sha256: str) -> str:
        digest = str(checksum_sha256).lower()
        return f"objects/{digest[:2]}/{digest[2:4]}/{digest}"

    def finalize(self, temp_key: str, final_key: str, *, expected_size: int) -> Path:
        temporary = self.path(temp_key)
        final = self.path(final_key)
        final.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if final.is_file():
                if final.stat().st_size != int(expected_size):
                    raise AtlasMediaError(
                        "atlas_media_blob_size_conflict",
                        "Хранилище обнаружило несовпадающий объект.",
                        retryable=True,
                    )
                if temporary.exists():
                    temporary.unlink()
                return final
            if not temporary.is_file():
                raise AtlasMediaError(
                    "atlas_media_blob_missing",
                    "Загруженный файл временно недоступен.",
                    retryable=True,
                )
            os.replace(temporary, final)
        return final

    def delete(self, storage_key: str) -> None:
        target = self.path(storage_key)
        with self._lock:
            if target.is_file():
                target.unlink()


def atlas_media_detect_type(head: bytes, declared: str | None) -> str:
    data = bytes(head or b"")
    clean_declared = str(declared or "").split(";", 1)[0].strip().lower()
    if data.startswith((b"MZ", b"\x7fELF")):
        raise AtlasMediaError(
            "atlas_media_executable_rejected",
            "Исполняемые файлы нельзя помещать в медиатеку Atlas.",
        )
    detected = "application/octet-stream"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        detected = "image/gif"
    elif data.startswith(b"%PDF-"):
        detected = "application/pdf"
    elif data.startswith(b"\x1aE\xdf\xa3"):
        detected = "video/webm"
    elif len(data) >= 12 and data[4:8] == b"ftyp":
        detected = "video/mp4"
    elif data.startswith(b"OggS"):
        detected = "audio/ogg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        detected = "audio/wav"
    elif data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        detected = "audio/mpeg"
    elif data.startswith(b"PK\x03\x04"):
        detected = "application/zip"
    else:
        try:
            sample = data.decode("utf-8")
            if sample and sum(character.isprintable() or character in "\r\n\t" for character in sample) / len(sample) > 0.92:
                detected = "text/plain"
        except (UnicodeDecodeError, ZeroDivisionError):
            pass

    allowed_exact = {
        "application/octet-stream",
        "application/pdf",
        "application/zip",
        "application/json",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    declared_allowed = (
        clean_declared in allowed_exact
        or clean_declared.startswith(("video/", "audio/", "image/", "text/"))
    )
    if detected == "application/zip" and clean_declared in allowed_exact:
        return clean_declared
    if detected == "application/octet-stream" and declared_allowed:
        return clean_declared
    if detected in allowed_exact or detected.startswith(("video/", "audio/", "image/", "text/")):
        return detected
    raise AtlasMediaError("atlas_media_type_rejected", "Этот тип файла пока не поддерживается Atlas.")


def atlas_media_kind_for_mime(mime_type: str) -> str:
    clean = str(mime_type or "").lower()
    for kind in ("video", "audio", "image"):
        if clean.startswith(f"{kind}/"):
            return kind
    return "file"


def atlas_media_scan(path: Path, command: str) -> str:
    clean_command = str(command or "").strip()
    if not clean_command:
        return "not_configured"
    arguments = shlex.split(clean_command)
    if not arguments:
        return "not_configured"
    rendered = [part.replace("{path}", str(path)) for part in arguments]
    if all("{path}" not in part for part in arguments):
        rendered.append(str(path))
    try:
        completed = subprocess.run(
            rendered,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AtlasMediaError(
            "atlas_media_scan_unavailable",
            "Проверка безопасности временно недоступна.",
            retryable=True,
        ) from exc
    if completed.returncode == 0:
        return "clean"
    if completed.returncode == 1:
        raise AtlasMediaError(
            "atlas_media_scan_rejected",
            "Файл не прошёл проверку безопасности.",
        )
    raise AtlasMediaError(
        "atlas_media_scan_unavailable",
        "Проверка безопасности завершилась с ошибкой.",
        retryable=True,
    )


__all__ = [
    "AtlasLocalBlobStore",
    "AtlasMediaConfig",
    "AtlasMediaError",
    "atlas_media_detect_type",
    "atlas_media_kind_for_mime",
    "atlas_media_scan",
]
