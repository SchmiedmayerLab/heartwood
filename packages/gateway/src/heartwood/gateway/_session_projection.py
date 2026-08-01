# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Gateway-owned session state presented consistently by every interface."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, computed_field

from heartwood.core_adapter import backend_error_is_fatal
from heartwood.gateway._workspace_paths import ProjectPathError, project_relative_path
from heartwood.session import CommandKind, EventKind, JsonValue, SessionEvent


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
    sequence: int
    kind: EventKind
    label: str
    detail: str


class ProjectionMessage(_ProjectionRecord):
    id: str
    sequence: int
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
    timeout: float | None = None
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
    resume: str | None = None


class ProjectionOtherActionDetails(_ProjectionRecord):
    """Typed fallback for an OpenHands tool without a specialized renderer."""

    kind: Literal["other"] = "other"


type ProjectionActionDetails = Annotated[
    ProjectionTerminalActionDetails
    | ProjectionFileEditorActionDetails
    | ProjectionTaskActionDetails
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
    outcome: ProjectionActionOutcome | None = None
    proposed_sequence: int = Field(serialization_alias="proposedSequence")
    updated_sequence: int = Field(serialization_alias="updatedSequence")


class ProjectionApprovalGroup(_ProjectionRecord):
    """One decision that applies to every listed OpenHands action."""

    group_id: str = Field(serialization_alias="groupId")
    actions: tuple[ProjectionActionRecord, ...]
    decision: Literal["approved", "denied"] | None = None
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


class ProjectionCommandOutcome(_ProjectionRecord):
    """Gateway-owned outcome of the most recently accepted command."""

    command_id: str = Field(serialization_alias="commandId")
    command_kind: CommandKind = Field(serialization_alias="commandKind")
    status: Literal["accepted", "rejected"]
    error_code: str | None = Field(default=None, serialization_alias="errorCode")
    message: str | None = None


class ProjectionTask(_ProjectionRecord):
    title: str
    status: Literal["todo", "in-progress", "done"]
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
    context_window: int | None = Field(default=None, serialization_alias="contextWindow")
    accumulated_cost: float = Field(default=0.0, ge=0, serialization_alias="accumulatedCost")


class ProjectionSubagent(_ProjectionRecord):
    invocation_id: str = Field(serialization_alias="invocationId")
    task_id: str | None = Field(default=None, serialization_alias="taskId")
    agent_name: str = Field(serialization_alias="agentName")
    role_label: str = Field(serialization_alias="roleLabel")
    status: Literal["proposed", "running", "completed", "error"]
    status_label: str = Field(serialization_alias="statusLabel")
    task_summary: str | None = Field(default=None, serialization_alias="taskSummary")
    result_summary: str | None = Field(default=None, serialization_alias="resultSummary")
    parent_session_id: str = Field(serialization_alias="parentSessionId")
    parent_action_id: str = Field(serialization_alias="parentActionId")


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


class SessionProjection(_ProjectionRecord):
    """Complete session projection owned by the gateway."""

    schema_version: Literal["heartwood.session-projection.v1"] = "heartwood.session-projection.v1"
    session_id: str = Field(serialization_alias="sessionId")
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
            command_outcome=None,
            latest_action_denied=False,
        ),
        serialization_alias="researcherStatus",
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


