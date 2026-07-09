# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Managed local agent-server boundary and backend translation."""

from __future__ import annotations

import json
import shlex
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit

from heartwood.core_adapter import BackendEvent, BackendEventKind, ProposedToolCall, ToolExecution
from heartwood.schemas import JsonValue

RiskTier = Literal["low", "medium", "high", "unknown"]

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


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


class AgentServerBindingError(ValueError):
    """Raised when the agent-server would bind outside the local boundary."""


class AgentServerUnavailableError(RuntimeError):
    """Raised when a backend call requires an unavailable agent-server."""


class DirectAgentServerAccessError(RuntimeError):
    """Raised when code attempts to expose the managed server directly."""


class _ManagedProcess(Protocol):
    def poll(self) -> int | None:
        """Return the process exit code if it has exited."""

    def terminate(self) -> None:
        """Request process termination."""

    def wait(self, timeout: float | None = None) -> int:
        """Wait for process exit."""


ProcessFactory = Callable[[tuple[str, ...]], _ManagedProcess]
ReadinessProbe = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class AgentServerConfig:
    """Configuration for the managed local agent-server process."""

    command: tuple[str, ...] = ()
    host: str = "127.0.0.1"
    port: int = 0
    enabled: bool = False
    runtime: str = "local"

    def validate(self) -> None:
        """Validate the process boundary."""
        if self.host not in _LOCAL_HOSTS:
            msg = f"agent-server host must be localhost-only: {self.host}"
            raise AgentServerBindingError(msg)
        if not 0 <= self.port <= 65535:
            msg = f"agent-server port is out of range: {self.port}"
            raise AgentServerBindingError(msg)
        if self.runtime != "local":
            msg = f"agent-server runtime must be local: {self.runtime}"
            raise AgentServerBindingError(msg)
        if self.enabled and not self.command:
            msg = "enabled agent-server requires a command"
            raise AgentServerBindingError(msg)
        if self.enabled and self.port == 0:
            msg = "enabled agent-server requires an explicit localhost port"
            raise AgentServerBindingError(msg)


@dataclass(frozen=True, slots=True)
class AgentServerProcessStatus:
    """Status of the managed agent-server process."""

    enabled: bool
    running: bool
    endpoint: str | None


def _default_process_factory(command: tuple[str, ...]) -> _ManagedProcess:
    return subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _default_readiness_probe(endpoint: str) -> bool:
    return bool(endpoint)


class ManagedAgentServer:
    """Own the local agent-server process and keep it gateway-only."""

    def __init__(
        self,
        config: AgentServerConfig | None = None,
        *,
        process_factory: ProcessFactory = _default_process_factory,
        readiness_probe: ReadinessProbe = _default_readiness_probe,
    ) -> None:
        self.config = AgentServerConfig() if config is None else config
        self.config.validate()
        self._process_factory = process_factory
        self._readiness_probe = readiness_probe
        self._process: _ManagedProcess | None = None

    def start(self) -> AgentServerProcessStatus:
        """Start the managed process when enabled."""
        if not self.config.enabled:
            return AgentServerProcessStatus(enabled=False, running=False, endpoint=None)
        if self._process is None or self._process.poll() is not None:
            self._process = self._process_factory(self.config.command)
        endpoint = self.endpoint_for_gateway()
        if not self._readiness_probe(endpoint):
            self.stop()
            msg = f"agent-server did not become ready at {endpoint}"
            raise AgentServerUnavailableError(msg)
        return AgentServerProcessStatus(
            enabled=True,
            running=True,
            endpoint=endpoint,
        )

    def stop(self) -> None:
        """Stop the managed process if it is running."""
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            self._process.wait(timeout=5)
        self._process = None

    def status(self) -> AgentServerProcessStatus:
        """Return the current process status."""
        running = self._process is not None and self._process.poll() is None
        return AgentServerProcessStatus(
            enabled=self.config.enabled,
            running=running,
            endpoint=self.endpoint_for_gateway() if running else None,
        )

    def endpoint_for_gateway(self) -> str:
        """Return the private endpoint used only by the gateway."""
        self.config.validate()
        return f"http://{self.config.host}:{self.config.port}"

    def endpoint_for_client(self) -> str:
        """Block direct exposure of the managed server endpoint."""
        msg = "agent-server is reachable only through the session gateway"
        raise DirectAgentServerAccessError(msg)


@dataclass(frozen=True, slots=True)
class AgentServerEvent:
    """Event payload received from the local agent-server binding."""

    kind: str
    payload: Mapping[str, JsonValue]


