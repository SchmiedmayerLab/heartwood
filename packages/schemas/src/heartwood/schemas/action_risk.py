# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Synthetic action-risk inputs and content-minimized measurement records."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from heartwood.schemas.evaluation import (
    EvaluationIdentifier,
    EvaluationReason,
    EvaluationRecord,
    NonnegativeFloat,
    Sha256,
)

type RiskLevel = Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"]
type RiskCategory = Literal["benign", "ambiguous", "destructive", "encoded", "injected", "network"]


class ActionRiskProbe(EvaluationRecord):
    """A labeled synthetic action; evaluating it must never execute the action."""

    case_id: EvaluationIdentifier
    category: RiskCategory
    must_confirm: bool
    tool: Literal["terminal", "file_editor"]
    command: str = Field(min_length=1, max_length=4096)
    path: str | None = Field(default=None, max_length=4096)
    file_text: str | None = Field(default=None, max_length=4096)
    model_risk: RiskLevel = "LOW"

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        """Accept only supported typed action shapes, never executable callbacks."""
        if self.tool == "terminal":
            if self.path is not None or self.file_text is not None:
                raise ValueError("Terminal probes cannot contain file-editor fields")
        elif self.path is None or self.command not in ("view", "create"):
            raise ValueError("File probes require a path and a view or create command")
        elif (self.command == "create") != (self.file_text is not None):
            raise ValueError("Only file creation requires file_text")
        return self


class ActionRiskCorpus(EvaluationRecord):
    """Versioned inputs whose fingerprint binds labels, actions, and model ratings."""

    schema_version: Literal["heartwood.action-risk-corpus.v1"] = "heartwood.action-risk-corpus.v1"
    probes: tuple[ActionRiskProbe, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_probes(self) -> Self:
        """Reject ambiguous identities that could hide a failed case."""
        identifiers = [probe.case_id for probe in self.probes]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Action-risk case identifiers must be unique")
        return self


class ActionRiskMeasurement(EvaluationRecord):
    """Analyzer decision and timing without the original command or file contents."""

    case_id: EvaluationIdentifier
    category: RiskCategory
    must_confirm: bool
    observed_risk: RiskLevel
    confirmation_required: bool
    elapsed_seconds: NonnegativeFloat


class ActionRiskEvaluation(EvaluationRecord):
    """One analyzer-only run, not a model's ability to predict action risk."""

    schema_version: Literal["heartwood.action-risk-evaluation.v1"] = (
        "heartwood.action-risk-evaluation.v1"
    )
    corpus_fingerprint: Sha256
    analyzer_fingerprint: Sha256
    evaluated_at: AwareDatetime
    openhands_version: EvaluationIdentifier
    confirmation_mode: Literal["always-confirm", "confirm-risky"]
    measurements: tuple[ActionRiskMeasurement, ...] = Field(min_length=1)
    model_calls: Literal[0] = 0
    reported_cost_usd: Literal[0] = 0

    @model_validator(mode="after")
    def unique_measurements(self) -> Self:
        """Require one measurement per distinct case."""
        identifiers = [item.case_id for item in self.measurements]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Action-risk measurements must have unique case identifiers")
        return self

    @property
    def false_approvals(self) -> int:
        """Count actions requiring review that the policy would permit."""
        return sum(
            item.must_confirm and not item.confirmation_required for item in self.measurements
        )

    @property
    def unnecessary_confirmations(self) -> int:
        """Count confirmations of labeled benign actions."""
        return sum(
            not item.must_confirm and item.confirmation_required for item in self.measurements
        )

    @property
    def unknown_classifications(self) -> int:
        """Count incomplete analyzer assessments separately from false approvals."""
        return sum(item.observed_risk == "UNKNOWN" for item in self.measurements)


class ActionRiskPolicy(EvaluationRecord):
    """Deployment-owned acceptance thresholds; no policy is selected implicitly."""

    deployment: EvaluationIdentifier
    maximum_false_approvals: Literal[0] = 0
    maximum_unnecessary_confirmation_fraction: float = Field(ge=0, le=1)
    maximum_mean_latency_seconds: float = Field(gt=0)


class ActionRiskAssessment(EvaluationRecord):
    """Bounded corpus evidence, never an automatic change to action approvals."""

    evaluation_fingerprint: Sha256
    policy: ActionRiskPolicy
    passed: bool
    reasons: tuple[EvaluationReason, ...]
