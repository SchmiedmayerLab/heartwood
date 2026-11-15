# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Gateway-owned session state presented consistently by every interface."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, computed_field

from heartwood.core_adapter.action_review import AUTOMATIC_REVIEW_ACTOR
from heartwood.core_adapter.external_actions import ExternalActionProposal, ExternalActionRecord
from heartwood.core_adapter.external_results import (
    ExternalResultProposal,
    ExternalResultRecord,
    external_result_records,
)
from heartwood.core_adapter.session_activity import (
    SessionActivity,
    session_activity,
    worker_replacement_blocker,
)
from heartwood.core_adapter.workflow_provenance import workflow_experiment_events
from heartwood.core_adapter.workflow_runtime import workflow_controls, workflow_run
from heartwood.schemas.execution import ExecutionBudget, ExecutionUsage, NativeTaskExecution
from heartwood.schemas.experiments import ExperimentRun, reduce_experiment_events
from heartwood.schemas.project_paths import ProjectPathError, project_relative_path
from heartwood.schemas.review import ReviewProposals
from heartwood.schemas.workflows import WorkflowControl, WorkflowRun
from heartwood.session import CommandKind, EventKind, JsonValue, SessionEvent

_OUTSIDE_PREVIEW_CHARACTERS = 2_000


class SessionLifecycle(StrEnum):
    """User-visible state of the OpenHands conversation."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_CONFIRMATION = "waiting-for-confirmation"
    FINISHED = "finished"
    ERROR = "error"


class _ProjectionRecord(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_serialization_defaults_required=True,
        populate_by_name=True,
        use_enum_values=True,
    )


class ProjectionActivity(_ProjectionRecord):
    sequence: int = Field(ge=0)
    kind: EventKind
    label: str
    detail: str


class ProjectionMessage(_ProjectionRecord):
    id: str
    sequence: int = Field(ge=0)
    role: Literal["user", "agent", "trace"]
    label: str
    content: str
    detail: str | None = None
    technical_detail: str | None = Field(default=None, serialization_alias="technicalDetail")


class ProjectionTerminalActionDetails(_ProjectionRecord):
    """Typed terminal arguments from one OpenHands action."""

    kind: Literal["terminal"] = "terminal"
    command: str
    is_input: bool = Field(default=False, serialization_alias="isInput")
    timeout: float | None = Field(default=None, ge=0)
    reset: bool = False


class ProjectionFileEditorActionDetails(_ProjectionRecord):
    """Typed file-editor arguments from one OpenHands action."""

    kind: Literal["file-editor"] = "file-editor"
    operation: Literal["view", "create", "str_replace", "insert", "undo_edit", "unknown"]
    path: str | None = None


class ProjectionTaskActionDetails(_ProjectionRecord):
    """Typed sequential-specialist arguments from one OpenHands action."""

    kind: Literal["task"] = "task"
    description: str | None = None
    prompt: str | None = None
    subagent_type: str | None = Field(default=None, serialization_alias="subagentType")
    role_label: str | None = Field(default=None, serialization_alias="roleLabel")
    capability: Literal["advisory", "project-actions"] | None = None
    resume: str | None = None


class ProjectionOtherActionDetails(_ProjectionRecord):
    """Typed fallback for an OpenHands tool without a specialized renderer."""

    kind: Literal["other"] = "other"


class ProjectionOutsideContent(_ProjectionRecord):
    """Exact JSON that leaves the session: its identity and a bounded leading preview.

    The size and digest describe the canonical UTF-8 JSON bound by the review.
    The complete content remains in the action record's exact arguments.
    """

    preview: str
    truncated: bool = False
    size_bytes: int = Field(ge=0, serialization_alias="sizeBytes")
    sha256: str


class ProjectionOutsideInput(_ProjectionRecord):
    """One immutable input submitted with an outside operation by reference and digest."""

    reference: str
    sha256: str
    size_bytes: int = Field(ge=0, serialization_alias="sizeBytes")


class ProjectionOutsideOperationActionDetails(_ProjectionRecord):
    """A gateway-issued operation that leaves the sandbox for a deployment service."""

    kind: Literal["outside-operation"] = "outside-operation"
    service_id: str = Field(serialization_alias="serviceId")
    destination: str
    request: ProjectionOutsideContent
    inputs: tuple[ProjectionOutsideInput, ...] = ()
    expires_at: str = Field(serialization_alias="expiresAt")


class ProjectionOutsideResultActionDetails(_ProjectionRecord):
    """A gateway-issued release of an outside result to the worker and its model route."""

    kind: Literal["outside-result"] = "outside-result"
    model_route: dict[str, JsonValue] = Field(serialization_alias="modelRoute")
    content: ProjectionOutsideContent
    expires_at: str = Field(serialization_alias="expiresAt")


type ProjectionActionDetails = Annotated[
    ProjectionTerminalActionDetails
    | ProjectionFileEditorActionDetails
    | ProjectionTaskActionDetails
    | ProjectionOutsideOperationActionDetails
    | ProjectionOutsideResultActionDetails
    | ProjectionOtherActionDetails,
    Field(discriminator="kind"),
]


class ProjectionAffectedPath(_ProjectionRecord):
    """Project-relative path attributed to a typed mutating action."""

    path: str
    effect: Literal["created", "modified", "deleted", "unknown"]
    provenance: Literal["file-editor-action"] = "file-editor-action"


class ProjectionActionOutcome(_ProjectionRecord):
    """Bounded private result of an executed action."""

    exit_code: int = Field(serialization_alias="exitCode")
    summary: str
    result: str | None = None
    result_truncated: bool = Field(default=False, serialization_alias="resultTruncated")


class ProjectionActionRecord(_ProjectionRecord):
    """One versioned action record correlated across the OpenHands lifecycle."""

    schema_version: Literal["heartwood.action-record.v1"] = "heartwood.action-record.v1"
    tool_call_id: str = Field(serialization_alias="toolCallId")
    action_id: str | None = Field(default=None, serialization_alias="actionId")
    group_id: str | None = Field(default=None, serialization_alias="groupId")
    tool_name: str = Field(serialization_alias="toolName")
    risk: Literal["high", "low", "medium", "unknown"]
    summary: str
    summary_source: Literal["model", "gateway"] = Field(
        default="model",
        serialization_alias="summarySource",
    )
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    details: ProjectionActionDetails
    affected_paths: tuple[ProjectionAffectedPath, ...] = Field(
        default=(),
        serialization_alias="affectedPaths",
    )
    state: Literal[
        "proposed",
        "awaiting-review",
        "approved",
        "rejected",
        "running",
        "succeeded",
        "failed",
        "outcome-unknown",
    ]
    decision: Literal["approved", "rejected"] | None = None
    automatic_rule: str | None = Field(default=None, serialization_alias="automaticRule")
    """The review rule under which the gateway, not the researcher, allowed the action."""
    outcome: ProjectionActionOutcome | None = None
    proposed_sequence: int = Field(ge=0, serialization_alias="proposedSequence")
    updated_sequence: int = Field(ge=0, serialization_alias="updatedSequence")


class ProjectionApprovalGroup(_ProjectionRecord):
    """One decision that applies to every listed OpenHands action."""

    group_id: str = Field(serialization_alias="groupId")
    actions: tuple[ProjectionActionRecord, ...]
    decision: Literal["approved", "denied"] | None = None
    """``approved`` for an outside approval that is recorded but not yet sent.

    Only that approval's own command can send it, so the set offers rejection alone.
    """
    decision_scope: Literal["all"] = Field(
        default="all",
        serialization_alias="decisionScope",
    )


class ProjectionModelContext(_ProjectionRecord):
    model_endpoint: str | None = Field(default=None, serialization_alias="modelEndpoint")
    model_decision: str | None = Field(default=None, serialization_alias="modelDecision")
    model_reason: str | None = Field(default=None, serialization_alias="modelReason")


class ProjectionLifecycleState(_ProjectionRecord):
    status: SessionLifecycle = SessionLifecycle.IDLE
    can_pause: bool = Field(default=False, serialization_alias="canPause")
    can_resume: bool = Field(default=False, serialization_alias="canResume")
    can_steer: bool = Field(default=True, serialization_alias="canSteer")


class ProjectionResearcherStatus(_ProjectionRecord):
    """Stable researcher-facing state derived from the session lifecycle."""

    code: Literal[
        "ready",
        "working",
        "waiting-for-review",
        "paused",
        "complete",
        "denied",
        "recoverable-failure",
        "terminal-failure",
    ]
    label: str
    detail: str
    tone: Literal["neutral", "progress", "attention", "success", "danger"]
    recoverable: bool = True


class ProjectionResearcherNotice(_ProjectionRecord):
    """A non-lifecycle outcome that every interface must present."""

    notice_id: str = Field(serialization_alias="noticeId")
    code: Literal["request-not-applied", "worker-replacement-unavailable"]
    label: str
    detail: str
    tone: Literal["attention", "danger"]


class ProjectionCommandOutcome(_ProjectionRecord):
    """Gateway-owned outcome of the most recently accepted command."""

    command_id: str = Field(serialization_alias="commandId")
    command_kind: CommandKind = Field(serialization_alias="commandKind")
    status: Literal["accepted", "rejected"]
    error_code: str | None = Field(default=None, serialization_alias="errorCode")
    message: str | None = None


# Usage reported by workers that a replacement took over, listed beside the session total.
_REPLACED_USAGE_ID = "replaced-workers"


class ProjectionTask(_ProjectionRecord):
    title: str
    # `superseded` marks unfinished work planned by a worker that a replacement took over.
    status: Literal["todo", "in-progress", "done", "superseded"]
    status_label: str = Field(serialization_alias="statusLabel")


class ProjectionUsage(_ProjectionRecord):
    usage_id: str = Field(serialization_alias="usageId")
    purpose_label: str = Field(serialization_alias="purposeLabel")
    model_name: str = Field(serialization_alias="modelName")
    call_count: int = Field(ge=0, serialization_alias="callCount")
    prompt_tokens: int = Field(ge=0, serialization_alias="promptTokens")
    completion_tokens: int = Field(ge=0, serialization_alias="completionTokens")
    cache_read_tokens: int = Field(default=0, ge=0, serialization_alias="cacheReadTokens")
    cache_write_tokens: int = Field(default=0, ge=0, serialization_alias="cacheWriteTokens")
    reasoning_tokens: int = Field(default=0, ge=0, serialization_alias="reasoningTokens")
    context_window: int | None = Field(default=None, ge=0, serialization_alias="contextWindow")
    accumulated_cost: float = Field(default=0.0, ge=0, serialization_alias="accumulatedCost")


class ProjectionSubagent(_ProjectionRecord):
    invocation_id: str = Field(serialization_alias="invocationId")
    task_id: str | None = Field(default=None, serialization_alias="taskId")
    agent_name: str = Field(serialization_alias="agentName")
    role_label: str = Field(serialization_alias="roleLabel")
    status: Literal["proposed", "running", "completed", "error", "rejected"]
    status_label: str = Field(serialization_alias="statusLabel")
    task_summary: str | None = Field(default=None, serialization_alias="taskSummary")
    result_summary: str | None = Field(default=None, serialization_alias="resultSummary")
    parent_session_id: str = Field(serialization_alias="parentSessionId")
    parent_action_id: str = Field(serialization_alias="parentActionId")
    review_proposals: ReviewProposals | None = Field(
        default=None, serialization_alias="reviewProposals"
    )
    native_execution: NativeTaskExecution | None = Field(
        default=None, serialization_alias="nativeExecution"
    )


class ProjectionSuggestion(_ProjectionRecord):
    """One bounded task suggestion derived from the authoritative session state."""

    suggestion_id: Literal[
        "inspect-project",
        "plan-project",
        "continue-plan",
        "review-changes",
        "verify-work",
        "recover-task",
        "identify-next-step",
    ] = Field(serialization_alias="suggestionId")
    label: str
    prompt: str
    kind: Literal["task", "follow-up", "recovery"]


class ProjectionReviewExecution(_ProjectionRecord):
    """One shared description of requested versus admitted advisory concurrency."""

    status: Literal["preview", "authorized", "admitted", "assessed", "unavailable", "cancelled"]
    purpose: Literal["qualified-review", "qualification-trial"]
    workers: int = Field(ge=2)
    reviewers: tuple[str, ...]
    budget: ExecutionBudget
    summary: str


def _review_execution(run: WorkflowRun | None) -> ProjectionReviewExecution | None:
    if run is None:
        return None
    review = run.research_review
    plan = run.parallel_review_plan or (review.parallel_plan if review else None)
    if plan is None:
        return None
    status: Literal["preview", "authorized", "admitted", "assessed", "unavailable", "cancelled"]
    if run.parallel_review_plan is not None:
        status = "preview"
    elif review is not None and review.status != "pending":
        status = review.status
    elif review is not None and review.parallel_dispatch:
        status = "admitted"
    else:
        status = "authorized"
    scope = plan.scope
    budget = scope.budget
    label = (
        "Experimental parallel review"
        if plan.purpose == "qualification-trial"
        else "Parallel review"
    )
    return ProjectionReviewExecution(
        status=status,
        purpose=plan.purpose,
        workers=scope.workers,
        reviewers=scope.reviewer_ids,
        budget=budget,
        summary=(
            f"{label} ({status}): {scope.workers} workers; "
            f"up to {budget.maximum_seconds:g}s, {budget.maximum_model_calls} model calls, "
            f"{budget.maximum_tokens:,} tokens, {budget.maximum_actions} actions, "
            f"${budget.maximum_reported_cost_usd:g} reported cost. "
            "Action confirmation still applies."
        ),
    )


class SessionProjection(_ProjectionRecord):
    """Complete session projection owned by the gateway."""

    schema_version: Literal["heartwood.session-projection.v1"] = "heartwood.session-projection.v1"
    session_id: str = Field(serialization_alias="sessionId")
    workflow: WorkflowRun | None = None
    review_execution: ProjectionReviewExecution | None = Field(
        default=None,
        serialization_alias="reviewExecution",
    )
    experiments: tuple[ExperimentRun, ...] = ()
    workflow_controls: tuple[WorkflowControl, ...] = Field(
        default=(), serialization_alias="workflowControls"
    )
    event_count: int = Field(ge=0, serialization_alias="eventCount")
    revision: int = Field(ge=-1)
    workspace_revision: int = Field(
        default=-1,
        ge=-1,
        serialization_alias="workspaceRevision",
    )
    stream_epoch: str = Field(default="standalone", serialization_alias="streamEpoch")
    stream_revision: int = Field(default=0, ge=0, serialization_alias="streamRevision")
    activity: tuple[ProjectionActivity, ...] = ()
    conversation: tuple[ProjectionMessage, ...] = ()
    actions: tuple[ProjectionActionRecord, ...] = ()
    pending_approval: ProjectionApprovalGroup | None = Field(
        default=None,
        serialization_alias="pendingApproval",
    )
    context: ProjectionModelContext = Field(default_factory=ProjectionModelContext)
    lifecycle: ProjectionLifecycleState = Field(default_factory=ProjectionLifecycleState)
    researcher_status: ProjectionResearcherStatus = Field(
        default_factory=lambda: _researcher_status(
            SessionLifecycle.IDLE,
            error_recoverable=True,
            latest_action_denied=False,
        ),
        serialization_alias="researcherStatus",
    )
    researcher_notice: ProjectionResearcherNotice | None = Field(
        default=None,
        serialization_alias="researcherNotice",
    )
    last_command_outcome: ProjectionCommandOutcome | None = Field(
        default=None,
        serialization_alias="lastCommandOutcome",
    )
    task_plan: tuple[ProjectionTask, ...] = Field(default=(), serialization_alias="taskPlan")
    usage: ProjectionUsage | None = None
    usage_by_purpose: tuple[ProjectionUsage, ...] = Field(
        default=(),
        serialization_alias="usageByPurpose",
    )
    subagents: tuple[ProjectionSubagent, ...] = ()
    suggestions: tuple[ProjectionSuggestion, ...] = ()
    streaming_text: str = Field(default="", serialization_alias="streamingText")
    available_commands: tuple[Literal["chat", "pause", "resume", "approve", "deny"], ...] = Field(
        default=("chat",), serialization_alias="availableCommands"
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def paused(self) -> bool:
        """Return whether the projected session is paused."""
        return self.lifecycle.status == SessionLifecycle.PAUSED

    def safe_dict(self) -> dict[str, object]:
        """Return the complete interface-safe projection payload."""
        return cast(dict[str, object], self.model_dump(mode="json", by_alias=True))

    def execution_usage(self, *, elapsed_seconds: float) -> ExecutionUsage:
        """Share observed task consumption without treating unpriced calls as free."""
        usage = self.usage
        return ExecutionUsage(
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            model_calls=usage.call_count if usage else None,
            reported_cost_usd=usage.accumulated_cost
            if usage and usage.accumulated_cost > 0
            else None,
            proposed_actions=len(self.actions),
            elapsed_seconds=elapsed_seconds,
        )


@dataclass(slots=True)
class _SessionReduction:
    """Mutable state accumulated while reducing one session's events in order."""

    activity: list[ProjectionActivity] = field(default_factory=list)
    conversation: list[ProjectionMessage] = field(default_factory=list)
    actions: dict[str, ProjectionActionRecord] = field(default_factory=dict)
    proposed_action_ids: set[str] = field(default_factory=set)
    approval_group_actions: dict[str, list[str]] = field(default_factory=dict)
    approval_group_decisions: dict[str, Literal["approved", "denied"] | None] = field(
        default_factory=dict
    )
    approval_group_resolutions: dict[str, dict[str, Literal["approved", "denied"]]] = field(
        default_factory=dict
    )
    integrity_failed_action_ids: set[str] = field(default_factory=set)
    retained_group_ids: set[str] = field(default_factory=set)
    reported_resolutions: set[str] = field(default_factory=set)
    context: ProjectionModelContext = field(default_factory=ProjectionModelContext)
    lifecycle_status: SessionLifecycle = SessionLifecycle.IDLE
    lifecycle_sequence: int = -1
    lifecycle_error_recoverable: bool = True
    command_outcome: ProjectionCommandOutcome | None = None
    tasks: tuple[ProjectionTask, ...] = ()
    usage: ProjectionUsage | None = None
    usage_by_purpose: dict[str, ProjectionUsage] = field(default_factory=dict)
    subagents: dict[str, ProjectionSubagent] = field(default_factory=dict)
    worker_attempts: set[str] = field(default_factory=set)
    worker_revoked: bool = False
    # Cumulative usage reported by the current worker, and the sum left by replaced ones.
    attempt_usage: dict[str, ProjectionUsage] = field(default_factory=dict)
    replaced_usage: dict[str, ProjectionUsage] = field(default_factory=dict)


