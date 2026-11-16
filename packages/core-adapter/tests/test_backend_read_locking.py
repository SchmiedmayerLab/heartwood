# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Backend reads never hold the session command lock."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from uuid import UUID, uuid4

import pytest

from heartwood.core_adapter import (
    BackendAgentMessageEvent,
    BackendEvent,
    DeterministicAgentBackend,
    SessionService,
)
from heartwood.core_adapter.worker_attempts import (
    WorkerAttemptLease,
    WorkerRevocationEvidence,
    WorkerRuntimeBinding,
)
from heartwood.session import CommandKind, EventKind, SessionCommand

_PROJECT = UUID("2ce140c9-e046-4fe3-9437-72b87341bf6f")
_IMAGE = "sha256:" + "a" * 64
_WAIT = 10.0


class StallingBackend(DeterministicAgentBackend):
    """Answer reads like a worker that can be made to stall and to report late state."""

    def __init__(self) -> None:
        super().__init__()
        self.stall = False
        self.reading = Event()
        self.release = Event()
        self.reads = 0
        self.observed: tuple[BackendEvent, ...] = ()

    def reconcile(
        self, *, session_id: str, known_source_event_ids: frozenset[str]
    ) -> tuple[BackendEvent, ...]:
        del session_id
        self.reads += 1
        if self.stall:
            self.reading.set()
            assert self.release.wait(_WAIT)
        return tuple(
            event for event in self.observed if event.source_event_id not in known_source_event_ids
        )


@pytest.fixture
def backend() -> StallingBackend:
    return StallingBackend()


@pytest.fixture
def service(tmp_path: Path, backend: StallingBackend) -> Iterator[SessionService]:
    value = SessionService.local_default(tmp_path, worker_project_id=_PROJECT, backend=backend)
    try:
        yield value
    finally:
        backend.release.set()
        value.close()


def _activate(service: SessionService) -> WorkerAttemptLease:
    intent = service.prepare_worker_attempt(
        image_digest=_IMAGE, sandbox_policy_sha256="b" * 64, input_manifest_sha256="c" * 64
    )
    lease = service.bind_worker_runtime(
        attempt_id=intent.attempt_id,
        runtime=WorkerRuntimeBinding.model_validate(
            {
                "sandbox_id": "synthetic-worker",
                "generation": "generation-1",
                "endpoint": "http://worker.test",
                "image_digest": _IMAGE,
                "sandbox_policy_sha256": "b" * 64,
            }
        ),
    )
    # The first read opens the backend under the command lock; later reads do not hold it.
    assert service.reconcile() == ()
    return lease


def _revocation(lease: WorkerAttemptLease) -> WorkerRevocationEvidence:
    return WorkerRevocationEvidence(
        attempt_id=lease.intent.attempt_id,
        sandbox_id="synthetic-worker",
        observer_id="synthetic-control-plane",
        evidence_sha256="d" * 64,
        outcome="revoked",
    )


def _command(service: SessionService, kind: CommandKind, **payload: str) -> SessionCommand:
    return SessionCommand(
        command_id=str(uuid4()),
        session_id=service.store.session_id,
        kind=kind,
        created_at="2026-01-01T00:00:00Z",
        payload=dict(payload),
    )


def _late_message() -> BackendAgentMessageEvent:
    return BackendAgentMessageEvent(message="Synthetic late state", source_event_id="late-1")


def _messages(service: SessionService) -> list[str]:
    return [
        str(event.payload["source_event_id"])
        for event in service.replay_events()
        if event.kind == EventKind.AGENT_MESSAGE_EMITTED and "source_event_id" in event.payload
    ]


