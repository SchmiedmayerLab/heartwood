# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Session gateway orchestration."""

from __future__ import annotations

import os
import secrets
import shlex
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from heartwood.core_adapter import AgentBackend, SessionResult, SessionService
from heartwood.gateway._agent_server import (
    AgentServerConfig,
    ManagedAgentServer,
    OpenHandsAgentServerBackend,
    OpenHandsHttpAgentServerClient,
)
from heartwood.gateway._stream import EventStreamHub, GatewayEventStream
from heartwood.session import SessionCommand, SessionEvent

SessionServiceFactory = Callable[[Path, str], SessionService]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _req: urllib.request.Request,
        _fp: object,
        _code: int,
        _msg: str,
        _headers: object,
        _newurl: str,
    ) -> None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


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
        self.agent_server = _agent_server_from_env() if agent_server is None else agent_server
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
    return SessionService.local_default(
        workspace,
        session_id=session_id,
        backend=_gateway_backend_from_env(workspace=workspace, session_id=session_id),
    )


def _agent_server_from_env() -> ManagedAgentServer:
    if not _truthy(os.environ.get("HEARTWOOD_AGENT_SERVER_ENABLED")):
        return ManagedAgentServer()
    os.environ.setdefault("HEARTWOOD_AGENT_SERVER_API_KEY", secrets.token_urlsafe(32))
    host = os.environ.get("HEARTWOOD_AGENT_SERVER_HOST", "127.0.0.1")
    port = _int_env("HEARTWOOD_AGENT_SERVER_PORT", default=8766)
    readiness_timeout = _float_env("HEARTWOOD_AGENT_SERVER_READY_TIMEOUT_SECONDS", default=60)
    command = tuple(
        shlex.split(
            os.environ.get(
                "HEARTWOOD_AGENT_SERVER_COMMAND",
                f"agent-server --host {host} --port {port}",
            )
        )
    )
    return ManagedAgentServer(
        AgentServerConfig(command=command, host=host, port=port, enabled=True),
        readiness_probe=lambda endpoint: _http_readiness_probe(
            endpoint,
            timeout_seconds=readiness_timeout,
        ),
    )


def _gateway_backend_from_env(*, workspace: Path, session_id: str) -> AgentBackend | None:
    backend_id = os.environ.get("HEARTWOOD_AGENT_BACKEND", "deterministic-local")
    if backend_id not in {"openhands-bash", "openhands-agent-server"}:
        return None
    host = os.environ.get("HEARTWOOD_AGENT_SERVER_HOST", "127.0.0.1")
    port = _int_env("HEARTWOOD_AGENT_SERVER_PORT", default=8766)
    endpoint = os.environ.get("HEARTWOOD_AGENT_SERVER_ENDPOINT", f"http://{host}:{port}")
    client = OpenHandsHttpAgentServerClient(
        endpoint=endpoint,
        api_key=os.environ.get("HEARTWOOD_AGENT_SERVER_API_KEY"),
        workspace=workspace / session_id,
    )
    return OpenHandsAgentServerBackend(client)


def _http_readiness_probe(endpoint: str, *, timeout_seconds: float = 60) -> bool:
    deadline = time.monotonic() + timeout_seconds
    ready_url = f"{endpoint}/api/tools/"
    while time.monotonic() < deadline:
        try:
            with _NO_REDIRECT_OPENER.open(ready_url, timeout=0.5):
                return True
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                time.sleep(0.2)
                continue
            return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    return False


def _truthy(value: str | None) -> bool:
    return value is not None and value.lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, *, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        msg = f"{name} must be an integer"
        raise ValueError(msg) from error


def _float_env(name: str, *, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as error:
        msg = f"{name} must be a number"
        raise ValueError(msg) from error
