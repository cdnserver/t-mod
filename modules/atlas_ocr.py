"""Bounded local OCR for reviewable Atlas forum attachments.

This module is intentionally *not* a knowledge indexer.  OCR is a fallible
transcription of an original file, so callers must keep the original forum
URL/checksum and require a human review before any text can influence Atlas'
legal answers.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_IMAGE_MIME_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
    }
)
_PDF_MIME_TYPE = "application/pdf"


class AtlasOcrError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = str(code)
        self.retryable = bool(retryable)


class AtlasOcrUnavailable(AtlasOcrError):
    """The container lacks a local OCR capability; it is not a bad source."""


@dataclass(frozen=True, slots=True)
class AtlasOcrConfig:
    max_input_bytes: int
    max_output_chars: int
    timeout_seconds: int
    max_pdf_pages: int
    languages: str

    @classmethod
    def from_env(cls) -> "AtlasOcrConfig":
        def bounded(name: str, default: int, low: int, high: int) -> int:
            try:
                value = int(str(os.getenv(name, str(default))).strip())
            except (TypeError, ValueError):
                value = default
            return max(low, min(high, value))

        languages = str(os.getenv("ATLAS_OCR_LANGUAGES", "rus+eng")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_+.-]{1,80}", languages):
            languages = "rus+eng"
        return cls(
            max_input_bytes=bounded("ATLAS_OCR_MAX_INPUT_MIB", 16, 1, 64) * 1024 * 1024,
            max_output_chars=bounded("ATLAS_OCR_MAX_OUTPUT_CHARS", 120_000, 1_000, 500_000),
            timeout_seconds=bounded("ATLAS_OCR_TIMEOUT_SECONDS", 75, 10, 240),
            max_pdf_pages=bounded("ATLAS_OCR_MAX_PDF_PAGES", 6, 1, 20),
            languages=languages,
        )


@dataclass(frozen=True, slots=True)
class AtlasOcrResult:
    text: str
    engine: str
    pages: int
    mime_type: str


def _clean_text(value: str, *, maximum: int) -> str:
    text = _CONTROL_RE.sub("", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    clean = "\n".join(lines).strip()
    if len(clean) > maximum:
        clean = clean[:maximum].rstrip() + "\n[Текст ограничен безопасным лимитом Atlas.]"
    return clean


def _run(command: Sequence[str], *, timeout: int) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise AtlasOcrUnavailable(
            "atlas_ocr_engine_unavailable",
            "Локальный модуль распознавания пока недоступен.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AtlasOcrError(
            "atlas_ocr_timeout",
            "Распознавание вложения превысило безопасное время ожидания.",
            retryable=True,
        ) from exc
    except OSError as exc:
        raise AtlasOcrError(
            "atlas_ocr_process_unavailable",
            "Не удалось запустить локальный модуль распознавания.",
            retryable=True,
        ) from exc


def _require_success(completed: subprocess.CompletedProcess[bytes], *, code: str) -> bytes:
    if completed.returncode != 0:
        raise AtlasOcrError(
            code,
            "Файл не удалось корректно распознать. Оригинал сохранён для проверки.",
        )
    return bytes(completed.stdout or b"")


def _tesseract_image(source: Path, config: AtlasOcrConfig) -> str:
    output = _require_success(
        _run(
            [
                "tesseract",
                str(source),
                "stdout",
                "-l",
                config.languages,
                "--psm",
                "6",
            ],
            timeout=config.timeout_seconds,
        ),
        code="atlas_ocr_image_failed",
    )
    return _clean_text(output.decode("utf-8", errors="replace"), maximum=config.max_output_chars)


def _ocr_pdf(source: Path, config: AtlasOcrConfig, workdir: Path) -> AtlasOcrResult:
    text_layer = _run(
        [
            "pdftotext",
            "-f",
            "1",
            "-l",
            str(config.max_pdf_pages),
            "-layout",
            "-nopgbrk",
            str(source),
            "-",
        ],
        timeout=config.timeout_seconds,
    )
    if text_layer.returncode == 0:
        clean = _clean_text(
            bytes(text_layer.stdout or b"").decode("utf-8", errors="replace"),
            maximum=config.max_output_chars,
        )
        if len(clean) >= 20:
            return AtlasOcrResult(clean, "pdftotext", 1, _PDF_MIME_TYPE)

    prefix = workdir / "page"
    rendered = _run(
        [
            "pdftoppm",
            "-f",
            "1",
            "-l",
            str(config.max_pdf_pages),
            "-r",
            "180",
            "-png",
            str(source),
            str(prefix),
        ],
        timeout=config.timeout_seconds,
    )
    _require_success(rendered, code="atlas_ocr_pdf_render_failed")
    pages = sorted(workdir.glob("page-*.png"))[: config.max_pdf_pages]
    if not pages:
        raise AtlasOcrError("atlas_ocr_pdf_empty", "PDF не содержит доступных для распознавания страниц.")
    blocks: list[str] = []
    remaining = config.max_output_chars
    for page_number, image in enumerate(pages, start=1):
        if remaining <= 0:
            break
        text = _tesseract_image(image, config)
        if text:
            blocks.append(f"[Страница {page_number}]\n{text[:remaining]}")
            remaining -= len(text)
    clean = _clean_text("\n\n".join(blocks), maximum=config.max_output_chars)
    if len(clean) < 20:
        raise AtlasOcrError("atlas_ocr_text_empty", "В скане не найден читаемый текст.")
    return AtlasOcrResult(clean, "pdftoppm+tesseract", len(pages), _PDF_MIME_TYPE)


def atlas_ocr_attachment(
    data: bytes,
    *,
    mime_type: str,
    filename: str = "attachment",
    config: AtlasOcrConfig | None = None,
) -> AtlasOcrResult:
    """Transcribe one bounded image/PDF attachment with local tools only.

    The result contains no citation authority.  The caller must retain the
    source attachment URL and only make the text searchable after a reviewer
    accepts it.
    """

    selected = config or AtlasOcrConfig.from_env()
    raw = bytes(data or b"")
    clean_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if not raw:
        raise AtlasOcrError("atlas_ocr_input_empty", "Вложение оказалось пустым.")
    if len(raw) > selected.max_input_bytes:
        raise AtlasOcrError(
            "atlas_ocr_input_too_large",
            "Вложение превышает безопасный лимит распознавания.",
        )
    if clean_mime not in _IMAGE_MIME_TYPES | {_PDF_MIME_TYPE}:
        raise AtlasOcrUnavailable(
            "atlas_ocr_type_unsupported",
            "Atlas пока распознаёт только изображения и PDF-вложения.",
        )

    suffix = ".pdf" if clean_mime == _PDF_MIME_TYPE else {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
    }.get(clean_mime, Path(str(filename or "attachment")).suffix or ".bin")
    with tempfile.TemporaryDirectory(prefix="atlas-ocr-") as temporary:
        workdir = Path(temporary)
        source = workdir / f"source{suffix}"
        source.write_bytes(raw)
        if clean_mime == _PDF_MIME_TYPE:
            return _ocr_pdf(source, selected, workdir)
        text = _tesseract_image(source, selected)
    if len(text) < 20:
        raise AtlasOcrError("atlas_ocr_text_empty", "В изображении не найден читаемый текст.")
    return AtlasOcrResult(text, "tesseract", 1, clean_mime)


__all__ = [
    "AtlasOcrConfig",
    "AtlasOcrError",
    "AtlasOcrResult",
    "AtlasOcrUnavailable",
    "atlas_ocr_attachment",
]
