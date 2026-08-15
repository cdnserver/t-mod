"""Member-facing legislation workspace used by the Reactor web surface.

The Discord editor and the web editor share the same durable workspace and
publication outbox.  This module deliberately owns validation/projection so
the browser never receives Discord-only identifiers or mutable database rows.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from modules.bill_editor_ai import BILL_EDITOR_MODEL, generate_bill_editor_draft
from modules.consensus_runtime import active_sessions, session_lock
from modules.delivery_runtime import wake_delivery_worker
from modules.tvrs_bill_editor import refresh_bill_workspace_panel
from modules.tvrs_config import TVRS_MATERIALS_CHANNEL_ID
from modules.tvrs_delivery import TVRS_BILL_PUBLICATION_TOPIC
from persistence import bill_workspace_repository as workspace_storage
from persistence import legislation_repository as legislation_storage
from persistence import profile_repository as profile_storage
from persistence import tvrs_repository as bill_storage


logger = logging.getLogger(__name__)


class ReactorLegislationError(RuntimeError):
    """A safe, user-facing editor failure."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = int(status)


_LIMITS = {
    "idea": 1800,
    "desired_outcome": 1000,
    "constraints_text": 1000,
    "title": 180,
    "summary": 3000,
    "materials": 1000,
    "implementation_plan": 1800,
    "leadership_actions": 1800,
}


def _text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if len(value) > _LIMITS[key]:
        raise ReactorLegislationError(
            "bill_field_too_long",
            f"Поле «{key}» длиннее допустимого.",
        )
    return value


def _workspace_id(payload: dict[str, Any]) -> int:
    try:
        workspace_id = int(payload.get("workspace_id") or 0)
    except (TypeError, ValueError) as exc:
        raise ReactorLegislationError(
            "bill_workspace_id_invalid",
            "Черновик не распознан. Обновите страницу и повторите действие.",
        ) from exc
    if workspace_id <= 0:
        raise ReactorLegislationError(
            "bill_workspace_id_invalid",
            "Черновик не распознан. Обновите страницу и повторите действие.",
        )
    return workspace_id


def _owned_workspace(
    guild_id: int,
    author_id: int,
    workspace_id: int,
) -> dict[str, Any]:
    workspace = workspace_storage.get_bill_workspace(int(workspace_id))
    if (
        workspace is None
        or int(workspace.get("guild_id") or 0) != int(guild_id)
        or int(workspace.get("author_id") or 0) != int(author_id)
    ):
        raise ReactorLegislationError(
            "bill_workspace_not_found",
            "Этот черновик не найден или принадлежит другому автору.",
            status=404,
        )
    if str(workspace.get("status") or "") not in {
        "draft",
        "review",
        "changes_requested",
    }:
        raise ReactorLegislationError(
            "bill_workspace_closed",
            "Черновик уже закрыт. Создайте новый законопроект.",
            status=409,
        )
    return workspace


def project_workspace(workspace: dict[str, Any] | None) -> dict[str, Any] | None:
    if workspace is None:
        return None
    required = {
        "idea": bool(str(workspace.get("idea") or "").strip()),
        "outcome": bool(str(workspace.get("desired_outcome") or "").strip()),
        "title": len(str(workspace.get("title") or "").strip()) >= 5,
        "text": len(str(workspace.get("summary") or "").strip()) >= 20,
        "implementation": len(
            str(workspace.get("implementation_plan") or "").strip()
        )
        >= 5,
        "leadership": len(
            str(workspace.get("leadership_actions") or "").strip()
        )
        >= 5,
    }
    return {
        "id": int(workspace["id"]),
        "status": str(workspace.get("status") or "draft"),
        "revision": int(workspace.get("revision") or 0),
        "ai_revision": int(workspace.get("ai_revision") or 0),
        "ai_model": str(workspace.get("ai_model") or "") or None,
        "idea": str(workspace.get("idea") or ""),
        "desired_outcome": str(workspace.get("desired_outcome") or ""),
        "constraints_text": str(workspace.get("constraints_text") or ""),
        "title": str(workspace.get("title") or ""),
        "summary": str(workspace.get("summary") or ""),
        "materials": str(workspace.get("materials") or ""),
        "decision_category": "ordinary",
        "implementation_plan": str(workspace.get("implementation_plan") or ""),
        "leadership_actions": str(workspace.get("leadership_actions") or ""),
        "execution_blocks": legislation_storage.parse_execution_blocks(
            workspace.get("execution_blocks_json")
        ),
        "moderation": {
            "status": str(workspace.get("moderation_status") or "draft"),
            "round": int(workspace.get("moderation_round") or 0),
            "note": str(workspace.get("moderation_note") or "") or None,
            "moderator": str(workspace.get("moderator_display") or "") or None,
            "submitted_at": workspace.get("submitted_at"),
            "reviewed_at": workspace.get("reviewed_at"),
        },
        "created_at": workspace.get("created_at"),
        "updated_at": workspace.get("updated_at"),
        "requirements": required,
        "progress": round(100 * sum(required.values()) / len(required)),
        "ready": all(required.values()),
    }


