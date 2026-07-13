# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Framework-neutral interaction controller for terminal clients."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from heartwood.gateway import ModelSettingsError, SessionGateway
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand, SessionEvent


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
class PendingAction:
    """One member of the current OpenHands confirmation batch."""

    request_id: str
    tool_call_id: str
    tool_name: str
    risk: str
    summary: str


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
        sequence = len(self.gateway.replay_events(session_id=self.session_id))
        command = SessionCommand(
            command_id=f"{self.session_id}-{kind.value}-{sequence:06d}",
            session_id=self.session_id,
            kind=kind,
            actor_id="human",
            created_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            payload={} if payload is None else payload,
        )
        return self.gateway.handle(command).events


def command_help() -> str:
    """Return the commands common to terminal clients."""
    return "/allow  /reject  /pause  /resume  /status  /replay  /audit-export  /help  /exit"


def pending_actions(events: Sequence[SessionEvent]) -> tuple[PendingAction, ...]:
    """Project unresolved confirmation requests from a persisted event stream."""
    pending: dict[str, PendingAction] = {}
    for event in events:
        kind = str(event.kind)
        if kind == EventKind.CONFIRMATION_REQUESTED.value:
            request = event.payload.get("request")
            if not isinstance(request, dict):
                continue
            tool_call_id = request.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                continue
            pending[tool_call_id] = PendingAction(
                request_id=str(request.get("request_id", "")),
                tool_call_id=tool_call_id,
                tool_name=str(request.get("tool_name", "unknown-tool")),
                risk=str(request.get("risk", "unknown")),
                summary=str(request.get("summary", request.get("tool_name", "action"))),
            )
        elif kind == EventKind.CONFIRMATION_RESOLVED.value:
            tool_call_id = event.payload.get("tool_call_id")
            if isinstance(tool_call_id, str):
                pending.pop(tool_call_id, None)
    return tuple(pending.values())


def format_model_status(gateway: SessionGateway) -> str:
    """Format the active model route without exposing credentials."""
    validation = gateway.validate_model_profile()
    profile = validation.get("profile", {})
    decision = validation.get("policy_decision", {})
    if not isinstance(profile, dict) or not isinstance(decision, dict):
        return "Model profile validation returned malformed data."
    return "\n".join(
        (
            f"Model: {profile.get('model')}",
            f"Credentials: {validation.get('credential_status')}",
            f"Action confirmation: {validation.get('action_confirmation_mode')}",
            f"Policy: {decision.get('decision')} ({decision.get('reason')})",
        )
    )
