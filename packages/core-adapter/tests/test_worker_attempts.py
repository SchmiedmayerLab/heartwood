# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import heartwood.core_adapter._state as state_module
import heartwood.core_adapter.worker_attempts as worker_attempts
from heartwood.core_adapter import (
    BackendAgentMessageEvent,
    BackendConfirmationRequestEvent,
    BackendConfirmationResolutionEvent,
    BackendEvent,
    BackendEventSink,
    BackendLifecycle,
    BackendLifecycleEvent,
    DeterministicAgentBackend,
    ProposedToolCall,
    SessionService,
    TokenDeltaSink,
)
from heartwood.core_adapter.worker_attempts import (
    WorkerAttemptError,
    WorkerAttemptIntent,
    WorkerAttemptLease,
    WorkerAttemptRecord,
    WorkerRevocationEvidence,
    WorkerRuntimeBinding,
    worker_replacement_blocker,
)
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand, SessionEvent

_PROJECT = UUID("2ce140c9-e046-4fe3-9437-72b87341bf6f")
_IMAGE = "sha256:" + "a" * 64


class RecordingBackend(DeterministicAgentBackend):
    def __init__(self) -> None:
        super().__init__()
        self.bindings: list[tuple[BackendEventSink, TokenDeltaSink]] = []
        self.dispatches = 0
        self.reconciliations = 0

    def bind_runtime(self, *, event_sink: BackendEventSink, token_sink: TokenDeltaSink) -> None:
        super().bind_runtime(event_sink=event_sink, token_sink=token_sink)
        self.bindings.append((event_sink, token_sink))

    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
        self.dispatches += 1
        return super().submit_turn(session_id=session_id, prompt=prompt)

    def reconcile(
        self, *, session_id: str, known_source_event_ids: frozenset[str]
    ) -> tuple[BackendEvent, ...]:
        self.reconciliations += 1
        return super().reconcile(
            session_id=session_id, known_source_event_ids=known_source_event_ids
        )


@pytest.fixture
def service(tmp_path: Path) -> Iterator[SessionService]:
    value = SessionService.local_default(
        tmp_path, worker_project_id=_PROJECT, backend=RecordingBackend()
    )
    try:
        yield value
    finally:
        value.close()


def _record(service: SessionService) -> WorkerAttemptRecord:
    record = service.worker_attempt()
    assert record is not None
    return record


def _prepare(service: SessionService) -> WorkerAttemptIntent:
    return service.prepare_worker_attempt(
        image_digest=_IMAGE, sandbox_policy_sha256="b" * 64, input_manifest_sha256="c" * 64
    )


def _runtime() -> WorkerRuntimeBinding:
    return WorkerRuntimeBinding.model_validate(
        {
            "sandbox_id": "synthetic-worker",
            "generation": "generation-1",
            "endpoint": "http://worker.test",
            "image_digest": _IMAGE,
            "sandbox_policy_sha256": "b" * 64,
        }
    )


def _activate(service: SessionService) -> WorkerAttemptLease:
    intent = _prepare(service)
    return service.bind_worker_runtime(attempt_id=intent.attempt_id, runtime=_runtime())


def _revocation(
    attempt_id: UUID, *, sandbox_id: str | None = "synthetic-worker"
) -> WorkerRevocationEvidence:
    return WorkerRevocationEvidence(
        attempt_id=attempt_id,
        sandbox_id=sandbox_id,
        observer_id="synthetic-control-plane",
        evidence_sha256="d" * 64,
        outcome="not-created" if sandbox_id is None else "revoked",
    )


def _chat(service: SessionService) -> None:
    service.handle(
        SessionCommand(
            command_id=str(uuid4()),
            session_id=service.store.session_id,
            kind=CommandKind.CHAT,
            created_at="2026-01-01T00:00:00Z",
            payload={"message": "Synthetic task"},
        )
    )


