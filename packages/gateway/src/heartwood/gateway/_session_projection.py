# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Gateway-owned session state presented consistently by every interface."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, computed_field

from heartwood.gateway._action_presentation import action_tool_label
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


class ProjectionApprovalAction(_ProjectionRecord):
    """One member of an atomic OpenHands action group."""

    target_id: str = Field(serialization_alias="targetId")
    tool_name: str = Field(serialization_alias="toolName")
    risk: Literal["high", "low", "medium", "unknown"] | None = None
    summary: str | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ProjectionApprovalGroup(_ProjectionRecord):
    """One decision that applies to every listed OpenHands action."""

    group_id: str = Field(serialization_alias="groupId")
    actions: tuple[ProjectionApprovalAction, ...]
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


class ProjectionUsage(_ProjectionRecord):
    usage_id: str = Field(serialization_alias="usageId")
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
    status: Literal["proposed", "running", "completed", "error"]
    parent_session_id: str = Field(serialization_alias="parentSessionId")
    parent_action_id: str = Field(serialization_alias="parentActionId")


class SessionProjection(_ProjectionRecord):
    """Complete session projection owned by the gateway."""

    schema_version: Literal["heartwood.session-projection.v1"] = "heartwood.session-projection.v1"
    session_id: str = Field(serialization_alias="sessionId")
    event_count: int = Field(ge=0, serialization_alias="eventCount")
    revision: int = Field(ge=-1)
    stream_epoch: str = Field(default="standalone", serialization_alias="streamEpoch")
    stream_revision: int = Field(default=0, ge=0, serialization_alias="streamRevision")
    activity: tuple[ProjectionActivity, ...] = ()
    conversation: tuple[ProjectionMessage, ...] = ()
    pending_approval: ProjectionApprovalGroup | None = Field(
        default=None,
        serialization_alias="pendingApproval",
    )
    context: ProjectionModelContext = Field(default_factory=ProjectionModelContext)
    lifecycle: ProjectionLifecycleState = Field(default_factory=ProjectionLifecycleState)
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
    approval_groups: dict[str, ProjectionApprovalGroup] = {}
    reported_resolutions: set[str] = set()
    context = ProjectionModelContext()
    lifecycle_status = SessionLifecycle.IDLE
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
            _append_message(
                conversation,
                event,
                role="trace",
                label="Trace",
                content=f"Proposed {action_tool_label(tool_name)}",
                detail=_string(event.payload.get("summary")) or None,
                technical_detail=(
                    json.dumps(arguments, indent=2, sort_keys=True) if arguments else None
                ),
            )
        elif kind == EventKind.TOOL_EXECUTION_RECORDED.value:
            tool_name = _string(event.payload.get("tool_name"))
            _append_message(
                conversation,
                event,
                role="trace",
                label="Tool",
                content=f"Ran {action_tool_label(tool_name)}",
                detail=(
                    _string(event.payload.get("summary"))
                    or f"Exit {_string(event.payload.get('exit_code')) or 'unknown'}"
                ),
            )
        elif kind == EventKind.CONFIRMATION_REQUESTED.value:
            request = _mapping(event.payload.get("request"))
            tool_call_id = _string(request.get("tool_call_id"))
            if tool_call_id:
                group_id = _string(request.get("group_id")) or "pending-action-set"
                action = ProjectionApprovalAction(
                    target_id=tool_call_id,
                    tool_name=_string(request.get("tool_name")),
                    risk=_action_risk(request.get("risk")),
                    summary=_string(request.get("summary")) or None,
                    arguments=_mapping(request.get("arguments")),
                )
                current_group = approval_groups.get(group_id)
                current_actions = () if current_group is None else current_group.actions
                if not any(item.target_id == tool_call_id for item in current_actions):
                    current_actions = (*current_actions, action)
                approval_groups[group_id] = ProjectionApprovalGroup(
                    group_id=group_id,
                    actions=current_actions,
                )
                lifecycle_status = SessionLifecycle.WAITING_FOR_CONFIRMATION
        elif kind == EventKind.CONFIRMATION_RESOLVED.value:
            tool_call_id = _string(event.payload.get("tool_call_id"))
            group_id = _string(event.payload.get("group_id"))
            if not group_id:
                group_id = next(
                    (
                        candidate.group_id
                        for candidate in approval_groups.values()
                        if any(action.target_id == tool_call_id for action in candidate.actions)
                    ),
                    "",
                )
            decision_value = _string(event.payload.get("decision"))
            approval_decision: Literal["approved", "denied"] = (
                "denied" if decision_value == "denied" else "approved"
            )
            current = approval_groups.get(group_id)
            if current is not None:
                approval_groups[group_id] = current.model_copy(
                    update={"decision": approval_decision}
                )
                if group_id not in reported_resolutions:
                    reported_resolutions.add(group_id)
                    action_count = len(current.actions)
                    action_label = "action" if action_count == 1 else "actions"
                    _append_message(
                        conversation,
                        event,
                        role="trace",
                        label="Approval",
                        content=(
                            f"Action set approved ({action_count} {action_label})"
                            if approval_decision == "approved"
                            else f"Action set rejected ({action_count} {action_label})"
                        ),
                        detail="The decision applied to every action in the set.",
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
        elif kind == EventKind.SESSION_RESUMED.value:
            lifecycle_status = SessionLifecycle.RUNNING
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

    lifecycle = ProjectionLifecycleState(
        status=lifecycle_status,
        can_pause=lifecycle_status == SessionLifecycle.RUNNING,
        can_resume=lifecycle_status == SessionLifecycle.PAUSED,
        can_steer=lifecycle_status
        in {
            SessionLifecycle.IDLE,
            SessionLifecycle.RUNNING,
            SessionLifecycle.PAUSED,
            SessionLifecycle.FINISHED,
        },
    )
    pending_approval = next(
        (group for group in reversed(tuple(approval_groups.values())) if group.decision is None),
        None,
    )
    return SessionProjection(
        session_id=session_id,
        event_count=len(events),
        revision=events[-1].sequence if events else -1,
        stream_epoch=stream_epoch,
        stream_revision=stream_revision,
        activity=tuple(activity),
        conversation=tuple(conversation),
        pending_approval=pending_approval,
        context=context,
        lifecycle=lifecycle,
        last_command_outcome=command_outcome,
        task_plan=tasks,
        usage=usage,
        usage_by_purpose=tuple(usage_by_purpose.values()),
        subagents=tuple(subagents.values()),
        streaming_text=(streaming_text if lifecycle_status == SessionLifecycle.RUNNING else ""),
        available_commands=_available_commands(
            lifecycle=lifecycle_status,
            has_pending_approval=pending_approval is not None,
        ),
    )


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
    )


