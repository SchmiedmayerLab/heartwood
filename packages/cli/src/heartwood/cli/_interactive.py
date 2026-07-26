# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Framework-neutral interaction controller for terminal clients."""

from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from heartwood.gateway import (
    ACTION_MODE_OPTIONS,
    ActionSettingsError,
    ModelSettingsError,
    ProjectionApprovalGroup,
    SessionGateway,
    SessionProjection,
    action_mode_label,
    action_risk_label,
    action_tool_label,
)
from heartwood.schemas import ActionSettingsResponse
from heartwood.session import (
    CommandKind,
    JsonValue,
    SessionCommand,
    SessionEvent,
    new_command_id,
)


@dataclass(frozen=True, slots=True)
class InteractionResult:
    """One user interaction projected for a terminal client."""

    events: tuple[SessionEvent, ...] = ()
    projection: SessionProjection | None = None
    message: str | None = None
    exit_requested: bool = False
    error: bool = False
    replace_transcript: bool = False

    @property
    def failed(self) -> bool:
        """Return whether this interaction recorded an error."""
        outcome = None if self.projection is None else self.projection.last_command_outcome
        return (
            self.error
            or (self.projection is not None and self.projection.lifecycle.status == "error")
            or (outcome is not None and outcome.status == "rejected")
        )


@dataclass(frozen=True, slots=True)
class InteractionActivity:
    """Honest waiting copy for one blocking terminal interaction."""

    label: str
    waiting_label: str
    guidance: str


_TASK_ACTIVITY = InteractionActivity(
    label="Working on your task",
    waiting_label="Still working on your task",
    guidance="Response time depends on the selected model and task.",
)
_DEFAULT_ACTIVITY = InteractionActivity(
    label="Running the command",
    waiting_label="Still running the command",
    guidance="Heartwood is waiting for the operation to complete.",
)
_COMMAND_ACTIVITIES = {
    "/allow": InteractionActivity(
        label="Continuing the approved action set",
        waiting_label="Still continuing the action set",
        guidance="The model may need time to process the tool results.",
    ),
    "/reject": InteractionActivity(
        label="Rejecting the action set",
        waiting_label="Still rejecting the action set",
        guidance="Heartwood is waiting for the session to settle.",
    ),
    "/pause": InteractionActivity(
        label="Pausing the session",
        waiting_label="Still pausing the session",
        guidance="Heartwood is waiting for the active operation to stop safely.",
    ),
    "/resume": InteractionActivity(
        label="Resuming the session",
        waiting_label="Still resuming the session",
        guidance="Response time depends on the selected model and task.",
    ),
    "/replay": InteractionActivity(
        label="Loading the conversation",
        waiting_label="Still loading the conversation",
        guidance="A long session can take additional time to restore.",
    ),
    "/audit-export": InteractionActivity(
        label="Preparing the audit export",
        waiting_label="Still preparing the audit export",
        guidance="Large session histories can take additional time to process.",
    ),
    "/status": InteractionActivity(
        label="Checking the session",
        waiting_label="Still checking the session",
        guidance="Heartwood is waiting for the project services to respond.",
    ),
    "/permissions": InteractionActivity(
        label="Updating action review",
        waiting_label="Still updating action review",
        guidance="Heartwood is waiting for the active project services to close safely.",
    ),
}