def test_intent_and_binding_are_paired_and_audit_minimized(service: SessionService) -> None:
    intent = _prepare(service)
    assert _record(service).phase == "prepared"
    assert len(service.audit_log.read()) == len(service.replay_events()) == 1
    lease = service.bind_worker_runtime(attempt_id=intent.attempt_id, runtime=_runtime())
    assert lease.active
    assert len(service.audit_log.read()) == len(service.replay_events()) == 2
    service.audit_log.verify()
    audit = service.audit_log.read()[-1].payload
    assert audit["attempt_id"] == str(intent.attempt_id)
    assert len(str(audit["binding_sha256"])) == 64
    assert "worker.test" not in str(audit)
    assert "synthetic-worker" not in str(audit)
    with pytest.raises(AttributeError):
        lease.intent = intent.model_copy(update={"attempt_id": uuid4()})  # type: ignore[misc]


@pytest.mark.parametrize("phase", ["prepared", "active", "fenced"])
def test_restart_cannot_recover_authority_or_fall_back(
    service: SessionService, tmp_path: Path, phase: str
) -> None:
    intent = _prepare(service)
    if phase != "prepared":
        service.bind_worker_runtime(attempt_id=intent.attempt_id, runtime=_runtime())
    if phase == "fenced":
        service.fence_worker_attempt(attempt_id=intent.attempt_id)
    service.close()
    backend = RecordingBackend()
    restored = SessionService.local_default(tmp_path, worker_project_id=_PROJECT, backend=backend)
    try:
        assert _record(restored).phase == phase
        assert restored.reconcile() == ()
        _chat(restored)
        assert backend.dispatches == backend.reconciliations == 0
        assert "reconciliation" in str(restored.replay_events()[-1].payload)
        with pytest.raises(WorkerAttemptError, match="revoked"):
            _prepare(restored)
        with pytest.raises(WorkerAttemptError, match="replayed"):
            restored.bind_worker_runtime(attempt_id=intent.attempt_id, runtime=_runtime())
    finally:
        restored.close()


def test_replacement_requires_revocation_and_preserves_conversation_identity(
    service: SessionService, tmp_path: Path
) -> None:
    lease = _activate(service)
    with pytest.raises(WorkerAttemptError, match="transition"):
        service.record_worker_revocation(_revocation(lease.intent.attempt_id))
    service.fence_worker_attempt(attempt_id=lease.intent.attempt_id)
    assert not lease.active
    with pytest.raises(ValidationError, match="another runtime"):
        service.record_worker_revocation(_revocation(lease.intent.attempt_id, sandbox_id="other"))
    service.record_worker_revocation(_revocation(lease.intent.attempt_id))
    count = len(service.replay_events())
    service.record_worker_revocation(_revocation(lease.intent.attempt_id))
    assert len(service.replay_events()) == count
    with pytest.raises(WorkerAttemptError, match="fresh idle"):
        _prepare(service)
    service.close()
    replacement = SessionService.local_default(tmp_path, worker_project_id=_PROJECT)
    try:
        fresh = _activate(replacement)
        assert fresh.active
        assert not lease.active
        assert fresh.intent.attempt_id != lease.intent.attempt_id
        assert fresh.intent.conversation_id == lease.intent.conversation_id
        assert fresh.intent.session_id == lease.intent.session_id
        assert fresh.intent.project_id == lease.intent.project_id
    finally:
        replacement.close()


@pytest.mark.parametrize("sandbox_id", [None, "observed-orphan"])
def test_interrupted_launch_requires_outside_observation(
    service: SessionService, tmp_path: Path, sandbox_id: str | None
) -> None:
    intent = _prepare(service)
    service.close()
    restored = SessionService.local_default(tmp_path, worker_project_id=_PROJECT)
    try:
        restored.fence_worker_attempt(attempt_id=intent.attempt_id)
        restored.record_worker_revocation(_revocation(intent.attempt_id, sandbox_id=sandbox_id))
        assert _prepare(restored).conversation_id == intent.conversation_id
    finally:
        restored.close()


