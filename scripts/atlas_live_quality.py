#!/usr/bin/env python3
"""Run a repeatable production-like Atlas retrieval and answer smoke suite.

The suite contains no user data and never changes Atlas state.  It exercises
the same retrieval and completion functions as web/Discord, writes a compact
JSON report and returns a non-zero exit code when a mandatory check regresses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.atlas_ai import atlas_answer, atlas_ai_health, atlas_search  # noqa: E402
from persistence.core import connect_readonly  # noqa: E402


@dataclass(frozen=True, slots=True)
class LiveCase:
    case_id: str
    question: str
    expected_title: str = ""
    agent_id: str = "atlas-tvr-a"
    required_terms: tuple[str, ...] = ()
    forbidden_terms: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    max_words: int = 0


CASES = (
    LiveCase(
        "uk-chapter-16",
        "Напиши полностью главу 16 Уголовного кодекса штата.",
        "Уголовный Кодекс",
        required_terms=("16.1", "16.19", "против правосудия"),
        max_words=2_500,
    ),
    LiveCase(
        "road-article-16",
        "Что предусматривает статья 16 Дорожного кодекса?",
        "Дорожный Кодекс",
        required_terms=("телефон", "$7000"),
        max_words=160,
    ),
    LiveCase(
        "ooc-dm",
        "Что такое DM и какое наказание предусмотрено правилами проекта?",
        "Основные правила проекта",
        required_terms=("DM",),
        max_words=220,
    ),
    LiveCase(
        "ooc-software",
        "Можно ли использовать стороннее ПО и как проходит проверка?",
        "Правила проверки на стороннее ПО",
        required_terms=("ПО",),
        max_words=360,
    ),
    LiveCase(
        "ooc-bank",
        "Кратко перечисли основные правила ограбления банков.",
        "Правила ограбления банков",
        required_terms=("12:00", "01:00"),
        max_words=180,
    ),
    LiveCase(
        "ic-detention",
        "Меня задержали сотрудники LSPD. Кратко: какие у меня права и что делать по шагам?",
        "Процессуальный Кодекс",
        required_terms=("задерж", "прав"),
        forbidden_terms=(
            "в библиотеке нет",
            "нет явного IC-кодекса",
            "точная статья не найдена",
        ),
        max_words=280,
    ),
    LiveCase(
        "ic-prosecutor",
        "Кратко объясни полномочия Генерального прокурора.",
        "О деятельности офиса Генерального прокурора",
        required_terms=("прокурор", "полномоч"),
        forbidden_terms=("в библиотеке Atlas нет", "нужная IC-норма отсутствует"),
        max_words=260,
    ),
    LiveCase(
        "complaint-dm",
        "Игрок убил меня без причины и диалога. Определи нарушение и составь краткую жалобу.",
        "Основные правила проекта",
        agent_id="atlas-complaints",
        required_terms=("DM",),
        forbidden_terms=("20 секунд", "храните оригинал видео минимум 48 часов"),
        forbidden_patterns=(
            r"\b\d{2}\.\d{2}\.\d{4}\b",
            r"\b\d{1,2}:\d{2}\b",
            r"\b[^\s]+\.(?:mp4|mov|png|jpe?g)\b",
        ),
        max_words=480,
    ),
    LiveCase(
        "social-greeting",
        "Привет!",
        required_terms=("привет",),
        forbidden_terms=("интерфейс", "библиотек", "режим работы", "LSPD", "phoenix"),
        max_words=30,
    ),
)


def _organization_id(project_code: str, explicit: int) -> int:
    if explicit > 0:
        return explicit
    with connect_readonly() as connection:
        row = connection.execute(
            "SELECT id FROM atlas_organizations WHERE project_code = ? ORDER BY id LIMIT 1",
            (project_code,),
        ).fetchone()
    if row is None:
        raise RuntimeError("atlas_live_quality_organization_missing")
    return int(row["id"])


def _source_titles(items: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("title") or "") for item in items]


def _contains(value: str, expected: str) -> bool:
    return expected.casefold() in value.casefold()


def _looks_complete(value: str) -> bool:
    clean = str(value or "").rstrip()
    if not clean:
        return False
    # Provider token exhaustion used to leave visible answers ending in
    # fragments such as ``...увеличивается в``. A finished citation marker is
    # also a valid ending for deterministic exact retrieval.
    return clean[-1] in ".!?…)]}»\"”'"


async def run(args: argparse.Namespace) -> dict[str, Any]:
    organization_id = _organization_id(args.project, args.organization_id)
    health = await atlas_ai_health(force=True)
    reports: list[dict[str, Any]] = []
    for case in CASES:
        started = time.monotonic()
        report: dict[str, Any] = {
            "case_id": case.case_id,
            "question": case.question,
            "agent_id": case.agent_id,
            "checks": {},
        }
        try:
            hits = await atlas_search(
                organization_id,
                case.question,
                server_code=args.server,
                faction_code=args.faction,
                limit=6,
                expanded=True,
            )
            titles = _source_titles(hits)
            report["top_sources"] = titles[:6]
            report["checks"]["retrieval"] = (
                not case.expected_title
                or any(_contains(title, case.expected_title) for title in titles[:3])
            )
            if args.include_model:
                result = await atlas_answer(
                    organization_id,
                    case.question,
                    server_code=args.server,
                    faction_code=args.faction,
                    response_mode="balanced",
                    model_id=case.agent_id,
                    latency_mode="standard",
                )
                answer = str(result.get("answer") or "").strip()
                cited_titles = _source_titles(list(result.get("citations") or []))
                report.update(
                    {
                        "answer": answer,
                        "answer_words": len(answer.split()),
                        "citations": cited_titles,
                        "model": result.get("model"),
                        "model_provider": result.get("model_provider"),
                        "model_release": result.get("model_release"),
                        "latency_ms": result.get("latency_ms"),
                    }
                )
                report["checks"].update(
                    {
                        "answer_nonempty": bool(answer),
                        "required_terms": all(_contains(answer, term) for term in case.required_terms),
                        "forbidden_terms": not any(
                            _contains(answer, term) for term in case.forbidden_terms
                        ),
                        "forbidden_patterns": not any(
                            re.search(pattern, answer, re.IGNORECASE)
                            for pattern in case.forbidden_patterns
                        ),
                        "word_limit": not case.max_words or len(answer.split()) <= case.max_words,
                        "complete": _looks_complete(answer),
                        "scope": (
                            str(result.get("project_code") or "") == args.project
                            and str(result.get("server_code") or "") == args.server
                            and str(result.get("faction_code") or "") == args.faction
                        ),
                        "agent": str((result.get("agent") or {}).get("id") or "") == case.agent_id,
                    }
                )
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            report["error"] = f"{type(exc).__name__}:{str(exc)[:800]}"
            report["checks"]["completed"] = False
        report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        report["passed"] = bool(report["checks"]) and all(report["checks"].values())
        reports.append(report)
    return {
        "schema_version": 1,
        "project_code": args.project,
        "server_code": args.server,
        "faction_code": args.faction,
        "organization_id": organization_id,
        "include_model": bool(args.include_model),
        "health": health,
        "case_count": len(reports),
        "passed_count": sum(bool(item["passed"]) for item in reports),
        "failed_count": sum(not bool(item["passed"]) for item in reports),
        "passed": bool(health.get("configured"))
        and str(health.get("qdrant") or "") == "ok"
        and all(bool(item["passed"]) for item in reports),
        "cases": reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Atlas live quality suite.")
    parser.add_argument("--project", default="majestic-rp")
    parser.add_argument("--server", default="phoenix-15")
    parser.add_argument("--faction", default="lspd")
    parser.add_argument("--organization-id", type=int, default=0)
    parser.add_argument("--include-model", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "case_count": report["case_count"],
                "passed_count": report["passed_count"],
                "failed_count": report["failed_count"],
                "passed": report["passed"],
                "output": str(args.output or ""),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
