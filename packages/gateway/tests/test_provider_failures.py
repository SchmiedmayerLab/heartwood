# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import socket
import time
from collections import deque
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import cast

import pytest

import heartwood.gateway._openhands_sdk as openhands_sdk_module
from heartwood.core_adapter import PendingActionGroup, SessionResult, SessionService
from heartwood.gateway import ModelProfile, OpenHandsSdkBackend
from heartwood.schemas import PolicyProfile
from heartwood.session import CommandKind, EventKind, SessionCommand, SessionEvent

_API_KEY_NAME = "HEARTWOOD_PROVIDER_TEST_API_KEY"
_API_KEY = "provider-test-credential-must-not-leak"
_RESPONSE_MARKER = "private-provider-response-must-not-leak"
_PRIVATE_ENDPOINT = "https://private.provider.invalid/secret"
_SESSION_ID = "provider-failure"
_SAFE_ERROR_REASONS = {
    "An agent action failed",
    "The agent conversation stopped",
    "The agent worker stopped",
}


@dataclass(frozen=True, slots=True)
class _ScriptedResponse:
    status: int
    body: bytes
    content_type: str
    delay: float = 0.0
    declared_length: int | None = None
    required_function_call_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _RecordedRequest:
    path: str
    authorization: str | None
    body: bytes


class _MockProviderState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._responses: deque[_ScriptedResponse] = deque()
        self._requests: list[_RecordedRequest] = []

    def enqueue(self, *responses: _ScriptedResponse) -> None:
        with self._lock:
            self._responses.extend(responses)

    def next_response(self, request: _RecordedRequest) -> _ScriptedResponse:
        with self._lock:
            self._requests.append(request)
            if self._responses:
                response = self._responses.popleft()
                if response.required_function_call_ids and not _has_matched_tool_history(
                    request,
                    response.required_function_call_ids,
                ):
                    return _strict_history_error()
                return response
        return _json_error(500)

    @property
    def requests(self) -> tuple[_RecordedRequest, ...]:
        with self._lock:
            return tuple(self._requests)


class _MockProviderServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _MockProviderHandler)
        self.state = _MockProviderState()

    @property
    def base_url(self) -> str:
        host, port = cast(tuple[str, int], self.server_address)
        return f"http://{host}:{port}/v1"

    @property
    def completion_endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def responses_endpoint(self) -> str:
        return f"{self.base_url}/responses"


