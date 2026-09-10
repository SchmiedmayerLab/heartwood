# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Structured artifacts shared by research tasks, reviewers, and independent checks."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

type ResearchText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]
type Count = Annotated[int, Field(ge=0)]
type ErrorMetric = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ResearchArtifact(BaseModel):
    """Closed structured output, distinct from content-minimized audit evidence."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class ReadinessResult(ResearchArtifact):
    """Aggregate evidence for deciding whether a dataset is ready for analysis."""

    row_count: Count
    subject_count: Count
    duplicate_rows: Count
    missing_by_column: dict[str, Count]
    invalid_by_column: dict[str, Count]
    arm_counts: dict[str, Count]
    leakage_columns: list[ResearchText]
    ready_for_analysis: bool


class AnalysisPlan(ResearchArtifact):
    """Explicit research intent and split assumptions before fitting a baseline."""

    question: ResearchText
    estimand: ResearchText
    outcome: ResearchText
    features: list[ResearchText] = Field(min_length=1)
    group_column: ResearchText
    split_column: ResearchText
    assumptions: list[ResearchText] = Field(min_length=1)
    limitations: list[ResearchText] = Field(min_length=1)


class BaselineResult(ResearchArtifact):
    """Held-out univariate linear baseline metrics and group-omission sensitivity."""

    outcome: ResearchText
    features: list[ResearchText] = Field(min_length=1)
    n_train: int = Field(gt=0)
    n_test: int = Field(gt=0)
    train_subjects: int = Field(gt=0)
    test_subjects: int = Field(gt=0)
    intercept: float
    slope: float
    test_rmse: ErrorMetric
    test_mae: ErrorMetric
    test_r2: float
    mean_baseline_rmse: ErrorMetric
    sensitivity_rmse: dict[str, ErrorMetric]


class ResultVerification(ResearchArtifact):
    """Honest reproduction status, including discrepancies that were not repaired."""

    status: Literal["reproduced", "discrepancy"]
    matching_artifacts: list[ResearchText]
    mismatched_artifacts: list[ResearchText]
