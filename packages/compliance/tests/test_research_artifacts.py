# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Independent scientific artifact checks, including plausible but incorrect results."""

from __future__ import annotations

import json
from dataclasses import replace
from importlib.resources import as_file, files

import pytest

from heartwood.compliance.research import (
    ResearchTask,
    research_suite,
    research_tasks,
    verify_research_artifacts,
)
from heartwood.fixtures import lint_fixture_tree


def _task(case_id: str) -> ResearchTask:
    return next(task for task in research_tasks() if task.case.case_id == case_id)


def _readiness() -> dict[str, str]:
    return {
        "readiness.json": json.dumps(
            {
                "row_count": 15,
                "subject_count": 8,
                "duplicate_rows": 1,
                "missing_by_column": {"measurement": 1},
                "invalid_by_column": {"measurement": 1, "visit": 1},
                "arm_counts": {"A": 7, "B": 8},
                "leakage_columns": ["future_response"],
                "ready_for_analysis": False,
            }
        ),
        "readiness.md": (
            "Resolve missing and invalid measurements and duplicates; exclude target leakage."
        ),
    }


def _baseline() -> dict[str, str]:
    return {
        "analysis.py": "print('This fixture establishes syntax only, not successful execution.')\n",
        "plan.json": json.dumps(
            {
                "question": "How does measurement predict response in held-out subjects?",
                "estimand": "Held-out visit-level squared prediction error",
                "assumptions": ["The prespecified subject partition is appropriate."],
                "limitations": ["Tiny synthetic sample; no scientific performance claim."],
                "outcome": "response",
                "features": ["measurement"],
                "group_column": "subject_id",
                "split_column": "partition",
            }
        ),
        "metrics.json": json.dumps(
            {
                "outcome": "response",
                "features": ["measurement"],
                "n_train": 8,
                "n_test": 4,
                "train_subjects": 4,
                "test_subjects": 2,
                "intercept": 1.5714285714285712,
                "slope": 3.0952380952380953,
                "test_rmse": 1.1147333248304414,
                "test_mae": 0.9523809523809526,
                "test_r2": 0.9185160402958996,
                "mean_baseline_rmse": 18.418740456393863,
                "sensitivity_rmse": {
                    "S001": 1.2649110640673513,
                    "S002": 1.0591732097037474,
                    "S003": 1.0814769377003841,
                    "S004": 1.5183234577813036,
                },
            }
        ),
        "predictions.csv": (
            "subject_id,visit,prediction\n"
            "S005,1,29.42857142857143\nS005,2,32.523809523809526\n"
            "S006,1,35.61904761904762\nS006,2,38.714285714285715\n"
        ),
        "report.md": (
            "Held-out results include subject-omission sensitivity and are synthetic only."
        ),
    }


def _passed(task: ResearchTask, artifacts: dict[str, str]) -> dict[str, bool]:
    return {
        check.check_id: check.status == "passed"
        for check in verify_research_artifacts(task, artifacts)
    }


def test_all_three_tasks_have_immutable_synthetic_inputs_and_stable_identity() -> None:
    tasks = research_tasks()
    assert [task.case.case_id for task in tasks] == [
        "dataset-readiness",
        "baseline-analysis",
        "result-verification",
    ]
    assert research_suite().fingerprint == research_suite().fingerprint
    for task in tasks:
        assert set(task.inputs) == {"data.csv", "dictionary.json"}
        with pytest.raises(TypeError):
            task.inputs["data.csv"] = "changed"  # type: ignore[index]
    with as_file(files("heartwood.compliance").joinpath("benchmarks", "data")) as directory:
        assert lint_fixture_tree(directory) == ()


def test_readiness_reports_real_aggregate_problems_without_silently_cleaning() -> None:
    assert all(_passed(_task("dataset-readiness"), _readiness()).values())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("row_count", 14),
        ("subject_count", 15),
        ("duplicate_rows", 0),
        ("missing_by_column", {}),
        ("invalid_by_column", {}),
        ("arm_counts", {"A": 6, "B": 6}),
        ("row_count", True),
        ("missing_by_column", {"measurement": -1}),
    ],
)
def test_readiness_rejects_inaccurate_or_invalid_counts(field: str, value: object) -> None:
    artifacts = _readiness()
    report = json.loads(artifacts["readiness.json"])
    report[field] = value
    artifacts["readiness.json"] = json.dumps(report)
    assert not _passed(_task("dataset-readiness"), artifacts)["readiness-counts"]


@pytest.mark.parametrize(
    ("field", "value"), [("leakage_columns", []), ("ready_for_analysis", True)]
)
def test_readiness_rejects_unsupported_readiness_claim(field: str, value: object) -> None:
    artifacts = _readiness()
    report = json.loads(artifacts["readiness.json"])
    report[field] = value
    artifacts["readiness.json"] = json.dumps(report)
    assert not _passed(_task("dataset-readiness"), artifacts)["readiness-leakage"]


