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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import AnyHttpUrl, ValidationError

from heartwood.audit import LocalEd25519CheckpointSigner, checkpoint_public_key_fingerprint
from heartwood.audit._anchored_signer import AnchoredCheckpointSigner
from heartwood.audit._records_s3 import CheckpointAuthorityScope, S3CheckpointRecords
from heartwood.core_adapter import (
    BackendConfirmationResolutionEvent,
    BackendEvent,
    BackendEventSink,
    BackendToolCallEvent,
    BackendToolExecutionEvent,
    DeterministicAgentBackend,
    ProposedToolCall,
    SessionService,
    TokenDeltaSink,
    ToolExecution,
)
from heartwood.core_adapter._state import SessionRecoveryError
from heartwood.core_adapter.checkpoint_evidence import (
    CheckpointEvidenceUnavailableError,
    SessionCheckpointEvidence,
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
from heartwood.core_adapter.external_results import ExternalResultRecord, external_result_records
from heartwood.core_adapter.worker_attempts import WorkerRuntimeBinding
from heartwood.schemas import AuditEvent, AuditRetention, JsonValue, PlatformOperationObservation
from heartwood.session import CommandKind, EventKind, SessionCommand, SessionEvent
from packages.audit.tests.records_fixtures import MemoryS3

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_PROJECT = uuid4()
_IMAGE = "sha256:" + "a" * 64


class Worker(DeterministicAgentBackend):
    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[str] = []
        self.delivery_hook: Callable[[], None] = lambda: None

    def submit_context(self, *, session_id: str, content: str) -> tuple[BackendEvent, ...]:
        assert session_id
        self.contexts.append(content)
        self.delivery_hook()
        return ()

    def bind_runtime(self, *, event_sink: BackendEventSink, token_sink: TokenDeltaSink) -> None:
        super().bind_runtime(event_sink=event_sink, token_sink=token_sink)
        self.emit = event_sink


class Platform:
    tool_names = frozenset({"submit_analysis"})
    policy_sha256 = "e" * 64

    def __init__(self) -> None:
        self.result_json = '{"aggregate":7}'
        self.release_authorized = True
        self.content = b"synthetic input"
        self.submissions: list[tuple[bytes, ...]] = []
        self.observations: list[ExternalActionProposal] = []
        self.authorized = True
        self.before_submit: Callable[[], None] = lambda: None
        self.after_submit: Callable[[], None] = lambda: None
        self.check_hook: Callable[[], None] = lambda: None
        self.status: Literal["running", "completed", "unknown"] = "completed"

    def prepare_result(self, record: ExternalActionRecord) -> str:
        assert record.observation is not None
        return self.result_json

    def check_result_policy(
        self,
        record: ExternalActionRecord,
        content_json: str,
        model_route_json: str,
        actor_id: str,
        now: datetime,
    ) -> None:
        assert record.terminal
        assert record.observation is not None
        assert canonical_request(content_json) == content_json
        assert json.loads(model_route_json)["endpoint"]
        assert now >= record.observation.operation.observed_at
        if not self.release_authorized or actor_id != "researcher":
            raise PermissionError("synthetic release credential")
        self.check_hook()

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
    record: ExternalActionRecord | ExternalResultRecord,
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


def result_proposal(service: SessionService, worker: Worker) -> ExternalResultRecord:
    outside = propose(service, worker)
    service.handle(command(service, outside))
    event = service.propose_external_result(outside.proposal.operation_id)
    return ExternalResultRecord.model_validate_json(json.dumps(event.payload["record"]))


def result_latest(service: SessionService) -> ExternalResultRecord:
    return list(external_result_records(service.replay_events()).values())[-1]


def test_result_release_requires_distinct_exact_review(setup: Setup) -> None:
    service, worker, platform = setup
    record = result_proposal(service, worker)
    assert worker.contexts == []
    assert record.proposal.review_call.arguments["content"] == {"aggregate": 7}
    route = record.proposal.review_call.arguments["model_route"]
    assert isinstance(route, dict)
    assert route["endpoint"] == worker.model_endpoint
    assert route["model_id"] == worker.model_id
    platform.result_json = '{"aggregate":999}'
    approved = command(service, record)
    service.handle(approved)
    assert worker.contexts == [record.proposal.message]
    assert json.loads(worker.contexts[0])["content"] == {"aggregate": 7}
    assert result_latest(service).phase == "submitted"
    assert service.handle(approved).replayed
    service.handle(command(service, record))
    assert len(worker.contexts) == 1
    assert not any(
        event.kind == EventKind.USER_MESSAGE_RECORDED for event in service.replay_events()
    )
    audit = [
        event
        for event in service.audit_log.read()
        if event.event_type == EventKind.EXTERNAL_RESULT_RECORDED.value
    ]
    assert len(audit) == 4
    assert "aggregate" not in str(audit)
    assert "content_json" not in str(audit)
    service.audit_log.verify()


def test_result_denial_does_not_deliver(setup: Setup) -> None:
    service, worker, platform = setup
    record = result_proposal(service, worker)
    platform.release_authorized = False
    service.handle(command(service, record, approved=False))
    assert result_latest(service).phase == "denied"
    assert worker.contexts == []


@pytest.mark.parametrize(
    "changed", ["actor", "worker", "policy", "route", "model", "expiry", "entitlement"]
)
def test_result_authority_changes_prevent_disclosure(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    service, worker, platform = setup
    record = result_proposal(service, worker)
    actor = "other" if changed == "actor" else "researcher"
    if changed == "worker":
        service.fence_worker_attempt(attempt_id=record.proposal.worker.attempt_id)
    elif changed == "policy":
        platform.policy_sha256 = "f" * 64
    elif changed == "route":
        monkeypatch.setattr(Worker, "model_endpoint", property(lambda _: "https://other.invalid"))
    elif changed == "model":
        monkeypatch.setattr(Worker, "model_id", property(lambda _: "different-model"))
    elif changed == "expiry":
        service.clock = lambda: (_NOW + timedelta(minutes=6)).isoformat()
    elif changed == "entitlement":
        platform.release_authorized = False
    service.handle(command(service, record, actor=actor))
    assert worker.contexts == []
    assert result_latest(service).phase == "proposed"


def test_result_fencing_during_service_check_prevents_delivery(setup: Setup) -> None:
    service, worker, platform = setup
    record = result_proposal(service, worker)
    platform.check_hook = lambda: service.fence_worker_attempt(
        attempt_id=record.proposal.worker.attempt_id
    )
    service.handle(command(service, record))
    assert worker.contexts == []


def test_result_has_fresh_validity_after_long_outside_operation(setup: Setup) -> None:
    service, worker, _ = setup
    outside = propose(service, worker)
    service.handle(command(service, outside))
    service.clock = lambda: (_NOW + timedelta(hours=1)).isoformat()
    event = service.propose_external_result(outside.proposal.operation_id)
    record = ExternalResultRecord.model_validate_json(json.dumps(event.payload["record"]))
    service.handle(command(service, record))
    assert worker.contexts == [record.proposal.message]


@pytest.mark.parametrize(
    "content", ['{"x":NaN}', '{"x":1,"x":2}', "[]", '{"x":"' + "a" * (64 * 1024) + '"}']
)
def test_result_rejects_unbounded_or_ambiguous_content(setup: Setup, content: str) -> None:
    service, worker, platform = setup
    outside = propose(service, worker)
    service.handle(command(service, outside))
    platform.result_json = content
    with pytest.raises(ExternalActionError, match="unavailable or invalid"):
        service.propose_external_result(outside.proposal.operation_id)
    assert worker.contexts == []
    assert external_result_records(service.replay_events()) == {}


def test_result_uncertain_delivery_is_never_repeated(setup: Setup, tmp_path: Path) -> None:
    service, worker, platform = setup
    record = result_proposal(service, worker)

    def lose_response() -> None:
        assert result_latest(service).phase == "submitting"
        raise TimeoutError("synthetic credential must stay private")

    worker.delivery_hook = lose_response
    approved = command(service, record)
    service.handle(approved)
    assert result_latest(service).phase == "unknown"
    assert len(worker.contexts) == 1
    assert "synthetic credential" not in str(service.replay_events())
    service.close()
    restored_worker = Worker()
    restored = SessionService.local_default(
        tmp_path, backend=restored_worker, worker_project_id=_PROJECT, external_actions=platform
    )
    try:
        assert restored.handle(approved).replayed
        restored.reconcile_external_actions()
        assert restored_worker.contexts == []
        assert result_latest(restored).phase == "unknown"
    finally:
        restored.close()


@pytest.mark.parametrize("fail_phase", ["submitting", "submitted"])
def test_result_journal_failure_cannot_cause_resubmission(
    setup: Setup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_phase: str
) -> None:
    service, worker, platform = setup
    record = result_proposal(service, worker)
    approved = command(service, record)
    original = service.store.commit_event

    def commit(event: SessionEvent, audit: AuditEvent) -> None:
        payload = event.payload.get("record")
        if (
            event.kind == EventKind.EXTERNAL_RESULT_RECORDED
            and isinstance(payload, dict)
            and payload["phase"] == fail_phase
        ):
            raise OSError("synthetic interrupted journal")
        original(event, audit)

    with monkeypatch.context() as patch:
        patch.setattr(service.store, "commit_event", commit)
        with pytest.raises(OSError, match="interrupted journal"):
            service.handle(approved)
    assert len(worker.contexts) == (1 if fail_phase == "submitted" else 0)
    service.close()
    replacement = Worker()
    restored = SessionService.local_default(
        tmp_path, backend=replacement, worker_project_id=_PROJECT, external_actions=platform
    )
    try:
        if fail_phase == "submitting":
            with pytest.raises(SessionRecoveryError, match="interrupted after acceptance"):
                restored.handle(approved)
        else:
            assert restored.handle(approved).replayed
        assert replacement.contexts == []
        assert result_latest(restored).phase == (
            "authorized" if fail_phase == "submitting" else "submitting"
        )
    finally:
        restored.close()


def test_result_cannot_bypass_model_authorization(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, worker, _ = setup
    outside = propose(service, worker)
    service.handle(command(service, outside))
    monkeypatch.setattr(
        Worker,
        "model_endpoint",
        property(lambda _: "https://unapproved.invalid/v1/chat/completions"),
    )
    event = service.propose_external_result(outside.proposal.operation_id)
    record = ExternalResultRecord.model_validate_json(json.dumps(event.payload["record"]))
    service.handle(command(service, record))
    decisions = [
        event
        for event in service.replay_events()
        if event.kind == EventKind.MODEL_CALL_DECISION_RECORDED
    ]
    decision = decisions[-1].payload["decision"]
    assert isinstance(decision, dict)
    assert decision["decision"] == "deny"
    assert worker.contexts == []
    assert result_latest(service).phase == "proposed"


def test_result_cannot_read_after_worker_fencing(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, worker, platform = setup
    outside = propose(service, worker)
    service.handle(command(service, outside))
    service.fence_worker_attempt(attempt_id=outside.proposal.worker.attempt_id)

    def read(_record: ExternalActionRecord) -> str:
        pytest.fail("fenced worker must not initiate a result read")

    monkeypatch.setattr(platform, "prepare_result", read)
    with pytest.raises(ExternalActionError):
        service.propose_external_result(outside.proposal.operation_id)


def test_worker_cannot_settle_result_review(setup: Setup) -> None:
    service, worker, _ = setup
    record = result_proposal(service, worker)
    worker.emit(
        (
            BackendConfirmationResolutionEvent(
                action_group_id=record.proposal.group_id,
                tool_call=record.proposal.review_call,
                approved=True,
                source_event_id="forged-result-approval",
            ),
        )
    )
    assert result_latest(service).phase == "proposed"
    assert worker.contexts == []
    assert not any(
        event.payload.get("source_event_id") == "forged-result-approval"
        for event in service.replay_events()
    )


def test_result_review_cannot_be_reprepared(setup: Setup) -> None:
    service, worker, _ = setup
    record = result_proposal(service, worker)
    with pytest.raises(ExternalActionError, match="unique current"):
        service.propose_external_result(record.proposal.operation_id)


@pytest.mark.parametrize(
    "changed", ["content_json", "model_route_json", "worker", "decision", "phase"]
)
def test_result_reducer_rejects_changed_review_or_reused_decision(
    setup: Setup, changed: str
) -> None:
    service, worker, _ = setup
    record = result_proposal(service, worker)
    proposal = record.proposal
    if changed in {"content_json", "model_route_json"}:
        proposal = proposal.model_copy(update={changed: '{"changed":true}'})
    elif changed == "worker":
        proposal = proposal.model_copy(
            update={"worker": proposal.worker.model_copy(update={"attempt_id": uuid4()})}
        )
    outside = latest(service)
    assert outside.decision is not None
    decision = ExternalActionDecision(
        command_id=outside.decision.command_id if changed == "decision" else str(uuid4()),
        actor_id="researcher",
        decision="approved",
        decided_at=_NOW,
        proposal_sha256=proposal.sha256,
    )
    changed_record = ExternalResultRecord(
        phase="submitted" if changed == "phase" else "submitting",
        proposal=proposal,
        decision=decision,
    )
    events = service.replay_events()
    forged = SessionEvent(
        event_id="forged-release",
        session_id=service.store.session_id,
        sequence=events[-1].sequence + 1,
        kind=EventKind.EXTERNAL_RESULT_RECORDED,
        occurred_at=_NOW.isoformat(),
        payload={"record": changed_record.model_dump(mode="json")},
    )
    with pytest.raises(ExternalActionError):
        external_result_records((*events, forged))


def test_result_reducer_requires_matching_terminal_observation(setup: Setup) -> None:
    service, worker, _ = setup
    record = result_proposal(service, worker)
    events = service.replay_events()
    changed = record.model_copy(
        update={"proposal": record.proposal.model_copy(update={"operation_id": uuid4()})}
    )
    forged = events[-1].model_copy(update={"payload": {"record": changed.model_dump(mode="json")}})
    with pytest.raises(ExternalActionError, match="matching outside observation"):
        external_result_records((*events[:-1], forged))


@pytest.mark.parametrize("busy", [False, True])
def test_result_does_not_override_active_work_or_native_review(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, busy: bool
) -> None:
    service, worker, _ = setup
    record = result_proposal(service, worker)
    if busy:
        monkeypatch.setattr(worker, "wait_for_idle", lambda _: False)
    else:
        worker.submit_turn(session_id=service.store.session_id, prompt="Keep this action pending")
    service.handle(command(service, record))
    assert worker.contexts == []
    assert result_latest(service).phase == "proposed"


@pytest.fixture
def evidence(setup: Setup, tmp_path: Path) -> tuple[SessionCheckpointEvidence, MemoryS3]:
    service, _, _ = setup
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "synthetic-signer.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    signer = LocalEd25519CheckpointSigner(
        private_key=path, signer_id="synthetic", key_id="test", key_version="1"
    )
    client = MemoryS3()
    records = S3CheckpointRecords(
        client=client,
        bucket="synthetic-records",
        prefix="audit",
        scope=CheckpointAuthorityScope(
            "synthetic",
            _PROJECT,
            service.store.session_id,
            AuditRetention(policy_id="synthetic", retain_until="2033-01-01"),
        ),
    )
    trusted = {checkpoint_public_key_fingerprint(key.public_key()): key.public_key()}
    authority = AnchoredCheckpointSigner(
        records=records, signer=signer, trusted_keys=trusted, clock=lambda: _NOW
    )
    controller = SessionCheckpointEvidence(
        records=records, signer=authority, trusted_keys=trusted, mode="buffered"
    )
    service._checkpoint_evidence = controller
    return controller, client


@pytest.mark.parametrize("release", [False, True])
def test_outside_evidence_outage_preserves_predispatch_recovery(
    setup: Setup, evidence: tuple[SessionCheckpointEvidence, MemoryS3], release: bool
) -> None:
    service, worker, platform = setup
    controller, client = evidence
    record = result_proposal(service, worker) if release else propose(service, worker)
    approved = command(service, record)
    before = len(platform.submissions)
    client.unavailable = True
    try:
        with pytest.raises(CheckpointEvidenceUnavailableError):
            service.handle(approved)
        retained = result_latest(service) if release else latest(service)
        assert retained.phase == "authorized"
        assert service.store.unresolved_command_ids() == (approved.command_id,)
        observations = len(platform.observations)
        assert service.reconcile_external_actions() == ()
        assert len(platform.observations) == observations
        with pytest.raises(CheckpointEvidenceUnavailableError):
            service.reconcile()
        assert len(platform.submissions) == before
        assert worker.contexts == []
    finally:
        client.unavailable = False

    def check_evidence() -> None:
        retained = controller.records.history()[-1]
        boundaries = []
        for event in service.replay_events():
            raw = event.payload.get("record")
            if not isinstance(raw, dict) or raw.get("phase") != "authorized":
                continue
            decision = raw.get("decision")
            if isinstance(decision, dict) and decision.get("command_id") == approved.command_id:
                boundaries.append(event)
        assert len(boundaries) == 1
        boundary = boundaries[0]
        assert retained.checkpoint.statement.audit_event_count > boundary.sequence
        assert (result_latest(service) if release else latest(service)).phase == "submitting"

    if release:
        worker.delivery_hook = check_evidence
    else:
        platform.before_submit = check_evidence
    service.reconcile()
    assert service.store.unresolved_command_ids() == ()
    assert (result_latest(service) if release else latest(service)).phase == (
        "submitted" if release else "observed"
    )
    assert len(platform.submissions) == before + (0 if release else 1)
    assert len(worker.contexts) == int(release)
    assert service.handle(approved).replayed
    assert len(platform.submissions) == before + (0 if release else 1)
    assert len(worker.contexts) == int(release)


@pytest.mark.parametrize("release", [False, True])
def test_pending_outside_authorization_rechecks_policy_after_outage(
    setup: Setup, evidence: tuple[SessionCheckpointEvidence, MemoryS3], release: bool
) -> None:
    service, worker, platform = setup
    _, client = evidence
    record = result_proposal(service, worker) if release else propose(service, worker)
    before = len(platform.submissions)
    client.unavailable = True
    try:
        with pytest.raises(CheckpointEvidenceUnavailableError):
            service.handle(command(service, record))
    finally:
        client.unavailable = False
    platform.release_authorized = False
    platform.authorized = False
    with pytest.raises(SessionRecoveryError, match="current policy"):
        service.reconcile()
    assert (result_latest(service) if release else latest(service)).phase == "authorized"
    assert len(platform.submissions) == before
    assert worker.contexts == []
    assert service.store.unresolved_command_ids()


@pytest.mark.parametrize("release", [False, True])
def test_predispatch_recovery_never_revives_a_worker_after_restart(
    setup: Setup,
    evidence: tuple[SessionCheckpointEvidence, MemoryS3],
    tmp_path: Path,
    release: bool,
) -> None:
    service, worker, platform = setup
    controller, client = evidence
    record = result_proposal(service, worker) if release else propose(service, worker)
    approved = command(service, record)
    before = len(platform.submissions)
    client.unavailable = True
    try:
        with pytest.raises(CheckpointEvidenceUnavailableError):
            service.handle(approved)
    finally:
        client.unavailable = False
    service.close()
    replacement = Worker()
    restored = SessionService.local_default(
        tmp_path,
        backend=replacement,
        worker_project_id=_PROJECT,
        external_actions=platform,
        checkpoint_evidence=controller,
        clock=lambda: _NOW.isoformat(),
    )
    try:
        assert restored.reconcile_external_actions() == ()
        with pytest.raises(SessionRecoveryError, match="interrupted after acceptance"):
            restored.handle(approved)
        assert restored.store.unresolved_command_ids() == (approved.command_id,)
        assert (result_latest(restored) if release else latest(restored)).phase == "authorized"
        assert len(platform.submissions) == before
        assert replacement.contexts == []
    finally:
        restored.close()


@pytest.mark.parametrize("change", ["input", "expiry"])
def test_pending_outside_action_revalidates_input_and_expiry(
    setup: Setup, evidence: tuple[SessionCheckpointEvidence, MemoryS3], change: str
) -> None:
    service, worker, platform = setup
    _, client = evidence
    record = propose(service, worker)
    client.unavailable = True
    try:
        with pytest.raises(CheckpointEvidenceUnavailableError):
            service.handle(command(service, record))
    finally:
        client.unavailable = False
    if change == "input":
        platform.content = b"changed input"
    else:
        service.clock = lambda: record.proposal.expires_at.isoformat()
    with pytest.raises(SessionRecoveryError, match="current policy"):
        service.reconcile()
    assert latest(service).phase == "authorized"
    assert platform.submissions == []


@pytest.mark.parametrize("release", [False, True])
def test_predispatch_recovery_validates_receipt_before_effect(
    setup: Setup,
    evidence: tuple[SessionCheckpointEvidence, MemoryS3],
    monkeypatch: pytest.MonkeyPatch,
    release: bool,
) -> None:
    service, worker, platform = setup
    _, client = evidence
    record = result_proposal(service, worker) if release else propose(service, worker)
    approved = command(service, record)
    before = len(platform.submissions)
    client.unavailable = True
    try:
        with pytest.raises(CheckpointEvidenceUnavailableError):
            service.handle(approved)
    finally:
        client.unavailable = False
    with monkeypatch.context() as patch:
        patch.setattr(service.store, "command_record", lambda _command_id: {})
        with pytest.raises(SessionRecoveryError, match="receipt is invalid"):
            service.reconcile()
    assert len(platform.submissions) == before
    assert worker.contexts == []
    assert (result_latest(service) if release else latest(service)).phase == "authorized"
