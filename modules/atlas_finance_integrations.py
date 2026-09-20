"""Bounded, read-only finance integrations for the Atlas administration plane."""

from __future__ import annotations

import os
from typing import Any

import aiohttp

async def openrouter_credit_snapshot() -> dict[str, Any]:
    key = os.getenv("OPENROUTER_MANAGEMENT_API_KEY", "").strip()
    if not key:
        return {
            "configured": False,
            "status": "not_configured",
            "message": "Добавьте отдельный OpenRouter Management API Key.",
        }
    timeout = aiohttp.ClientTimeout(total=12, connect=4)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://openrouter.ai/api/v1/credits",
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            ) as response:
                payload = await response.json(content_type=None)
                if response.status != 200:
                    return {
                        "configured": True,
                        "status": "error",
                        "http_status": response.status,
                        "message": str(payload.get("error", {}).get("message") or "OpenRouter недоступен")[:240],
                    }
    except (aiohttp.ClientError, TimeoutError) as exc:
        return {"configured": True, "status": "unavailable", "message": type(exc).__name__}
    data = dict(payload.get("data") or {})
    total = float(data.get("total_credits") or 0)
    used = float(data.get("total_usage") or 0)
    return {
        "configured": True,
        "status": "ok",
        "total_credits_usd": round(total, 6),
        "total_usage_usd": round(used, 6),
        "remaining_credits_usd": round(max(0.0, total - used), 6),
    }

__all__ = ["openrouter_credit_snapshot"]