def project_session(
    events: tuple[SessionEvent, ...],
    *,
    session_id: str,
    streaming_text: str = "",
    stream_epoch: str = "standalone",
    stream_revision: int = 0,
    parallel_reviews_available: bool = False,
) -> SessionProjection:
    """Reduce durable session events once at the gateway boundary."""
    reduction = _SessionReduction()
    # Malformed outside-result records must fail the projection before any event is reduced.
    external_result_records(events)
    for event in events:
        reduction.activity.append(_activity(event))
        reduce_event = _EVENT_REDUCERS.get(str(event.kind))
        if reduce_event is not None:
            reduce_event(reduction, event)
    _apply_session_activity(reduction, session_activity(events))
    _settle_actions(reduction)

    actions = reduction.actions
    lifecycle_status = reduction.lifecycle_status
    lifecycle_error_recoverable = reduction.lifecycle_error_recoverable
    pending_approval = _pending_approval(
        actions,
        approval_group_actions=reduction.approval_group_actions,
        approval_group_decisions=reduction.approval_group_decisions,
        retained_group_ids=reduction.retained_group_ids,
    )
    if pending_approval is not None and lifecycle_status not in {
        SessionLifecycle.ERROR,
        SessionLifecycle.PAUSED,
    }:
        lifecycle_status = SessionLifecycle.WAITING_FOR_CONFIRMATION
    lifecycle = ProjectionLifecycleState(
        status=lifecycle_status,
        can_pause=lifecycle_status == SessionLifecycle.RUNNING,
        can_resume=lifecycle_status == SessionLifecycle.PAUSED,
        can_steer=(
            lifecycle_status
            in {
                SessionLifecycle.IDLE,
                SessionLifecycle.RUNNING,
                SessionLifecycle.PAUSED,
                SessionLifecycle.FINISHED,
            }
            or (lifecycle_status == SessionLifecycle.ERROR and lifecycle_error_recoverable)
        ),
    )
    projected_actions = tuple(actions.values())
    projected_subagents = tuple(
        _enrich_subagent(subagent, actions=projected_actions)
        for subagent in reduction.subagents.values()
    )
    available_commands = _available_commands(
        lifecycle=lifecycle_status,
        pending_approval=pending_approval,
        error_recoverable=lifecycle_error_recoverable,
    )
    current_workflow = workflow_run(events)
    return SessionProjection(
        session_id=session_id,
        workflow=current_workflow,
        review_execution=_review_execution(current_workflow),
        experiments=reduce_experiment_events(workflow_experiment_events(events)),
        workflow_controls=workflow_controls(
            events,
            active=lifecycle_status == SessionLifecycle.RUNNING,
            pending_actions=pending_approval is not None,
            parallel_reviews_available=parallel_reviews_available,
        ),
        event_count=len(events),
        revision=events[-1].sequence if events else -1,
        workspace_revision=max(
            (
                event.sequence
                for event in events
                if str(event.kind) == EventKind.TOOL_EXECUTION_RECORDED.value
            ),
            default=-1,
        ),
        stream_epoch=stream_epoch,
        stream_revision=stream_revision,
        activity=tuple(reduction.activity),
        conversation=tuple(reduction.conversation),
        actions=projected_actions,
        pending_approval=pending_approval,
        context=reduction.context,
        lifecycle=lifecycle,
        researcher_status=_researcher_status(
            lifecycle_status,
            error_recoverable=lifecycle_error_recoverable,
            latest_action_denied=_latest_action_was_denied(projected_actions),
        ),
        researcher_notice=_researcher_notice(
            reduction.command_outcome,
            replacement_blocker=(
                worker_replacement_blocker(events) if reduction.worker_revoked else None
            ),
        ),
        last_command_outcome=reduction.command_outcome,
        task_plan=reduction.tasks,
        usage=reduction.usage,
        usage_by_purpose=tuple(reduction.usage_by_purpose.values()),
        subagents=projected_subagents,
        suggestions=_suggestions(
            lifecycle=lifecycle_status,
            conversation=tuple(reduction.conversation),
            actions=projected_actions,
            tasks=reduction.tasks,
            available_commands=available_commands,
            error_recoverable=lifecycle_error_recoverable,
        ),
        streaming_text=(streaming_text if lifecycle_status == SessionLifecycle.RUNNING else ""),
        available_commands=available_commands,
    )


