# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session gateway orchestration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from heartwood.core_adapter import SessionResult, SessionService
from heartwood.gateway._agent_server import ManagedAgentServer
from heartwood.gateway._stream import EventStreamHub, GatewayEventStream
from heartwood.session import SessionCommand, SessionEvent

SessionServiceFactory = Callable[[Path, str], SessionService]


class SessionGateway:
    """Own session services, event streams, and the managed agent-server boundary."""

    def __init__(
        self,
        *,
        workspace: Path,
        service_factory: SessionServiceFactory | None = None,
        agent_server: ManagedAgentServer | None = None,
    ) -> None:
        self.workspace = workspace
        self.agent_server = ManagedAgentServer() if agent_server is None else agent_server
        self._service_factory = service_factory or _default_service_factory
        self._services: dict[str, SessionService] = {}
        self._streams = EventStreamHub()

    def start(self) -> None:
        """Start managed gateway dependencies."""
        self.agent_server.start()

    def stop(self) -> None:
        """Stop managed gateway dependencies."""
        self.agent_server.stop()

    def handle(self, command: SessionCommand) -> SessionResult:
        """Handle one command and publish emitted events."""
        service = self._service(command.session_id)
        result = service.handle(command)
        self._streams.publish(session_id=command.session_id, events=result.events)
        return result

    def replay_events(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> tuple[SessionEvent, ...]:
        """Replay persisted events for a session."""
        events = self._service(session_id).replay_events()
        if after_sequence is None:
            return events
        return tuple(event for event in events if event.sequence > after_sequence)

    def websocket(
        self,
        *,
        session_id: str,
        after_sequence: int | None = None,
    ) -> GatewayEventStream:
        """Connect a WebSocket-style event stream with replay."""
        return self._streams.connect(
            session_id=session_id,
            replay_events=self.replay_events(
                session_id=session_id,
                after_sequence=after_sequence,
            ),
        )

    def _service(self, session_id: str) -> SessionService:
        service = self._services.get(session_id)
        if service is None:
            service = self._service_factory(self.workspace, session_id)
            self._services[session_id] = service
        return service


def _default_service_factory(workspace: Path, session_id: str) -> SessionService:
    return SessionService.local_default(workspace, session_id=session_id)