@pytest.mark.parametrize("release_writer", [False, True])
def test_old_callbacks_cannot_reacquire_authority(
    service: SessionService, release_writer: bool
) -> None:
    backend = service.backend
    assert isinstance(backend, RecordingBackend)
    tokens: list[str] = []
    service._token_sink = tokens.append
    lease = _activate(service)
    event_sink, token_sink = backend.bindings[-1]
    event_sink((BackendAgentMessageEvent(message="accepted", source_event_id="before"),))
    token_sink("accepted")
    assert service.replay_events()[-1].payload["worker_attempt_id"] == str(lease.intent.attempt_id)
    assert service.audit_log.read()[-1].payload["worker_attempt_id"] == str(lease.intent.attempt_id)
    if release_writer:
        identity = service.store.writer_identity
        service.store.release_writer()
        event_sink((BackendAgentMessageEvent(message="stale", source_event_id="released"),))
        assert not service.store.owns_writer
        service.store.acquire_writer()
        assert service.store.writer_identity != identity
    else:
        service.fence_worker_attempt(attempt_id=lease.intent.attempt_id)
    count = len(service.replay_events())
    # Includes callbacks captured before the worker was bound.
    for events, token in backend.bindings:
        events((BackendAgentMessageEvent(message="stale", source_event_id=str(uuid4())),))
        token("stale")
    assert len(service.replay_events()) == count
    assert tokens == ["accepted"]
    assert not lease.active


@pytest.mark.parametrize(
    ("field", "value"),
    [("image_digest", "sha256:" + "e" * 64), ("sandbox_policy_sha256", "f" * 64)],
)
def test_runtime_must_match_prepared_policy(
    service: SessionService, field: str, value: str
) -> None:
    intent = _prepare(service)
    runtime = WorkerRuntimeBinding.model_validate({**_runtime().model_dump(), field: value})
    with pytest.raises(ValidationError, match="authorized image and policy"):
        service.bind_worker_runtime(attempt_id=intent.attempt_id, runtime=runtime)
    assert len(service.replay_events()) == 1


def test_wrong_project_cannot_reconcile_or_replace(service: SessionService, tmp_path: Path) -> None:
    intent = _prepare(service)
    service.close()
    wrong = SessionService.local_default(tmp_path, worker_project_id=uuid4())
    try:
        with pytest.raises(WorkerAttemptError, match="another project"):
            wrong.fence_worker_attempt(attempt_id=intent.attempt_id)
    finally:
        wrong.close()


