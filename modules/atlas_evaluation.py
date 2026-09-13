"""Offline, scope-aware quality gates for Atlas model releases.

This module never calls a model and never writes to production data.  It turns
human-curated JSONL cases and already captured Atlas answers into a deterministic
release report.  Semantic quality remains a human judgement; the gate catches
the non-negotiable regressions that can be checked automatically: tenant scope,
agent lane, source expectations, banned phrases and output bounds.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


EVALUATION_SCHEMA_VERSION = 1
_CODE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class AtlasEvaluationError(ValueError):
    """A malformed evaluation bundle must fail before release review."""


def _code(value: object, *, field: str) -> str:
    selected = str(value or "").strip().lower()
    if not _CODE_RE.fullmatch(selected):
        raise AtlasEvaluationError(f"atlas_eval_{field}_invalid")
    return selected


def _string_list(value: object, *, field: str, limit: int = 32) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise AtlasEvaluationError(f"atlas_eval_{field}_invalid")
    result: list[str] = []
    seen: set[str] = set()
    for item in value[:limit]:
        selected = " ".join(str(item or "").split())[:300]
        key = selected.casefold()
        if selected and key not in seen:
            result.append(selected)
            seen.add(key)
    return tuple(result)


def _integer_list(value: object, *, field: str, limit: int = 32) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise AtlasEvaluationError(f"atlas_eval_{field}_invalid")
    result: list[int] = []
    for item in value[:limit]:
        try:
            selected = int(item)
        except (TypeError, ValueError) as exc:
            raise AtlasEvaluationError(f"atlas_eval_{field}_invalid") from exc
        if selected > 0 and selected not in result:
            result.append(selected)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class AtlasEvaluationCase:
    case_id: str
    project_code: str
    agent_id: str
    server_code: str
    faction_code: str
    question: str
    required_terms: tuple[str, ...] = ()
    forbidden_terms: tuple[str, ...] = ()
    required_source_ids: tuple[int, ...] = ()
    min_citations: int = 0
    max_words: int = 0
    notes: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AtlasEvaluationCase":
        case_id = _code(value.get("case_id"), field="case_id")
        question = " ".join(str(value.get("question") or "").split())[:8_000]
        if len(question) < 2:
            raise AtlasEvaluationError("atlas_eval_question_required")
        try:
            min_citations = int(value.get("min_citations") or 0)
            max_words = int(value.get("max_words") or 0)
        except (TypeError, ValueError) as exc:
            raise AtlasEvaluationError("atlas_eval_limit_invalid") from exc
        if min_citations < 0 or min_citations > 32 or max_words < 0 or max_words > 10_000:
            raise AtlasEvaluationError("atlas_eval_limit_invalid")
        return cls(
            case_id=case_id,
            project_code=_code(value.get("project_code"), field="project_code"),
            agent_id=_code(value.get("agent_id"), field="agent_id"),
            server_code=_code(value.get("server_code"), field="server_code"),
            faction_code=_code(value.get("faction_code"), field="faction_code"),
            question=question,
            required_terms=_string_list(value.get("required_terms"), field="required_terms"),
            forbidden_terms=_string_list(value.get("forbidden_terms"), field="forbidden_terms"),
            required_source_ids=_integer_list(
                value.get("required_source_ids"), field="required_source_ids"
            ),
            min_citations=min_citations,
            max_words=max_words,
            notes=" ".join(str(value.get("notes") or "").split())[:1_000],
        )

    def public(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "project_code": self.project_code,
            "agent_id": self.agent_id,
            "server_code": self.server_code,
            "faction_code": self.faction_code,
            "question": self.question,
            "required_terms": list(self.required_terms),
            "forbidden_terms": list(self.forbidden_terms),
            "required_source_ids": list(self.required_source_ids),
            "min_citations": self.min_citations,
            "max_words": self.max_words,
            "notes": self.notes,
        }


def load_evaluation_cases(path: Path) -> list[AtlasEvaluationCase]:
    """Load a private JSONL suite and reject duplicate or mixed case IDs."""

    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise AtlasEvaluationError("atlas_eval_cases_unavailable") from exc
    cases: list[AtlasEvaluationCase] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AtlasEvaluationError(f"atlas_eval_case_json_invalid:{line_number}") from exc
        if not isinstance(decoded, dict):
            raise AtlasEvaluationError(f"atlas_eval_case_json_invalid:{line_number}")
        case = AtlasEvaluationCase.from_mapping(decoded)
        if case.case_id in seen:
            raise AtlasEvaluationError("atlas_eval_case_duplicate")
        cases.append(case)
        seen.add(case.case_id)
    if not cases:
        raise AtlasEvaluationError("atlas_eval_cases_empty")
    return cases


def _citation_ids(value: object) -> set[int]:
    result: set[int] = set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, Mapping):
            continue
        try:
            source_id = int(item.get("source_id") or 0)
        except (TypeError, ValueError):
            continue
        if source_id > 0:
            result.add(source_id)
    return result


def evaluate_atlas_result(
    case: AtlasEvaluationCase,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate only objective release checks for a captured Atlas answer."""

    answer = str(result.get("answer") or "").strip()
    lower_answer = answer.casefold()
    citation_ids = _citation_ids(result.get("citations"))
    actual_project = str(result.get("project_code") or "").strip().lower()
    actual_server = str(result.get("server_code") or "").strip().lower()
    actual_faction = str(result.get("faction_code") or "").strip().lower()
    agent_payload = result.get("agent")
    raw_agent = (
        agent_payload.get("id") if isinstance(agent_payload, Mapping) else result.get("agent_id")
    )
    actual_agent = str(raw_agent or "").strip().lower()
    required_found = [term for term in case.required_terms if term.casefold() in lower_answer]
    forbidden_found = [term for term in case.forbidden_terms if term.casefold() in lower_answer]
    required_sources_missing = sorted(set(case.required_source_ids) - citation_ids)
    checks = {
        "nonempty_answer": bool(answer),
        "model_provenance": bool(
            str(result.get("model") or "").strip()
            and str(result.get("model_provider") or "").strip()
            and str(result.get("model_release") or "").strip()
        ),
        "project_scope": actual_project == case.project_code,
        "server_scope": actual_server == case.server_code,
        "faction_scope": actual_faction == case.faction_code,
        "agent_scope": actual_agent == case.agent_id,
        "required_terms": len(required_found) == len(case.required_terms),
        "forbidden_terms": not forbidden_found,
        "citation_count": len(citation_ids) >= case.min_citations,
        "required_sources": not required_sources_missing,
        "word_limit": not case.max_words or len(answer.split()) <= case.max_words,
    }
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "case_id": case.case_id,
        "project_code": case.project_code,
        "server_code": case.server_code,
        "faction_code": case.faction_code,
        "agent_id": case.agent_id,
        "model": str(result.get("model") or ""),
        "model_provider": str(result.get("model_provider") or ""),
        "model_release": str(result.get("model_release") or ""),
        "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "answer_words": len(answer.split()),
        "citation_source_ids": sorted(citation_ids),
        "required_terms_found": required_found,
        "forbidden_terms_found": forbidden_found,
        "required_source_ids_missing": required_sources_missing,
        "checks": checks,
        "passed": all(checks.values()),
    }


def build_evaluation_report(
    cases: Iterable[AtlasEvaluationCase],
    results: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    for case in cases:
        result = results.get(case.case_id)
        if result is None:
            reports.append(
                {
                    "schema_version": EVALUATION_SCHEMA_VERSION,
                    "case_id": case.case_id,
                    "project_code": case.project_code,
                    "agent_id": case.agent_id,
                    "checks": {"result_present": False},
                    "passed": False,
                }
            )
            continue
        reports.append(evaluate_atlas_result(case, result))
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "case_count": len(reports),
        "passed_count": sum(bool(item.get("passed")) for item in reports),
        "failed_count": sum(not bool(item.get("passed")) for item in reports),
        "passed": bool(reports) and all(bool(item.get("passed")) for item in reports),
        "cases": reports,
    }


__all__ = [
    "EVALUATION_SCHEMA_VERSION",
    "AtlasEvaluationCase",
    "AtlasEvaluationError",
    "build_evaluation_report",
    "evaluate_atlas_result",
    "load_evaluation_cases",
]
