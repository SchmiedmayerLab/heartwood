# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the verified prototype skill scripts."""

from __future__ import annotations

import json
from pathlib import Path

from heartwood.skills import LocalSkillVerifier, SkillTestHarness

_DATA_ROOT = Path("fixtures/synthetic/omop-like")
_SKILLS_ROOT = Path("skills/verified")


def _load_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_omop_cohort_summary_script_emits_qc_and_export_guard(tmp_path: Path) -> None:
    manifest = LocalSkillVerifier(_SKILLS_ROOT).load_manifest(_SKILLS_ROOT / "omop-cohort-summary")
    output = tmp_path / "cohort-summary.json"
    SkillTestHarness(_SKILLS_ROOT).run(
        manifest,
        "--data-root",
        str(_DATA_ROOT),
        "--output",
        str(output),
    )

    payload = _load_json(output)
    summary = payload["summary"]
    quality_checks = payload["quality_checks"]
    export_guard = payload["export_guard"]
    assert isinstance(summary, dict)
    assert isinstance(quality_checks, dict)
    assert isinstance(export_guard, dict)
    assert summary["participant_count"] == 3
    assert quality_checks["condition_references_known_persons"] is True
    assert export_guard["aggregate_count_floor"] == 20
    assert export_guard["exportable"] is False


def test_aggregate_export_script_suppresses_low_counts(tmp_path: Path) -> None:
    verifier = LocalSkillVerifier(_SKILLS_ROOT)
    cohort = verifier.load_manifest(_SKILLS_ROOT / "omop-cohort-summary")
    export = verifier.load_manifest(_SKILLS_ROOT / "aggregate-export")
    summary_path = tmp_path / "cohort-summary.json"
    export_path = tmp_path / "aggregate-export.json"
    harness = SkillTestHarness(_SKILLS_ROOT)
    harness.run(cohort, "--data-root", str(_DATA_ROOT), "--output", str(summary_path))
    harness.run(export, "--summary", str(summary_path), "--output", str(export_path))

    payload = _load_json(export_path)
    assert payload["exported"] is False
    assert payload["suppressed"] is True
    assert payload["aggregates"] == {}
    assert "participant_count" not in json.dumps(payload["aggregates"])


def test_aggregate_export_script_exports_when_floor_is_satisfied(tmp_path: Path) -> None:
    export = LocalSkillVerifier(_SKILLS_ROOT).load_manifest(_SKILLS_ROOT / "aggregate-export")
    summary_path = tmp_path / "large-summary.json"
    output_path = tmp_path / "large-export.json"
    summary_path.write_text(
        json.dumps(
            {
                "summary": {
                    "participant_count": 21,
                    "condition_occurrence_count": 34,
                }
            }
        ),
        encoding="utf-8",
    )
    SkillTestHarness(_SKILLS_ROOT).run(
        export,
        "--summary",
        str(summary_path),
        "--output",
        str(output_path),
    )
    payload = _load_json(output_path)
    assert payload["exported"] is True
    assert payload["aggregates"] == {
        "participant_count": 21,
        "condition_occurrence_count": 34,
    }


def test_baseline_model_script_omits_row_values(tmp_path: Path) -> None:
    manifest = LocalSkillVerifier(_SKILLS_ROOT).load_manifest(_SKILLS_ROOT / "baseline-model")
    output = tmp_path / "baseline-model.json"
    SkillTestHarness(_SKILLS_ROOT).run(
        manifest,
        "--data-root",
        str(_DATA_ROOT),
        "--output",
        str(output),
    )
    payload = _load_json(output)
    quality_checks = payload["quality_checks"]
    assert isinstance(quality_checks, dict)
    assert quality_checks["row_values_exported"] is False
    assert "person_id" not in json.dumps(payload)