def test_failed_fence_commit_still_closes_local_admission(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = _activate(service)

    def fail(*_args: object) -> None:
        raise OSError("synthetic journal interruption")

    with monkeypatch.context() as context:
        context.setattr(service.store, "commit_event", fail)
        with pytest.raises(OSError, match="interruption"):
            service.fence_worker_attempt(attempt_id=lease.intent.attempt_id)
    assert not lease.active
    assert _record(service).phase == "active"
    service.fence_worker_attempt(attempt_id=lease.intent.attempt_id)
    assert _record(service).phase == "fenced"


def test_interrupted_pair_recovers_without_activating(
    service: SessionService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    append = state_module._append_private_json_line

    def interrupted(path: Path, content: str) -> None:
        if path == service.store.events_path:
            raise OSError("synthetic session append interruption")
        append(path, content)

    with monkeypatch.context() as context:
        context.setattr(state_module, "_append_private_json_line", interrupted)
        with pytest.raises(OSError, match="interruption"):
            _prepare(service)
    assert service.store.pending_commit_path.exists()
    service.close()
    restored = SessionService.local_default(tmp_path, worker_project_id=_PROJECT)
    try:
        _chat(restored)
        assert not restored.store.pending_commit_path.exists()
        restored.audit_log.verify()
        assert len(restored.audit_log.read()) == len(restored.replay_events())
        assert restored.replay_events()[0].kind == EventKind.WORKER_ATTEMPT_RECORDED
        assert "reconciliation" in str(restored.replay_events()[-1].payload)
    finally:
        restored.close()


def test_unmanaged_project_cannot_issue_attempt(tmp_path: Path) -> None:
    local = SessionService.local_default(tmp_path)
    try:
        with pytest.raises(WorkerAttemptError, match="deployment project"):
            _prepare(local)
    finally:
        local.close()


def test_backend_binding_failure_does_not_leave_dispatch_authority(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _prepare(service)

    def fail(**_kwargs: object) -> None:
        raise RuntimeError("synthetic backend binding failure")

    monkeypatch.setattr(service.backend, "bind_runtime", fail)
    with pytest.raises(RuntimeError, match="binding failure"):
        service.bind_worker_runtime(attempt_id=intent.attempt_id, runtime=_runtime())
    _chat(service)
    backend = service.backend
    assert isinstance(backend, RecordingBackend)
    assert backend.dispatches == 0
    assert "reconciliation" in str(service.replay_events()[-1].payload)


def test_restored_backend_cannot_emit_unbound_tokens(
    service: SessionService,
    tmp_path: Path,
) -> None:
    _activate(service)
    service.close()
    tokens: list[str] = []
    backend = RecordingBackend()
    restored = SessionService.local_default(
        tmp_path,
        backend=backend,
        worker_project_id=_PROJECT,
        token_sink=tokens.append,
    )
    try:
        backend.bindings[0][1]("before recovery")
        restored.reconcile()
        backend.bindings[0][1]("after recovery")
        assert tokens == []
    finally:
        restored.close()


def test_outside_runtime_observation_fences_changed_generation(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_attempts, "_RUNTIME_OBSERVATION_SECONDS", 0.0)
    lease = _activate(service)
    generation = lease.runtime.generation
    observations: list[WorkerRuntimeBinding] = []

    def observe(runtime: WorkerRuntimeBinding) -> None:
        observations.append(runtime)
        if runtime.generation != generation:
            raise RuntimeError("synthetic private control-plane detail")

    lease.require_runtime_observation(observe)
    lease.ensure_active()
    assert observations == [lease.runtime, lease.runtime]
    generation = "replacement"
    with (
        pytest.raises(WorkerAttemptError, match="runtime observation") as error,
        lease.admit_callback(),
    ):
        pytest.fail("Changed generation reached the outside callback")
    assert "private" not in str(error.value)
    assert not lease.active
    generation = lease.runtime.generation
    with pytest.raises(WorkerAttemptError, match="closed"):
        lease.ensure_active()


def test_outside_runtime_observer_cannot_be_replaced(service: SessionService) -> None:
    lease = _activate(service)
    lease.require_runtime_observation(lambda _runtime: None)
    with pytest.raises(WorkerAttemptError, match="cannot be replaced"):
        lease.require_runtime_observation(lambda _runtime: None)


def test_outside_observer_cannot_reopen_fenced_admission(service: SessionService) -> None:
    lease = _activate(service)
    lease.fence()
    with pytest.raises(WorkerAttemptError, match="closed"):
        lease.require_runtime_observation(lambda _runtime: pytest.fail("Fenced observer invoked"))


def test_fencing_during_outside_observation_prevents_callback(service: SessionService) -> None:
    lease = _activate(service)
    with pytest.raises(WorkerAttemptError, match="closed"):
        lease.require_runtime_observation(lambda _runtime: lease.fence())
    assert not lease.active


def test_passing_runtime_observation_is_reused_while_fencing_stays_immediate(
    service: SessionService,
) -> None:
    lease = _activate(service)
    observations: list[WorkerRuntimeBinding] = []
    lease.require_runtime_observation(observations.append)
    lease.ensure_active()
    with lease.admit_callback():
        pass
    assert observations == [lease.runtime]
    lease.fence()
    with pytest.raises(WorkerAttemptError, match="closed"):
        lease.ensure_active()
    with pytest.raises(WorkerAttemptError, match="closed"), lease.admit_callback():
        pytest.fail("Fenced attempt reached the callback")
    assert observations == [lease.runtime]


def test_fencing_does_not_wait_for_a_runtime_observation(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_attempts, "_RUNTIME_OBSERVATION_SECONDS", 0.0)
    lease = _activate(service)
    observing, release = Event(), Event()
    stall = False

    def observe(_runtime: WorkerRuntimeBinding) -> None:
        if stall:
            observing.set()
            assert release.wait(10)

    def callback() -> None:
        with lease.admit_callback():
            pytest.fail("Fenced attempt reached the callback")

    lease.require_runtime_observation(observe)
    stall = True
    with ThreadPoolExecutor(max_workers=2) as executor:
        admitted = executor.submit(callback)
        assert observing.wait(10)
        try:
            executor.submit(lease.fence).result(timeout=5)
        finally:
            release.set()
        with pytest.raises(WorkerAttemptError, match="closed"):
            admitted.result(timeout=10)


def _task(service: SessionService) -> None:
    service.handle(
        SessionCommand(
            command_id=str(uuid4()),
            session_id=service.store.session_id,
            kind=CommandKind.CHAT,
            created_at="2026-01-01T00:00:00Z",
            payload={"prompt": "Synthetic task"},
        )
    )


def _revoke(service: SessionService, lease: WorkerAttemptLease) -> None:
    service.fence_worker_attempt(attempt_id=lease.intent.attempt_id)
    service.record_worker_revocation(_revocation(lease.intent.attempt_id))


def test_verified_revocation_lets_a_new_owner_continue_with_a_new_attempt(
    service: SessionService, tmp_path: Path
) -> None:
    first_backend = service.backend
    assert isinstance(first_backend, RecordingBackend)
    lease = _activate(service)
    stale_events, stale_token = first_backend.bindings[-1]
    _revoke(service, lease)
    _task(service)
    assert first_backend.dispatches == 0
    assert "reconciliation" in str(service.replay_events()[-1].payload)
    service.close()

    backend = RecordingBackend()
    tokens: list[str] = []
    restored = SessionService.local_default(
        tmp_path, worker_project_id=_PROJECT, backend=backend, token_sink=tokens.append
    )
    try:
        replacement = _activate(restored)
        assert replacement.active
        assert not lease.active
        assert replacement.intent.attempt_id != lease.intent.attempt_id
        assert replacement.intent.conversation_id == lease.intent.conversation_id
        _task(restored)
        assert backend.dispatches == 1
        recorded = restored.replay_events()[-1]
        assert recorded.payload["worker_attempt_id"] == str(replacement.intent.attempt_id)
        count = len(restored.replay_events())
        stale_events((BackendAgentMessageEvent(message="stale", source_event_id="late"),))
        stale_token("stale")
        assert len(restored.replay_events()) == count
        assert tokens == []
        with pytest.raises(WorkerAttemptError, match="fresh idle"):
            _prepare(restored)
    finally:
        restored.close()


def test_revoked_attempt_does_not_block_the_next_owners_first_dispatch(
    service: SessionService, tmp_path: Path
) -> None:
    _revoke(service, _activate(service))
    service.close()
    backend = RecordingBackend()
    restored = SessionService.local_default(tmp_path, worker_project_id=_PROJECT, backend=backend)
    try:
        # An owner that launches no workers never runs the revoked session on its own backend.
        _task(restored)
        assert backend.dispatches == 0
        assert "launches no replacement" in str(restored.replay_events()[-1].payload)
        restored.admit_worker_replacement()
        restored.handle(
            SessionCommand(
                command_id="after-admission",
                session_id=restored.store.session_id,
                kind=CommandKind.CHAT,
                created_at="2026-01-01T00:00:00Z",
                payload={"prompt": "Synthetic task"},
            )
        )
        assert backend.dispatches == 1
        assert _record(restored).phase == "revoked"
    finally:
        restored.close()


def _journal(*entries: tuple[EventKind, dict[str, JsonValue]]) -> tuple[SessionEvent, ...]:
    return tuple(
        SessionEvent(
            event_id=f"event-{sequence}",
            session_id="session-main",
            sequence=sequence,
            kind=kind,
            occurred_at="2026-01-01T00:00:00Z",
            payload=payload,
        )
        for sequence, (kind, payload) in enumerate(entries)
    )


_RUNNING = (EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "running"})
_REFUSED = (EventKind.ERROR_RECORDED, {"command": "chat", "reason": "prompt is required"})


@pytest.mark.parametrize(
    ("entries", "blocked"),
    [
        ((), False),
        ((_RUNNING, (EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "finished"})), False),
        ((_RUNNING, (EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "error"})), False),
        ((_RUNNING,), True),
        # A refused command records an error without ending the run or the pause it met.
        ((_RUNNING, _REFUSED), True),
        (((EventKind.SESSION_PAUSED, {}), _REFUSED), True),
        (
            (
                (EventKind.SESSION_PAUSED, {}),
                (EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "idle"}),
            ),
            True,
        ),
        (
            (
                (EventKind.SESSION_PAUSED, {}),
                (EventKind.SESSION_RESUMED, {}),
                (EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "finished"}),
            ),
            False,
        ),
        (
            (
                (EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "error"}),
                (EventKind.ERROR_RECORDED, {"code": "HW-AGENT-006", "reason": "unknown"}),
            ),
            True,
        ),
        (
            (
                (EventKind.CONFIRMATION_REQUESTED, {"request": {"tool_call_id": "call-1"}}),
                (EventKind.AGENT_LIFECYCLE_UPDATED, {"status": "idle"}),
            ),
            True,
        ),
        (
            (
                (EventKind.CONFIRMATION_REQUESTED, {"request": {"tool_call_id": "call-1"}}),
                (EventKind.CONFIRMATION_RESOLVED, {"tool_call_id": "call-1"}),
            ),
            False,
        ),
    ],
)
def test_replacement_requires_a_run_the_journal_shows_as_settled(
    entries: tuple[tuple[EventKind, dict[str, JsonValue]], ...], blocked: bool
) -> None:
    assert (worker_replacement_blocker(_journal(*entries)) is not None) is blocked


