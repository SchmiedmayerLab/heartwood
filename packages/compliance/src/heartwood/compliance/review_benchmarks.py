# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Pinned plan-review cases with a negative control and independent finding checks."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from types import MappingProxyType

from heartwood.compliance.research import ResearchTask, research_tasks
from heartwood.core_adapter.research_review import assess_research_review
from heartwood.core_adapter.research_workflows import research_workflow
from heartwood.model_policy.parallel_reviews import PARALLEL_REVIEW_CHECKS
from heartwood.schemas.evaluation import (
    EvaluationCase,
    EvaluationCheck,
    EvaluationDimension,
    EvaluationSuite,
    RequiredEvaluationCheck,
)
from heartwood.schemas.research import AnalysisPlan
from heartwood.schemas.review import ResearchReviewRun, review_digest

_INPUT_ROLES = MappingProxyType(
    {"data": "data.csv", "dictionary": "dictionary.json", "plan": "plan.json"}
)
_EXPECTED: Mapping[str, frozenset[str]] = MappingProxyType(
    {"plan-valid": frozenset(), "plan-outcome-leakage": frozenset({"analysis-plan-incompatible"})}
)


def planning_review_tasks() -> tuple[ResearchTask, ...]:
    """Reuse the pinned baseline data; vary only the plan supplied for advisory review."""
    baseline = next(task for task in research_tasks() if task.case.case_id == "baseline-analysis")
    stage = research_workflow("baseline-analysis").stage("plan")
    plan = AnalysisPlan(
        question="Does measurement predict response for held-out subjects?",
        estimand="Held-out visit prediction error",
        outcome="response",
        features=["measurement"],
        group_column="subject_id",
        split_column="partition",
        assumptions=["Prespecified subject-disjoint split"],
        limitations=["Small synthetic sample; no clinical or general scientific validity"],
    )
    instruction = (
        "Review the supplied baseline analysis plan against its data and dictionary. "
        "Use the workflow's selected advisory specialists and structured review proposals. "
        "Do not modify files, correct the plan, advance the workflow, access other files, "
        "or use the network. Report unsupported concerns as limitations, not verified defects."
    )
    checks = {
        "review.connectivity": EvaluationDimension.CONNECTIVITY,
        "review.artifacts": EvaluationDimension.ARTIFACT_COMPLETENESS,
        "review.structured-results": EvaluationDimension.CODING_CORRECTNESS,
        **PARALLEL_REVIEW_CHECKS,
    }
    cases = []
    for case_id, expected in _EXPECTED.items():
        selected = plan.model_copy(update={"features": ["future_response"]}) if expected else plan
        inputs = {**baseline.inputs, "plan.json": selected.model_dump_json() + "\n"}
        cases.append(
            ResearchTask(
                case=EvaluationCase(
                    case_id=case_id,
                    workflow_id="baseline-analysis",
                    review_stage_id=stage.stage_id,
                    specialist_ids=stage.specialist_ids,
                    fixture_digest=review_digest(
                        {
                            "inputs": inputs,
                            "instruction": instruction,
                            "expected_conditions": sorted(expected),
                        }
                    ),
                    required_checks=tuple(
                        RequiredEvaluationCheck(check_id=key, dimension=value)
                        for key, value in checks.items()
                    ),
                ),
                inputs=MappingProxyType(inputs),
                instruction=instruction,
                artifact_paths=tuple(_INPUT_ROLES.values()),
            )
        )
    return tuple(cases)


def planning_review_suite() -> EvaluationSuite:
    """Include both detection and false-positive control in route qualification."""
    return EvaluationSuite(
        suite_id="heartwood.synthetic-plan-review.v1",
        cases=tuple(task.case for task in planning_review_tasks()),
    )


def verify_planning_review(task: ResearchTask, review: ResearchReviewRun) -> EvaluationCheck:
    """Score exact maintained evidence, including missed defects and false accusations.

    This establishes only the finding check. It does not establish model execution,
    approval, isolation, scheduling, synthesis, replay, usage, or route qualification.
    """
    if task not in planning_review_tasks():
        raise ValueError("Plan review requires an unchanged maintained case")
    review = ResearchReviewRun.model_validate(review)
    observed = {role: task.inputs[path] for role, path in _INPUT_ROLES.items()}
    expected = _EXPECTED[task.case.case_id]
    actual = assess_research_review(review.snapshot, review.submissions, observed=observed)
    passed = (
        review.status == "assessed"
        and review.assessment == actual
        and set(review.reviewer_ids) == set(task.case.specialist_ids)
        and len(review.submissions) == len(task.case.specialist_ids)
        and {item.artifact_id for item in review.snapshot.artifacts} == set(_INPUT_ROLES)
        and all(finding.verification == "verified" for finding in actual.findings)
        and {finding.condition for finding in actual.findings} == expected
    )
    # Empty proposals cannot conceal substituted files in the negative control.
    if passed:
        encoded = {role: text.encode() for role, text in observed.items()}
        passed = all(
            artifact.file.sha256 == hashlib.sha256(encoded[artifact.artifact_id]).hexdigest()
            and artifact.file.size_bytes == len(encoded[artifact.artifact_id])
            for artifact in review.snapshot.artifacts
        )
    return EvaluationCheck(
        check_id="review.findings",
        dimension=PARALLEL_REVIEW_CHECKS["review.findings"],
        status="passed" if passed else "failed",
    )
