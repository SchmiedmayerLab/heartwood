# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session service that accepts commands and emits structured events."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeAlias, cast

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
from heartwood.core_adapter._facade import AgentBackend, DeterministicAgentBackend
from heartwood.core_adapter._state import FileSessionStore
from heartwood.schemas import ApprovalRecord, JsonValue
from heartwood.session import CommandKind, EventKind, SessionCommand, SessionEvent

ApprovalDecision: TypeAlias = Literal["approved", "denied"]
ApprovalTargetType: TypeAlias = Literal["skill", "egress", "model-call"]

_APPROVAL_TARGET_TYPES = {"skill", "egress", "model-call"}


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
        elif command_kind == CommandKind.RUN.value:
            events.extend(self._handle_run(command))
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

    def _handle_run(self, command: SessionCommand) -> tuple[SessionEvent, ...]:
        prompt = str(command.payload.get("prompt", ""))
        endpoint = str(command.payload.get("endpoint", "https://model.local.invalid/v1/chat"))
        request = ModelCallRequest(
            endpoint=endpoint,
            capability_tier=self.model_provider_adapter.capability_tier,
            purpose="synthetic core harness run",
        )
        decision = self.model_provider_adapter.evaluate_model_call(request)
        policy_event = self._record_event(
            EventKind.MODEL_CALL_DECISION_RECORDED,
            {"decision": decision.model_dump(mode="json")},
        )
        turn = self.backend.run(
            session_id=command.session_id,
            prompt_length=len(prompt),
            approved=self._has_model_call_approval(decision.decision_id)
            and decision.decision == "allow",
        )
        execution = turn.tool_execution
        tool_event = self._record_event(
            EventKind.TOOL_EXECUTION_RECORDED,
            {
                "backend_id": turn.backend_id,
                "tool_name": execution.tool_name if execution else "none",
                "exit_code": execution.exit_code if execution else 1,
                "summary": execution.summary if execution else "no tool execution",
            },
        )
        return (policy_event, tool_event)

    def _handle_audit_export(self) -> SessionEvent:
        event = self._record_event(
            EventKind.AUDIT_EXPORT_RECORDED,
            {
                "path": str(self.store.audit_export_path),
                "event_count": len(self.audit_log.read()) + 1,
                "scrubbed": True,
            },
        )
        exported = self.audit_log.export_jsonl(scrub=True)
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