class _MockProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _MockProviderServer

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        request = _RecordedRequest(
            path=self.path,
            authorization=self.headers.get("Authorization"),
            body=self.rfile.read(content_length),
        )
        response = self.server.state.next_response(request)
        if response.delay:
            time.sleep(response.delay)
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header(
                "Content-Length",
                str(
                    len(response.body)
                    if response.declared_length is None
                    else response.declared_length
                ),
            )
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response.body)
            self.wfile.flush()
        except BrokenPipeError:
            return
        finally:
            if response.declared_length is not None:
                with suppress(OSError):
                    self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
            self.close_connection = True

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@pytest.fixture
def mock_provider() -> Iterator[_MockProviderServer]:
    server = _MockProviderServer()
    worker = Thread(target=server.serve_forever, name="mock-openai-provider", daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.fixture(autouse=True)
def _fast_provider_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_LLM_LOCAL_NUM_RETRIES", 2)
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_LLM_RETRY_MIN_WAIT_SECONDS", 0)
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_LLM_RETRY_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_LLM_RETRY_MULTIPLIER", 1.0)
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_LLM_LOCAL_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_PROGRESS_POLL_SECONDS", 0.01)


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_failures_are_safe_non_retryable_and_idempotent(
    tmp_path: Path,
    mock_provider: _MockProviderServer,
    status: int,
) -> None:
    mock_provider.state.enqueue(_json_error(status))
    service = _service(tmp_path, mock_provider)
    command = _command(command_id=f"authentication-{status}")

    try:
        initial = service.handle(command)
        replayed = service.handle(command)
        events = _wait_for_terminal_events(service)

        assert replayed == SessionResult(events=initial.events, replayed=True)
        assert len(mock_provider.state.requests) == 1
        _assert_request_uses_credential(mock_provider.state.requests[0])
        _assert_safe_failure(events, service)
    finally:
        service.close()


@pytest.mark.parametrize(
    ("failures", "expected_requests"),
    [
        ((429,), 2),
        ((500, 503), 3),
    ],
)
def test_transient_provider_failures_retry_and_recover_within_the_bound(
    tmp_path: Path,
    mock_provider: _MockProviderServer,
    failures: tuple[int, ...],
    expected_requests: int,
) -> None:
    mock_provider.state.enqueue(
        *(_json_error(status) for status in failures),
        _successful_stream("Recovered after a transient provider failure."),
    )
    service = _service(tmp_path, mock_provider)

    try:
        service.handle(_command(command_id=f"transient-{'-'.join(map(str, failures))}"))
        events = _wait_for_terminal_events(service, expected_status="finished")

        assert len(mock_provider.state.requests) == expected_requests
        assert _agent_messages(events) == ["Recovered after a transient provider failure."]
        assert not _error_events(events)
        assert all(
            request.authorization == f"Bearer {_API_KEY}"
            for request in mock_provider.state.requests
        )
    finally:
        service.close()


def test_new_command_recovers_after_a_terminal_provider_failure(
    tmp_path: Path,
    mock_provider: _MockProviderServer,
) -> None:
    mock_provider.state.enqueue(
        _json_error(401),
        _successful_stream("Recovered on the next command."),
    )
    service = _service(tmp_path, mock_provider)

    try:
        service.handle(_command(command_id="terminal-failure"))
        failed_events = _wait_for_terminal_events(service)

        service.handle(_command(command_id="recovery-command"))
        recovered_events = _wait_for_terminal_events(service, expected_status="finished")

        assert len(mock_provider.state.requests) == 2
        _assert_safe_failure(failed_events, service)
        assert _agent_messages(recovered_events) == ["Recovered on the next command."]
        lifecycle_statuses = [
            event.payload.get("status")
            for event in recovered_events
            if event.kind == EventKind.AGENT_LIFECYCLE_UPDATED
        ]
        assert lifecycle_statuses[-1] == "finished"
    finally:
        service.close()


@pytest.mark.parametrize("status", [429, 503])
def test_transient_provider_failures_stop_at_the_configured_retry_bound(
    tmp_path: Path,
    mock_provider: _MockProviderServer,
    status: int,
) -> None:
    mock_provider.state.enqueue(*(_json_error(status) for _ in range(6)))
    service = _service(tmp_path, mock_provider)

    try:
        service.handle(_command(command_id=f"bounded-{status}"))
        events = _wait_for_terminal_events(service)

        assert len(mock_provider.state.requests) == 6
        _assert_safe_failure(events, service)
    finally:
        service.close()


def test_provider_timeout_is_bounded_and_content_safe(
    tmp_path: Path,
    mock_provider: _MockProviderServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openhands_sdk_module, "_AGENT_LLM_LOCAL_NUM_RETRIES", 0)
    mock_provider.state.enqueue(
        *(
            _ScriptedResponse(
                status=200,
                body=_successful_stream("late").body,
                content_type="text/event-stream",
                delay=1.2,
            )
            for _ in range(3)
        )
    )
    service = _service(tmp_path, mock_provider)
    started = time.monotonic()

    try:
        service.handle(_command(command_id="timeout"))
        events = _wait_for_terminal_events(service)

        assert time.monotonic() - started < 8
        assert len(mock_provider.state.requests) == 3
        _assert_safe_failure(events, service)
    finally:
        service.close()


@pytest.mark.parametrize(
    ("responses", "expected_requests"),
    [
        (
            (
                _ScriptedResponse(
                    status=200,
                    body=b"data: {not-valid-json}\n\ndata: [DONE]\n\n",
                    content_type="text/event-stream",
                ),
                _ScriptedResponse(
                    status=200,
                    body=b"data: {not-valid-json}\n\ndata: [DONE]\n\n",
                    content_type="text/event-stream",
                ),
            ),
            2,
        ),
        (
            (
                _ScriptedResponse(
                    status=200,
                    body=(
                        b'data: {"id":"chatcmpl-interrupted",'
                        b'"object":"chat.completion.chunk","created":1,'
                        b'"model":"mock-coder","choices":[{"index":0,"delta":'
                        b'{"role":"assistant","content":"partial'
                    ),
                    content_type="text/event-stream",
                    declared_length=4_096,
                ),
                *(
                    _ScriptedResponse(
                        status=503,
                        body=b'{"error":{"message":"synthetic provider unavailable"}}',
                        content_type="application/json",
                    )
                    for _ in range(6)
                ),
            ),
            7,
        ),
    ],
    ids=["malformed", "interrupted"],
)
def test_malformed_or_interrupted_streams_fail_closed_without_leaking_content(
    tmp_path: Path,
    mock_provider: _MockProviderServer,
    responses: tuple[_ScriptedResponse, ...],
    expected_requests: int,
) -> None:
    mock_provider.state.enqueue(*responses)
    service = _service(tmp_path, mock_provider)

    try:
        service.handle(_command(command_id="broken-stream"))
        events = _wait_for_terminal_events(service)

        assert len(mock_provider.state.requests) == expected_requests
        _assert_safe_failure(events, service)
        assert "partial" not in _serialized_events(events)
    finally:
        service.close()


@pytest.mark.parametrize(
    "calls",
    [
        (
            (
                "call-response-restart",
                "printf restored > response-restart.txt",
                "response-restart.txt",
                "restored",
            ),
        ),
        (
            (
                "call-response-first",
                "printf first > response-first.txt",
                "response-first.txt",
                "first",
            ),
            (
                "call-response-second",
                "printf second > response-second.txt",
                "response-second.txt",
                "second",
            ),
        ),
    ],
    ids=["single-action", "grouped-actions"],
)
def test_responses_api_restart_preserves_matched_tool_history(
    tmp_path: Path,
    mock_provider: _MockProviderServer,
    calls: tuple[tuple[str, str, str, str], ...],
) -> None:
    call_ids = tuple(call_id for call_id, *_rest in calls)
    mock_provider.state.enqueue(
        _responses_tool_stream(calls),
        _responses_text_stream(
            "The synthetic files were created once.",
            required_function_call_ids=call_ids,
        ),
    )
    first = _responses_service(tmp_path, mock_provider)

    try:
        first.handle(_responses_command(command_id="responses-start"))
        original_group = _wait_for_pending_action_group(first)

        assert tuple(action.tool_call_id for action in original_group.actions) == call_ids
        assert len(mock_provider.state.requests) == 1
        assert not any(
            (tmp_path / "project" / filename).exists()
            for _call_id, _command_text, filename, _content in calls
        )
    finally:
        first.close()

    restored = _responses_service(tmp_path, mock_provider)
    try:
        restored_group = _wait_for_pending_action_group(restored)
        assert restored_group == original_group
        approve = SessionCommand(
            command_id="responses-approve",
            session_id=_SESSION_ID,
            kind=CommandKind.APPROVE,
            actor_id="synthetic-user",
            created_at="2026-07-26T00:00:01Z",
            payload={"target_type": "action-set", "target_id": restored_group.group_id},
        )

        approved = restored.handle(approve)
        events = _wait_for_terminal_events(restored, expected_status="finished")
        replayed_approval = restored.handle(approve)

        assert replayed_approval == SessionResult(events=approved.events, replayed=True)
        requests = list(mock_provider.state.requests)
        assert len(requests) == 2
        _assert_matched_responses_history(requests[1], call_ids)
        for _call_id, _command_text, filename, expected in calls:
            assert (tmp_path / "project" / filename).read_text(encoding="utf-8") == expected
        assert _event_count(events, EventKind.USER_MESSAGE_RECORDED) == 1
        assert _event_count(events, EventKind.TOOL_CALL_PROPOSED) == len(calls)
        assert _event_count(events, EventKind.CONFIRMATION_REQUESTED) == len(calls)
        assert _event_count(events, EventKind.APPROVAL_RECORDED) == 1
        assert _event_count(events, EventKind.CONFIRMATION_RESOLVED) == len(calls)
        assert _event_count(events, EventKind.TOOL_EXECUTION_RECORDED) == len(calls)
        assert _agent_messages(events) == ["The synthetic files were created once."]
        assert not _error_events(events)

        restored.handle(
            SessionCommand(
                command_id="responses-audit-export",
                session_id=_SESSION_ID,
                kind=CommandKind.AUDIT_EXPORT,
                actor_id="synthetic-user",
                created_at="2026-07-26T00:00:02Z",
            )
        )
        replay = restored.replay_events()
        assert restored.replay_events() == replay
        audit_events = restored.audit_log.read()
        restored.audit_log.verify(audit_events)
        assert len(audit_events) == len(replay)
        assert [event.event_type for event in audit_events] == [str(event.kind) for event in replay]
        audit_export = restored.store.read_audit_export()
        assert all(
            command_text not in audit_export
            for _call_id, command_text, _filename, _content in calls
        )
    finally:
        restored.close()


def _service(
    tmp_path: Path,
    provider: _MockProviderServer,
) -> SessionService:
    profile = ModelProfile(
        profile_id="mock-provider",
        model="openai/mock-coder",
        policy_endpoint=provider.completion_endpoint,
        base_url=provider.base_url,
        credential_kind="environment",
        api_key_env=_API_KEY_NAME,
        max_input_tokens=16_384,
        max_output_tokens=256,
    )
    return _service_for_profile(
        tmp_path,
        profile=profile,
        conversation_key=f"provider-failure:{tmp_path.name}",
    )


def _responses_service(
    tmp_path: Path,
    provider: _MockProviderServer,
) -> SessionService:
    profile = ModelProfile(
        profile_id="mock-responses-provider",
        model="openai/gpt-5-mini",
        policy_endpoint=provider.responses_endpoint,
        base_url=provider.base_url,
        credential_kind="environment",
        api_key_env=_API_KEY_NAME,
        max_input_tokens=16_384,
        max_output_tokens=256,
    )
    return _service_for_profile(
        tmp_path,
        profile=profile,
        conversation_key=f"responses-restart:{tmp_path.name}",
    )


def _service_for_profile(
    tmp_path: Path,
    *,
    profile: ModelProfile,
    conversation_key: str,
) -> SessionService:
    backend = OpenHandsSdkBackend(
        profile=profile,
        workspace=tmp_path / "project",
        skills_dir=tmp_path / "skills",
        persistence_dir=tmp_path / "openhands",
        conversation_key=conversation_key,
        credential_environment_names=(_API_KEY_NAME,),
        env={_API_KEY_NAME: _API_KEY},
    )
    policy = PolicyProfile(
        policy_id=profile.profile_id,
        platform_id="generic",
        allowed_model_endpoints=(profile.policy_endpoint,),
        allowed_capability_tiers=("supervised",),
        allowed_action_confirmation_modes=("always-confirm",),
        credential_allowlist=(_API_KEY_NAME,),
    )
    return SessionService.local_default(
        tmp_path / "sessions",
        session_id=_SESSION_ID,
        backend=backend,
        policy_profile=policy,
        env={_API_KEY_NAME: _API_KEY},
        clock=lambda: "2026-07-26T00:00:00Z",
    )


def _command(*, command_id: str) -> SessionCommand:
    return SessionCommand(
        command_id=command_id,
        session_id=_SESSION_ID,
        kind=CommandKind.CHAT,
        actor_id="synthetic-user",
        created_at="2026-07-26T00:00:00Z",
        payload={"prompt": "Return one short synthetic result without using tools."},
    )


def _responses_command(*, command_id: str) -> SessionCommand:
    return SessionCommand(
        command_id=command_id,
        session_id=_SESSION_ID,
        kind=CommandKind.CHAT,
        actor_id="synthetic-user",
        created_at="2026-07-26T00:00:00Z",
        payload={"prompt": "Create the requested synthetic files using terminal commands."},
    )


def _wait_for_terminal_events(
    service: SessionService,
    *,
    expected_status: str = "error",
) -> tuple[SessionEvent, ...]:
    deadline = time.monotonic() + 8
    events: tuple[SessionEvent, ...] = ()
    statuses: list[object] = []
    while time.monotonic() < deadline:
        events = service.replay_events()
        statuses = [
            event.payload.get("status")
            for event in events
            if event.kind == EventKind.AGENT_LIFECYCLE_UPDATED
        ]
        if statuses and statuses[-1] == expected_status:
            return events
        time.sleep(0.01)
    kinds = [str(event.kind) for event in events]
    raise AssertionError(
        f"provider scenario did not reach {expected_status}; "
        f"observed statuses={statuses!r}, kinds={kinds!r}"
    )


def _wait_for_pending_action_group(service: SessionService) -> PendingActionGroup:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        group = service.backend.pending_action_group(session_id=_SESSION_ID)
        if group is not None:
            return group
        time.sleep(0.01)
    raise AssertionError("provider scenario did not request confirmation")


def _assert_request_uses_credential(request: _RecordedRequest) -> None:
    assert request.path == "/v1/chat/completions"
    assert request.authorization == f"Bearer {_API_KEY}"
    payload = json.loads(request.body)
    assert payload["model"] == "mock-coder"


def _assert_safe_failure(
    events: tuple[SessionEvent, ...],
    service: SessionService,
) -> None:
    errors = _error_events(events)
    assert errors
    assert {str(event.payload.get("reason")) for event in errors} <= _SAFE_ERROR_REASONS
    assert all(
        isinstance(event.payload.get("code"), str)
        and str(event.payload["code"]).startswith("HW-AGENT-")
        for event in errors
    )
    statuses = [
        event.payload.get("status")
        for event in events
        if event.kind == EventKind.AGENT_LIFECYCLE_UPDATED
    ]
    assert "running" in statuses
    assert statuses[-1] == "error"
    serialized = "\n".join(
        (
            _serialized_events(events),
            _audit_export(service),
        )
    )
    for private_value in (_API_KEY, _RESPONSE_MARKER, _PRIVATE_ENDPOINT):
        assert private_value not in serialized


def _error_events(events: tuple[SessionEvent, ...]) -> list[SessionEvent]:
    return [event for event in events if event.kind == EventKind.ERROR_RECORDED]


def _agent_messages(events: tuple[SessionEvent, ...]) -> list[object]:
    return [
        event.payload.get("content")
        for event in events
        if event.kind == EventKind.AGENT_MESSAGE_EMITTED
    ]


def _event_count(events: tuple[SessionEvent, ...], kind: EventKind) -> int:
    return sum(event.kind == kind for event in events)


def _serialized_events(events: tuple[SessionEvent, ...]) -> str:
    return "\n".join(event.model_dump_json() for event in events)


def _audit_export(service: SessionService) -> str:
    service.handle(
        SessionCommand(
            command_id="audit-export",
            session_id=_SESSION_ID,
            kind=CommandKind.AUDIT_EXPORT,
            actor_id="synthetic-user",
            created_at="2026-07-26T00:00:00Z",
        )
    )
    return service.store.read_audit_export()


def _json_error(status: int) -> _ScriptedResponse:
    return _ScriptedResponse(
        status=status,
        body=json.dumps(
            {
                "error": {
                    "message": (
                        f"{_RESPONSE_MARKER}; credential={_API_KEY}; endpoint={_PRIVATE_ENDPOINT}"
                    ),
                    "type": "provider_test_error",
                }
            }
        ).encode(),
        content_type="application/json",
    )


def _successful_stream(content: str) -> _ScriptedResponse:
    chunks = (
        {
            "id": "chatcmpl-success",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "mock-coder",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": content},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-success",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "mock-coder",
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
        },
    )
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return _ScriptedResponse(
        status=200,
        body=f"{body}data: [DONE]\n\n".encode(),
        content_type="text/event-stream",
    )


def _responses_tool_stream(
    calls: tuple[tuple[str, str, str, str], ...],
) -> _ScriptedResponse:
    return _responses_stream(
        response_id="response-tool-calls",
        output=[
            {
                "id": f"fc_{call_id}",
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": "terminal",
                "arguments": json.dumps({"command": command_text}),
            }
            for call_id, command_text, _filename, _content in calls
        ],
    )


def _responses_text_stream(
    content: str,
    *,
    required_function_call_ids: tuple[str, ...],
) -> _ScriptedResponse:
    return _responses_stream(
        response_id="response-finished",
        output=[
            {
                "id": "message-finished",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "annotations": [],
                        "text": content,
                    }
                ],
            }
        ],
        required_function_call_ids=required_function_call_ids,
    )