@pytest.mark.parametrize("left", ["running", "paused", "waiting-for-confirmation", "review"])
def test_work_left_open_by_a_revoked_worker_is_not_handed_to_a_replacement(
    service: SessionService, tmp_path: Path, left: str
) -> None:
    backend = service.backend
    assert isinstance(backend, RecordingBackend)
    lease = _activate(service)
    event_sink, _token_sink = backend.bindings[-1]
    call = ProposedToolCall(
        tool_call_id="call-open", tool_name="terminal", risk="medium", summary="Synthetic action"
    )
    if left == "review":
        event_sink(
            (
                BackendConfirmationRequestEvent(
                    tool_call=call, action_group_id="group-open", source_event_id="request"
                ),
                BackendLifecycleEvent(lifecycle=BackendLifecycle.IDLE, source_event_id="idle"),
            )
        )
    else:
        event_sink(
            (BackendLifecycleEvent(lifecycle=BackendLifecycle(left), source_event_id="left"),)
        )
    _revoke(service, lease)
    service.close()
    restored_backend = RecordingBackend()
    restored = SessionService.local_default(
        tmp_path, worker_project_id=_PROJECT, backend=restored_backend
    )
    try:
        _task(restored)
        assert restored_backend.dispatches == restored_backend.reconciliations == 0
        assert "continue in a new session" in str(restored.replay_events()[-1].payload)
        with pytest.raises(WorkerAttemptError, match="replacement is unavailable"):
            _prepare(restored)
        assert _record(restored).phase == "revoked"
    finally:
        restored.close()