def project_bill(
    guild_id: int,
    bill: dict[str, Any],
    preferred_names: dict[int, str] | None = None,
) -> dict[str, Any]:
    channel_id = int(bill.get("channel_id") or 0)
    message_id = int(bill.get("message_id") or 0)
    author_id = int(bill.get("author_id") or 0)
    return {
        "id": int(bill["id"]),
        "number": int(bill.get("bill_number") or 0),
        "title": str(bill.get("title") or "Без названия"),
        "summary": str(bill.get("summary") or ""),
        "materials": str(bill.get("materials") or ""),
        "implementation_plan": str(bill.get("implementation_plan") or ""),
        "leadership_actions": str(bill.get("leadership_actions") or ""),
        "execution_blocks": legislation_storage.parse_execution_blocks(
            bill.get("execution_blocks_json")
        ),
        "moderation": {
            "moderator": str(bill.get("moderated_by_display") or "") or None,
            "moderated_at": bill.get("moderated_at"),
        },
        "author": str(
            (preferred_names or {}).get(author_id)
            or bill.get("author_display")
            or "Участник Товарищества"
        ),
        "author_id": author_id,
        "status": str(bill.get("status") or "draft"),
        "result_status": bill.get("result_status"),
        "overall_percent": bill.get("result_overall_percent"),
        "required_percent": bill.get("result_required_percent"),
        "created_at": bill.get("created_at"),
        "discord_url": (
            f"https://discord.com/channels/{int(guild_id)}/{channel_id}/{message_id}"
            if channel_id > 0 and message_id > 0
            else None
        ),
    }


def legislation_snapshot(
    guild_id: int,
    author_id: int,
    *,
    limit: int = 80,
    moderator: bool = False,
) -> dict[str, Any]:
    workspace = workspace_storage.get_open_bill_workspace(guild_id, author_id)
    bills = bill_storage.tvrs_public_bill_catalog(guild_id, limit=limit)
    preferred_names = profile_storage.profile_preferred_names(
        guild_id,
        [int(bill.get("author_id") or 0) for bill in bills],
    )
    queued = sum(
        1
        for bill in bills
        if str(bill.get("status") or "")
        in {"publishing", "draft", "queued", "requeued"}
    )
    author_workspaces = legislation_storage.list_author_workspaces(
        guild_id, author_id, limit=80
    )
    workspace_events = legislation_storage.moderation_events(
        [int(item["id"]) for item in author_workspaces]
    )
    moderation_queue = (
        legislation_storage.list_moderation_queue(guild_id) if moderator else []
    )
    moderation_queue_events = legislation_storage.moderation_events(
        [int(item["id"]) for item in moderation_queue]
    )
    tasks = legislation_storage.task_board(guild_id)
    return {
        "workspace": project_workspace(workspace),
        "my_workspaces": [
            {
                **(project_workspace(item) or {}),
                "events": workspace_events.get(int(item["id"]), []),
            }
            for item in author_workspaces
        ],
        "bills": [project_bill(guild_id, bill, preferred_names) for bill in bills],
        "moderation": {
            "allowed": bool(moderator),
            "queue": [
                {
                    **(project_workspace(item) or {}),
                    "author": str(item.get("author_display") or "Участник"),
                    "author_id": int(item.get("author_id") or 0),
                    "events": moderation_queue_events.get(int(item["id"]), []),
                }
                for item in moderation_queue
            ],
        },
        "tasks": tasks,
        "next_number": bill_storage.tvrs_next_bill_number(guild_id),
        "queued": queued,
    }


def create_workspace(
    guild_id: int,
    author_id: int,
    author_display: str,
) -> tuple[dict[str, Any], bool]:
    workspace, created = workspace_storage.create_or_get_bill_workspace(
        guild_id=int(guild_id),
        author_id=int(author_id),
        author_display=str(author_display),
        parent_channel_id=TVRS_MATERIALS_CHANNEL_ID,
    )
    return project_workspace(workspace) or {}, created


