#!/usr/bin/env python3
"""Build a locally reviewed, provider-neutral Atlas fine-tuning bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.atlas_finetuning import build_finetuning_bundle  # noqa: E402
from persistence.atlas_repository import atlas_training_candidates  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export liked Atlas answers as redacted review candidates and build "
            "train/eval JSONL only from rows marked approved."
        )
    )
    parser.add_argument("--output-dir", type=Path, default=Path("build/atlas-finetuning"))
    parser.add_argument("--approvals", type=Path)
    parser.add_argument("--organization-id", type=int)
    parser.add_argument(
        "--all-organizations",
        action="store_true",
        help="Explicitly include every Atlas space (review confidentiality first).",
    )
    parser.add_argument("--limit", type=int, default=5_000)
    args = parser.parse_args()
    if args.organization_id is None and not args.all_organizations:
        parser.error("select --organization-id or explicitly pass --all-organizations")
    if args.organization_id is not None and args.all_organizations:
        parser.error("--organization-id and --all-organizations are mutually exclusive")

    rows = atlas_training_candidates(
        organization_id=args.organization_id,
        limit=args.limit,
    )
    manifest = build_finetuning_bundle(
        rows,
        args.output_dir,
        approvals_path=args.approvals,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
