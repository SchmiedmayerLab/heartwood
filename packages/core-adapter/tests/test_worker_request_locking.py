# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""A session's first read and its commands' worker requests hold no session lock."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest

from heartwood.core_adapter import (
    BackendAgentMessageEvent,
    BackendErrorCode,
    BackendEvent,
    DeterministicAgentBackend,
    PendingActionGroup,
    SessionService,
)
from heartwood.core_adapter.worker_attempts import (
    WorkerAttemptLease,
    WorkerRevocationEvidence,
    WorkerRuntimeBinding,
)
from heartwood.session import CommandKind, EventKind, SessionCommand

_PROJECT = UUID("6f9d1a4e-0b9c-4f7e-9d43-2b7c1e5a8f10")
_IMAGE = "sha256:" + "a" * 64
_WAIT = 10.0


class BlockingBackend(DeterministicAgentBackend):
    """Answer like a worker whose next request to one chosen method stalls until released."""

    def __init__(self) -> None:
        super().__init__()
        self.block: str | None = None
        self.entered = Event()
        self.release = Event()
        self.calls: Counter[str] = Counter()

    def _stall(self, name: str) -> None:
        self.calls[name] += 1
        if self.block == name:
            self.block = None
            self.entered.set()
            assert self.release.wait(_WAIT)

    def reconcile(
        self, *, session_id: str, known_source_event_ids: frozenset[str]
    ) -> tuple[BackendEvent, ...]:
        del session_id
        self._stall("reconcile")
        late = BackendAgentMessageEvent(message="Synthetic late state", source_event_id="late-1")
        return () if late.source_event_id in known_source_event_ids else (late,)

    def pending_action_group(self, *, session_id: str) -> PendingActionGroup | None:
        group = super().pending_action_group(session_id=session_id)
        self._stall("pending_action_group")
        return group

    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
        self._stall("submit_turn")
        return super().submit_turn(session_id=session_id, prompt=prompt)

    def resolve_confirmation(
        self, *, session_id: str, action_group_id: str, approved: bool
    ) -> tuple[BackendEvent, ...]:
        self._stall("resolve_confirmation")
        return super().resolve_confirmation(
            session_id=session_id, action_group_id=action_group_id, approved=approved
        )

    def pause(self, *, session_id: str) -> tuple[BackendEvent, ...]:
        del session_id
        self._stall("pause")
        return (BackendAgentMessageEvent(message="Synthetic pause", source_event_id="paused-1"),)

    def resume(self, *, session_id: str) -> tuple[BackendEvent, ...]:
        del session_id
        self._stall("resume")
        return (BackendAgentMessageEvent(message="Synthetic resume", source_event_id="resumed-1"),)


@pytest.fixture
def backend() -> BlockingBackend:
    return BlockingBackend()


@pytest.fixture
def service(tmp_path: Path, backend: BlockingBackend) -> Iterator[SessionService]:
    value = SessionService.local_default(tmp_path, worker_project_id=_PROJECT, backend=backend)
    try:
        yield value
    finally:
        backend.release.set()
        value.close()


