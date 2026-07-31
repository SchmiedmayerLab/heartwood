# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session orchestration over policy, an agent backend, state, and audit."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import assert_never, cast

from heartwood.adapters import PlatformAdapter
from heartwood.adapters.platform import GenericPlatformAdapter, select_platform_adapter
from heartwood.audit import AuditIntegrityError, AuditLog
from heartwood.core_adapter._facade import (
    AgentBackend,
    BackendAgentMessageEvent,
    BackendConfirmationRequestEvent,
    BackendConfirmationResolutionEvent,
    BackendErrorCode,
    BackendErrorEvent,
    BackendEvent,
    BackendLifecycleEvent,
    BackendSubagentEvent,
    BackendTaskPlanEvent,
    BackendToolCallEvent,
    BackendToolExecutionEvent,
    BackendUsageEvent,
    DeterministicAgentBackend,
    backend_error_is_fatal,
    backend_error_message,
)
from heartwood.core_adapter._state import FileSessionStore, SessionRecoveryError
from heartwood.model_policy import ModelPolicyEngine
from heartwood.schemas import ConfirmationRequest, JsonValue, PolicyProfile
from heartwood.session import (
    CommandKind,
    EventKind,
    SessionCommand,
    SessionEvent,
    compute_session_event_hash,
)


@dataclass(frozen=True, slots=True)
class SessionResult:
    """Events emitted while handling one command."""

    events: tuple[SessionEvent, ...]
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class _ApprovalIntent:
    command_id: str
    group_id: str
    approved: bool
    tool_call_ids: tuple[str, ...]


class CommandConflictError(ValueError):
    """Raised when a command identifier is reused with different content."""