def project_session(
    events: tuple[SessionEvent, ...],
    *,
    session_id: str,
    streaming_text: str = "",
    stream_epoch: str = "standalone",
    stream_revision: int = 0,
) -> SessionProjection:
    """Reduce durable session events once at the gateway boundary."""
    activity: list[ProjectionActivity] = []
    conversation: list[ProjectionMessage] = []
    actions: dict[str, ProjectionActionRecord] = {}
    approval_group_actions: dict[str, list[str]] = {}
    approval_group_decisions: dict[str, Literal["approved", "denied"] | None] = {}
    approval_group_resolutions: dict[
        str,
        dict[str, Literal["approved", "denied"]],
    ] = {}
    integrity_failed_action_ids: set[str] = set()
    reported_resolutions: set[str] = set()
    context = ProjectionModelContext()
    lifecycle_status = SessionLifecycle.IDLE
    lifecycle_sequence = -1
    lifecycle_error_recoverable = True
    command_outcome: ProjectionCommandOutcome | None = None
    tasks: tuple[ProjectionTask, ...] = ()
    usage: ProjectionUsage | None = None
    usage_by_purpose: dict[str, ProjectionUsage] = {}
    subagents: dict[str, ProjectionSubagent] = {}

    for event in events:
        activity.append(_activity(event))
        kind = str(event.kind)
        if kind == EventKind.USER_MESSAGE_RECORDED.value:
            _append_message(
                conversation,
                event,
                role="user",
                label="You",
                content=_string(event.payload.get("content")),
                message_id=f"local-{_string(event.payload.get('command_id'))}",
            )
        elif kind == EventKind.COMMAND_RECEIVED.value:
            command_outcome = ProjectionCommandOutcome(
                command_id=_string(event.payload.get("command_id")),
                command_kind=CommandKind(_string(event.payload.get("command_kind"))),
                status="accepted",
            )
        elif kind == EventKind.AGENT_MESSAGE_EMITTED.value:
            _append_message(
                conversation,
                event,
                role="agent",
                label="Agent",
                content=_string(event.payload.get("content")),
            )
        elif kind == EventKind.TOOL_CALL_PROPOSED.value:
            tool_name = _string(event.payload.get("tool_name"))
            arguments = _mapping(event.payload.get("arguments"))
            tool_call_id = _string(event.payload.get("tool_call_id"))
            if tool_call_id:
                if tool_call_id in actions:
                    integrity_failed_action_ids.add(tool_call_id)
                    _mark_projection_integrity_failure(
                        actions,
                        conversation,
                        event,
                        tool_call_ids=(tool_call_id,),
                        detail="An action identity was proposed more than once.",
                    )
                    lifecycle_status = SessionLifecycle.ERROR
                    lifecycle_sequence = event.sequence
                    lifecycle_error_recoverable = False
                    continue
                actions[tool_call_id] = _action_record(
                    event,
                    payload=event.payload,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                )
        elif kind == EventKind.TOOL_EXECUTION_RECORDED.value:
            tool_name = _string(event.payload.get("tool_name"))
            tool_call_id = _string(event.payload.get("tool_call_id"))
            action = actions.get(tool_call_id)
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
            if tool_call_id:
                if action is not None and action.decision == "rejected":
                    integrity_failed_action_ids.add(tool_call_id)
                    _mark_projection_integrity_failure(
                        actions,
                        conversation,
                        event,
                        tool_call_ids=(tool_call_id,),
                        detail="A rejected action has a recorded execution.",
                    )
                    lifecycle_status = SessionLifecycle.ERROR
                    lifecycle_sequence = event.sequence
                    lifecycle_error_recoverable = False
                    continue
                if action is None:
                    action = _action_record(
                        event,
                        payload=event.payload,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        arguments={},
                    )
                    actions[tool_call_id] = action
                    integrity_failed_action_ids.add(tool_call_id)
                    _mark_projection_integrity_failure(
                        actions,
                        conversation,
                        event,
                        tool_call_ids=(tool_call_id,),
                        detail="An action execution has no matching proposal.",
                    )
                    lifecycle_status = SessionLifecycle.ERROR
                    lifecycle_sequence = event.sequence
                    lifecycle_error_recoverable = False
                    continue
                action_id = _string(event.payload.get("action_id")) or None
                if (
                    (action.action_id is not None and action.action_id != action_id)
                    or action.tool_name != tool_name
                    or action.outcome is not None
                ):
                    integrity_failed_action_ids.add(tool_call_id)
                    _mark_projection_integrity_failure(
                        actions,
                        conversation,
                        event,
                        tool_call_ids=(tool_call_id,),
                        detail="An action execution conflicts with its stable identity.",
                    )
                    lifecycle_status = SessionLifecycle.ERROR
                    lifecycle_sequence = event.sequence
                    lifecycle_error_recoverable = False
                    continue
                actions[tool_call_id] = action.model_copy(
                    update={
                        "action_id": action.action_id or action_id,
                        "decision": action.decision or "approved",
                        "state": "failed" if exit_code != 0 else "succeeded",
                        "outcome": outcome,
                        "updated_sequence": event.sequence,
                    }
                )
        elif kind == EventKind.CONFIRMATION_REQUESTED.value:
            request = _mapping(event.payload.get("request"))
            tool_call_id = _string(request.get("tool_call_id"))
            if tool_call_id:
                group_id = _string(request.get("group_id")) or "pending-action-set"
                existing_action = actions.get(tool_call_id)
                request_action_id = _string(request.get("action_id")) or None
                request_tool_name = _string(request.get("tool_name"))
                if existing_action is not None and (
                    (
                        request_action_id is not None
                        and existing_action.action_id is not None
                        and request_action_id != existing_action.action_id
                    )
                    or request_tool_name != existing_action.tool_name
                    or (
                        existing_action.group_id is not None
                        and existing_action.group_id != group_id
                    )
                ):
                    integrity_failed_action_ids.add(tool_call_id)
                    _mark_projection_integrity_failure(
                        actions,
                        conversation,
                        event,
                        tool_call_ids=(tool_call_id,),
                        detail="An approval request conflicts with its stable action identity.",
                    )
                    lifecycle_status = SessionLifecycle.ERROR
                    lifecycle_sequence = event.sequence
                    lifecycle_error_recoverable = False
                    continue
                action = existing_action or _action_record(
                    event,
                    payload=request,
                    tool_call_id=tool_call_id,
                    tool_name=request_tool_name,
                    arguments=_mapping(request.get("arguments")),
                )
                actions[tool_call_id] = action.model_copy(
                    update={
                        "group_id": group_id,
                        "state": "awaiting-review",
                        "updated_sequence": event.sequence,
                    }
                )
                group_actions = approval_group_actions.setdefault(group_id, [])
                if tool_call_id not in group_actions:
                    group_actions.append(tool_call_id)
                approval_group_decisions[group_id] = None
                lifecycle_status = SessionLifecycle.WAITING_FOR_CONFIRMATION
        elif kind == EventKind.APPROVAL_RECORDED.value:
            group_id = _string(event.payload.get("group_id"))
            decision = _approval_decision(event.payload.get("decision"))
            tool_call_ids = tuple(
                item
                for item in (
                    _string(value) for value in _sequence(event.payload.get("tool_call_ids"))
                )
                if item
            )
            if group_id and decision is not None and tool_call_ids:
                group_actions = approval_group_actions.setdefault(group_id, [])
                for tool_call_id in tool_call_ids:
                    if tool_call_id not in group_actions:
                        group_actions.append(tool_call_id)
                    action = actions.get(tool_call_id)
                    if action is not None:
                        actions[tool_call_id] = _resolved_action(
                            action,
                            group_id=group_id,
                            decision=decision,
                            sequence=event.sequence,
                        )
                approval_group_decisions[group_id] = decision
                approval_group_resolutions[group_id] = dict.fromkeys(tool_call_ids, decision)
                _append_approval_message(
                    conversation,
                    event,
                    group_id=group_id,
                    decision=decision,
                    action_count=len(tool_call_ids),
                    reported_resolutions=reported_resolutions,
                )
                lifecycle_status = SessionLifecycle.IDLE
        elif kind == EventKind.CONFIRMATION_RESOLVED.value:
            tool_call_id = _string(event.payload.get("tool_call_id"))
            group_id = _string(event.payload.get("group_id"))
            if not group_id:
                group_id = next(
                    (
                        candidate_group_id
                        for candidate_group_id, candidate_actions in approval_group_actions.items()
                        if tool_call_id in candidate_actions
                    ),
                    "",
                )
            approval_decision = _approval_decision(event.payload.get("decision"))
            current_actions = approval_group_actions.get(group_id)
            if current_actions is not None and approval_decision is not None:
                group_decision = approval_group_decisions.get(group_id)
                if group_decision is not None and group_decision != approval_decision:
                    integrity_failed_action_ids.update(current_actions)
                    _mark_projection_integrity_failure(
                        actions,
                        conversation,
                        event,
                        tool_call_ids=tuple(current_actions),
                        detail="An action-set resolution contradicts its recorded decision.",
                    )
                    lifecycle_status = SessionLifecycle.ERROR
                    lifecycle_sequence = event.sequence
                    lifecycle_error_recoverable = False
                    continue
                resolutions = approval_group_resolutions.setdefault(group_id, {})
                resolutions[tool_call_id] = approval_decision
                action = actions.get(tool_call_id)
                if action is not None:
                    actions[tool_call_id] = _resolved_action(
                        action,
                        group_id=group_id,
                        decision=approval_decision,
                        sequence=event.sequence,
                    )
                resolved_decisions = {resolutions.get(candidate) for candidate in current_actions}
                if (
                    None not in resolved_decisions
                    and len(resolved_decisions) == 1
                    and group_decision is None
                ):
                    approval_group_decisions[group_id] = approval_decision
                    _append_approval_message(
                        conversation,
                        event,
                        group_id=group_id,
                        decision=approval_decision,
                        action_count=len(current_actions),
                        reported_resolutions=reported_resolutions,
                    )
                    lifecycle_status = SessionLifecycle.IDLE
        elif kind == EventKind.MODEL_CALL_DECISION_RECORDED.value:
            model_decision = _mapping(event.payload.get("decision"))
            context = ProjectionModelContext(
                model_endpoint=_string(model_decision.get("endpoint")) or None,
                model_decision=_string(model_decision.get("decision")) or None,
                model_reason=_string(model_decision.get("reason")) or None,
            )
        elif kind == EventKind.AGENT_LIFECYCLE_UPDATED.value:
            lifecycle_status = _lifecycle(_string(event.payload.get("status")))
            lifecycle_sequence = event.sequence
        elif kind == EventKind.TASK_PLAN_UPDATED.value:
            tasks = tuple(_task(item) for item in _sequence(event.payload.get("tasks")))
        elif kind == EventKind.MODEL_USAGE_UPDATED.value:
            usage_update = _usage(_mapping(event.payload.get("usage")))
            if usage_update is not None:
                if usage_update.usage_id == "total":
                    usage = usage_update
                else:
                    usage_by_purpose[usage_update.usage_id] = usage_update
        elif kind == EventKind.SUBAGENT_UPDATED.value:
            subagent = _subagent(_mapping(event.payload.get("subagent")))
            if subagent.invocation_id:
                subagents[subagent.invocation_id] = subagent
        elif kind == EventKind.SESSION_PAUSED.value:
            lifecycle_status = SessionLifecycle.PAUSED
            lifecycle_sequence = event.sequence
        elif kind == EventKind.SESSION_RESUMED.value:
            lifecycle_status = SessionLifecycle.RUNNING
            lifecycle_sequence = event.sequence
        elif kind == EventKind.ERROR_RECORDED.value:
            error_code = _string(event.payload.get("code"))
            error_message = _error_detail(
                reason=_string(event.payload.get("reason")),
                code=error_code,
            )
            _append_message(
                conversation,
                event,
                role="trace",
                label="System",
                content="The task could not be completed",
                detail=error_message,
            )
            if command_outcome is not None:
                command_outcome = command_outcome.model_copy(
                    update={
                        "status": "rejected",
                        "error_code": error_code or None,
                        "message": error_message,
                    }
                )
            if event.payload.get("affects_lifecycle") is not False:
                lifecycle_status = SessionLifecycle.ERROR
                lifecycle_sequence = event.sequence
                lifecycle_error_recoverable = (
                    lifecycle_error_recoverable and not backend_error_is_fatal(error_code)
                )

    if lifecycle_status == SessionLifecycle.RUNNING:
        actions = {
            tool_call_id: (
                action.model_copy(
                    update={
                        "state": "running",
                        "updated_sequence": max(
                            action.updated_sequence,
                            lifecycle_sequence,
                        ),
                    }
                )
                if action.state in {"approved", "proposed"}
                else action
            )
            for tool_call_id, action in actions.items()
        }
    if not lifecycle_error_recoverable:
        lifecycle_status = SessionLifecycle.ERROR
        actions = {
            tool_call_id: (
                action.model_copy(
                    update={
                        "state": "outcome-unknown",
                        "updated_sequence": max(
                            action.updated_sequence,
                            lifecycle_sequence,
                        ),
                    }
                )
                if action.outcome is None and action.state not in {"rejected", "awaiting-review"}
                else action
            )
            for tool_call_id, action in actions.items()
        }
    for tool_call_id in integrity_failed_action_ids:
        action = actions.get(tool_call_id)
        if action is not None:
            actions[tool_call_id] = action.model_copy(
                update={
                    "state": "outcome-unknown",
                    "outcome": None,
                }
            )

    pending_approval = _pending_approval(
        actions,
        approval_group_actions=approval_group_actions,
        approval_group_decisions=approval_group_decisions,
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
        _enrich_subagent(subagent, actions=projected_actions) for subagent in subagents.values()
    )
    available_commands = _available_commands(
        lifecycle=lifecycle_status,
        has_pending_approval=pending_approval is not None,
        error_recoverable=lifecycle_error_recoverable,
    )
    return SessionProjection(
        session_id=session_id,
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
        activity=tuple(activity),
        conversation=tuple(conversation),
        actions=projected_actions,
        pending_approval=pending_approval,
        context=context,
        lifecycle=lifecycle,
        researcher_status=_researcher_status(
            lifecycle_status,
            error_recoverable=lifecycle_error_recoverable,
            command_outcome=command_outcome,
            latest_action_denied=_latest_action_was_denied(projected_actions),
        ),
        last_command_outcome=command_outcome,
        task_plan=tasks,
        usage=usage,
        usage_by_purpose=tuple(usage_by_purpose.values()),
        subagents=projected_subagents,
        suggestions=_suggestions(
            lifecycle=lifecycle_status,
            conversation=tuple(conversation),
            actions=projected_actions,
            tasks=tasks,
            available_commands=available_commands,
            error_recoverable=lifecycle_error_recoverable,
        ),
        streaming_text=(streaming_text if lifecycle_status == SessionLifecycle.RUNNING else ""),
        available_commands=available_commands,
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
            resume=_string(arguments.get("resume")) or None,
        )
    return ProjectionOtherActionDetails()


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
            f"Action set approved ({action_count} {action_label})"
            if decision == "approved"
            else f"Action set rejected ({action_count} {action_label})"
        ),
        detail="The decision applied to every action in the set.",
    )