def test_settled_review_and_run_allow_a_replacement(
    service: SessionService, tmp_path: Path
) -> None:
    backend = service.backend
    assert isinstance(backend, RecordingBackend)
    lease = _activate(service)
    event_sink, _token_sink = backend.bindings[-1]
    call = ProposedToolCall(
        tool_call_id="call-settled", tool_name="terminal", risk="medium", summary="Synthetic"
    )
    event_sink(
        (
            BackendLifecycleEvent(lifecycle=BackendLifecycle.RUNNING, source_event_id="running"),
            BackendConfirmationRequestEvent(
                tool_call=call, action_group_id="group-settled", source_event_id="request"
            ),
            BackendConfirmationResolutionEvent(
                tool_call=call,
                action_group_id="group-settled",
                approved=False,
                source_event_id="resolution",
            ),
            BackendLifecycleEvent(lifecycle=BackendLifecycle.FINISHED, source_event_id="finished"),
        )
    )
    _revoke(service, lease)
    service.close()
    restored = SessionService.local_default(tmp_path, worker_project_id=_PROJECT)
    try:
        assert _activate(restored).intent.conversation_id == lease.intent.conversation_id
    finally:
        restored.close()


def test_fencing_notifies_the_launcher_once_without_reopening_admission(
    service: SessionService,
) -> None:
    lease = _activate(service)
    notified: list[bool] = []
    lease.on_fenced(lambda: notified.append(lease.active))
    with pytest.raises(WorkerAttemptError, match="cannot be replaced"):
        lease.on_fenced(lambda: None)
    assert notified == []
    lease.fence()
    service.fence_worker_attempt(attempt_id=lease.intent.attempt_id)
    assert notified == [False]


def test_launcher_registered_after_fencing_is_notified_immediately(
    service: SessionService,
) -> None:
    lease = _activate(service)
    lease.fence()
    notified: list[bool] = []
    lease.on_fenced(lambda: notified.append(True))
    assert notified == [True]