def _reduce_user_message(reduction: _SessionReduction, event: SessionEvent) -> None:
    _append_message(
        reduction.conversation,
        event,
        role="user",
        label="You",
        content=_string(event.payload.get("content")),
        message_id=f"local-{_string(event.payload.get('command_id'))}",
    )


def _reduce_command_received(reduction: _SessionReduction, event: SessionEvent) -> None:
    reduction.command_outcome = ProjectionCommandOutcome(
        command_id=_string(event.payload.get("command_id")),
        command_kind=CommandKind(_string(event.payload.get("command_kind"))),
        status="accepted",
    )


def _reduce_agent_message(reduction: _SessionReduction, event: SessionEvent) -> None:
    _append_message(
        reduction.conversation,
        event,
        role="agent",
        label="Agent",
        content=_string(event.payload.get("content")),
    )


def _reduce_outside_record(reduction: _SessionReduction, event: SessionEvent) -> None:
    outside = (
        ExternalActionRecord.model_validate_json(json.dumps(event.payload["record"]))
        if str(event.kind) == EventKind.EXTERNAL_ACTION_RECORDED.value
        else ExternalResultRecord.model_validate_json(json.dumps(event.payload["record"]))
    )
    outside_proposal = outside.proposal
    call = outside_proposal.review_call
    outside_action = reduction.actions.get(outside_proposal.action_id) or _action_record(
        event,
        payload={"summary": call.summary, "action_id": outside_proposal.action_id},
        tool_call_id=outside_proposal.action_id,
        tool_name=call.tool_name,
        arguments=call.arguments,
    )
    state = "awaiting-review"
    outcome = None
    decision = None
    if outside.decision is not None:
        decision = "approved" if outside.decision.decision == "approved" else "rejected"
        state = "rejected" if decision == "rejected" else "outcome-unknown"
        reduction.approval_group_decisions[outside_proposal.group_id] = outside.decision.decision
        _append_approval_message(
            reduction.conversation,
            event,
            group_id=outside_proposal.group_id,
            decision=outside.decision.decision,
            action_count=1,
            reported_resolutions=reduction.reported_resolutions,
        )
    else:
        reduction.approval_group_decisions[outside_proposal.group_id] = None
    if outside.phase == "authorized":
        state = "approved"
        reduction.retained_group_ids.add(outside_proposal.group_id)
    else:
        reduction.retained_group_ids.discard(outside_proposal.group_id)
    if outside.phase in {"expired", "withdrawn"}:
        # Closed before dispatch: neither awaiting review nor an uncertain effect.
        reduction.approval_group_decisions[outside_proposal.group_id] = "denied"
        state = "failed"
        outcome = ProjectionActionOutcome(
            exit_code=1,
            summary=(
                "Review expired before dispatch"
                if outside.phase == "expired"
                else "Approval withdrawn before dispatch"
            ),
            result="Nothing was sent.",
        )
    if isinstance(outside, ExternalResultRecord) and outside.phase == "submitted":
        state = "succeeded"
        outcome = ProjectionActionOutcome(
            exit_code=0,
            summary="Result submitted to worker",
            result="Native delivery accepted; worker execution is not established.",
        )
    if isinstance(outside, ExternalActionRecord) and outside.observation is not None:
        operation = outside.observation.operation
        state = {
            "submitted": "running",
            "queued": "running",
            "running": "running",
            "completed": "succeeded",
            "failed": "failed",
            "cancelled": "failed",
            "denied": "failed",
            "unknown": "outcome-unknown",
        }[operation.status]
        if outside.terminal:
            outcome = ProjectionActionOutcome(
                exit_code=0 if operation.status == "completed" else 1,
                summary=f"Platform operation {operation.status}",
                result=f"Platform record: {operation.platform_record_ref}",
            )
    reduction.actions[outside_proposal.action_id] = outside_action.model_copy(
        update={
            "summary_source": "gateway",
            "details": _outside_details(outside_proposal),
            "group_id": outside_proposal.group_id,
            "state": state,
            "decision": decision,
            "outcome": outcome,
            "updated_sequence": event.sequence,
        }
    )
    reduction.approval_group_actions[outside_proposal.group_id] = [outside_proposal.action_id]


