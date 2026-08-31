"""Private web routes for a senator's consensus preparation sheet."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from modules.consensus_preparation_artifacts import generate_preparation_pdf
from modules.consensus_web_auth import ConsensusWebPrincipal, csrf_matches
from persistence import consensus_preparation_repository as preparation_storage
from persistence import tvrs_repository as tvrs_storage


AuthenticateRequest = Callable[
    [web.Request],
    Awaitable[tuple[ConsensusWebPrincipal | None, bool]],
]


_VOTE_LABELS = {
    "yes": "Поддержать",
    "no": "Не поддерживать",
    "abstain": "Воздержаться",
}


def _personal_login_required() -> web.Response:
    return web.json_response(
        {
            "error": "personal_login_required",
            "message": "Лист подготовки доступен только из персональной ссылки Discord.",
        },
        status=403,
    )


def _invalid_payload(message: str = "Проверьте заполнение листа подготовки.") -> web.Response:
    return web.json_response(
        {"error": "invalid_preparation_payload", "message": message},
        status=400,
    )


def _bill_payload(bill: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(bill.get("id") or 0),
        "bill_number": int(bill.get("bill_number") or 0),
        "title": str(bill.get("title") or "Без названия"),
        "summary": str(bill.get("summary") or ""),
        "materials": str(bill.get("materials") or ""),
        "decision_category": str(bill.get("decision_category") or "ordinary"),
        "status": str(bill.get("status") or ""),
        "created_at": str(bill.get("created_at") or "") or None,
        "updated_at": str(bill.get("updated_at") or "") or None,
        "author": {
            "id": int(bill.get("author_id") or 0) or None,
            "name": str(bill.get("author_display") or "").strip()
            or "Автор не указан",
        },
        "source_url": (
            f"https://discord.com/channels/{int(bill.get('guild_id') or 0)}/"
            f"{int(bill.get('channel_id') or 0)}/{int(bill.get('message_id') or 0)}"
            if bill.get("channel_id") and bill.get("message_id")
            else None
        ),
    }


def _source_changed(sheet: dict[str, Any], bill: dict[str, Any]) -> bool:
    saved_source = str(sheet.get("source_bill_updated_at") or "")
    current_source = str(bill.get("updated_at") or "")
    return bool(saved_source and current_source and saved_source != current_source)


def _safe_filename_component(value: Any) -> str:
    chars = [char for char in str(value or "") if char.isascii() and char.isalnum()]
    return "".join(chars)[:48] or "bill"


def register_consensus_preparation_routes(
    app: web.Application,
    *,
    guild_id: int,
    authenticate: AuthenticateRequest,
) -> None:
    """Register personal preparation routes before the generic bill detail route."""

    async def get_personal_principal(
        request: web.Request,
    ) -> ConsensusWebPrincipal | None:
        principal, legacy_read_only = await authenticate(request)
        if legacy_read_only or principal is None:
            return None
        return principal

    async def get_bill(request: web.Request) -> dict[str, Any] | None:
        try:
            bill_id = int(request.match_info.get("bill_id") or 0)
        except (TypeError, ValueError):
            return None
        if bill_id <= 0:
            return None
        bill = await asyncio.to_thread(tvrs_storage.tvrs_get_bill_dict_by_id, bill_id)
        if bill is None or int(bill.get("guild_id") or 0) != int(guild_id):
            return None
        return bill

    async def get_preparation(request: web.Request) -> web.Response:
        principal = await get_personal_principal(request)
        if principal is None:
            return _personal_login_required()
        bill = await get_bill(request)
        if bill is None:
            raise web.HTTPNotFound()
        sheet = await asyncio.to_thread(
            preparation_storage.get_preparation_sheet,
            int(guild_id),
            int(bill["id"]),
            int(principal.user_id),
        )
        return web.json_response(
            {
                "bill": _bill_payload(bill),
                "sheet": sheet,
                "source_changed": _source_changed(sheet, bill),
                "notice": "Личный лист. Предварительная позиция не является голосом и никому не видна.",
            },
            dumps=lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )

    async def save_preparation(request: web.Request) -> web.Response:
        principal = await get_personal_principal(request)
        if principal is None:
            return _personal_login_required()
        if not csrf_matches(request, principal):
            return web.json_response(
                {
                    "error": "csrf_failed",
                    "message": "Обновите панель и повторите сохранение.",
                },
                status=403,
            )
        if request.content_length is not None and request.content_length > 96_000:
            return _invalid_payload("Лист подготовки слишком большой.")
        bill = await get_bill(request)
        if bill is None:
            raise web.HTTPNotFound()
        try:
            payload = await request.json()
        except (json.JSONDecodeError, TypeError):
            return _invalid_payload("Не удалось прочитать изменения листа.")
        if not isinstance(payload, dict):
            return _invalid_payload()
        try:
            sheet = await asyncio.to_thread(
                preparation_storage.save_preparation_sheet,
                int(guild_id),
                int(bill["id"]),
                int(principal.user_id),
                user_display=principal.display_name,
                expected_revision=payload.get("expected_revision"),
                questions=payload.get("questions"),
                notes=payload.get("notes"),
                preliminary_vote=payload.get("preliminary_vote"),
                preliminary_vote_reason=payload.get("preliminary_vote_reason"),
                review_flags=payload.get("review_flags"),
                source_bill_updated_at=str(bill.get("updated_at") or "") or None,
            )
        except preparation_storage.PreparationRevisionConflict:
            return web.json_response(
                {
                    "error": "preparation_revision_conflict",
                    "message": (
                        "Лист был изменён в другой вкладке. Обновите его, чтобы не "
                        "перезаписать новые заметки."
                    ),
                },
                status=409,
            )
        except ValueError as exc:
            return _invalid_payload(
                "Проверьте длину заметок, вопросов и предварительной позиции."
            )
        return web.json_response(
            {
                "ok": True,
                "sheet": sheet,
                "source_changed": False,
                "draft_vote_label": _VOTE_LABELS.get(
                    str(sheet.get("preliminary_vote") or ""),
                    "Позиция формируется",
                ),
            },
            dumps=lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )

    async def preparation_pdf(request: web.Request) -> web.Response:
        principal = await get_personal_principal(request)
        if principal is None:
            return _personal_login_required()
        bill = await get_bill(request)
        if bill is None:
            raise web.HTTPNotFound()
        sheet = await asyncio.to_thread(
            preparation_storage.get_preparation_sheet,
            int(guild_id),
            int(bill["id"]),
            int(principal.user_id),
        )
        try:
            payload = await asyncio.to_thread(
                generate_preparation_pdf,
                bill,
                sheet,
                principal.display_name,
            )
        except Exception as exc:  # pragma: no cover - operational guard for downloads
            raise web.HTTPServiceUnavailable(
                text="Лист подготовки временно не удалось собрать."
            ) from exc
        bill_number = int(bill.get("bill_number") or 0)
        file_name = f"consensus-preparation-{bill_number:03d}-{_safe_filename_component(principal.user_id)}.pdf"
        response = web.Response(body=payload, content_type="application/pdf")
        response.headers["Content-Disposition"] = f'attachment; filename="{file_name}"'
        response.headers["Cache-Control"] = "private, no-store"
        return response

    app.router.add_get("/api/bills/{bill_id}/preparation", get_preparation)
    app.router.add_post("/api/bills/{bill_id}/preparation", save_preparation)
    app.router.add_get("/api/bills/{bill_id}/preparation.pdf", preparation_pdf)


__all__ = ["register_consensus_preparation_routes"]
