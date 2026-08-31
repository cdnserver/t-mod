#!/usr/bin/env python3
"""Validate captured Atlas responses against a private, versioned eval suite.

The script is intentionally offline: it does not call an AI provider, transmit
questions, or change a rollout.  Capture candidate and base-model answers by
an approved runner, then gate the proposed release locally before enabling it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.atlas_evaluation import (  # noqa: E402
    AtlasEvaluationError,
    build_evaluation_report,
    load_evaluation_cases,
)


def _load_results(path: Path) -> dict[str, dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise AtlasEvaluationError("atlas_eval_results_unavailable") from exc
    result: dict[str, dict[str, Any]] = {}
    for line_number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AtlasEvaluationError(f"atlas_eval_result_json_invalid:{line_number}") from exc
        if not isinstance(item, dict):
            raise AtlasEvaluationError(f"atlas_eval_result_json_invalid:{line_number}")
        case_id = str(item.get("case_id") or "").strip().lower()
        payload = item.get("result") if isinstance(item.get("result"), dict) else item
        if not case_id or case_id in result:
            raise AtlasEvaluationError("atlas_eval_result_duplicate_or_missing")
        result[case_id] = dict(payload)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Atlas offline release checks against captured JSONL answers."
    )
    parser.add_argument("--cases", type=Path, required=True, help="Private cases JSONL")
    parser.add_argument("--results", type=Path, required=True, help="Captured response JSONL")
    parser.add_argument("--output", type=Path, required=True, help="Evaluation report JSON")
    args = parser.parse_args()
    try:
        report = build_evaluation_report(
            load_evaluation_cases(args.cases),
            _load_results(args.results),
        )
    except AtlasEvaluationError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        args.output.chmod(0o600)
    except OSError:
        pass
    print(json.dumps({key: report[key] for key in ("case_count", "passed_count", "failed_count", "passed")}, ensure_ascii=False))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