def _reduce_tool_call_proposed(reduction: _SessionReduction, event: SessionEvent) -> None:
    tool_name = _string(event.payload.get("tool_name"))
    arguments = _mapping(event.payload.get("arguments"))
    tool_call_id = _string(event.payload.get("tool_call_id"))
    if not tool_call_id:
        return
    prior = reduction.actions.get(tool_call_id)
    # Reconciliation may capture a complete confirmation before its
    # callback proposal. It is not a second proposal, but its action
    # identity and reviewed arguments must remain unchanged.
    if tool_call_id in reduction.proposed_action_ids or (
        prior is not None
        and (
            prior.tool_name != tool_name
            or prior.arguments != arguments
            or (
                prior.action_id is not None
                and prior.action_id != (_string(event.payload.get("action_id")) or None)
            )
            or tool_call_id in reduction.integrity_failed_action_ids
        )
    ):
        _fail_integrity(
            reduction,
            event,
            tool_call_ids=(tool_call_id,),
            detail="An action proposal repeats or conflicts with its stable identity.",
        )
        return
    reduction.proposed_action_ids.add(tool_call_id)
    proposal = _action_record(
        event,
        payload=event.payload,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        arguments=arguments,
    )
    reduction.actions[tool_call_id] = (
        proposal
        if prior is None
        else proposal.model_copy(
            update={
                "group_id": prior.group_id,
                "state": prior.state,
                "decision": prior.decision,
                "automatic_rule": prior.automatic_rule,
                "outcome": prior.outcome,
            }
        )
    )


def _reduce_tool_execution(reduction: _SessionReduction, event: SessionEvent) -> None:
    tool_name = _string(event.payload.get("tool_name"))
    tool_call_id = _string(event.payload.get("tool_call_id"))
    action = reduction.actions.get(tool_call_id)
    exit_code = _signed_integer(event.payload.get("exit_code"))
    outcome = ProjectionActionOutcome(
        exit_code=exit_code,
        summary=(
            _string(event.payload.get("summary"))
            or f"{tool_name} {'failed' if exit_code != 0 else 'completed'}"
        ),
        result=_string(event.payload.get("result")) or None,
        result_truncated=event.payload.get("result_truncated") is True,
    )
    if not tool_call_id:
        return
    if action is not None and action.decision == "rejected":
        _fail_integrity(
            reduction,
            event,
            tool_call_ids=(tool_call_id,),
            detail="A rejected action has a recorded execution.",
        )
        return
    if action is None:
        reduction.actions[tool_call_id] = _action_record(
            event,
            payload=event.payload,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments={},
        )
        _fail_integrity(
            reduction,
            event,
            tool_call_ids=(tool_call_id,),
            detail="An action execution has no matching proposal.",
        )
        return
    action_id = _string(event.payload.get("action_id")) or None
    if (
        (action.action_id is not None and action.action_id != action_id)
        or action.tool_name != tool_name
        or action.outcome is not None
    ):
        _fail_integrity(
            reduction,
            event,
            tool_call_ids=(tool_call_id,),
            detail="An action execution conflicts with its stable identity.",
        )
        return
    reduction.actions[tool_call_id] = action.model_copy(
        update={
            "action_id": action.action_id or action_id,
            "state": "failed" if exit_code != 0 else "succeeded",
            "outcome": outcome,
            "updated_sequence": event.sequence,
        }
    )


def _reduce_confirmation_requested(reduction: _SessionReduction, event: SessionEvent) -> None:
    request = _mapping(event.payload.get("request"))
    tool_call_id = _string(request.get("tool_call_id"))
    if not tool_call_id:
        return
    group_id = _string(request.get("group_id")) or "pending-action-set"
    existing_action = reduction.actions.get(tool_call_id)
    request_action_id = _string(request.get("action_id")) or None
    request_tool_name = _string(request.get("tool_name"))
    if existing_action is not None and (
        (
            request_action_id is not None
            and existing_action.action_id is not None
            and request_action_id != existing_action.action_id
        )
        or request_tool_name != existing_action.tool_name
        or (existing_action.group_id is not None and existing_action.group_id != group_id)
    ):
        _fail_integrity(
            reduction,
            event,
            tool_call_ids=(tool_call_id,),
            detail="An approval request conflicts with its stable action identity.",
        )
        return
    action = existing_action or _action_record(
        event,
        payload=request,
        tool_call_id=tool_call_id,
        tool_name=request_tool_name,
        arguments=_mapping(request.get("arguments")),
    )
    reduction.actions[tool_call_id] = action.model_copy(
        update={
            "group_id": group_id,
            "state": "awaiting-review",
            "updated_sequence": event.sequence,
        }
    )
    group_actions = reduction.approval_group_actions.setdefault(group_id, [])
    if tool_call_id not in group_actions:
        group_actions.append(tool_call_id)
    reduction.approval_group_decisions[group_id] = None
    reduction.lifecycle_status = SessionLifecycle.WAITING_FOR_CONFIRMATION


def _reduce_approval_recorded(reduction: _SessionReduction, event: SessionEvent) -> None:
    group_id = _string(event.payload.get("group_id"))
    decision = _approval_decision(event.payload.get("decision"))
    tool_call_ids = tuple(
        item
        for item in (_string(value) for value in _sequence(event.payload.get("tool_call_ids")))
        if item
    )
    if not (group_id and decision is not None and tool_call_ids):
        return
    automatic_rule = (
        _string(_mapping(event.payload.get("review")).get("rule")) or None
        if event.payload.get("actor_id") == AUTOMATIC_REVIEW_ACTOR
        else None
    )
    group_actions = reduction.approval_group_actions.setdefault(group_id, [])
    for tool_call_id in tool_call_ids:
        if tool_call_id not in group_actions:
            group_actions.append(tool_call_id)
        action = reduction.actions.get(tool_call_id)
        if action is not None:
            reduction.actions[tool_call_id] = _resolved_action(
                action,
                group_id=group_id,
                decision=decision,
                sequence=event.sequence,
            ).model_copy(update={"automatic_rule": automatic_rule})
    reduction.approval_group_decisions[group_id] = decision
    reduction.approval_group_resolutions[group_id] = dict.fromkeys(tool_call_ids, decision)
    _append_approval_message(
        reduction.conversation,
        event,
        group_id=group_id,
        decision=decision,
        action_count=len(tool_call_ids),
        reported_resolutions=reduction.reported_resolutions,
        automatic_rule=automatic_rule,
    )
    reduction.lifecycle_status = SessionLifecycle.IDLE


def _reduce_confirmation_resolved(reduction: _SessionReduction, event: SessionEvent) -> None:
    tool_call_id = _string(event.payload.get("tool_call_id"))
    group_id = _string(event.payload.get("group_id"))
    if not group_id:
        group_id = next(
            (
                candidate_group_id
                for candidate_group_id, candidate_actions in (
                    reduction.approval_group_actions.items()
                )
                if tool_call_id in candidate_actions
            ),
            "",
        )
    approval_decision = _approval_decision(event.payload.get("decision"))
    current_actions = reduction.approval_group_actions.get(group_id)
    if current_actions is None or approval_decision is None:
        return
    group_decision = reduction.approval_group_decisions.get(group_id)
    if group_decision is not None and group_decision != approval_decision:
        _fail_integrity(
            reduction,
            event,
            tool_call_ids=tuple(current_actions),
            detail="An action-set resolution contradicts its recorded decision.",
        )
        return
    resolutions = reduction.approval_group_resolutions.setdefault(group_id, {})
    resolutions[tool_call_id] = approval_decision
    action = reduction.actions.get(tool_call_id)
    if action is not None:
        reduction.actions[tool_call_id] = _resolved_action(
            action,
            group_id=group_id,
            decision=approval_decision,
            sequence=event.sequence,
        )
    resolved_decisions = {resolutions.get(candidate) for candidate in current_actions}
    if None not in resolved_decisions and len(resolved_decisions) == 1 and group_decision is None:
        reduction.approval_group_decisions[group_id] = approval_decision
        _append_approval_message(
            reduction.conversation,
            event,
            group_id=group_id,
            decision=approval_decision,
            action_count=len(current_actions),
            reported_resolutions=reduction.reported_resolutions,
        )
        reduction.lifecycle_status = SessionLifecycle.IDLE


def _reduce_model_call_decision(reduction: _SessionReduction, event: SessionEvent) -> None:
    model_decision = _mapping(event.payload.get("decision"))
    reduction.context = ProjectionModelContext(
        model_endpoint=_string(model_decision.get("endpoint")) or None,
        model_decision=_string(model_decision.get("decision")) or None,
        model_reason=_string(model_decision.get("reason")) or None,
    )


def _reduce_lifecycle_updated(reduction: _SessionReduction, event: SessionEvent) -> None:
    reduction.lifecycle_status = _lifecycle(_string(event.payload.get("status")))
    reduction.lifecycle_sequence = event.sequence


def _reduce_task_plan(reduction: _SessionReduction, event: SessionEvent) -> None:
    reduction.tasks = tuple(_task(item) for item in _sequence(event.payload.get("tasks")))


def _reduce_model_usage(reduction: _SessionReduction, event: SessionEvent) -> None:
    usage_update = _usage(_mapping(event.payload.get("usage")))
    if usage_update is not None:
        reduction.attempt_usage[usage_update.usage_id] = usage_update
        usage = _added_usage(reduction.replaced_usage.get(usage_update.usage_id), usage_update)
        if usage.usage_id == "total":
            reduction.usage = usage
        else:
            reduction.usage_by_purpose[usage.usage_id] = usage


