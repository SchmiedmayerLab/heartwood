# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Review scoring distinguishes detection, false positives, and substituted evidence."""

from dataclasses import replace
from pathlib import Path

import pytest

from heartwood.compliance.review_benchmarks import (
    planning_review_suite,
    planning_review_tasks,
    verify_planning_review,
)
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.gateway._research_review import ResearchReviewEvaluator
from heartwood.gateway._workspace import WorkspaceInspector
from heartwood.schemas.review import (
    ResearchReviewRun,
    ReviewCandidate,
    ReviewProposals,
    ReviewSubmission,
)


@pytest.mark.parametrize("case_id", ["plan-valid", "plan-outcome-leakage"])
@pytest.mark.parametrize("propose", [False, True])
@pytest.mark.parametrize("substitute", [False, True])
def test_plan_review_scores_detection_and_false_accusations(
    tmp_path: Path, case_id: str, propose: bool, substitute: bool
) -> None:
    task = next(task for task in planning_review_tasks() if task.case.case_id == case_id)
    for path, text in task.inputs.items():
        (tmp_path / path).write_text(text)
    if substitute:
        (tmp_path / "plan.json").write_text(task.inputs["plan.json"] + " ")
    gateway = SessionGateway(project=ProjectContext(tmp_path))
    snapshot = gateway.prepare_research_review(
        {"data": "data.csv", "dictionary": "dictionary.json", "plan": "plan.json"}
    )
    candidates = (
        (
            ReviewCandidate(
                candidate_id="plan-claim",
                condition="analysis-plan-incompatible",
                category="statistical",
                severity="high",
                summary="The plan uses a prohibited outcome-derived feature.",
                artifact_ids=("plan",),
            ),
        )
        if propose
        else ()
    )
    submissions = tuple(
        ReviewSubmission.associate(
            ReviewProposals(candidates=candidates),
            snapshot=snapshot,
            review_id=f"review-{index}",
            reviewer_id=role,
        )
        for index, role in enumerate(task.case.specialist_ids)
    )
    review = ResearchReviewRun(
        review_id="benchmark-review",
        snapshot=snapshot,
        reviewer_ids=task.case.specialist_ids,
        started_sequence=1,
        status="assessed",
        submissions=submissions,
        assessment=gateway.assess_research_review(snapshot, submissions),
    )
    result = verify_planning_review(task, review)
    expected = not substitute and propose == (case_id == "plan-outcome-leakage")
    assert result.status == ("passed" if expected else "failed")
    assert result.check_id == "review.findings"
    assert review.assessment is not None
    if propose:
        assert len(review.assessment.findings) == 1
        assert len(review.assessment.findings[0].sources) == 2
    for changed in (
        replace(task, instruction="Different task"),
        replace(task, inputs={**task.inputs, "plan.json": "{}"}),
        replace(task, case=task.case.model_copy(update={"fixture_digest": "a" * 64})),
    ):
        with pytest.raises(ValueError, match="unchanged maintained case"):
            verify_planning_review(changed, review)
    pending = ResearchReviewRun(
        review_id="pending-review",
        snapshot=snapshot,
        reviewer_ids=task.case.specialist_ids,
        started_sequence=1,
    )
    assert verify_planning_review(task, pending).status == "failed"
    duplicated = (*submissions, submissions[0].model_copy(update={"review_id": "extra-task"}))
    duplicate_review = review.model_copy(
        update={
            "submissions": duplicated,
            "assessment": gateway.assess_research_review(snapshot, duplicated),
        }
    )
    assert verify_planning_review(task, duplicate_review).status == "failed"
    single_review = review.model_copy(
        update={
            "reviewer_ids": (submissions[0].reviewer_id,),
            "submissions": submissions[:1],
            "assessment": gateway.assess_research_review(snapshot, submissions[:1]),
        }
    )
    assert verify_planning_review(task, single_review).status == "failed"
    if propose and expected:
        evaluator = ResearchReviewEvaluator(WorkspaceInspector(gateway.project))
        correction = evaluator.prepare_correction(review, output_directory="corrected")
        assert tuple(item.artifact_id for item in correction.outputs) == ("plan",)
        valid = next(item for item in planning_review_tasks() if item.case.case_id == "plan-valid")
        (tmp_path / "corrected").mkdir()
        for output in correction.outputs:
            (tmp_path / output.path).write_text(valid.inputs["plan.json"])
        corrected = evaluator.assess_correction(review, correction)
        assert all(check.status == "not_observed" for check in corrected.checks)
        assert {name: (tmp_path / name).read_text() for name in task.inputs} == dict(task.inputs)
        (tmp_path / "data.csv").write_text(task.inputs["data.csv"] + "\n")
        with pytest.raises(ValueError, match="changed"):
            evaluator.assess_correction(review, correction)


def test_plan_review_suite_keeps_positive_and_negative_controls_bound() -> None:
    tasks = planning_review_tasks()
    suite = planning_review_suite()
    assert tuple(task.case for task in tasks) == suite.cases
    assert len(suite.cases) == 2
    assert suite.cases[0].fixture_digest != suite.cases[1].fixture_digest
    assert suite.fingerprint == planning_review_suite().fingerprint
    assert tasks[0].inputs["data.csv"] == tasks[1].inputs["data.csv"]
    assert tasks[0].inputs["dictionary.json"] == tasks[1].inputs["dictionary.json"]
    assert tasks[0].instruction == tasks[1].instruction
    assert all(task.case.review_stage_id == "plan" for task in tasks)
