"""Archival PDF dossiers for OVR investigations."""

from __future__ import annotations

import html
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions

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

_PAPER = colors.HexColor("#060A0D")
_SURFACE = colors.HexColor("#0D151A")
_SURFACE_ALT = colors.HexColor("#101B20")
_TEXT = colors.HexColor("#ECF7F2")
_MUTED = colors.HexColor("#81928C")
_DIM = colors.HexColor("#596963")
_MINT = colors.HexColor("#68E2B4")
_AMBER = colors.HexColor("#E9BB6C")
_RED = colors.HexColor("#FF7883")
_BLUE = colors.HexColor("#82B2E8")
_LINE = colors.HexColor("#25332F")

_STATUS_LABELS = {
    "new": "Входящий сигнал",
    "screening": "Сбор сведений",
    "needs_info": "Ожидаются сведения",
    "analysis": "Аналитика",
    "decision": "Подготовка решения",
    "approved": "Допуск одобрен",
    "denied": "В допуске отказано",
    "archived": "Архив",
}
_KIND_LABELS = {
    "admission": "Проверка при вступлении",
    "background": "Фоновая проверка",
    "incident": "Инцидент",
    "internal": "Внутренняя проверка",
    "other": "Иное расследование",
}
_RISK_LABELS = {
    "unrated": "Не определён",
    "low": "Низкий",
    "medium": "Средний",
    "high": "Высокий",
    "critical": "Критический",
}
_PRIORITY_LABELS = {
    "normal": "Обычный",
    "important": "Важный",
    "urgent": "Срочный",
    "critical": "Критический",
}
_CLASSIFICATION_LABELS = {
    "restricted": "ОГРАНИЧЕНО",
    "secret": "СЕКРЕТНО",
    "top_secret": "ОСОБОЙ ВАЖНОСТИ",
}
_MATERIAL_KIND_LABELS = {
    "document": "Документ",
    "link": "Ссылка",
    "testimony": "Свидетельство",
    "observation": "Наблюдение",
    "media": "Медиа",
    "other": "Иное",
}
_RELIABILITY_LABELS = {
    "unrated": "Не оценено",
    "low": "Низкая",
    "medium": "Средняя",
    "high": "Высокая",
    "confirmed": "Подтверждено",
}
_MATERIAL_STATUS_LABELS = {
    "new": "Новый",
    "verified": "Проверен",
    "rejected": "Отклонён",
}
_TASK_STATUS_LABELS = {
    "todo": "К выполнению",
    "doing": "В работе",
    "blocked": "Заблокирована",
    "done": "Выполнена",
}
_EVENT_LABELS = {
    "created": "Расследование зарегистрировано",
    "claim": "Расследование принято в работу",
    "note": "Добавлена служебная запись",
    "update": "Аналитическая карточка обновлена",
    "needs_info": "Запрошены дополнительные сведения",
    "analysis": "Начат аналитический этап",
    "decision": "Материалы подготовлены к решению",
    "approve": "Допуск одобрен",
    "deny": "В допуске отказано",
    "reopen": "Расследование открыто повторно",
    "archive": "Расследование перенесено в архив",
    "material_added": "Добавлен материал",
    "material_status": "Изменён статус материала",
    "relation_added": "Добавлена связь",
    "task_added": "Поставлена задача",
    "task_status": "Изменён статус задачи",
}


def _first_existing(candidates: Iterable[str]) -> str:
    return next((item for item in candidates if Path(item).is_file()), next(iter(candidates)))


def _fonts() -> tuple[str, str]:
    regular = "TModOVRRegular"
    bold = "TModOVRBold"
    if regular not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(regular, _first_existing(_FONT_CANDIDATES)))
    if bold not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(bold, _first_existing(_BOLD_FONT_CANDIDATES)))
    return regular, bold


def _clean_text(value: Any, fallback: str = "") -> str:
    text = str(value or fallback).strip()
    return (
        text.replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )


def _safe(value: Any, fallback: str = "Не указано") -> str:
    text = _clean_text(value, fallback)
    return html.escape(text or fallback).replace("\n", "<br/>")


def _excerpt(value: Any, limit: int = 620) -> str:
    text = _clean_text(value, "Цель не сформулирована.")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip(" ,.;:-") + "…"


def _moment(value: Any, fallback: str = "Не указано") -> str:
    clean = str(value or "").strip()
    if not clean:
        return fallback
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError:
        return _clean_text(clean)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone()
    return local.strftime("%d.%m.%Y / %H:%M")


