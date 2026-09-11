# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Synthetic authorization supplied by tests, never deployment qualification evidence."""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import pytest

from heartwood.schemas.execution import ExecutionBudget
from heartwood.schemas.parallel_reviews import (
    ParallelReviewPlan,
    ParallelReviewPolicy,
    ReviewExecutionScope,
    ReviewQualificationEvidence,
)


@pytest.fixture
def parallel_review_plan() -> ParallelReviewPlan:
    return ParallelReviewPlan(
        scope=ReviewExecutionScope(
            project_fingerprint="1" * 64,
            session_id="session-1",
            workflow_run_id="synthetic-review",
            workflow_id="baseline-analysis",
            stage_id="verification",
            revision=1,
            snapshot_fingerprint="2" * 64,
            reviewer_ids=("data-quality-reviewer", "statistical-reviewer"),
            workers=2,
            budget=ExecutionBudget(),
        ),
        suite_fingerprint="3" * 64,
        case_id="synthetic-review",
        sequential_configuration_fingerprint="4" * 64,
        parallel_configuration_fingerprint="5" * 64,
        policy=ParallelReviewPolicy(),
        evidence=tuple(
            ReviewQualificationEvidence(
                run_id=uuid5(NAMESPACE_URL, f"synthetic-admission:{index}"),
                record_fingerprint=f"{index:064x}",
            )
            for index in range(6)
        ),
        valid_until=datetime(2026, 10, 1, tzinfo=UTC),
    )
