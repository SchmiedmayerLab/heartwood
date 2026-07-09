# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for managed agent-server binding and backend translation."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from heartwood.adapters.data import LocalFilesystemDataSourceAdapter
from heartwood.adapters.model import FakeLocalModelProviderAdapter
from heartwood.adapters.platform import GenericPlatformAdapter
from heartwood.core_adapter import BackendEventKind, FileSessionStore, SessionService
from heartwood.gateway import (
    AgentServerBindingError,
    AgentServerConfig,
    AgentServerEvent,
    AgentServerUnavailableError,
    DirectAgentServerAccessError,
    ManagedAgentServer,
    OpenHandsAgentServerBackend,
    SessionGateway,
)
from heartwood.session import CommandKind, EventKind, SessionCommand


class _FakeProcess:
    def __init__(self) -> None:
        self.terminated = False

    def poll(self) -> int | None:
        return None if not self.terminated else 0

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        assert timeout is None or timeout >= 0
        return 0


class _FakeClient:
    def chat_turn(self, *, session_id: str, prompt_length: int) -> Sequence[AgentServerEvent]:
        return (
            AgentServerEvent(
                kind="message",
                payload={"content": f"chat:{session_id}:{prompt_length}"},
            ),
        )

    def run_turn(
        self, *, session_id: str, prompt_length: int, approved: bool
    ) -> Sequence[AgentServerEvent]:
        return (
            AgentServerEvent(kind="message", payload={"content": "planned"}),
            AgentServerEvent(
                kind="tool_call",
                payload={
                    "tool_call_id": f"{session_id}-toolcall-0",
                    "tool_name": "heartwood.synthetic.noop",
                    "risk": "low",
                    "summary": "run no-op",
                },
            ),
            AgentServerEvent(
                kind="confirmation",
                payload={
                    "tool_call_id": f"{session_id}-toolcall-0",
                    "tool_name": "heartwood.synthetic.noop",
                    "risk": "low",
                    "summary": "run no-op",
                    "approved": approved,
                },
            ),
            AgentServerEvent(
                kind="tool_result",
                payload={
                    "tool_name": "heartwood.synthetic.noop",
                    "exit_code": 0 if approved else 1,
                    "summary": f"done after {prompt_length}",
                },
            ),
        )


def _command(
    kind: CommandKind,
    *,
    session_id: str = "session-1",
    prompt: str = "",
) -> SessionCommand:
    return SessionCommand(
        command_id=f"{session_id}-{kind.value}",
        session_id=session_id,
        kind=kind,
        actor_id="synthetic-user",
        created_at="2026-01-01T00:00:00Z",
        payload={"prompt": prompt},
    )


def _openhands_service_factory(workspace: Path, session_id: str) -> SessionService:
    platform = GenericPlatformAdapter()
    policy = platform.default_policy_profile()
    return SessionService(
        store=FileSessionStore(workspace, session_id),
        platform_adapter=platform,
        data_source_adapter=LocalFilesystemDataSourceAdapter.synthetic_omop(),
        model_provider_adapter=FakeLocalModelProviderAdapter(policy),
        backend=OpenHandsAgentServerBackend(_FakeClient()),
        env={},
        clock=lambda: "2026-01-01T00:00:00Z",
    )


def test_agent_server_rejects_non_local_bindings() -> None:
    with pytest.raises(AgentServerBindingError):
        AgentServerConfig(host="0.0.0.0").validate()


def test_agent_server_rejects_non_local_runtime() -> None:
    with pytest.raises(AgentServerBindingError):
        AgentServerConfig(runtime="docker").validate()


def test_enabled_agent_server_requires_explicit_port() -> None:
    with pytest.raises(AgentServerBindingError):
        AgentServerConfig(command=("agent-server", "--local"), enabled=True).validate()


