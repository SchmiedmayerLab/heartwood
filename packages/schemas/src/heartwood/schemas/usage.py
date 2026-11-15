# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Content-free project usage summary for pilot evaluation."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ActionSetCounts",
    "ExperimentRunCounts",
    "ProjectUsageCounts",
    "ProjectUsageSummary",
    "ReproductionCounts",
    "WorkflowRunCounts",
]

_Count = Annotated[int, Field(ge=0, strict=True)]


class _UsageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ActionSetCounts(_UsageRecord):
    """Recorded decisions on proposed action sets."""

    allowed: _Count
    rejected: _Count


class WorkflowRunCounts(_UsageRecord):
    """Research workflow runs by their latest recorded phase."""

    ready: _Count
    running: _Count
    review: _Count
    blocked: _Count
    completed: _Count
    cancelled: _Count


class ReproductionCounts(_UsageRecord):
    """Reviewed reproduction executions by recorded outcome."""

    succeeded: _Count
    failed: _Count
    unsettled: _Count = Field(description="Prepared reproductions without a recorded outcome.")


class ExperimentRunCounts(_UsageRecord):
    """Experiment records by their current status."""

    started: _Count
    interrupted: _Count
    resumed: _Count
    succeeded: _Count
    failed: _Count
    cancelled: _Count


class ProjectUsageCounts(_UsageRecord):
    """Counts and outcomes derived from verified session and experiment records."""

    sessions: _Count
    turns: _Count = Field(
        description="Agent turns started by a researcher message or a workflow stage."
    )
    action_sets: ActionSetCounts
    workflow_runs: WorkflowRunCounts
    reproductions: ReproductionCounts
    experiment_runs: ExperimentRunCounts


class ProjectUsageSummary(_UsageRecord):
    """Project evidence without prompts, paths, file names, model output or record identifiers.

    The summary identifier is a random value created with the project state; it links
    summaries from one project without identifying the project, a person or a session.
    A failed audit verification, or a history left incomplete by an interrupted commit,
    reports no counts rather than partial counts.
    """

    schema_version: Literal["heartwood.project-usage-summary.v1"] = (
        "heartwood.project-usage-summary.v1"
    )
    summary_id: str = Field(
        pattern=r"^[0-9a-f]{32}$",
        json_schema_extra={"pattern": r"^[0-9a-f]{32}(?![\s\S])"},
        description="Random per-project summary identifier.",
    )
    release_version: str = Field(
        pattern=r"^[0-9A-Za-z.+-]{1,64}$",
        json_schema_extra={"pattern": r"^[0-9A-Za-z.+-]{1,64}(?![\s\S])"},
    )
    platform_id: str = Field(
        pattern=r"^[a-z0-9-]{1,64}$",
        json_schema_extra={"pattern": r"^[a-z0-9-]{1,64}(?![\s\S])"},
    )
    audit_verification: Literal["verified", "incomplete", "failed"]
    counts: ProjectUsageCounts | None

    @model_validator(mode="after")
    def _counts_require_verification(self) -> Self:
        if (self.audit_verification == "verified") != (self.counts is not None):
            raise ValueError("Counts are reported only after the audit history verifies")
        return self
