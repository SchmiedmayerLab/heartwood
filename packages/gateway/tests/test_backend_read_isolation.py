# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""One session's backend read holds no gateway-wide lock."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any, cast
from uuid import uuid4

import pytest

from heartwood.core_adapter import BackendEvent, DeterministicAgentBackend, SessionService
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.session import CommandKind, EventKind, SessionCommand

_WAIT = 10.0


class StallingBackend(DeterministicAgentBackend):
    """Answer reads and turns like a worker that can be made to stall."""

    def __init__(self, *, stall: bool = False) -> None:
        super().__init__()
        self.stall = stall
        self.stall_turn = False
        self.reading = Event()
        self.release = Event()

    def reconcile(
        self, *, session_id: str, known_source_event_ids: frozenset[str]
    ) -> tuple[BackendEvent, ...]:
        if self.stall:
            self.reading.set()
            assert self.release.wait(_WAIT)
        return super().reconcile(
            session_id=session_id, known_source_event_ids=known_source_event_ids
        )

    def submit_turn(self, *, session_id: str, prompt: str) -> tuple[BackendEvent, ...]:
        if self.stall_turn:
            self.reading.set()
            assert self.release.wait(_WAIT)
        return super().submit_turn(session_id=session_id, prompt=prompt)


class _Owner:
    def __init__(self, service: SessionService) -> None:
        self.close = service.close


class _Gateway:
    def __init__(self, workspace: Path) -> None:
        self.backends: dict[str, list[StallingBackend]] = {}
        # The session whose next service stalls from its first read on.
        self.stalled: str | None = None
        self.workers = False
        self.gateway = SessionGateway(
            project=ProjectContext(workspace),
            env={},
            backend_id="deterministic",
            service_factory=self._service,
        )

    def _service(self, root: Path, session_id: str) -> SessionService:
        backend = StallingBackend(stall=session_id == self.stalled)
        self.backends.setdefault(session_id, []).append(backend)
        service = SessionService.local_default(root, session_id=session_id, backend=backend)
        if self.workers:
            # The gateway treats a service with a managed owner as one a worker answers.
            self.gateway._worker_sessions[id(service)] = cast(Any, _Owner(service))
        return service

    def command(self, session_id: str, kind: CommandKind, **payload: str) -> SessionCommand:
        return SessionCommand(
            command_id=str(uuid4()),
            session_id=session_id,
            kind=kind,
            actor_id="synthetic-user",
            created_at="2026-01-01T00:00:00Z",
            payload=dict(payload),
        )

    def serve(self, title: str) -> tuple[str, StallingBackend]:
        """Create a session whose service has opened its backend and awaits review."""
        session_id = str(self.gateway.create_session(title)["session_id"])
        self.gateway.handle(self.command(session_id, CommandKind.CHAT, prompt="Synthetic task"))
        return session_id, self.backends[session_id][-1]


@pytest.fixture
def served(tmp_path: Path) -> Iterator[_Gateway]:
    value = _Gateway(tmp_path)
    try:
        yield value
    finally:
        for backends in value.backends.values():
            for backend in backends:
                backend.release.set()
        value.gateway.stop()


@pytest.mark.parametrize("blocked", ["projection", "stream", "command", "idle-wait"])
def test_stalled_session_read_blocks_no_other_session_or_settings(
    served: _Gateway, blocked: str
) -> None:
    gateway = served.gateway
    stalled, backend = served.serve("Stalled")
    other, _other_backend = served.serve("Other")
    operations: dict[str, Callable[[], object]] = {
        "projection": lambda: gateway.session_projection(session_id=stalled),
        "stream": lambda: gateway.open_event_stream(session_id=stalled)[0].close(),
        "command": lambda: gateway.handle(served.command(stalled, CommandKind.PAUSE)),
        "idle-wait": lambda: gateway.wait_for_session_idle(session_id=stalled),
    }
    backend.stall = True
    with ThreadPoolExecutor(max_workers=2) as executor:
        reading = executor.submit(operations[blocked])
        assert backend.reading.wait(_WAIT)
        try:
            for unaffected in (
                lambda: gateway.session_projection(session_id=other),
                lambda: gateway.handle(served.command(other, CommandKind.PAUSE)),
                lambda: gateway.open_event_stream(session_id=other)[0].close(),
                gateway.action_settings,
                gateway.sessions,
            ):
                executor.submit(unaffected).result(timeout=_WAIT / 2)
        finally:
            backend.release.set()
        reading.result(timeout=_WAIT)


