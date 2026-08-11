"""Visual result cards and archival PDF protocols for plenary consensus."""

from __future__ import annotations

import hashlib
import html
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
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


def _font_path(*, bold: bool = False) -> str:
    candidates = _BOLD_FONT_CANDIDATES if bold else _FONT_CANDIDATES
    return next((item for item in candidates if Path(item).is_file()), candidates[0])


def _artifact_root() -> Path:
    configured = str(os.getenv("CONSENSUS_ARTIFACTS_DIR", "")).strip()
    root = Path(configured) if configured else persistence_core.DATA_DIR.parent / "consensus-artifacts"
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
    return session_artifact_directory(session_key) / f"result-{int(bill_number):03d}.png"


def session_report_path(session_key: str) -> Path:
    return session_artifact_directory(session_key) / "consensus.pdf"


def session_cover_path(session_key: str) -> Path:
    return session_artifact_directory(session_key) / "summary.png"


def _fit_text(draw: ImageDraw.ImageDraw, value: str, *, font: ImageFont.FreeTypeFont, width: int, max_lines: int) -> list[str]:
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
        "accepted": ("ПРИНЯТ", (116, 226, 151)),
        "rejected": ("ОТКЛОНЁН", (241, 129, 119)),
        "vetoed": ("ПРАВО ВЕТО", (241, 164, 117)),
        "oral": ("УСТНОЕ РЕШЕНИЕ", (181, 196, 239)),
    }.get(str(status), ("ЗАФИКСИРОВАН", (208, 222, 211)))


