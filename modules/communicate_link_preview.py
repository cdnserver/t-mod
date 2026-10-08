"""Small, permission-aware previews of internal documents; never fetch URLs."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from persistence.core import connect_readonly


def _identifier(value: str | None) -> int | None:
    if not value or not re.fullmatch(r"[0-9]{1,18}", value):
        return None
    result = int(value)
    return result if result > 0 else None


def resource_reference(raw: str) -> tuple[str, int] | None:
    if not isinstance(raw, str) or len(raw) > 2048:
        return None
    try:
        url = urlsplit(raw)
        if url.scheme != "https" or url.username or url.password or url.port not in (None, 443):
            return None
        query = parse_qs(url.query)
        host = (url.hostname or "").lower()
        if host == "ovr.tvr.lat" or (host == "reactor.tvr.lat" and url.path.startswith("/ovr")):
            match = re.search(r"/(?:case|cases)/([0-9]+)(?:/|$)", url.path)
            identifier = _identifier(match[1] if match else query.get("case_id", [None])[0])
            return ("ovr", identifier) if identifier else None
        if host in {"consensus.tvr.lat", "reactor.tvr.lat", "home.tvr.lat"}:
            match = re.search(r"/(?:bill|bills|initiative|initiatives)/([0-9]+)(?:/|$)", url.path)
            identifier = _identifier(match[1] if match else query.get("bill_id", [None])[0])
            if identifier:
                return "bill", identifier
            number = _identifier(query.get("bill_number", [None])[0])
            if number:
                return "bill_number", number
    except (ValueError, TypeError):
        pass
    return None


def _excerpt(value: Any, limit: int = 220) -> str:
    text = str(value or "")
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[`*_#~|]+", "", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    shortened = text[:limit].rsplit(" ", 1)[0]
    return (shortened or text[:limit]) + "…"


def document_preview(guild_id: int, user_id: int, raw_url: str, *, can_read_ovr: bool = False,
                     can_moderate_bills: bool = False) -> dict[str, str] | None:
    reference = resource_reference(raw_url)
    if reference is None:
        return None
    kind, identifier = reference
    with connect_readonly() as con:
        if kind == "ovr":
            # Do not query restricted metadata at all before checking access.
            if not can_read_ovr:
                return None
            row = con.execute(
                "SELECT case_number,status,objective FROM ovr_cases WHERE guild_id=? AND id=?",
                (guild_id, identifier),
            ).fetchone()
            if row is None:
                return None
            status = {
                "new": "Новое дело", "screening": "Проверка", "needs_info": "Ожидает сведений",
                "analysis": "Анализ", "decision": "Подготовка решения", "approved": "Одобрено",
                "denied": "Отклонено", "archived": "Архив",
            }.get(str(row["status"]), "")
            return {"title": f"Расследование №{row['case_number']}", "resource": "Дело ОВР", "status": status,
                    "detail": _excerpt(row["objective"]) or "Материалы расследования и ход рассмотрения дела."}
        # Drafts are visible only to their author/moderator. A document becomes
        # shareable to other accounts only after actual Discord publication.
        field = "bill_number" if kind == "bill_number" else "id"
        row = con.execute(
            f"""SELECT bill_number,title,summary,status,message_id FROM tvrs_bills
                WHERE guild_id=? AND {field}=?
                  AND ((message_id IS NOT NULL AND message_id>0) OR author_id=? OR ?=1)""",
            (guild_id, identifier, user_id, 1 if can_moderate_bills else 0),
        ).fetchone()
        if row is None:
            return None
        status = {
            "publishing": "Публикуется", "draft": "В повестке" if row["message_id"] else "Черновик", "queued": "В очереди",
            "requeued": "Повторное рассмотрение", "voting": "Идёт голосование",
            "pending_veto": "Ожидает завершения", "accepted": "Принято", "rejected": "Отклонено",
            "vetoed": "Вето", "cancelled": "Отменено",
        }.get(str(row["status"]), "")
        return {"title": _excerpt(row["title"], 150), "resource": f"Инициатива №{row['bill_number']}",
                "detail": _excerpt(row["summary"]), "status": status}
