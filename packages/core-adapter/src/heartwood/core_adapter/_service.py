# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session orchestration over policy, an agent backend, state, and audit."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from heartwood.adapters import DataSourceAdapter, PlatformAdapter
from heartwood.adapters.data import LocalFilesystemDataSourceAdapter
from heartwood.adapters.platform import GenericPlatformAdapter
from heartwood.audit import AuditLog
from heartwood.core_adapter._facade import (
    AgentBackend,
    BackendEvent,
    BackendEventKind,
    DeterministicAgentBackend,
    LocalWorkspaceAgentBackend,
    ProposedToolCall,
    ToolExecution,
)
from heartwood.core_adapter._state import FileSessionStore
from heartwood.model_policy import ModelPolicyEngine
from heartwood.schemas import ConfirmationRequest, JsonValue, PolicyProfile
from heartwood.session import CommandKind, EventKind, SessionCommand, SessionEvent


@dataclass(frozen=True, slots=True)
class SessionResult:
    """Events emitted while handling one command."""

    events: tuple[SessionEvent, ...]


class SessionService:
    """Core session service shared by every interaction surface."""

    def __init__(
        self,
        *,
        store: FileSessionStore,
        platform_adapter: PlatformAdapter,
        data_source_adapter: DataSourceAdapter,
        backend: AgentBackend,
        policy_profile: PolicyProfile | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.audit_log = AuditLog(store.audit_path)
        self.platform_adapter = platform_adapter
        self.data_source_adapter = data_source_adapter
        self.backend = backend
        self.policy_profile = policy_profile or platform_adapter.default_policy_profile()
        self.policy = ModelPolicyEngine(self.policy_profile)
        self.env = os.environ if env is None else env
        self.clock: Callable[[], str] = _utc_now if clock is None else clock
        self.backend.restore_pending(_pending_tool_calls(self.store.read_events()))

    @classmethod
    def synthetic_default(
        cls,
        workspace: Path,
        *,
        session_id: str = "session-synthetic-001",
        env: Mapping[str, str] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> SessionService:
        """Build the deterministic synthetic service used in tests and replay."""
        platform = GenericPlatformAdapter()
        return cls(
            store=FileSessionStore(workspace, session_id),
            platform_adapter=platform,
            data_source_adapter=LocalFilesystemDataSourceAdapter.synthetic_omop(),
            backend=DeterministicAgentBackend(),
            env={} if env is None else env,
            clock=(lambda: "2026-01-01T00:00:00Z") if clock is None else clock,
        )

    @classmethod
    def local_default(
        cls,
        workspace: Path,
        *,
        session_id: str = "session-local",
        backend: AgentBackend | None = None,
        policy_profile: PolicyProfile | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> SessionService:
        """Build a local service using the caller environment."""
        platform = GenericPlatformAdapter()
        active_env = os.environ if env is None else env
        store = FileSessionStore(workspace, session_id)
        return cls(
            store=store,
            platform_adapter=platform,
            data_source_adapter=LocalFilesystemDataSourceAdapter.synthetic_omop(),
            backend=_backend_from_env(store=store, env=active_env) if backend is None else backend,
            policy_profile=policy_profile,
            env=active_env,
            clock=clock,
        )

    def handle(self, command: SessionCommand) -> SessionResult:
        """Handle one command, persist events, and append audit records."""
        if command.session_id != self.store.session_id:
            msg = (
                f"command session {command.session_id} does not match "
                f"store session {self.store.session_id}"
            )
            raise ValueError(msg)
        self.store.append_command(command)
        command_kind = _kind_value(command.kind)
        events = [
            self._record_event(
                EventKind.COMMAND_RECEIVED,
                {
                    "actor_id": command.actor_id,
                    "command_id": command.command_id,
                    "command_kind": command_kind,
                },
            )
        ]
        if command_kind == CommandKind.DETECT.value:
            events.append(self._handle_detect())
        elif command_kind in {CommandKind.CHAT.value, CommandKind.RUN.value}:
            events.extend(self._handle_task(command))
        elif command_kind in {CommandKind.APPROVE.value, CommandKind.DENY.value}:
            events.extend(self._handle_action_decision(command))
        elif command_kind == CommandKind.PAUSE.value:
            self.backend.pause()
            events.append(
                self._record_event(EventKind.SESSION_PAUSED, {"command_id": command.command_id})
            )
        elif command_kind == CommandKind.RESUME.value:
            pending = _pending_tool_call(self.store.read_events())
            if pending is not None:
                events.append(
                    self._record_event(
                        EventKind.ERROR_RECORDED,
                        {
                            "command": command_kind,
                            "reason": "resolve the pending action before resuming",
                        },
                    )
                )
            else:
                authorized = True
                authorization_events: list[SessionEvent] = []
                if self.backend.continuation_requires_model_authorization:
                    authorized, authorization_events = self._authorize_backend(
                        command,
                        purpose=f"resumed agent turn through {self.backend.backend_id}",
                    )
                events.extend(authorization_events)
                if authorized:
                    events.append(
                        self._record_event(
                            EventKind.SESSION_RESUMED,
                            {"command_id": command.command_id},
                        )
                    )
                    events.extend(
                        self._translate_backend_events(
                            self.backend.resume(session_id=command.session_id)
                        )
                    )
        elif command_kind == CommandKind.AUDIT_EXPORT.value:
            events.append(self._handle_audit_export())
        else:
            events.append(
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {"command": command_kind, "reason": "command is not implemented"},
                )
            )
        return SessionResult(events=tuple(events))

    def replay_events(self) -> tuple[SessionEvent, ...]:
        """Return persisted session events after verifying the audit chain."""
        self.audit_log.verify()
        return self.store.read_events()

    def close(self) -> None:
        """Release backend resources."""
        self.backend.close()

    def _handle_detect(self) -> SessionEvent:
        platform = self.platform_adapter.detect(self.env)
        dataset = self.data_source_adapter.fingerprint()
        return self._record_event(
            EventKind.DETECTION_PROPOSED,
            {
                "platform": {
                    "adapter_id": platform.adapter_id,
                    "confidence": platform.confidence,
                    "evidence": list(platform.evidence),
                },
                "dataset": {
                    "source_id": self.data_source_adapter.source_id,
                    "dataset_type": dataset.dataset_type,
                    "confidence": dataset.confidence,
                    "evidence": list(dataset.evidence),
                },
            },
        )

    def _handle_task(self, command: SessionCommand) -> tuple[SessionEvent, ...]:
        prompt_value = command.payload.get("prompt")
        if not isinstance(prompt_value, str) or not (prompt := prompt_value.strip()):
            return (
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {"command": _kind_value(command.kind), "reason": "prompt is required"},
                ),
            )
        user_event = self._record_event(
            EventKind.USER_MESSAGE_RECORDED,
            {
                "actor_id": command.actor_id,
                "command_id": command.command_id,
                "content": prompt,
            },
        )
        if _pending_tool_call(self.store.read_events()) is not None:
            return (
                user_event,
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "command": _kind_value(command.kind),
                        "reason": "resolve the pending action before submitting another task",
                    },
                ),
            )
        authorized, authorization_events = self._authorize_backend(
            command,
            purpose=f"agent turn through {self.backend.backend_id}",
        )
        if not authorized:
            return (user_event, *authorization_events)
        stream = self.backend.submit_turn(session_id=command.session_id, prompt=prompt)
        return (user_event, *authorization_events, *self._translate_backend_events(stream))

    def _authorize_backend(
        self,
        command: SessionCommand,
        *,
        purpose: str,
    ) -> tuple[bool, list[SessionEvent]]:
        """Authorize one backend operation that may continue model execution."""
        configuration_error = self.backend.configuration_error
        if configuration_error is not None:
            return False, [
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "backend_id": self.backend.backend_id,
                        "reason": configuration_error,
                    },
                )
            ]
        decision = self.policy.evaluate(
            endpoint=self.backend.model_endpoint,
            capability_tier=self.backend.capability_tier,
            action_confirmation_mode=self.backend.action_confirmation_mode,
            credential_reference=self.backend.credential_reference,
            decision_id=f"{command.command_id}-model-route",
            purpose=purpose,
        )
        attestation = self.policy.attestation(
            decision=decision,
            record_id=f"{command.command_id}-attestation",
            session_id=command.session_id,
            occurred_at=self.clock(),
        )
        policy_event = self._record_event(
            EventKind.MODEL_CALL_DECISION_RECORDED,
            {
                "decision": decision.model_dump(mode="json"),
                "attestation": attestation.model_dump(mode="json"),
                "model_profile": {
                    "backend_id": self.backend.backend_id,
                    "profile_id": self.backend.model_profile_id,
                    "capability_tier": self.backend.capability_tier,
                    "action_confirmation_mode": self.backend.action_confirmation_mode,
                },
            },
        )
        if decision.decision != "allow":
            return False, [
                policy_event,
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "command": _kind_value(command.kind),
                        "reason": "active model profile is denied by platform policy",
                    },
                ),
            ]
        return True, [policy_event]

    def _handle_action_decision(self, command: SessionCommand) -> tuple[SessionEvent, ...]:
        target_type = str(command.payload.get("target_type", "tool-call"))
        if target_type != "tool-call":
            return (
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "command": _kind_value(command.kind),
                        "reason": "interactive approval is supported only for pending tool actions",
                    },
                ),
            )
        tool_call_id = str(command.payload.get("target_id", ""))
        if not tool_call_id:
            return (
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {"command": _kind_value(command.kind), "reason": "target_id is required"},
                ),
            )
        approved = _kind_value(command.kind) == CommandKind.APPROVE.value
        pending_by_id = {
            pending.tool_call_id: pending
            for pending in _pending_tool_calls(self.store.read_events())
        }
        pending = pending_by_id.get(tool_call_id)
        if pending is None:
            return (
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "command": _kind_value(command.kind),
                        "reason": f"no matching pending action: {tool_call_id}",
                    },
                ),
            )
        events: list[SessionEvent] = []
        if (
            approved
            and len(pending_by_id) == 1
            and self.backend.continuation_requires_model_authorization
        ):
            authorized, authorization_events = self._authorize_backend(
                command,
                purpose=f"approved action continuation through {self.backend.backend_id}",
            )
            events.extend(authorization_events)
            if not authorized:
                return tuple(events)
        events.extend(
            self._translate_backend_events(
                self.backend.resolve_confirmation(
                    session_id=command.session_id,
                    tool_call_id=tool_call_id,
                    approved=approved,
                )
            )
        )
        return tuple(events)

    def _translate_backend_events(self, stream: tuple[BackendEvent, ...]) -> list[SessionEvent]:
        translated: list[SessionEvent] = []
        for event in stream:
            if event.kind == BackendEventKind.AGENT_MESSAGE:
                translated.append(
                    self._record_event(EventKind.AGENT_MESSAGE_EMITTED, {"content": event.message})
                )
            elif event.kind == BackendEventKind.TOOL_CALL_PROPOSED:
                tool_call = _require_tool_call(event)
                translated.append(
                    self._record_event(
                        EventKind.TOOL_CALL_PROPOSED,
                        {
                            "tool_call_id": tool_call.tool_call_id,
                            "tool_name": tool_call.tool_name,
                            "risk": tool_call.risk,
                            "summary": tool_call.summary,
                        },
                    )
                )
            elif event.kind == BackendEventKind.CONFIRMATION_REQUESTED:
                translated.append(self._record_confirmation_request(event))
            elif event.kind == BackendEventKind.CONFIRMATION_RESOLVED:
                tool_call = _require_tool_call(event)
                translated.append(
                    self._record_event(
                        EventKind.CONFIRMATION_RESOLVED,
                        {
                            "tool_call_id": tool_call.tool_call_id,
                            "decision": "approved" if event.approved else "denied",
                        },
                    )
                )
            elif event.kind == BackendEventKind.TOOL_EXECUTION:
                execution = _require_tool_execution(event)
                translated.append(
                    self._record_event(
                        EventKind.TOOL_EXECUTION_RECORDED,
                        {
                            "backend_id": self.backend.backend_id,
                            "tool_name": execution.tool_name,
                            "exit_code": execution.exit_code,
                            "summary": execution.summary,
                        },
                    )
                )
            elif event.kind == BackendEventKind.ERROR:
                translated.append(
                    self._record_event(
                        EventKind.ERROR_RECORDED,
                        {"backend_id": self.backend.backend_id, "reason": event.message or "error"},
                    )
                )
        return translated

    def _record_confirmation_request(self, event: BackendEvent) -> SessionEvent:
        tool_call = _require_tool_call(event)
        request = ConfirmationRequest(
            request_id=f"{tool_call.tool_call_id}-confirm",
            session_id=self.store.session_id,
            tool_call_id=tool_call.tool_call_id,
            tool_name=tool_call.tool_name,
            risk=tool_call.risk,
            summary=tool_call.summary,
        )
        return self._record_event(
            EventKind.CONFIRMATION_REQUESTED,
            {"request": request.model_dump(mode="json")},
        )

    def _handle_audit_export(self) -> SessionEvent:
        event = self._record_event(
            EventKind.AUDIT_EXPORT_RECORDED,
            {
                "path": str(self.store.audit_export_path),
                "event_count": len(self.audit_log.read()) + 1,
                "scrubbed": True,
            },
        )
        self.store.write_audit_export(self.audit_log.export_jsonl())
        return event

    def _record_event(self, kind: EventKind, payload: dict[str, JsonValue]) -> SessionEvent:
        sequence = self.store.next_sequence()
        audit_event = self.audit_log.append(
            session_id=self.store.session_id,
            event_type=kind.value,
            occurred_at=self.clock(),
            payload=payload,
        )
        event = SessionEvent(
            event_id=f"{self.store.session_id}-event-{sequence:06d}",
            session_id=self.store.session_id,
            sequence=sequence,
            kind=kind,
            occurred_at=audit_event.occurred_at,
            payload=payload,
            previous_event_hash=audit_event.previous_event_hash,
        )
        self.store.append_event(event)
        return event