@pytest.mark.parametrize("control", ["fence", "revoke"])
def test_stalled_read_delays_neither_fencing_nor_revocation_and_is_discarded(
    service: SessionService, backend: StallingBackend, control: str
) -> None:
    lease = _activate(service)
    backend.observed = (_late_message(),)
    backend.stall = True
    with ThreadPoolExecutor(max_workers=2) as executor:
        read = executor.submit(service.reconcile)
        assert backend.reading.wait(_WAIT)
        act: Callable[[], object] = (
            (lambda: service.fence_worker_attempt(attempt_id=lease.intent.attempt_id))
            if control == "fence"
            else (lambda: service.revoke_worker(lambda _record: _revocation(lease)))
        )
        try:
            executor.submit(act).result(timeout=_WAIT / 2)
            assert not lease.active
            attempt = service.worker_attempt()
            assert attempt is not None
            assert attempt.phase == ("fenced" if control == "fence" else "revoked")
        finally:
            backend.release.set()
        assert read.result(timeout=_WAIT) == ()
    assert _messages(service) == []


def test_prompt_revocation_during_a_stalled_read_completes_and_discards_it(
    service: SessionService, backend: StallingBackend
) -> None:
    lease = _activate(service)
    revoked = Event()

    def revoke_promptly() -> None:
        def revoke() -> None:
            service.revoke_worker(lambda _record: _revocation(lease))
            revoked.set()

        Thread(target=revoke, daemon=True).start()

    lease.on_fenced(revoke_promptly)
    backend.observed = (_late_message(),)
    backend.stall = True
    with ThreadPoolExecutor(max_workers=1) as executor:
        read = executor.submit(service.reconcile)
        assert backend.reading.wait(_WAIT)
        try:
            # A transport fences without session locks; revocation then needs the command lock.
            lease.fence()
            assert revoked.wait(_WAIT / 2)
        finally:
            backend.release.set()
        assert read.result(timeout=_WAIT) == ()
    attempt = service.worker_attempt()
    assert attempt is not None
    assert attempt.phase == "revoked"
    assert _messages(service) == []


def test_back_to_back_reads_do_not_starve_revocation(service: SessionService) -> None:
    lease = _activate(service)
    revoked = Event()

    def revoke() -> None:
        service.revoke_worker(lambda _record: _revocation(lease))
        revoked.set()

    lock = service._command_lock
    with ThreadPoolExecutor(max_workers=1) as executor:
        lock.acquire()
        try:
            revoking = executor.submit(revoke)
            deadline = monotonic() + _WAIT
            # Like reads that follow one another, replay the journal under the command lock,
            # then release it and request it again at once.
            while not revoked.is_set() and monotonic() < deadline:
                service.replay_events()
                lock.release()
                lock.acquire()
            starved = not revoked.is_set()
        finally:
            lock.release()
        revoking.result(timeout=_WAIT)
    assert not starved
    attempt = service.worker_attempt()
    assert attempt is not None
    assert attempt.phase == "revoked"


def test_read_is_committed_once_when_nothing_interleaves(
    service: SessionService, backend: StallingBackend
) -> None:
    _activate(service)
    backend.observed = (_late_message(),)
    assert [event.kind for event in service.reconcile()] == [EventKind.AGENT_MESSAGE_EMITTED]
    assert service.reconcile() == ()
    assert _messages(service) == ["late-1"]


def test_command_during_a_stalled_read_is_not_delayed_and_the_read_is_repeated(
    service: SessionService, backend: StallingBackend
) -> None:
    _activate(service)
    backend.observed = (_late_message(),)
    backend.stall = True
    with ThreadPoolExecutor(max_workers=2) as executor:
        read = executor.submit(service.reconcile)
        assert backend.reading.wait(_WAIT)
        reads = backend.reads
        try:
            pause = executor.submit(
                service.handle,
                _command(service, CommandKind.PAUSE),
                reconcile_before_command=False,
            ).result(timeout=_WAIT / 2)
        finally:
            backend.release.set()
        # The journal advanced under the read, so its observation was read again.
        assert [event.kind for event in read.result(timeout=_WAIT)] == [
            EventKind.AGENT_MESSAGE_EMITTED
        ]
    assert backend.reads == reads + 1
    events = service.replay_events()
    assert pause.events[-1].sequence < next(
        event.sequence for event in events if event.kind == EventKind.AGENT_MESSAGE_EMITTED
    )
    assert _messages(service) == ["late-1"]


