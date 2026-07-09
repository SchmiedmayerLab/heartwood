# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Notebook view models derived from the shared session event stream."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from heartwood.gateway import SessionGateway
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand, SessionEvent


@dataclass(frozen=True, slots=True)
class ActivityItem:
    """One visible session activity entry."""

    sequence: int
    kind: str
    label: str
    detail: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One notebook chat message."""

    role: Literal["assistant", "system"]
    content: str


@dataclass(frozen=True, slots=True)
class DatasetProposal:
    """Dataset proposal rendered from a detection event."""

    source_id: str
    dataset_type: str
    confidence: float
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillProposal:
    """Skill or tool proposal visible to notebook users."""

    target_id: str
    status: Literal["proposed", "approved", "denied"]
    detail: str


@dataclass(frozen=True, slots=True)
class ApprovalControl:
    """Actionable approval control rendered from session events."""

    target_type: str
    target_id: str
    label: str
    decision: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyStatus:
    """Policy decision status for one proposed model call."""

    decision: str
    endpoint: str
    reason: str


@dataclass(frozen=True, slots=True)
class ExportAction:
    """Available export action or completed export artifact."""

    label: str
    path: str


@dataclass(frozen=True, slots=True)
class NotebookViewModel:
    """Notebook-ready projection of session state."""

    session_id: str
    event_count: int
    activity: tuple[ActivityItem, ...]
    chat: tuple[ChatMessage, ...]
    dataset_proposals: tuple[DatasetProposal, ...]
    skill_proposals: tuple[SkillProposal, ...]
    approval_controls: tuple[ApprovalControl, ...]
    policy_status: tuple[PolicyStatus, ...]
    export_actions: tuple[ExportAction, ...]
    paused: bool


class NotebookSession:
    """Notebook API over the same gateway command and event stream as the CLI."""

    def __init__(
        self,
        *,
        workspace: Path = Path(".heartwood") / "sessions",
        session_id: str = "session-local",
        gateway: SessionGateway | None = None,
    ) -> None:
        self.workspace = workspace
        self.session_id = session_id
        self.gateway = SessionGateway(workspace=workspace) if gateway is None else gateway
        self._next_command_sequence = len(self.gateway.replay_events(session_id=session_id))

    def detect(self) -> NotebookViewModel:
        """Run platform and dataset detection and return the notebook view model."""
        return self._handle(CommandKind.DETECT)

    def chat(self, prompt: str) -> NotebookViewModel:
        """Run one chat turn and return the notebook view model."""
        return self._handle(CommandKind.CHAT, {"prompt": prompt})

    def run(
        self,
        prompt: str = "run the synthetic workflow",
        *,
        endpoint: str = "https://model.local.invalid/v1/chat",
        invoke_model: bool = False,
    ) -> NotebookViewModel:
        """Run the synthetic workflow through the policy-gated session path."""
        return self._handle(
            CommandKind.RUN,
            {
                "prompt": prompt,
                "endpoint": endpoint,
                "invoke_model": invoke_model,
            },
        )

    def approve(
        self,
        *,
        target_type: str,
        target_id: str,
        reason: str | None = None,
    ) -> NotebookViewModel:
        """Approve a pending session action."""
        payload: dict[str, JsonValue] = {"target_type": target_type, "target_id": target_id}
        if reason is not None:
            payload["reason"] = reason
        return self._handle(CommandKind.APPROVE, payload)

    def deny(
        self,
        *,
        target_type: str,
        target_id: str,
        reason: str | None = None,
    ) -> NotebookViewModel:
        """Deny a pending session action."""
        payload: dict[str, JsonValue] = {"target_type": target_type, "target_id": target_id}
        if reason is not None:
            payload["reason"] = reason
        return self._handle(CommandKind.DENY, payload)

    def pause(self) -> NotebookViewModel:
        """Pause the session."""
        return self._handle(CommandKind.PAUSE)

    def resume(self) -> NotebookViewModel:
        """Resume the session."""
        return self._handle(CommandKind.RESUME)

    def audit_export(self) -> NotebookViewModel:
        """Export the scrubbed audit log."""
        return self._handle(CommandKind.AUDIT_EXPORT)

    def replay(self) -> NotebookViewModel:
        """Replay persisted events as a notebook view model."""
        events = self.gateway.replay_events(session_id=self.session_id)
        self._next_command_sequence = max(self._next_command_sequence, len(events))
        return build_view_model(events)

    def _handle(
        self,
        kind: CommandKind,
        payload: dict[str, JsonValue] | None = None,
    ) -> NotebookViewModel:
        command = self._command(kind, payload)
        self.gateway.handle(command)
        return self.replay()

    def _command(
        self,
        kind: CommandKind,
        payload: dict[str, JsonValue] | None,
    ) -> SessionCommand:
        sequence = self._next_command_sequence
        self._next_command_sequence += 1
        return SessionCommand(
            command_id=f"{self.session_id}-{kind.value}-{sequence:06d}",
            session_id=self.session_id,
            kind=kind,
            actor_id="human",
            created_at=_utc_now(),
            payload={} if payload is None else payload,
        )


def build_view_model(events: tuple[SessionEvent, ...]) -> NotebookViewModel:
    """Build a notebook view model from shared session events."""
    activity: list[ActivityItem] = []
    chat: list[ChatMessage] = []
    datasets: list[DatasetProposal] = []
    skills: list[SkillProposal] = []
    approvals: list[ApprovalControl] = []
    policies: list[PolicyStatus] = []
    exports: list[ExportAction] = []
    paused = False
    session_id = events[-1].session_id if events else ""
    for event in events:
        kind = _event_kind(event)
        activity.append(_activity_item(event))
        if kind == EventKind.AGENT_MESSAGE_EMITTED.value:
            chat.append(
                ChatMessage(role="assistant", content=str(event.payload.get("content", "")))
            )
        elif kind == EventKind.DETECTION_PROPOSED.value:
            dataset = _mapping_payload(event.payload["dataset"], "dataset")
            datasets.append(
                DatasetProposal(
                    source_id=str(dataset["source_id"]),
                    dataset_type=str(dataset["dataset_type"]),
                    confidence=_float_payload(dataset["confidence"], "dataset.confidence"),
                    evidence=_string_list_payload(dataset["evidence"], "dataset.evidence"),
                )
            )
        elif kind == EventKind.TOOL_CALL_PROPOSED.value:
            target_id = str(event.payload["tool_call_id"])
            skills.append(
                SkillProposal(
                    target_id=target_id,
                    status="proposed",
                    detail=str(event.payload.get("summary", "")),
                )
            )
            approvals.append(
                ApprovalControl(
                    target_type="tool-call",
                    target_id=target_id,
                    label=f"Approve {event.payload.get('tool_name', 'tool call')}",
                )
            )
        elif kind == EventKind.CONFIRMATION_REQUESTED.value:
            request = _mapping_payload(event.payload["request"], "request")
            approvals.append(
                ApprovalControl(
                    target_type="tool-call",
                    target_id=str(request["tool_call_id"]),
                    label=f"Review {request['tool_name']}",
                )
            )
        elif kind == EventKind.APPROVAL_RECORDED.value:
            approval = _mapping_payload(event.payload["approval"], "approval")
            if approval["target_type"] == "skill":
                skills.append(
                    SkillProposal(
                        target_id=str(approval["target_id"]),
                        status=_skill_status(str(approval["decision"])),
                        detail=str(approval.get("reason", "")),
                    )
                )
            approvals.append(
                ApprovalControl(
                    target_type=str(approval["target_type"]),
                    target_id=str(approval["target_id"]),
                    label=f"{approval['decision']} {approval['target_type']}",
                    decision=str(approval["decision"]),
                )
            )
        elif kind == EventKind.MODEL_CALL_DECISION_RECORDED.value:
            decision = _mapping_payload(event.payload["decision"], "decision")
            policies.append(
                PolicyStatus(
                    decision=str(decision["decision"]),
                    endpoint=str(decision["endpoint"]),
                    reason=str(decision["reason"]),
                )
            )
            approvals.append(
                ApprovalControl(
                    target_type="model-call",
                    target_id=str(decision["decision_id"]),
                    label=f"Review model call to {decision['endpoint']}",
                )
            )
        elif kind == EventKind.AUDIT_EXPORT_RECORDED.value:
            exports.append(
                ExportAction(
                    label="Scrubbed audit JSONL",
                    path=str(event.payload["path"]),
                )
            )
        elif kind == EventKind.SESSION_PAUSED.value:
            paused = True
        elif kind == EventKind.SESSION_RESUMED.value:
            paused = False
    return NotebookViewModel(
        session_id=session_id,
        event_count=len(events),
        activity=tuple(activity),
        chat=tuple(chat),
        dataset_proposals=tuple(datasets),
        skill_proposals=tuple(skills),
        approval_controls=tuple(approvals),
        policy_status=tuple(policies),
        export_actions=tuple(exports),
        paused=paused,
    )


def _activity_item(event: SessionEvent) -> ActivityItem:
    return ActivityItem(
        sequence=event.sequence,
        kind=_event_kind(event),
        label=_activity_label(_event_kind(event)),
        detail=_activity_detail(event),
    )


def _activity_label(kind: str) -> str:
    labels = {
        EventKind.COMMAND_RECEIVED.value: "Command received",
        EventKind.DETECTION_PROPOSED.value: "Detection proposed",
        EventKind.APPROVAL_RECORDED.value: "Approval recorded",
        EventKind.MODEL_CALL_DECISION_RECORDED.value: "Model-call decision",
        EventKind.AGENT_MESSAGE_EMITTED.value: "Agent message",
        EventKind.TOOL_CALL_PROPOSED.value: "Tool proposed",
        EventKind.CONFIRMATION_REQUESTED.value: "Confirmation requested",
        EventKind.CONFIRMATION_RESOLVED.value: "Confirmation resolved",
        EventKind.TOOL_EXECUTION_RECORDED.value: "Tool execution",
        EventKind.SESSION_PAUSED.value: "Session paused",
        EventKind.SESSION_RESUMED.value: "Session resumed",
        EventKind.AUDIT_EXPORT_RECORDED.value: "Audit export",
        EventKind.ERROR_RECORDED.value: "Error",
    }
    return labels.get(kind, kind)


def _activity_detail(event: SessionEvent) -> str:
    kind = _event_kind(event)
    if kind == EventKind.MODEL_CALL_DECISION_RECORDED.value:
        decision = _mapping_payload(event.payload["decision"], "decision")
        return f"{decision['decision']} {decision['endpoint']}"
    if kind == EventKind.TOOL_CALL_PROPOSED.value:
        return str(event.payload.get("tool_name", ""))
    if kind == EventKind.TOOL_EXECUTION_RECORDED.value:
        return f"exit={event.payload.get('exit_code', '')}"
    if kind == EventKind.AUDIT_EXPORT_RECORDED.value:
        return str(event.payload.get("path", ""))
    return str(event.payload.get("command_id", ""))


def _mapping_payload(value: JsonValue, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        msg = f"expected {name} payload to be an object"
        raise TypeError(msg)
    return value


def _float_payload(value: JsonValue, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"expected {name} payload to be numeric"
        raise TypeError(msg)
    return float(value)


def _string_list_payload(value: JsonValue, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        msg = f"expected {name} payload to be a string list"
        raise TypeError(msg)
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            msg = f"expected {name} payload to be a string list"
            raise TypeError(msg)
        items.append(item)
    return tuple(items)


def _skill_status(value: str) -> Literal["proposed", "approved", "denied"]:
    if value == "approved":
        return "approved"
    if value == "denied":
        return "denied"
    return "proposed"


def _event_kind(event: SessionEvent) -> str:
    return str(event.kind)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