def _require_tool_call(event: BackendEvent) -> ProposedToolCall:
    if event.tool_call is None:  # pragma: no cover - backend contract guarantees presence
        msg = f"{event.kind} event is missing a tool call"
        raise TypeError(msg)
    return event.tool_call


def _require_tool_execution(event: BackendEvent) -> ToolExecution:
    if event.tool_execution is None:  # pragma: no cover - backend contract guarantees presence
        msg = f"{event.kind} event is missing a tool execution"
        raise TypeError(msg)
    return event.tool_execution


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _kind_value(kind: CommandKind | str) -> str:
    return kind.value if isinstance(kind, CommandKind) else kind


def _backend_from_env(*, store: FileSessionStore, env: Mapping[str, str]) -> AgentBackend:
    backend_id = env.get("HEARTWOOD_AGENT_BACKEND", "deterministic-local")
    if backend_id in {"deterministic-local", "deterministic"}:
        return DeterministicAgentBackend()
    if backend_id in {"local-workspace", "workspace"}:
        return LocalWorkspaceAgentBackend(store.session_dir / "agent-artifacts")
    msg = f"unsupported HEARTWOOD_AGENT_BACKEND: {backend_id}"
    raise ValueError(msg)


def _pending_tool_calls(events: tuple[SessionEvent, ...]) -> tuple[ProposedToolCall, ...]:
    pending: dict[str, ProposedToolCall] = {}
    for event in events:
        kind = str(event.kind)
        if kind == EventKind.CONFIRMATION_REQUESTED.value:
            request = event.payload.get("request")
            if isinstance(request, dict):
                risk_value = str(request.get("risk", "unknown"))
                risk = risk_value if risk_value in {"low", "medium", "high"} else "unknown"
                tool_call = ProposedToolCall(
                    tool_call_id=str(request.get("tool_call_id", "")),
                    tool_name=str(request.get("tool_name", "unknown-tool")),
                    risk=cast(Literal["low", "medium", "high", "unknown"], risk),
                    summary=str(request.get("summary", "pending action")),
                )
                pending[tool_call.tool_call_id] = tool_call
        elif kind == EventKind.CONFIRMATION_RESOLVED.value:
            tool_call_id = str(event.payload.get("tool_call_id", ""))
            pending.pop(tool_call_id, None)
    return tuple(pending.values())


def _pending_tool_call(events: tuple[SessionEvent, ...]) -> ProposedToolCall | None:
    pending = _pending_tool_calls(events)
    return pending[-1] if pending else None