def test_baseline_artifacts_match_independent_heldout_reference_values() -> None:
    task = _task("baseline-analysis")
    checks = _passed(task, _baseline())
    assert all(checks.values())
    assert "independent-script-rerun" not in checks
    assert "approved-actions-only" not in checks
    assert "audit-verified" not in checks
    assert set(checks) < {check.check_id for check in task.case.required_checks}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("test_rmse", 0.0),
        ("test_r2", 1.0),
        ("mean_baseline_rmse", 0.0),
        ("n_train", 12),
        ("test_subjects", 4),
        ("features", ["future_response"]),
        ("outcome", "measurement"),
        ("slope", float("nan")),
    ],
)
def test_baseline_rejects_training_leakage_population_and_metric_errors(
    field: str, value: object
) -> None:
    artifacts = _baseline()
    metrics = json.loads(artifacts["metrics.json"])
    metrics[field] = value
    artifacts["metrics.json"] = json.dumps(metrics)
    assert not _passed(_task("baseline-analysis"), artifacts)["baseline-heldout-metrics"]


def test_sensitivity_is_computed_by_omitting_whole_subjects() -> None:
    artifacts = _baseline()
    metrics = json.loads(artifacts["metrics.json"])
    metrics["sensitivity_rmse"]["S004"] = metrics["test_rmse"]
    artifacts["metrics.json"] = json.dumps(metrics)
    checks = _passed(_task("baseline-analysis"), artifacts)
    assert checks["baseline-heldout-metrics"]
    assert not checks["baseline-sensitivity"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("features", ["future_response"]),
        ("group_column", "visit"),
        ("split_column", "random_row_split"),
        ("limitations", []),
        ("assumptions", [None]),
    ],
)
def test_analysis_plan_requires_the_prespecified_scientific_design(
    field: str, value: object
) -> None:
    artifacts = _baseline()
    plan = json.loads(artifacts["plan.json"])
    plan[field] = value
    artifacts["plan.json"] = json.dumps(plan)
    assert not _passed(_task("baseline-analysis"), artifacts)["baseline-plan"]


@pytest.mark.parametrize(
    "replacement",
    [
        "S005,1,0",
        "S001,1,29.42857142857143",
        "S005,1,nan",
        "S005,1,29.42857142857143,extra",
    ],
)
def test_predictions_require_exact_heldout_visits_and_values(replacement: str) -> None:
    artifacts = _baseline()
    artifacts["predictions.csv"] = artifacts["predictions.csv"].replace(
        "S005,1,29.42857142857143", replacement
    )
    assert not _passed(_task("baseline-analysis"), artifacts)["baseline-predictions"]


@pytest.mark.parametrize("source", ["def broken(:", "", "# no program\n"])
def test_script_syntax_requires_a_parseable_program(source: str) -> None:
    artifacts = _baseline()
    artifacts["analysis.py"] = source
    assert not _passed(_task("baseline-analysis"), artifacts)["baseline-script-syntax"]


@pytest.mark.parametrize("changed", [False, True])
def test_reproduction_requires_an_honest_byte_comparison(changed: bool) -> None:
    artifacts = _baseline()
    artifacts.update(
        {f"reproduced/{name}": artifacts[name] for name in ("metrics.json", "predictions.csv")}
    )
    if changed:
        artifacts["reproduced/metrics.json"] += "\n"
    artifacts["verification.md"] = "Re-executed the synthetic analysis and compared both outputs."
    artifacts["verification.json"] = json.dumps(
        {
            "status": "discrepancy" if changed else "reproduced",
            "matching_artifacts": ["predictions.csv"]
            if changed
            else ["metrics.json", "predictions.csv"],
            "mismatched_artifacts": ["metrics.json"] if changed else [],
        }
    )
    assert all(_passed(_task("result-verification"), artifacts).values())
    if changed:
        artifacts["verification.json"] = json.dumps(
            {
                "status": "reproduced",
                "matching_artifacts": ["metrics.json", "predictions.csv"],
                "mismatched_artifacts": [],
            }
        )
        assert not _passed(_task("result-verification"), artifacts)["verification-comparison"]


@pytest.mark.parametrize(
    "case_id", ["dataset-readiness", "baseline-analysis", "result-verification"]
)
def test_missing_or_malformed_artifacts_never_satisfy_the_case(case_id: str) -> None:
    task = _task(case_id)
    for artifacts in ({}, dict.fromkeys(task.artifact_paths, "{malformed")):
        assert not all(_passed(task, artifacts).values())


def test_artifact_limits_and_task_identity_are_enforced() -> None:
    task = _task("baseline-analysis")
    with pytest.raises(ValueError, match="text limit"):
        verify_research_artifacts(task, {"metrics.json": "x" * (512 * 1024 + 1)})
    with pytest.raises(ValueError, match="contract changed"):
        verify_research_artifacts(replace(task, instruction="Use the outcome as a predictor"), {})
