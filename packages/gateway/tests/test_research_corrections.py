# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Correction evidence preserves originals and reuses the independent research checks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from heartwood.compliance.research import research_tasks
from heartwood.core_adapter.research_corrections import assess_research_correction
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.gateway._research_review import ResearchReviewEvaluator
from heartwood.gateway._workspace import WorkspaceInspector
from heartwood.schemas.review import (
    ResearchCorrectionAttempt,
    ResearchCorrectionRun,
    ResearchReviewRun,
    ReviewCandidate,
    ReviewCorrectionPlan,
    ReviewProposals,
    ReviewSubmission,
)


def _review(
    root: Path, category: str = "coding"
) -> tuple[ResearchReviewEvaluator, ResearchReviewRun, dict[str, str]]:
    if category == "coding":
        (root / "analysis.py").write_text("def broken(\n")
        (root / "notes.txt").write_text("Synthetic study context.\n")
        paths = {"program": "analysis.py", "notes": "notes.txt"}
        corrected = {"program": "print('synthetic')\n"}
        condition = "python-source-invalid"
        affected: tuple[str, ...] = ("program",)
    elif category == "reproducibility":
        (root / "original.txt").write_text("synthetic,1\n")
        (root / "reproduced.txt").write_text("synthetic,2\n")
        (root / "predictions.csv").write_text("prediction\n1\n")
        (root / "reproduced.csv").write_text("prediction\n1\n")
        (root / "verification.json").write_text("{}")
        paths = {
            "metrics": "original.txt",
            "reproduced-metrics": "reproduced.txt",
            "predictions": "predictions.csv",
            "reproduced-predictions": "reproduced.csv",
            "verification": "verification.json",
        }
        corrected = {
            "reproduced-metrics": "synthetic,1\n",
            "reproduced-predictions": "prediction\n1\n",
            "verification": "{}",
        }
        condition = "reproduction-artifact-mismatch"
        affected = ("metrics", "predictions", "reproduced-metrics", "reproduced-predictions")
    else:
        task = next(item for item in research_tasks() if item.case.case_id == "baseline-analysis")
        for name, content in task.inputs.items():
            (root / name).write_text(content)
        script = (
            Path(__file__).parents[2] / "compliance/tests/fixtures/research/reference_analysis.py"
        )
        subprocess.run(
            [
                sys.executable,
                str(script),
                "--data",
                str(root / "data.csv"),
                "--output-dir",
                str(root / "reference"),
            ],
            check=True,
            timeout=30,
        )
        corrected = {
            "metrics": (root / "reference/metrics.json").read_text(),
            "predictions": (root / "reference/predictions.csv").read_text(),
        }
        metrics = json.loads(corrected["metrics"])
        metrics["test_rmse"] += 5
        (root / "metrics.json").write_text(json.dumps(metrics))
        (root / "predictions.csv").write_text(corrected["predictions"])
        (root / "plan.json").write_text(
            json.dumps(
                {
                    "question": "Predict response",
                    "estimand": "Held-out prediction error",
                    "outcome": "response",
                    "features": ["measurement"],
                    "group_column": "subject_id",
                    "split_column": "partition",
                    "assumptions": ["Prespecified split"],
                    "limitations": ["Synthetic data"],
                }
            )
        )
        paths = {
            "data": "data.csv",
            "dictionary": "dictionary.json",
            "plan": "plan.json",
            "metrics": "metrics.json",
            "predictions": "predictions.csv",
        }
        condition = "baseline-result-inconsistent"
        affected = ("metrics", "predictions")
    evaluator = ResearchReviewEvaluator(WorkspaceInspector(ProjectContext(root)))
    snapshot = evaluator.prepare(paths)
    candidate = ReviewCandidate.model_validate(
        {
            "candidate_id": "defect",
            "condition": condition,
            "category": category,
            "severity": "critical",
            "summary": "Untrusted diagnosis, not correction authority.",
            "artifact_ids": affected,
        }
    )
    submission = ReviewSubmission.associate(
        ReviewProposals(candidates=(candidate,)),
        review_id="native-task",
        reviewer_id="selected-reviewer",
        snapshot=snapshot,
    )
    review = ResearchReviewRun(
        review_id="review-one",
        snapshot=snapshot,
        reviewer_ids=("selected-reviewer",),
        started_sequence=1,
        status="assessed",
        submissions=(submission,),
        assessment=evaluator.assess(snapshot, (submission,)),
    )
    assert review.assessment is not None
    assert review.assessment.findings[0].verification == "verified"
    return evaluator, review, corrected


