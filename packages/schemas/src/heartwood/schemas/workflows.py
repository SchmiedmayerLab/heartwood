# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Versioned research-workflow definitions shared by execution and evaluation."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from heartwood.schemas.execution import ExecutionBudget, ExecutionUsage
from heartwood.schemas.identifiers import WorkflowIdentifier as WorkflowIdentifier
from heartwood.schemas.project_paths import project_relative_path
from heartwood.schemas.review import ResearchReviewRun

type WorkflowText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)
]
type WorkflowInputValue = Annotated[str, StringConstraints(min_length=1, max_length=8000)]
type WorkflowOutcomeStatus = Literal["success", "partial_success", "blocked", "failed", "unknown"]


class WorkflowRecord(BaseModel):
    """Closed immutable workflow metadata, without model or provider settings."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        json_schema_serialization_defaults_required=True,
    )


class WorkflowInput(WorkflowRecord):
    """An explicit researcher-supplied file or research objective."""

    input_id: WorkflowIdentifier
    label: WorkflowText
    description: WorkflowText
    kind: Literal["file", "text"]


class WorkflowArtifact(WorkflowRecord):
    """A declared output beneath the workflow's project-relative output directory."""

    artifact_id: WorkflowIdentifier
    label: WorkflowText
    relative_path: str = Field(min_length=1, max_length=512)
    media_type: Literal["text/markdown", "text/csv", "text/x-python", "application/json"]

    @field_validator("relative_path")
    @classmethod
    def public_path(cls, value: str) -> str:
        """Use the same normalized public-path rules as workspace inspection."""
        project_relative_path(value, allow_root=False)
        return value


class WorkflowCheck(WorkflowRecord):
    """A required deterministic check resolved by the gateway's evaluator registry."""

    check_id: WorkflowIdentifier
    evaluator_id: WorkflowIdentifier
    description: WorkflowText
    artifact_ids: tuple[WorkflowIdentifier, ...] = Field(min_length=1)


class WorkflowStage(WorkflowRecord):
    """One ordered task with explicit context, outputs, and completion gates."""

    stage_id: WorkflowIdentifier
    label: WorkflowText
    instruction: WorkflowText
    reads: tuple[WorkflowIdentifier, ...] = Field(min_length=1)
    writes: tuple[WorkflowIdentifier, ...] = Field(min_length=1)
    checks: tuple[WorkflowCheck, ...] = Field(min_length=1)
    reviewer_gate: Literal["none", "researcher"] = "researcher"
    skill_ids: tuple[WorkflowIdentifier, ...] = ()
    specialist_ids: tuple[WorkflowIdentifier, ...] = ()
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)

    @model_validator(mode="after")
    def unique_references(self) -> Self:
        """Reject duplicate identities instead of silently merging their meaning."""
        for values in (self.reads, self.writes, self.skill_ids, self.specialist_ids):
            if len(values) != len(set(values)):
                raise ValueError("Workflow stage references must be unique")
        for check in self.checks:
            if len(check.artifact_ids) != len(set(check.artifact_ids)):
                raise ValueError("Workflow check references must be unique")
        return self