class SessionService:
    """Core session service shared by every interaction surface."""

    def __init__(
        self,
        *,
        store: FileSessionStore,
        platform_adapter: PlatformAdapter,
        backend: AgentBackend,
        policy_profile: PolicyProfile | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], str] | None = None,
        event_sink: Callable[[tuple[SessionEvent, ...]], None] | None = None,
        token_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.audit_log = AuditLog(store.audit_path)
        self.platform_adapter = platform_adapter
        self.backend = backend
        self.policy_profile = policy_profile or platform_adapter.default_policy_profile()
        self.policy = ModelPolicyEngine(self.policy_profile)
        self.env = os.environ if env is None else env
        self.clock: Callable[[], str] = _utc_now if clock is None else clock
        self._command_lock = RLock()
        self._event_sink = event_sink or (lambda _events: None)
        self._token_sink = token_sink or (lambda _delta: None)
        self._known_source_event_ids: set[str] | None = None
        self.backend.bind_runtime(
            event_sink=self._accept_backend_events,
            token_sink=self._token_sink,
        )

    @classmethod
    def synthetic_default(
        cls,
        workspace: Path,
        *,
        session_id: str = "session-synthetic-001",
        env: Mapping[str, str] | None = None,
        clock: Callable[[], str] | None = None,
        event_sink: Callable[[tuple[SessionEvent, ...]], None] | None = None,
        token_sink: Callable[[str], None] | None = None,
    ) -> SessionService:
        """Build the deterministic synthetic service used in tests and replay."""
        platform = GenericPlatformAdapter()
        store = FileSessionStore(workspace, session_id)
        return cls(
            store=store,
            platform_adapter=platform,
            backend=DeterministicAgentBackend(
                persistence_path=store.session_dir / ".deterministic-backend.json"
            ),
            env={} if env is None else env,
            clock=(lambda: "2026-01-01T00:00:00Z") if clock is None else clock,
            event_sink=event_sink,
            token_sink=token_sink,
        )

    @classmethod
    def local_default(
        cls,
        workspace: Path,
        *,
        session_id: str = "session-main",
        backend: AgentBackend | None = None,
        policy_profile: PolicyProfile | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], str] | None = None,
        event_sink: Callable[[tuple[SessionEvent, ...]], None] | None = None,
        token_sink: Callable[[str], None] | None = None,
    ) -> SessionService:
        """Build a local service with an explicitly supplied or deterministic backend."""
        active_env = os.environ if env is None else env
        platform = select_platform_adapter(active_env)
        store = FileSessionStore(workspace, session_id)
        return cls(
            store=store,
            platform_adapter=platform,
            backend=(
                DeterministicAgentBackend(
                    persistence_path=store.session_dir / ".deterministic-backend.json"
                )
                if backend is None
                else backend
            ),
            policy_profile=policy_profile,
            env=active_env,
            clock=clock,
            event_sink=event_sink,
            token_sink=token_sink,
        )

    def handle(
        self,
        command: SessionCommand,
        *,
        unavailable_reason: str | None = None,
        reconcile_before_command: bool = True,
    ) -> SessionResult:
        """Handle one command, persist events, and append audit records."""
        if command.session_id != self.store.session_id:
            msg = (
                f"command session {command.session_id} does not match "
                f"store session {self.store.session_id}"
            )
            raise ValueError(msg)
        with self._command_lock:
            self.store.acquire_writer()
            self._recover_pending_commit_locked()
            command_kind = _kind_value(command.kind)
            reconciled = (
                ()
                if command_kind == CommandKind.AUDIT_EXPORT.value or not reconcile_before_command
                else (
                    *self._reconcile_locked(),
                    *self._recover_approval_commands_locked(),
                )
            )
            if reconciled:
                self._event_sink(reconciled)
            persisted = self.replay_events()
            command_hash = _command_hash(command)
            record = self.store.command_record(command.command_id)
            if record is not None:
                return _command_result_from_receipt(persisted, command, command_hash, record)
            unresolved = self.store.unresolved_command_ids()
            if unresolved:
                raise SessionRecoveryError(
                    f"session {self.store.session_id} has an interrupted command "
                    f"({unresolved[0]}) and cannot accept more work; replay and verify the "
                    "session, then continue in a new session"
                )
            duplicate = _legacy_duplicate_command_result(persisted, command, command_hash)
            if duplicate is not None:
                self.store.record_completed_legacy_command(
                    command_id=command.command_id,
                    command_hash=command_hash,
                    first_sequence=duplicate.events[0].sequence,
                    last_sequence=duplicate.events[-1].sequence,
                )
                return duplicate
            if unavailable_reason is None and command_kind != CommandKind.AUDIT_EXPORT.value:
                fatal_error = next(
                    (
                        event
                        for event in reversed(persisted)
                        if event.kind == EventKind.ERROR_RECORDED
                        and backend_error_is_fatal(event.payload.get("code"))
                    ),
                    None,
                )
                if fatal_error is not None:
                    unavailable_reason = (
                        f"{command_kind} is unavailable because this session has "
                        "an unknown execution outcome"
                    )
            first_sequence = self.store.next_sequence()
            self.store.accept_command(
                command_id=command.command_id,
                command_hash=command_hash,
                first_sequence=first_sequence,
            )
            result = self._handle_new_command(
                command,
                unavailable_reason=unavailable_reason,
            )
            self.store.complete_command(
                command_id=command.command_id,
                command_hash=command_hash,
                first_sequence=first_sequence,
                last_sequence=result.events[-1].sequence,
            )
            return result

    def _handle_new_command(
        self,
        command: SessionCommand,
        *,
        unavailable_reason: str | None,
    ) -> SessionResult:
        """Execute a command that has not previously been accepted."""
        command_kind = _kind_value(command.kind)
        events = [
            self._record_event(
                EventKind.COMMAND_RECEIVED,
                {
                    "actor_id": command.actor_id,
                    "command_id": command.command_id,
                    "command_hash": _command_hash(command),
                    "command_kind": command_kind,
                },
            )
        ]
        if unavailable_reason is not None:
            events.append(
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "command": command_kind,
                        "reason": unavailable_reason,
                        "affects_lifecycle": False,
                    },
                )
            )
            return SessionResult(events=tuple(events))
        if command_kind == CommandKind.CHAT.value:
            events.extend(self._handle_task(command))
        elif command_kind in {CommandKind.APPROVE.value, CommandKind.DENY.value}:
            events.extend(self._handle_action_decision(command))
        elif command_kind == CommandKind.PAUSE.value:
            backend_events = self.backend.pause(session_id=command.session_id)
            if not any(isinstance(event, BackendErrorEvent) for event in backend_events):
                events.append(
                    self._record_event(
                        EventKind.SESSION_PAUSED,
                        {"command_id": command.command_id},
                    )
                )
            events.extend(self._translate_backend_events(backend_events))
        elif command_kind == CommandKind.RESUME.value:
            pending_group = self.backend.pending_action_group(session_id=command.session_id)
            if pending_group is not None:
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
                    backend_events = self.backend.resume(session_id=command.session_id)
                    if not any(isinstance(event, BackendErrorEvent) for event in backend_events):
                        events.append(
                            self._record_event(
                                EventKind.SESSION_RESUMED,
                                {"command_id": command.command_id},
                            )
                        )
                    events.extend(self._translate_backend_events(backend_events))
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
        """Return events after verifying their one-to-one audit correspondence."""
        return self.store.replay_events()

    def reconcile(self) -> tuple[SessionEvent, ...]:
        """Commit OpenHands state not yet represented in the durable session."""
        with self._command_lock:
            self.store.acquire_writer()
            self._recover_pending_commit_locked()
            if self._has_failed_approval_recovery_locked():
                events = self._recover_approval_commands_locked()
            else:
                events = (
                    *self._reconcile_locked(),
                    *self._recover_approval_commands_locked(),
                )
        if events:
            self._event_sink(events)
        return events

    def close(self) -> None:
        """Release backend resources."""
        self.backend.close()
        self.store.release_writer()

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
        if self.backend.pending_action_group(session_id=command.session_id) is not None:
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
        target_type = str(command.payload.get("target_type", "action-set"))
        if target_type != "action-set":
            return (
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "command": _kind_value(command.kind),
                        "reason": (
                            "interactive approval is supported only for the complete pending "
                            "action set"
                        ),
                    },
                ),
            )
        action_group_id = str(command.payload.get("target_id", ""))
        if not action_group_id:
            return (
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {"command": _kind_value(command.kind), "reason": "target_id is required"},
                ),
            )
        approved = _kind_value(command.kind) == CommandKind.APPROVE.value
        pending_group = self.backend.pending_action_group(session_id=command.session_id)
        if pending_group is None or pending_group.group_id != action_group_id:
            return (
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "command": _kind_value(command.kind),
                        "reason": (f"no matching pending action group: {action_group_id}"),
                    },
                ),
            )
        events: list[SessionEvent] = []
        if approved and self.backend.continuation_requires_model_authorization:
            authorized, authorization_events = self._authorize_backend(
                command,
                purpose=f"approved action continuation through {self.backend.backend_id}",
            )
            events.extend(authorization_events)
            if not authorized:
                return tuple(events)
        decision = "approved" if approved else "denied"
        events.append(
            self._record_event(
                EventKind.APPROVAL_RECORDED,
                {
                    "command_id": command.command_id,
                    "group_id": pending_group.group_id,
                    "decision": decision,
                    "tool_call_ids": [action.tool_call_id for action in pending_group.actions],
                },
            )
        )
        backend_events = self.backend.resolve_confirmation(
            session_id=command.session_id,
            action_group_id=action_group_id,
            approved=approved,
        )
        events.extend(self._translate_backend_events(backend_events))
        if not _backend_confirmation_resolved(
            backend_events,
            group_id=pending_group.group_id,
            tool_call_ids=tuple(action.tool_call_id for action in pending_group.actions),
            approved=approved,
        ):
            events.append(
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "backend_id": self.backend.backend_id,
                        "code": BackendErrorCode.ACTION_OUTCOME_UNKNOWN.value,
                        "reason": backend_error_message(BackendErrorCode.ACTION_OUTCOME_UNKNOWN),
                        "command_id": command.command_id,
                        "group_id": pending_group.group_id,
                        "tool_call_ids": [action.tool_call_id for action in pending_group.actions],
                    },
                )
            )
        return tuple(events)

    def _translate_backend_events(self, stream: tuple[BackendEvent, ...]) -> list[SessionEvent]:
        translated: list[SessionEvent] = []
        known_source_event_ids = self._known_source_event_ids_locked()
        for event in stream:
            if (
                event.source_event_id is not None
                and event.source_event_id in known_source_event_ids
            ):
                continue
            source_payload: dict[str, JsonValue] = (
                {"source_event_id": event.source_event_id}
                if event.source_event_id is not None
                else {}
            )
            if isinstance(event, BackendAgentMessageEvent):
                translated.append(
                    self._record_event(
                        EventKind.AGENT_MESSAGE_EMITTED,
                        {"content": event.message, **source_payload},
                    )
                )
            elif isinstance(event, BackendToolCallEvent):
                tool_call = event.tool_call
                translated.append(
                    self._record_event(
                        EventKind.TOOL_CALL_PROPOSED,
                        {
                            "tool_call_id": tool_call.tool_call_id,
                            "action_id": tool_call.action_id,
                            "tool_name": tool_call.tool_name,
                            "kind": tool_call.kind,
                            "risk": tool_call.risk,
                            "summary": tool_call.summary,
                            "arguments": tool_call.arguments,
                            "affected_paths": list(tool_call.affected_paths),
                            "project_path": tool_call.project_path,
                            **source_payload,
                        },
                    )
                )
            elif isinstance(event, BackendConfirmationRequestEvent):
                translated.append(self._record_confirmation_request(event))
            elif isinstance(event, BackendConfirmationResolutionEvent):
                tool_call = event.tool_call
                translated.append(
                    self._record_event(
                        EventKind.CONFIRMATION_RESOLVED,
                        {
                            "tool_call_id": tool_call.tool_call_id,
                            "group_id": event.action_group_id,
                            "decision": "approved" if event.approved else "denied",
                            **source_payload,
                        },
                    )
                )
            elif isinstance(event, BackendToolExecutionEvent):
                execution = event.tool_execution
                translated.append(
                    self._record_event(
                        EventKind.TOOL_EXECUTION_RECORDED,
                        {
                            "backend_id": self.backend.backend_id,
                            "tool_call_id": execution.tool_call_id,
                            "action_id": execution.action_id,
                            "tool_name": execution.tool_name,
                            "exit_code": execution.exit_code,
                            "summary": execution.summary,
                            "result": execution.result,
                            "result_truncated": execution.result_truncated,
                            **source_payload,
                        },
                    )
                )
            elif isinstance(event, BackendLifecycleEvent):
                translated.append(
                    self._record_event(
                        EventKind.AGENT_LIFECYCLE_UPDATED,
                        {"status": event.lifecycle.value, **source_payload},
                    )
                )
            elif isinstance(event, BackendTaskPlanEvent):
                translated.append(
                    self._record_event(
                        EventKind.TASK_PLAN_UPDATED,
                        {
                            "tasks": [
                                {
                                    "title": task.title,
                                    "status": task.status.value,
                                }
                                for task in event.tasks
                            ],
                            **source_payload,
                        },
                    )
                )
            elif isinstance(event, BackendUsageEvent):
                usage = event.usage
                translated.append(
                    self._record_event(
                        EventKind.MODEL_USAGE_UPDATED,
                        {
                            "usage": {
                                "usage_id": usage.usage_id,
                                "model_name": usage.model_name,
                                "call_count": usage.call_count,
                                "prompt_tokens": usage.prompt_tokens,
                                "completion_tokens": usage.completion_tokens,
                                "cache_read_tokens": usage.cache_read_tokens,
                                "cache_write_tokens": usage.cache_write_tokens,
                                "reasoning_tokens": usage.reasoning_tokens,
                                "context_window": usage.context_window,
                                "accumulated_cost": usage.accumulated_cost,
                            },
                            **source_payload,
                        },
                    )
                )
            elif isinstance(event, BackendSubagentEvent):
                subagent = event.subagent
                translated.append(
                    self._record_event(
                        EventKind.SUBAGENT_UPDATED,
                        {
                            "subagent": {
                                "invocation_id": subagent.invocation_id,
                                "task_id": subagent.task_id,
                                "agent_name": subagent.agent_name,
                                "status": subagent.status.value,
                                "parent_session_id": subagent.parent_session_id,
                                "parent_action_id": subagent.parent_action_id,
                            },
                            **source_payload,
                        },
                    )
                )
            elif isinstance(event, BackendErrorEvent):
                translated.append(
                    self._record_event(
                        EventKind.ERROR_RECORDED,
                        {
                            "backend_id": self.backend.backend_id,
                            "code": event.error_code.value,
                            "reason": backend_error_message(event.error_code),
                            **source_payload,
                        },
                    )
                )
            else:
                assert_never(event)
        return translated

    def _record_confirmation_request(
        self,
        event: BackendConfirmationRequestEvent,
    ) -> SessionEvent:
        tool_call = event.tool_call
        request = ConfirmationRequest(
            request_id=f"{tool_call.tool_call_id}-confirm",
            session_id=self.store.session_id,
            tool_call_id=tool_call.tool_call_id,
            tool_name=tool_call.tool_name,
            risk=tool_call.risk,
            summary=tool_call.summary,
            arguments=tool_call.arguments,
        )
        request_payload = request.model_dump(mode="json")
        request_payload["group_id"] = event.action_group_id
        request_payload["action_id"] = tool_call.action_id
        request_payload["kind"] = tool_call.kind
        request_payload["affected_paths"] = list(tool_call.affected_paths)
        request_payload["project_path"] = tool_call.project_path
        if event.source_event_id is not None:
            request_payload["source_event_id"] = event.source_event_id
        return self._record_event(
            EventKind.CONFIRMATION_REQUESTED,
            {
                "request": request_payload,
                **(
                    {"source_event_id": event.source_event_id}
                    if event.source_event_id is not None
                    else {}
                ),
            },
        )

    def _accept_backend_events(self, stream: tuple[BackendEvent, ...]) -> None:
        """Persist final background events and publish their shared projection inputs."""
        if not stream:
            return
        with self._command_lock:
            self.store.acquire_writer()
            self._recover_pending_commit_locked()
            events = tuple(self._translate_backend_events(stream))
        if events:
            self._event_sink(events)

    def _reconcile_locked(self) -> tuple[SessionEvent, ...]:
        known_source_event_ids = self._known_source_event_ids_locked()
        return tuple(
            self._translate_backend_events(
                self.backend.reconcile(
                    session_id=self.store.session_id,
                    known_source_event_ids=frozenset(known_source_event_ids),
                )
            )
        )

    def _known_source_event_ids_locked(self) -> set[str]:
        if self._known_source_event_ids is None:
            self._known_source_event_ids = set(_source_event_ids(self.replay_events()))
        return self._known_source_event_ids

    def _recover_pending_commit_locked(self) -> None:
        if self.store.recover_pending_commit() and self._known_source_event_ids is not None:
            self._known_source_event_ids = set(_source_event_ids(self.replay_events()))

    def _has_failed_approval_recovery_locked(self) -> bool:
        unresolved_command_ids = self.store.unresolved_command_ids()
        if not unresolved_command_ids:
            return False
        events = self.replay_events()
        for command_id in unresolved_command_ids:
            record = self.store.command_record(command_id)
            if record is None:
                raise SessionRecoveryError(f"missing command receipt for {command_id}")
            intent = _approval_intent(events, record)
            if intent is not None and _approval_intent_failed(events, intent):
                return True
        return False

    def _recover_approval_commands_locked(self) -> tuple[SessionEvent, ...]:
        """Finish an interrupted approval only when persisted state makes it unambiguous."""
        recovered: list[SessionEvent] = []
        for command_id in self.store.unresolved_command_ids():
            events = self.replay_events()
            record = self.store.command_record(command_id)
            if record is None:
                raise SessionRecoveryError(f"missing command receipt for {command_id}")
            intent = _approval_intent(events, record)
            if intent is None:
                continue
            if not _approval_intent_finished(events, intent):
                pending_group = self.backend.pending_action_group(session_id=self.store.session_id)
                if (
                    pending_group is not None
                    and pending_group.group_id == intent.group_id
                    and tuple(action.tool_call_id for action in pending_group.actions)
                    == intent.tool_call_ids
                ):
                    backend_events = self.backend.resolve_confirmation(
                        session_id=self.store.session_id,
                        action_group_id=intent.group_id,
                        approved=intent.approved,
                    )
                    translated = self._translate_backend_events(backend_events)
                    recovered.extend(translated)
                else:
                    recovered.append(self._record_unknown_approval_outcome(intent))
                events = self.replay_events()
            if not _approval_intent_finished(events, intent):
                recovered.append(self._record_unknown_approval_outcome(intent))
                events = self.replay_events()
            if not _approval_intent_finished(events, intent):
                continue
            first_sequence = record.get("first_sequence")
            command_hash = record.get("command_hash")
            if not isinstance(first_sequence, int) or not isinstance(command_hash, str):
                raise SessionRecoveryError(f"accepted approval receipt is invalid for {command_id}")
            last_sequence = events[-1].sequence
            self.store.complete_command(
                command_id=command_id,
                command_hash=command_hash,
                first_sequence=first_sequence,
                last_sequence=last_sequence,
            )
        return tuple(recovered)

    def _record_unknown_approval_outcome(self, intent: _ApprovalIntent) -> SessionEvent:
        return self._record_event(
            EventKind.ERROR_RECORDED,
            {
                "backend_id": self.backend.backend_id,
                "code": BackendErrorCode.ACTION_OUTCOME_UNKNOWN.value,
                "reason": backend_error_message(BackendErrorCode.ACTION_OUTCOME_UNKNOWN),
                "command_id": intent.command_id,
                "group_id": intent.group_id,
                "tool_call_ids": list(intent.tool_call_ids),
            },
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
        audit_events = self.audit_log.read()
        self.audit_log.verify(audit_events)
        if len(audit_events) != sequence:
            raise AuditIntegrityError(
                "session event and audit logs have different lengths before append"
            )
        previous_event_hash = audit_events[-1].event_hash if audit_events else None
        occurred_at = self.clock()
        event = SessionEvent(
            event_id=f"{self.store.session_id}-event-{sequence:06d}",
            session_id=self.store.session_id,
            sequence=sequence,
            kind=kind,
            occurred_at=occurred_at,
            payload=payload,
            previous_event_hash=previous_event_hash,
        )
        audit_payload = {
            **_audit_payload(kind, payload),
            "session_event_hash": compute_session_event_hash(event),
        }
        audit_event = self.audit_log.prepare(
            session_id=self.store.session_id,
            event_type=kind.value,
            occurred_at=occurred_at,
            payload=audit_payload,
        )
        if (
            audit_event.sequence != event.sequence
            or audit_event.previous_event_hash != previous_event_hash
        ):
            raise AuditIntegrityError("audit log changed during session event append")
        self.store.commit_event(event, audit_event)
        source_event_id = payload.get("source_event_id")
        if (
            self._known_source_event_ids is not None
            and isinstance(source_event_id, str)
            and source_event_id
        ):
            self._known_source_event_ids.add(source_event_id)
        return event


def _audit_payload(kind: EventKind, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Project an operational event into its content-minimized audit representation."""
    if kind == EventKind.COMMAND_RECEIVED:
        return _selected_audit_fields(
            payload,
            "actor_id",
            "command_id",
            "command_hash",
            "command_kind",
        )
    if kind == EventKind.MODEL_CALL_DECISION_RECORDED:
        decision = payload.get("decision")
        model_profile = payload.get("model_profile")
        return {
            "decision": (
                _selected_audit_fields(
                    decision,
                    "decision_id",
                    "policy_profile_id",
                    "endpoint",
                    "capability_tier",
                    "decision",
                )
                if isinstance(decision, dict)
                else {}
            ),
            "model_profile": (
                _selected_audit_fields(
                    model_profile,
                    "backend_id",
                    "profile_id",
                    "capability_tier",
                    "action_confirmation_mode",
                )
                if isinstance(model_profile, dict)
                else {}
            ),
        }
    if kind == EventKind.APPROVAL_RECORDED:
        tool_call_ids = payload.get("tool_call_ids")
        return {
            **_selected_audit_fields(
                payload,
                "command_id",
                "group_id",
                "decision",
            ),
            "action_count": len(tool_call_ids) if isinstance(tool_call_ids, list) else 0,
        }
    if kind == EventKind.USER_MESSAGE_RECORDED:
        content = payload.get("content")
        return {
            "actor_id": payload.get("actor_id", ""),
            "command_id": payload.get("command_id", ""),
            "content_chars": len(content) if isinstance(content, str) else 0,
        }
    if kind == EventKind.AGENT_MESSAGE_EMITTED:
        content = payload.get("content")
        return {
            "content_chars": len(content) if isinstance(content, str) else 0,
        }
    if kind == EventKind.TOOL_CALL_PROPOSED:
        return _selected_audit_fields(
            payload,
            "tool_call_id",
            "tool_name",
            "risk",
        )
    if kind == EventKind.CONFIRMATION_REQUESTED:
        request = payload.get("request")
        if not isinstance(request, dict):
            return {"request": "invalid"}
        return {
            "request": _selected_audit_fields(
                request,
                "request_id",
                "group_id",
                "tool_call_id",
                "tool_name",
                "risk",
            )
        }
    if kind == EventKind.CONFIRMATION_RESOLVED:
        return _selected_audit_fields(
            payload,
            "group_id",
            "tool_call_id",
            "decision",
        )
    if kind == EventKind.TOOL_EXECUTION_RECORDED:
        return _selected_audit_fields(
            payload,
            "backend_id",
            "tool_call_id",
            "action_id",
            "tool_name",
            "exit_code",
        )
    if kind == EventKind.TASK_PLAN_UPDATED:
        tasks = payload.get("tasks")
        task_items = tasks if isinstance(tasks, list) else []
        counts: dict[str, int] = {}
        for task in task_items:
            if isinstance(task, dict):
                status = str(task.get("status", "unknown"))
                counts[status] = counts.get(status, 0) + 1
        normalized_counts: dict[str, JsonValue] = dict(counts)
        return {
            "task_count": len(task_items),
            "status_counts": normalized_counts,
        }
    if kind == EventKind.MODEL_USAGE_UPDATED:
        usage = payload.get("usage")
        return {
            "usage": (
                _selected_audit_fields(
                    usage,
                    "usage_id",
                    "model_name",
                    "call_count",
                    "prompt_tokens",
                    "completion_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "context_window",
                    "accumulated_cost",
                )
                if isinstance(usage, dict)
                else {}
            )
        }
    if kind == EventKind.SUBAGENT_UPDATED:
        subagent = payload.get("subagent")
        return {
            "subagent": (
                _selected_audit_fields(
                    subagent,
                    "invocation_id",
                    "task_id",
                    "agent_name",
                    "status",
                    "parent_session_id",
                    "parent_action_id",
                )
                if isinstance(subagent, dict)
                else {}
            )
        }
    if kind == EventKind.AGENT_LIFECYCLE_UPDATED:
        return _selected_audit_fields(payload, "status")
    if kind in {EventKind.SESSION_PAUSED, EventKind.SESSION_RESUMED}:
        return _selected_audit_fields(payload, "command_id")
    if kind == EventKind.ERROR_RECORDED:
        minimized = _selected_audit_fields(
            payload,
            "backend_id",
            "code",
            "command",
        )
        if "reason" in payload:
            minimized["reason"] = "[scrubbed]"
        return minimized
    if kind == EventKind.AUDIT_EXPORT_RECORDED:
        return _selected_audit_fields(payload, "event_count", "scrubbed")
    return {"payload_omitted": True}


def _selected_audit_fields(
    payload: Mapping[str, JsonValue],
    *names: str,
) -> dict[str, JsonValue]:
    return {name: payload[name] for name in names if name in payload}


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _kind_value(kind: CommandKind | str) -> str:
    return kind.value if isinstance(kind, CommandKind) else kind


def _command_hash(command: SessionCommand) -> str:
    canonical = json.dumps(
        command.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _legacy_duplicate_command_result(
    events: tuple[SessionEvent, ...],
    command: SessionCommand,
    expected_hash: str,
) -> SessionResult | None:
    for index, event in enumerate(events):
        if event.kind != EventKind.COMMAND_RECEIVED:
            continue
        if event.payload.get("command_id") != command.command_id:
            continue
        if event.payload.get("command_hash") != expected_hash:
            raise CommandConflictError(
                f"command id {command.command_id} was already used with different content"
            )
        end = len(events)
        for candidate_index in range(index + 1, len(events)):
            if events[candidate_index].kind == EventKind.COMMAND_RECEIVED:
                end = candidate_index
                break
        return SessionResult(events=events[index:end], replayed=True)
    return None


def _command_result_from_receipt(
    events: tuple[SessionEvent, ...],
    command: SessionCommand,
    expected_hash: str,
    record: dict[str, object],
) -> SessionResult:
    if record.get("command_hash") != expected_hash:
        raise CommandConflictError(
            f"command id {command.command_id} was already used with different content"
        )
    if record.get("state") != "completed":
        raise SessionRecoveryError(
            f"command {command.command_id} was interrupted after acceptance and will not be "
            "executed again automatically; inspect session replay, then continue in a new session"
        )
    first_sequence = record.get("first_sequence")
    last_sequence = record.get("last_sequence")
    if not isinstance(first_sequence, int) or not isinstance(last_sequence, int):
        raise SessionRecoveryError(f"completed command receipt is invalid for {command.command_id}")
    selected = tuple(event for event in events if first_sequence <= event.sequence <= last_sequence)
    expected_count = last_sequence - first_sequence + 1
    if (
        len(selected) != expected_count
        or not selected
        or selected[0].kind != EventKind.COMMAND_RECEIVED
        or selected[0].payload.get("command_id") != command.command_id
        or selected[0].payload.get("command_hash") != expected_hash
    ):
        raise SessionRecoveryError(
            f"completed command events do not match the receipt for {command.command_id}"
        )
    return SessionResult(events=selected, replayed=True)


def _source_event_ids(events: tuple[SessionEvent, ...]) -> frozenset[str]:
    return frozenset(
        source_event_id
        for event in events
        if isinstance((source_event_id := event.payload.get("source_event_id")), str)
        and source_event_id
    )


def _approval_intent(
    events: tuple[SessionEvent, ...],
    record: Mapping[str, object],
) -> _ApprovalIntent | None:
    command_id = record.get("command_id")
    first_sequence = record.get("first_sequence")
    if not isinstance(command_id, str) or not isinstance(first_sequence, int):
        raise SessionRecoveryError("accepted command receipt is invalid")
    command_event = next(
        (event for event in events if event.sequence == first_sequence),
        None,
    )
    if (
        command_event is None
        or command_event.kind != EventKind.COMMAND_RECEIVED
        or command_event.payload.get("command_id") != command_id
    ):
        raise SessionRecoveryError(f"accepted command event is unavailable for {command_id}")
    if command_event.payload.get("command_kind") not in {
        CommandKind.APPROVE.value,
        CommandKind.DENY.value,
    }:
        return None
    approval = next(
        (
            event
            for event in events
            if event.kind == EventKind.APPROVAL_RECORDED
            and event.payload.get("command_id") == command_id
        ),
        None,
    )
    if approval is None:
        return None
    group_id = approval.payload.get("group_id")
    decision = approval.payload.get("decision")
    raw_tool_call_ids = approval.payload.get("tool_call_ids")
    if (
        not isinstance(group_id, str)
        or decision not in {"approved", "denied"}
        or not isinstance(raw_tool_call_ids, list)
        or not raw_tool_call_ids
        or any(not isinstance(tool_call_id, str) for tool_call_id in raw_tool_call_ids)
    ):
        raise SessionRecoveryError(f"accepted approval intent is invalid for {command_id}")
    return _ApprovalIntent(
        command_id=command_id,
        group_id=group_id,
        approved=decision == "approved",
        tool_call_ids=tuple(cast(str, tool_call_id) for tool_call_id in raw_tool_call_ids),
    )


def _approval_intent_resolved(
    events: tuple[SessionEvent, ...],
    intent: _ApprovalIntent,
) -> bool:
    expected_decision = "approved" if intent.approved else "denied"
    resolved = {
        tool_call_id
        for event in events
        if event.kind == EventKind.CONFIRMATION_RESOLVED
        and event.payload.get("group_id") == intent.group_id
        and event.payload.get("decision") == expected_decision
        and isinstance((tool_call_id := event.payload.get("tool_call_id")), str)
    }
    return resolved == set(intent.tool_call_ids)


def _approval_intent_failed(
    events: tuple[SessionEvent, ...],
    intent: _ApprovalIntent,
) -> bool:
    return any(
        event.kind == EventKind.ERROR_RECORDED
        and event.payload.get("code") == BackendErrorCode.ACTION_OUTCOME_UNKNOWN.value
        and event.payload.get("command_id") == intent.command_id
        and event.payload.get("group_id") == intent.group_id
        for event in events
    )


def _approval_intent_finished(
    events: tuple[SessionEvent, ...],
    intent: _ApprovalIntent,
) -> bool:
    return _approval_intent_resolved(events, intent) or _approval_intent_failed(events, intent)


def _backend_confirmation_resolved(
    events: tuple[BackendEvent, ...],
    *,
    group_id: str,
    tool_call_ids: tuple[str, ...],
    approved: bool,
) -> bool:
    resolved = {
        event.tool_call.tool_call_id
        for event in events
        if isinstance(event, BackendConfirmationResolutionEvent)
        and event.action_group_id == group_id
        and event.approved is approved
    }
    return resolved == set(tool_call_ids)
