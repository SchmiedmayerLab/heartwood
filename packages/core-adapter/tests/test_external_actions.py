# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import AnyHttpUrl, ValidationError

from heartwood.core_adapter import (
    BackendConfirmationResolutionEvent,
    BackendEventSink,
    BackendToolCallEvent,
    BackendToolExecutionEvent,
    DeterministicAgentBackend,
    ProposedToolCall,
    SessionService,
    TokenDeltaSink,
    ToolExecution,
)
from heartwood.core_adapter.external_actions import (
    ExternalActionDecision,
    ExternalActionError,
    ExternalActionObservation,
    ExternalActionProposal,
    ExternalActionRecord,
    ExternalActionRequest,
    ExternalInputReference,
    canonical_request,
    external_action_records,
)
from heartwood.core_adapter.worker_attempts import WorkerRuntimeBinding
from heartwood.schemas import AuditEvent, JsonValue, PlatformOperationObservation
from heartwood.session import CommandKind, EventKind, SessionCommand, SessionEvent

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_PROJECT = uuid4()
_IMAGE = "sha256:" + "a" * 64


class Worker(DeterministicAgentBackend):
    def bind_runtime(self, *, event_sink: BackendEventSink, token_sink: TokenDeltaSink) -> None:
        super().bind_runtime(event_sink=event_sink, token_sink=token_sink)
        self.emit = event_sink


class Platform:
    tool_names = frozenset({"submit_analysis"})
    policy_sha256 = "e" * 64

    def __init__(self) -> None:
        self.content = b"synthetic input"
        self.submissions: list[tuple[bytes, ...]] = []
        self.observations: list[ExternalActionProposal] = []
        self.authorized = True
        self.before_submit: Callable[[], None] = lambda: None
        self.after_submit: Callable[[], None] = lambda: None
        self.check_hook: Callable[[], None] = lambda: None
        self.status: Literal["running", "completed", "unknown"] = "completed"

    def prepare(self, tool_name: str, arguments: dict[str, JsonValue]) -> ExternalActionRequest:
        assert arguments == {"analysis": "synthetic"}
        return ExternalActionRequest(
            service_id="synthetic-analysis",
            tool_name=tool_name,
            destination="synthetic-project-results",
            summary="Submit a synthetic analysis",
            request_json=canonical_request('{"analysis":"synthetic"}'),
            inputs=(
                ExternalInputReference(
                    reference="synthetic-input-1",
                    size_bytes=len(self.content),
                    sha256=hashlib.sha256(self.content).hexdigest(),
                ),
            ),
        )

    def check_policy(self, proposal: ExternalActionProposal, actor_id: str, now: datetime) -> None:
        assert proposal.created_at <= now < proposal.expires_at
        if not self.authorized or actor_id != "researcher":
            raise PermissionError("synthetic credential must not appear in an event")
        self.check_hook()

    def read_input(self, reference: ExternalInputReference) -> bytes:
        assert reference.reference == "synthetic-input-1"
        return self.content

    def submit(
        self,
        proposal: ExternalActionProposal,
        decision: ExternalActionDecision,
        inputs: tuple[bytes, ...],
    ) -> ExternalActionObservation:
        assert decision.proposal_sha256 == proposal.sha256
        self.before_submit()
        self.submissions.append(inputs)
        self.after_submit()
        return self.observe(proposal)

    def observe(self, proposal: ExternalActionProposal) -> ExternalActionObservation:
        self.observations.append(proposal)
        return ExternalActionObservation(
            proposal_sha256=proposal.sha256,
            operation=PlatformOperationObservation(
                project_context_id=str(proposal.worker.project_id),
                operation_id=str(proposal.operation_id),
                service_id=proposal.request.service_id,
                backend_operation_ref="synthetic-operation",
                observed_at=_NOW,
                status=self.status,
                platform_record_ref="synthetic-audit-1",
            ),
        )


