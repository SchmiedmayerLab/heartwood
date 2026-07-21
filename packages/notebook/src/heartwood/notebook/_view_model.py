# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Notebook view models derived from the shared session event stream."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from heartwood.gateway import DEFAULT_SESSION_ID, ModelProfile, ProjectContext, SessionGateway
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

    role: Literal["assistant", "system", "user"]
    content: str


@dataclass(frozen=True, slots=True)
class SkillProposal:
    """Skill or tool proposal visible to notebook users."""

    target_id: str
    status: Literal["proposed", "approved", "denied"]
    detail: str


@dataclass(frozen=True, slots=True)
class ApprovalAction:
    """One visible member of a pending OpenHands action set."""

    target_id: str
    tool_name: str
    risk: str
    summary: str
    arguments: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApprovalControl:
    """One actionable whole-set decision rendered from session events."""

    target_type: str
    target_id: str
    label: str
    decision: str | None = None
    actions: tuple[ApprovalAction, ...] = ()


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
        project: ProjectContext | None = None,
        session_id: str = DEFAULT_SESSION_ID,
        gateway: SessionGateway | None = None,
    ) -> None:
        gateway_project = getattr(gateway, "project", None)
        if project is None and isinstance(gateway_project, ProjectContext):
            self.project = gateway_project
        else:
            self.project = ProjectContext.current() if project is None else project
        if (
            isinstance(gateway_project, ProjectContext)
            and gateway_project.root != self.project.root
        ):
            raise ValueError("notebook project must match the injected gateway project")
        self.session_id = session_id
        self.gateway = SessionGateway(project=self.project) if gateway is None else gateway
        self._next_command_sequence = len(self.gateway.replay_events(session_id=session_id))

    def chat(self, prompt: str) -> NotebookViewModel:
        """Run one chat turn and return the notebook view model."""
        return self._handle(CommandKind.CHAT, {"prompt": prompt})

    def model_settings(self) -> dict[str, object]:
        """Return non-secret model profiles and presets."""
        return self.gateway.model_settings()

    def initialize_project(self) -> dict[str, object]:
        """Confirm the current directory and create private project state."""
        return self.gateway.initialize_project(interface="notebook")

    def project_readiness(self) -> dict[str, object]:
        """Return the shared project setup and compute readiness report."""
        return self.gateway.project_readiness()

    def startup_plan(self) -> dict[str, object]:
        """Return the shared notebook startup and recovery projection."""
        return self.gateway.startup_plan(interface="notebook")

    def platform_capabilities(self) -> dict[str, object]:
        """Return capabilities for the detected execution environment."""
        return self.gateway.platform_capabilities()

    def configure_model_source(self, source_id: str) -> dict[str, object]:
        """Prepare the same project model source used by terminal and browser clients."""
        return self.gateway.configure_model_source(source_id)

    def discover_models(
        self,
        connection_id: str,
        *,
        token: str | None = None,
        base_url: str | None = None,
        refresh: bool = False,
        remember: bool = False,
    ) -> dict[str, object]:
        """Discover models through the shared authorized connection catalog."""
        return self.gateway.discover_models(
            connection_id,
            token=token,
            base_url=base_url,
            refresh=refresh,
            remember=remember,
        )

    def connect_model(
        self,
        connection_id: str,
        model_id: str,
        *,
        token: str | None = None,
        base_url: str | None = None,
        manual: bool = False,
        remember: bool = False,
    ) -> dict[str, object]:
        """Select a discovered model through the shared connection workflow."""
        return self.gateway.connect_model(
            connection_id,
            model_id,
            token=token,
            base_url=base_url,
            manual=manual,
            remember=remember,
        )

    def credential_settings(self) -> dict[str, object]:
        """Return non-secret credential-store and binding status."""
        return self.gateway.credential_settings()

    def forget_credential(self, connection_id: str) -> dict[str, object]:
        """Forget a process or saved credential for one connection."""
        return self.gateway.forget_credential(connection_id)

    def save_model_profile(self, profile: ModelProfile) -> dict[str, object]:
        """Add or update one non-secret model profile."""
        return self.gateway.save_model_profile(profile)

    def select_model_profile(self, profile_id: str) -> dict[str, object]:
        """Select the model profile used by subsequent turns."""
        return self.gateway.select_model_profile(profile_id)

    def validate_model_profile(self, profile_id: str | None = None) -> dict[str, object]:
        """Validate credential availability and route authorization."""
        return self.gateway.validate_model_profile(profile_id)

    def model_artifacts(self) -> dict[str, object]:
        """Return default and user-selected Heartwood-managed model choices."""
        return self.gateway.model_artifacts()

    def inspect_model_repository(
        self,
        repository: str,
        *,
        revision: str | None = None,
    ) -> dict[str, object]:
        """Inspect supported candidates from one Hugging Face model repository."""
        return self.gateway.inspect_model_repository(repository, revision=revision)

    def download_local_model(self, model_id: str) -> dict[str, object]:
        """Start a recommended Heartwood-managed model download."""
        return self.gateway.download_local_model(model_id)

    def download_custom_local_model(
        self,
        repository: str,
        *,
        revision: str | None = None,
    ) -> dict[str, object]:
        """Start one inspected user-selected Heartwood-managed model download."""
        return self.gateway.download_custom_local_model(
            repository,
            revision=revision,
        )

    def import_local_model(
        self,
        source: Path,
        *,
        source_repository: str,
        source_revision: str,
        license_posture: str,
        context_window: int = 32_768,
    ) -> dict[str, object]:
        """Import and select a reviewed Heartwood-managed model through the shared gateway."""
        return self.gateway.import_local_model(
            source,
            source_repository=source_repository,
            source_revision=source_revision,
            license_posture=license_posture,
            context_window=context_window,
        )

    def action_settings(self) -> dict[str, object]:
        """Return the shared action-confirmation settings."""
        return self.gateway.action_settings()

    def select_action_confirmation_mode(self, mode: str) -> dict[str, object]:
        """Select a deployment-allowed action-confirmation mode."""
        return self.gateway.select_action_confirmation_mode(mode)

    def browser_url(self, *, port: int = 8767) -> str | None:
        """Return the supported browser URL, or ``None`` on terminal-only platforms."""
        access_url = self.gateway.startup_plan(interface="web", port=port).get("access_url")
        return access_url if isinstance(access_url, str) else None

    def close(self) -> None:
        """Release active conversations and process-scoped credentials."""
        self.gateway.stop()

    def __enter__(self) -> NotebookSession:
        """Return this session for a managed notebook context."""
        return self

    def __exit__(
        self,
        _exception_type: object,
        _exception: object,
        _traceback: object,
    ) -> None:
        """Release resources when leaving a managed notebook context."""
        self.close()

    def approve(
        self,
        *,
        tool_call_id: str,
    ) -> NotebookViewModel:
        """Allow the complete pending OpenHands action set once."""
        return self._handle(
            CommandKind.APPROVE,
            {"target_type": "tool-call", "target_id": tool_call_id},
        )

    def deny(
        self,
        *,
        tool_call_id: str,
    ) -> NotebookViewModel:
        """Reject the complete pending OpenHands action set."""
        return self._handle(
            CommandKind.DENY,
            {"target_type": "tool-call", "target_id": tool_call_id},
        )

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
    skills: list[SkillProposal] = []
    approvals: list[ApprovalControl] = []
    pending_actions: dict[str, ApprovalAction] = {}
    policies: list[PolicyStatus] = []
    exports: list[ExportAction] = []
    paused = False
    session_id = events[-1].session_id if events else ""
    for event in events:
        kind = _event_kind(event)
        activity.append(_activity_item(event))
        if kind == EventKind.USER_MESSAGE_RECORDED.value:
            chat.append(ChatMessage(role="user", content=str(event.payload.get("content", ""))))
        elif kind == EventKind.AGENT_MESSAGE_EMITTED.value:
            chat.append(
                ChatMessage(role="assistant", content=str(event.payload.get("content", "")))
            )
        elif kind == EventKind.CONFIRMATION_REQUESTED.value:
            request = _mapping_payload(event.payload["request"], "request")
            target_id = str(request["tool_call_id"])
            pending_actions[target_id] = ApprovalAction(
                target_id=target_id,
                tool_name=str(request.get("tool_name", "unknown-tool")),
                risk=str(request.get("risk", "unknown")),
                summary=str(request.get("summary", request.get("tool_name", "action"))),
                arguments=(
                    cast(dict[str, JsonValue], request["arguments"])
                    if isinstance(request.get("arguments"), dict)
                    else {}
                ),
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
            _upsert_approval(
                approvals,
                ApprovalControl(
                    target_type=str(approval["target_type"]),
                    target_id=str(approval["target_id"]),
                    label=f"{approval['decision']} {approval['target_type']}",
                    decision=str(approval["decision"]),
                ),
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
        elif kind == EventKind.CONFIRMATION_RESOLVED.value:
            target_id = str(event.payload["tool_call_id"])
            pending_actions.pop(target_id, None)
            _upsert_approval(
                approvals,
                ApprovalControl(
                    target_type="tool-call",
                    target_id=target_id,
                    label=f"Action {event.payload['decision']}",
                    decision=str(event.payload["decision"]),
                ),
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
    if pending_actions:
        actions = tuple(pending_actions.values())
        label = "action" if len(actions) == 1 else "actions"
        approvals.append(
            ApprovalControl(
                target_type="tool-call",
                target_id=actions[0].target_id,
                label=f"Review complete action set ({len(actions)} {label})",
                actions=actions,
            )
        )
    return NotebookViewModel(
        session_id=session_id,
        event_count=len(events),
        activity=tuple(activity),
        chat=tuple(chat),
        skill_proposals=tuple(skills),
        approval_controls=tuple(approvals),
        policy_status=tuple(policies),
        export_actions=tuple(exports),
        paused=paused,
    )


def _upsert_approval(approvals: list[ApprovalControl], control: ApprovalControl) -> None:
    for index, existing in enumerate(approvals):
        if existing.target_type == control.target_type and existing.target_id == control.target_id:
            approvals[index] = control
            return
    approvals.append(control)


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
        EventKind.APPROVAL_RECORDED.value: "Approval recorded",
        EventKind.MODEL_CALL_DECISION_RECORDED.value: "Model-call decision",
        EventKind.USER_MESSAGE_RECORDED.value: "Researcher message",
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