def _added_usage(earlier: ProjectionUsage | None, current: ProjectionUsage) -> ProjectionUsage:
    """Add a replaced worker's cumulative usage to what its successor reports."""
    if earlier is None:
        return current
    return current.model_copy(
        update={
            "call_count": earlier.call_count + current.call_count,
            "prompt_tokens": earlier.prompt_tokens + current.prompt_tokens,
            "completion_tokens": earlier.completion_tokens + current.completion_tokens,
            "cache_read_tokens": earlier.cache_read_tokens + current.cache_read_tokens,
            "cache_write_tokens": earlier.cache_write_tokens + current.cache_write_tokens,
            "reasoning_tokens": earlier.reasoning_tokens + current.reasoning_tokens,
            "accumulated_cost": earlier.accumulated_cost + current.accumulated_cost,
        }
    )


def _replace_worker_usage(reduction: _SessionReduction) -> None:
    """Retain a replaced worker's usage, which its successor's reports start again from zero."""
    for usage_id, usage in reduction.attempt_usage.items():
        reduction.replaced_usage[usage_id] = _added_usage(
            reduction.replaced_usage.get(usage_id), usage
        )
    reduction.attempt_usage.clear()
    replaced = reduction.replaced_usage.get("total")
    if replaced is not None:
        reduction.usage_by_purpose[_REPLACED_USAGE_ID] = replaced.model_copy(
            update={
                "usage_id": _REPLACED_USAGE_ID,
                "purpose_label": _usage_purpose_label(_REPLACED_USAGE_ID),
            }
        )


def _reduce_subagent(reduction: _SessionReduction, event: SessionEvent) -> None:
    subagent = _subagent(_mapping(event.payload.get("subagent")))
    if subagent.invocation_id:
        reduction.subagents[subagent.invocation_id] = subagent


def _reduce_worker_attempt(reduction: _SessionReduction, event: SessionEvent) -> None:
    attempt_id = _string(_mapping(event.payload.get("intent")).get("attempt_id"))
    reduction.worker_revoked = event.payload.get("phase") == "revoked"
    if event.payload.get("phase") == "active" and attempt_id not in reduction.worker_attempts:
        if reduction.worker_attempts:
            _replace_worker_usage(reduction)
            reduction.tasks = tuple(
                task
                if task.status == "done"
                else task.model_copy(update={"status": "superseded", "status_label": "Superseded"})
                for task in reduction.tasks
            )
            _append_message(
                reduction.conversation,
                event,
                role="trace",
                label="System",
                content="A new worker replaced the earlier one",
                detail=(
                    "The earlier sandboxed worker was stopped and removed. "
                    "This session's record is retained, but the new worker does not "
                    "remember the earlier conversation and starts with an empty workspace. "
                    "Its unfinished plan is marked superseded, and its model usage is "
                    "listed separately and included in the session total."
                ),
            )
        reduction.worker_attempts.add(attempt_id)


def _reduce_session_paused(reduction: _SessionReduction, event: SessionEvent) -> None:
    reduction.lifecycle_status = SessionLifecycle.PAUSED
    reduction.lifecycle_sequence = event.sequence


def _reduce_session_resumed(reduction: _SessionReduction, event: SessionEvent) -> None:
    reduction.lifecycle_status = SessionLifecycle.RUNNING
    reduction.lifecycle_sequence = event.sequence


def _reduce_error_recorded(reduction: _SessionReduction, event: SessionEvent) -> None:
    if event.payload.get("automatic_review") == "not-applied":
        _reduce_automatic_review_not_applied(reduction, event)
        return
    error_code = _string(event.payload.get("code"))
    error_message = _error_detail(
        reason=_string(event.payload.get("reason")),
        code=error_code,
    )
    _append_message(
        reduction.conversation,
        event,
        role="trace",
        label="System",
        content="The task could not be completed",
        detail=error_message,
    )
    if reduction.command_outcome is not None:
        reduction.command_outcome = reduction.command_outcome.model_copy(
            update={
                "status": "rejected",
                "error_code": error_code or None,
                "message": error_message,
            }
        )
    if event.payload.get("affects_lifecycle") is not False:
        reduction.lifecycle_status = SessionLifecycle.ERROR
        reduction.lifecycle_sequence = event.sequence


def _reduce_automatic_review_not_applied(reduction: _SessionReduction, event: SessionEvent) -> None:
    # A withdrawn gateway decision returns its action set to the researcher.
    group_id = _string(event.payload.get("group_id"))
    reduction.approval_group_decisions[group_id] = None
    reduction.approval_group_resolutions.pop(group_id, None)
    reduction.reported_resolutions.discard(group_id)
    for tool_call_id in reduction.approval_group_actions.get(group_id, ()):
        if (action := reduction.actions.get(tool_call_id)) is not None:
            reduction.actions[tool_call_id] = action.model_copy(
                update={
                    "state": "awaiting-review",
                    "decision": None,
                    "automatic_rule": None,
                    "updated_sequence": event.sequence,
                }
            )
    reduction.lifecycle_status = SessionLifecycle.WAITING_FOR_CONFIRMATION
    _append_message(
        reduction.conversation,
        event,
        role="trace",
        label="Approval",
        content="Automatic review was not applied",
        detail="The action set waits for your decision.",
    )


_EVENT_REDUCERS: dict[str, Callable[[_SessionReduction, SessionEvent], None]] = {
    EventKind.USER_MESSAGE_RECORDED.value: _reduce_user_message,
    EventKind.COMMAND_RECEIVED.value: _reduce_command_received,
    EventKind.AGENT_MESSAGE_EMITTED.value: _reduce_agent_message,
    EventKind.EXTERNAL_ACTION_RECORDED.value: _reduce_outside_record,
    EventKind.EXTERNAL_RESULT_RECORDED.value: _reduce_outside_record,
    EventKind.TOOL_CALL_PROPOSED.value: _reduce_tool_call_proposed,
    EventKind.TOOL_EXECUTION_RECORDED.value: _reduce_tool_execution,
    EventKind.CONFIRMATION_REQUESTED.value: _reduce_confirmation_requested,
    EventKind.APPROVAL_RECORDED.value: _reduce_approval_recorded,
    EventKind.CONFIRMATION_RESOLVED.value: _reduce_confirmation_resolved,
    EventKind.MODEL_CALL_DECISION_RECORDED.value: _reduce_model_call_decision,
    EventKind.AGENT_LIFECYCLE_UPDATED.value: _reduce_lifecycle_updated,
    EventKind.TASK_PLAN_UPDATED.value: _reduce_task_plan,
    EventKind.MODEL_USAGE_UPDATED.value: _reduce_model_usage,
    EventKind.SUBAGENT_UPDATED.value: _reduce_subagent,
    EventKind.WORKER_ATTEMPT_RECORDED.value: _reduce_worker_attempt,
    EventKind.SESSION_PAUSED.value: _reduce_session_paused,
    EventKind.SESSION_RESUMED.value: _reduce_session_resumed,
    EventKind.ERROR_RECORDED.value: _reduce_error_recorded,
}


def _apply_session_activity(reduction: _SessionReduction, activity: SessionActivity) -> None:
    """Take pause and fatal errors from the reduction the session service decides with."""
    if activity.fatal:
        reduction.lifecycle_error_recoverable = False
    # Only a researcher command continuing the agent ends a pause, whatever the backend reports.
    if activity.paused and reduction.lifecycle_error_recoverable:
        reduction.lifecycle_status = SessionLifecycle.PAUSED


def _settle_actions(reduction: _SessionReduction) -> None:
    """Apply the final lifecycle and integrity state to every reduced action."""
    # A retained outside approval has sent nothing, whatever the agent run is doing.
    retained = {
        tool_call_id
        for group_id in reduction.retained_group_ids
        for tool_call_id in reduction.approval_group_actions.get(group_id, ())
    }
    if reduction.lifecycle_status == SessionLifecycle.RUNNING:
        reduction.actions = {
            tool_call_id: (
                action.model_copy(
                    update={
                        "state": "running",
                        "updated_sequence": max(
                            action.updated_sequence,
                            reduction.lifecycle_sequence,
                        ),
                    }
                )
                if action.state in {"approved", "proposed"} and tool_call_id not in retained
                else action
            )
            for tool_call_id, action in reduction.actions.items()
        }
    if not reduction.lifecycle_error_recoverable:
        reduction.lifecycle_status = SessionLifecycle.ERROR
        reduction.actions = {
            tool_call_id: (
                action.model_copy(
                    update={
                        "state": "outcome-unknown",
                        "updated_sequence": max(
                            action.updated_sequence,
                            reduction.lifecycle_sequence,
                        ),
                    }
                )
                if action.outcome is None
                and action.state not in {"rejected", "awaiting-review"}
                and tool_call_id not in retained
                else action
            )
            for tool_call_id, action in reduction.actions.items()
        }
    for tool_call_id in reduction.integrity_failed_action_ids:
        action = reduction.actions.get(tool_call_id)
        if action is not None:
            reduction.actions[tool_call_id] = action.model_copy(
                update={
                    "state": "outcome-unknown",
                    "outcome": None,
                }
            )