def _write_correction(root: Path, plan: ReviewCorrectionPlan, contents: dict[str, str]) -> None:
    for output in plan.outputs:
        path = root / output.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents[output.artifact_id])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewer_ids", ["selected-reviewer", "selected-reviewer"]),
        ("reviewer_ids", ["unrelated-reviewer"]),
        ("status", "pending"),
        ("status", "unavailable"),
        ("unavailable_reason", "invalid-review"),
        ("submissions", []),
        ("assessment", None),
    ],
)
def test_review_journal_rejects_unbound_or_incomplete_results(
    tmp_path: Path, field: str, value: object
) -> None:
    _, review, _ = _review(tmp_path)
    record = review.model_dump(mode="json")
    record[field] = value
    with pytest.raises(ValidationError):
        ResearchReviewRun.model_validate(record)


@pytest.mark.parametrize("mutation", ["source", "assessment", "duplicate", "claim", "alias"])
def test_review_journal_rejects_substituted_evidence(tmp_path: Path, mutation: str) -> None:
    _, review, _ = _review(tmp_path)
    record = review.model_dump(mode="json")
    if mutation == "source":
        record["submissions"][0]["snapshot_sha256"] = "0" * 64
    elif mutation == "assessment":
        record["assessment"]["snapshot_sha256"] = "0" * 64
    elif mutation == "duplicate":
        record["submissions"].append(record["submissions"][0])
    elif mutation == "claim":
        record["assessment"]["findings"][0]["verified_claim"] = None
    else:
        record["snapshot"]["artifacts"][1]["file"]["path"] = record["snapshot"]["artifacts"][0][
            "file"
        ]["path"]
    with pytest.raises(ValidationError):
        ResearchReviewRun.model_validate(record)


@pytest.mark.parametrize(
    "mutation",
    [
        "limit",
        "duplicate-attempt",
        "pending-success",
        "wrong-review",
        "wrong-plan",
        "changed-source",
        "missing-finding",
    ],
)
def test_correction_series_rejects_inconsistent_persisted_evidence(
    tmp_path: Path, mutation: str
) -> None:
    evaluator, review, corrected = _review(tmp_path)
    plan = evaluator.prepare_correction(review, output_directory="correction-one")
    _write_correction(tmp_path, plan, corrected)
    attempt = ResearchCorrectionAttempt(
        attempt_id="attempt-one",
        plan=plan,
        started_sequence=10,
        status="assessed",
        assessment=evaluator.assess_correction(review, plan),
    )
    series = ResearchCorrectionRun(
        correction_id="correction-one",
        stage_id="execute",
        review=review,
        maximum_attempts=2,
        attempts=(attempt,),
        stop_reason="corrected",
    )
    data = series.model_dump(mode="json")
    if mutation == "limit":
        data["maximum_attempts"] = 0
    elif mutation == "duplicate-attempt":
        data["attempts"].append(data["attempts"][0])
    elif mutation == "pending-success":
        data["attempts"][0].update(status="pending", assessment=None)
    elif mutation == "wrong-review":
        data["attempts"][0]["plan"]["review_id"] = "unrelated-review"
    elif mutation == "wrong-plan":
        data["attempts"][0]["assessment"]["plan_sha256"] = "0" * 64
    elif mutation == "changed-source":
        data["attempts"][0]["assessment"]["snapshot"]["artifacts"][0]["file"]["path"] = (
            "unrelated.py"
        )
    else:
        data["attempts"][0]["assessment"]["checks"] = []
    with pytest.raises(ValidationError):
        ResearchCorrectionRun.model_validate(data)
    assert ResearchCorrectionRun.model_validate_json(series.model_dump_json()) == series