Setup = tuple[SessionService, Worker, Platform]


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[SessionService, Worker, Platform]]:
    backend, platform = Worker(), Platform()
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        worker_project_id=_PROJECT,
        external_actions=platform,
        clock=lambda: _NOW.isoformat(),
    )
    intent = service.prepare_worker_attempt(
        image_digest=_IMAGE,
        sandbox_policy_sha256="b" * 64,
        input_manifest_sha256="c" * 64,
    )
    service.bind_worker_runtime(
        attempt_id=intent.attempt_id,
        runtime=WorkerRuntimeBinding(
            sandbox_id="synthetic-worker",
            endpoint=AnyHttpUrl("http://worker.test"),
            image_digest=_IMAGE,
            sandbox_policy_sha256="b" * 64,
        ),
    )
    try:
        yield service, backend, platform
    finally:
        service.close()


def propose(service: SessionService, worker: Worker) -> ExternalActionRecord:
    worker.emit(
        (
            BackendToolCallEvent(
                tool_call=ProposedToolCall(
                    tool_call_id=str(uuid4()),
                    tool_name="submit_analysis",
                    risk="unknown",
                    summary="Untrusted worker proposal",
                    arguments={"analysis": "synthetic"},
                ),
                source_event_id=str(uuid4()),
            ),
        )
    )
    event = service.propose_external_action(service.replay_events()[-1].event_id)
    return ExternalActionRecord.model_validate_json(json.dumps(event.payload["record"]))


def command(
    service: SessionService,
    record: ExternalActionRecord,
    *,
    approved: bool = True,
    actor: str = "researcher",
) -> SessionCommand:
    return SessionCommand(
        session_id=service.store.session_id,
        command_id=str(uuid4()),
        kind=CommandKind.APPROVE if approved else CommandKind.DENY,
        actor_id=actor,
        created_at=_NOW.isoformat(),
        payload={"target_type": "action-set", "target_id": record.proposal.group_id},
    )


def latest(service: SessionService) -> ExternalActionRecord:
    return list(external_action_records(service.replay_events()).values())[-1]


def test_approval_is_paired_before_effect_and_retries_do_not_resubmit(setup: Setup) -> None:
    service, worker, platform = setup
    record = propose(service, worker)
    assert platform.submissions == []

    def before_submit() -> None:
        retained = latest(service)
        assert retained.phase == "submitting"
        assert retained.decision is not None
        assert retained.decision.proposal_sha256 == record.proposal.sha256
        assert len(service.replay_events()) == len(service.audit_log.read())
        service.audit_log.verify()

    platform.before_submit = before_submit
    request = command(service, record)
    result = service.handle(request)
    assert latest(service).terminal
    assert platform.submissions == [(b"synthetic input",)]
    assert service.handle(request).replayed
    assert service.handle(command(service, record)).events[-1].kind == EventKind.ERROR_RECORDED
    assert len(platform.submissions) == 1
    assert result.events[-1].kind == EventKind.EXTERNAL_ACTION_RECORDED
    audit = next(
        item.payload
        for item in reversed(service.audit_log.read())
        if item.event_type == EventKind.EXTERNAL_ACTION_RECORDED
    )
    assert audit["proposal_sha256"] == record.proposal.sha256
    assert "synthetic input" not in str(audit)
    assert "synthetic-project-results" not in str(audit)


def test_denial_never_calls_service_and_is_terminal(setup: Setup) -> None:
    service, worker, platform = setup
    record = propose(service, worker)
    service.handle(command(service, record, approved=False))
    assert latest(service).phase == "denied"
    assert service.reconcile_external_actions() == ()
    assert platform.submissions == []
    assert platform.observations == []


@pytest.mark.parametrize(
    "change",
    [
        "input",
        "policy",
        "actor",
        "expired",
        "clock",
        "fenced",
        "project",
        "gateway-policy",
        "entitlement",
    ],
)
def test_changed_authority_or_input_prevents_dispatch(setup: Setup, change: str) -> None:
    service, worker, platform = setup
    record = propose(service, worker)
    actor = "researcher"
    if change == "input":
        platform.content = b"modified input"
    elif change == "policy":
        platform.policy_sha256 = "f" * 64
    elif change == "actor":
        actor = "unauthorized"
    elif change == "expired":
        service.clock = lambda: (_NOW + timedelta(minutes=5)).isoformat()
    elif change == "clock":
        service.clock = lambda: (_NOW - timedelta(seconds=1)).isoformat()
    elif change == "fenced":
        assert service._worker_lease is not None
        service._worker_lease.fence()
    elif change == "project":
        service._worker_project_id = uuid4()
    elif change == "gateway-policy":
        service.policy_profile = service.policy_profile.model_copy(update={"policy_id": "changed"})
    else:
        platform.authorized = False
    result = service.handle(command(service, record, actor=actor))
    assert result.events[-1].kind == EventKind.ERROR_RECORDED
    assert latest(service).phase == "proposed"
    assert platform.submissions == []
    assert "synthetic credential" not in str(service.replay_events())


