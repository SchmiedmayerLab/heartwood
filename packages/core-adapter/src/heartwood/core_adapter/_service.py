# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session service that accepts commands and emits structured events."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeAlias, cast
from urllib.parse import urlsplit

from heartwood.adapters import (
    DataSourceAdapter,
    ModelCallRequest,
    ModelProviderAdapter,
    PlatformAdapter,
)
from heartwood.adapters.data import LocalFilesystemDataSourceAdapter
from heartwood.adapters.model import FakeLocalModelProviderAdapter
from heartwood.adapters.platform import GenericPlatformAdapter
from heartwood.audit import AuditLog
from heartwood.core_adapter._facade import (
    AgentBackend,
    BackendEvent,
    BackendEventKind,
    DeterministicAgentBackend,
    ProposedToolCall,
    ToolExecution,
)
from heartwood.core_adapter._state import FileSessionStore
from heartwood.model_policy import ModelPolicyEngine
from heartwood.schemas import ApprovalRecord, ConfirmationRequest, JsonValue
from heartwood.session import CommandKind, EventKind, SessionCommand, SessionEvent

ApprovalDecision: TypeAlias = Literal["approved", "denied"]
ApprovalTargetType: TypeAlias = Literal["skill", "egress", "model-call", "tool-call"]

_APPROVAL_TARGET_TYPES = {"skill", "egress", "model-call", "tool-call"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _req: urllib.request.Request,
        _fp: object,
        _code: int,
        _msg: str,
        _headers: object,
        _newurl: str,
    ) -> None:
        return None


class LocalModelInvocationError(RuntimeError):
    """Raised when the local model demo endpoint cannot be invoked safely."""


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


@dataclass(frozen=True, slots=True)
class SessionResult:
    """Events emitted while handling one command."""

    events: tuple[SessionEvent, ...]


