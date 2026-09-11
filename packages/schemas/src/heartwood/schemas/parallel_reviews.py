# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Measured eligibility for concurrent advisory reviews, separate from researcher consent."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, TypeAdapter, model_validator

from heartwood.schemas.evaluation import (
    EvaluationAssessment,
    EvaluationConfiguration,
    EvaluationIdentifier,
    EvaluationPolicy,
    EvaluationReason,
    EvaluationRecord,
    EvaluationRun,
    EvaluationSuite,
    Sha256,
)
from heartwood.schemas.execution import ExecutionBudget


class ParallelReviewPolicy(EvaluationPolicy):
    """A non-regression gate with an explicit latency benefit and consumption allowance."""

    minimum_latency_reduction: float = Field(default=0.1, gt=0, lt=1)
    maximum_cost_ratio: float = Field(default=1.25, ge=1)
    maximum_token_ratio: float = Field(default=1.25, ge=1)
    maximum_workers: int = Field(default=8, ge=2, le=16, strict=True)


class ParallelReviewComparison(EvaluationRecord):
    """Matched-case latency medians and total consumption; unknown is not a measured zero."""

    case_id: EvaluationIdentifier
    sequential_seconds: float = Field(gt=0)
    parallel_seconds: float = Field(gt=0)
    sequential_reported_cost_usd: float = Field(ge=0)
    parallel_reported_cost_usd: float = Field(ge=0)
    sequential_tokens: int = Field(ge=0)
    parallel_tokens: int = Field(ge=0)


class ReviewQualification(EvaluationRecord):
    """Operator-supplied retained evidence for one route and workflow review stage."""

    suite: EvaluationSuite
    case_id: EvaluationIdentifier
    sequential: EvaluationConfiguration
    parallel: EvaluationConfiguration
    policy: ParallelReviewPolicy = Field(default_factory=ParallelReviewPolicy)
    runs: tuple[EvaluationRun, ...] = Field(min_length=6, max_length=1000)


class ReviewQualifications(EvaluationRecord):
    """Deployment input, not a project setting or a self-authenticating success claim."""

    schema_version: Literal["heartwood.review-qualifications.v1"] = (
        "heartwood.review-qualifications.v1"
    )
    routes: tuple[ReviewQualification, ...] = Field(min_length=1, max_length=32)


class ParallelReviewAssessment(EvaluationRecord):
    """Recomputed comparison over retained trials; not authority to dispatch or approve work."""

    sequential: EvaluationAssessment
    parallel: EvaluationAssessment
    policy: ParallelReviewPolicy
    comparisons: tuple[ParallelReviewComparison, ...]
    qualified: bool
    reasons: tuple[EvaluationReason, ...]


class ReviewExecutionScope(EvaluationRecord):
    """The exact session work a researcher may authorize, not a standing permission."""

    project_fingerprint: Sha256
    session_id: EvaluationIdentifier
    workflow_run_id: EvaluationIdentifier
    workflow_id: EvaluationIdentifier
    stage_id: EvaluationIdentifier
    revision: int = Field(ge=0, strict=True)
    snapshot_fingerprint: Sha256
    reviewer_ids: tuple[EvaluationIdentifier, ...] = Field(min_length=2, max_length=16)
    workers: int = Field(ge=2, le=16, strict=True)
    budget: ExecutionBudget

    @model_validator(mode="after")
    def distinct_reviewers(self) -> Self:
        """Every worker must correspond to a distinct requested advisory role."""
        if len(set(self.reviewer_ids)) != len(self.reviewer_ids):
            raise ValueError("Reviewers must be distinct")
        if self.workers > len(self.reviewer_ids):
            raise ValueError("Worker count exceeds the selected reviewers")
        return self


class ReviewQualificationEvidence(EvaluationRecord):
    """Bind each retained result's content, not just its mutable filename or identity."""

    run_id: UUID
    record_fingerprint: Sha256


class ParallelReviewPlan(EvaluationRecord):
    """Stable preview identity; the gateway still journals consent and rechecks dispatch."""

    purpose: Literal["qualified-review"] = "qualified-review"
    scope: ReviewExecutionScope
    suite_fingerprint: Sha256
    case_id: EvaluationIdentifier
    sequential_configuration_fingerprint: Sha256
    parallel_configuration_fingerprint: Sha256
    policy: ParallelReviewPolicy
    evidence: tuple[ReviewQualificationEvidence, ...] = Field(min_length=6)
    valid_until: AwareDatetime


class ParallelReviewTrialPlan(EvaluationRecord):
    """One experimental benchmark admission, never evidence of a qualified route."""

    purpose: Literal["qualification-trial"] = "qualification-trial"
    scope: ReviewExecutionScope
    suite_fingerprint: Sha256
    case_id: EvaluationIdentifier
    trial_id: UUID
    reservation_fingerprint: Sha256
    seed: int = Field(ge=0, strict=True)
    configuration_fingerprint: Sha256
    runtime_fingerprint: Sha256
    valid_until: AwareDatetime


type ReviewExecutionPlan = Annotated[
    ParallelReviewPlan | ParallelReviewTrialPlan, Field(discriminator="purpose")
]
_REVIEW_EXECUTION_PLAN: TypeAdapter[ReviewExecutionPlan] = TypeAdapter(ReviewExecutionPlan)


def parse_review_execution_plan(value: object) -> ReviewExecutionPlan:
    """Revalidate the closed plan variants at persistence and native adapter boundaries."""
    return _REVIEW_EXECUTION_PLAN.validate_python(value)


class ReviewDispatchAction(EvaluationRecord):
    """Native action identity and content digest recorded before advisory dispatch."""

    event_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    reviewer_id: EvaluationIdentifier
    action_fingerprint: Sha256