def _case_number(case: dict[str, Any]) -> str:
    return f"ОВР-{int(case.get('case_number') or 0):03d}"


def _full_name(case: dict[str, Any]) -> str:
    return " ".join(
        item for item in (
            _clean_text(case.get("first_name")),
            _clean_text(case.get("last_name")),
        ) if item
    ) or "Без имени"


def _styles(regular: str, bold: str) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "eyebrow": ParagraphStyle(
            "OVREyebrow", parent=base["BodyText"], fontName=bold, fontSize=7,
            leading=9, textColor=_MINT, spaceAfter=3 * mm,
        ),
        "cover_number": ParagraphStyle(
            "OVRCoverNumber", parent=base["Title"], fontName=bold, fontSize=45,
            leading=48, textColor=_MINT, spaceAfter=2 * mm,
        ),
        "cover_title": ParagraphStyle(
            "OVRCoverTitle", parent=base["Title"], fontName=bold, fontSize=27,
            leading=31, textColor=_TEXT, spaceAfter=2 * mm,
        ),
        "cover_name": ParagraphStyle(
            "OVRCoverName", parent=base["Title"], fontName=regular, fontSize=15,
            leading=19, textColor=colors.HexColor("#B9C8C2"), spaceAfter=10 * mm,
        ),
        "title": ParagraphStyle(
            "OVRTitle", parent=base["Heading1"], fontName=bold, fontSize=22,
            leading=26, textColor=_TEXT, spaceAfter=4 * mm,
        ),
        "section": ParagraphStyle(
            "OVRSection", parent=base["Heading2"], fontName=bold, fontSize=8,
            leading=10, textColor=_MINT, spaceBefore=5 * mm, spaceAfter=3 * mm,
        ),
        "subsection": ParagraphStyle(
            "OVRSubsection", parent=base["Heading3"], fontName=bold, fontSize=11,
            leading=14, textColor=_TEXT, spaceBefore=2 * mm, spaceAfter=1.5 * mm,
        ),
        "body": ParagraphStyle(
            "OVRBody", parent=base["BodyText"], fontName=regular, fontSize=9.2,
            leading=14, textColor=colors.HexColor("#C4D1CC"), spaceAfter=3 * mm,
            splitLongWords=True,
        ),
        "body_small": ParagraphStyle(
            "OVRBodySmall", parent=base["BodyText"], fontName=regular, fontSize=7.7,
            leading=11, textColor=_MUTED, splitLongWords=True,
        ),
        "label": ParagraphStyle(
            "OVRLabel", parent=base["BodyText"], fontName=bold, fontSize=6.5,
            leading=8, textColor=_MUTED,
        ),
        "value": ParagraphStyle(
            "OVRValue", parent=base["BodyText"], fontName=bold, fontSize=8.4,
            leading=11, textColor=_TEXT, splitLongWords=True,
        ),
        "metric": ParagraphStyle(
            "OVRMetric", parent=base["BodyText"], fontName=bold, fontSize=18,
            leading=20, textColor=_TEXT, alignment=TA_CENTER,
        ),
        "metric_label": ParagraphStyle(
            "OVRMetricLabel", parent=base["BodyText"], fontName=bold, fontSize=6.2,
            leading=8, textColor=_MUTED, alignment=TA_CENTER,
        ),
        "table_header": ParagraphStyle(
            "OVRTableHeader", parent=base["BodyText"], fontName=bold, fontSize=6.3,
            leading=8, textColor=_MINT,
        ),
        "table_value": ParagraphStyle(
            "OVRTableValue", parent=base["BodyText"], fontName=regular, fontSize=7.6,
            leading=10.5, textColor=colors.HexColor("#C6D2CE"), splitLongWords=True,
        ),
        "right": ParagraphStyle(
            "OVRRight", parent=base["BodyText"], fontName=bold, fontSize=7.6,
            leading=10, textColor=_TEXT, alignment=TA_RIGHT,
        ),
    }


def _meta_table(rows: list[tuple[str, str]], styles: dict[str, ParagraphStyle]) -> Table:
    cells: list[list[Any]] = []
    for index in range(0, len(rows), 2):
        pair = rows[index:index + 2]
        row: list[Any] = []
        for label, value in pair:
            row.append(
                [
                    Paragraph(_safe(label), styles["label"]),
                    Paragraph(_safe(value), styles["value"]),
                ]
            )
        if len(row) == 1:
            row.append([Paragraph("", styles["label"]), Paragraph("", styles["value"])])
        cells.append(row)
    table = Table(cells, colWidths=[83 * mm, 83 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), _SURFACE),
                ("BOX", (0, 0), (-1, -1), 0.4, _LINE),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, _LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3.2 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2 * mm),
            ]
        )
    )
    return table