def _responses_stream(
    *,
    response_id: str,
    output: list[dict[str, object]],
    required_function_call_ids: tuple[str, ...] = (),
) -> _ScriptedResponse:
    completed = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "created_at": 1,
            "model": "gpt-5-mini",
            "object": "response",
            "output": output,
            "status": "completed",
        },
    }
    return _ScriptedResponse(
        status=200,
        body=f"data: {json.dumps(completed)}\n\ndata: [DONE]\n\n".encode(),
        content_type="text/event-stream",
        required_function_call_ids=required_function_call_ids,
    )


def _has_matched_tool_history(
    request: _RecordedRequest,
    required_function_call_ids: tuple[str, ...],
) -> bool:
    try:
        payload = json.loads(request.body)
    except (TypeError, ValueError):
        return False
    input_items = payload.get("input")
    if request.path != "/v1/responses" or not isinstance(input_items, list):
        return False
    call_positions: dict[str, int] = {}
    output_positions: dict[str, int] = {}
    for index, item in enumerate(input_items):
        if not isinstance(item, dict):
            continue
        call_id = item.get("call_id")
        if not isinstance(call_id, str):
            continue
        if item.get("type") == "function_call":
            if call_id in call_positions or item.get("id") != f"fc_{call_id}":
                return False
            call_positions[call_id] = index
        elif item.get("type") == "function_call_output":
            if call_id in output_positions or call_id not in call_positions:
                return False
            output_positions[call_id] = index
    return all(
        call_id in call_positions
        and call_id in output_positions
        and call_positions[call_id] < output_positions[call_id]
        for call_id in required_function_call_ids
    )


def _assert_matched_responses_history(
    request: _RecordedRequest,
    required_function_call_ids: tuple[str, ...],
) -> None:
    assert request.authorization == f"Bearer {_API_KEY}"
    assert _has_matched_tool_history(request, required_function_call_ids)


def _strict_history_error() -> _ScriptedResponse:
    return _ScriptedResponse(
        status=400,
        body=json.dumps(
            {
                "error": {
                    "message": "No tool call found for function call output",
                    "type": "invalid_request_error",
                }
            }
        ).encode(),
        content_type="application/json",
    )