@pytest.mark.parametrize("category", ["coding", "statistical", "reproducibility"])
@pytest.mark.parametrize("correct", [True, False])
def test_correction_rechecks_exact_defects_without_overwriting_evidence(
    tmp_path: Path, category: str, correct: bool
) -> None:
    evaluator, review, corrected = _review(tmp_path, category)
    before = {
        item.file.path: (tmp_path / item.file.path).read_bytes()
        for item in review.snapshot.artifacts
    }
    plan = evaluator.prepare_correction(review, output_directory="correction-one")
    assert not (tmp_path / "correction-one").exists()
    assert plan == evaluator.prepare_correction(review, output_directory="correction-one")
    assert {item.artifact_id for item in plan.outputs} == set(corrected)
    if not correct:
        corrected = {
            item.artifact_id: (tmp_path / item.file.path).read_text()
            for item in review.snapshot.artifacts
        }
    _write_correction(tmp_path, plan, corrected)
    result = evaluator.assess_correction(review, plan)
    assert result.plan_sha256 == plan.fingerprint
    assert result.snapshot is not None
    assert [item.status for item in result.checks] == [
        "not_observed" if correct else "still_observed"
    ]
    assert {
        item.file.path: (tmp_path / item.file.path).read_bytes()
        for item in review.snapshot.artifacts
    } == before
    assert evaluator.assess_correction(review, plan) == result
    restored = ResearchReviewEvaluator(WorkspaceInspector(ProjectContext(tmp_path)))
    assert restored.assess_correction(review, plan) == result


@pytest.mark.parametrize(
    "destination",
    ["analysis.py", "analysis.py/new", ".heartwood/new", "../outside", "existing", "link"],
)
def test_correction_rejects_unsafe_or_used_destinations(tmp_path: Path, destination: str) -> None:
    evaluator, review, _ = _review(tmp_path)
    (tmp_path / "existing").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "existing", target_is_directory=True)
    with pytest.raises(ValueError, match=r"overlaps|private|relative|parent|absent|symlink"):
        evaluator.prepare_correction(review, output_directory=destination)


@pytest.mark.parametrize("change", ["source", "context", "missing", "forged-plan"])
def test_correction_cannot_rewrite_its_review_context(tmp_path: Path, change: str) -> None:
    evaluator, review, corrected = _review(tmp_path)
    plan = evaluator.prepare_correction(review, output_directory="correction-one")
    _write_correction(tmp_path, plan, corrected)
    if change == "source":
        (tmp_path / "analysis.py").write_text(corrected["program"])
    elif change == "context":
        (tmp_path / "notes.txt").write_text("Different context")
    elif change == "missing":
        (tmp_path / "analysis.py").unlink()
    else:
        plan = plan.model_copy(update={"finding_ids": ("0" * 64,)})
    with pytest.raises(ValueError, match=r"changed|does not match"):
        evaluator.assess_correction(review, plan)


@pytest.mark.parametrize("missing", ["absent", "symlink", "invalid-utf8"])
def test_unavailable_outputs_never_count_as_correction(tmp_path: Path, missing: str) -> None:
    evaluator, review, corrected = _review(tmp_path)
    plan = evaluator.prepare_correction(review, output_directory="correction-one")
    if missing != "absent":
        _write_correction(tmp_path, plan, corrected)
        path = tmp_path / plan.outputs[0].path
        path.unlink()
        if missing == "symlink":
            path.symlink_to(tmp_path / "analysis.py")
        else:
            path.write_bytes(b"\xff")
    result = evaluator.assess_correction(review, plan)
    assert result.snapshot is None
    assert result.checks[0].status == "unavailable"