class InteractiveSession:
    """Translate terminal input into the shared gateway command contract."""

    def __init__(self, gateway: SessionGateway, *, session_id: str) -> None:
        self.gateway = gateway
        self.session_id = session_id

    def replay(self) -> SessionProjection:
        """Return the gateway-owned session projection."""
        return self.gateway.session_projection(session_id=self.session_id)

    def pending_approval(self) -> ProjectionApprovalGroup | None:
        """Return the complete unresolved OpenHands action group."""
        return self.replay().pending_approval

    def wait_until_stable(self, *, poll_interval: float = 0.1) -> SessionProjection:
        """Wait for a background run to reach an interactive boundary."""
        while True:
            projection = self.replay()
            if projection.lifecycle.status != "running":
                return projection
            time.sleep(poll_interval)

    def action_settings(self) -> ActionSettingsResponse:
        """Return the shared project action-confirmation settings."""
        return self.gateway.action_settings()

    def select_action_mode(self, value: str) -> ActionSettingsResponse:
        """Select an action-confirmation mode by its public command value."""
        mode = next(
            (
                option.mode
                for option in ACTION_MODE_OPTIONS
                if value in {option.command_value, option.mode}
            ),
            None,
        )
        if mode is None:
            raise ActionSettingsError(f"unsupported action confirmation mode: {value}")
        projection = self.replay()
        if projection.pending_approval is not None:
            raise ActionSettingsError(
                "resolve the pending action set before changing the action-review mode"
            )
        if projection.lifecycle.status == "running":
            raise ActionSettingsError(
                "wait for the active task to reach a review point before changing "
                "the action-review mode"
            )
        return self.gateway.select_action_confirmation_mode(mode)

    def submit(self, line: str) -> InteractionResult:
        """Submit a prompt or slash command."""
        text = line.strip()
        if not text:
            return InteractionResult()
        if not text.startswith("/"):
            events = self._handle(CommandKind.CHAT, {"prompt": text})
            return InteractionResult(events=events, projection=self.replay())
        try:
            parts = shlex.split(text)
        except ValueError:
            return InteractionResult(message="Invalid command syntax.")
        directive = parts[0]
        if directive in {"/quit", "/exit"} and len(parts) == 1:
            return InteractionResult(exit_requested=True)
        if directive in {"/allow", "/reject"} and len(parts) in {1, 2}:
            target = self._decision_target(parts[1] if len(parts) == 2 else None)
            if target is None:
                return InteractionResult(
                    message="No actions are awaiting review.",
                    error=True,
                )
            kind = CommandKind.APPROVE if directive == "/allow" else CommandKind.DENY
            events = self._handle(
                kind,
                {"target_type": "action-set", "target_id": target},
            )
            return InteractionResult(
                events=events,
                projection=self.replay(),
            )
        if directive == "/pause" and len(parts) == 1:
            events = self._handle(CommandKind.PAUSE)
            return InteractionResult(events=events, projection=self.replay())
        if directive == "/resume" and len(parts) == 1:
            events = self._handle(CommandKind.RESUME)
            return InteractionResult(events=events, projection=self.replay())
        if directive == "/replay" and len(parts) == 1:
            return InteractionResult(projection=self.replay(), replace_transcript=True)
        if directive == "/audit-export" and len(parts) == 1:
            events = self._handle(CommandKind.AUDIT_EXPORT)
            return InteractionResult(events=events, projection=self.replay())
        if directive == "/status" and len(parts) == 1:
            try:
                return InteractionResult(message=format_model_status(self.gateway))
            except ModelSettingsError as error:
                return InteractionResult(message=str(error))
        if directive == "/permissions" and len(parts) in {1, 2}:
            try:
                settings = (
                    self.action_settings() if len(parts) == 1 else self.select_action_mode(parts[1])
                )
            except ActionSettingsError as error:
                return InteractionResult(message=str(error), error=True)
            return InteractionResult(message=format_action_settings(settings))
        if directive == "/help" and len(parts) == 1:
            return InteractionResult(message=command_help())
        return InteractionResult(message=f"Unknown command: {directive}")

    def _decision_target(self, requested_id: str | None) -> str | None:
        approval = self.pending_approval()
        if approval is None:
            return None
        if requested_id is None:
            return approval.group_id
        if requested_id == approval.group_id:
            return requested_id
        return requested_id

    def _handle(
        self,
        kind: CommandKind,
        payload: dict[str, JsonValue] | None = None,
    ) -> tuple[SessionEvent, ...]:
        command = SessionCommand(
            command_id=new_command_id(self.session_id, kind),
            session_id=self.session_id,
            kind=kind,
            actor_id="human",
            created_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            payload={} if payload is None else payload,
        )
        return self.gateway.handle(command).events


def command_help() -> str:
    """Return the commands common to terminal clients."""
    return (
        "/allow  /reject  /permissions  /pause  /resume  /status  "
        "/replay  /audit-export  /help  /exit"
    )


def interaction_activity(line: str) -> InteractionActivity:
    """Describe client-side waiting without inventing agent workflow steps."""
    directive = line.strip().split(maxsplit=1)[0] if line.strip() else ""
    if not directive.startswith("/"):
        return _TASK_ACTIVITY
    return _COMMAND_ACTIVITIES.get(directive, _DEFAULT_ACTIVITY)