class WorkflowDefinition(WorkflowRecord):
    """Pinned sequential task contract; advisory concurrency does not change its order."""

    schema_version: Literal["heartwood.workflow-definition.v1"] = "heartwood.workflow-definition.v1"
    workflow_id: WorkflowIdentifier
    version: int = Field(ge=1)
    label: WorkflowText
    description: WorkflowText
    inputs: tuple[WorkflowInput, ...] = Field(min_length=1, max_length=32)
    artifacts: tuple[WorkflowArtifact, ...] = Field(min_length=1, max_length=64)
    stages: tuple[WorkflowStage, ...] = Field(min_length=1, max_length=32)
    budget: ExecutionBudget

    @property
    def fingerprint(self) -> str:
        """Bind all stages, gates, budgets, and presentation metadata to one identity."""
        content = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def stage(self, stage_id: str) -> WorkflowStage:
        """Resolve one declared stage without introducing fallback behavior."""
        for stage in self.stages:
            if stage.stage_id == stage_id:
                return stage
        raise ValueError("Unknown workflow stage")

    @model_validator(mode="after")
    def coherent_data_flow(self) -> Self:
        """Require ordered dependencies, one output owner, and checks for every output."""
        inputs = [item.input_id for item in self.inputs]
        artifacts = [item.artifact_id for item in self.artifacts]
        stages = [item.stage_id for item in self.stages]
        checks = [check.check_id for stage in self.stages for check in stage.checks]
        for values in (inputs + artifacts, stages, checks):
            if len(values) != len(set(values)):
                raise ValueError("Workflow identities must be unique")
        paths = [item.relative_path.casefold() for item in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("Workflow artifact paths must be distinct")
        for path in paths:
            if any(other.startswith(path + "/") for other in paths):
                raise ValueError("Workflow artifact paths cannot shadow a directory")
        available = set(inputs)
        produced: set[str] = set()
        for stage in self.stages:
            if not set(stage.reads) <= available:
                raise ValueError("Workflow stage reads an undeclared or future input")
            outputs = set(stage.writes)
            if not outputs <= set(artifacts) or outputs & produced:
                raise ValueError("Workflow outputs require one declared producing stage")
            scope = set(stage.reads) | outputs
            if any(not set(check.artifact_ids) <= scope for check in stage.checks):
                raise ValueError("Workflow checks must refer to the stage's declared context")
            checked = {item for check in stage.checks for item in check.artifact_ids}
            if not outputs <= checked:
                raise ValueError("Every workflow output requires a deterministic check")
            produced.update(outputs)
            available.update(outputs)
        if produced != set(artifacts):
            raise ValueError("Every workflow artifact requires a producing stage")
        return self


class WorkflowCatalogEntry(WorkflowRecord):
    """One maintained workflow and any checks missing from this runtime."""

    definition: WorkflowDefinition
    unavailable_checks: tuple[WorkflowIdentifier, ...] = ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def available(self) -> bool:
        """Expose runtime support, not model qualification or execution permission."""
        return not self.unavailable_checks


class WorkflowCatalog(WorkflowRecord):
    """Read-only workflow discovery shared by every interface."""

    workflows: tuple[WorkflowCatalogEntry, ...]


class WorkflowValueFingerprint(WorkflowRecord):
    """Digest of one bound input or output, without retaining its content."""

    artifact_id: WorkflowIdentifier
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkflowCheckResult(WorkflowRecord):
    """Gateway evaluator result bound to the exact inputs it inspected."""

    check_id: WorkflowIdentifier
    evaluator_id: WorkflowIdentifier
    status: Literal["passed", "failed", "not_run"]
    inspected: tuple[WorkflowValueFingerprint, ...]


class WorkflowStageAssessment(WorkflowRecord):
    """Evidence eligibility, separate from the researcher's permission to advance."""

    workflow_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_id: WorkflowIdentifier
    evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_satisfied: bool
    researcher_review_required: bool
    reasons: tuple[str, ...]


class WorkflowBoundInput(WorkflowRecord):
    """Private researcher input, bound to the bytes accepted at preparation."""

    input_id: WorkflowIdentifier
    kind: Literal["file", "text"]
    value: WorkflowInputValue
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def safe_file_path(self) -> Self:
        """Preserve exact input text while validating public file references."""
        if self.kind == "file":
            project_relative_path(self.value, allow_root=False)
        elif not self.value.strip():
            raise ValueError("Workflow text inputs must not be blank")
        return self


class WorkflowProjectBinding(WorkflowRecord):
    """Project-relative inputs and output location; never an external workspace root."""

    workflow_id: WorkflowIdentifier
    workflow_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_directory: str = Field(min_length=1, max_length=512)
    inputs: tuple[WorkflowBoundInput, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def safe_binding(self) -> Self:
        """Reject private output paths and ambiguous input identities."""
        project_relative_path(self.output_directory, allow_root=False)
        if len({item.input_id for item in self.inputs}) != len(self.inputs):
            raise ValueError("Workflow input bindings must be unique")
        return self


class WorkflowStageEvaluation(WorkflowRecord):
    """Content-minimized checks and assessment, not permission to advance a stage."""

    artifacts: tuple[WorkflowValueFingerprint, ...]
    checks: tuple[WorkflowCheckResult, ...]
    assessment: WorkflowStageAssessment


class WorkflowStart(WorkflowRecord):
    """Explicitly bind a new workflow to an unused session."""

    action: Literal["start"]
    workflow_id: WorkflowIdentifier
    inputs: dict[WorkflowIdentifier, WorkflowInputValue] = Field(min_length=1, max_length=32)
    output_directory: str = Field(min_length=1, max_length=512)


class WorkflowTransition(WorkflowRecord):
    """Apply a transition only to the exact run and revision the researcher saw."""

    action: Literal["run", "evaluate", "cancel", "request-review", "assess-review"]
    run_id: str = Field(min_length=1)
    revision: int = Field(ge=0, strict=True)


class WorkflowReview(WorkflowRecord):
    """Accept or reject checked stage evidence, never the underlying tool actions."""

    action: Literal["review"]
    run_id: str = Field(min_length=1)
    revision: int = Field(ge=0, strict=True)
    evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: bool = Field(strict=True)


type WorkflowRequest = Annotated[
    WorkflowStart | WorkflowTransition | WorkflowReview, Field(discriminator="action")
]


class WorkflowControl(WorkflowRecord):
    """A presentation affordance carrying the exact revision-bound command to submit."""

    control_id: Literal[
        "run", "evaluate", "accept", "decline", "cancel", "request-review", "assess-review"
    ]
    label: WorkflowText
    request: WorkflowTransition | WorkflowReview


class WorkflowRun(WorkflowRecord):
    """Authoritative stage snapshot stored in the paired session and audit journal."""

    run_id: str = Field(min_length=1)
    revision: int = Field(ge=0)
    binding: WorkflowProjectBinding
    stage_id: WorkflowIdentifier
    phase: Literal["ready", "running", "review", "blocked", "completed", "cancelled"]
    completed: tuple[WorkflowStageEvaluation, ...] = ()
    evaluation: WorkflowStageEvaluation | None = None
    started_sequence: int | None = Field(default=None, ge=0)
    created_at: AwareDatetime
    stage_started_at: AwareDatetime | None = None
    stage_usage_baseline: ExecutionUsage | None = None
    research_review: ResearchReviewRun | None = None