class SessionService:
    """Core session orchestration over adapters, policy, state, and audit logging."""

    def __init__(
        self,
        *,
        store: FileSessionStore,
        platform_adapter: PlatformAdapter,
        data_source_adapter: DataSourceAdapter,
        model_provider_adapter: ModelProviderAdapter,
        backend: AgentBackend,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.audit_log = AuditLog(store.audit_path)
        self.platform_adapter = platform_adapter
        self.data_source_adapter = data_source_adapter
        self.model_provider_adapter = model_provider_adapter
        self.backend = backend
        self.env = os.environ if env is None else env
        self.clock: Callable[[], str] = _utc_now if clock is None else clock

    @classmethod
    def synthetic_default(
        cls,
        workspace: Path,
        *,
        session_id: str = "session-synthetic-001",
        env: Mapping[str, str] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> SessionService:
        """Build the default synthetic local session service."""
        platform = GenericPlatformAdapter()
        policy = platform.default_policy_profile()
        return cls(
            store=FileSessionStore(workspace, session_id),
            platform_adapter=platform,
            data_source_adapter=LocalFilesystemDataSourceAdapter.synthetic_omop(),
            model_provider_adapter=FakeLocalModelProviderAdapter(policy),
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
        env: Mapping[str, str] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> SessionService:
        """Build the default local session service using the caller environment."""
        platform = GenericPlatformAdapter()
        policy = platform.default_policy_profile()
        return cls(
            store=FileSessionStore(workspace, session_id),
            platform_adapter=platform,
            data_source_adapter=LocalFilesystemDataSourceAdapter.synthetic_omop(),
            model_provider_adapter=FakeLocalModelProviderAdapter(policy),
            backend=DeterministicAgentBackend(),
            env=os.environ if env is None else env,
            clock=clock,
        )

    def handle(self, command: SessionCommand) -> SessionResult:
        """Handle one command, persist emitted events, and append audit records."""
        if command.session_id != self.store.session_id:
            msg = (
                f"command session {command.session_id} does not match "
                f"store session {self.store.session_id}"
            )
            raise ValueError(msg)
        self.store.append_command(command)
        events = [
            self._record_event(EventKind.COMMAND_RECEIVED, {"command_id": command.command_id})
        ]
        command_kind = _kind_value(command.kind)
        if command_kind == CommandKind.DETECT.value:
            events.append(self._handle_detect())
        elif command_kind in {CommandKind.APPROVE.value, CommandKind.DENY.value}:
            events.append(self._handle_approval(command))
        elif command_kind == CommandKind.CHAT.value:
            events.extend(self._handle_chat(command))
        elif command_kind == CommandKind.RUN.value:
            events.extend(self._handle_run(command))
        elif command_kind == CommandKind.PAUSE.value:
            events.append(
                self._record_event(EventKind.SESSION_PAUSED, {"command_id": command.command_id})
            )
        elif command_kind == CommandKind.RESUME.value:
            events.append(
                self._record_event(EventKind.SESSION_RESUMED, {"command_id": command.command_id})
            )
        elif command_kind == CommandKind.AUDIT_EXPORT.value:
            events.append(self._handle_audit_export())
        else:
            events.append(
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "command": command_kind,
                        "reason": "command is not implemented in the core harness yet",
                    },
                )
            )
        return SessionResult(events=tuple(events))

    def replay_events(self) -> tuple[SessionEvent, ...]:
        """Return persisted session events for deterministic replay tests."""
        self.audit_log.verify()
        return self.store.read_events()

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

    def _handle_approval(self, command: SessionCommand) -> SessionEvent:
        decision: ApprovalDecision = (
            "approved" if _kind_value(command.kind) == CommandKind.APPROVE.value else "denied"
        )
        approval = ApprovalRecord(
            approval_id=str(command.payload.get("approval_id", f"{command.command_id}-approval")),
            session_id=command.session_id,
            target_type=_approval_target_type(command.payload.get("target_type", "skill")),
            target_id=str(command.payload.get("target_id", "heartwood.synthetic.omop-summary")),
            decision=decision,
            actor_id=command.actor_id,
            occurred_at=self.clock(),
            reason=_optional_string(command.payload.get("reason")),
        )
        return self._record_event(
            EventKind.APPROVAL_RECORDED,
            {"approval": approval.model_dump(mode="json", by_alias=True)},
        )

    def _handle_chat(self, command: SessionCommand) -> tuple[SessionEvent, ...]:
        prompt = str(command.payload.get("prompt", ""))
        stream = self.backend.chat_turn(session_id=command.session_id, prompt_length=len(prompt))
        return tuple(self._translate_backend_events(command.session_id, stream))

    def _handle_run(self, command: SessionCommand) -> tuple[SessionEvent, ...]:
        prompt = str(command.payload.get("prompt", ""))
        endpoint = str(command.payload.get("endpoint", "https://model.local.invalid/v1/chat"))
        request = ModelCallRequest(
            endpoint=endpoint,
            capability_tier=self.model_provider_adapter.capability_tier,
            purpose="synthetic core harness run",
        )
        invoke_model = bool(command.payload.get("invoke_model", False))
        decision = self.model_provider_adapter.evaluate_model_call(request)
        policy_payload: dict[str, JsonValue] = {"decision": decision.model_dump(mode="json")}
        error_event: SessionEvent | None = None
        model_call_approved = (
            self._has_model_call_approval(decision.decision_id) and decision.decision == "allow"
        )
        if invoke_model:
            try:
                engine = ModelPolicyEngine(self.platform_adapter.default_policy_profile())
                attestation = engine.attestation(
                    decision=decision,
                    record_id=f"{command.command_id}-attestation",
                    session_id=command.session_id,
                    occurred_at=self.clock(),
                )
                policy_payload["attestation"] = attestation.model_dump(mode="json")
                if decision.decision == "allow" and not model_call_approved:
                    error_event = self._record_event(
                        EventKind.ERROR_RECORDED,
                        {
                            "command": _kind_value(command.kind),
                            "reason": (
                                "local model invocation requires approved model-call decision"
                            ),
                        },
                    )
                elif model_call_approved:
                    response = _invoke_loopback_model(
                        endpoint=endpoint,
                        session_id=command.session_id,
                        prompt_length=len(prompt),
                    )
                    policy_payload["response_metadata"] = _model_response_metadata(response)
            except LocalModelInvocationError as error:
                error_event = self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "command": _kind_value(command.kind),
                        "reason": str(error),
                    },
                )
        policy_event = self._record_event(EventKind.MODEL_CALL_DECISION_RECORDED, policy_payload)
        model_invocation_required = invoke_model and model_call_approved
        model_invocation_succeeded = (
            not model_invocation_required or "response_metadata" in policy_payload
        )
        approved = model_call_approved and model_invocation_succeeded
        stream = self.backend.run_turn(
            session_id=command.session_id,
            prompt_length=len(prompt),
            approved=approved,
        )
        events: list[SessionEvent] = []
        if error_event is not None:
            events.append(error_event)
        events.append(policy_event)
        events.extend(self._translate_backend_events(command.session_id, stream))
        return tuple(events)

    def _translate_backend_events(
        self, session_id: str, stream: tuple[BackendEvent, ...]
    ) -> list[SessionEvent]:
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
            elif event.kind == BackendEventKind.CONFIRMATION:
                translated.append(self._record_confirmation(session_id, event))
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
        return translated

    def _record_confirmation(self, session_id: str, event: BackendEvent) -> SessionEvent:
        tool_call = _require_tool_call(event)
        if event.approved:
            return self._record_event(
                EventKind.CONFIRMATION_RESOLVED,
                {"tool_call_id": tool_call.tool_call_id, "decision": "approved"},
            )
        request = ConfirmationRequest(
            request_id=f"{tool_call.tool_call_id}-confirm",
            session_id=session_id,
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
        exported = self.audit_log.export_jsonl()
        self.store.audit_export_path.write_text(exported, encoding="utf-8")
        return event

    def _has_model_call_approval(self, decision_id: str) -> bool:
        for event in self.store.read_events():
            if _event_kind_value(event.kind) != EventKind.APPROVAL_RECORDED.value:
                continue
            approval_payload = _dict_payload(event.payload.get("approval"), "approval")
            if (
                approval_payload.get("target_type") == "model-call"
                and approval_payload.get("target_id") == decision_id
                and approval_payload.get("decision") == "approved"
            ):
                return True
        return False

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


def _optional_string(value: JsonValue | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _kind_value(kind: CommandKind | str) -> str:
    if isinstance(kind, CommandKind):
        return kind.value
    return kind


def _event_kind_value(kind: EventKind | str) -> str:
    if isinstance(kind, EventKind):
        return kind.value
    return kind


def _approval_target_type(value: JsonValue | None) -> ApprovalTargetType:
    candidate = "skill" if value is None else str(value)
    if candidate not in _APPROVAL_TARGET_TYPES:
        msg = f"unsupported approval target type: {candidate}"
        raise ValueError(msg)
    return cast(ApprovalTargetType, candidate)


def _dict_payload(value: JsonValue | None, name: str) -> dict[str, JsonValue]:
    if isinstance(value, dict):
        return value
    msg = f"expected {name} payload to be an object"
    raise TypeError(msg)


def _invoke_loopback_model(
    *,
    endpoint: str,
    session_id: str,
    prompt_length: int,
) -> JsonValue:
    parsed = urlsplit(endpoint)
    host = parsed.hostname or ""
    if parsed.scheme != "http" or host not in _LOOPBACK_HOSTS:
        msg = "local model demo can invoke only http loopback endpoints"
        raise LocalModelInvocationError(msg)
    request_payload = {
        "model": "heartwood-local-demo",
        "messages": [{"role": "user", "content": "[scrubbed]"}],
        "metadata": {
            "session_id": session_id,
            "prompt_length": prompt_length,
        },
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(request_payload, sort_keys=True).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=5) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            msg = "local model invocation rejected redirect"
        else:
            msg = f"local model invocation failed: {error}"
        raise LocalModelInvocationError(msg) from error
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        msg = f"local model invocation failed: {error}"
        raise LocalModelInvocationError(msg) from error
    if not isinstance(decoded, str | int | float | bool | list | dict) and decoded is not None:
        msg = "local model returned a non-JSON response"
        raise LocalModelInvocationError(msg)
    return cast(JsonValue, decoded)


def _model_response_metadata(response: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(response, dict):
        return {"status": "ok", "response_type": type(response).__name__}
    metadata: dict[str, JsonValue] = {"status": "ok"}
    for key in ("id", "model", "object"):
        value = response.get(key)
        if isinstance(value, str):
            metadata[key] = value
    choices = response.get("choices")
    if isinstance(choices, list):
        metadata["choices_count"] = len(choices)
    usage = response.get("usage")
    if isinstance(usage, dict):
        safe_usage: dict[str, JsonValue] = {}
        for key, value in usage.items():
            if isinstance(value, int | float) and not isinstance(value, bool):
                safe_usage[key] = value
        metadata["usage"] = safe_usage
    return metadata