def test_event_journaled_by_its_live_callback_during_a_read_is_not_repeated(
    service: SessionService, backend: StallingBackend
) -> None:
    _activate(service)
    backend.observed = (_late_message(),)
    backend.stall = True
    with ThreadPoolExecutor(max_workers=1) as executor:
        read = executor.submit(service.reconcile)
        assert backend.reading.wait(_WAIT)
        try:
            backend._event_sink((_late_message(),))
        finally:
            backend.release.set()
        assert read.result(timeout=_WAIT) == ()
    assert _messages(service) == ["late-1"]


def test_closing_during_a_stalled_read_discards_it(
    service: SessionService, backend: StallingBackend
) -> None:
    _activate(service)
    backend.observed = (_late_message(),)
    backend.stall = True
    with ThreadPoolExecutor(max_workers=2) as executor:
        read = executor.submit(service.reconcile)
        assert backend.reading.wait(_WAIT)
        try:
            executor.submit(service.close).result(timeout=_WAIT / 2)
        finally:
            backend.release.set()
        with pytest.raises(RuntimeError, match="closed"):
            read.result(timeout=_WAIT)
    assert _messages(service) == []


def test_read_answered_by_a_closing_backend_is_discarded(
    service: SessionService, backend: StallingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _activate(service)
    backend.observed = (_late_message(),)
    backend.stall = True
    closing, closed = Event(), Event()

    def close() -> None:
        closing.set()
        assert closed.wait(_WAIT)

    monkeypatch.setattr(backend, "close", close)
    with ThreadPoolExecutor(max_workers=2) as executor:
        read = executor.submit(service.reconcile)
        assert backend.reading.wait(_WAIT)
        stopping = executor.submit(service.close)
        assert closing.wait(_WAIT)
        try:
            backend.release.set()
            assert read.result(timeout=_WAIT) == ()
        finally:
            closed.set()
        stopping.result(timeout=_WAIT)
    assert _messages(service) == []


def test_concurrent_approval_reconcile_and_stop_keep_commands_atomic(
    service: SessionService,
) -> None:
    lease = _activate(service)
    stop = Event()

    def reconcile() -> None:
        while not stop.is_set():
            try:
                service.reconcile()
            except RuntimeError:
                return

    def review() -> list[SessionCommand]:
        handled = []
        for _ in range(4):
            task = _command(service, CommandKind.CHAT, prompt="Synthetic task")
            requested = service.handle(task).events
            handled.append(task)
            group = next(
                (
                    event.payload["request"]["group_id"]  # type: ignore[call-overload, index]
                    for event in requested
                    if event.kind == EventKind.CONFIRMATION_REQUESTED
                ),
                None,
            )
            if group is None:
                break
            approval = _command(
                service, CommandKind.APPROVE, target_type="action-set", target_id=str(group)
            )
            service.handle(approval)
            handled.append(approval)
        return handled

    with ThreadPoolExecutor(max_workers=4) as executor:
        readers = [executor.submit(reconcile) for _ in range(2)]
        reviewing = executor.submit(review)
        try:
            handled = reviewing.result(timeout=_WAIT * 3)
            executor.submit(service.revoke_worker, lambda _record: _revocation(lease)).result(
                timeout=_WAIT
            )
            executor.submit(service.close).result(timeout=_WAIT)
        finally:
            stop.set()
        for reader in readers:
            reader.result(timeout=_WAIT)

    assert len(handled) > 2
    events = service.store.replay_events()
    service.audit_log.verify()
    received = {
        str(event.payload["command_id"]): event.sequence
        for event in events
        if event.kind == EventKind.COMMAND_RECEIVED
    }
    spans = []
    for command in handled:
        record = service.store.command_record(command.command_id)
        assert record is not None
        assert record["state"] == "completed"
        assert record["first_sequence"] == received[command.command_id]
        spans.append((record["first_sequence"], record["last_sequence"]))
    # No command's accepted, journaled and completed span contains another command.
    assert all(not first < other <= last for first, last in spans for other in received.values())
