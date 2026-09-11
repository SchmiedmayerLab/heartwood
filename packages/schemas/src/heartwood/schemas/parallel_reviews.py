# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Measured eligibility for concurrent advisory reviews, separate from researcher consent."""

from pydantic import Field

from heartwood.schemas.evaluation import (
    EvaluationAssessment,
    EvaluationIdentifier,
    EvaluationPolicy,
    EvaluationReason,
    EvaluationRecord,
)


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
