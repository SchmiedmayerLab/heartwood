# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session orchestration over policy, an agent backend, state, and audit."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Literal, assert_never, cast
from uuid import UUID, uuid4

from heartwood.adapters import PlatformAdapter
from heartwood.adapters.platform import GenericPlatformAdapter, select_platform_adapter
from heartwood.audit import AuditIntegrityError, AuditLog, prepare_audit_event
from heartwood.core_adapter._facade import (
    AgentBackend,
    BackendAgentMessageEvent,
    BackendConfirmationRequestEvent,
    BackendConfirmationResolutionEvent,
    BackendErrorCode,
    BackendErrorEvent,
    BackendEvent,
    BackendExecutionSettledEvent,
    BackendLifecycleEvent,
    BackendSubagentEvent,
    BackendTaskPlanEvent,
    BackendToolCallEvent,
    BackendToolExecutionEvent,
    BackendUsageEvent,
    DeterministicAgentBackend,
    PendingActionGroup,
    backend_error_is_fatal,
    backend_error_message,
)
from heartwood.core_adapter._state import FileSessionStore, SessionRecoveryError
from heartwood.core_adapter.external_actions import (
    ExternalActionAdapter,
    ExternalActionDecision,
    ExternalActionError,
    ExternalActionProposal,
    ExternalActionRecord,
    ExternalActionRequest,
    canonical_request,
    external_action_records,
    reserved_external_identity,
    validate_external_transition,
)
from heartwood.core_adapter.external_results import (
    RESULT_PREFIX,
    ExternalResultProposal,
    ExternalResultRecord,
    external_result_records,
    validate_result_transition,
)
from heartwood.core_adapter.reproduction_journal import (
    JournaledReproduction,
    observe_reproduction,
    prepare_reproduction,
    reproduction_records,
)
from heartwood.core_adapter.worker_attempts import (
    WorkerAttemptError,
    WorkerAttemptIntent,
    WorkerAttemptLease,
    WorkerAttemptRecord,
    WorkerRevocationEvidence,
    WorkerRuntimeBinding,
    current_worker_attempt,
    validate_worker_transition,
)
from heartwood.core_adapter.workflow_review import validate_parallel_review_plan
from heartwood.core_adapter.workflow_runtime import (
    WorkflowEvaluator,
    handle_workflow_command,
    record_parallel_review_admission,
    settle_workflow_correction,
    settle_workflow_review,
    workflow_admission_reason,
    workflow_run,
)
from heartwood.model_policy import ModelPolicyEngine
from heartwood.schemas import ConfirmationRequest, JsonValue, PolicyProfile
from heartwood.schemas.experiments import ExperimentExportBinding
from heartwood.schemas.parallel_reviews import ReviewDispatchAction, ReviewExecutionPlan
from heartwood.schemas.workflows import WorkflowRun
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
        workflow_evaluator: WorkflowEvaluator | None = None,
        worker_project_id: UUID | None = None,
        external_actions: ExternalActionAdapter | None = None,
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
        self._worker_control_lock = RLock()
        self._worker_control_active = False
        self._closed = False
        self._event_sink = event_sink or (lambda _events: None)
        self._token_sink = token_sink or (lambda _delta: None)
        self._known_source_event_ids: set[str] | None = None
        self._journal_writer_identity: str | None = None
        self._worker_record: WorkerAttemptRecord | None = None
        self._workflow_evaluator = workflow_evaluator
        self._external_actions = external_actions
        self._worker_project_id = worker_project_id
        self._worker_lease: WorkerAttemptLease | None = None
        self._worker_attempt_created = False
        self.backend.bind_runtime(
            event_sink=self._accept_unbound_backend_events,
            token_sink=self._accept_unbound_backend_token,
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
        workflow_evaluator: WorkflowEvaluator | None = None,
        worker_project_id: UUID | None = None,
        external_actions: ExternalActionAdapter | None = None,
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
            workflow_evaluator=workflow_evaluator,
            worker_project_id=worker_project_id,
            external_actions=external_actions,
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
            if self._closed:
                raise RuntimeError("Session service is closed")
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
            if command_kind not in {CommandKind.REPLAY.value, CommandKind.AUDIT_EXPORT.value}:
                unavailable_reason = unavailable_reason or self._worker_admission_error_locked()
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
                    incompatible = BackendErrorCode.SESSION_RUNTIME_INCOMPATIBLE.value
                    unavailable_reason = f"{command_kind} is unavailable because this session " + (
                        "requires its original agent runtime"
                        if fatal_error.payload.get("code") == incompatible
                        else "has an unknown execution outcome"
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
        if command_kind == CommandKind.WORKFLOW.value:
            events.extend(handle_workflow_command(self, command, self._workflow_evaluator))
        elif command_kind == CommandKind.CHAT.value:
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
            events.append(self._handle_audit_export(command))
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
            if self._closed:
                raise RuntimeError("Session service is closed")
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
        with self._control_worker():
            with self._command_lock:
                if self._closed:
                    return
            self.backend.close()
            with self._command_lock:
                if self._worker_lease is not None:
                    self._worker_lease.fence()
                self._closed = True
                self.store.release_writer()

    def propose_external_action(self, source_event_id: str) -> SessionEvent:
        """Prepare review of a retained worker proposal through a trusted service adapter."""
        with self._command_lock:
            self._require_worker_owner_locked()
            lease = self._worker_lease
            adapter = self._external_actions
            if (
                lease is None
                or adapter is None
                or self._worker_admission_error_locked() is not None
            ):
                raise ExternalActionError(
                    "outside operation requires an admitted worker and service"
                )
            events = self.replay_events()
            records = external_action_records(events)
            existing = next(
                (
                    item
                    for item in records.values()
                    if item.proposal.source_event_id == source_event_id
                ),
                None,
            )
            if existing is not None:
                raise ExternalActionError("worker proposal already has an outside operation")
            source = next((event for event in events if event.event_id == source_event_id), None)
            if (
                source is None
                or source.kind != EventKind.TOOL_CALL_PROPOSED
                or source.payload.get("worker_attempt_id") != str(lease.intent.attempt_id)
            ):
                raise ExternalActionError("outside operation requires a current worker proposal")
            tool_name, arguments = source.payload.get("tool_name"), source.payload.get("arguments")
            if (
                not isinstance(tool_name, str)
                or tool_name not in adapter.tool_names
                or not isinstance(arguments, dict)
            ):
                raise ExternalActionError("worker tool has no trusted outside service binding")
            policy = adapter.policy_sha256
            request = ExternalActionRequest.model_validate(adapter.prepare(tool_name, arguments))
            if request.tool_name != tool_name:
                raise ExternalActionError("service request changed the proposed tool")
            created_at = self._external_now()
            proposal = ExternalActionProposal(
                operation_id=uuid4(),
                worker=lease.intent,
                source_event_id=source_event_id,
                service_policy_sha256=policy,
                request=request,
                created_at=created_at,
                expires_at=created_at + timedelta(minutes=5),
            )
            self._check_external_authority(proposal)
            event = self._record_external_action(
                ExternalActionRecord(phase="proposed", proposal=proposal)
            )
        self._event_sink((event,))
        return event

    def _result_model_route(self) -> str:
        return canonical_request(
            json.dumps(
                {
                    "backend_id": self.backend.backend_id,
                    "model_id": self.backend.model_id,
                    "profile_id": self.backend.model_profile_id,
                    "endpoint": self.backend.model_endpoint,
                    "credential_reference": self.backend.credential_reference,
                    "capability_tier": self.backend.capability_tier,
                    "action_confirmation_mode": self.backend.action_confirmation_mode,
                }
            )
        )

    def propose_external_result(self, operation_id: UUID) -> SessionEvent:
        """Prepare separate review before outside content can enter worker/model context."""
        with self._command_lock:
            self._require_worker_owner_locked()
            adapter, lease = self._external_actions, self._worker_lease
            events = self.replay_events()
            outside = external_action_records(events).get(operation_id)
            if (
                adapter is None
                or lease is None
                or outside is None
                or not outside.terminal
                or outside.observation is None
                or outside.proposal.worker != lease.intent
                or any(
                    item.proposal.operation_id == operation_id
                    for item in external_result_records(events).values()
                )
            ):
                raise ExternalActionError("result release requires a unique current worker outcome")
            source = next(
                event
                for event in reversed(events)
                if event.kind == EventKind.EXTERNAL_ACTION_RECORDED
                and ExternalActionRecord.model_validate_json(json.dumps(event.payload["record"]))
                == outside
            )
            policy = adapter.policy_sha256
            route = self._result_model_route()
            created_at = self._external_now()
            proposal = ExternalResultProposal(
                release_id=uuid4(),
                operation_id=operation_id,
                operation_sha256=outside.proposal.sha256,
                observation_event_id=source.event_id,
                worker=lease.intent,
                service_policy_sha256=policy,
                model_route_json=route,
                content_json="{}",
                created_at=created_at,
                expires_at=created_at + timedelta(minutes=5),
            )
            self._check_result_authority(proposal)
            try:
                content = canonical_request(adapter.prepare_result(outside))
                proposal = ExternalResultProposal.model_validate(
                    proposal.model_copy(update={"content_json": content})
                )
            except Exception:
                raise ExternalActionError("outside result is unavailable or invalid") from None
            self._check_result_authority(proposal)
            event = self._record_external_result(
                ExternalResultRecord(phase="proposed", proposal=proposal)
            )
        self._event_sink((event,))
        return event

    def _check_result_authority(self, proposal: ExternalResultProposal) -> None:
        adapter, lease = self._external_actions, self._worker_lease
        policy = json.dumps(
            self.policy_profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        if (
            adapter is None
            or lease is None
            or not lease.active
            or lease.intent != proposal.worker
            or self._worker_project_id != proposal.worker.project_id
            or self._worker_admission_error_locked() is not None
            or hashlib.sha256(policy.encode()).hexdigest() != proposal.worker.gateway_policy_sha256
            or adapter.policy_sha256 != proposal.service_policy_sha256
            or self._result_model_route() != proposal.model_route_json
            or not proposal.created_at <= self._external_now() < proposal.expires_at
        ):
            raise ExternalActionError("result release authority or destination changed")

    def _record_external_result(self, record: ExternalResultRecord) -> SessionEvent:
        previous = external_result_records(self.replay_events()).get(record.proposal.release_id)
        validate_result_transition(previous, record)
        return self._record_event(
            EventKind.EXTERNAL_RESULT_RECORDED,
            {
                "record": cast(dict[str, JsonValue], record.model_dump(mode="json")),
                "worker_attempt_id": str(record.proposal.worker.attempt_id),
                "worker_project_id": str(record.proposal.worker.project_id),
            },
        )

    def _decide_external_result(
        self, command: SessionCommand, current: ExternalResultRecord
    ) -> tuple[SessionEvent, ...]:
        proposal, adapter = current.proposal, self._external_actions
        approved = _kind_value(command.kind) == CommandKind.APPROVE.value
        emitted: list[SessionEvent] = []
        try:
            if current.phase != "proposed":
                raise ExternalActionError("result release already has a decision")
            if approved:
                self._check_result_authority(proposal)
                if (
                    not self.backend.wait_for_idle(0)
                    or self.backend.pending_action_group(session_id=command.session_id) is not None
                ):
                    raise ExternalActionError("worker has unfinished execution or review")
                assert adapter is not None
                outside = external_action_records(self.replay_events())[proposal.operation_id]
                adapter.check_result_policy(
                    outside,
                    proposal.content_json,
                    proposal.model_route_json,
                    command.actor_id,
                    self._external_now(),
                )
                self._check_result_authority(proposal)
                authorized, authorization_events = self._authorize_backend(
                    command, purpose="release reviewed outside result to the worker model"
                )
                emitted.extend(authorization_events)
                if not authorized:
                    return tuple(emitted)
                self._check_result_authority(proposal)
            decision = ExternalActionDecision(
                command_id=command.command_id,
                actor_id=command.actor_id,
                decision="approved" if approved else "denied",
                decided_at=self._external_now(),
                proposal_sha256=proposal.sha256,
            )
            intent = ExternalResultRecord(
                phase="submitting" if approved else "denied", proposal=proposal, decision=decision
            )
        except Exception:
            return (
                *emitted,
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "reason": "outside result release validation failed",
                        "affects_lifecycle": False,
                    },
                ),
            )
        # Journal failure must prevent disclosure, not be reduced to a delivery error.
        emitted.append(self._record_external_result(intent))
        if not approved:
            return tuple(emitted)
        stream: tuple[BackendEvent, ...] = ()
        phase: Literal["unknown", "submitted"]
        try:
            self._check_result_authority(proposal)
            stream = self.backend.submit_context(
                session_id=command.session_id, content=proposal.message
            )
            phase = (
                "unknown"
                if any(isinstance(event, BackendErrorEvent) for event in stream)
                else "submitted"
            )
        except Exception:
            phase = "unknown"
        emitted.append(
            self._record_external_result(
                ExternalResultRecord(phase=phase, proposal=proposal, decision=decision)
            )
        )
        emitted.extend(self._translate_backend_events(stream))
        return tuple(emitted)

    def reconcile_external_actions(self) -> tuple[SessionEvent, ...]:
        """Query retained outside operation IDs without reviving a worker or submitting work."""
        with self._command_lock:
            if self._closed:
                raise ExternalActionError("session is closed")
            self.store.acquire_writer()
            self._recover_pending_commit_locked()
            adapter = self._external_actions
            if adapter is None:
                return ()
            emitted: list[SessionEvent] = []
            for record in external_action_records(self.replay_events()).values():
                if record.phase in {"proposed", "denied"} or record.terminal:
                    continue
                if record.proposal.worker.project_id != self._worker_project_id:
                    raise ExternalActionError("outside journal belongs to another project")
                try:
                    observed = ExternalActionRecord(
                        phase="observed",
                        proposal=record.proposal,
                        decision=record.decision,
                        observation=adapter.observe(record.proposal),
                    )
                    # Validate even an adapter returning an unchecked model_copy.
                    observed = ExternalActionRecord.model_validate_json(observed.model_dump_json())
                except Exception:
                    observed = ExternalActionRecord(
                        phase="unknown",
                        proposal=record.proposal,
                        decision=record.decision,
                    )
                if observed != record:
                    emitted.append(self._record_external_action(observed))
            self._recover_external_approvals_locked()
        if emitted:
            self._event_sink(tuple(emitted))
        return tuple(emitted)

    def _external_now(self) -> datetime:
        value = datetime.fromisoformat(self.clock().replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ExternalActionError("outside operation requires a timezone-aware clock")
        return value

    def _check_external_authority(self, proposal: ExternalActionProposal) -> None:
        lease, adapter = self._worker_lease, self._external_actions
        policy = json.dumps(
            self.policy_profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        if (
            lease is None
            or adapter is None
            or not lease.active
            or lease.intent != proposal.worker
            or self._worker_project_id != proposal.worker.project_id
            or self._worker_admission_error_locked() is not None
            or hashlib.sha256(policy.encode()).hexdigest() != proposal.worker.gateway_policy_sha256
            or adapter.policy_sha256 != proposal.service_policy_sha256
            or proposal.request.tool_name not in adapter.tool_names
            or not proposal.created_at <= self._external_now() < proposal.expires_at
        ):
            raise ExternalActionError("outside operation authority is stale or unavailable")

    def _record_external_action(self, record: ExternalActionRecord) -> SessionEvent:
        previous = external_action_records(self.replay_events()).get(record.proposal.operation_id)
        validate_external_transition(previous, record)
        return self._record_event(
            EventKind.EXTERNAL_ACTION_RECORDED,
            {
                "record": cast(dict[str, JsonValue], record.model_dump(mode="json")),
                "worker_attempt_id": str(record.proposal.worker.attempt_id),
                "worker_project_id": str(record.proposal.worker.project_id),
            },
        )

    def _decide_external_action(
        self, command: SessionCommand, current: ExternalActionRecord
    ) -> tuple[SessionEvent, ...]:
        proposal, adapter = current.proposal, self._external_actions
        approved = _kind_value(command.kind) == CommandKind.APPROVE.value
        if current.phase != "proposed":
            return (
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "reason": "outside operation already has a decision",
                        "affects_lifecycle": False,
                    },
                ),
            )
        inputs: list[bytes] = []
        try:
            if approved:
                self._check_external_authority(proposal)
                assert adapter is not None
                for reference in proposal.request.inputs:
                    content = adapter.read_input(reference)
                    if (
                        type(content) is not bytes
                        or len(content) != reference.size_bytes
                        or hashlib.sha256(content).hexdigest() != reference.sha256
                    ):
                        raise ExternalActionError("reviewed outside input changed")
                    inputs.append(content)
                adapter.check_policy(proposal, command.actor_id, self._external_now())
                self._check_external_authority(proposal)
            decision = ExternalActionDecision(
                command_id=command.command_id,
                actor_id=command.actor_id,
                decision="approved" if approved else "denied",
                decided_at=self._external_now(),
                proposal_sha256=proposal.sha256,
            )
            intent = ExternalActionRecord(
                phase="submitting" if approved else "denied", proposal=proposal, decision=decision
            )
        except Exception:
            return (
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "reason": "outside authorization or input validation failed",
                        "affects_lifecycle": False,
                    },
                ),
            )
        # Commit the decision before any service effect. Journal failures must propagate.
        emitted = [self._record_external_action(intent)]
        if not approved:
            return tuple(emitted)
        assert adapter is not None
        try:
            self._check_external_authority(proposal)
            observation = adapter.submit(proposal, decision, tuple(inputs))
            result = ExternalActionRecord(
                phase="observed", proposal=proposal, decision=decision, observation=observation
            )
            result = ExternalActionRecord.model_validate_json(result.model_dump_json())
        except Exception:
            result = ExternalActionRecord(phase="unknown", proposal=proposal, decision=decision)
        emitted.append(self._record_external_action(result))
        return tuple(emitted)

    def _reserved_external_event(self, event: BackendEvent) -> bool:
        if isinstance(
            event,
            (
                BackendToolCallEvent,
                BackendConfirmationRequestEvent,
                BackendConfirmationResolutionEvent,
            ),
        ):
            call = event.tool_call
            if (
                reserved_external_identity(call.tool_call_id)
                or reserved_external_identity(call.action_id)
                or call.tool_call_id.startswith(RESULT_PREFIX)
                or (call.action_id or "").startswith(RESULT_PREFIX)
            ):
                return True
            if isinstance(
                event, (BackendConfirmationRequestEvent, BackendConfirmationResolutionEvent)
            ):
                return any(
                    item.proposal.group_id == event.action_group_id
                    for item in external_action_records(self.replay_events()).values()
                ) or any(
                    item.proposal.group_id == event.action_group_id
                    for item in external_result_records(self.replay_events()).values()
                )
        elif isinstance(event, BackendToolExecutionEvent):
            return (
                reserved_external_identity(event.tool_execution.tool_call_id)
                or reserved_external_identity(event.tool_execution.action_id)
                or event.tool_execution.tool_call_id.startswith(RESULT_PREFIX)
                or (event.tool_execution.action_id or "").startswith(RESULT_PREFIX)
            )
        return False

    def worker_attempt(self) -> WorkerAttemptRecord | None:
        """Read retained worker identity without restoring execution authority."""
        with self._command_lock:
            return current_worker_attempt(self.replay_events())

    @contextmanager
    def _control_worker(self) -> Iterator[None]:
        with self._worker_control_lock:
            if self._worker_control_active:
                raise WorkerAttemptError("worker lifecycle operation is already in progress")
            self._worker_control_active = True
            try:
                yield
            finally:
                self._worker_control_active = False

    def launch_worker(
        self,
        launch: Callable[[WorkerAttemptIntent], WorkerRuntimeBinding],
        *,
        image_digest: str,
        sandbox_policy_sha256: str,
        input_manifest_sha256: str,
    ) -> WorkerAttemptLease:
        """Retain intent before one trusted launch; leave uncertain outcomes fenced."""
        with self._control_worker():
            intent = self.prepare_worker_attempt(
                image_digest=image_digest,
                sandbox_policy_sha256=sandbox_policy_sha256,
                input_manifest_sha256=input_manifest_sha256,
            )
            try:
                runtime = launch(intent)
                if runtime.generation is None:
                    raise WorkerAttemptError("worker process generation is unavailable")
                return self.bind_worker_runtime(attempt_id=intent.attempt_id, runtime=runtime)
            except BaseException as error:
                # A retained prepared intent also blocks replay if fencing cannot be written.
                with suppress(Exception):
                    self.fence_worker_attempt(attempt_id=intent.attempt_id)
                if not isinstance(error, Exception):
                    raise
                raise WorkerAttemptError("worker launch requires outside reconciliation") from None

    def revoke_worker(
        self, revoke: Callable[[WorkerAttemptRecord], WorkerRevocationEvidence]
    ) -> WorkerRevocationEvidence:
        """Fence before outside revocation and retain only matching observer evidence."""
        with self._control_worker():
            current = self.worker_attempt()
            if current is None:
                raise WorkerAttemptError("no worker attempt exists")
            if current.revocation is not None:
                return current.revocation
            self.fence_worker_attempt(attempt_id=current.intent.attempt_id)
            current = self.worker_attempt()
            assert current is not None
            try:
                evidence = revoke(current)
                self.record_worker_revocation(evidence)
            except Exception:
                raise WorkerAttemptError(
                    "worker revocation requires outside reconciliation"
                ) from None
            return evidence

    def prepare_worker_attempt(
        self, *, image_digest: str, sandbox_policy_sha256: str, input_manifest_sha256: str
    ) -> WorkerAttemptIntent:
        """Retain a fresh gateway-issued attempt before trusted sandbox launch."""
        with self._command_lock:
            self._require_worker_owner_locked()
            if self._worker_attempt_created or not self.backend.wait_for_idle(0):
                raise WorkerAttemptError("a fresh idle backend is required for a worker attempt")
            previous = current_worker_attempt(self.replay_events())
            if previous is not None and previous.phase != "revoked":
                raise WorkerAttemptError("previous worker access has not been revoked")
            if previous is None and any(
                event.kind
                in {
                    EventKind.USER_MESSAGE_RECORDED,
                    EventKind.TOOL_CALL_PROPOSED,
                    EventKind.MODEL_CALL_DECISION_RECORDED,
                }
                for event in self.replay_events()
            ):
                raise WorkerAttemptError("local execution history requires explicit migration")
            assert self._worker_project_id is not None
            policy = json.dumps(
                self.policy_profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
            intent = WorkerAttemptIntent(
                project_id=self._worker_project_id,
                session_id=self.store.session_id,
                attempt_id=uuid4(),
                conversation_id=previous.intent.conversation_id if previous else uuid4(),
                image_digest=image_digest,
                gateway_policy_sha256=hashlib.sha256(policy).hexdigest(),
                sandbox_policy_sha256=sandbox_policy_sha256,
                input_manifest_sha256=input_manifest_sha256,
            )
            self._worker_attempt_created = True
            self._record_worker_transition_locked(
                WorkerAttemptRecord(phase="prepared", intent=intent)
            )
            return intent

    def bind_worker_runtime(
        self, *, attempt_id: UUID, runtime: WorkerRuntimeBinding
    ) -> WorkerAttemptLease:
        """Activate only this service's retained intent and observed runtime."""
        with self._command_lock:
            self._require_worker_owner_locked()
            current = self._require_worker_attempt_locked(attempt_id)
            if not self._worker_attempt_created or self._worker_lease is not None:
                raise WorkerAttemptError("replayed worker intent requires launcher reconciliation")
            record = WorkerAttemptRecord(phase="active", intent=current.intent, runtime=runtime)
            self._record_worker_transition_locked(record)
            writer_identity = self.store.writer_identity
            lease = WorkerAttemptLease(
                record,
                owns_writer=lambda: (
                    not self._closed and self.store.writer_identity == writer_identity
                ),
            )
            self._worker_lease = lease
            try:
                self.backend.bind_runtime(
                    event_sink=lambda events: self._accept_worker_events(lease, events),
                    token_sink=lambda delta: self._accept_worker_token(lease, delta),
                )
            except Exception:
                lease.fence()
                raise
            return lease

    def fence_worker_attempt(self, *, attempt_id: UUID) -> None:
        """Close local admission before recording the required outside revocation."""
        with self._command_lock:
            self._require_worker_owner_locked()
            current = self._require_worker_attempt_locked(attempt_id)
            if current.phase == "fenced":
                return
            if self._worker_lease is not None:
                self._worker_lease.fence()
            self._record_worker_transition_locked(
                WorkerAttemptRecord(phase="fenced", intent=current.intent, runtime=current.runtime)
            )

    def record_worker_revocation(self, evidence: WorkerRevocationEvidence) -> None:
        """Retain trusted launcher evidence; this method performs no infrastructure mutation."""
        with self._command_lock:
            self._require_worker_owner_locked()
            current = self._require_worker_attempt_locked(evidence.attempt_id)
            record = WorkerAttemptRecord(
                phase="revoked", intent=current.intent, runtime=current.runtime, revocation=evidence
            )
            if record != current:
                self._record_worker_transition_locked(record)

    def _require_worker_owner_locked(self) -> None:
        if self._closed or self._worker_project_id is None:
            raise WorkerAttemptError("worker lifecycle requires an owned deployment project")
        self.store.acquire_writer()
        self._recover_pending_commit_locked()
        current = current_worker_attempt(self.replay_events())
        if current is not None and current.intent.project_id != self._worker_project_id:
            raise WorkerAttemptError("worker journal belongs to another project")

    def _require_worker_attempt_locked(self, attempt_id: UUID) -> WorkerAttemptRecord:
        current = current_worker_attempt(self.replay_events())
        if current is None or current.intent.attempt_id != attempt_id:
            raise WorkerAttemptError("worker attempt is not current")
        return current

    def _record_worker_transition_locked(self, record: WorkerAttemptRecord) -> None:
        validate_worker_transition(current_worker_attempt(self.replay_events()), record)
        self._record_event(
            EventKind.WORKER_ATTEMPT_RECORDED,
            cast(dict[str, JsonValue], record.model_dump(mode="json")),
        )

    def _worker_admission_error_locked(self) -> str | None:
        self._known_source_event_ids_locked()
        current = self._worker_record
        if current is None:
            return None
        lease = self._worker_lease
        if (
            current.phase != "active"
            or lease is None
            or not lease.active
            or lease.intent != current.intent
            or self._worker_project_id != current.intent.project_id
        ):
            return "worker attempt requires trusted launcher reconciliation"
        return None

    def _accept_worker_events(
        self, lease: WorkerAttemptLease, events: tuple[BackendEvent, ...]
    ) -> None:
        with self._command_lock:
            if lease is self._worker_lease and lease.active:
                self._accept_backend_events(events)

    def _accept_unbound_backend_events(self, events: tuple[BackendEvent, ...]) -> None:
        with self._command_lock:
            if not self._worker_attempt_created:
                self._accept_backend_events(events)

    def _accept_unbound_backend_token(self, delta: str) -> None:
        if (
            not self._closed
            and not self._worker_attempt_created
            and self._known_source_event_ids is not None
            and self._worker_record is None
            and self.store.writer_identity is not None
            and self.store.writer_identity == self._journal_writer_identity
        ):
            self._token_sink(delta)

    def _accept_worker_token(self, lease: WorkerAttemptLease, delta: str) -> None:
        if lease is not self._worker_lease:
            return
        try:
            with lease.admit_callback():
                self._token_sink(delta)
        except WorkerAttemptError:
            return

    def admit_parallel_review(
        self, actions: tuple[ReviewDispatchAction, ...], *, cancelled: Callable[[], bool]
    ) -> ReviewExecutionPlan | None:
        """Recheck a consented review without holding session locks during qualification I/O."""
        with self._command_lock:
            self._require_review_owner(cancelled)
            self._recover_pending_commit_locked()
            current = workflow_run(self.replay_events())
            review = current.research_review if current is not None else None
            if review is None or review.parallel_plan is None:
                return None
            if review.status != "pending" or review.parallel_dispatch:
                raise ValueError("Parallel review was already admitted or has ended")
            evaluator = self._workflow_evaluator
            if evaluator is None or current is None:
                raise ValueError("Parallel review evidence is unavailable")
            plan = review.parallel_plan
            prepared = WorkflowRun.model_validate(
                {
                    **current.model_dump(),
                    "revision": plan.scope.revision,
                    "research_review": None,
                    "parallel_review_plan": plan,
                }
            )
        now = datetime.fromisoformat(self.clock())
        snapshot = evaluator.prepare_review(current.binding, current.stage_id)
        refreshed = validate_parallel_review_plan(
            evaluator.prepare_parallel_review(prepared, session_id=self.store.session_id, now=now),
            prepared,
            snapshot,
            session_id=self.store.session_id,
            now=now,
        )
        if refreshed.fingerprint != plan.fingerprint:
            raise ValueError("Parallel review qualification changed before dispatch")
        if evaluator.prepare_review(current.binding, current.stage_id) != snapshot:
            raise ValueError("Parallel review files changed during qualification")
        with self._command_lock:
            self._require_review_owner(cancelled)
            if workflow_run(
                self.replay_events()
            ) != current or refreshed.valid_until <= datetime.fromisoformat(self.clock()):
                raise ValueError("Parallel review changed or was cancelled before dispatch")
            if reason := workflow_admission_reason(self, evaluator, None):
                raise ValueError(reason)
            event = record_parallel_review_admission(self, current, actions)
        self._event_sink((event,))
        return refreshed

    def _require_review_owner(self, cancelled: Callable[[], bool]) -> None:
        if cancelled() or not self.store.owns_writer:
            raise ValueError("Parallel review no longer owns an active session")

    def wait_for_idle(self, timeout: float = 0) -> bool:
        """Wait for final callbacks without taking the session command lock.

        This is an observation, not an ownership lease or permission to advance.
        A pending approval can be idle; lifecycle and evidence still decide next steps.
        """
        if not math.isfinite(timeout) or not 0 <= timeout <= 30:
            raise ValueError("Session idle timeout must be between zero and 30 seconds")
        return self.backend.wait_for_idle(timeout)

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
        if reason := workflow_admission_reason(self, self._workflow_evaluator, command):
            return False, [
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "command": _kind_value(command.kind),
                        "reason": reason,
                        "affects_lifecycle": False,
                    },
                )
            ]
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
        release = next(
            (
                record
                for record in external_result_records(self.replay_events()).values()
                if record.proposal.group_id == action_group_id
            ),
            None,
        )
        if release is not None:
            return self._decide_external_result(command, release)
        outside = next(
            (
                record
                for record in external_action_records(self.replay_events()).values()
                if record.proposal.group_id == action_group_id
            ),
            None,
        )
        if outside is not None:
            return self._decide_external_action(command, outside)
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
        if not self._pending_proposal_is_durable(pending_group):
            return (
                self._record_event(
                    EventKind.ERROR_RECORDED,
                    {
                        "command": _kind_value(command.kind),
                        "reason": "the complete action proposal is not yet durably published",
                        "affects_lifecycle": False,
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
        if approved and self._workflow_evaluator is not None:
            current = workflow_run(self.replay_events())
            if current is not None:
                recorded = reproduction_records(
                    self.replay_events(), run_id=current.run_id, stage_id=current.stage_id
                )
                if not any(record.group_id == pending_group.group_id for _, record in recorded):
                    observation = prepare_reproduction(
                        current,
                        session_id=command.session_id,
                        group=pending_group,
                        inspector=self._workflow_evaluator,
                    )
                    if observation is not None:
                        events.append(self._record_reproduction(observation))
        events.append(
            self._record_event(
                EventKind.APPROVAL_RECORDED,
                {
                    "command_id": command.command_id,
                    "actor_id": command.actor_id,
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

    def _pending_proposal_is_durable(self, group: PendingActionGroup) -> bool:
        fields = ("tool_call_id", "action_id", "tool_name", "kind", "arguments")
        expected = {
            action.tool_call_id: {field: getattr(action, field) for field in fields}
            for action in group.actions
        }
        proposals: dict[str, dict[str, JsonValue]] = {}
        requests: dict[str, dict[str, JsonValue]] = {}
        for event in self.replay_events():
            payload = event.payload
            if event.kind == EventKind.TOOL_CALL_PROPOSED:
                target = proposals
            elif event.kind == EventKind.CONFIRMATION_REQUESTED:
                request = payload.get("request")
                if not isinstance(request, dict) or request.get("group_id") != group.group_id:
                    continue
                payload = request
                target = requests
            else:
                continue
            identity = payload.get("tool_call_id")
            if isinstance(identity, str) and identity in expected:
                target[identity] = {field: payload.get(field) for field in fields}
        return proposals == expected and requests == expected

    def _translate_backend_events(
        self, stream: tuple[BackendEvent, ...], *, live: bool = True
    ) -> list[SessionEvent]:
        translated: list[SessionEvent] = []
        execution_settled = False
        known_source_event_ids = self._known_source_event_ids_locked()
        for event in stream:
            if isinstance(event, BackendExecutionSettledEvent):
                execution_settled = live
                continue
            if (
                event.source_event_id is not None
                and event.source_event_id in known_source_event_ids
            ):
                continue
            if self._reserved_external_event(event):
                translated.append(
                    self._record_event(
                        EventKind.ERROR_RECORDED,
                        {
                            "reason": "worker event selected an outside operation identity",
                            "affects_lifecycle": False,
                        },
                    )
                )
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
                        {
                            "content": event.message,
                            **(
                                {"outcome_status": event.outcome_status}
                                if event.outcome_status
                                else {}
                            ),
                            **source_payload,
                        },
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
                            "specialist_label": tool_call.specialist_label,
                            "specialist_capability": tool_call.specialist_capability,
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
                            "working_directory": execution.working_directory,
                            **source_payload,
                        },
                    )
                )
                if live and self._workflow_evaluator is not None:
                    current = workflow_run(self.replay_events())
                    if current is not None:
                        observation = observe_reproduction(
                            current,
                            events=self.replay_events(),
                            execution=translated[-1],
                            inspector=self._workflow_evaluator,
                        )
                        if observation is not None:
                            translated.append(self._record_reproduction(observation))
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
                                "role_label": subagent.role_label,
                                "status": subagent.status.value,
                                "parent_session_id": subagent.parent_session_id,
                                "parent_action_id": subagent.parent_action_id,
                                **(
                                    {
                                        "native_execution": subagent.native_execution.model_dump(
                                            mode="json"
                                        )
                                    }
                                    if subagent.native_execution is not None
                                    else {}
                                ),
                                **(
                                    {
                                        "review_proposals": subagent.review_proposals.model_dump(
                                            mode="json"
                                        )
                                    }
                                    if subagent.review_proposals is not None
                                    else {}
                                ),
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
        translated.extend(
            settle_workflow_review(
                self, self._workflow_evaluator, execution_settled=execution_settled
            )
        )
        translated.extend(
            settle_workflow_correction(
                self,
                self._workflow_evaluator,
                execution_settled=execution_settled,
                live=live,
            )
        )
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
            if self._closed or (self._worker_lease is not None and not self._worker_lease.active):
                return
            self.store.acquire_writer()
            self._recover_pending_commit_locked()
            if self._worker_admission_error_locked() is not None:
                return
            events = tuple(self._translate_backend_events(stream))
        if events:
            self._event_sink(events)

    def _reconcile_locked(self) -> tuple[SessionEvent, ...]:
        if self._worker_admission_error_locked() is not None:
            return ()
        known_source_event_ids = self._known_source_event_ids_locked()
        return tuple(
            self._translate_backend_events(
                self.backend.reconcile(
                    session_id=self.store.session_id,
                    known_source_event_ids=frozenset(known_source_event_ids),
                ),
                live=False,
            )
        )

    def _known_source_event_ids_locked(self) -> set[str]:
        if (
            self._known_source_event_ids is None
            or self._journal_writer_identity != self.store.writer_identity
        ):
            events = self.replay_events()
            self._known_source_event_ids = set(_source_event_ids(events))
            self._worker_record = current_worker_attempt(events)
            self._journal_writer_identity = self.store.writer_identity
        return self._known_source_event_ids

    def _recover_pending_commit_locked(self) -> None:
        if self.store.recover_pending_commit():
            self._known_source_event_ids = None
            self._known_source_event_ids_locked()

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

    def _recover_external_approvals_locked(self) -> None:
        """Complete retained outside decisions without dispatching any backend operation."""
        unresolved = self.store.unresolved_command_ids()
        if not unresolved:
            return
        events = self.replay_events()
        outside: list[ExternalActionRecord | ExternalResultRecord] = [
            *external_action_records(events).values(),
            *external_result_records(events).values(),
        ]
        for command_id in unresolved:
            decided = next(
                (
                    item
                    for item in outside
                    if item.decision is not None and item.decision.command_id == command_id
                ),
                None,
            )
            if decided is None:
                continue
            receipt = self.store.command_record(command_id)
            if receipt is None:
                raise SessionRecoveryError("outside decision has no command receipt")
            first, digest = receipt.get("first_sequence"), receipt.get("command_hash")
            if not isinstance(first, int) or not isinstance(digest, str):
                raise SessionRecoveryError("outside approval receipt is invalid")
            self.store.complete_command(
                command_id=command_id,
                command_hash=digest,
                first_sequence=first,
                last_sequence=self.replay_events()[-1].sequence,
            )

    def _recover_approval_commands_locked(self) -> tuple[SessionEvent, ...]:
        """Finish approvals whose retained state makes recovery unambiguous."""
        self._recover_external_approvals_locked()
        recovered: list[SessionEvent] = []
        if self._worker_admission_error_locked() is not None:
            return tuple(recovered)
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

    def _handle_audit_export(self, command: SessionCommand) -> SessionEvent:
        binding = command.payload.get("experiment_export")
        metadata: dict[str, JsonValue] = {}
        if binding is not None:
            parsed = ExperimentExportBinding.model_validate(binding)
            metadata["experiment_export"] = cast(
                dict[str, JsonValue], parsed.model_dump(mode="json")
            )
        event = self._record_event(
            EventKind.AUDIT_EXPORT_RECORDED,
            {
                "path": str(self.store.audit_export_path),
                "event_count": self.store.next_sequence() + 1,
                "scrubbed": True,
                **metadata,
            },
        )
        content, _verification = self.store.verified_audit_export()
        self.store.write_audit_export(content)
        return event

    def _record_reproduction(self, observation: JournaledReproduction) -> SessionEvent:
        payload = cast(dict[str, JsonValue], observation.model_dump(mode="json"))
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return self._record_event(
            EventKind.WORKFLOW_EXECUTION_RECORDED,
            {
                "observation": payload,
                "run_id": observation.witness.run_id,
                "stage_id": observation.witness.stage_id,
                "tool_call_id": observation.witness.tool_call_id,
                "group_id": observation.group_id,
                "status": observation.witness.status,
                "observation_fingerprint": fingerprint,
                "preparation_event_id": observation.preparation_event_id,
                "execution_event_id": observation.execution_event_id,
            },
        )

    def _record_event(self, kind: EventKind, payload: dict[str, JsonValue]) -> SessionEvent:
        correlation: dict[str, JsonValue] = {}
        if self._worker_lease is not None and kind not in {
            EventKind.WORKER_ATTEMPT_RECORDED,
            EventKind.EXTERNAL_ACTION_RECORDED,
            EventKind.EXTERNAL_RESULT_RECORDED,
        }:
            correlation = {
                "worker_attempt_id": str(self._worker_lease.intent.attempt_id),
                "worker_project_id": str(self._worker_lease.intent.project_id),
            }
            payload = {**payload, **correlation}
        sequence, previous_event_hash = self.store.verified_head()
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
            **correlation,
            "session_event_hash": compute_session_event_hash(event),
        }
        audit_event = prepare_audit_event(
            session_id=self.store.session_id,
            sequence=sequence,
            previous_event_hash=previous_event_hash,
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
        if kind == EventKind.WORKER_ATTEMPT_RECORDED:
            self._worker_record = WorkerAttemptRecord.model_validate(payload)
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
    if kind == EventKind.EXTERNAL_RESULT_RECORDED:
        release = ExternalResultRecord.model_validate_json(json.dumps(payload["record"]))
        return {
            "release_id": str(release.proposal.release_id),
            "operation_id": str(release.proposal.operation_id),
            "proposal_sha256": release.proposal.sha256,
            "phase": release.phase,
            "actor_id": release.decision.actor_id if release.decision else None,
            "command_id": release.decision.command_id if release.decision else None,
        }
    if kind == EventKind.EXTERNAL_ACTION_RECORDED:
        outside = ExternalActionRecord.model_validate_json(json.dumps(payload["record"]))
        return {
            "operation_id": str(outside.proposal.operation_id),
            "proposal_sha256": outside.proposal.sha256,
            "phase": outside.phase,
            "actor_id": outside.decision.actor_id if outside.decision else None,
            "command_id": outside.decision.command_id if outside.decision else None,
            "status": outside.observation.operation.status if outside.observation else None,
        }
    if kind == EventKind.WORKER_ATTEMPT_RECORDED:
        record = WorkerAttemptRecord.model_validate(payload)
        return {
            "phase": record.phase,
            "project_id": str(record.intent.project_id),
            "attempt_id": str(record.intent.attempt_id),
            "conversation_id": str(record.intent.conversation_id),
            "binding_sha256": hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    if kind == EventKind.WORKFLOW_EXECUTION_RECORDED:
        return _selected_audit_fields(
            payload,
            "run_id",
            "stage_id",
            "tool_call_id",
            "group_id",
            "status",
            "observation_fingerprint",
            "preparation_event_id",
            "execution_event_id",
        )
    if kind == EventKind.WORKFLOW_UPDATED:
        return _selected_audit_fields(
            payload,
            "command_id",
            "actor_id",
            "run_id",
            "stage_id",
            "phase",
            "revision",
            "transition",
            "approved",
            "workflow_fingerprint",
            "evidence_fingerprint",
            "assessed_stage_id",
            "experiment_fingerprint",
            "research_review_fingerprint",
            "research_correction_fingerprint",
        )
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
                    "role_label",
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
        return _selected_audit_fields(payload, "event_count", "scrubbed", "experiment_export")
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
