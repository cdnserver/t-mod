"""Deterministic PDF renderer for immutable Atlas legal revisions."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, ListFlowable, ListItem, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)


PUBLISHER = "Atlas · Технологии Товарищества"
_MONTHS = ("", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")


def _human_date(value: object) -> str:
    try:
        selected = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return f"{selected.day} {_MONTHS[selected.month]} {selected.year} года"


def legal_pdf_directory() -> Path:
    return Path(os.getenv("ATLAS_LEGAL_REVISION_DIR", "/app/persistent/data/atlas-legal"))


def legal_pdf_path(revision: dict) -> Path:
    return legal_pdf_directory() / f"{revision['document_key']}-v{int(revision['version_number'])}.pdf"


def _font(candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise RuntimeError("atlas_legal_pdf_font_unavailable")


def _register_fonts() -> None:
    if "AtlasLegalBody" in pdfmetrics.getRegisteredFontNames():
        return
    regular = _font((
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Verdana.ttf",
    ))
    bold = _font((
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
        regular,
    ))
    pdfmetrics.registerFont(TTFont("AtlasLegalBody", regular))
    pdfmetrics.registerFont(TTFont("AtlasLegalHeading", bold))


class _Document(BaseDocTemplate):
    def __init__(self, filename: Path, revision: dict):
        super().__init__(
            str(filename), pagesize=A4, leftMargin=22 * mm, rightMargin=22 * mm,
            topMargin=27 * mm, bottomMargin=23 * mm, title=str(revision["title"]),
            author=PUBLISHER, subject=f"Atlas · {revision['version_label']}", creator="Atlas Information Center",
        )
        self.revision = revision
        self.addPageTemplates(PageTemplate(
            id="atlas-legal", frames=[Frame(self.leftMargin, self.bottomMargin, self.width, self.height)],
            onPage=self._decorate,
        ))

    def _decorate(self, canvas, doc):
        width, height = A4
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#C9D9E7"))
        canvas.setLineWidth(.35)
        canvas.line(22 * mm, height - 18 * mm, width - 22 * mm, height - 18 * mm)
        canvas.line(22 * mm, 16 * mm, width - 22 * mm, 16 * mm)
        canvas.setFont("AtlasLegalHeading", 6.8)
        canvas.setFillColor(colors.HexColor("#154A73"))
        canvas.drawString(22 * mm, height - 14 * mm, "ATLAS  /  ПРАВОВОЙ ЦЕНТР")
        canvas.setFont("AtlasLegalBody", 7.2)
        canvas.setFillColor(colors.HexColor("#60788C"))
        canvas.drawRightString(width - 22 * mm, height - 14 * mm, str(self.revision["version_label"]))
        canvas.drawString(22 * mm, 10 * mm, "atlas.tvr.lat")
        canvas.drawCentredString(width / 2, 10 * mm, f"Редакция {int(self.revision['version_number'])}")
        canvas.drawRightString(width - 22 * mm, 10 * mm, str(doc.page))
        canvas.restoreState()


def render_legal_pdf(revision: dict) -> tuple[Path, str]:
    _register_fonts()
    target = legal_pdf_path(revision)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".pdf.tmp")
    styles = getSampleStyleSheet()
    title = ParagraphStyle("title", parent=styles["Title"], fontName="AtlasLegalHeading", fontSize=24,
                           leading=30, textColor=colors.HexColor("#0A1C2B"), spaceAfter=11)
    subtitle = ParagraphStyle("subtitle", parent=styles["Normal"], fontName="AtlasLegalBody", fontSize=10,
                              leading=16, textColor=colors.HexColor("#536C80"), spaceAfter=18)
    section = ParagraphStyle("section", parent=styles["Heading2"], fontName="AtlasLegalHeading", fontSize=11,
                             leading=16, textColor=colors.HexColor("#103D60"), spaceBefore=10, spaceAfter=6,
                             keepWithNext=True)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontName="AtlasLegalBody", fontSize=9.2,
                          leading=14.5, textColor=colors.HexColor("#263D4F"), spaceAfter=7)
    notice = ParagraphStyle("notice", parent=body, backColor=colors.HexColor("#EEF5FA"),
                            borderColor=colors.HexColor("#BCD4E6"), borderWidth=.5, borderPadding=9,
                            textColor=colors.HexColor("#173E5B"), spaceBefore=5, spaceAfter=9)
    meta = ParagraphStyle("meta", parent=body, fontSize=7.8, leading=11, textColor=colors.HexColor("#536D82"))
    story = [
        Spacer(1, 6 * mm), Paragraph("ОФИЦИАЛЬНАЯ РЕДАКЦИЯ", meta),
        Paragraph(escape(str(revision["title"])), title),
        Paragraph(escape(str(revision.get("summary") or "")), subtitle),
    ]
    metadata = Table([
        [Paragraph("РЕДАКЦИЯ", meta), Paragraph("ВСТУПАЕТ В СИЛУ", meta)],
        [Paragraph(escape(str(revision["version_label"])), meta), Paragraph(escape(_human_date(revision["effective_from"])), meta)],
        [Paragraph("ПУБЛИКАТОР", meta), Paragraph("ПОСТОЯННЫЙ АДРЕС", meta)],
        [Paragraph(PUBLISHER, meta), Paragraph("https://atlas.tvr.lat/legal", meta)],
    ], colWidths=[82 * mm, 82 * mm])
    metadata.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F4F8FB")),
        ("BOX", (0, 0), (-1, -1), .4, colors.HexColor("#CFDDE8")),
        ("INNERGRID", (0, 0), (-1, -1), .25, colors.HexColor("#D9E5EE")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [metadata, Spacer(1, 7 * mm)]
    for block in revision.get("blocks") or []:
        kind = str(block.get("type") or "paragraph")
        heading = str(block.get("title") or "").strip()
        if heading:
            story.append(Paragraph(escape(heading), section))
        if kind == "list":
            items = [ListItem(Paragraph(escape(str(item)), body), leftIndent=4 * mm) for item in block.get("items") or []]
            story.append(ListFlowable(items, bulletType="bullet", leftIndent=5 * mm, bulletFontName="AtlasLegalBody"))
        else:
            text = str(block.get("text") or "").strip()
            if text:
                story.append(Paragraph(escape(text).replace("\n", "<br/>"), notice if kind == "notice" else body))
    _Document(temporary, revision).build(story)
    temporary.replace(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return target, digest


__all__ = ["legal_pdf_path", "render_legal_pdf"]
