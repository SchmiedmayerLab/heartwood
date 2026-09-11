# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Measured eligibility for concurrent advisory reviews, separate from researcher consent."""

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from heartwood.schemas.evaluation import (
    EvaluationAssessment,
    EvaluationIdentifier,
    EvaluationPolicy,
    EvaluationReason,
    EvaluationRecord,
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

    scope: ReviewExecutionScope
    suite_fingerprint: Sha256
    case_id: EvaluationIdentifier
    sequential_configuration_fingerprint: Sha256
    parallel_configuration_fingerprint: Sha256
    policy: ParallelReviewPolicy
    evidence: tuple[ReviewQualificationEvidence, ...] = Field(min_length=6)
    valid_until: AwareDatetime


class ReviewDispatchAction(EvaluationRecord):
    """Native action identity and content digest recorded before advisory dispatch."""

    event_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    reviewer_id: EvaluationIdentifier
    action_fingerprint: Sha256