def test_correction_detects_output_changed_after_capture(tmp_path: Path) -> None:
    evaluator, review, corrected = _review(tmp_path)
    plan = evaluator.prepare_correction(review, output_directory="correction-one")
    _write_correction(tmp_path, plan, corrected)
    paths = {item.artifact_id: item.file.path for item in review.snapshot.artifacts}
    paths.update({item.artifact_id: item.path for item in plan.outputs})
    snapshot = evaluator.prepare(paths)
    original = {
        item.artifact_id: (tmp_path / item.file.path).read_text()
        for item in review.snapshot.artifacts
    }
    changed = {name: (tmp_path / path).read_text() for name, path in paths.items()}
    changed["program"] = "different(\n"
    result = assess_research_correction(
        review,
        plan,
        snapshot,
        original_observed=original,
        corrected_observed=changed,
    )
    assert result.checks[0].status == "stale"


def test_stale_review_and_disproved_findings_are_not_admitted(tmp_path: Path) -> None:
    evaluator, review, corrected = _review(tmp_path)
    (tmp_path / "analysis.py").write_text(corrected["program"])
    with pytest.raises(ValueError, match="changed"):
        evaluator.prepare_correction(review, output_directory="correction-one")
    snapshot = evaluator.prepare({"program": "analysis.py", "notes": "notes.txt"})
    submission = ReviewSubmission.associate(
        ReviewProposals(candidates=review.submissions[0].candidates),
        review_id="new-task",
        reviewer_id="selected-reviewer",
        snapshot=snapshot,
    )
    review = review.model_copy(
        update={
            "snapshot": snapshot,
            "submissions": (submission,),
            "assessment": evaluator.assess(snapshot, (submission,)),
        }
    )
    with pytest.raises(ValueError, match="No independently verified"):
        evaluator.prepare_correction(review, output_directory="correction-one")


@pytest.mark.parametrize("field", ["severity", "reason", "verified_claim"])
def test_forged_assessment_cannot_authorize_a_correction(tmp_path: Path, field: str) -> None:
    evaluator, review, _ = _review(tmp_path)
    assert review.assessment is not None
    finding = review.assessment.findings[0]
    changed = finding.model_copy(update={field: "low" if field == "severity" else "fabricated"})
    review = review.model_copy(
        update={"assessment": review.assessment.model_copy(update={"findings": (changed,)})}
    )
    with pytest.raises(ValueError, match="inconsistent"):
        evaluator.prepare_correction(review, output_directory="correction-one")
    assert not (tmp_path / "correction-one").exists()


def test_gateway_correction_contract_is_read_only_and_matches_its_evaluator(tmp_path: Path) -> None:
    evaluator, review, corrected = _review(tmp_path)
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={})
    try:
        plan = gateway.prepare_research_correction(review, output_directory="correction-one")
        assert not (tmp_path / ".heartwood").exists()
        assert not (tmp_path / "correction-one").exists()
        assert plan == evaluator.prepare_correction(review, output_directory="correction-one")
        _write_correction(tmp_path, plan, corrected)
        result = gateway.assess_research_correction(review, plan)
        assert result == evaluator.assess_correction(review, plan)
        assert not (tmp_path / ".heartwood").exists()
        assert result.checks[0].status == "not_observed"
    finally:
        gateway.stop()


def test_syntax_correction_does_not_execute_or_establish_runtime_correctness(
    tmp_path: Path,
) -> None:
    evaluator, review, _ = _review(tmp_path)
    plan = evaluator.prepare_correction(review, output_directory="correction-one")
    marker = tmp_path / "must-not-exist"
    _write_correction(
        tmp_path,
        plan,
        {
            "program": f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
            "raise RuntimeError('failure')\n"
        },
    )
    result = evaluator.assess_correction(review, plan)
    assert result.checks[0].status == "not_observed"
    assert not marker.exists()
