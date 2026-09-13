#!/usr/bin/env python3
"""Deterministically verify exact provision retrieval across the saved corpus."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.atlas_ai import (  # noqa: E402
    _atlas_legal_search_text,
    _atlas_structured_legal_candidates,
)
from persistence import atlas_repository as storage  # noqa: E402


_HEADING_RE = re.compile(
    r"(?im)^[^\S\r\n]*(?:(глава|раздел|статья|статьи)[^\S\r\n]+"
    r"(?:№[^\S\r\n]*)?(\d+(?:\.\d+){0,3}|[ivxlcdm]{1,8})"
    r"|(\d+\.\d+(?:\.\d+){0,2}))(?!\.\d)(?=[.\s:—-]|$)"
)


def _references(source: dict[str, Any], maximum: int) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in _HEADING_RE.finditer(
        _atlas_legal_search_text(str(source.get("content_text") or ""))
    ):
        label = str(match.group(1) or "").casefold()
        value = str(match.group(2) or match.group(3) or "")
        kind = (
            "chapter"
            if label == "глава"
            else "section"
            if label == "раздел"
            else "article"
        )
        item = (kind, value)
        if item not in found:
            found.append(item)
    if len(found) <= maximum:
        return found
    indexes = sorted(
        {round(index * (len(found) - 1) / (maximum - 1)) for index in range(maximum)}
    )
    return [found[index] for index in indexes]


def audit(*, per_source: int = 6, feed_key: str = "") -> dict[str, Any]:
    sources = storage.atlas_indexable_knowledge_sources(limit=10_000)
    if feed_key:
        sources = [
            source
            for source in sources
            if str((source.get("metadata") or {}).get("sync_feed") or "") == feed_key
        ]
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for source in sources:
        grouped[(int(source["organization_id"]), str(source.get("project_code") or ""))].append(source)

    failures: list[dict[str, Any]] = []
    checks = 0
    covered_sources = 0
    for source in sources:
        references = _references(source, max(2, min(12, int(per_source))))
        if not references:
            continue
        covered_sources += 1
        target_server = str(source.get("server_code") or "")
        target_faction = str(source.get("faction_code") or "")
        corpus = [
            item
            for item in grouped[
                (int(source["organization_id"]), str(source.get("project_code") or ""))
            ]
            if (
                str(item.get("federation_scope") or "workspace") in {"platform", "project"}
                or str(item.get("server_code") or "") == target_server
            )
            and (
                str(item.get("federation_scope") or "workspace") != "faction"
                or str(item.get("faction_code") or "") == target_faction
            )
        ]
        for kind, value in references:
            checks += 1
            noun = {"article": "статью", "chapter": "главу", "section": "раздел"}[kind]
            query = f"Покажи {noun} {value} документа «{source['title']}»"
            candidates = _atlas_structured_legal_candidates(query, corpus)
            ranked = sorted(candidates, key=lambda item: float(item.get("score") or 0), reverse=True)
            top = ranked[0] if ranked else None
            if (
                top is None
                or int(top.get("source_id") or 0) != int(source["id"])
                or str(top.get("reference") or "") != f"{kind}:{value}"
            ):
                failures.append(
                    {
                        "source_id": int(source["id"]),
                        "title": str(source.get("title") or ""),
                        "reference": f"{kind}:{value}",
                        "selected_source_id": int(top.get("source_id") or 0) if top else None,
                        "selected_title": str(top.get("title") or "") if top else None,
                    }
                )
    return {
        "ok": not failures and checks > 0,
        "sources": len(sources),
        "sources_with_structured_references": covered_sources,
        "checks": checks,
        "passed": checks - len(failures),
        "failed": len(failures),
        "failures": failures[:100],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-source", type=int, default=6)
    parser.add_argument("--feed", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    result = audit(per_source=args.per_source, feed_key=str(args.feed or ""))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
