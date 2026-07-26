# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Framework-neutral interaction controller for terminal clients."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime

from heartwood.gateway import (
    ACTION_MODE_OPTIONS,
    ActionSettingsError,
    ModelSettingsError,
    SessionGateway,
    action_mode_label,
)
from heartwood.schemas import ActionSettingsResponse
from heartwood.session import (
    CommandKind,
    EventKind,
    JsonValue,
    PendingToolAction,
    SessionCommand,
    SessionEvent,
    new_command_id,
    pending_tool_actions,
)

PendingAction = PendingToolAction
pending_actions = pending_tool_actions


@dataclass(frozen=True, slots=True)
class InteractionResult:
    """One user interaction projected for a terminal client."""

    events: tuple[SessionEvent, ...] = ()
    message: str | None = None
    exit_requested: bool = False
    error: bool = False
    replace_transcript: bool = False

    @property
    def failed(self) -> bool:
        """Return whether this interaction recorded an error."""
        return self.error or any(
            str(event.kind) == EventKind.ERROR_RECORDED.value for event in self.events
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

    def replay(self) -> tuple[SessionEvent, ...]:
        """Return the persisted conversation."""
        return self.gateway.replay_events(session_id=self.session_id)

    def pending_actions(self) -> tuple[PendingAction, ...]:
        """Return the unresolved members of the current OpenHands action batch."""
        return pending_actions(self.replay())

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
        if self.pending_actions():
            raise ActionSettingsError(
                "resolve the pending action set before changing the action-review mode"
            )
        return self.gateway.select_action_confirmation_mode(mode)

    def submit(self, line: str) -> InteractionResult:
        """Submit a prompt or slash command."""
        text = line.strip()
        if not text:
            return InteractionResult()
        if not text.startswith("/"):
            return InteractionResult(events=self._handle(CommandKind.CHAT, {"prompt": text}))
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
            return InteractionResult(
                events=self._handle(
                    kind,
                    {"target_type": "tool-call", "target_id": target},
                )
            )
        if directive == "/pause" and len(parts) == 1:
            return InteractionResult(events=self._handle(CommandKind.PAUSE))
        if directive == "/resume" and len(parts) == 1:
            return InteractionResult(events=self._handle(CommandKind.RESUME))
        if directive == "/replay" and len(parts) == 1:
            return InteractionResult(events=self.replay(), replace_transcript=True)
        if directive == "/audit-export" and len(parts) == 1:
            return InteractionResult(events=self._handle(CommandKind.AUDIT_EXPORT))
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
        actions = self.pending_actions()
        if not actions:
            return None
        if requested_id is None:
            return actions[0].tool_call_id
        for action in actions:
            if requested_id in {action.tool_call_id, action.request_id}:
                return action.tool_call_id
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