def _metrics(
    values: list[tuple[str, Any]],
    styles: dict[str, ParagraphStyle],
) -> Table:
    width = 166 * mm / max(1, len(values))
    table = Table(
        [
            [Paragraph(_safe(value, "0"), styles["metric"]) for _, value in values],
            [Paragraph(_safe(label), styles["metric_label"]) for label, _ in values],
        ],
        colWidths=[width] * len(values),
        rowHeights=[12 * mm, 8 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), _SURFACE),
                ("BOX", (0, 0), (-1, -1), 0.4, _LINE),
                ("LINEAFTER", (0, 0), (-2, -1), 0.3, _LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 1 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 1 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
            ]
        )
    )
    return table


def _data_table(
    rows: list[list[Any]],
    widths: list[float],
    styles: dict[str, ParagraphStyle],
) -> Table:
    converted: list[list[Any]] = []
    for row_index, row in enumerate(rows):
        converted.append(
            [
                value if hasattr(value, "wrap") else Paragraph(
                    _safe(value, "-"),
                    styles["table_header" if row_index == 0 else "table_value"],
                )
                for value in row
            ]
        )
    table = Table(converted, colWidths=widths, repeatRows=1, hAlign="LEFT")
    commands: list[tuple[Any, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), _SURFACE_ALT),
        ("LINEABOVE", (0, 0), (-1, 0), 0.65, _MINT),
        ("LINEBELOW", (0, 0), (-1, 0), 0.35, _LINE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.2, _LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2.3 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.3 * mm),
    ]
    for row_index in range(2, len(rows), 2):
        commands.append(("BACKGROUND", (0, row_index), (-1, row_index), colors.HexColor("#0A1115")))
    table.setStyle(TableStyle(commands))
    return table


def _material_story(
    materials: list[dict[str, Any]],
    styles: dict[str, ParagraphStyle],
) -> list[Any]:
    if not materials:
        return [Paragraph("Материалы в досье отсутствуют.", styles["body"])]
    story: list[Any] = []
    for index, item in enumerate(materials, 1):
        status = _MATERIAL_STATUS_LABELS.get(str(item.get("status")), str(item.get("status") or "Новый"))
        reliability = _RELIABILITY_LABELS.get(str(item.get("reliability")), str(item.get("reliability") or "Не оценено"))
        header = Table(
            [[
                Paragraph(
                    f"{index:02d} / {_safe(_MATERIAL_KIND_LABELS.get(str(item.get('kind')), item.get('kind')))}",
                    styles["table_header"],
                ),
                Paragraph(_safe(f"{status} / {reliability}"), styles["right"]),
            ]],
            colWidths=[104 * mm, 62 * mm],
        )
        header.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), _SURFACE_ALT),
                    ("LINEABOVE", (0, 0), (-1, 0), 0.6, _MINT if item.get("status") == "verified" else _AMBER),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ]
            )
        )
        title = Paragraph(_safe(item.get("title"), "Материал без названия"), styles["subsection"])
        metadata = Paragraph(
            _safe(
                f"Источник: {item.get('source_url') or 'не указан'}  /  "
                f"Добавил: {item.get('created_by_display') or 'не указан'}  /  "
                f"{_moment(item.get('created_at'))}"
            ),
            styles["body_small"],
        )
        story.extend([KeepTogether([header, Spacer(1, 2.5 * mm), title, metadata])])
        if item.get("content"):
            story.append(Paragraph(_safe(item.get("content")), styles["body"]))
        story.append(Spacer(1, 3 * mm))
    return story


