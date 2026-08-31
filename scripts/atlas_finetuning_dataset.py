#!/usr/bin/env python3
"""Build a locally reviewed, provider-neutral Atlas fine-tuning bundle."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.atlas_finetuning import (  # noqa: E402
    DEFAULT_PROJECT_CODE,
    AtlasDatasetScopeError,
    atlas_agent_system_prompt,
    build_finetuning_bundle,
)
from persistence.atlas_repository import atlas_training_candidates  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export liked Atlas answers as redacted review candidates and build "
            "train/eval JSONL only from rows marked approved."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Private folder for one project and one agent (default is scoped automatically).",
    )
    parser.add_argument("--approvals", type=Path)
    parser.add_argument("--organization-id", type=int)
    parser.add_argument(
        "--project-code",
        default=DEFAULT_PROJECT_CODE,
        help=f"Atlas project boundary (default: {DEFAULT_PROJECT_CODE}).",
    )
    parser.add_argument(
        "--agent-id",
        default="atlas-tvr-a",
        help="One registered Atlas agent; each agent receives its own dataset.",
    )
    parser.add_argument(
        "--all-organizations",
        action="store_true",
        help="Explicitly include every Atlas space inside the selected project and agent lane.",
    )
    parser.add_argument("--limit", type=int, default=5_000)
    args = parser.parse_args()
    if args.organization_id is None and not args.all_organizations:
        parser.error("select --organization-id or explicitly pass --all-organizations")
    if args.organization_id is not None and args.all_organizations:
        parser.error("--organization-id and --all-organizations are mutually exclusive")
    if args.limit < 1:
        parser.error("--limit must be positive")
    try:
        # Reject unknown aliases before reading any history.  The resulting
        # prompt is intentionally unused here: it confirms the export lane is
        # backed by the installed Atlas agent registry.
        atlas_agent_system_prompt(args.agent_id)
    except AtlasDatasetScopeError as exc:
        parser.error(str(exc))

    project_code = str(args.project_code or "").strip().lower()
    agent_id = str(args.agent_id or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", project_code):
        parser.error("atlas_finetuning_project_invalid")
    output_dir = args.output_dir or (
        Path("build") / "atlas-finetuning" / project_code / agent_id
    )

    rows = atlas_training_candidates(
        organization_id=args.organization_id,
        project_code=project_code,
        agent_id=agent_id,
        limit=args.limit,
    )
    try:
        manifest = build_finetuning_bundle(
            rows,
            output_dir,
            approvals_path=args.approvals,
            project_code=project_code,
            agent_id=agent_id,
        )
    except AtlasDatasetScopeError as exc:
        parser.error(str(exc))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
