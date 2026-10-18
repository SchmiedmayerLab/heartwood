# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Draft planning messages; validation establishes consistency, never spending authority.

The platform authenticates every observation and authorizes every operation independently.
No record here calculates provider prices, reserves funds, or enforces a spending limit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
Revision = Annotated[int, Field(ge=1)]
Sha256 = Annotated[
    str,
    Field(pattern=r"^[a-f0-9]{64}$", json_schema_extra={"pattern": r"^[a-f0-9]{64}(?![\s\S])"}),
]
DecimalAmount = Annotated[
    str,
    Field(
        pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$",
        json_schema_extra={"pattern": r"^(0|[1-9][0-9]*)(\.[0-9]+)?(?![\s\S])"},
        description="Finite nonnegative decimal string; no exponent, rounding, or binary float.",
    ),
]

SignedDecimalAmount = Annotated[
    str,
    Field(
        pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$",
        json_schema_extra={"pattern": r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?(?![\s\S])"},
        description="Finite decimal balance; negative availability reports an overrun.",
    ),
]


class _PlanningRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class PlanningReference(_PlanningRecord):
    """An immutable revision of a plan, prediction, or other project-local record."""

    record_id: Text
    revision: Revision


class WorkloadEvidence(_PlanningRecord):
    """Project-local evidence or researcher correction; absent measurements remain unknown."""

    evidence_id: Text
    project_context_id: Text
    kind: Literal["prior-run", "researcher-input", "sample-run", "correction"]
    reference: Text
    description: Text
    observed_at: AwareDatetime
    content_sha256: Sha256 | None = None


class WorkloadQuantity(_PlanningRecord):
    """A predicted quantity and uncertainty; units identify the actual measurement scale."""

    metric: Text
    unit: Text
    expected: DecimalAmount | None = None
    lower: DecimalAmount | None = None
    upper: DecimalAmount | None = None
    uncertainty: Text

    @model_validator(mode="after")
    def ordered_bounds(self) -> Self:
        """Reject inconsistent bounds without inventing missing values."""
        _ordered(self.lower, self.expected, self.upper)
        return self


class WorkloadElement(_PlanningRecord):
    """One separately estimable exploration, batch, or inference element."""

    element_id: Text
    kind: Literal["exploration", "batch", "inference"]
    description: Text
    runtime_profile_ref: Text
    quantities: tuple[WorkloadQuantity, ...]
    assumptions: tuple[Text, ...] = ()
    evidence_ids: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def unique_metrics(self) -> Self:
        """Keep each metric unambiguous within its element."""
        _unique([item.metric for item in self.quantities], "workload metrics")
        _unique(self.evidence_ids, "evidence references")
        return self


class WorkloadPrediction(_PlanningRecord):
    """Agent/researcher workload prediction, not a platform price or execution request."""

    schema_version: Literal["heartwood.workload-prediction.v1"] = "heartwood.workload-prediction.v1"
    project_context_id: Text
    prediction: PlanningReference
    plan: PlanningReference
    created_at: AwareDatetime
    elements: tuple[WorkloadElement, ...] = Field(min_length=1)
    assumptions: tuple[Text, ...] = ()
    uncertainty: Text
    evidence: tuple[WorkloadEvidence, ...] = ()
    supersedes: PlanningReference | None = None

    @model_validator(mode="after")
    def consistent_evidence(self) -> Self:
        """Keep evidence in the launch-bound project and revisions explicit."""
        _unique([item.element_id for item in self.elements], "workload elements")
        _unique([item.evidence_id for item in self.evidence], "evidence identities")
        evidence_ids = {item.evidence_id for item in self.evidence}
        if any(item.project_context_id != self.project_context_id for item in self.evidence):
            raise ValueError("prediction evidence must belong to the same project context")
        if any(not set(item.evidence_ids) <= evidence_ids for item in self.elements):
            raise ValueError("workload element references missing evidence")
        if self.supersedes is not None and (
            self.supersedes.record_id != self.prediction.record_id
            or self.supersedes.revision >= self.prediction.revision
        ):
            raise ValueError("supersedes must identify an earlier revision of this prediction")
        return self


class CostDenomination(_PlanningRecord):
    """Currency or platform allocation unit; never a resource measurement unit."""

    kind: Literal["currency", "allocation-unit"]
    unit: Text


class EstimatedCost(_PlanningRecord):
    """Platform-produced cost range; unknown is not zero and partial is not complete."""

    denomination: CostDenomination
    coverage: Literal["complete", "partial", "unknown"]
    expected: DecimalAmount | None = None
    lower: DecimalAmount | None = None
    upper: DecimalAmount | None = None
    limitations: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def consistent_coverage(self) -> Self:
        """Validate known amounts and their explicitly stated limitations."""
        _ordered(self.lower, self.expected, self.upper)
        known = any(value is not None for value in (self.expected, self.lower, self.upper))
        if self.coverage == "unknown" and known:
            raise ValueError("unknown cost cannot contain numeric amounts")
        if self.coverage != "unknown" and not known:
            raise ValueError("known or partial cost requires an amount")
        if self.coverage != "complete" and not self.limitations:
            raise ValueError("partial or unknown cost requires limitations")
        return self


class ElementEstimate(_PlanningRecord):
    """Element cost coverage; included components are breakdowns, not additional totals."""

    element_ids: tuple[Text, ...] = Field(min_length=1)
    cost: EstimatedCost
    included_in_total: bool

    @model_validator(mode="after")
    def unique_elements(self) -> Self:
        """Reject duplicate coverage within one component."""
        _unique(self.element_ids, "estimate element references")
        return self


class FocusEvidenceReference(_PlanningRecord):
    """Unmodified FOCUS pricing/cost evidence, retaining its version and draft revision."""

    specification_version: Text
    dataset: Text
    reference: Text
    draft_revision: Text | None = None
    content_sha256: Sha256 | None = None


class PlatformEstimate(_PlanningRecord):
    """Completed estimate result, identical for immediate and asynchronous requests."""

    schema_version: Literal["heartwood.platform-estimate.v1"] = "heartwood.platform-estimate.v1"
    project_context_id: Text
    estimate_id: Text
    scope: Literal["plan", "elements"]
    prediction: PlanningReference
    plan: PlanningReference
    created_at: AwareDatetime
    valid_until: AwareDatetime
    total: EstimatedCost
    covered_element_ids: tuple[Text, ...] = Field(min_length=1)
    element_estimates: tuple[ElementEstimate, ...] = ()
    pricing_assumptions: tuple[Text, ...] = ()
    exclusions: tuple[Text, ...] = ()
    focus_evidence: tuple[FocusEvidenceReference, ...] = ()

    @model_validator(mode="after")
    def consistent_total(self) -> Self:
        """Prevent ambiguous component coverage without calculating prices."""
        if self.valid_until <= self.created_at:
            raise ValueError("estimate validity must end after creation")
        _unique(self.covered_element_ids, "estimate coverage")
        components: list[str] = []
        for item in self.element_estimates:
            if item.cost.denomination != self.total.denomination:
                raise ValueError("estimate costs must use one denomination")
            if item.included_in_total:
                if not set(item.element_ids) <= set(self.covered_element_ids):
                    raise ValueError("included elements must belong to total coverage")
            elif set(item.element_ids) & set(self.covered_element_ids):
                raise ValueError("additional estimate components cannot overlap total coverage")
            components.extend(item.element_ids)
        _unique(components, "estimate components")
        if self.exclusions and self.total.coverage == "complete":
            raise ValueError("an estimate with exclusions cannot claim complete cost coverage")
        return self


class BackendRequestReference(_PlanningRecord):
    """Exact native request bytes; this record does not clone backend request schemas."""

    service_id: Text
    protocol: Text
    request_ref: Text
    request_sha256: Sha256


class OperationResourceLimit(_PlanningRecord):
    """A declared hard operation limit; enforcement belongs to the platform."""

    metric: Text
    unit: Text
    maximum: DecimalAmount


class ProposedOperation(_PlanningRecord):
    """One exact operation with its review-visible effects and native request binding."""

    operation_id: Text
    element_ids: tuple[Text, ...] = Field(min_length=1)
    description: Text
    resource_limits: tuple[OperationResourceLimit, ...] = ()
    data_scope: Text
    destination: Text
    estimate_ids: tuple[Text, ...] = ()
    request: BackendRequestReference

    @model_validator(mode="after")
    def unique_references(self) -> Self:
        """Keep operation coverage and estimate references unambiguous."""
        _unique(self.element_ids, "operation element references")
        _unique(self.estimate_ids, "operation estimate references")
        _unique([item.metric for item in self.resource_limits], "operation resource limits")
        return self


class OperationSetProposal(_PlanningRecord):
    """The exact JSON document reviewed by a researcher, not a reusable spending envelope."""

    schema_version: Literal["heartwood.operation-set-proposal.v1"] = (
        "heartwood.operation-set-proposal.v1"
    )
    project_context_id: Text
    proposal: PlanningReference
    plan: PlanningReference
    prediction: PlanningReference
    created_at: AwareDatetime
    operations: tuple[ProposedOperation, ...] = Field(min_length=1)
    estimates: tuple[PlatformEstimate, ...] = ()

    @model_validator(mode="after")
    def unique_operations(self) -> Self:
        """Reject repeated identities within the exact operation set."""
        _unique([item.operation_id for item in self.operations], "proposal operations")
        _unique([item.estimate_id for item in self.estimates], "proposal estimate snapshots")
        estimate_ids = {item.estimate_id for item in self.estimates}
        estimates_by_id = {item.estimate_id: item for item in self.estimates}
        if any(not set(item.estimate_ids) <= estimate_ids for item in self.operations):
            raise ValueError("proposal operations must reference embedded estimate snapshots")
        if any(
            item.project_context_id != self.project_context_id
            or item.plan != self.plan
            or item.prediction != self.prediction
            for item in self.estimates
        ):
            raise ValueError("proposal estimate snapshots must match its project and revisions")
        for operation in self.operations:
            applicable = [estimates_by_id[identity] for identity in operation.estimate_ids]
            scopes = [_estimate_element_ids(item) for item in applicable]
            if any(not scope & set(operation.element_ids) for scope in scopes):
                raise ValueError("operation estimates must cover relevant workload elements")
            covered = {element for scope in scopes for element in scope}
            if applicable and not set(operation.element_ids) <= covered:
                raise ValueError(
                    "operation estimates must represent every element; "
                    "use explicit partial or unknown component costs when needed"
                )
        return self


class ReviewedOperationSet(_PlanningRecord):
    """Parsed proposal plus the hash/reference of its exact immutable JSON bytes.

    Parsing this envelope does not verify referenced bytes. Use reviewed_operation_set
    with the actual reviewed document to establish its content binding.
    """

    proposal: OperationSetProposal
    document_ref: Text
    document_sha256: Sha256


def reviewed_operation_set(document: bytes, *, reference: str) -> ReviewedOperationSet:
    """Parse and hash exact reviewed bytes; no reserialization or canonicalization is used."""
    try:
        json.loads(document, object_pairs_hook=_unique_json_keys)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("reviewed proposal must be valid JSON with unique object keys") from None
    return ReviewedOperationSet(
        proposal=OperationSetProposal.model_validate_json(document),
        document_ref=reference,
        document_sha256=hashlib.sha256(document).hexdigest(),
    )


def verify_reviewed_operation_set(reviewed: ReviewedOperationSet, document: bytes) -> None:
    """Verify a stored envelope against exact reviewed bytes before trusting its content binding."""
    verified = reviewed_operation_set(document, reference=reviewed.document_ref)
    if (
        verified.document_sha256 != reviewed.document_sha256
        or verified.proposal != reviewed.proposal
    ):
        raise ValueError("reviewed proposal does not match its exact document bytes")


def _unique_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


class ResearcherDecision(_PlanningRecord):
    """Session-audited decision over exact proposal bytes; not platform authorization."""

    schema_version: Literal["heartwood.researcher-decision.v1"] = "heartwood.researcher-decision.v1"
    project_context_id: Text
    decision_id: Text
    proposal: PlanningReference
    document_sha256: Sha256
    decision: Literal["approved", "denied", "withdrawn"]
    decided_at: AwareDatetime
    actor_ref: Text
    audit_ref: Text


class BudgetObservation(_PlanningRecord):
    """Platform-reported snapshot, not a local ledger or a guarantee of future availability."""

    schema_version: Literal["heartwood.budget-observation.v1"] = "heartwood.budget-observation.v1"
    project_context_id: Text
    budget_id: Text
    observed_at: AwareDatetime
    denomination: CostDenomination
    allocated: DecimalAmount | None = None
    reserved: DecimalAmount | None = None
    spent: DecimalAmount | None = None
    available: SignedDecimalAmount | None = None
    constraints: tuple[Text, ...] = ()
    limitations: tuple[Text, ...] = ()
    platform_record_ref: Text

    @model_validator(mode="after")
    def explain_unknown(self) -> Self:
        """Keep missing accounting observations explicit."""
        if not self.limitations and any(
            value is None for value in (self.allocated, self.reserved, self.spent, self.available)
        ):
            raise ValueError("missing budget amounts require limitations")
        return self


class PlatformAuthorization(_PlanningRecord):
    """Observed platform decision/reservation; a parsed claim grants no actual authority."""

    schema_version: Literal["heartwood.platform-authorization.v1"] = (
        "heartwood.platform-authorization.v1"
    )
    project_context_id: Text
    authorization_id: Text
    proposal: PlanningReference
    document_sha256: Sha256
    status: Literal["authorized", "denied", "pending", "unknown", "revoked"]
    observed_at: AwareDatetime
    valid_until: AwareDatetime | None = None
    reservation_ref: Text | None = None
    budget_id: Text | None = None
    reason: Text
    platform_record_ref: Text

    @model_validator(mode="after")
    def valid_observation(self) -> Self:
        """Validate reservation context; expired authorizations remain representable."""
        if self.reservation_ref is not None and self.budget_id is None:
            raise ValueError("a reservation reference requires a budget identity")
        return self


class PlatformOperationObservation(_PlanningRecord):
    """Backend execution observation, separate from MCP task/request status and cancellation."""

    schema_version: Literal["heartwood.platform-operation-observation.v1"] = (
        "heartwood.platform-operation-observation.v1"
    )
    project_context_id: Text
    operation_id: Text
    service_id: Text
    backend_operation_ref: Text
    observed_at: AwareDatetime
    status: Literal[
        "submitted", "queued", "running", "completed", "failed", "cancelled", "denied", "unknown"
    ]
    platform_record_ref: Text
    limitations: tuple[Text, ...] = ()


class PlanningOverview(_PlanningRecord):
    """One project/plan revision's linked view; neither an accounting nor approval engine."""

    schema_version: Literal["heartwood.planning-overview.v1"] = "heartwood.planning-overview.v1"
    prediction: WorkloadPrediction
    estimates: tuple[PlatformEstimate, ...] = ()
    operation_sets: tuple[ReviewedOperationSet, ...] = ()
    researcher_decisions: tuple[ResearcherDecision, ...] = ()
    authorizations: tuple[PlatformAuthorization, ...] = ()
    budgets: tuple[BudgetObservation, ...] = ()
    operations: tuple[PlatformOperationObservation, ...] = ()

    @model_validator(mode="after")
    def consistent_links(self) -> Self:
        """Reject cross-project, stale-revision, changed-content, and dangling links."""
        prediction = self.prediction
        elements = {item.element_id for item in prediction.elements}
        _unique([item.estimate_id for item in self.estimates], "overview estimates")
        _unique([item.budget_id for item in self.budgets], "overview budgets")
        _unique([item.decision_id for item in self.researcher_decisions], "researcher decisions")
        _unique([item.authorization_id for item in self.authorizations], "platform authorizations")
        _unique([item.operation_id for item in self.operations], "operation observations")
        records: list[
            PlatformEstimate
            | ResearcherDecision
            | PlatformAuthorization
            | BudgetObservation
            | PlatformOperationObservation
            | OperationSetProposal
        ] = [
            *self.estimates,
            *self.researcher_decisions,
            *self.authorizations,
            *self.budgets,
            *self.operations,
        ]
        records.extend(item.proposal for item in self.operation_sets)
        if any(item.project_context_id != prediction.project_context_id for item in records):
            raise ValueError("overview records must share the launch-bound project context")
        planned_records: list[PlatformEstimate | OperationSetProposal] = [
            *self.estimates,
            *(item.proposal for item in self.operation_sets),
        ]
        for item in planned_records:
            if item.prediction != prediction.prediction or item.plan != prediction.plan:
                raise ValueError(
                    "overview records must match the exact plan and prediction revision"
                )
        for estimate in self.estimates:
            referenced = set(estimate.covered_element_ids)
            referenced.update(
                element for item in estimate.element_estimates for element in item.element_ids
            )
            if not referenced <= elements:
                raise ValueError("estimate references an unknown workload element")
            if (
                estimate.scope == "plan"
                and estimate.total.coverage == "complete"
                and set(estimate.covered_element_ids) != elements
            ):
                raise ValueError("complete plan estimate must cover every workload element")
        proposals = {
            (item.proposal.proposal.record_id, item.proposal.proposal.revision): item
            for item in self.operation_sets
        }
        if len(proposals) != len(self.operation_sets):
            raise ValueError("overview proposal revisions must be unique")
        estimate_ids = {item.estimate_id for item in self.estimates}
        estimates_by_id = {item.estimate_id: item for item in self.estimates}
        proposed_operations: dict[str, ProposedOperation] = {}
        for reviewed in self.operation_sets:
            if any(
                estimates_by_id.get(item.estimate_id) != item
                for item in reviewed.proposal.estimates
            ):
                raise ValueError("reviewed estimate snapshot differs from the overview estimate")
            for operation in reviewed.proposal.operations:
                if (
                    not set(operation.element_ids) <= elements
                    or not set(operation.estimate_ids) <= estimate_ids
                ):
                    raise ValueError("proposal references unknown elements or estimates")
                if operation.operation_id in proposed_operations:
                    raise ValueError("operation identity cannot be reused in another proposal")
                proposed_operations[operation.operation_id] = operation
        decisions: list[ResearcherDecision | PlatformAuthorization] = [
            *self.researcher_decisions,
            *self.authorizations,
        ]
        for decision in decisions:
            matched = proposals.get((decision.proposal.record_id, decision.proposal.revision))
            if matched is None or matched.document_sha256 != decision.document_sha256:
                raise ValueError("decision must bind the exact reviewed proposal document")
        budget_ids = {item.budget_id for item in self.budgets}
        if any(
            item.budget_id is not None and item.budget_id not in budget_ids
            for item in self.authorizations
        ):
            raise ValueError("authorization references a missing budget observation")
        for observed in self.operations:
            matched_operation = proposed_operations.get(observed.operation_id)
            if (
                matched_operation is None
                or matched_operation.request.service_id != observed.service_id
            ):
                raise ValueError("backend observation must match a proposed operation and service")
        return self


def _unique(values: Sequence[str], name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must be unique")


def _ordered(lower: str | None, expected: str | None, upper: str | None) -> None:
    present = [Decimal(value) for value in (lower, expected, upper) if value is not None]
    if present != sorted(present):
        raise ValueError("quantity or cost bounds must be ordered")


def _estimate_element_ids(estimate: PlatformEstimate) -> set[str]:
    # Included breakdowns already belong to total coverage; additional components
    # have their own independently validated cost coverage and limitations.
    return set(estimate.covered_element_ids) | {
        element
        for item in estimate.element_estimates
        if not item.included_in_total
        for element in item.element_ids
    }