def save_workspace(
    guild_id: int,
    author_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        workspace_id = _workspace_id(payload)
        expected_revision = int(payload.get("expected_revision"))
    except (TypeError, ValueError) as exc:
        raise ReactorLegislationError(
            "bill_workspace_revision_required",
            "Обновите страницу и повторите сохранение.",
            status=409,
        ) from exc
    _owned_workspace(guild_id, author_id, workspace_id)
    fields = {key: _text(payload, key) for key in _LIMITS}
    try:
        blocks_json = legislation_storage.execution_blocks_json(
            payload.get("execution_blocks")
        )
    except ValueError as exc:
        raise ReactorLegislationError(
            str(exc),
            "Проверьте цепочку исполнения: у каждого блока нужны тип и название.",
        ) from exc
    try:
        updated = workspace_storage.update_bill_workspace(
            workspace_id,
            expected_revision=expected_revision,
            decision_category="ordinary",
            status="review" if fields["title"] and fields["summary"] else "draft",
            execution_blocks_json=blocks_json,
            **fields,
        )
    except ValueError as exc:
        if str(exc) == "bill_workspace_revision_conflict":
            raise ReactorLegislationError(
                "bill_workspace_revision_conflict",
                "Черновик уже изменился в другой вкладке. Данные обновлены — проверьте их перед повтором.",
                status=409,
            ) from exc
        raise
    return project_workspace(updated) or {}


def generate_workspace_draft(
    guild_id: int,
    author_id: int,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    saved = save_workspace(guild_id, author_id, payload)
    if len(saved["idea"]) < 10 or len(saved["desired_outcome"]) < 5:
        raise ReactorLegislationError(
            "bill_editor_input_incomplete",
            "Опишите идею и желаемый результат чуть подробнее.",
        )
    draft = generate_bill_editor_draft(
        idea=saved["idea"],
        desired_outcome=saved["desired_outcome"],
        constraints_text=saved["constraints_text"],
        current_draft=saved,
    )
    updated = workspace_storage.update_bill_workspace(
        int(saved["id"]),
        expected_revision=int(saved["revision"]),
        title=draft.title,
        summary=draft.summary,
        materials=draft.materials or "",
        decision_category="ordinary",
        implementation_plan=draft.implementation_plan,
        leadership_actions=draft.leadership_actions,
        ai_model=BILL_EDITOR_MODEL,
        increment_ai_revision=True,
        status="review",
    )
    return project_workspace(updated) or {}, draft.clarification


async def publish_workspace(
    bot: discord.Client,
    guild_id: int,
    author_id: int,
    author_display: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    if payload.get("confirmed") is not True:
        raise ReactorLegislationError(
            "confirmation_required",
            "Подтвердите отправку законопроекта на модерацию.",
        )
    workspace_id = _workspace_id(payload)
    workspace = workspace_storage.get_bill_workspace(workspace_id)
    if (
        workspace is None
        or int(workspace.get("guild_id") or 0) != int(guild_id)
        or int(workspace.get("author_id") or 0) != int(author_id)
    ):
        raise ReactorLegislationError(
            "bill_workspace_not_found",
            "Этот черновик не найден или принадлежит другому автору.",
            status=404,
        )
    if str(workspace.get("status") or "") == "moderation":
        return project_workspace(workspace) or {}, False
    if str(workspace.get("status") or "") not in {
        "draft",
        "review",
        "changes_requested",
    }:
        raise ReactorLegislationError(
            "bill_workspace_closed",
            "Этот законопроект уже прошёл текущий этап.",
            status=409,
        )
    try:
        expected_revision = int(payload.get("expected_revision"))
    except (TypeError, ValueError) as exc:
        raise ReactorLegislationError(
            "bill_workspace_revision_required",
            "Обновите данные черновика перед отправкой.",
            status=409,
        ) from exc
    if int(workspace.get("revision") or 0) != expected_revision:
        raise ReactorLegislationError(
            "bill_workspace_revision_conflict",
            "Черновик изменился. Проверьте актуальную версию перед отправкой.",
            status=409,
        )
    projected = project_workspace(workspace) or {}
    if not projected.get("ready"):
        raise ReactorLegislationError(
            "bill_workspace_incomplete",
            "Заполните идею, текст и план исполнения перед отправкой.",
        )
    try:
        submitted = await asyncio.to_thread(
            legislation_storage.submit_for_moderation,
            int(workspace["id"]),
            guild_id=int(guild_id),
            author_id=int(author_id),
            author_display=str(author_display),
            expected_revision=expected_revision,
        )
    except ValueError as exc:
        code = str(exc)
        status = 409 if "revision" in code or "editable" in code else 400
        raise ReactorLegislationError(
            code,
            "Не удалось отправить актуальную версию. Обновите страницу и повторите.",
            status=status,
        ) from exc
    try:
        await refresh_bill_workspace_panel(bot, submitted)
    except Exception:  # noqa: BLE001 - web state is already durable
        logger.exception("Could not refresh legacy Discord workspace projection")
    return project_workspace(submitted) or {}, True


async def moderate_workspace(
    bot: discord.Client,
    guild_id: int,
    moderator_id: int,
    moderator_display: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Apply one idempotent moderation decision and publish only approved bills."""

    workspace_id = _workspace_id(payload)
    try:
        expected_revision = int(payload.get("expected_revision"))
    except (TypeError, ValueError) as exc:
        raise ReactorLegislationError(
            "bill_workspace_revision_required",
            "Карточка изменилась. Обновите очередь модерации.",
            status=409,
        ) from exc
    decision = str(payload.get("decision") or "").strip().lower()
    note = str(payload.get("note") or "").strip()
    workspace = workspace_storage.get_bill_workspace(workspace_id)
    if workspace is None or int(workspace.get("guild_id") or 0) != int(guild_id):
        raise ReactorLegislationError(
            "bill_workspace_not_found", "Законопроект не найден.", status=404
        )
    if decision in {"changes_requested", "rejected"}:
        try:
            updated = await asyncio.to_thread(
                legislation_storage.record_moderation_decision,
                workspace_id,
                guild_id=int(guild_id),
                moderator_id=int(moderator_id),
                moderator_display=str(moderator_display),
                expected_revision=expected_revision,
                decision=decision,
                note=note,
            )
        except ValueError as exc:
            raise ReactorLegislationError(
                str(exc), "Не удалось сохранить решение модерации.", status=409
            ) from exc
        return project_workspace(updated) or {}
    if decision != "approved":
        raise ReactorLegislationError(
            "bill_moderation_decision_invalid", "Выберите решение модерации."
        )
    projected = project_workspace(workspace) or {}
    if not projected.get("ready"):
        raise ReactorLegislationError(
            "bill_workspace_incomplete",
            "Нельзя одобрить неполный законопроект. Отправьте его на дополнение.",
        )
    async with session_lock(int(guild_id)):
        runtime = active_sessions.get(int(guild_id))
        if runtime is not None and not bool(getattr(runtime, "finished", False)):
            raise ReactorLegislationError(
                "bill_submission_locked_by_active_consensus",
                "Идёт консенсус. Одобрите проект после завершения заседания.",
                status=409,
            )
        from persistence.core import utc_now_iso

        moderated_at = utc_now_iso()
        try:
            bill, _, _ = await asyncio.to_thread(
                bill_storage.tvrs_create_bill_with_publication,
                guild_id=int(guild_id),
                channel_id=TVRS_MATERIALS_CHANNEL_ID,
                author_id=int(workspace.get("author_id") or 0),
                author_display=str(workspace.get("author_display") or "Участник"),
                title=projected["title"],
                summary=projected["summary"],
                materials=projected["materials"] or None,
                decision_category="ordinary",
                implementation_plan=projected["implementation_plan"],
                leadership_actions=projected["leadership_actions"],
                execution_blocks_json=legislation_storage.execution_blocks_json(
                    projected.get("execution_blocks")
                ),
                moderated_by_id=int(moderator_id),
                moderated_by_display=str(moderator_display),
                moderated_at=moderated_at,
                editor_workspace_id=workspace_id,
                delivery_topic=TVRS_BILL_PUBLICATION_TOPIC,
            )
            updated = await asyncio.to_thread(
                legislation_storage.record_moderation_decision,
                workspace_id,
                guild_id=int(guild_id),
                moderator_id=int(moderator_id),
                moderator_display=str(moderator_display),
                expected_revision=expected_revision,
                decision="approved",
                note=note,
                submitted_bill_id=int(bill.id),
            )
            await asyncio.to_thread(
                legislation_storage.ensure_execution_tasks,
                guild_id=int(guild_id),
                bill_id=int(bill.id),
                workspace_id=workspace_id,
                blocks=projected.get("execution_blocks"),
                actor_id=int(moderator_id),
                actor_display=str(moderator_display),
            )
        except ValueError as exc:
            raise ReactorLegislationError(
                str(exc), "Не удалось завершить одобрение законопроекта.", status=409
            ) from exc
    wake_delivery_worker()
    return project_workspace(updated) or {}


def cancel_workspace(
    guild_id: int,
    author_id: int,
    payload: dict[str, Any],
) -> None:
    if payload.get("confirmed") is not True:
        raise ReactorLegislationError(
            "confirmation_required",
            "Подтвердите отмену черновика.",
        )
    workspace_id = _workspace_id(payload)
    current = workspace_storage.get_bill_workspace(workspace_id)
    if (
        current is not None
        and int(current.get("guild_id") or 0) == int(guild_id)
        and int(current.get("author_id") or 0) == int(author_id)
        and str(current.get("status") or "") == "cancelled"
    ):
        return
    workspace = _owned_workspace(guild_id, author_id, workspace_id)
    workspace_storage.finish_bill_workspace(
        int(workspace["id"]),
        author_id=int(author_id),
        status="cancelled",
    )


__all__ = [
    "ReactorLegislationError",
    "cancel_workspace",
    "create_workspace",
    "generate_workspace_draft",
    "legislation_snapshot",
    "moderate_workspace",
    "project_workspace",
    "publish_workspace",
    "save_workspace",
]
