# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Gateway projection of research plans, platform estimates and budget observations.

Every interface renders this one projection. It links platform records to the plan
revisions and researcher decisions they belong to; it is not an accounting record, and
a reported estimate or balance in it never guarantees that the platform admits work.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from heartwood.schemas._planning_contract import (
    BudgetObservation,
    EstimatedCost,
    PlanningOverview,
    PlanningReference,
)
from heartwood.schemas._platform_metadata import PlatformLaunchContext

__all__ = [
    "ElementReview",
    "EstimateComparison",
    "EstimateState",
    "ObservationEvidence",
    "PlanProjection",
    "PlanRevision",
    "PlanningAgentTools",
    "PlanningEstimateRequest",
    "PlanningOutcome",
    "PlanningProjection",
    "PlanningServiceStatus",
    "PlanningServices",
]

_Text = Annotated[str, Field(min_length=1, pattern=r"\S")]

type PlanningServiceStatus = Literal[
    "unchecked", "available", "not-offered", "unavailable", "refused"
]


class _PlanningView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PlanningEstimateRequest(_PlanningView):
    """Ask the platform to estimate a recorded prediction, whole or for selected elements."""

    prediction: PlanningReference
    scope: Literal["plan", "elements"] = "plan"
    element_ids: tuple[_Text, ...] = Field(
        default=(), description="Elements to estimate; required for, and only for, elements scope."
    )


class PlanningServices(_PlanningView):
    """What the platform's planning service offered when Heartwood last asked."""

    budgets: PlanningServiceStatus
    estimates: PlanningServiceStatus
    checked_at: AwareDatetime | None = None


class PlanningAgentTools(_PlanningView):
    """Whether a session's agent can call the planning tools, reported like research services."""

    availability: Literal["available", "unavailable"]
    unavailable_reason: _Text | None = None
    unavailable_code: _Text | None = None

    @model_validator(mode="after")
    def _reason_with_unavailability(self) -> Self:
        unavailable = self.availability == "unavailable"
        if unavailable != (self.unavailable_reason is not None) or unavailable != (
            self.unavailable_code is not None
        ):
            raise ValueError("unavailable agent tools need a code and reason, available none")
        return self


class PlanningOutcome(_PlanningView):
    """A planning condition with its stable diagnostic and the record it concerns."""

    code: _Text
    title: _Text
    next_action: _Text
    subject: _Text = Field(description="The service, estimate, budget or operation concerned.")
    diagnostic: _Text


class ElementReview(_PlanningView):
    """Review state of one plan element; researcher approval is not platform authorization."""

    element_id: _Text
    kind: Literal["exploration", "batch", "inference"]
    runtime_profile_ref: _Text
    review: Literal["approved", "denied", "unchanged-since-approval", "requires-review"]
    approved_in: PlanningReference | None = Field(
        default=None, description="Prediction revision whose reviewed proposal was approved."
    )
    platform: Literal["authorized", "denied", "pending", "unknown", "revoked", "not-requested"]


class EstimateState(_PlanningView):
    """Freshness of one platform estimate as of the projection time."""

    estimate_id: _Text
    state: Literal["current", "expired"]
    budget_changed: bool = Field(
        description="A budget observed after the estimate differs from the one before it."
    )


class ObservationEvidence(_PlanningView):
    """A citation for an immutable platform observation in this launch context."""

    reference: str
    operation_id: _Text
    service_id: _Text
    backend_operation_ref: _Text
    observed_at: AwareDatetime
    status: _Text
    platform_record_ref: _Text


class PlanRevision(_PlanningView):
    """One prediction revision with its linked platform records and review states."""

    observation_evidence: tuple[ObservationEvidence, ...] = ()
    overview: PlanningOverview
    elements: tuple[ElementReview, ...]
    estimates: tuple[EstimateState, ...]


class EstimateComparison(_PlanningView):
    """One complete-plan estimate for comparing revisions and runtime configurations."""

    prediction: PlanningReference
    estimate_id: _Text
    runtime_profile_refs: tuple[_Text, ...]
    total: EstimatedCost
    state: Literal["current", "expired"]
    comparable: bool = Field(description="Uses the denomination of the plan's first estimate.")


class PlanProjection(_PlanningView):
    """All recorded revisions of one plan."""

    plan_id: _Text
    revisions: tuple[PlanRevision, ...]
    comparison: tuple[EstimateComparison, ...]


class PlanningProjection(_PlanningView):
    """Plans, estimates, budgets and outcomes, the same for every interface."""

    schema_version: Literal["heartwood.planning-projection.v1"] = "heartwood.planning-projection.v1"
    offered: bool = Field(description="Whether this deployment connects a planning service.")
    launch: PlatformLaunchContext | None = None
    services: PlanningServices | None = None
    agent_tools: PlanningAgentTools | None = Field(
        default=None, description="Agent planning tool availability; absent when not offered."
    )
    budgets: tuple[BudgetObservation, ...] = Field(
        default=(), description="The latest observation of each platform budget."
    )
    plans: tuple[PlanProjection, ...] = ()
    outcomes: tuple[PlanningOutcome, ...] = ()