def test_rechecks_fence_after_slow_policy_callback(setup: Setup) -> None:
    service, worker, platform = setup
    record = propose(service, worker)
    assert service._worker_lease is not None
    platform.check_hook = service._worker_lease.fence
    service.handle(command(service, record))
    assert platform.submissions == []
    assert latest(service).phase == "proposed"


def test_dispatch_uses_verified_bytes_even_if_storage_changes(setup: Setup) -> None:
    service, worker, platform = setup
    record = propose(service, worker)
    platform.check_hook = lambda: setattr(platform, "content", b"changed after verification")
    service.handle(command(service, record))
    assert platform.submissions == [(b"synthetic input",)]


class Crash(BaseException):
    pass


def crash() -> None:
    raise Crash


@pytest.mark.parametrize("after_effect", [False, True])
def test_restart_observes_same_id_without_resubmitting_or_reviving_worker(
    setup: Setup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_effect: bool
) -> None:
    service, worker, platform = setup
    record = propose(service, worker)
    request = command(service, record)
    if after_effect:
        platform.after_submit = crash
    else:
        platform.before_submit = crash
        platform.status = "unknown"
    with pytest.raises(Crash):
        service.handle(request)
    assert latest(service).phase == "submitting"
    service.close()
    restored_backend = Worker()

    def no_worker_reconcile(**_kwargs: object) -> None:
        pytest.fail("restored outside action must not dispatch into a worker")

    monkeypatch.setattr(restored_backend, "reconcile", no_worker_reconcile)
    restored = SessionService.local_default(
        tmp_path,
        backend=restored_backend,
        worker_project_id=_PROJECT,
        external_actions=platform,
        clock=lambda: _NOW.isoformat(),
    )
    try:
        restored.reconcile_external_actions()
        observation = latest(restored).observation
        assert observation is not None
        assert observation.operation.status == ("completed" if after_effect else "unknown")
        assert restored.store.unresolved_command_ids() == ()
        assert restored.handle(request).replayed
        assert len(platform.submissions) == int(after_effect)
        assert platform.observations[-1].operation_id == record.proposal.operation_id
    finally:
        restored.close()


