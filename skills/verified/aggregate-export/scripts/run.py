# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Apply aggregate count floor rules to a synthetic cohort summary."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_SKILL_ID = "heartwood.synthetic.aggregate-export"


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if isinstance(value, dict):
        return value
    msg = f"expected {name} to be an object"
    raise TypeError(msg)


def build_export(
    summary: Mapping[str, object], *, aggregate_count_floor: int = 20
) -> dict[str, Any]:
    """Build an aggregate export decision from a cohort summary."""
    if aggregate_count_floor < 0:
        msg = "aggregate count floor must be non-negative"
        raise ValueError(msg)
    summary_payload = _mapping(summary.get("summary"), "summary")
    participant_count = int(summary_payload.get("participant_count", 0))
    if participant_count < aggregate_count_floor:
        return {
            "schema_version": "heartwood.skill-output.v1",
            "skill_id": _SKILL_ID,
            "exported": False,
            "suppressed": True,
            "aggregate_count_floor": aggregate_count_floor,
            "reason": "cohort is below the aggregate count floor",
            "aggregates": {},
        }
    aggregates = {
        "participant_count": participant_count,
        "condition_occurrence_count": int(summary_payload.get("condition_occurrence_count", 0)),
    }
    if "target_condition_occurrence_count" in summary_payload:
        aggregates["target_condition_occurrence_count"] = int(
            summary_payload["target_condition_occurrence_count"]
        )
    return {
        "schema_version": "heartwood.skill-output.v1",
        "skill_id": _SKILL_ID,
        "exported": True,
        "suppressed": False,
        "aggregate_count_floor": aggregate_count_floor,
        "reason": "aggregate output satisfies the count floor",
        "aggregates": aggregates,
    }


def _non_negative_int(value: str) -> int:
    floor = int(value)
    if floor < 0:
        msg = "aggregate count floor must be non-negative"
        raise argparse.ArgumentTypeError(msg)
    return floor


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply aggregate export guards.")
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--aggregate-count-floor", default=20, type=_non_negative_int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the aggregate export skill."""
    args = _build_parser().parse_args(argv)
    summary = _mapping(json.loads(args.summary.read_text(encoding="utf-8")), "summary file")
    export = build_export(summary, aggregate_count_floor=args.aggregate_count_floor)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(export, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