def _mark_projection_integrity_failure(
    actions: dict[str, ProjectionActionRecord],
    conversation: list[ProjectionMessage],
    event: SessionEvent,
    *,
    tool_call_ids: tuple[str, ...],
    detail: str,
) -> None:
    for tool_call_id in tool_call_ids:
        action = actions.get(tool_call_id)
        if action is not None:
            actions[tool_call_id] = action.model_copy(
                update={
                    "state": "outcome-unknown",
                    "outcome": None,
                    "updated_sequence": event.sequence,
                }
            )
    _append_message(
        conversation,
        event,
        role="trace",
        label="System",
        content="Session history failed an integrity check",
        detail=detail,
    )


def _pending_approval(
    actions: dict[str, ProjectionActionRecord],
    *,
    approval_group_actions: dict[str, list[str]],
    approval_group_decisions: dict[str, Literal["approved", "denied"] | None],
) -> ProjectionApprovalGroup | None:
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
    status: Literal["proposed", "running", "completed", "error"]
    if status_value == "completed":
        status = "completed"
    elif status_value == "error":
        status = "error"
    elif status_value == "proposed":
        status = "proposed"
    else:
        status = "running"
    agent_name = _string(value.get("agent_name"))
    return ProjectionSubagent(
        invocation_id=_string(value.get("invocation_id")),
        task_id=_string(value.get("task_id")) or None,
        agent_name=agent_name,
        role_label=_subagent_role_label(agent_name),
        status=status,
        status_label={
            "completed": "Complete",
            "error": "Stopped With an Error",
            "proposed": "Proposed",
            "running": "Working",
        }[status],
        parent_session_id=_string(value.get("parent_session_id")),
        parent_action_id=_string(value.get("parent_action_id")),
    )


