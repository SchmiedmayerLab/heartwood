# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Build a deterministic baseline model artifact from synthetic tables."""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_SKILL_ID = "heartwood.synthetic.baseline-model"


def _read_table(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def build_model(data_root: Path) -> dict[str, Any]:
    """Build a deterministic model artifact without row-level output."""
    person_rows = _read_table(data_root / "person.csv")
    condition_rows = _read_table(data_root / "condition_occurrence.csv")
    participant_count = max(len(person_rows), 1)
    condition_ratio = round(len(condition_rows) / participant_count, 4)
    return {
        "schema_version": "heartwood.skill-output.v1",
        "skill_id": _SKILL_ID,
        "model": {
            "model_type": "synthetic-condition-baseline",
            "feature_names": ("age_bucket", "condition_history"),
            "coefficients": {
                "intercept": 0.0,
                "condition_history": condition_ratio,
            },
        },
        "quality_checks": {
            "synthetic_only": True,
            "row_values_exported": False,
            "requires_network": False,
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a synthetic baseline model artifact.")
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the baseline model skill."""
    args = _build_parser().parse_args(argv)
    model = build_model(args.data_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(model, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
