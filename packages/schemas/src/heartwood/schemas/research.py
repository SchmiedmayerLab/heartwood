# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Structured artifacts shared by research tasks, reviewers, and independent checks."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

type ResearchText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]
type Count = Annotated[int, Field(ge=0)]
type ErrorMetric = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ResearchArtifact(BaseModel):
    """Closed structured output, distinct from content-minimized audit evidence."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class ResearchSplit(ResearchArtifact):
    """Declared held-out partition; group overlap is checked against the data."""

    column: ResearchText
    train: ResearchText
    test: ResearchText
    keep_subjects_together: Literal[True] = True

    @model_validator(mode="after")
    def distinct_labels(self) -> Self:
        """Prevent one partition label from serving both roles."""
        if self.train == self.test:
            raise ValueError("Training and test partitions must differ")
        return self


class ColumnValidity(ResearchArtifact):
    """Explicit numeric bounds or permitted categorical values."""

    minimum: float | None = None
    maximum: float | None = None
    values: list[ResearchText] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def ordered_bounds(self) -> Self:
        """Reject contradictory numeric validity rules."""
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Column bounds must be ordered")
        return self


class ResearchDictionary(ResearchArtifact):
    """Data roles used by the maintained tabular research checks."""

    grouping_key: ResearchText
    primary_key: list[ResearchText] = Field(min_length=1, max_length=16)
    outcome: ResearchText
    permitted_predictors: list[ResearchText] = Field(min_length=1, max_length=128)
    excluded_predictors: dict[ResearchText, ResearchText] = Field(default_factory=dict)
    split: ResearchSplit
    validity: dict[ResearchText, ColumnValidity] = Field(default_factory=dict)
    arm_column: ResearchText | None = None
    unit_of_analysis: ResearchText | None = None
    notes: ResearchText | None = None
    synthetic: bool | None = None

    @model_validator(mode="after")
    def coherent_roles(self) -> Self:
        """Require unambiguous identities and explicitly permitted predictors."""
        if len({self.grouping_key, self.outcome, self.split.column}) != 3:
            raise ValueError("Group, outcome, and partition columns must differ")
        for names in (self.primary_key, self.permitted_predictors):
            if len(names) != len(set(names)):
                raise ValueError("Data dictionary columns must be unique")
        forbidden = {self.outcome, self.grouping_key, self.split.column, *self.excluded_predictors}
        if forbidden.intersection(self.permitted_predictors):
            raise ValueError("Predictors cannot include outcomes, groups, splits, or exclusions")
        if self.grouping_key not in self.primary_key:
            raise ValueError("The primary key must include the grouping key")
        return self


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
    outcome: ResearchText = Field(description="Exact outcome column name from the data dictionary.")
    features: list[ResearchText] = Field(
        min_length=1,
        description="Exact predictor column names only, without prose, backticks, or explanations.",
    )
    group_column: ResearchText = Field(
        description="Exact grouping column name from the dictionary."
    )
    split_column: ResearchText = Field(
        description="Exact partition column name from the dictionary."
    )
    assumptions: list[ResearchText] = Field(min_length=1)
    limitations: list[ResearchText] = Field(min_length=1)


class BaselineResult(ResearchArtifact):
    """Held-out univariate linear baseline metrics and group-omission sensitivity."""

    outcome: ResearchText = Field(description="Exact outcome column name used by the fitted model.")
    features: list[ResearchText] = Field(
        min_length=1,
        description="Exact predictor column names used by the fitted model, without explanations.",
    )
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