def test_disabled_agent_server_does_not_spawn_process() -> None:
    spawned = False

    def process_factory(command: tuple[str, ...]) -> _FakeProcess:
        nonlocal spawned
        assert command == ()
        spawned = True
        return _FakeProcess()

    server = ManagedAgentServer(process_factory=process_factory)
    status = server.start()

    assert status.enabled is False
    assert status.running is False
    assert spawned is False


def test_session_gateway_starts_and_stops_managed_agent_server(tmp_path: Path) -> None:
    process = _FakeProcess()

    def process_factory(command: tuple[str, ...]) -> _FakeProcess:
        assert command == ("agent-server", "--local")
        return process

    server = ManagedAgentServer(
        AgentServerConfig(command=("agent-server", "--local"), port=8765, enabled=True),
        process_factory=process_factory,
    )
    gateway = SessionGateway(workspace=tmp_path, agent_server=server)

    gateway.start()

    assert server.status().running is True
    gateway.stop()
    assert process.terminated is True


def test_enabled_agent_server_is_managed_and_gateway_only() -> None:
    process = _FakeProcess()

    def process_factory(command: tuple[str, ...]) -> _FakeProcess:
        assert command == ("agent-server", "--local")
        return process

    server = ManagedAgentServer(
        AgentServerConfig(command=("agent-server", "--local"), port=8765, enabled=True),
        process_factory=process_factory,
    )

    status = server.start()

    assert status.running is True
    assert status.endpoint == "http://127.0.0.1:8765"
    with pytest.raises(DirectAgentServerAccessError):
        server.endpoint_for_client()
    server.stop()
    assert process.terminated is True


def test_openhands_backend_requires_running_enabled_agent_server() -> None:
    server = ManagedAgentServer(
        AgentServerConfig(command=("agent-server", "--local"), port=8765, enabled=True)
    )
    backend = OpenHandsAgentServerBackend(_FakeClient(), server=server)

    with pytest.raises(AgentServerUnavailableError):
        backend.chat_turn(session_id="session-1", prompt_length=1)


def test_openhands_backend_translates_fake_agent_server_events() -> None:
    backend = OpenHandsAgentServerBackend(_FakeClient())

    chat_events = backend.chat_turn(session_id="session-1", prompt_length=4)
    run_events = backend.run_turn(session_id="session-1", prompt_length=3, approved=True)

    assert chat_events[0].kind == BackendEventKind.AGENT_MESSAGE
    assert [event.kind for event in run_events] == [
        BackendEventKind.AGENT_MESSAGE,
        BackendEventKind.TOOL_CALL_PROPOSED,
        BackendEventKind.CONFIRMATION,
        BackendEventKind.TOOL_EXECUTION,
    ]
    assert run_events[2].approved is True
    assert run_events[3].tool_execution is not None
    assert run_events[3].tool_execution.exit_code == 0


def test_gateway_routes_openhands_backend_events_through_session_contract(tmp_path: Path) -> None:
    gateway = SessionGateway(workspace=tmp_path, service_factory=_openhands_service_factory)
    stream = gateway.websocket(session_id="session-1")

    result = gateway.handle(_command(CommandKind.CHAT, prompt="hello"))

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.AGENT_MESSAGE_EMITTED.value,
    ]
    assert result.events[1].payload["content"] == "chat:session-1:5"
    assert [event.kind for event in stream.receive()] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.AGENT_MESSAGE_EMITTED.value,
    ]


def test_openhands_backend_rejects_unknown_event_kind() -> None:
    class BadClient(_FakeClient):
        def chat_turn(self, *, session_id: str, prompt_length: int) -> Sequence[AgentServerEvent]:
            assert session_id == "session-1"
            assert prompt_length == 1
            return (AgentServerEvent(kind="unknown", payload={}),)

    backend = OpenHandsAgentServerBackend(BadClient())

    with pytest.raises(ValueError, match="unsupported agent-server event kind"):
        backend.chat_turn(session_id="session-1", prompt_length=1)