def format_action_arguments(arguments: dict[str, JsonValue]) -> tuple[str, ...]:
    """Render exact action arguments consistently across terminal clients."""
    if not arguments:
        return ()
    return tuple(json.dumps(arguments, indent=2, sort_keys=True).splitlines())


def format_projection_lines(
    projection: SessionProjection,
    *,
    include_pending_review: bool = True,
    after_sequence: int | None = None,
) -> tuple[str, ...]:
    """Render the shared projection without reconstructing session state."""
    lines: list[str] = []
    for message in projection.conversation:
        if after_sequence is not None and message.sequence <= after_sequence:
            continue
        prefix = f"[{message.sequence:03d}]"
        lines.append(f"{prefix} {message.label}: {message.content}")
        if message.detail:
            lines.append(f"  {message.detail}")
        if message.technical_detail:
            lines.extend(f"    {line}" for line in message.technical_detail.splitlines())
    if projection.streaming_text:
        lines.append(f"[...] Agent: {projection.streaming_text}")
    if projection.task_plan:
        lines.append("Task plan:")
        lines.extend(
            f"  [{'x' if task.status == 'done' else '·'}] {task.title}"
            for task in projection.task_plan
        )
    if projection.usage is not None:
        usage = projection.usage
        total_tokens = usage.prompt_tokens + usage.completion_tokens
        lines.append(
            f"Model activity: {usage.call_count} calls · "
            f"{total_tokens:,} tokens · {usage.model_name}"
        )
        lines.extend(
            f"  {item.usage_id}: {item.call_count} calls · "
            f"{item.prompt_tokens + item.completion_tokens:,} tokens"
            for item in projection.usage_by_purpose
        )
    if projection.subagents:
        lines.append("Specialists:")
        lines.extend(
            f"  {item.agent_name}: {item.status} · invocation {item.invocation_id}"
            f"{f' · task {item.task_id}' if item.task_id is not None else ''}"
            for item in projection.subagents
        )
    approval = projection.pending_approval
    if approval is not None and include_pending_review:
        label = "action" if len(approval.actions) == 1 else "actions"
        lines.append(f"Review {len(approval.actions)} {label} as one OpenHands action set:")
        for index, action in enumerate(approval.actions, 1):
            tool_label = action_tool_label(action.tool_name)
            risk_label = action_risk_label(action.risk or "unknown")
            lines.append(f"  {index}. {action.summary or tool_label} [{tool_label} · {risk_label}]")
            if argument_lines := format_action_arguments(action.arguments):
                lines.append("     Arguments:")
                lines.extend(f"       {line}" for line in argument_lines)
        lines.extend(
            (
                "Allow the complete set once: /allow",
                "Reject the complete set: /reject",
            )
        )
    return tuple(lines)


def format_model_status(gateway: SessionGateway) -> str:
    """Format the active model route without exposing credentials."""
    validation = gateway.validate_model_profile()
    profile = validation["profile"]
    decision = validation["policy_decision"]
    return "\n".join(
        (
            f"Model: {profile['model']}",
            f"Credentials: {validation['credential_status']}",
            f"Action review: {action_mode_label(validation['action_confirmation_mode'])}",
            f"Policy: {decision['decision']} ({decision['reason']})",
        )
    )


def format_action_settings(settings: ActionSettingsResponse) -> str:
    """Format gateway-owned action-mode metadata for terminal interfaces."""
    lines = ["Action review", "", settings["scope_description"], ""]
    if not settings["change_allowed"] and settings["change_blocked_reason"]:
        lines.extend((settings["change_blocked_reason"], ""))
    selected = settings["confirmation_mode"]
    for item in settings["modes"]:
        marker = "*" if item["mode"] == selected else " "
        recommended = " (recommended)" if item["recommended"] else ""
        allowed = item["allowed"]
        availability = "" if allowed else " (unavailable)"
        lines.append(f"{marker} {item['label']}{recommended}{availability}")
        lines.append(f"  {item['description']}")
        if not allowed and (reason := item["unavailable_reason"]):
            lines.append(f"  {reason}")
        else:
            lines.append(f"  Select: /permissions {item['command_value']}")
        lines.append("")
    return "\n".join(lines).rstrip()