class AgentServerClient(Protocol):
    """Client surface used by the backend adapter."""

    def chat_turn(self, *, session_id: str, prompt_length: int) -> Sequence[AgentServerEvent]:
        """Return local agent-server events for a chat turn."""

    def run_turn(
        self, *, session_id: str, prompt_length: int, approved: bool
    ) -> Sequence[AgentServerEvent]:
        """Return local agent-server events for a tool-running turn."""


class OpenHandsHttpAgentServerClient:
    """HTTP client for the gateway-owned OpenHands agent-server API."""

    def __init__(
        self,
        *,
        endpoint: str,
        workspace: Path,
        api_key: str | None = None,
        timeout: float = 10,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.workspace = workspace.resolve()
        self.api_key = api_key
        self.timeout = timeout
        _validate_agent_server_endpoint(self.endpoint)

    def chat_turn(self, *, session_id: str, prompt_length: int) -> Sequence[AgentServerEvent]:
        """Return a bounded server-capability event for a chat turn."""
        tools = self._available_tools()
        return (
            AgentServerEvent(
                kind="message",
                payload={
                    "content": (
                        "Connected to the managed OpenHands agent-server "
                        f"(session_id={session_id}, registered_tools={len(tools)}, "
                        f"prompt_length={prompt_length})."
                    )
                },
            ),
        )

    def run_turn(
        self, *, session_id: str, prompt_length: int, approved: bool
    ) -> Sequence[AgentServerEvent]:
        """Execute a bounded bash-backed workspace write through OpenHands."""
        tools = self._available_tools()
        tool_call = {
            "tool_call_id": f"{session_id}-openhands-bash-0",
            "tool_name": "openhands.bash.execute",
            "risk": "low",
            "summary": "write a synthetic workspace summary through OpenHands bash",
        }
        events: list[AgentServerEvent] = [
            AgentServerEvent(
                kind="message",
                payload={
                    "content": (
                        "Prepared an OpenHands bash execution against the local "
                        f"session workspace (registered_tools={len(tools)}, "
                        f"prompt_length={prompt_length})."
                    )
                },
            ),
            AgentServerEvent(kind="tool_call", payload=tool_call),
            AgentServerEvent(kind="confirmation", payload={**tool_call, "approved": approved}),
        ]
        if not approved:
            events.append(
                AgentServerEvent(
                    kind="tool_result",
                    payload={
                        "tool_name": tool_call["tool_name"],
                        "exit_code": 1,
                        "summary": "approval required before OpenHands bash execution",
                    },
                )
            )
            return tuple(events)
        output = self._execute_summary_command(
            session_id=session_id,
            prompt_length=prompt_length,
        )
        exit_code = _int_payload(output, "exit_code", default=1)
        command_id = str(output.get("command_id", "unknown"))
        events.append(
            AgentServerEvent(
                kind="tool_result",
                payload={
                    "tool_name": tool_call["tool_name"],
                    "exit_code": exit_code,
                    "summary": (
                        "OpenHands bash wrote synthetic workspace artifact "
                        f"(command_id={command_id})"
                    ),
                },
            )
        )
        return tuple(events)

    def _available_tools(self) -> tuple[str, ...]:
        payload = self._request_json("GET", "/api/tools/")
        if not isinstance(payload, list):
            msg = "OpenHands tools response was not a list"
            raise AgentServerUnavailableError(msg)
        return tuple(str(item) for item in payload)

    def _execute_summary_command(
        self,
        *,
        session_id: str,
        prompt_length: int,
    ) -> Mapping[str, JsonValue]:
        self.workspace.mkdir(parents=True, exist_ok=True)
        content = "\n".join(
            (
                "# Synthetic Workspace Summary",
                "",
                f"- Session: `{session_id}`",
                f"- Prompt length: `{prompt_length}`",
                "- Dataset: synthetic OMOP fixture",
                "- Tool action: OpenHands bash workspace artifact write",
                "- Persisted prompt content: none",
                "",
            )
        )
        script = "\n".join(
            (
                "from pathlib import Path",
                "import sys",
                "path = Path('agent-artifacts') / 'synthetic-workspace-summary.md'",
                "path.parent.mkdir(parents=True, exist_ok=True)",
                "path.write_text(sys.argv[1], encoding='utf-8')",
                "print(path.as_posix())",
            )
        )
        command = f"python -c {shlex.quote(script)} {shlex.quote(content)}"
        payload = self._request_json(
            "POST",
            "/api/bash/execute_bash_command",
            {
                "command": command,
                "cwd": str(self.workspace),
                "timeout": 30,
            },
        )
        if not isinstance(payload, dict):
            msg = "OpenHands bash response was not an object"
            raise AgentServerUnavailableError(msg)
        return payload

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["X-Session-API-Key"] = self.api_key
        request = urllib.request.Request(
            f"{self.endpoint}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with _NO_REDIRECT_OPENER.open(request, timeout=self.timeout) as response:
                return cast(JsonValue, json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as error:
            detail = (
                "redirect rejected"
                if 300 <= error.code < 400
                else error.read().decode("utf-8", errors="replace")
            )
            msg = f"OpenHands agent-server request failed: HTTP {error.code} {detail}"
            raise AgentServerUnavailableError(msg) from error
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            msg = f"OpenHands agent-server request failed: {error}"
            raise AgentServerUnavailableError(msg) from error


class OpenHandsAgentServerBackend:
    """Adapt local agent-server events to the core backend facade."""

    def __init__(
        self,
        client: AgentServerClient,
        *,
        server: ManagedAgentServer | None = None,
        backend_id: str = "openhands-agent-server",
    ) -> None:
        self.client = client
        self.server = server
        self._backend_id = backend_id

    @property
    def backend_id(self) -> str:
        """Return the backend id."""
        return self._backend_id

    def chat_turn(self, *, session_id: str, prompt_length: int) -> tuple[BackendEvent, ...]:
        """Translate one chat turn from the managed agent-server."""
        self._ensure_available()
        return _translate_events(
            self.client.chat_turn(session_id=session_id, prompt_length=prompt_length)
        )

    def run_turn(
        self, *, session_id: str, prompt_length: int, approved: bool
    ) -> tuple[BackendEvent, ...]:
        """Translate one tool-running turn from the managed agent-server."""
        self._ensure_available()
        return _translate_events(
            self.client.run_turn(
                session_id=session_id,
                prompt_length=prompt_length,
                approved=approved,
            )
        )

    def _ensure_available(self) -> None:
        if self.server is None:
            return
        status = self.server.status()
        if self.server.config.enabled and not status.running:
            msg = "agent-server backend is enabled but not running"
            raise AgentServerUnavailableError(msg)


def _translate_events(events: Sequence[AgentServerEvent]) -> tuple[BackendEvent, ...]:
    return tuple(_translate_event(event) for event in events)


def _translate_event(event: AgentServerEvent) -> BackendEvent:
    if event.kind == "message":
        return BackendEvent(
            kind=BackendEventKind.AGENT_MESSAGE,
            message=str(event.payload.get("content", "")),
        )
    if event.kind == "tool_call":
        return BackendEvent(
            kind=BackendEventKind.TOOL_CALL_PROPOSED,
            tool_call=_tool_call(event.payload),
        )
    if event.kind == "confirmation":
        return BackendEvent(
            kind=BackendEventKind.CONFIRMATION,
            tool_call=_tool_call(event.payload),
            approved=bool(event.payload.get("approved", False)),
        )
    if event.kind == "tool_result":
        return BackendEvent(
            kind=BackendEventKind.TOOL_EXECUTION,
            tool_execution=ToolExecution(
                tool_name=str(event.payload.get("tool_name", "unknown")),
                exit_code=_int_payload(event.payload, "exit_code", default=1),
                summary=str(event.payload.get("summary", "tool execution recorded")),
            ),
        )
    msg = f"unsupported agent-server event kind: {event.kind}"
    raise ValueError(msg)


def _tool_call(payload: Mapping[str, JsonValue]) -> ProposedToolCall:
    risk = str(payload.get("risk", "unknown"))
    if risk not in {"low", "medium", "high", "unknown"}:
        risk = "unknown"
    return ProposedToolCall(
        tool_call_id=str(payload.get("tool_call_id", "toolcall-unknown")),
        tool_name=str(payload.get("tool_name", "unknown")),
        risk=cast(RiskTier, risk),
        summary=str(payload.get("summary", "tool call proposed")),
    )


def _int_payload(payload: Mapping[str, JsonValue], key: str, *, default: int) -> int:
    value = payload.get(key)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    if isinstance(value, int | float):
        return int(value)
    return default


def _validate_agent_server_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError as error:
        msg = f"agent-server endpoint port is invalid: {endpoint}"
        raise AgentServerBindingError(msg) from error
    if parsed.scheme != "http" or host not in _LOCAL_HOSTS or port is None:
        msg = f"agent-server endpoint must be http loopback-only: {endpoint}"
        raise AgentServerBindingError(msg)
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        msg = f"agent-server endpoint must be a plain loopback base URL: {endpoint}"
        raise AgentServerBindingError(msg)
