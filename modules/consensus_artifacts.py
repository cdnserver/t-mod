"""Ceremonial result cards and archival PDF protocols for plenary consensus."""

from __future__ import annotations

import hashlib
import html
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from modules.consensus_core import LiveConsensusSession, LiveResult
from persistence import core as persistence_core
from persistence import tvrs_repository as storage


_ARTIFACT_DESIGN_VERSION = 4
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)
_BOLD_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)

_DARK = colors.HexColor("#070A09")
_PAPER = colors.HexColor("#070A09")
_INK = colors.HexColor("#EEF2EC")
_MUTED = colors.HexColor("#87968B")
_GREEN = colors.HexColor("#73B483")
_PALE_GREEN = colors.HexColor("#17261C")
_HAIRLINE = colors.HexColor("#29372E")


def _first_existing(candidates: tuple[str, ...]) -> str:
    return next((item for item in candidates if Path(item).is_file()), candidates[0])


def _font_path(*, bold: bool = False) -> str:
    return _first_existing(_BOLD_FONT_CANDIDATES if bold else _FONT_CANDIDATES)


def _artifact_root() -> Path:
    configured = str(os.getenv("CONSENSUS_ARTIFACTS_DIR", "")).strip()
    root = (
        Path(configured)
        if configured
        else persistence_core.DATA_DIR.parent / "consensus-artifacts"
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def session_artifact_slug(session_key: str) -> str:
    digest = hashlib.sha256(str(session_key).encode("utf-8")).hexdigest()[:18]
    return f"session-{digest}"


def session_artifact_directory(session_key: str) -> Path:
    path = _artifact_root() / session_artifact_slug(session_key)
    path.mkdir(parents=True, exist_ok=True)
    return path


def result_card_path(session_key: str, bill_number: int) -> Path:
    return session_artifact_directory(session_key) / (
        f"result-{int(bill_number):03d}-v{_ARTIFACT_DESIGN_VERSION}.png"
    )


def session_report_path(session_key: str) -> Path:
    return session_artifact_directory(session_key) / (
        f"consensus-v{_ARTIFACT_DESIGN_VERSION}.pdf"
    )


def session_cover_path(session_key: str) -> Path:
    return session_artifact_directory(session_key) / (
        f"summary-v{_ARTIFACT_DESIGN_VERSION}.png"
    )


def _fit_text(
    draw: ImageDraw.ImageDraw,
    value: str,
    *,
    font: ImageFont.FreeTypeFont,
    width: int,
    max_lines: int,
) -> list[str]:
    words = str(value or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or draw.textlength(candidate, font=font) <= width:
            current = candidate
            continue
        lines.append(current)
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and words and " ".join(lines) != " ".join(words):
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines or ["Без названия"]


def _status_copy(status: str) -> tuple[str, tuple[int, int, int]]:
    return {
        "accepted": ("ПРИНЯТ", (128, 214, 151)),
        "rejected": ("ОТКЛОНЁН", (229, 130, 119)),
        "vetoed": ("ПРАВО ВЕТО", (225, 163, 108)),
        "oral": ("УСТНОЕ РЕШЕНИЕ", (166, 184, 225)),
    }.get(str(status), ("ЗАФИКСИРОВАН", (196, 209, 199)))


def _soft_glow(
    image: Image.Image,
    *,
    center: tuple[int, int],
    radius: int,
    color: tuple[int, int, int],
) -> Image.Image:
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    x, y = center
    layer_draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        fill=(*color, 54),
    )
    layer = layer.filter(ImageFilter.GaussianBlur(radius // 2))
    return Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB")


def _image_background(accent: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", (1600, 900), (5, 8, 7))
    draw = ImageDraw.Draw(image)
    for y in range(image.height):
        ratio = y / image.height
        draw.line(
            (0, y, image.width, y),
            fill=(
                5 + round(ratio * 3),
                8 + round(ratio * 4),
                7 + round(ratio * 3),
            ),
        )
    return _soft_glow(
        image,
        center=(1330, 150),
        radius=470,
        color=tuple(max(24, channel // 2) for channel in accent),
    )


def _bill_record(result: LiveResult) -> dict[str, Any]:
    try:
        return storage.tvrs_get_bill_dict_by_id(int(result.bill_id)) or {}
    except Exception:
        return {}


def generate_result_card(
    session: LiveConsensusSession,
    result: LiveResult,
    *,
    force: bool = False,
) -> Path:
    destination = result_card_path(session.session_key, result.bill_number)
    if destination.is_file() and not force:
        return destination

    status_label, accent = _status_copy(result.status)
    image = _image_background(accent)
    draw = ImageDraw.Draw(image)
    regular = _font_path()
    bold = _font_path(bold=True)
    label_font = ImageFont.truetype(bold, 18)
    micro_font = ImageFont.truetype(bold, 14)
    body_font = ImageFont.truetype(regular, 21)
    title_font = ImageFont.truetype(bold, 57)
    metric_font = ImageFont.truetype(bold, 132)
    metric_small_font = ImageFont.truetype(bold, 42)

    # Architectural lines replace the previous rounded office card.
    draw.line((82, 80, 1518, 80), fill=(57, 69, 61), width=1)
    draw.line((82, 820, 1518, 820), fill=(57, 69, 61), width=1)
    draw.line((82, 80, 82, 820), fill=accent, width=2)
    ring_color = tuple(round(channel * 0.38) for channel in accent)
    draw.arc((1110, -250, 1720, 360), 24, 205, fill=ring_color, width=2)
    draw.arc((1200, -170, 1650, 280), 24, 205, fill=(75, 90, 80), width=1)

    draw.text((112, 112), "T-MOD  /  CONSENSUS", font=label_font, fill=(205, 214, 207))
    draw.text(
        (1518, 112),
        f"ЗАСЕДАНИЕ {int(session.plenary_number):02d}",
        font=label_font,
        fill=(130, 143, 133),
        anchor="ra",
    )
    draw.text((112, 168), f"ЗАКОНОПРОЕКТ  /  {int(result.bill_number):03d}", font=micro_font, fill=accent)
    draw.line((1218, 176, 1342, 176), fill=accent, width=2)
    draw.text((1360, 176), status_label, font=label_font, fill=accent, anchor="lm")

    lines = _fit_text(draw, result.title, font=title_font, width=1110, max_lines=3)
    title_y = 216
    for index, line in enumerate(lines):
        draw.text((112, title_y + index * 70), line, font=title_font, fill=(242, 242, 235))

    bill = _bill_record(result)
    author = str(bill.get("author_display") or "Автор не указан")
    draw.text((114, 454), f"Автор · {author}", font=body_font, fill=(137, 149, 140))

    draw.text((112, 535), "ОБЩИЙ КОНСЕНСУС", font=micro_font, fill=(132, 145, 135))
    overall_copy = f"{float(result.overall_percent):.1f}"
    draw.text((104, 560), overall_copy, font=metric_font, fill=(241, 243, 237))
    overall_width = draw.textlength(overall_copy, font=metric_font)
    draw.text((104 + overall_width + 14, 661), "%", font=metric_small_font, fill=accent, anchor="ls")

    metric_columns = (
        (550, "ТОВАРИЩЕСТВО", float(result.internal_percent)),
        (785, "ПОРОГ", float(result.required_percent or 50.0)),
        (1000, "ПРОТИВ", float(result.opposed_percent)),
    )
    for x, label, value in metric_columns:
        draw.text((x, 565), label, font=micro_font, fill=(129, 142, 132))
        draw.text((x, 602), f"{value:.1f}%", font=metric_small_font, fill=(221, 226, 218))

    draw.line((550, 690, 1490, 690), fill=(48, 59, 51), width=1)
    labels = (("first", "I"), ("second", "II"), ("third", "III"), ("consensus", "TVR"))
    for index, (key, label) in enumerate(labels):
        x = 550 + index * 235
        value = str(result.block_votes.get(key) or "inactive")
        copy = {
            "yes": "ЗА",
            "no": "ПРОТИВ",
            "abstain": "ВОЗДЕРЖАЛСЯ",
            "inactive": "НЕАКТИВЕН",
        }.get(value, value.upper())
        draw.text((x, 721), label, font=micro_font, fill=(121, 134, 124))
        draw.text((x, 754), copy, font=label_font, fill=(224, 230, 222))

    generated = datetime.now(timezone.utc)
    draw.text((112, 849), f"Ведущий · {session.leader_display}", font=body_font, fill=(118, 130, 121))
    draw.text(
        (1518, 849),
        generated.strftime("%d.%m.%Y  /  %H:%M UTC"),
        font=body_font,
        fill=(118, 130, 121),
        anchor="ra",
    )
    image.save(destination, "PNG", optimize=True)
    return destination


def generate_session_cover(
    session: LiveConsensusSession,
    *,
    force: bool = False,
) -> Path:
    destination = session_cover_path(session.session_key)
    if destination.is_file() and not force:
        return destination
    image = _image_background((118, 193, 139))
    draw = ImageDraw.Draw(image)
    bold = _font_path(bold=True)
    regular = _font_path()
    label_font = ImageFont.truetype(bold, 17)
    session_font = ImageFont.truetype(bold, 190)
    title_font = ImageFont.truetype(bold, 62)
    body_font = ImageFont.truetype(regular, 23)

    draw.arc((815, -420, 1760, 525), 35, 205, fill=(70, 106, 80), width=2)
    draw.arc((975, -290, 1630, 365), 35, 205, fill=(42, 68, 49), width=1)
    draw.line((82, 80, 1518, 80), fill=(57, 69, 61), width=1)
    draw.text((82, 112), "T-MOD  /  CONSENSUS", font=label_font, fill=(206, 215, 208))
    draw.text((1518, 112), "ИТОГОВЫЙ ПРОТОКОЛ", font=label_font, fill=(129, 143, 132), anchor="ra")
    draw.text((92, 212), f"{int(session.plenary_number):02d}", font=session_font, fill=(239, 242, 236))
    draw.text((565, 272), "Пленарный", font=title_font, fill=(236, 239, 233))
    draw.text((565, 351), "Консенсус", font=title_font, fill=(236, 239, 233))
    draw.text((570, 446), "Решения Светлого круга Товарищества", font=body_font, fill=(135, 150, 139))

    counts = (
        ("РЕШЕНИЙ", len(session.results)),
        ("ПРИНЯТО", sum(item.status == "accepted" for item in session.results)),
        ("ОТКЛОНЕНО", sum(item.status == "rejected" for item in session.results)),
        ("ВЕТО", sum(item.status == "vetoed" for item in session.results)),
    )
    draw.line((82, 602, 1518, 602), fill=(54, 66, 57), width=1)
    for index, (label, value) in enumerate(counts):
        x = 82 + index * 359
        if index:
            draw.line((x, 629, x, 742), fill=(46, 57, 49), width=1)
        draw.text((x + 24, 636), label, font=label_font, fill=(123, 137, 126))
        draw.text((x + 20, 674), str(value), font=ImageFont.truetype(bold, 55), fill=(228, 233, 226))
    draw.line((82, 770, 1518, 770), fill=(54, 66, 57), width=1)
    draw.text((82, 815), f"Ведущий · {session.leader_display}", font=body_font, fill=(122, 135, 125))
    draw.text((1518, 815), "ТОВАРИЩЕСТВО", font=label_font, fill=(103, 118, 107), anchor="ra")
    image.save(destination, "PNG", optimize=True)
    return destination


def _register_pdf_fonts() -> tuple[str, str]:
    definitions = (
        ("TModSans", _font_path()),
        ("TModSansBold", _font_path(bold=True)),
    )
    registered = set(pdfmetrics.getRegisteredFontNames())
    for name, path in definitions:
        if name not in registered:
            pdfmetrics.registerFont(TTFont(name, path))
    return tuple(item[0] for item in definitions)  # type: ignore[return-value]


def _safe(value: Any) -> str:
    return html.escape(str(value or "—")).replace("\n", "<br/>")


def _rgb_hex(value: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{channel:02X}" for channel in value)


def generate_session_report(
    session: LiveConsensusSession,
    *,
    force: bool = False,
) -> Path:
    destination = session_report_path(session.session_key)
    if destination.is_file() and not force:
        return destination

    regular, bold = _register_pdf_fonts()
    generated_at = datetime.now(timezone.utc)
    started_at = session.created_at.astimezone(timezone.utc)
    duration_minutes = max(
        1,
        round((generated_at - started_at).total_seconds() / 60),
    )
    styles = getSampleStyleSheet()

    cover_eyebrow = ParagraphStyle(
        "CoverEyebrow",
        parent=styles["BodyText"],
        fontName=bold,
        fontSize=7.5,
        leading=10,
        tracking=2.3,
        textColor=colors.HexColor("#9EB0A2"),
        spaceAfter=8 * mm,
    )
    cover_number = ParagraphStyle(
        "CoverNumber",
        parent=styles["Title"],
        fontName=bold,
        fontSize=76,
        leading=78,
        textColor=colors.HexColor("#F1F3EE"),
        spaceAfter=0,
    )
    cover_title = ParagraphStyle(
        "CoverTitle",
        parent=styles["Title"],
        fontName=bold,
        fontSize=28,
        leading=32,
        textColor=colors.HexColor("#F1F3EE"),
        spaceAfter=3 * mm,
    )
    cover_subtitle = ParagraphStyle(
        "CoverSubtitle",
        parent=styles["BodyText"],
        fontName=regular,
        fontSize=11,
        leading=15,
        textColor=colors.HexColor("#9EACA1"),
        spaceAfter=17 * mm,
    )
    cover_meta_label = ParagraphStyle(
        "CoverMetaLabel",
        parent=styles["BodyText"],
        fontName=bold,
        fontSize=6.5,
        leading=8,
        textColor=colors.HexColor("#819086"),
        spaceAfter=2 * mm,
    )
    cover_meta_value = ParagraphStyle(
        "CoverMetaValue",
        parent=styles["BodyText"],
        fontName=regular,
        fontSize=9.5,
        leading=12,
        textColor=colors.HexColor("#E5E9E3"),
    )
    cover_metric = ParagraphStyle(
        "CoverMetric",
        parent=styles["BodyText"],
        fontName=bold,
        fontSize=25,
        leading=28,
        textColor=colors.HexColor("#F1F3EE"),
        alignment=TA_CENTER,
    )
    cover_metric_label = ParagraphStyle(
        "CoverMetricLabel",
        parent=styles["BodyText"],
        fontName=bold,
        fontSize=6.5,
        leading=8,
        textColor=colors.HexColor("#839188"),
        alignment=TA_CENTER,
    )
    bill_kicker = ParagraphStyle(
        "BillKicker",
        parent=styles["BodyText"],
        fontName=bold,
        fontSize=7.2,
        leading=9,
        textColor=_GREEN,
        spaceAfter=4 * mm,
    )
    bill_title = ParagraphStyle(
        "BillTitle",
        parent=styles["Title"],
        fontName=bold,
        fontSize=21,
        leading=25,
        textColor=_INK,
        alignment=TA_LEFT,
        spaceAfter=5 * mm,
    )
    body = ParagraphStyle(
        "ProtocolBody",
        parent=styles["BodyText"],
        fontName=regular,
        fontSize=9.2,
        leading=14.2,
        textColor=colors.HexColor("#C3CEC5"),
        spaceAfter=3 * mm,
    )
    body_small = ParagraphStyle(
        "ProtocolBodySmall",
        parent=body,
        fontSize=8,
        leading=11,
        textColor=_MUTED,
    )
    section = ParagraphStyle(
        "ProtocolSection",
        parent=styles["Heading2"],
        fontName=bold,
        fontSize=7,
        leading=9,
        textColor=_GREEN,
        spaceBefore=4 * mm,
        spaceAfter=2.5 * mm,
    )
    metric_label = ParagraphStyle(
        "ProtocolMetricLabel",
        parent=body,
        fontName=bold,
        fontSize=6.2,
        leading=8,
        textColor=_MUTED,
        alignment=TA_CENTER,
    )
    metric_value = ParagraphStyle(
        "ProtocolMetricValue",
        parent=body,
        fontName=bold,
        fontSize=16,
        leading=19,
        textColor=_INK,
        alignment=TA_CENTER,
    )
    table_header = ParagraphStyle(
        "ProtocolTableHeader",
        parent=body,
        fontName=bold,
        fontSize=7,
        leading=9,
        textColor=_MUTED,
    )
    table_value = ParagraphStyle(
        "ProtocolTableValue",
        parent=body,
        fontName=regular,
        fontSize=8.3,
        leading=10.5,
        textColor=_INK,
    )
    table_vote = ParagraphStyle(
        "ProtocolTableVote",
        parent=table_value,
        fontName=bold,
        alignment=TA_RIGHT,
    )

    document = SimpleDocTemplate(
        str(destination),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=21 * mm,
        bottomMargin=18 * mm,
        title=f"Протокол пленарного консенсуса №{session.plenary_number}",
        author="T-Mod · Товарищество",
        subject="Итоговый протокол решений Светлого круга",
    )

    accepted = sum(item.status == "accepted" for item in session.results)
    rejected = sum(item.status == "rejected" for item in session.results)
    vetoed = sum(item.status == "vetoed" for item in session.results)
    story: list[Any] = [
        Paragraph("T-MOD  /  CONSENSUS  /  ТОВАРИЩЕСТВО", cover_eyebrow),
        Spacer(1, 13 * mm),
        Paragraph(f"{int(session.plenary_number):02d}", cover_number),
        Paragraph("Пленарный Консенсус", cover_title),
        Paragraph("Итоговый протокол решений Светлого круга", cover_subtitle),
    ]

    meta_data = [
        [
            Paragraph("ВЕДУЩИЙ", cover_meta_label),
            Paragraph("НАЧАЛО", cover_meta_label),
            Paragraph("ПРОДОЛЖИТЕЛЬНОСТЬ", cover_meta_label),
        ],
        [
            Paragraph(_safe(session.leader_display), cover_meta_value),
            Paragraph(started_at.strftime("%d.%m.%Y  /  %H:%M UTC"), cover_meta_value),
            Paragraph(f"{duration_minutes} мин.", cover_meta_value),
        ],
    ]
    meta = Table(meta_data, colWidths=[58 * mm, 58 * mm, 50 * mm])
    meta.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.HexColor("#354139")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, 0), 4 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1 * mm),
            ]
        )
    )
    story.extend([meta, Spacer(1, 11 * mm)])

    summary_values = (
        ("РЕШЕНИЙ", len(session.results)),
        ("ПРИНЯТО", accepted),
        ("ОТКЛОНЕНО", rejected),
        ("ВЕТО", vetoed),
    )
    summary = Table(
        [
            [Paragraph(str(value), cover_metric) for _, value in summary_values],
            [Paragraph(label, cover_metric_label) for label, _ in summary_values],
        ],
        colWidths=[41.5 * mm] * 4,
        rowHeights=[12 * mm, 8 * mm],
    )
    summary.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.HexColor("#354139")),
                ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#354139")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
            ]
        )
    )
    story.extend([summary, PageBreak()])

    participant_names = {
        int(item.user_id): str(item.display_name)
        for item in session.participants.values()
    }
    for index, result in enumerate(session.results):
        bill = _bill_record(result)
        status_label, accent_rgb = _status_copy(result.status)
        accent = colors.HexColor(_rgb_hex(accent_rgb))
        category = {
            "ordinary": "Обычное решение",
            "significant": "Значимое решение",
            "supreme": "Верховное решение",
        }.get(str(result.decision_category), str(result.decision_category or "Решение"))

        story.extend(
            [
                Paragraph(
                    f"ЗАКОНОПРОЕКТ  /  {int(result.bill_number):03d}",
                    bill_kicker,
                ),
                Paragraph(_safe(result.title), bill_title),
            ]
        )
        decision_line = Table(
            [
                [
                    Paragraph(
                        f"Автор · <b>{_safe(bill.get('author_display') or 'не указан')}</b><br/>"
                        f"{_safe(category)}",
                        body_small,
                    ),
                    Paragraph(status_label, ParagraphStyle(
                        f"Status{index}", parent=body_small, fontName=bold,
                        fontSize=8, leading=10, textColor=accent, alignment=TA_RIGHT,
                    )),
                ]
            ],
            colWidths=[125 * mm, 41 * mm],
        )
        decision_line.setStyle(
            TableStyle(
                [
                    ("LINEABOVE", (0, 0), (-1, 0), 0.65, accent),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.35, _HAIRLINE),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 3.5 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5 * mm),
                ]
            )
        )
        story.extend([decision_line, Spacer(1, 5 * mm)])

        metric_data = [
            [
                Paragraph("ОБЩИЙ КОНСЕНСУС", metric_label),
                Paragraph("ТОВАРИЩЕСТВО", metric_label),
                Paragraph("ПОРОГ", metric_label),
                Paragraph("ПРОТИВ", metric_label),
            ],
            [
                Paragraph(f"{result.overall_percent:.1f}%", metric_value),
                Paragraph(f"{result.internal_percent:.1f}%", metric_value),
                Paragraph(f"{result.required_percent:.1f}%", metric_value),
                Paragraph(f"{result.opposed_percent:.1f}%", metric_value),
            ],
        ]
        metrics = Table(metric_data, colWidths=[41.5 * mm] * 4)
        metrics.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#101713")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
                    ("TOPPADDING", (0, 0), (-1, 0), 3.2 * mm),
                    ("BOTTOMPADDING", (0, -1), (-1, -1), 3.5 * mm),
                    ("LINEAFTER", (0, 0), (-2, -1), 0.35, colors.HexColor("#27352C")),
                ]
            )
        )
        story.extend([metrics, Spacer(1, 3 * mm)])

        if bill.get("summary"):
            story.extend(
                [
                    Paragraph("СУТЬ ПРЕДЛОЖЕНИЯ", section),
                    Paragraph(_safe(bill.get("summary")), body),
                ]
            )
        if bill.get("materials"):
            story.extend(
                [
                    Paragraph("МАТЕРИАЛЫ", section),
                    Paragraph(_safe(bill.get("materials")), body_small),
                ]
            )

        vote_rows: list[list[Any]] = [
            [
                Paragraph("УЧАСТНИК", table_header),
                Paragraph("ГОЛОС", ParagraphStyle(
                    f"VoteHeader{index}", parent=table_header, alignment=TA_RIGHT,
                )),
            ]
        ]
        for user_id, vote in sorted(
            result.votes.items(),
            key=lambda item: participant_names.get(
                int(item[0]), str(item[0])
            ).casefold(),
        ):
            vote_rows.append(
                [
                    Paragraph(
                        _safe(participant_names.get(int(user_id), str(user_id))),
                        table_value,
                    ),
                    Paragraph(
                        _safe({
                            "yes": "За",
                            "no": "Против",
                            "abstain": "Воздержался",
                        }.get(str(vote), str(vote))),
                        table_vote,
                    ),
                ]
            )
        votes = Table(vote_rows, colWidths=[125 * mm, 41 * mm], repeatRows=1)
        vote_style: list[tuple[Any, ...]] = [
            ("LINEABOVE", (0, 0), (-1, 0), 0.55, _GREEN),
            ("LINEBELOW", (0, 0), (-1, 0), 0.35, _HAIRLINE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 2.3 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.3 * mm),
            ("LINEBELOW", (0, 1), (-1, -1), 0.2, colors.HexColor("#26332B")),
        ]
        for row_index in range(2, len(vote_rows), 2):
            vote_style.append(
                ("BACKGROUND", (0, row_index), (-1, row_index), colors.HexColor("#0E1511"))
            )
        votes.setStyle(TableStyle(vote_style))
        story.extend(
            [
                Paragraph("ИМЕННОЙ ПРОТОКОЛ ГОЛОСОВ", section),
                votes,
            ]
        )
        if index < len(session.results) - 1:
            story.append(PageBreak())

    def decorate_cover(canvas: Any, _: Any) -> None:
        width, height = A4
        canvas.saveState()
        canvas.setFillColor(_DARK)
        canvas.rect(0, 0, width, height, fill=1, stroke=0)
        canvas.setStrokeColor(colors.HexColor("#26342B"))
        canvas.setLineWidth(0.6)
        for radius in (54 * mm, 75 * mm, 98 * mm):
            canvas.circle(width - 15 * mm, height - 28 * mm, radius, fill=0, stroke=1)
        canvas.setStrokeColor(colors.HexColor("#6F9D7A"))
        canvas.setLineWidth(1.1)
        canvas.line(20 * mm, height - 17 * mm, 43 * mm, height - 17 * mm)
        canvas.setFillColor(colors.HexColor("#647268"))
        canvas.setFont(regular, 7)
        canvas.drawString(20 * mm, 10 * mm, "Светлый круг · архив решений")
        canvas.drawRightString(width - 20 * mm, 10 * mm, "T-Mod / TVR")
        canvas.restoreState()

    def decorate_protocol_page(canvas: Any, doc: Any) -> None:
        width, height = A4
        canvas.saveState()
        canvas.setFillColor(_PAPER)
        canvas.rect(0, 0, width, height, fill=1, stroke=0)
        canvas.setStrokeColor(colors.HexColor("#22362A"))
        canvas.setLineWidth(0.55)
        for radius in (31 * mm, 46 * mm, 63 * mm):
            canvas.circle(width + 4 * mm, height + 2 * mm, radius, fill=0, stroke=1)
        canvas.setStrokeColor(_GREEN)
        canvas.setLineWidth(1.15)
        canvas.line(12 * mm, 18 * mm, 12 * mm, height - 18 * mm)
        canvas.setStrokeColor(_HAIRLINE)
        canvas.setLineWidth(0.35)
        canvas.line(20 * mm, height - 14 * mm, width - 20 * mm, height - 14 * mm)
        canvas.setFont(bold, 6.5)
        canvas.setFillColor(_GREEN)
        canvas.drawString(20 * mm, height - 10.5 * mm, "T-MOD  /  CONSENSUS")
        canvas.setFont(regular, 7)
        canvas.setFillColor(_MUTED)
        canvas.drawString(20 * mm, 9.5 * mm, f"Заседание {int(session.plenary_number):02d}")
        canvas.drawRightString(width - 20 * mm, 9.5 * mm, f"{doc.page:02d}")
        canvas.restoreState()

    document.build(
        story,
        onFirstPage=decorate_cover,
        onLaterPages=decorate_protocol_page,
    )
    return destination


def ensure_session_artifacts(
    session: LiveConsensusSession,
    *,
    force: bool = False,
) -> dict[str, Any]:
    cards = [
        generate_result_card(session, result, force=force)
        for result in session.results
    ]
    return {
        "pdf": generate_session_report(session, force=force),
        "cover": generate_session_cover(session, force=force),
        "cards": cards,
    }


__all__ = [
    "ensure_session_artifacts",
    "generate_result_card",
    "generate_session_cover",
    "generate_session_report",
    "result_card_path",
    "session_artifact_directory",
    "session_cover_path",
    "session_report_path",
]