def generate_result_card(
    session: LiveConsensusSession,
    result: LiveResult,
    *,
    force: bool = False,
) -> Path:
    destination = result_card_path(session.session_key, result.bill_number)
    if destination.is_file() and not force:
        return destination
    image = Image.new("RGB", (1600, 900), (5, 8, 8))
    draw = ImageDraw.Draw(image)
    regular = _font_path()
    bold = _font_path(bold=True)
    title_font = ImageFont.truetype(bold, 66)
    label_font = ImageFont.truetype(bold, 20)
    metric_label_font = ImageFont.truetype(bold, 16)
    small_font = ImageFont.truetype(regular, 23)
    metric_font = ImageFont.truetype(bold, 82)
    block_font = ImageFont.truetype(bold, 22)
    status_label, accent = _status_copy(result.status)

    for radius, alpha in ((650, 25), (410, 18), (230, 14)):
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        layer_draw = ImageDraw.Draw(layer)
        layer_draw.ellipse((800 - radius, 450 - radius, 800 + radius, 450 + radius), outline=(*accent, alpha), width=2)
        image = Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.text((82, 70), "T  ·  C O N S E N S U S", font=label_font, fill=(192, 208, 196))
    draw.text((1518, 70), f"{int(session.plenary_number):02d}", font=label_font, fill=(143, 161, 148), anchor="ra")
    draw.rounded_rectangle((80, 138, 1520, 820), radius=34, fill=(10, 15, 14), outline=(45, 59, 50), width=2)
    draw.text((126, 186), f"ЗАКОНОПРОЕКТ №{int(result.bill_number):03d}", font=label_font, fill=accent)
    draw.rounded_rectangle((1210, 176, 1468, 224), radius=24, fill=tuple(max(0, value // 8) for value in accent), outline=accent, width=1)
    draw.text((1339, 200), status_label, font=label_font, fill=accent, anchor="mm")
    lines = _fit_text(draw, result.title, font=title_font, width=1040, max_lines=3)
    for index, line in enumerate(lines):
        draw.text((126, 254 + index * 76), line, font=title_font, fill=(242, 246, 241))

    metric_y = 520
    draw.text((126, metric_y), "ОБЩИЙ КОНСЕНСУС", font=metric_label_font, fill=(137, 154, 142))
    draw.text((126, metric_y + 38), f"{float(result.overall_percent):.1f}%", font=metric_font, fill=(239, 245, 239))
    draw.text((520, metric_y), "КОНСЕНСУС ТОВАРИЩЕСТВА", font=metric_label_font, fill=(137, 154, 142))
    draw.text((520, metric_y + 55), f"{float(result.internal_percent):.1f}%", font=ImageFont.truetype(bold, 48), fill=(214, 226, 216))
    draw.text((850, metric_y), "ТРЕБУЕМЫЙ ПОРОГ", font=metric_label_font, fill=(137, 154, 142))
    draw.text((850, metric_y + 55), f"{float(result.required_percent or 50):.1f}%", font=ImageFont.truetype(bold, 48), fill=(214, 226, 216))

    labels = (("first", "I"), ("second", "II"), ("third", "III"), ("consensus", "TVR"))
    for index, (key, label) in enumerate(labels):
        x = 1120 + (index % 2) * 180
        y = 510 + (index // 2) * 112
        value = str(result.block_votes.get(key) or "inactive")
        copy = {"yes": "ЗА", "no": "ПРОТИВ", "abstain": "ВОЗД.", "inactive": "НЕАКТ."}.get(value, value.upper())
        draw.rounded_rectangle((x, y, x + 160, y + 92), radius=20, fill=(16, 23, 20), outline=(48, 65, 54), width=1)
        draw.text((x + 18, y + 17), label, font=small_font, fill=(139, 157, 144))
        draw.text((x + 18, y + 50), copy, font=block_font, fill=(229, 236, 229))
    draw.text((126, 770), f"Ведущий · {session.leader_display}", font=small_font, fill=(130, 148, 136))
    draw.text((1470, 770), datetime.now(timezone.utc).strftime("%d.%m.%Y · %H:%M UTC"), font=small_font, fill=(130, 148, 136), anchor="ra")
    image.save(destination, "PNG", optimize=True)
    return destination


def generate_session_cover(session: LiveConsensusSession, *, force: bool = False) -> Path:
    destination = session_cover_path(session.session_key)
    if destination.is_file() and not force:
        return destination
    image = Image.new("RGB", (1600, 900), (5, 8, 8))
    draw = ImageDraw.Draw(image)
    bold = _font_path(bold=True)
    regular = _font_path()
    draw.ellipse((370, -260, 1230, 600), outline=(41, 67, 48), width=2)
    draw.ellipse((510, -120, 1090, 460), outline=(30, 50, 36), width=2)
    draw.text((800, 104), "Т О В А Р И Щ Е С Т В О", font=ImageFont.truetype(bold, 22), fill=(154, 175, 160), anchor="ma")
    draw.text((800, 246), f"{int(session.plenary_number)}", font=ImageFont.truetype(bold, 168), fill=(241, 246, 241), anchor="mm")
    draw.text((800, 390), "Пленарный Консенсус", font=ImageFont.truetype(bold, 62), fill=(235, 241, 235), anchor="mm")
    draw.text((800, 468), "Итоговый протокол", font=ImageFont.truetype(regular, 32), fill=(150, 169, 155), anchor="mm")
    counts = {
        "Принято": sum(item.status == "accepted" for item in session.results),
        "Отклонено": sum(item.status == "rejected" for item in session.results),
        "Вето": sum(item.status == "vetoed" for item in session.results),
        "Всего": len(session.results),
    }
    for index, (label, value) in enumerate(counts.items()):
        x = 235 + index * 300
        draw.rounded_rectangle((x, 590, x + 250, 720), radius=26, fill=(11, 17, 15), outline=(43, 58, 48), width=2)
        draw.text((x + 28, 620), label.upper(), font=ImageFont.truetype(bold, 17), fill=(135, 153, 140))
        draw.text((x + 28, 655), str(value), font=ImageFont.truetype(bold, 45), fill=(232, 239, 232))
    draw.text((800, 810), f"Ведущий · {session.leader_display}", font=ImageFont.truetype(regular, 24), fill=(139, 157, 144), anchor="mm")
    image.save(destination, "PNG", optimize=True)
    return destination


def _register_pdf_fonts() -> tuple[str, str]:
    regular_name = "TModSans"
    bold_name = "TModSansBold"
    if regular_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(regular_name, _font_path()))
    if bold_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(bold_name, _font_path(bold=True)))
    return regular_name, bold_name


def _safe(value: Any) -> str:
    return html.escape(str(value or "—")).replace("\n", "<br/>")


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
    duration_minutes = max(1, round((generated_at - started_at).total_seconds() / 60))
    styles = getSampleStyleSheet()
    title = ParagraphStyle("TTitle", parent=styles["Title"], fontName=bold, fontSize=24, leading=29, textColor=colors.HexColor("#101712"), alignment=TA_LEFT, spaceAfter=6 * mm)
    heading = ParagraphStyle("THeading", parent=styles["Heading2"], fontName=bold, fontSize=15, leading=19, textColor=colors.HexColor("#172219"), spaceBefore=3 * mm, spaceAfter=3 * mm)
    body = ParagraphStyle("TBody", parent=styles["BodyText"], fontName=regular, fontSize=9.5, leading=14, textColor=colors.HexColor("#29352C"), spaceAfter=3 * mm)
    caption = ParagraphStyle("TCaption", parent=body, fontSize=7.5, leading=10, textColor=colors.HexColor("#617066"), alignment=TA_CENTER)
    document = SimpleDocTemplate(str(destination), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm, title=f"Протокол пленарного консенсуса №{session.plenary_number}", author="T-Mod")
    story: list[Any] = [
        Paragraph("Т О В А Р И Щ Е С Т В О", caption),
        Spacer(1, 7 * mm),
        Paragraph(f"Протокол пленарного консенсуса №{int(session.plenary_number)}", title),
        Paragraph(
            f"Ведущий: <b>{_safe(session.leader_display)}</b><br/>"
            f"Начало: <b>{started_at.strftime('%d.%m.%Y · %H:%M UTC')}</b><br/>"
            f"Завершение: <b>{generated_at.strftime('%d.%m.%Y · %H:%M UTC')}</b> · "
            f"продолжительность: <b>{duration_minutes} мин.</b><br/>"
            f"Участников с подтверждённым мандатом: "
            f"<b>{len(session.confirmed_participants())}</b><br/>"
            f"Рассмотрено законопроектов: <b>{len(session.results)}</b>",
            body,
        ),
        Spacer(1, 4 * mm),
    ]
    summary_data = [["Итог", "Количество"], ["Принято", str(sum(item.status == "accepted" for item in session.results))], ["Отклонено", str(sum(item.status == "rejected" for item in session.results))], ["Вето", str(sum(item.status == "vetoed" for item in session.results))]]
    summary = Table(summary_data, colWidths=[115 * mm, 35 * mm])
    summary.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, 0), bold), ("FONTNAME", (0, 1), (-1, -1), regular), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEE9")), ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#CAD4CC")), ("PADDING", (0, 0), (-1, -1), 7)]))
    story.extend([summary, PageBreak()])

    participant_names = {int(item.user_id): str(item.display_name) for item in session.participants.values()}
    for index, result in enumerate(session.results):
        bill = storage.tvrs_get_bill_dict_by_id(int(result.bill_id)) or {}
        status_label, _ = _status_copy(result.status)
        story.extend([
            Paragraph(f"Законопроект №{int(result.bill_number):03d}", caption),
            Paragraph(_safe(result.title), title),
            Paragraph(f"Автор: <b>{_safe(bill.get('author_display') or 'не указан')}</b> · Решение: <b>{_safe(status_label)}</b>", body),
        ])
        metric_data = [["Общий консенсус", "Товарищество", "Порог", "Против"], [f"{result.overall_percent:.1f}%", f"{result.internal_percent:.1f}%", f"{result.required_percent:.1f}%", f"{result.opposed_percent:.1f}%"]]
        metrics = Table(metric_data, colWidths=[39 * mm] * 4)
        metrics.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, 0), regular), ("FONTNAME", (0, 1), (-1, 1), bold), ("FONTSIZE", (0, 0), (-1, 0), 7.5), ("FONTSIZE", (0, 1), (-1, 1), 15), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#68776C")), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F6F3")), ("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#D0D9D2")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("PADDING", (0, 0), (-1, -1), 8)]))
        story.extend([metrics, Spacer(1, 5 * mm)])
        if bill.get("summary"):
            story.extend([Paragraph("Текст предложения", heading), Paragraph(_safe(bill.get("summary")), body)])
        if bill.get("materials"):
            story.extend([Paragraph("Материалы", heading), Paragraph(_safe(bill.get("materials")), body)])
        vote_rows = [["Участник", "Голос"]]
        for user_id, vote in sorted(result.votes.items(), key=lambda item: participant_names.get(int(item[0]), str(item[0])).casefold()):
            vote_rows.append([participant_names.get(int(user_id), str(user_id)), {"yes": "За", "no": "Против", "abstain": "Воздержался"}.get(str(vote), str(vote))])
        votes = Table(vote_rows, colWidths=[115 * mm, 41 * mm], repeatRows=1)
        votes.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, 0), bold), ("FONTNAME", (0, 1), (-1, -1), regular), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEE9")), ("GRID", (0, 0), (-1, -1), .3, colors.HexColor("#D1DAD3")), ("PADDING", (0, 0), (-1, -1), 6)]))
        story.extend([Paragraph("Именной протокол голосов", heading), votes])
        if index < len(session.results) - 1:
            story.append(PageBreak())

    def decorate_page(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont(regular, 7)
        canvas.setFillColor(colors.HexColor("#748078"))
        canvas.drawString(18 * mm, 10 * mm, "T-Mod · Consensus · Светлый круг")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Страница {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=decorate_page, onLaterPages=decorate_page)
    return destination


def ensure_session_artifacts(session: LiveConsensusSession, *, force: bool = False) -> dict[str, Any]:
    cards = [generate_result_card(session, result, force=force) for result in session.results]
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