def generate_investigation_report(
    detail: dict[str, Any],
    *,
    destination: Path,
    force: bool = False,
) -> Path:
    case = dict(detail.get("case") or {})
    if not case:
        raise ValueError("ovr_report_case_required")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size > 0 and not force:
        return destination

    regular, bold = _fonts()
    styles = _styles(regular, bold)
    materials = [dict(item) for item in detail.get("materials") or []]
    relations = [dict(item) for item in detail.get("relations") or []]
    tasks = [dict(item) for item in detail.get("tasks") or []]
    events = [dict(item) for item in detail.get("events") or []]
    verified = sum(item.get("status") == "verified" for item in materials)
    completed_tasks = sum(item.get("status") == "done" for item in tasks)
    generated_at = datetime.now(timezone.utc)

    document = SimpleDocTemplate(
        str(destination), pagesize=A4,
        leftMargin=22 * mm, rightMargin=22 * mm,
        topMargin=21 * mm, bottomMargin=18 * mm,
        title=f"{_case_number(case)} / {_full_name(case)}",
        author="T-Mod / Отдел внешней разведки",
        subject="Архивное досье расследования ОВР",
    )

    story: list[Any] = [
        Paragraph("T-MOD / ОТДЕЛ ВНЕШНЕЙ РАЗВЕДКИ / ДОСЬЕ", styles["eyebrow"]),
        Spacer(1, 15 * mm),
        Paragraph(_case_number(case), styles["cover_number"]),
        Paragraph("Расследование", styles["cover_title"]),
        Paragraph(_safe(_full_name(case)), styles["cover_name"]),
        _meta_table(
            [
                ("СТАТУС", _STATUS_LABELS.get(str(case.get("status")), str(case.get("status") or "Не указан"))),
                ("ГРИФ", _CLASSIFICATION_LABELS.get(str(case.get("classification")), str(case.get("classification") or "ОГРАНИЧЕНО"))),
                ("ТИП", _KIND_LABELS.get(str(case.get("case_kind")), str(case.get("case_kind") or "Расследование"))),
                ("РИСК", _RISK_LABELS.get(str(case.get("risk_level")), str(case.get("risk_level") or "Не определён"))),
                ("ОТВЕТСТВЕННЫЙ", _clean_text(case.get("assigned_to_display"), "Не назначен")),
                ("ПРИОРИТЕТ", _PRIORITY_LABELS.get(str(case.get("priority")), str(case.get("priority") or "Обычный"))),
                ("ОТКРЫТО", _moment(case.get("created_at"))),
                ("КОНТРОЛЬНЫЙ СРОК", _moment(case.get("due_at"))),
            ],
            styles,
        ),
        Spacer(1, 9 * mm),
        _metrics(
            [
                ("МАТЕРИАЛОВ", len(materials)),
                ("ПРОВЕРЕНО", verified),
                ("СВЯЗЕЙ", len(relations)),
                ("ЗАДАЧ ВЫПОЛНЕНО", f"{completed_tasks}/{len(tasks)}"),
            ],
            styles,
        ),
        Spacer(1, 8 * mm),
        Paragraph("ЦЕЛЬ РАССЛЕДОВАНИЯ", styles["section"]),
        Paragraph(_safe(_excerpt(case.get("objective"))), styles["body"]),
        PageBreak(),
        Paragraph("ИДЕНТИФИКАЦИОННАЯ КАРТА", styles["eyebrow"]),
        Paragraph(_safe(_full_name(case)), styles["title"]),
        _meta_table(
            [
                ("СТАТИК", _clean_text(case.get("static_id"), "Не указан")),
                ("DISCORD", _clean_text(case.get("discord_text"), "Не указан")),
                ("ПСЕВДОНИМЫ", _clean_text(case.get("aliases"), "Не установлены")),
                ("ОРГАНИЗАЦИИ", _clean_text(case.get("affiliations"), "Не установлены")),
                ("ФОРУМ", _clean_text(case.get("forum_url"), "Не указан")),
                ("ИНИЦИАТОР", _clean_text(case.get("created_by_display"), "Не указан")),
            ],
            styles,
        ),
        Paragraph("ИСХОДНЫЕ СВЕДЕНИЯ", styles["section"]),
        Paragraph(_safe(case.get("additional_info"), "Исходные сведения не указаны."), styles["body"]),
        Paragraph("АНАЛИТИЧЕСКОЕ ЯДРО", styles["section"]),
        Paragraph("Цель расследования", styles["subsection"]),
        Paragraph(_safe(case.get("objective"), "Цель не сформулирована."), styles["body"]),
        Paragraph("Рабочая гипотеза", styles["subsection"]),
        Paragraph(_safe(case.get("hypothesis"), "Рабочая гипотеза не сформирована."), styles["body"]),
        Paragraph("Оперативная сводка", styles["subsection"]),
        Paragraph(_safe(case.get("executive_summary"), "Оперативная сводка не подготовлена."), styles["body"]),
        Paragraph("Установленные факты и итоговый анализ", styles["subsection"]),
        Paragraph(_safe(case.get("findings"), "Итоговый анализ не подготовлен."), styles["body"]),
        Paragraph("Связи с семьёй Nowa", styles["subsection"]),
        Paragraph(_safe(case.get("nowa_links"), "Сведения отсутствуют."), styles["body"]),
        PageBreak(),
        Paragraph("ДОКАЗАТЕЛЬНАЯ БАЗА", styles["eyebrow"]),
        Paragraph("Материалы расследования", styles["title"]),
        *_material_story(materials, styles),
    ]

    if relations:
        story.extend(
            [
                PageBreak(),
                Paragraph("КАРТА ОКРУЖЕНИЯ", styles["eyebrow"]),
                Paragraph("Установленные связи", styles["title"]),
                _data_table(
                    [["ЧЕЛОВЕК / ОБЪЕКТ", "ХАРАКТЕР СВЯЗИ", "ИДЕНТИФИКАТОРЫ", "ПОДТВЕРЖДЕНИЕ / ДЕТАЛИ"]] + [
                        [
                            item.get("person_name"),
                            item.get("relation_type"),
                            " / ".join(filter(None, [
                                f"Статик {item.get('static_id')}" if item.get("static_id") else "",
                                str(item.get("discord_text") or ""),
                            ])) or "Не указаны",
                            f"{_RELIABILITY_LABELS.get(str(item.get('confidence')), item.get('confidence') or 'Не оценено')}\n{item.get('details') or ''}",
                        ]
                        for item in relations
                    ],
                    [39 * mm, 32 * mm, 39 * mm, 56 * mm],
                    styles,
                ),
            ]
        )

    story.extend(
        [
            Paragraph("ОПЕРАТИВНЫЙ ПЛАН", styles["section"]),
            Paragraph("Задачи расследования", styles["title"]),
        ]
    )
    if tasks:
        story.append(
            _data_table(
                [["ЗАДАЧА", "СТАТУС", "ОТВЕТСТВЕННЫЙ", "СРОК / ПРИОРИТЕТ"]] + [
                    [
                        f"{item.get('title') or 'Без названия'}\n{item.get('description') or ''}",
                        _TASK_STATUS_LABELS.get(str(item.get("status")), item.get("status") or "Не указан"),
                        item.get("assignee_display") or "Не назначен",
                        f"{_moment(item.get('due_at'))}\n{_PRIORITY_LABELS.get(str(item.get('priority')), item.get('priority') or 'Обычный')}",
                    ]
                    for item in tasks
                ],
                [70 * mm, 28 * mm, 35 * mm, 33 * mm],
                styles,
            )
        )
    else:
        story.append(Paragraph("Оперативные задачи не поставлены.", styles["body"]))

    story.extend(
        [
            PageBreak(),
            Paragraph("ХОД РАССЛЕДОВАНИЯ", styles["eyebrow"]),
            Paragraph("Неизменяемая хронология", styles["title"]),
        ]
    )
    if events:
        story.append(
            _data_table(
                [["ДАТА", "СОБЫТИЕ", "АВТОР", "СЛУЖЕБНАЯ ЗАПИСЬ"]] + [
                    [
                        _moment(item.get("created_at")),
                        _EVENT_LABELS.get(str(item.get("action")), item.get("action") or "Событие"),
                        item.get("actor_display") or "Система",
                        item.get("note") or "-",
                    ]
                    for item in events
                ],
                [32 * mm, 38 * mm, 32 * mm, 64 * mm],
                styles,
            )
        )
    else:
        story.append(Paragraph("События расследования отсутствуют.", styles["body"]))

    decision_status = str(case.get("status") or "")
    if decision_status in {"approved", "denied", "archived"} or case.get("decision_reason"):
        decision_color = _MINT if str(case.get("decision")) == "approved" else _RED
        decision = Table(
            [[
                Paragraph(
                    _safe(_STATUS_LABELS.get(str(case.get("decision")), case.get("decision") or "Итоговое решение")),
                    ParagraphStyle(
                        "OVRDecision", parent=styles["subsection"], textColor=decision_color,
                        fontSize=14, leading=18,
                    ),
                ),
                Paragraph(_safe(case.get("decision_reason"), "Основание не указано."), styles["body"]),
            ]],
            colWidths=[48 * mm, 118 * mm],
        )
        decision.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), _SURFACE),
                    ("BOX", (0, 0), (-1, -1), 0.7, decision_color),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * mm),
                ]
            )
        )
        story.extend([Paragraph("ИТОГОВОЕ РЕШЕНИЕ", styles["section"]), decision])

    story.extend(
        [
            Spacer(1, 9 * mm),
            Paragraph(
                _safe(
                    f"Отчёт сформирован {generated_at.strftime('%d.%m.%Y / %H:%M UTC')}. "
                    f"Версия карточки: {int(case.get('revision') or 0)}. "
                    "Документ отражает состояние досье на момент формирования."
                ),
                styles["body_small"],
            ),
        ]
    )

    def decorate_cover(canvas: Any, _: Any) -> None:
        width, height = A4
        canvas.saveState()
        canvas.setFillColor(_PAPER)
        canvas.rect(0, 0, width, height, fill=1, stroke=0)
        canvas.setStrokeColor(colors.HexColor("#1F3830"))
        canvas.setLineWidth(0.45)
        for radius in (42 * mm, 64 * mm, 88 * mm, 115 * mm):
            canvas.circle(width + 8 * mm, height - 13 * mm, radius, fill=0, stroke=1)
        canvas.setFillColor(colors.HexColor("#0B2B20"))
        canvas.circle(width - 20 * mm, height - 25 * mm, 6 * mm, fill=1, stroke=0)
        canvas.setFillColor(_MINT)
        canvas.circle(width - 20 * mm, height - 25 * mm, 1.4 * mm, fill=1, stroke=0)
        canvas.setStrokeColor(_MINT)
        canvas.setLineWidth(1.15)
        canvas.line(22 * mm, height - 17 * mm, 49 * mm, height - 17 * mm)
        canvas.setFont(regular, 7)
        canvas.setFillColor(_DIM)
        canvas.drawString(22 * mm, 10 * mm, "Закрытый оперативно-аналитический контур")
        canvas.drawRightString(width - 22 * mm, 10 * mm, "T-Mod / OVR")
        canvas.restoreState()

    def decorate_page(canvas: Any, doc: Any) -> None:
        width, height = A4
        canvas.saveState()
        canvas.setFillColor(_PAPER)
        canvas.rect(0, 0, width, height, fill=1, stroke=0)
        canvas.setStrokeColor(colors.HexColor("#16352A"))
        canvas.setLineWidth(0.35)
        for radius in (25 * mm, 39 * mm, 54 * mm):
            canvas.circle(width + 5 * mm, height + 3 * mm, radius, fill=0, stroke=1)
        canvas.setStrokeColor(_MINT)
        canvas.setLineWidth(1)
        canvas.line(13 * mm, 18 * mm, 13 * mm, height - 18 * mm)
        canvas.setStrokeColor(_LINE)
        canvas.setLineWidth(0.35)
        canvas.line(22 * mm, height - 14 * mm, width - 22 * mm, height - 14 * mm)
        canvas.setFont(bold, 6.5)
        canvas.setFillColor(_MINT)
        canvas.drawString(22 * mm, height - 10.5 * mm, f"T-MOD / OVR / {_case_number(case)}")
        canvas.setFont(regular, 6.5)
        canvas.setFillColor(_DIM)
        canvas.drawString(22 * mm, 9.5 * mm, _CLASSIFICATION_LABELS.get(str(case.get("classification")), "ОГРАНИЧЕНО"))
        canvas.drawRightString(width - 22 * mm, 9.5 * mm, f"{int(doc.page):02d}")
        canvas.restoreState()

    document.build(story, onFirstPage=decorate_cover, onLaterPages=decorate_page)
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise OSError("ovr_report_generation_failed")
    return destination


def encrypt_investigation_report(
    source: Path,
    destination: Path,
    password: str,
) -> Path:
    clean_password = str(password or "")
    if len(clean_password) < 8 or len(clean_password) > 128:
        raise ValueError("ovr_report_password_invalid")
    source = Path(source)
    destination = Path(destination)
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("ovr_report_source_missing")
    destination.parent.mkdir(parents=True, exist_ok=True)

    reader = PdfReader(str(source))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer.encrypt(
        user_password=clean_password,
        owner_password=secrets.token_urlsafe(48),
        permissions_flag=UserAccessPermissions(0),
        algorithm="AES-256-R5",
    )
    with destination.open("wb") as stream:
        writer.write(stream)

    verification = PdfReader(str(destination))
    if not verification.is_encrypted or verification.decrypt(clean_password) == 0:
        destination.unlink(missing_ok=True)
        raise OSError("ovr_report_encryption_failed")
    return destination


__all__ = [
    "encrypt_investigation_report",
    "generate_investigation_report",
]