def _enrich_subagent(
    subagent: ProjectionSubagent,
    *,
    actions: tuple[ProjectionActionRecord, ...],
) -> ProjectionSubagent:
    action = next(
        (
            item
            for item in actions
            if item.tool_call_id == subagent.invocation_id
            or item.action_id == subagent.parent_action_id
        ),
        None,
    )
    if action is None or action.details.kind != "task":
        return subagent
    task_summary = _bounded_summary(
        action.details.description or action.details.prompt,
    )
    result_summary = None if action.outcome is None else _bounded_summary(action.outcome.summary)
    return subagent.model_copy(
        update={
            "task_summary": task_summary,
            "result_summary": result_summary,
        }
    )


def _usage_purpose_label(value: str) -> str:
    return {
        "agent": "Primary Agent",
        "condenser": "Context Management",
        "critic": "Response Review",
        "total": "Total Model Activity",
    }.get(value, _display_identifier(value, fallback="Model Activity"))


def _subagent_role_label(value: str) -> str:
    return {
        "research-planner": "Research Planner",
    }.get(value, _display_identifier(value, fallback="Specialist"))


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
    command_outcome: ProjectionCommandOutcome | None,
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
    if command_outcome is not None and command_outcome.status == "rejected":
        return ProjectionResearcherStatus(
            code="denied",
            label="Request Not Applied",
            detail=(
                command_outcome.message or "Review the request and session state before retrying."
            ),
            tone="attention",
        )
    return ProjectionResearcherStatus(
        code="ready",
        label="Ready",
        detail="Heartwood is ready for the next task.",
        tone="neutral",
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
    has_pending_approval: bool,
    error_recoverable: bool,
) -> tuple[Literal["chat", "pause", "resume", "approve", "deny"], ...]:
    if has_pending_approval:
        return ("approve", "deny")
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
    EventKind.AGENT_LIFECYCLE_UPDATED.value: "Agent lifecycle",
    EventKind.AGENT_MESSAGE_EMITTED.value: "Agent message",
    EventKind.APPROVAL_RECORDED.value: "Approval recorded",
    EventKind.AUDIT_EXPORT_RECORDED.value: "Audit export",
    EventKind.COMMAND_RECEIVED.value: "Command received",
    EventKind.CONFIRMATION_REQUESTED.value: "Confirmation requested",
    EventKind.CONFIRMATION_RESOLVED.value: "Confirmation resolved",
    EventKind.ERROR_RECORDED.value: "Error",
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