def _bind(service: SessionService) -> WorkerAttemptLease:
    intent = service.prepare_worker_attempt(
        image_digest=_IMAGE, sandbox_policy_sha256="b" * 64, input_manifest_sha256="c" * 64
    )
    return service.bind_worker_runtime(
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


def _control(service: SessionService, lease: WorkerAttemptLease, control: str) -> None:
    if control == "fence":
        service.fence_worker_attempt(attempt_id=lease.intent.attempt_id)
    else:
        service.revoke_worker(lambda _record: _revocation(lease))


def _opened(service: SessionService) -> bool:
    return service.backend_opened


def _request_group(service: SessionService) -> str:
    events = service.handle(_command(service, CommandKind.CHAT, prompt="Synthetic task")).events
    return next(
        str(event.payload["request"]["group_id"])  # type: ignore[call-overload, index]
        for event in events
        if event.kind == EventKind.CONFIRMATION_REQUESTED
    )


@pytest.mark.parametrize("control", ["fence", "revoke"])
def test_stalled_first_read_delays_neither_fencing_nor_revocation_and_is_discarded(
    service: SessionService, backend: BlockingBackend, control: str
) -> None:
    lease = _bind(service)
    backend.block = "reconcile"
    with ThreadPoolExecutor(max_workers=2) as executor:
        read = executor.submit(service.reconcile)
        assert backend.entered.wait(_WAIT)
        assert not _opened(service)
        try:
            executor.submit(_control, service, lease, control).result(timeout=_WAIT / 2)
            attempt = service.worker_attempt()
            assert attempt is not None
            assert attempt.phase == ("fenced" if control == "fence" else "revoked")
        finally:
            backend.release.set()
        assert read.result(timeout=_WAIT) == ()
    assert service.backend_opened
    assert not any(
        event.kind == EventKind.AGENT_MESSAGE_EMITTED for event in service.replay_events()
    )


def _stalled_command(service: SessionService, request: str) -> SessionCommand:
    """Return a command that sends ``request`` to the worker."""
    if request in {"pending_action_group", "submit_turn"}:
        return _command(service, CommandKind.CHAT, prompt="Synthetic task")
    if request == "resolve_confirmation":
        group = _request_group(service)
        return _command(service, CommandKind.APPROVE, target_type="action-set", target_id=group)
    return _command(service, CommandKind.PAUSE if request == "pause" else CommandKind.RESUME)


@pytest.mark.parametrize("control", ["fence", "revoke"])
@pytest.mark.parametrize(
    "request_name",
    ["pending_action_group", "submit_turn", "resolve_confirmation", "pause", "resume"],
)
def test_stalled_command_request_delays_neither_fencing_nor_revocation_and_is_discarded(
    service: SessionService, backend: BlockingBackend, request_name: str, control: str
) -> None:
    lease = _bind(service)
    service.reconcile()
    command = _stalled_command(service, request_name)
    calls = backend.calls[request_name]
    backend.block = request_name
    with ThreadPoolExecutor(max_workers=2) as executor:
        handled = executor.submit(service.handle, command)
        assert backend.entered.wait(_WAIT)
        try:
            executor.submit(_control, service, lease, control).result(timeout=_WAIT / 2)
        finally:
            backend.release.set()
        result = handled.result(timeout=_WAIT)
    assert backend.calls[request_name] == calls + 1
    recorded = service.replay_events()
    first = result.events[0].sequence
    last = result.events[-1].sequence
    # The fence was journaled while the command waited; nothing the worker answered was.
    assert any(
        event.kind == EventKind.WORKER_ATTEMPT_RECORDED and first < event.sequence < last
        for event in recorded
    )
    own = [event for event in recorded if first <= event.sequence <= last]
    assert not any(
        event.kind
        in {
            EventKind.AGENT_MESSAGE_EMITTED,
            EventKind.TOOL_CALL_PROPOSED,
            EventKind.CONFIRMATION_REQUESTED,
            EventKind.TOOL_EXECUTION_RECORDED,
        }
        for event in own
    )
    codes = {event.payload.get("code") for event in own if event.kind == EventKind.ERROR_RECORDED}
    assert codes & {
        BackendErrorCode.WORKER_STOPPED.value,
        BackendErrorCode.ACTION_OUTCOME_UNKNOWN.value,
    }
    assert service.store.unresolved_command_ids() == ()
    # The completed receipt replays the same result; the request is not sent again.
    again = service.handle(command)
    assert again.replayed
    assert [event.sequence for event in again.events] == [event.sequence for event in own]
    assert backend.calls[request_name] == calls + 1


def test_writers_wait_for_a_command_whose_request_stalls(
    service: SessionService, backend: BlockingBackend
) -> None:
    _bind(service)
    service.reconcile()
    task = _command(service, CommandKind.CHAT, prompt="Synthetic task")
    backend.block = "submit_turn"
    with ThreadPoolExecutor(max_workers=4) as executor:
        handled = executor.submit(service.handle, task)
        assert backend.entered.wait(_WAIT)
        callback = executor.submit(
            backend._event_sink,
            (BackendAgentMessageEvent(message="Live progress", source_event_id="live-1"),),
        )
        retried = executor.submit(service.handle, task)
        reading = executor.submit(service.reconcile)
        try:
            for future in (callback, retried, reading):
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.2)
            assert service.worker_attempt() is not None
        finally:
            backend.release.set()
        result = handled.result(timeout=_WAIT)
        callback.result(timeout=_WAIT)
        repeated = retried.result(timeout=_WAIT)
        reading.result(timeout=_WAIT)
    assert backend.calls["submit_turn"] == 1
    assert repeated.replayed
    first, last = result.events[0].sequence, result.events[-1].sequence
    assert [event.sequence for event in repeated.events] == list(range(first, last + 1))
    live = next(
        event
        for event in service.replay_events()
        if event.payload.get("source_event_id") == "live-1"
    )
    assert live.sequence > last
    assert any(event.kind == EventKind.CONFIRMATION_REQUESTED for event in result.events)


def test_close_waits_for_the_command_in_progress_and_new_commands_are_refused(
    service: SessionService, backend: BlockingBackend
) -> None:
    _bind(service)
    service.reconcile()
    backend.block = "submit_turn"
    with ThreadPoolExecutor(max_workers=3) as executor:
        handled = executor.submit(
            service.handle, _command(service, CommandKind.CHAT, prompt="Synthetic task")
        )
        assert backend.entered.wait(_WAIT)
        closing = executor.submit(service.close)
        with pytest.raises(TimeoutError):
            closing.result(timeout=0.2)
        backend.release.set()
        result = handled.result(timeout=_WAIT)
        closing.result(timeout=_WAIT)
    assert any(event.kind == EventKind.CONFIRMATION_REQUESTED for event in result.events)
    assert service.store.unresolved_command_ids() == ()
    with pytest.raises(RuntimeError, match="closed"):
        service.handle(_command(service, CommandKind.CHAT, prompt="Another task"))
