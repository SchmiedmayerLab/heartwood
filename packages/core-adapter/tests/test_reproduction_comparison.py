# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

import json

import pytest

from heartwood.core_adapter.research_checks import (
    MAX_RESEARCH_TEXT_BYTES,
    compare_reproduction_artifacts,
)


def _artifacts(*, discrepancy: bool = False) -> dict[str, str]:
    return {
        "metrics": "{}",
        "reproduced-metrics": "{ }" if discrepancy else "{}",
        "predictions": "prediction\n1\n",
        "reproduced-predictions": "prediction\n1\n",
        "verification": json.dumps(
            {
                "status": "discrepancy" if discrepancy else "reproduced",
                "matching_artifacts": ["predictions.csv"]
                if discrepancy
                else ["metrics.json", "predictions.csv"],
                "mismatched_artifacts": ["metrics.json"] if discrepancy else [],
            }
        ),
    }


@pytest.mark.parametrize("require_match", [True, False])
def test_comparison_accepts_exact_reported_matches(require_match: bool) -> None:
    assert compare_reproduction_artifacts(_artifacts(), require_match=require_match)


def test_honest_discrepancy_is_not_a_reproduced_baseline() -> None:
    artifacts = _artifacts(discrepancy=True)
    assert compare_reproduction_artifacts(artifacts, require_match=False)
    assert not compare_reproduction_artifacts(artifacts, require_match=True)


@pytest.mark.parametrize("damage", ["missing", "malformed", "duplicate", "dishonest", "oversized"])
def test_comparison_rejects_incomplete_or_misreported_results(damage: str) -> None:
    artifacts = _artifacts()
    if damage == "missing":
        del artifacts["reproduced-metrics"]
    elif damage == "malformed":
        artifacts["verification"] = "not json"
    elif damage == "duplicate":
        report = json.loads(artifacts["verification"])
        report["matching_artifacts"].append("metrics.json")
        artifacts["verification"] = json.dumps(report)
    elif damage == "dishonest":
        artifacts["reproduced-metrics"] = "changed"
    else:
        artifacts["reproduced-metrics"] = "x" * (MAX_RESEARCH_TEXT_BYTES + 1)
    assert not compare_reproduction_artifacts(artifacts, require_match=False)