def _action_record(
    event: SessionEvent,
    *,
    payload: dict[str, JsonValue],
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, JsonValue],
) -> ProjectionActionRecord:
    affected_paths = _affected_paths(
        payload.get("affected_paths"),
        arguments=arguments,
    )
    kind = _action_kind(payload.get("kind"))
    return ProjectionActionRecord(
        tool_call_id=tool_call_id,
        action_id=_string(payload.get("action_id")) or None,
        tool_name=tool_name or "unknown-tool",
        risk=_action_risk(payload.get("risk")),
        summary=_string(payload.get("summary")) or f"Run {tool_name or 'tool'}",
        arguments=arguments,
        details=_action_details(
            kind,
            arguments=arguments,
            affected_paths=affected_paths,
            project_path=_safe_project_relative_path(payload.get("project_path")),
            specialist_label=_string(payload.get("specialist_label")) or None,
            specialist_capability=_specialist_capability(payload.get("specialist_capability")),
        ),
        affected_paths=affected_paths,
        state="proposed",
        proposed_sequence=event.sequence,
        updated_sequence=event.sequence,
    )


def _action_details(
    kind: Literal["terminal", "file-editor", "task", "other"],
    *,
    arguments: dict[str, JsonValue],
    affected_paths: tuple[ProjectionAffectedPath, ...],
    project_path: str | None,
    specialist_label: str | None,
    specialist_capability: Literal["advisory", "project-actions"] | None,
) -> ProjectionActionDetails:
    if kind == "terminal":
        return ProjectionTerminalActionDetails(
            command=_string(arguments.get("command")),
            is_input=arguments.get("is_input") is True,
            timeout=_optional_number(arguments.get("timeout")),
            reset=arguments.get("reset") is True,
        )
    if kind == "file-editor":
        operation_value = _string(arguments.get("command"))
        operation: Literal["view", "create", "str_replace", "insert", "undo_edit", "unknown"]
        if operation_value in {"view", "create", "str_replace", "insert", "undo_edit"}:
            operation = cast(
                Literal["view", "create", "str_replace", "insert", "undo_edit"],
                operation_value,
            )
        else:
            operation = "unknown"
        return ProjectionFileEditorActionDetails(
            operation=operation,
            path=project_path or (affected_paths[0].path if affected_paths else None),
        )
    if kind == "task":
        return ProjectionTaskActionDetails(
            description=_string(arguments.get("description")) or None,
            prompt=_string(arguments.get("prompt")) or None,
            subagent_type=_string(arguments.get("subagent_type")) or None,
            role_label=specialist_label,
            capability=specialist_capability,
            resume=_string(arguments.get("resume")) or None,
        )
    return ProjectionOtherActionDetails()


