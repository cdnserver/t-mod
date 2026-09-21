"""Database-only projection for the member consensus preparation queue.

The legacy Discord web runtime and the standalone API deliberately share this
builder while the route is migrated.  Keeping one projection prevents the two
backends from drifting during shadow comparison and automatic fallback.
"""

from __future__ import annotations

from typing import Any

from persistence import consensus_preparation_repository as preparation_storage
from persistence import tvrs_repository as tvrs_storage


def build_reactor_preparation_payload(
    guild_id: int,
    user_id: int,
    *,
    limit: int = 24,
) -> dict[str, Any]:
    """Return upcoming bills with only the requesting member's safe markers."""

    bills = tvrs_storage.tvrs_queue_bills(int(guild_id), max(1, min(int(limit), 100)))
    bill_ids = [int(bill.get("id") or 0) for bill in bills]
    statuses = preparation_storage.preparation_statuses(
        int(guild_id),
        int(user_id),
        bill_ids,
    )
    items: list[dict[str, Any]] = []
    for bill in bills:
        bill_id = int(bill.get("id") or 0)
        if bill_id <= 0:
            continue
        marker = statuses.get(bill_id) or {}
        items.append(
            {
                "id": bill_id,
                "number": int(bill.get("bill_number") or 0),
                "title": str(bill.get("title") or "Без названия"),
                "summary": str(bill.get("summary") or ""),
                "author": str(bill.get("author_display") or "Автор не указан"),
                "status": str(bill.get("status") or "queued"),
                "updated_at": str(bill.get("updated_at") or "") or None,
                "preparation": {
                    "prepared": bool(marker.get("prepared")),
                    "preliminary_vote": marker.get("preliminary_vote"),
                    "updated_at": marker.get("updated_at"),
                },
            }
        )
    prepared = sum(
        1
        for item in items
        if bool((item.get("preparation") or {}).get("prepared"))
    )
    return {
        "items": items,
        "total": len(items),
        "prepared": prepared,
        "notice": (
            "Листы подготовки личные: заметки и предварительная позиция "
            "не являются голосом и никому не видны."
        ),
    }


__all__ = ["build_reactor_preparation_payload"]