def _usage(value: dict[str, JsonValue]) -> ProjectionUsage | None:
    model_name = _string(value.get("model_name"))
    if not model_name:
        return None
    return ProjectionUsage(
        usage_id=_string(value.get("usage_id")) or "total",
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
    return ProjectionSubagent(
        invocation_id=_string(value.get("invocation_id")),
        task_id=_string(value.get("task_id")) or None,
        agent_name=_string(value.get("agent_name")),
        status=status,
        parent_session_id=_string(value.get("parent_session_id")),
        parent_action_id=_string(value.get("parent_action_id")),
    )


def _lifecycle(value: str) -> SessionLifecycle:
    try:
        return SessionLifecycle(value)
    except ValueError:
        return SessionLifecycle.ERROR


def _available_commands(
    *,
    lifecycle: SessionLifecycle,
    has_pending_approval: bool,
) -> tuple[Literal["chat", "pause", "resume", "approve", "deny"], ...]:
    if has_pending_approval:
        return ("approve", "deny")
    if lifecycle == SessionLifecycle.RUNNING:
        return ("chat", "pause")
    if lifecycle == SessionLifecycle.PAUSED:
        return ("chat", "resume")
    if lifecycle == SessionLifecycle.ERROR:
        return ()
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
    if value in {"high", "low", "medium", "unknown"}:
        return cast(Literal["high", "low", "medium", "unknown"], value)
    return "unknown"


def _integer(value: JsonValue | None) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _optional_integer(value: JsonValue | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


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
    "ProjectionActivity",
    "ProjectionApprovalAction",
    "ProjectionApprovalGroup",
    "ProjectionLifecycleState",
    "ProjectionMessage",
    "ProjectionModelContext",
    "ProjectionSubagent",
    "ProjectionTask",
    "ProjectionUsage",
    "SessionLifecycle",
    "SessionProjection",
    "project_session",
]