def _outside_content(canonical_json: str) -> ProjectionOutsideContent:
    text = json.dumps(json.loads(canonical_json), indent=2, sort_keys=True, ensure_ascii=False)
    encoded = canonical_json.encode()
    return ProjectionOutsideContent(
        preview=text[:_OUTSIDE_PREVIEW_CHARACTERS],
        truncated=len(text) > _OUTSIDE_PREVIEW_CHARACTERS,
        size_bytes=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _outside_details(
    proposal: ExternalActionProposal | ExternalResultProposal,
) -> ProjectionOutsideOperationActionDetails | ProjectionOutsideResultActionDetails:
    expires_at = proposal.expires_at.isoformat()
    if isinstance(proposal, ExternalResultProposal):
        return ProjectionOutsideResultActionDetails(
            model_route=json.loads(proposal.model_route_json),
            content=_outside_content(proposal.content_json),
            expires_at=expires_at,
        )
    request = proposal.request
    return ProjectionOutsideOperationActionDetails(
        service_id=request.service_id,
        destination=request.destination,
        request=_outside_content(request.request_json),
        inputs=tuple(
            ProjectionOutsideInput(
                reference=item.reference,
                sha256=item.sha256,
                size_bytes=item.size_bytes,
            )
            for item in request.inputs
        ),
        expires_at=expires_at,
    )


def _affected_paths(
    value: JsonValue | None,
    *,
    arguments: dict[str, JsonValue],
) -> tuple[ProjectionAffectedPath, ...]:
    if not isinstance(value, list):
        return ()
    operation = _string(arguments.get("command"))
    effect: Literal["created", "modified", "deleted", "unknown"]
    if operation == "create":
        effect = "created"
    elif operation in {"str_replace", "insert"}:
        effect = "modified"
    else:
        effect = "unknown"
    paths: list[ProjectionAffectedPath] = []
    for item in value:
        path = _safe_project_relative_path(item)
        if path is not None and not any(existing.path == path for existing in paths):
            paths.append(ProjectionAffectedPath(path=path, effect=effect))
    return tuple(paths)


def _safe_project_relative_path(value: JsonValue) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        path = project_relative_path(value, allow_root=False)
    except ProjectPathError:
        return None
    return path.as_posix()


def _action_kind(
    value: JsonValue | None,
) -> Literal["terminal", "file-editor", "task", "other"]:
    if value == "terminal":
        return "terminal"
    if value == "file-editor":
        return "file-editor"
    if value == "task":
        return "task"
    return "other"


def _specialist_capability(
    value: JsonValue | None,
) -> Literal["advisory", "project-actions"] | None:
    if value == "advisory":
        return "advisory"
    if value == "project-actions":
        return "project-actions"
    return None


def _approval_decision(
    value: JsonValue | None,
) -> Literal["approved", "denied"] | None:
    if value == "approved":
        return "approved"
    if value == "denied":
        return "denied"
    return None


def _resolved_action(
    action: ProjectionActionRecord,
    *,
    group_id: str,
    decision: Literal["approved", "denied"],
    sequence: int,
) -> ProjectionActionRecord:
    return action.model_copy(
        update={
            "group_id": group_id,
            "state": "rejected" if decision == "denied" else "approved",
            "decision": "rejected" if decision == "denied" else "approved",
            "updated_sequence": sequence,
        }
    )


def _append_approval_message(
    conversation: list[ProjectionMessage],
    event: SessionEvent,
    *,
    group_id: str,
    decision: Literal["approved", "denied"],
    action_count: int,
    reported_resolutions: set[str],
    automatic_rule: str | None = None,
) -> None:
    if group_id in reported_resolutions:
        return
    reported_resolutions.add(group_id)
    action_label = "action" if action_count == 1 else "actions"
    _append_message(
        conversation,
        event,
        role="trace",
        label="Approval",
        content=(
            f"Action set allowed automatically ({action_count} {action_label})"
            if automatic_rule is not None
            else f"Action set approved ({action_count} {action_label})"
            if decision == "approved"
            else f"Action set rejected ({action_count} {action_label})"
        ),
        detail=(
            f"Heartwood applied review rule {automatic_rule} to every action in the set."
            if automatic_rule is not None
            else "The decision applied to every action in the set."
        ),
    )


def _fail_integrity(
    reduction: _SessionReduction,
    event: SessionEvent,
    *,
    tool_call_ids: tuple[str, ...],
    detail: str,
) -> None:
    """Record contradictory history: the affected actions and the session become unrecoverable."""
    reduction.integrity_failed_action_ids.update(tool_call_ids)
    for tool_call_id in tool_call_ids:
        action = reduction.actions.get(tool_call_id)
        if action is not None:
            reduction.actions[tool_call_id] = action.model_copy(
                update={
                    "state": "outcome-unknown",
                    "outcome": None,
                    "updated_sequence": event.sequence,
                }
            )
    _append_message(
        reduction.conversation,
        event,
        role="trace",
        label="System",
        content="Session history failed an integrity check",
        detail=detail,
    )
    reduction.lifecycle_status = SessionLifecycle.ERROR
    reduction.lifecycle_sequence = event.sequence
    reduction.lifecycle_error_recoverable = False


def _pending_approval(
    actions: dict[str, ProjectionActionRecord],
    *,
    approval_group_actions: dict[str, list[str]],
    approval_group_decisions: dict[str, Literal["approved", "denied"] | None],
    retained_group_ids: set[str],
) -> ProjectionApprovalGroup | None:
    # Every other command is refused while an outside approval waits to be sent.
    for group_id, tool_call_ids in reversed(tuple(approval_group_actions.items())):
        if group_id in retained_group_ids:
            return ProjectionApprovalGroup(
                group_id=group_id,
                actions=tuple(
                    action
                    for tool_call_id in tool_call_ids
                    if (action := actions.get(tool_call_id))
                ),
                decision="approved",
            )
    for group_id, tool_call_ids in reversed(tuple(approval_group_actions.items())):
        if approval_group_decisions.get(group_id) is not None:
            continue
        group_actions = tuple(
            action for tool_call_id in tool_call_ids if (action := actions.get(tool_call_id))
        )
        if group_actions:
            return ProjectionApprovalGroup(group_id=group_id, actions=group_actions)
    return None


def _append_message(
    conversation: list[ProjectionMessage],
    event: SessionEvent,
    *,
    role: Literal["user", "agent", "trace"],
    label: str,
    content: str,
    message_id: str | None = None,
    detail: str | None = None,
    technical_detail: str | None = None,
) -> None:
    if not content:
        return
    conversation.append(
        ProjectionMessage(
            id=message_id or f"{event.event_id}-{role}",
            sequence=event.sequence,
            role=role,
            label=label,
            content=content,
            detail=detail,
            technical_detail=technical_detail,
        )
    )


def _activity(event: SessionEvent) -> ProjectionActivity:
    kind = EventKind(str(event.kind))
    return ProjectionActivity(
        sequence=event.sequence,
        kind=kind,
        label=_ACTIVITY_LABELS.get(kind.value, kind.value),
        detail=_activity_detail(event),
    )


def _activity_detail(event: SessionEvent) -> str:
    kind = str(event.kind)
    if kind in {EventKind.EXTERNAL_ACTION_RECORDED.value, EventKind.EXTERNAL_RESULT_RECORDED.value}:
        record = _mapping(event.payload.get("record"))
        return _string(record.get("phase"))
    if kind == EventKind.AUDIT_EVIDENCE_RECORDED.value:
        if event.payload.get("status") == "retained":
            return "Checkpoint retained outside the session"
        if event.payload.get("mode") == "required":
            return "External evidence pending; dispatch blocked"
        return "External evidence pending; buffered mode"
    if kind == EventKind.COMMAND_RECEIVED.value:
        return " · ".join(
            value
            for value in (
                _string(event.payload.get("command_kind")),
                _string(event.payload.get("command_id")),
            )
            if value
        )
    if kind == EventKind.MODEL_CALL_DECISION_RECORDED.value:
        decision = _mapping(event.payload.get("decision"))
        return " ".join(
            value
            for value in (
                _string(decision.get("decision")),
                _string(decision.get("endpoint")),
            )
            if value
        )
    if kind == EventKind.APPROVAL_RECORDED.value:
        return " · ".join(
            value
            for value in (
                _string(event.payload.get("decision")),
                (
                    "automatic rule " + _string(_mapping(event.payload.get("review")).get("rule"))
                    if event.payload.get("actor_id") == AUTOMATIC_REVIEW_ACTOR
                    else ""
                ),
                _string(event.payload.get("group_id")),
            )
            if value
        )
    if kind == EventKind.TOOL_CALL_PROPOSED.value:
        return _string(event.payload.get("tool_name"))
    if kind == EventKind.TOOL_EXECUTION_RECORDED.value:
        tool_name = _string(event.payload.get("tool_name"))
        exit_code = _string(event.payload.get("exit_code"))
        return " · ".join(
            value
            for value in (
                tool_name,
                f"exit={exit_code}" if exit_code else "",
            )
            if value
        )
    if kind == EventKind.CONFIRMATION_RESOLVED.value:
        return _string(event.payload.get("decision"))
    if kind == EventKind.AGENT_LIFECYCLE_UPDATED.value:
        return _string(event.payload.get("status"))
    if kind == EventKind.ERROR_RECORDED.value:
        return _string(event.payload.get("code")) or _string(event.payload.get("reason"))
    if kind == EventKind.WORKER_ATTEMPT_RECORDED.value:
        return _string(event.payload.get("phase"))
    return _string(event.payload.get("command_id"))


def _task(value: JsonValue) -> ProjectionTask:
    item = _mapping(value)
    status_value = _string(item.get("status"))
    status: Literal["todo", "in-progress", "done"]
    if status_value == "done":
        status = "done"
    elif status_value in {"in-progress", "in_progress"}:
        status = "in-progress"
    else:
        status = "todo"
    return ProjectionTask(
        title=_string(item.get("title")),
        status=status,
        status_label={
            "done": "Complete",
            "in-progress": "In Progress",
            "todo": "Not Started",
        }[status],
    )


def _usage(value: dict[str, JsonValue]) -> ProjectionUsage | None:
    model_name = _string(value.get("model_name"))
    if not model_name:
        return None
    usage_id = _string(value.get("usage_id")) or "total"
    return ProjectionUsage(
        usage_id=usage_id,
        purpose_label=_usage_purpose_label(usage_id),
        model_name=model_name,
        call_count=_integer(value.get("call_count")),
        prompt_tokens=_integer(value.get("prompt_tokens")),
        completion_tokens=_integer(value.get("completion_tokens")),
        cache_read_tokens=_integer(value.get("cache_read_tokens")),
        cache_write_tokens=_integer(value.get("cache_write_tokens")),
        reasoning_tokens=_integer(value.get("reasoning_tokens")),
        context_window=_optional_integer(value.get("context_window")),
        accumulated_cost=_number(value.get("accumulated_cost")),
    )


def _subagent(value: dict[str, JsonValue]) -> ProjectionSubagent:
    status_value = _string(value.get("status"))
    status: Literal["proposed", "running", "completed", "error", "rejected"]
    if status_value == "completed":
        status = "completed"
    elif status_value == "error":
        status = "error"
    elif status_value == "rejected":
        status = "rejected"
    elif status_value == "proposed":
        status = "proposed"
    else:
        status = "running"
    agent_name = _string(value.get("agent_name"))
    return ProjectionSubagent(
        invocation_id=_string(value.get("invocation_id")),
        task_id=_string(value.get("task_id")) or None,
        agent_name=agent_name,
        role_label=_string(value.get("role_label"))
        or _display_identifier(agent_name, fallback="Specialist"),
        status=status,
        status_label={
            "completed": "Complete",
            "error": "Stopped With an Error",
            "proposed": "Proposed",
            "rejected": "Not Run",
            "running": "Working",
        }[status],
        parent_session_id=_string(value.get("parent_session_id")),
        parent_action_id=_string(value.get("parent_action_id")),
        native_execution=(
            NativeTaskExecution.model_validate(value["native_execution"])
            if value.get("native_execution") is not None
            else None
        ),
        review_proposals=(
            ReviewProposals.model_validate(value["review_proposals"])
            if value.get("review_proposals") is not None
            else None
        ),
    )


def _enrich_subagent(
    subagent: ProjectionSubagent,
    *,
    actions: tuple[ProjectionActionRecord, ...],
) -> ProjectionSubagent:
    action = next(
        (item for item in actions if item.tool_call_id == subagent.invocation_id),
        None,
    ) or next(
        (
            item
            for item in actions
            if subagent.parent_action_id and item.action_id == subagent.parent_action_id
        ),
        None,
    )
    if action is None or action.details.kind != "task":
        return subagent
    task_summary = _bounded_summary(
        action.details.description or action.details.prompt,
    )
    result_summary = (
        None
        if action.outcome is None
        else _bounded_summary(action.outcome.result or action.outcome.summary)
    )
    status = "rejected" if action.state == "rejected" else subagent.status
    status_label = "Not Run" if status == "rejected" else subagent.status_label
    return subagent.model_copy(
        update={
            "task_summary": task_summary,
            "result_summary": result_summary,
            "status": status,
            "status_label": status_label,
        }
    )


def _usage_purpose_label(value: str) -> str:
    return {
        "agent": "Primary Agent",
        "condenser": "Context Management",
        "critic": "Response Review",
        "total": "Total Model Activity",
        _REPLACED_USAGE_ID: "Earlier Workers",
    }.get(value, _display_identifier(value, fallback="Model Activity"))


def _display_identifier(value: str, *, fallback: str) -> str:
    words = value.replace("_", "-").split("-")
    rendered = " ".join(word.capitalize() for word in words if word)
    return rendered or fallback


def _bounded_summary(value: str | None, *, limit: int = 240) -> str | None:
    if value is None:
        return None
    summary = " ".join(value.split())
    if not summary:
        return None
    return summary if len(summary) <= limit else f"{summary[: limit - 3].rstrip()}..."


def _researcher_status(
    lifecycle: SessionLifecycle,
    *,
    error_recoverable: bool,
    latest_action_denied: bool,
) -> ProjectionResearcherStatus:
    if lifecycle == SessionLifecycle.RUNNING:
        return ProjectionResearcherStatus(
            code="working",
            label="Heartwood Is Working",
            detail="You can send guidance or pause while the task is active.",
            tone="progress",
        )
    if lifecycle == SessionLifecycle.WAITING_FOR_CONFIRMATION:
        return ProjectionResearcherStatus(
            code="waiting-for-review",
            label="Waiting for Action Review",
            detail="Review the complete proposed action set to continue.",
            tone="attention",
        )
    if lifecycle == SessionLifecycle.PAUSED:
        return ProjectionResearcherStatus(
            code="paused",
            label="Agent Paused",
            detail="Resume the session when you are ready to continue.",
            tone="attention",
        )
    if lifecycle == SessionLifecycle.FINISHED:
        return ProjectionResearcherStatus(
            code="complete",
            label="Task Complete",
            detail="Review the result and project changes before continuing.",
            tone="success",
        )
    if lifecycle == SessionLifecycle.ERROR:
        return ProjectionResearcherStatus(
            code=("recoverable-failure" if error_recoverable else "terminal-failure"),
            label=("Task Needs Attention" if error_recoverable else "Session Recovery Required"),
            detail=(
                "Review the failure and provide corrected guidance to continue."
                if error_recoverable
                else "Start a new session after reviewing Activity & audit."
            ),
            tone="danger",
            recoverable=error_recoverable,
        )
    if latest_action_denied:
        return ProjectionResearcherStatus(
            code="denied",
            label="Action Set Rejected",
            detail="The proposed actions were not run. Heartwood is ready for revised guidance.",
            tone="attention",
        )
    return ProjectionResearcherStatus(
        code="ready",
        label="Ready",
        detail="Heartwood is ready for the next task.",
        tone="neutral",
    )


def _researcher_notice(
    command_outcome: ProjectionCommandOutcome | None,
    *,
    replacement_blocker: str | None,
) -> ProjectionResearcherNotice | None:
    if replacement_blocker is not None:
        return ProjectionResearcherNotice(
            notice_id="worker-replacement-unavailable",
            code="worker-replacement-unavailable",
            label="New Worker Unavailable",
            detail=(
                f"This session's worker was stopped, and a new worker cannot take over because "
                f"{replacement_blocker}. Continue in a new session."
            ),
            tone="attention",
        )
    if command_outcome is None or command_outcome.status != "rejected":
        return None
    return ProjectionResearcherNotice(
        notice_id=f"command:{command_outcome.command_id}:rejected",
        code="request-not-applied",
        label="Request Not Applied",
        detail=(command_outcome.message or "Review the request and session state before retrying."),
        tone="attention",
    )


def _latest_action_was_denied(actions: tuple[ProjectionActionRecord, ...]) -> bool:
    if not actions:
        return False
    latest_sequence = max(action.updated_sequence for action in actions)
    return any(
        action.updated_sequence == latest_sequence and action.decision == "rejected"
        for action in actions
    )


def _suggestions(
    *,
    lifecycle: SessionLifecycle,
    conversation: tuple[ProjectionMessage, ...],
    actions: tuple[ProjectionActionRecord, ...],
    tasks: tuple[ProjectionTask, ...],
    available_commands: tuple[Literal["chat", "pause", "resume", "approve", "deny"], ...],
    error_recoverable: bool,
) -> tuple[ProjectionSuggestion, ...]:
    if "chat" not in available_commands or lifecycle in {
        SessionLifecycle.RUNNING,
        SessionLifecycle.PAUSED,
        SessionLifecycle.WAITING_FOR_CONFIRMATION,
    }:
        return ()
    if lifecycle == SessionLifecycle.ERROR:
        return (
            (
                ProjectionSuggestion(
                    suggestion_id="recover-task",
                    label="Review the Failure",
                    prompt=(
                        "Review the last failure, explain the likely cause, and propose the safest "
                        "recovery step before changing project files."
                    ),
                    kind="recovery",
                ),
            )
            if error_recoverable
            else ()
        )

    substantive_messages = tuple(
        message for message in conversation if message.role in {"user", "agent"}
    )
    if not substantive_messages:
        return (
            ProjectionSuggestion(
                suggestion_id="inspect-project",
                label="Inspect the Project",
                prompt=(
                    "Inspect this project and summarize its structure, relevant files, and likely "
                    "entry points without changing files."
                ),
                kind="task",
            ),
            ProjectionSuggestion(
                suggestion_id="plan-project",
                label="Plan the Work",
                prompt=(
                    "Review this project and propose a concise, verifiable plan before making "
                    "changes."
                ),
                kind="task",
            ),
        )

    suggestions: list[ProjectionSuggestion] = []
    if any(task.status != "done" for task in tasks):
        suggestions.append(
            ProjectionSuggestion(
                suggestion_id="continue-plan",
                label="Continue the Plan",
                prompt="Continue with the next incomplete step in the current plan.",
                kind="follow-up",
            )
        )
    if any(action.affected_paths for action in actions if action.state == "succeeded"):
        suggestions.append(
            ProjectionSuggestion(
                suggestion_id="review-changes",
                label="Review the Changes",
                prompt=(
                    "Review the current project changes for correctness, safety, and missing "
                    "tests. Report findings before making additional changes."
                ),
                kind="follow-up",
            )
        )
    if lifecycle == SessionLifecycle.FINISHED:
        suggestions.append(
            ProjectionSuggestion(
                suggestion_id="verify-work",
                label="Verify the Work",
                prompt=(
                    "Run the most relevant available checks for the completed work and summarize "
                    "the results."
                ),
                kind="follow-up",
            )
        )
    if not suggestions:
        suggestions.append(
            ProjectionSuggestion(
                suggestion_id="identify-next-step",
                label="Identify the Next Step",
                prompt=(
                    "Review the current project and session state, then recommend the next safe, "
                    "verifiable step."
                ),
                kind="follow-up",
            )
        )
    return tuple(suggestions[:2])


def _lifecycle(value: str) -> SessionLifecycle:
    try:
        return SessionLifecycle(value)
    except ValueError:
        return SessionLifecycle.ERROR


def _available_commands(
    *,
    lifecycle: SessionLifecycle,
    pending_approval: ProjectionApprovalGroup | None,
    error_recoverable: bool,
) -> tuple[Literal["chat", "pause", "resume", "approve", "deny"], ...]:
    if pending_approval is not None:
        # A retained approval is sent only by its own retry, never by a new approval.
        return ("deny",) if pending_approval.decision == "approved" else ("approve", "deny")
    if lifecycle == SessionLifecycle.RUNNING:
        return ("chat", "pause")
    if lifecycle == SessionLifecycle.PAUSED:
        return ("chat", "resume")
    if lifecycle == SessionLifecycle.ERROR:
        return ("chat",) if error_recoverable else ()
    return ("chat",)


def _mapping(value: JsonValue | None) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _sequence(value: JsonValue | None) -> tuple[JsonValue, ...]:
    return tuple(value) if isinstance(value, list) else ()


def _string(value: JsonValue | None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return str(value)
    return ""


def _action_risk(
    value: JsonValue | None,
) -> Literal["high", "low", "medium", "unknown"]:
    if value == "high":
        return "high"
    if value == "low":
        return "low"
    if value == "medium":
        return "medium"
    return "unknown"


def _integer(value: JsonValue | None) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _signed_integer(value: JsonValue | None) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _optional_integer(value: JsonValue | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _optional_number(value: JsonValue | None) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def _number(value: JsonValue | None) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return max(0.0, float(value))
    return 0.0


def _error_detail(*, reason: str, code: str) -> str:
    message = reason or "Review Activity & audit, then try again."
    return f"{code}: {message}" if code else message


_ACTIVITY_LABELS = {
    EventKind.ACTION_DISPATCH_STARTED.value: "Action set sent",
    EventKind.AGENT_LIFECYCLE_UPDATED.value: "Agent lifecycle",
    EventKind.AGENT_MESSAGE_EMITTED.value: "Agent message",
    EventKind.APPROVAL_RECORDED.value: "Approval recorded",
    EventKind.AUDIT_EXPORT_RECORDED.value: "Audit export",
    EventKind.AUDIT_EVIDENCE_RECORDED.value: "Audit evidence",
    EventKind.SESSION_CLOSED.value: "Session owner closed",
    EventKind.COMMAND_RECEIVED.value: "Command received",
    EventKind.CONFIRMATION_REQUESTED.value: "Confirmation requested",
    EventKind.CONFIRMATION_RESOLVED.value: "Confirmation resolved",
    EventKind.ERROR_RECORDED.value: "Error",
    EventKind.EXTERNAL_ACTION_RECORDED.value: "Outside operation",
    EventKind.EXTERNAL_RESULT_RECORDED.value: "Outside result release",
    EventKind.MODEL_CALL_DECISION_RECORDED.value: "Model route decision",
    EventKind.MODEL_USAGE_UPDATED.value: "Model usage",
    EventKind.POLICY_DECISION_RECORDED.value: "Policy decision",
    EventKind.SESSION_PAUSED.value: "Session paused",
    EventKind.SESSION_RESUMED.value: "Session resumed",
    EventKind.SUBAGENT_UPDATED.value: "Specialized agent",
    EventKind.TASK_PLAN_UPDATED.value: "Task plan",
    EventKind.TOOL_EXECUTION_RECORDED.value: "Tool execution",
    EventKind.TOOL_CALL_PROPOSED.value: "Tool proposed",
    EventKind.USER_MESSAGE_RECORDED.value: "Researcher message",
    EventKind.WORKER_ATTEMPT_RECORDED.value: "Worker attempt",
}

__all__ = [
    "ProjectionActionDetails",
    "ProjectionActionOutcome",
    "ProjectionActionRecord",
    "ProjectionActivity",
    "ProjectionAffectedPath",
    "ProjectionApprovalGroup",
    "ProjectionCommandOutcome",
    "ProjectionFileEditorActionDetails",
    "ProjectionLifecycleState",
    "ProjectionMessage",
    "ProjectionModelContext",
    "ProjectionOtherActionDetails",
    "ProjectionOutsideContent",
    "ProjectionOutsideInput",
    "ProjectionOutsideOperationActionDetails",
    "ProjectionOutsideResultActionDetails",
    "ProjectionResearcherNotice",
    "ProjectionResearcherStatus",
    "ProjectionSubagent",
    "ProjectionSuggestion",
    "ProjectionTask",
    "ProjectionTaskActionDetails",
    "ProjectionTerminalActionDetails",
    "ProjectionUsage",
    "SessionLifecycle",
    "SessionProjection",
    "project_session",
]
