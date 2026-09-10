# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Content-minimized contracts for reproducible research evaluations."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

type EvaluationIdentifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,255}$")
]
type Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
type EvaluationReason = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,1023}$")
]
type NonnegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class EvaluationDimension(StrEnum):
    """Independent claims that must not be collapsed into a single success flag."""

    CONNECTIVITY = "connectivity"
    TOOL_COMPATIBILITY = "tool_compatibility"
    WORKFLOW_COMPLETION = "workflow_completion"
    ARTIFACT_COMPLETENESS = "artifact_completeness"
    CODING_CORRECTNESS = "coding_correctness"
    STATISTICAL_CORRECTNESS = "statistical_correctness"
    POLICY_ADHERENCE = "policy_adherence"
    RECOVERY = "recovery"


class EvaluationRecord(BaseModel):
    """Immutable, closed evidence structure with no prompt or tool-output fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    @property
    def fingerprint(self) -> str:
        """Bind evidence to all record fields using canonical JSON."""
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EvaluationConfiguration(EvaluationRecord):
    """Exact model, runtime, and platform scope of a measurement."""

    provider: EvaluationIdentifier
    model: EvaluationIdentifier
    model_revision: EvaluationIdentifier | None
    platform: EvaluationIdentifier
    hardware: tuple[EvaluationIdentifier, ...] = Field(min_length=1)
    runtime: EvaluationIdentifier
    openhands_version: EvaluationIdentifier
    driver: EvaluationIdentifier | None = None
    cuda: EvaluationIdentifier | None = None
    precision: EvaluationIdentifier
    context_tokens: int = Field(gt=0)
    output_tokens: int = Field(gt=0)
    tensor_parallelism: int = Field(default=1, gt=0)
    tool_parser: EvaluationIdentifier
    skill_tree_digest: Sha256
    harness_revision: Sha256


class EvaluationCheck(EvaluationRecord):
    """An independently checked result, without free-form findings or data."""

    check_id: EvaluationIdentifier
    dimension: EvaluationDimension
    status: Literal["passed", "failed", "not_run"]


class RequiredEvaluationCheck(EvaluationRecord):
    """Check identity and meaning pinned by a maintained case definition."""

    check_id: EvaluationIdentifier
    dimension: EvaluationDimension


class EvaluationCase(EvaluationRecord):
    """Immutable case contract shared with workflow and platform acceptance."""

    case_id: EvaluationIdentifier
    workflow_id: EvaluationIdentifier
    fixture_digest: Sha256
    required_checks: tuple[RequiredEvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_checks(self) -> Self:
        """Reject ambiguous check identities in a case contract."""
        identifiers = [check.check_id for check in self.required_checks]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Evaluation check identifiers must be unique")
        return self


class EvaluationSuite(EvaluationRecord):
    """Pinned cases and the checks necessary to make a research-quality claim."""

    schema_version: Literal["heartwood.evaluation-suite.v1"] = "heartwood.evaluation-suite.v1"
    suite_id: EvaluationIdentifier
    cases: tuple[EvaluationCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def complete_dimensions(self) -> Self:
        """Require distinct cases and coverage of every research evidence dimension."""
        identifiers = [case.case_id for case in self.cases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Evaluation case identifiers must be unique")
        dimensions = {check.dimension for case in self.cases for check in case.required_checks}
        if dimensions != set(EvaluationDimension):
            raise ValueError("Evaluation suite must cover every research evidence dimension")
        return self


class EvaluationUsage(EvaluationRecord):
    """Measured efficiency; unavailable usage and cost remain unknown, not zero."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    model_calls: int | None = Field(default=None, ge=0)
    reported_cost_usd: NonnegativeFloat | None = None
    elapsed_seconds: NonnegativeFloat


class EvaluationBudget(EvaluationRecord):
    """Observed admission limits for a benchmark, not a provider-side spending cap."""

    maximum_seconds: float = Field(default=300, gt=0, le=3600)
    maximum_model_calls: int = Field(default=20, gt=0, le=100)
    maximum_tokens: int = Field(default=100_000, gt=0)
    maximum_reported_cost_usd: float = Field(default=1, gt=0)
    maximum_actions: int = Field(default=30, gt=0, le=100)


class EvaluationRun(EvaluationRecord):
    """One dated trial of a pinned case against an exact runtime configuration."""

    schema_version: Literal["heartwood.evaluation-run.v1"] = "heartwood.evaluation-run.v1"
    run_id: UUID
    suite_id: EvaluationIdentifier
    suite_fingerprint: Sha256
    case_id: EvaluationIdentifier
    fixture_digest: Sha256
    seed: int = Field(ge=0)
    execution: Literal["deterministic", "live_model"]
    configuration: EvaluationConfiguration
    started_at: AwareDatetime
    finished_at: AwareDatetime
    checks: tuple[EvaluationCheck, ...]
    usage: EvaluationUsage
    budget: EvaluationBudget = Field(default_factory=EvaluationBudget)

    @model_validator(mode="after")
    def validate_trial(self) -> Self:
        """Reject backwards time and duplicate check identities."""
        if self.finished_at < self.started_at:
            raise ValueError("Evaluation cannot finish before it starts")
        identifiers = [check.check_id for check in self.checks]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Evaluation check identifiers must be unique")
        return self


class EvaluationPolicy(EvaluationRecord):
    """Explicit repeat and freshness requirements for a deployment's evidence."""

    minimum_repeats: int = Field(default=3, ge=3)
    maximum_age_days: int = Field(default=30, gt=0)


class EvaluationAssessment(EvaluationRecord):
    """A reproducible evidence decision, not an automatic model-catalog promotion."""

    configuration_fingerprint: Sha256
    suite_fingerprint: Sha256
    assessed_at: AwareDatetime
    policy: EvaluationPolicy
    qualified: bool
    reasons: tuple[EvaluationReason, ...]
    evidence_run_ids: tuple[UUID, ...]
