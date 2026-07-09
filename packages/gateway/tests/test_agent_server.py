# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for managed agent-server binding and backend translation."""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

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
    OpenHandsHttpAgentServerClient,
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


class _OpenHandsHttpHandler(BaseHTTPRequestHandler):
    api_key = "local-session-key"
    request_log: ClassVar[list[dict[str, Any]]] = []
    workspace: ClassVar[Path | None] = None

    def do_GET(self) -> None:
        self.__class__.request_log.append(
            {"method": "GET", "path": self.path, "api_key": self.headers.get("X-Session-API-Key")}
        )
        if self.path != "/api/tools/" or self.headers.get("X-Session-API-Key") != self.api_key:
            self.send_response(404)
            self.end_headers()
            return
        self._json_response(["TerminalTool", "FileEditorTool"])

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.request_log.append(
            {
                "method": "POST",
                "path": self.path,
                "api_key": self.headers.get("X-Session-API-Key"),
                "payload": payload,
            }
        )
        if (
            self.path != "/api/bash/execute_bash_command"
            or self.headers.get("X-Session-API-Key") != self.api_key
        ):
            self.send_response(404)
            self.end_headers()
            return
        workspace = self.__class__.workspace
        if workspace is None:
            self.send_response(500)
            self.end_headers()
            return
        artifact = workspace / "agent-artifacts" / "synthetic-workspace-summary.md"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            "\n".join(
                (
                    "# Synthetic Workspace Summary",
                    "",
                    "- Session: `session-1`",
                    "- Prompt length: `9`",
                    "- Dataset: synthetic OMOP fixture",
                    "- Tool action: OpenHands bash workspace artifact write",
                    "- Persisted prompt content: none",
                    "",
                )
            ),
            encoding="utf-8",
        )
        self._json_response(
            {
                "command_id": "command-local-1",
                "exit_code": 0,
                "stdout": f"{artifact.as_posix()}\n",
                "stderr": "",
            }
        )

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json_response(self, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _OpenHandsRedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(302)
        self.send_header("Location", "https://public.example.invalid/api/tools/")
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _start_openhands_http_server(workspace: Path) -> tuple[HTTPServer, threading.Thread, str]:
    _OpenHandsHttpHandler.workspace = workspace
    server = HTTPServer(("127.0.0.1", 0), _OpenHandsHttpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def _start_redirect_http_server() -> tuple[HTTPServer, threading.Thread, str]:
    server = HTTPServer(("127.0.0.1", 0), _OpenHandsRedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def _stop_http_server(server: HTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    thread.join(timeout=5)
    _OpenHandsHttpHandler.workspace = None


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


def test_session_gateway_can_configure_agent_server_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEARTWOOD_AGENT_SERVER_ENABLED", "true")
    monkeypatch.setenv("HEARTWOOD_AGENT_SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("HEARTWOOD_AGENT_SERVER_PORT", "8766")
    monkeypatch.setenv(
        "HEARTWOOD_AGENT_SERVER_COMMAND",
        "bash images/generic/scripts/start_agent_server.sh",
    )

    gateway = SessionGateway(workspace=tmp_path)

    assert gateway.agent_server.config.enabled is True
    assert gateway.agent_server.config.command == (
        "bash",
        "images/generic/scripts/start_agent_server.sh",
    )
    assert gateway.agent_server.endpoint_for_gateway() == "http://127.0.0.1:8766"


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


def test_enabled_agent_server_fails_closed_when_readiness_probe_fails() -> None:
    process = _FakeProcess()

    def process_factory(command: tuple[str, ...]) -> _FakeProcess:
        assert command == ("agent-server", "--local")
        return process

    server = ManagedAgentServer(
        AgentServerConfig(command=("agent-server", "--local"), port=8765, enabled=True),
        process_factory=process_factory,
        readiness_probe=lambda endpoint: endpoint.endswith(":9999"),
    )

    with pytest.raises(AgentServerUnavailableError, match="did not become ready"):
        server.start()
    assert process.terminated is True
    assert server.status().running is False


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


def test_openhands_http_client_executes_bash_command_through_server(tmp_path: Path) -> None:
    _OpenHandsHttpHandler.request_log.clear()
    server, thread, endpoint = _start_openhands_http_server(tmp_path)
    try:
        client = OpenHandsHttpAgentServerClient(
            endpoint=endpoint,
            api_key=_OpenHandsHttpHandler.api_key,
            workspace=tmp_path,
        )
        events = client.run_turn(session_id="session-1", prompt_length=9, approved=True)
    finally:
        _stop_http_server(server, thread)

    assert [event.kind for event in events] == [
        "message",
        "tool_call",
        "confirmation",
        "tool_result",
    ]
    assert events[-1].payload["tool_name"] == "openhands.bash.execute"
    assert events[-1].payload["exit_code"] == 0
    artifact = tmp_path / "agent-artifacts" / "synthetic-workspace-summary.md"
    assert artifact.exists()
    artifact_text = artifact.read_text(encoding="utf-8")
    assert "OpenHands bash workspace artifact write" in artifact_text
    assert "Persisted prompt content: none" in artifact_text
    assert {entry["path"] for entry in _OpenHandsHttpHandler.request_log} == {
        "/api/tools/",
        "/api/bash/execute_bash_command",
    }


def test_openhands_http_client_rejects_redirects(tmp_path: Path) -> None:
    server, thread, endpoint = _start_redirect_http_server()
    try:
        client = OpenHandsHttpAgentServerClient(endpoint=endpoint, workspace=tmp_path)
        with pytest.raises(AgentServerUnavailableError, match="redirect rejected"):
            client.chat_turn(session_id="session-1", prompt_length=1)
    finally:
        _stop_http_server(server, thread)


def test_gateway_can_select_openhands_http_backend_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _OpenHandsHttpHandler.request_log.clear()
    server, thread, endpoint = _start_openhands_http_server(tmp_path / "session-1")
    monkeypatch.setenv("HEARTWOOD_AGENT_BACKEND", "openhands-bash")
    monkeypatch.setenv("HEARTWOOD_AGENT_SERVER_ENDPOINT", endpoint)
    monkeypatch.setenv("HEARTWOOD_AGENT_SERVER_API_KEY", _OpenHandsHttpHandler.api_key)
    try:
        gateway = SessionGateway(workspace=tmp_path)
        approval = SessionCommand(
            command_id="approve-model-call",
            session_id="session-1",
            kind=CommandKind.APPROVE,
            actor_id="synthetic-user",
            created_at="2026-01-01T00:00:00Z",
            payload={
                "target_type": "model-call",
                "target_id": "decision-synthetic-model-call",
            },
        )
        run = _command(CommandKind.RUN, prompt="run")
        gateway.handle(approval)
        result = gateway.handle(run)
    finally:
        _stop_http_server(server, thread)

    tool_execution = result.events[-1]
    assert tool_execution.kind == EventKind.TOOL_EXECUTION_RECORDED.value
    assert tool_execution.payload["backend_id"] == "openhands-agent-server"
    assert tool_execution.payload["tool_name"] == "openhands.bash.execute"
    assert tool_execution.payload["exit_code"] == 0
    assert (tmp_path / "session-1" / "agent-artifacts" / "synthetic-workspace-summary.md").exists()


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
