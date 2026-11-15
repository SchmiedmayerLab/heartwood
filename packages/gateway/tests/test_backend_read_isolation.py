# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""One session's backend read holds no gateway-wide lock."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from heartwood.core_adapter import BackendEvent, DeterministicAgentBackend, SessionService
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.session import CommandKind, SessionCommand

_WAIT = 10.0


class StallingBackend(DeterministicAgentBackend):
    """Answer reads like a worker that can be made to stall."""

    def __init__(self) -> None:
        super().__init__()
        self.stall = False
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


class _Gateway:
    def __init__(self, workspace: Path) -> None:
        self.backends: dict[str, list[StallingBackend]] = {}
        self.gateway = SessionGateway(
            project=ProjectContext(workspace),
            env={},
            backend_id="deterministic",
            service_factory=self._service,
        )

    def _service(self, root: Path, session_id: str) -> SessionService:
        backend = StallingBackend()
        self.backends.setdefault(session_id, []).append(backend)
        return SessionService.local_default(root, session_id=session_id, backend=backend)

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