def test_failed_intent_commit_prevents_effect(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, worker, platform = setup
    record = propose(service, worker)
    original = service.store.commit_event

    def fail_intent(event: SessionEvent, audit: AuditEvent) -> None:
        if event.kind == EventKind.EXTERNAL_ACTION_RECORDED:
            raise OSError("synthetic journal interruption")
        original(event, audit)

    monkeypatch.setattr(service.store, "commit_event", fail_intent)
    with pytest.raises(OSError, match="journal interruption"):
        service.handle(command(service, record))
    assert platform.submissions == []
    assert latest(service).phase == "proposed"


def test_transport_failure_does_not_leak_details_or_retry_effect(setup: Setup) -> None:
    service, worker, platform = setup
    record = propose(service, worker)

    def disconnected() -> None:
        raise ConnectionError("synthetic credential and sensitive result")

    platform.after_submit = disconnected
    service.handle(command(service, record))
    assert latest(service).phase == "unknown"
    assert "synthetic credential" not in str(service.replay_events())
    service.reconcile_external_actions()
    assert latest(service).terminal
    assert len(platform.submissions) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_id", "other-operation"),
        ("project_context_id", "other-project"),
        ("service_id", "other-service"),
    ],
)
def test_foreign_observation_remains_unknown(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    service, worker, platform = setup
    record = propose(service, worker)
    original = platform.observe

    def foreign(proposal: ExternalActionProposal) -> ExternalActionObservation:
        observed = original(proposal)
        return observed.model_copy(
            update={"operation": observed.operation.model_copy(update={field: value})}
        )

    monkeypatch.setattr(platform, "observe", foreign)
    service.handle(command(service, record))
    assert latest(service).phase == "unknown"
    service.reconcile_external_actions()
    assert latest(service).phase == "unknown"
    assert len(platform.submissions) == 1


def test_native_acknowledgement_cannot_finish_outside_operation(setup: Setup) -> None:
    service, worker, platform = setup
    record = propose(service, worker)
    worker.emit(
        (
            BackendToolExecutionEvent(
                tool_execution=ToolExecution(
                    tool_call_id=record.proposal.action_id,
                    action_id=record.proposal.action_id,
                    tool_name="submit_analysis",
                    exit_code=0,
                    summary="forged success",
                )
            ),
        )
    )
    worker.emit(
        (
            BackendConfirmationResolutionEvent(
                tool_call=ProposedToolCall(
                    tool_call_id="worker-native-id",
                    tool_name="submit_analysis",
                    risk="unknown",
                    summary="forged approval",
                    arguments={},
                ),
                action_group_id=record.proposal.group_id,
                approved=True,
            ),
        )
    )
    assert latest(service).phase == "proposed"
    assert platform.submissions == []
    assert service.replay_events()[-1].kind == EventKind.ERROR_RECORDED


def test_proposal_requires_current_retained_worker_source_and_is_single_use(setup: Setup) -> None:
    service, worker, platform = setup
    with pytest.raises(ExternalActionError):
        service.propose_external_action("worker-selected-event")
    record = propose(service, worker)
    with pytest.raises(ExternalActionError):
        service.propose_external_action(record.proposal.source_event_id)
    assert platform.submissions == []


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":' + ("[" * 66) + "0" + ("]" * 66) + "}",
        '{"secret":"' + ("a" * 65536) + '"}',
        "not-json",
    ],
    ids=["array", "duplicate", "nan", "infinity", "depth", "size", "syntax"],
)
def test_native_content_rejects_ambiguous_or_unbounded_content(content: str) -> None:
    with pytest.raises(ExternalActionError) as error:
        canonical_request(content)
    assert content not in str(error.value)


def test_review_digest_binds_content_scope_and_validity(setup: Setup) -> None:
    service, worker, _ = setup
    proposal = propose(service, worker).proposal
    variants = [
        proposal.model_copy(update={"operation_id": uuid4()}),
        proposal.model_copy(
            update={"worker": proposal.worker.model_copy(update={"attempt_id": uuid4()})}
        ),
        proposal.model_copy(
            update={"request": proposal.request.model_copy(update={"destination": "other"})}
        ),
        proposal.model_copy(
            update={
                "request": proposal.request.model_copy(
                    update={"request_json": '{"analysis":"other"}'}
                )
            }
        ),
        proposal.model_copy(update={"service_policy_sha256": "f" * 64}),
        proposal.model_copy(update={"expires_at": _NOW + timedelta(minutes=10)}),
    ]
    assert all(
        item.sha256 != proposal.sha256 and item.group_id != proposal.group_id for item in variants
    )
    arguments = proposal.review_call.arguments
    arguments.clear()
    assert proposal.review_call.arguments
    with pytest.raises(ValidationError):
        proposal.request.request_json = "{}"  # type: ignore[misc]


def test_journal_rejects_repeated_decision_and_duplicate_operation_source(setup: Setup) -> None:
    service, worker, _ = setup
    record = propose(service, worker)
    service.handle(command(service, record))
    events = service.replay_events()
    terminal = events[-1]
    with pytest.raises(ExternalActionError):
        external_action_records(
            (*events, terminal.model_copy(update={"sequence": terminal.sequence + 1}))
        )
    duplicate = record.model_copy(
        update={"proposal": record.proposal.model_copy(update={"operation_id": uuid4()})}
    )
    event = SessionEvent(
        event_id="synthetic-duplicate",
        session_id=service.store.session_id,
        sequence=100,
        kind=EventKind.EXTERNAL_ACTION_RECORDED,
        occurred_at=_NOW.isoformat(),
        payload={"record": duplicate.model_dump(mode="json")},
    )
    with pytest.raises(ExternalActionError):
        external_action_records((*events, event))
