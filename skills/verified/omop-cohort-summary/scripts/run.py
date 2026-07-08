# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Build aggregate cohort summary output for synthetic OMOP-like tables."""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_SKILL_ID = "heartwood.synthetic.omop-cohort-summary"


def _read_table(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def build_summary(data_root: Path, *, aggregate_count_floor: int = 20) -> dict[str, Any]:
    """Build aggregate cohort counts and quality checks."""
    if aggregate_count_floor < 0:
        msg = "aggregate count floor must be non-negative"
        raise ValueError(msg)
    person_rows = _read_table(data_root / "person.csv")
    condition_rows = _read_table(data_root / "condition_occurrence.csv")
    person_ids = {row["person_id"] for row in person_rows if row.get("person_id")}
    condition_person_ids = {row["person_id"] for row in condition_rows if row.get("person_id")}
    missing_references = sorted(condition_person_ids - person_ids)
    participant_count = len(person_ids)
    condition_count = len(condition_rows)
    return {
        "schema_version": "heartwood.skill-output.v1",
        "skill_id": _SKILL_ID,
        "dataset_type": "omop-cdm",
        "summary": {
            "participant_count": participant_count,
            "condition_occurrence_count": condition_count,
            "condition_person_coverage_count": len(condition_person_ids & person_ids),
        },
        "quality_checks": {
            "person_table_present": bool(person_rows),
            "condition_table_present": bool(condition_rows),
            "condition_references_known_persons": not missing_references,
            "row_values_exported": False,
        },
        "export_guard": {
            "aggregate_count_floor": aggregate_count_floor,
            "exportable": participant_count >= aggregate_count_floor,
        },
    }


def _non_negative_int(value: str) -> int:
    floor = int(value)
    if floor < 0:
        msg = "aggregate count floor must be non-negative"
        raise argparse.ArgumentTypeError(msg)
    return floor


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize synthetic OMOP-like tables.")
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--aggregate-count-floor", default=20, type=_non_negative_int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the cohort summary skill."""
    args = _build_parser().parse_args(argv)
    summary = build_summary(args.data_root, aggregate_count_floor=args.aggregate_count_floor)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