def test_service_replaced_during_a_stalled_read_is_read_again(served: _Gateway) -> None:
    gateway = served.gateway
    stalled, backend = served.serve("Stalled")
    backend.stall = True
    with ThreadPoolExecutor(max_workers=2) as executor:
        reading = executor.submit(gateway.session_projection, session_id=stalled)
        assert backend.reading.wait(_WAIT)
        try:
            executor.submit(gateway._reset_services).result(timeout=_WAIT / 2)
        finally:
            backend.release.set()
        projection = reading.result(timeout=_WAIT)
    assert projection.session_id == stalled
    assert len(served.backends[stalled]) == 2
    assert gateway._services[stalled].backend is served.backends[stalled][-1]


def test_replacing_worker_service_is_not_first_read_under_the_lock(served: _Gateway) -> None:
    gateway = served.gateway
    served.workers = True
    stalled, backend = served.serve("Stalled")
    backend.stall = True
    with ThreadPoolExecutor(max_workers=2) as executor:
        reading = executor.submit(gateway.session_projection, session_id=stalled)
        assert backend.reading.wait(_WAIT)
        try:
            executor.submit(gateway._reset_services).result(timeout=_WAIT / 2)
        finally:
            backend.release.set()
        assert reading.result(timeout=_WAIT).session_id == stalled
    replacement = gateway._services[stalled]
    assert replacement.backend is served.backends[stalled][-1]
    # The replacement's first read, which can launch a worker, is its own next read.
    assert not _opened(replacement)
    gateway.session_projection(session_id=stalled)
    assert _opened(replacement)


def _opened(service: SessionService) -> bool:
    return service.backend_opened


def _unaffected(served: _Gateway, other: str) -> tuple[Callable[[], object], ...]:
    gateway = served.gateway
    return (
        lambda: gateway.session_projection(session_id=other),
        lambda: gateway.handle(served.command(other, CommandKind.PAUSE)),
        lambda: gateway.open_event_stream(session_id=other)[0].close(),
        gateway.action_settings,
        gateway.sessions,
    )


@pytest.mark.parametrize("opened_by", ["command", "projection"])
def test_stalled_first_read_blocks_no_other_session_or_settings(
    served: _Gateway, opened_by: str
) -> None:
    gateway = served.gateway
    served.workers = True
    other, _other_backend = served.serve("Other")
    if opened_by == "command":
        stalled = str(gateway.create_session("Stalled")["session_id"])
        first: Callable[[], object] = lambda: gateway.handle(  # noqa: E731
            served.command(stalled, CommandKind.CHAT, prompt="Synthetic task")
        )
    else:
        # A session awaiting review after a gateway restart is opened by its next projection.
        stalled, _backend = served.serve("Stalled")
        gateway._reset_services()
        first = lambda: gateway.session_projection(session_id=stalled)  # noqa: E731
    served.stalled = stalled
    with ThreadPoolExecutor(max_workers=2) as executor:
        opening = executor.submit(first)
        backend = _wait_for_backend(served, stalled)
        assert backend.reading.wait(_WAIT)
        assert not gateway._services[stalled].backend_opened
        try:
            for unaffected in _unaffected(served, other):
                executor.submit(unaffected).result(timeout=_WAIT / 2)
        finally:
            backend.release.set()
        opening.result(timeout=_WAIT)
    assert gateway._services[stalled].backend_opened


def test_stalled_command_request_blocks_no_other_session_or_settings(served: _Gateway) -> None:
    gateway = served.gateway
    served.workers = True
    stalled, backend = served.serve("Stalled")
    other, _other_backend = served.serve("Other")
    group = next(
        str(event.payload["request"]["group_id"])  # type: ignore[call-overload, index]
        for event in gateway.session_snapshot(session_id=stalled).events
        if event.kind == EventKind.CONFIRMATION_REQUESTED
    )
    gateway.handle(
        served.command(stalled, CommandKind.APPROVE, target_type="action-set", target_id=group)
    )
    backend.stall_turn = True
    with ThreadPoolExecutor(max_workers=2) as executor:
        turn = executor.submit(
            gateway.handle, served.command(stalled, CommandKind.CHAT, prompt="Another task")
        )
        assert backend.reading.wait(_WAIT)
        try:
            for unaffected in _unaffected(served, other):
                executor.submit(unaffected).result(timeout=_WAIT / 2)
        finally:
            backend.release.set()
        result = turn.result(timeout=_WAIT)
    assert any(event.kind == EventKind.CONFIRMATION_REQUESTED for event in result.events)


def _wait_for_backend(served: _Gateway, session_id: str) -> StallingBackend:
    for _ in range(int(_WAIT / 0.01)):
        backends = served.backends.get(session_id, [])
        if backends and backends[-1].stall:
            return backends[-1]
        time.sleep(0.01)
    raise AssertionError("the session's service was not created")
