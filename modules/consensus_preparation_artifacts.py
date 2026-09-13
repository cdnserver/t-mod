"""Personal, editable PDF preparation sheets for consensus bills.

The document deliberately remains private and ephemeral: it is generated into
memory for the requesting senator and is never written to the shared consensus
artifact directory.  The PDF has a visual record of the saved preparation and
fillable fields for working offline.  Offline edits stay in that file and are
not sent back to T-Mod automatically.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    TextStringObject,
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)
_BOLD_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)

_DARK = colors.HexColor("#070A09")
_SURFACE = colors.HexColor("#101713")
_SURFACE_RAISED = colors.HexColor("#142019")
_INK = colors.HexColor("#EEF2EC")
_MUTED = colors.HexColor("#8D9B90")
_GREEN = colors.HexColor("#73B483")
_HAIRLINE = colors.HexColor("#29372E")
_GOLD = colors.HexColor("#D5B978")
_RED = colors.HexColor("#D98A7D")
_PAGE_WIDTH, _PAGE_HEIGHT = A4
_LEFT = 20 * mm
_RIGHT = _PAGE_WIDTH - 20 * mm
_BOTTOM = 18 * mm
_TOP = _PAGE_HEIGHT - 18 * mm

# This has to stay under 256 distinct glyphs because ReportLab serializes the
# embedded TrueType subset with a one-byte encoding.  It is intentionally drawn
# off-page before any visible text so users can enter Russian text in AcroForm
# fields later without requiring an external font installation.
_INPUT_GLYPH_SEED = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 "
    "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    "№«»—–…•✓☐"
)

_VOTE_LABELS = {
    "yes": "ПОДДЕРЖАТЬ",
    "no": "НЕ ПОДДЕРЖИВАТЬ",
    "abstain": "ВОЗДЕРЖАТЬСЯ",
}
_CATEGORY_LABELS = {
    "ordinary": "Обычное решение",
    "significant": "Значимое решение",
    "supreme": "Верховное решение",
}


def _first_existing(candidates: tuple[str, ...]) -> str:
    return next((item for item in candidates if Path(item).is_file()), candidates[0])


def _register_fonts() -> tuple[str, str]:
    regular_name = "TModPreparationSans"
    bold_name = "TModPreparationSansBold"
    registered = set(pdfmetrics.getRegisteredFontNames())
    if regular_name not in registered:
        pdfmetrics.registerFont(TTFont(regular_name, _first_existing(_FONT_CANDIDATES)))
    if bold_name not in registered:
        pdfmetrics.registerFont(TTFont(bold_name, _first_existing(_BOLD_FONT_CANDIDATES)))
    return regular_name, bold_name


def _clean(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _format_timestamp(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return "не сохранено"
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%d.%m.%Y · %H:%M UTC")


def _wrap_lines(
    pdf: canvas.Canvas,
    value: Any,
    *,
    font_name: str,
    font_size: float,
    width: float,
) -> list[str]:
    """Wrap text while preserving explicit paragraphs and long URLs."""

    text = _clean(value)
    if not text:
        return []
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if not current or pdf.stringWidth(candidate, font_name, font_size) <= width:
                current = candidate
                continue
            lines.append(current)
            current = ""
            remaining = word
            while pdf.stringWidth(remaining, font_name, font_size) > width:
                cut = max(1, len(remaining) - 1)
                while cut > 1 and pdf.stringWidth(
                    remaining[:cut], font_name, font_size
                ) > width:
                    cut -= 1
                lines.append(remaining[:cut])
                remaining = remaining[cut:]
            current = remaining
        if current:
            lines.append(current)
    return lines


def _draw_lines(
    pdf: canvas.Canvas,
    lines: list[str],
    *,
    x: float,
    y: float,
    font_name: str,
    font_size: float,
    leading: float,
    color: colors.Color,
    max_lines: int | None = None,
) -> float:
    shown = lines if max_lines is None else lines[:max_lines]
    pdf.setFont(font_name, font_size)
    pdf.setFillColor(color)
    for line in shown:
        pdf.drawString(x, y, line)
        y -= leading
    if max_lines is not None and len(lines) > max_lines:
        pdf.drawRightString(_RIGHT, y + leading, "…")
        y -= leading
    return y


def _draw_page_frame(
    pdf: canvas.Canvas,
    *,
    regular: str,
    bold: str,
    page_number: int,
    page_label: str,
) -> None:
    pdf.saveState()
    pdf.setFillColor(_DARK)
    pdf.rect(0, 0, _PAGE_WIDTH, _PAGE_HEIGHT, fill=1, stroke=0)
    pdf.setStrokeColor(colors.HexColor("#203126"))
    pdf.setLineWidth(0.45)
    for radius in (30 * mm, 47 * mm, 65 * mm):
        pdf.circle(_PAGE_WIDTH + 4 * mm, _PAGE_HEIGHT + 1 * mm, radius, fill=0, stroke=1)
    pdf.setStrokeColor(_GREEN)
    pdf.setLineWidth(1.05)
    pdf.line(12 * mm, _BOTTOM, 12 * mm, _TOP)
    pdf.setStrokeColor(_HAIRLINE)
    pdf.setLineWidth(0.35)
    pdf.line(_LEFT, _TOP + 4 * mm, _RIGHT, _TOP + 4 * mm)
    pdf.setFont(bold, 6.7)
    pdf.setFillColor(_GREEN)
    pdf.drawString(_LEFT, _TOP + 7.7 * mm, "T-MOD  /  CONSENSUS")
    pdf.setFont(regular, 6.5)
    pdf.setFillColor(_MUTED)
    pdf.drawRightString(_RIGHT, _TOP + 7.7 * mm, page_label)
    pdf.drawString(_LEFT, 9.5 * mm, "ЛИЧНЫЙ ЛИСТ ПОДГОТОВКИ · НЕ ЯВЛЯЕТСЯ ГОЛОСОМ")
    pdf.drawRightString(_RIGHT, 9.5 * mm, f"{page_number:02d}")
    pdf.restoreState()


def _draw_eyebrow(
    pdf: canvas.Canvas,
    value: str,
    *,
    x: float,
    y: float,
    font_name: str,
    color: colors.Color = _GREEN,
) -> None:
    pdf.setFillColor(color)
    pdf.setFont(font_name, 6.8)
    pdf.drawString(x, y, value.upper())


def _draw_notice(
    pdf: canvas.Canvas,
    *,
    regular: str,
    bold: str,
    y: float,
) -> float:
    height = 28 * mm
    pdf.setFillColor(colors.HexColor("#16231A"))
    pdf.roundRect(_LEFT, y - height, _RIGHT - _LEFT, height, 4 * mm, fill=1, stroke=0)
    pdf.setStrokeColor(colors.HexColor("#365844"))
    pdf.setLineWidth(0.55)
    pdf.roundRect(_LEFT, y - height, _RIGHT - _LEFT, height, 4 * mm, fill=0, stroke=1)
    pdf.setFillColor(_GREEN)
    pdf.setFont(bold, 7)
    pdf.drawString(_LEFT + 5 * mm, y - 8 * mm, "ЛИЧНАЯ ПОДГОТОВКА")
    copy = (
        "Предварительная позиция не передаётся ведущему, не попадает в бюллетень "
        "и не меняет официальный результат."
    )
    lines = _wrap_lines(
        pdf,
        copy,
        font_name=regular,
        font_size=7.7,
        width=_RIGHT - _LEFT - 10 * mm,
    )
    _draw_lines(
        pdf,
        lines,
        x=_LEFT + 5 * mm,
        y=y - 14 * mm,
        font_name=regular,
        font_size=7.7,
        leading=10,
        color=colors.HexColor("#C9D5CA"),
        max_lines=2,
    )
    return y - height - 7 * mm


def _draw_vote_card(
    pdf: canvas.Canvas,
    *,
    regular: str,
    bold: str,
    vote: str | None,
    reason: str,
    y: float,
) -> float:
    height = 45 * mm
    accent = _GREEN if vote == "yes" else (_RED if vote == "no" else _GOLD)
    pdf.setFillColor(_SURFACE)
    pdf.roundRect(_LEFT, y - height, _RIGHT - _LEFT, height, 4 * mm, fill=1, stroke=0)
    pdf.setStrokeColor(accent)
    pdf.setLineWidth(0.65)
    pdf.roundRect(_LEFT, y - height, _RIGHT - _LEFT, height, 4 * mm, fill=0, stroke=1)
    _draw_eyebrow(
        pdf,
        "Предварительная позиция",
        x=_LEFT + 5 * mm,
        y=y - 8 * mm,
        font_name=bold,
        color=accent,
    )
    pdf.setFillColor(_INK)
    pdf.setFont(bold, 14)
    pdf.drawString(
        _LEFT + 5 * mm,
        y - 17 * mm,
        _VOTE_LABELS.get(vote or "", "ПОЗИЦИЯ ФОРМИРУЕТСЯ"),
    )
    reason_lines = _wrap_lines(
        pdf,
        reason or "Добавьте основания позиции в рабочем поле листа.",
        font_name=regular,
        font_size=7.8,
        width=_RIGHT - _LEFT - 10 * mm,
    )
    _draw_lines(
        pdf,
        reason_lines,
        x=_LEFT + 5 * mm,
        y=y - 26 * mm,
        font_name=regular,
        font_size=7.8,
        leading=10.2,
        color=_MUTED,
        max_lines=3,
    )
    return y - height - 7 * mm


def _draw_cover_page(
    pdf: canvas.Canvas,
    *,
    bill: dict[str, Any],
    sheet: dict[str, Any],
    owner_display: str,
    regular: str,
    bold: str,
) -> None:
    _draw_page_frame(
        pdf,
        regular=regular,
        bold=bold,
        page_number=1,
        page_label="PRIVATE PREPARATION",
    )
    y = _TOP - 11 * mm
    _draw_eyebrow(pdf, "СВЕТЛЫЙ КРУГ · ПОДГОТОВКА", x=_LEFT, y=y, font_name=bold)
    y -= 13 * mm
    pdf.setFillColor(_INK)
    pdf.setFont(bold, 31)
    pdf.drawString(_LEFT, y, "Лист подготовки")
    pdf.setFillColor(_GREEN)
    pdf.setFont(bold, 12)
    pdf.drawRightString(
        _RIGHT,
        y + 1 * mm,
        f"№ {int(bill.get('bill_number') or 0):03d}",
    )
    y -= 13 * mm
    title_lines = _wrap_lines(
        pdf,
        bill.get("title") or "Без названия",
        font_name=bold,
        font_size=18,
        width=_RIGHT - _LEFT,
    )
    y = _draw_lines(
        pdf,
        title_lines,
        x=_LEFT,
        y=y,
        font_name=bold,
        font_size=18,
        leading=21.5,
        color=_INK,
        max_lines=3,
    )
    y -= 3 * mm
    pdf.setStrokeColor(_HAIRLINE)
    pdf.setLineWidth(0.4)
    pdf.line(_LEFT, y, _RIGHT, y)
    y -= 8 * mm
    author_payload = bill.get("author") if isinstance(bill.get("author"), dict) else {}
    author_name = (
        _clean(author_payload.get("name"))
        or _clean(bill.get("author_display"))
        or "не указан"
    )
    metadata = (
        ("АВТОР", author_name),
        ("ТИП РЕШЕНИЯ", _CATEGORY_LABELS.get(_clean(bill.get("decision_category")), "Обычное решение")),
        ("ВЛАДЕЛЕЦ ЛИСТА", _clean(owner_display) or "Участник Товарищества"),
    )
    cell_width = (_RIGHT - _LEFT) / len(metadata)
    for index, (label, value) in enumerate(metadata):
        x = _LEFT + index * cell_width
        _draw_eyebrow(pdf, label, x=x, y=y, font_name=bold, color=_MUTED)
        value_lines = _wrap_lines(
            pdf,
            value,
            font_name=regular,
            font_size=8.2,
            width=cell_width - 4 * mm,
        )
        _draw_lines(
            pdf,
            value_lines,
            x=x,
            y=y - 5 * mm,
            font_name=regular,
            font_size=8.2,
            leading=10,
            color=_INK,
            max_lines=2,
        )
    y -= 26 * mm
    y = _draw_notice(pdf, regular=regular, bold=bold, y=y)
    y = _draw_vote_card(
        pdf,
        regular=regular,
        bold=bold,
        vote=_clean(sheet.get("preliminary_vote")) or None,
        reason=_clean(sheet.get("preliminary_vote_reason")),
        y=y,
    )
    _draw_eyebrow(pdf, "СУТЬ ЗАКОНОПРОЕКТА", x=_LEFT, y=y, font_name=bold)
    summary_lines = _wrap_lines(
        pdf,
        bill.get("summary") or "Текст законопроекта не сохранён.",
        font_name=regular,
        font_size=8.4,
        width=_RIGHT - _LEFT,
    )
    _draw_lines(
        pdf,
        summary_lines,
        x=_LEFT,
        y=y - 6 * mm,
        font_name=regular,
        font_size=8.4,
        leading=11.5,
        color=colors.HexColor("#C8D2C9"),
        max_lines=8,
    )


def _field_colors() -> dict[str, colors.Color]:
    return {
        "fillColor": colors.HexColor("#0E1511"),
        "borderColor": colors.HexColor("#3A5844"),
        "textColor": colors.HexColor("#EAF1EA"),
    }


def _draw_working_page(
    pdf: canvas.Canvas,
    *,
    sheet: dict[str, Any],
    regular: str,
    bold: str,
) -> None:
    _draw_page_frame(
        pdf,
        regular=regular,
        bold=bold,
        page_number=2,
        page_label="РЕДАКТИРУЕМЫЙ РАБОЧИЙ ЛИСТ",
    )
    y = _TOP - 11 * mm
    _draw_eyebrow(pdf, "РАБОЧЕЕ ПОЛЕ", x=_LEFT, y=y, font_name=bold)
    y -= 12 * mm
    pdf.setFillColor(_INK)
    pdf.setFont(bold, 22)
    pdf.drawString(_LEFT, y, "Вопросы, аргументы, решение")
    y -= 8 * mm
    info = (
        "Поля ниже можно редактировать в PDF-совместимом просмотрщике. Изменения "
        "в этом файле остаются у вас и не синхронизируются с T-Mod автоматически."
    )
    _draw_lines(
        pdf,
        _wrap_lines(pdf, info, font_name=regular, font_size=7.7, width=_RIGHT - _LEFT),
        x=_LEFT,
        y=y,
        font_name=regular,
        font_size=7.7,
        leading=9.7,
        color=_MUTED,
        max_lines=2,
    )

    field_colors = _field_colors()
    vote = _clean(sheet.get("preliminary_vote"))
    _draw_eyebrow(pdf, "ПРЕДВАРИТЕЛЬНАЯ ПОЗИЦИЯ", x=_LEFT, y=663, font_name=bold)
    options = (
        ("yes", "Поддержать", _GREEN, _LEFT),
        ("no", "Не поддерживать", _RED, _LEFT + 51 * mm),
        ("abstain", "Воздержаться", _GOLD, _LEFT + 112 * mm),
    )
    for value, label, accent, x in options:
        pdf.acroForm.radio(
            name="prep_vote",
            value=value,
            selected=vote == value,
            buttonStyle="circle",
            fillColor=_SURFACE,
            borderColor=accent,
            textColor=accent,
            borderWidth=1,
            size=4.6 * mm,
            x=x,
            y=646,
            tooltip="Preliminary vote",
            fieldFlags="radio",
        )
        pdf.setFillColor(_INK)
        pdf.setFont(regular, 7.8)
        pdf.drawString(x + 6.7 * mm, 649.5, label)

    _draw_eyebrow(pdf, "ПРОВЕРКА ПЕРЕД ЗАСЕДАНИЕМ", x=_LEFT, y=621, font_name=bold)
    flags = sheet.get("review_flags") if isinstance(sheet.get("review_flags"), dict) else {}
    checkboxes = (
        ("prep_read_text", "Текст прочитан", bool(flags.get("read_text")), _LEFT),
        ("prep_verify_sources", "Источники проверены", bool(flags.get("verify_sources")), _LEFT + 57 * mm),
        ("prep_need_discussion", "Нужно обсуждение", bool(flags.get("need_discussion")), _LEFT + 123 * mm),
    )
    for name, label, checked, x in checkboxes:
        pdf.acroForm.checkbox(
            name=name,
            checked=checked,
            buttonStyle="check",
            fillColor=_SURFACE,
            borderColor=_GREEN,
            textColor=_GREEN,
            borderWidth=1,
            size=4.2 * mm,
            x=x,
            y=604,
            tooltip="Preparation review",
            fieldFlags="",
        )
        pdf.setFillColor(_MUTED)
        pdf.setFont(regular, 7.2)
        pdf.drawString(x + 6.2 * mm, 607, label)

    _draw_eyebrow(pdf, "ВОПРОСЫ К АВТОРУ / ВЕДУЩЕМУ", x=_LEFT, y=577, font_name=bold)
    pdf.acroForm.textfield(
        name="prep_questions",
        value="",
        x=_LEFT,
        y=418,
        width=_RIGHT - _LEFT,
        height=151,
        tooltip="Questions",
        fontName="Helvetica",
        fontSize=9.5,
        maxlen=12_000,
        fieldFlags="multiline",
        **field_colors,
    )
    _draw_eyebrow(pdf, "ОСНОВАНИЕ ПРЕДВАРИТЕЛЬНОЙ ПОЗИЦИИ", x=_LEFT, y=391, font_name=bold)
    pdf.acroForm.textfield(
        name="prep_vote_reason",
        value="",
        x=_LEFT,
        y=326,
        width=_RIGHT - _LEFT,
        height=54,
        tooltip="Preliminary vote reason",
        fontName="Helvetica",
        fontSize=9.5,
        maxlen=2_400,
        fieldFlags="multiline",
        **field_colors,
    )
    _draw_eyebrow(pdf, "АРГУМЕНТЫ, РИСКИ И ЛИЧНЫЕ ПОМЕТКИ", x=_LEFT, y=299, font_name=bold)
    pdf.acroForm.textfield(
        name="prep_notes",
        value="",
        x=_LEFT,
        y=72,
        width=_RIGHT - _LEFT,
        height=216,
        tooltip="Preparation notes",
        fontName="Helvetica",
        fontSize=9.5,
        maxlen=16_000,
        fieldFlags="multiline",
        **field_colors,
    )


def _draw_text_section_pages(
    pdf: canvas.Canvas,
    *,
    heading: str,
    body: str,
    first_page_number: int,
    page_label: str,
    regular: str,
    bold: str,
) -> int:
    """Draw a paginated immutable bill source section and return next number."""

    lines = _wrap_lines(
        pdf,
        body or "Материал не приложен.",
        font_name=regular,
        font_size=8.7,
        width=_RIGHT - _LEFT,
    )
    page_number = first_page_number
    index = 0
    max_lines = 51
    while index < len(lines) or (not lines and index == 0):
        _draw_page_frame(
            pdf,
            regular=regular,
            bold=bold,
            page_number=page_number,
            page_label=page_label,
        )
        y = _TOP - 11 * mm
        _draw_eyebrow(pdf, page_label, x=_LEFT, y=y, font_name=bold)
        y -= 12 * mm
        pdf.setFillColor(_INK)
        pdf.setFont(bold, 21)
        pdf.drawString(_LEFT, y, heading)
        y -= 12 * mm
        chunk = lines[index:index + max_lines]
        _draw_lines(
            pdf,
            chunk,
            x=_LEFT,
            y=y,
            font_name=regular,
            font_size=8.7,
            leading=12.1,
            color=colors.HexColor("#D0D9D0"),
        )
        index += len(chunk)
        if not lines:
            index = 1
        pdf.showPage()
        page_number += 1
    return page_number


def _embedded_ttf(page: Any) -> tuple[Any, Any]:
    resources = page["/Resources"].get_object()
    fonts = resources["/Font"].get_object()
    for reference in fonts.values():
        font = reference.get_object()
        if (
            str(font.get("/Subtype") or "") == "/TrueType"
            and font.get("/ToUnicode")
            and font.get("/FontDescriptor")
        ):
            return reference, font
    raise ValueError("Cyrillic TTF was not embedded in the preparation template")


def _form_font_codec(font: Any) -> tuple[Callable[[str], bytes], Callable[[str, float], float]]:
    raw = font["/ToUnicode"].get_object().get_data().decode("latin-1")
    char_to_code = {
        chr(int(destination, 16)): int(source, 16)
        for source, destination in re.findall(
            r"<([0-9A-Fa-f]{2})>\s+<([0-9A-Fa-f]{4,})>",
            raw,
        )
    }
    first_char = int(font["/FirstChar"])
    widths = [float(item) for item in font["/Widths"]]
    fallback = char_to_code.get("?", char_to_code.get(" ", first_char))

    def code_for(character: str) -> int:
        candidate = char_to_code.get(character, fallback)
        if candidate < first_char or candidate >= first_char + len(widths):
            return fallback
        return candidate

    def encode(value: str) -> bytes:
        return bytes(code_for(character) for character in value)

    def width_of(value: str, size: float) -> float:
        return sum(widths[code_for(character) - first_char] for character in value) * size / 1000

    return encode, width_of


def _wrap_form_lines(
    value: str,
    *,
    max_width: float,
    width_of: Callable[[str, float], float],
    size: float,
) -> list[str]:
    lines: list[str] = []
    for paragraph in _clean(value).split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if not current or width_of(candidate, size) <= max_width:
                current = candidate
                continue
            lines.append(current)
            current = word
            while width_of(current, size) > max_width and len(current) > 1:
                cut = len(current) - 1
                while cut > 1 and width_of(current[:cut], size) > max_width:
                    cut -= 1
                lines.append(current[:cut])
                current = current[cut:]
        if current:
            lines.append(current)
    return lines


def _text_appearance(
    writer: PdfWriter,
    font_reference: Any,
    encode: Callable[[str], bytes],
    width_of: Callable[[str, float], float],
    value: str,
    *,
    box_width: float,
    box_height: float,
    size: float = 9.5,
) -> Any:
    lines = _wrap_form_lines(
        value,
        max_width=max(10, box_width - 10),
        width_of=width_of,
        size=size,
    )
    leading = round(size * 1.35, 2)
    lines = lines[:max(1, int((box_height - 10) // leading))]
    commands = [
        "q",
        "0.055 0.082 0.067 rg",
        f"0 0 {box_width:.2f} {box_height:.2f} re f",
        "0.23 0.35 0.27 RG",
        "0.65 w",
        f"0.33 0.33 {box_width - 0.66:.2f} {box_height - 0.66:.2f} re S",
        "0.92 0.95 0.93 rg",
        "BT",
        f"/TModInput {size:.2f} Tf",
        f"1 0 0 1 5 {box_height - size - 5:.2f} Tm",
    ]
    for index, line in enumerate(lines):
        if index:
            commands.append(f"0 -{leading:.2f} Td")
        commands.append(f"<{encode(line).hex().upper()}> Tj")
    commands.extend(("ET", "Q"))
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode("ascii"))
    stream[NameObject("/Type")] = NameObject("/XObject")
    stream[NameObject("/Subtype")] = NameObject("/Form")
    stream[NameObject("/BBox")] = ArrayObject(
        [
            FloatObject(0),
            FloatObject(0),
            FloatObject(box_width),
            FloatObject(box_height),
        ]
    )
    stream[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/TModInput"): font_reference}
            )
        }
    )
    return writer._add_object(stream)


def _attach_unicode_input_appearances(
    source: bytes,
    values: dict[str, str],
) -> bytes:
    """Give ReportLab's empty widgets a Cyrillic-capable embedded font.

    ReportLab only permits the standard PDF 14 fonts for AcroForm fields.  It
    does however embed our TTF for the printed design.  Reusing that reference
    in the AcroForm resource dictionary produces portable Cyrillic fields
    without relying on a PDF viewer having Arial/Noto installed.
    """

    reader = PdfReader(BytesIO(source))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    font_reference, font = _embedded_ttf(writer.pages[0])
    encode, width_of = _form_font_codec(font)
    form = writer._root_object["/AcroForm"].get_object()
    resources = form.get("/DR")
    if resources is None:
        resources = DictionaryObject()
        form[NameObject("/DR")] = resources
    else:
        resources = resources.get_object()
    fonts = resources.get("/Font")
    if fonts is None:
        fonts = DictionaryObject()
        resources[NameObject("/Font")] = fonts
    else:
        fonts = fonts.get_object()
    fonts[NameObject("/TModInput")] = font_reference
    form[NameObject("/DA")] = TextStringObject("/TModInput 9.5 Tf 0.92 0.95 0.93 rg")
    form[NameObject("/NeedAppearances")] = BooleanObject(False)

    fields = form.get("/Fields") or []
    for field_reference in fields:
        field = field_reference.get_object()
        if str(field.get("/FT") or "") != "/Tx":
            continue
        name = str(field.get("/T") or "")
        if name not in values:
            continue
        value = str(values[name] or "")
        rect = field.get("/Rect") or []
        if len(rect) != 4:
            continue
        box_width = float(rect[2]) - float(rect[0])
        box_height = float(rect[3]) - float(rect[1])
        field[NameObject("/DA")] = TextStringObject(
            "/TModInput 9.5 Tf 0.92 0.95 0.93 rg"
        )
        field[NameObject("/V")] = TextStringObject(value)
        field[NameObject("/DV")] = TextStringObject(value)
        field[NameObject("/AP")] = DictionaryObject(
            {
                NameObject("/N"): _text_appearance(
                    writer,
                    font_reference,
                    encode,
                    width_of,
                    value,
                    box_width=box_width,
                    box_height=box_height,
                )
            }
        )
    destination = BytesIO()
    writer.write(destination)
    return destination.getvalue()


def _questions_value(sheet: dict[str, Any]) -> str:
    questions = sheet.get("questions") if isinstance(sheet.get("questions"), list) else []
    result: list[str] = []
    for index, item in enumerate(questions, start=1):
        if not isinstance(item, dict):
            continue
        marker = "✓" if bool(item.get("resolved")) else "•"
        item_text = _clean(item.get("text"))
        if item_text:
            result.append(f"{marker} {index}. {item_text}")
    return "\n".join(result)


def generate_preparation_pdf(
    bill: dict[str, Any],
    sheet: dict[str, Any],
    owner_display: str,
) -> bytes:
    """Return an in-memory, private and editable consensus preparation PDF."""

    regular, bold = _register_fonts()
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    pdf.setTitle(f"Лист подготовки · законопроект №{int(bill.get('bill_number') or 0)}")
    pdf.setAuthor("T-Mod · Товарищество")
    pdf.setSubject("Личный лист подготовки к пленарному консенсусу")
    # First regular-font use: makes every allowed input glyph part of the same
    # embedded TrueType subset later wired into the AcroForm fields.
    pdf.setFont(regular, 1)
    pdf.drawString(-2000, -2000, _INPUT_GLYPH_SEED)

    _draw_cover_page(
        pdf,
        bill=bill,
        sheet=sheet,
        owner_display=owner_display,
        regular=regular,
        bold=bold,
    )
    pdf.showPage()
    _draw_working_page(pdf, sheet=sheet, regular=regular, bold=bold)
    pdf.showPage()
    next_page = _draw_text_section_pages(
        pdf,
        heading="Полный текст законопроекта",
        body=_clean(bill.get("summary")) or "Текст законопроекта не сохранён.",
        first_page_number=3,
        page_label="ПЕРВОИСТОЧНИК · ТЕКСТ",
        regular=regular,
        bold=bold,
    )
    _draw_text_section_pages(
        pdf,
        heading="Материалы и ссылки",
        body=_clean(bill.get("materials")) or "Материалы не приложены.",
        first_page_number=next_page,
        page_label="ПЕРВОИСТОЧНИК · МАТЕРИАЛЫ",
        regular=regular,
        bold=bold,
    )
    pdf.save()

    field_values = {
        "prep_questions": _questions_value(sheet),
        "prep_vote_reason": _clean(sheet.get("preliminary_vote_reason")),
        "prep_notes": _clean(sheet.get("notes")),
    }
    return _attach_unicode_input_appearances(buffer.getvalue(), field_values)


__all__ = ["generate_preparation_pdf"]
