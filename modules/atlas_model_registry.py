"""Provider-neutral routing for Atlas model releases.

The registry deliberately contains no training or upload code.  It decides
which *already deployed* model may answer a request, and keeps the normal
OpenRouter route available as a bounded fallback.  That lets a fine-tuned
release be enabled for one project and one Atlas agent at a time instead of
silently changing answers for the entire platform.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


TOGETHER_CHAT_COMPLETIONS_URL = "https://api.together.ai/v1/chat/completions"


def _clean_set(value: str | None) -> frozenset[str]:
    return frozenset(
        item.strip().lower()
        for item in str(value or "").split(",")
        if item.strip()
    )


def _enabled(value: str | bool | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _chat_endpoint(value: str | None, *, fallback: str) -> str:
    endpoint = str(value or "").strip().rstrip("/")
    if not endpoint:
        return fallback
    if endpoint.endswith("/chat/completions"):
        return endpoint
    return endpoint + "/chat/completions"


def _rollout_entry(
    value: str | None,
    *,
    agent_id: str,
    project_code: str,
) -> dict[str, Any] | None:
    """Find the most specific enabled release in a safe JSON rollout list.

    The list is configuration, not user input. Invalid entries are ignored so
    a typo can never take down the base Atlas route. Exact project/agent
    matches outrank ``*``; ties keep their file order, making rollouts
    deterministic and reviewable.
    """

    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, list):
        return None
    selected: tuple[int, int, dict[str, Any]] | None = None
    clean_agent = str(agent_id or "").strip().lower()
    clean_project = str(project_code or "").strip().lower()
    for index, item in enumerate(decoded):
        if not isinstance(item, dict) or not _enabled(item.get("enabled", True)):
            continue
        item_agent = str(item.get("agent_id") or "*").strip().lower()
        item_project = str(item.get("project_code") or "*").strip().lower()
        if item_agent not in {"*", clean_agent} or item_project not in {"*", clean_project}:
            continue
        if str(item.get("provider") or "together").strip().lower() != "together":
            continue
        model = str(item.get("model") or "").strip()
        if not model:
            continue
        specificity = int(item_agent == clean_agent) + int(item_project == clean_project)
        candidate = (specificity, -index, dict(item))
        if selected is None or candidate[:2] > selected[:2]:
            selected = candidate
    return selected[2] if selected is not None else None


@dataclass(frozen=True, slots=True)
class AtlasModelRoute:
    """A provider endpoint selected for one answer.

    The key is intentionally never included in :meth:`public`; result rows
    keep enough provenance for evaluation and incident review without exposing
    a credential into threads, logs, or exported datasets.
    """

    provider: str
    model: str
    endpoint: str
    api_key: str
    referer: str = ""
    title: str = ""
    release: str = "base"

    @property
    def configured(self) -> bool:
        return bool(self.model and self.endpoint and self.api_key)

    def headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.provider == "openrouter":
            if self.referer:
                headers["HTTP-Referer"] = self.referer
            if self.title:
                headers["X-OpenRouter-Title"] = self.title
        return headers

    def public(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "release": self.release,
        }


@dataclass(frozen=True, slots=True)
class AtlasModelSelection:
    """Primary route plus a safe base-model fallback when explicitly enabled."""

    primary: AtlasModelRoute
    fallback: AtlasModelRoute | None
    reason: str


def select_atlas_model_route(
    *,
    openrouter_key: str,
    openrouter_url: str,
    openrouter_model: str,
    openrouter_referer: str,
    openrouter_title: str,
    together_key: str = "",
    together_url: str = TOGETHER_CHAT_COMPLETIONS_URL,
    fine_tuned_model: str = "",
    fine_tuned_enabled: str | bool = False,
    fine_tuned_provider: str = "together",
    fine_tuned_agents: str = "atlas-tvr-a",
    fine_tuned_projects: str = "",
    fine_tuned_fallback: str | bool = True,
    fine_tuned_rollouts: str = "",
    agent_id: str = "atlas-tvr-a",
    project_code: str = "",
    direct_mode: bool = False,
    latency_mode: str = "standard",
    special_model: str = "",
) -> AtlasModelSelection:
    """Select a route without making a network call.

    ``special_model`` is used for Atlas 2 and Overlay.  These deliberately
    stay on the base provider: a fine-tuned legal assistant must never replace
    the opt-in direct-writing mode or latency-sensitive in-game overlay.
    """

    base = AtlasModelRoute(
        provider="openrouter",
        model=str(special_model or openrouter_model).strip(),
        # OPENROUTER_API_URL has always meant the complete endpoint in T-Mod;
        # preserve custom gateways and local OpenAI-compatible test servers
        # verbatim instead of appending a second `/chat/completions` suffix.
        endpoint=str(openrouter_url or "").strip().rstrip("/"),
        api_key=str(openrouter_key or "").strip(),
        referer=str(openrouter_referer or "").strip(),
        title=str(openrouter_title or "").strip(),
        release="base",
    )
    if direct_mode or str(latency_mode or "").strip().lower() == "overlay" or special_model:
        return AtlasModelSelection(base, None, "special_route")
    if not _enabled(fine_tuned_enabled):
        return AtlasModelSelection(base, None, "fine_tuning_disabled")
    rollout = _rollout_entry(
        fine_tuned_rollouts,
        agent_id=agent_id,
        project_code=project_code,
    )
    rollout_provider = str((rollout or {}).get("provider") or fine_tuned_provider or "together")
    if rollout_provider.strip().lower() != "together":
        return AtlasModelSelection(base, None, "fine_tuning_provider_unsupported")
    if rollout is not None:
        tuned = AtlasModelRoute(
            provider="together",
            model=str(rollout.get("model") or "").strip(),
            endpoint=_chat_endpoint(
                str(rollout.get("endpoint") or together_url),
                fallback=TOGETHER_CHAT_COMPLETIONS_URL,
            ),
            api_key=str(together_key or "").strip(),
            release=str(rollout.get("release") or "fine-tuned").strip()[:120] or "fine-tuned",
        )
        if not tuned.configured:
            return AtlasModelSelection(base, None, "fine_tuning_not_configured")
        fallback_enabled = rollout.get("fallback", fine_tuned_fallback)
        return AtlasModelSelection(
            tuned,
            base if _enabled(fallback_enabled) and base.configured else None,
            "fine_tuned_rollout",
        )
    allowed_agents = _clean_set(fine_tuned_agents) or frozenset({"atlas-tvr-a"})
    if str(agent_id or "").strip().lower() not in allowed_agents:
        return AtlasModelSelection(base, None, "agent_not_enrolled")
    allowed_projects = _clean_set(fine_tuned_projects)
    if allowed_projects and str(project_code or "").strip().lower() not in allowed_projects:
        return AtlasModelSelection(base, None, "project_not_enrolled")
    tuned = AtlasModelRoute(
        provider="together",
        model=str(fine_tuned_model or "").strip(),
        endpoint=_chat_endpoint(together_url, fallback=TOGETHER_CHAT_COMPLETIONS_URL),
        api_key=str(together_key or "").strip(),
        release="fine-tuned",
    )
    if not tuned.configured:
        return AtlasModelSelection(base, None, "fine_tuning_not_configured")
    return AtlasModelSelection(
        tuned,
        base if _enabled(fine_tuned_fallback) and base.configured else None,
        "fine_tuned_rollout",
    )


def route_from_mapping(settings: Mapping[str, object], **request: object) -> AtlasModelSelection:
    """Small adapter for configuration pages and tests.

    It remains intentionally boring: only approved settings are accepted and
    provider credentials never cross the result boundary.
    """

    return select_atlas_model_route(
        openrouter_key=str(settings.get("openrouter_key") or ""),
        openrouter_url=str(settings.get("openrouter_url") or ""),
        openrouter_model=str(settings.get("openrouter_model") or ""),
        openrouter_referer=str(settings.get("openrouter_referer") or ""),
        openrouter_title=str(settings.get("openrouter_title") or ""),
        together_key=str(settings.get("together_key") or ""),
        together_url=str(settings.get("together_url") or TOGETHER_CHAT_COMPLETIONS_URL),
        fine_tuned_model=str(settings.get("fine_tuned_model") or ""),
        fine_tuned_enabled=settings.get("fine_tuned_enabled", False),
        fine_tuned_provider=str(settings.get("fine_tuned_provider") or "together"),
        fine_tuned_agents=str(settings.get("fine_tuned_agents") or "atlas-tvr-a"),
        fine_tuned_projects=str(settings.get("fine_tuned_projects") or ""),
        fine_tuned_fallback=settings.get("fine_tuned_fallback", True),
        fine_tuned_rollouts=str(settings.get("fine_tuned_rollouts") or ""),
        agent_id=str(request.get("agent_id") or "atlas-tvr-a"),
        project_code=str(request.get("project_code") or ""),
        direct_mode=bool(request.get("direct_mode", False)),
        latency_mode=str(request.get("latency_mode") or "standard"),
        special_model=str(request.get("special_model") or ""),
    )


__all__ = [
    "AtlasModelRoute",
    "AtlasModelSelection",
    "TOGETHER_CHAT_COMPLETIONS_URL",
    "route_from_mapping",
    "select_atlas_model_route",
]
