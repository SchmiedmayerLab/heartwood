# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the core session service."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from heartwood.adapters.data import LocalFilesystemDataSourceAdapter
from heartwood.adapters.model import FakeLocalModelProviderAdapter
from heartwood.adapters.platform import GenericPlatformAdapter
from heartwood.core_adapter import (
    DeterministicAgentBackend,
    FileSessionStore,
    SessionService,
    SessionStoreBoundaryError,
)
from heartwood.schemas import PolicyProfile
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand


def _command(kind: CommandKind, **payload: JsonValue) -> SessionCommand:
    return SessionCommand(
        command_id=f"command-{kind.value}",
        session_id="session-synthetic-001",
        kind=kind,
        actor_id="synthetic-user",
        created_at="2026-01-01T00:00:00Z",
        payload=payload,
    )


def test_session_detect_emits_platform_and_dataset_proposals(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    result = service.handle(_command(CommandKind.DETECT))

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.DETECTION_PROPOSED.value,
    ]
    detection = result.events[1].payload
    platform = detection["platform"]
    dataset = detection["dataset"]
    assert isinstance(platform, dict)
    assert isinstance(dataset, dict)
    assert platform["adapter_id"] == "generic"
    assert dataset["dataset_type"] == "omop-cdm"
    assert "rows" not in json.dumps(detection)


def test_session_records_approval_and_resume_events(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.DETECT))
    service.handle(
        _command(
            CommandKind.APPROVE,
            approval_id="approval-1",
            target_type="skill",
            target_id="heartwood.synthetic.omop-summary",
        )
    )

    resumed = SessionService.synthetic_default(tmp_path)
    events = resumed.replay_events()
    assert [event.sequence for event in events] == [0, 1, 2, 3]
    assert events[-1].kind == EventKind.APPROVAL_RECORDED.value


def test_session_store_next_sequence_is_cached_after_resume(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.DETECT))

    store = FileSessionStore(tmp_path, "session-synthetic-001")
    assert store.next_sequence() == 2
    assert store.next_sequence() == 3


def test_session_run_records_policy_decision_without_prompt_content(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(
        _command(
            CommandKind.APPROVE,
            target_type="model-call",
            target_id="decision-synthetic-model-call",
        )
    )
    result = service.handle(
        _command(
            CommandKind.RUN,
            prompt="show me the first records",
            endpoint="https://model.local.invalid/v1/chat",
        )
    )

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.MODEL_CALL_DECISION_RECORDED.value,
        EventKind.AGENT_MESSAGE_EMITTED.value,
        EventKind.TOOL_CALL_PROPOSED.value,
        EventKind.CONFIRMATION_RESOLVED.value,
        EventKind.TOOL_EXECUTION_RECORDED.value,
    ]
    decision = result.events[1].payload["decision"]
    assert isinstance(decision, dict)
    assert decision["decision"] == "allow"
    assert result.events[-1].payload["exit_code"] == 0
    assert "show me" not in json.dumps([event.model_dump(mode="json") for event in result.events])
    commands = (tmp_path / "session-synthetic-001" / "commands.jsonl").read_text(encoding="utf-8")
    assert "show me" not in commands


def test_session_run_can_invoke_allowlisted_loopback_model(tmp_path: Path) -> None:
    server = _LocalModelServer()
    service = _loopback_service(tmp_path, server.endpoint)
    service.handle(
        _command(
            CommandKind.APPROVE,
            target_type="model-call",
            target_id="decision-synthetic-model-call",
        )
    )
    try:
        result = service.handle(
            _command(
                CommandKind.RUN,
                prompt="invoke local model",
                endpoint=server.endpoint,
                invoke_model=True,
            )
        )
    finally:
        server.close()

    decision = result.events[1].payload["decision"]
    metadata = result.events[1].payload["response_metadata"]
    assert isinstance(decision, dict)
    assert isinstance(metadata, dict)
    assert decision["decision"] == "allow"
    assert metadata["model"] == "heartwood-local-demo"
    assert metadata["status"] == "ok"
    assert server.requests == [{"prompt_length": 18, "session_id": "session-synthetic-001"}]
    assert "Synthetic local model response" not in json.dumps(
        [event.model_dump(mode="json") for event in result.events]
    )


def test_session_run_requires_approval_before_local_model_invocation(tmp_path: Path) -> None:
    server = _LocalModelServer()
    service = _loopback_service(tmp_path, server.endpoint)
    try:
        result = service.handle(
            _command(
                CommandKind.RUN,
                prompt="invoke local model",
                endpoint=server.endpoint,
                invoke_model=True,
            )
        )
    finally:
        server.close()

    error = next(event for event in result.events if event.kind == EventKind.ERROR_RECORDED.value)
    decision_event = next(
        event
        for event in result.events
        if event.kind == EventKind.MODEL_CALL_DECISION_RECORDED.value
    )
    execution = next(
        event for event in result.events if event.kind == EventKind.TOOL_EXECUTION_RECORDED.value
    )
    assert server.requests == []
    assert error.payload["reason"] == "local model invocation requires approved model-call decision"
    assert "response_metadata" not in decision_event.payload
    assert execution.payload["exit_code"] == 1


def test_session_run_rejects_local_model_redirects(tmp_path: Path) -> None:
    server = _RedirectModelServer()
    service = _loopback_service(tmp_path, server.endpoint)
    service.handle(
        _command(
            CommandKind.APPROVE,
            target_type="model-call",
            target_id="decision-synthetic-model-call",
        )
    )
    try:
        result = service.handle(
            _command(
                CommandKind.RUN,
                prompt="invoke redirecting local model",
                endpoint=server.endpoint,
                invoke_model=True,
            )
        )
    finally:
        server.close()

    error = next(event for event in result.events if event.kind == EventKind.ERROR_RECORDED.value)
    decision_event = next(
        event
        for event in result.events
        if event.kind == EventKind.MODEL_CALL_DECISION_RECORDED.value
    )
    execution = next(
        event for event in result.events if event.kind == EventKind.TOOL_EXECUTION_RECORDED.value
    )
    assert error.payload["reason"] == "local model invocation rejected redirect"
    assert "response_metadata" not in decision_event.payload
    assert execution.payload["exit_code"] == 1


def test_session_run_blocks_tool_execution_when_local_model_invocation_fails(
    tmp_path: Path,
) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(
        _command(
            CommandKind.APPROVE,
            target_type="model-call",
            target_id="decision-synthetic-model-call",
        )
    )
    result = service.handle(
        _command(
            CommandKind.RUN,
            prompt="invoke missing local model",
            endpoint="https://model.local.invalid/v1/chat",
            invoke_model=True,
        )
    )

    assert any(event.kind == EventKind.ERROR_RECORDED.value for event in result.events)
    execution = next(
        event for event in result.events if event.kind == EventKind.TOOL_EXECUTION_RECORDED.value
    )
    assert execution.payload["exit_code"] == 1


def test_session_denied_model_endpoint_still_records_noop_tool_failure(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    result = service.handle(
        _command(
            CommandKind.RUN,
            prompt="run",
            endpoint="https://public.example.invalid/v1/chat",
        )
    )

    decision = result.events[1].payload["decision"]
    assert isinstance(decision, dict)
    assert decision["decision"] == "deny"
    confirmation = next(
        event for event in result.events if event.kind == EventKind.CONFIRMATION_REQUESTED.value
    )
    assert isinstance(confirmation.payload["request"], dict)
    execution = next(
        event for event in result.events if event.kind == EventKind.TOOL_EXECUTION_RECORDED.value
    )
    assert execution.payload["exit_code"] == 1


def test_session_run_requires_prior_model_call_approval(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    result = service.handle(
        _command(
            CommandKind.RUN,
            prompt="run",
            endpoint="https://model.local.invalid/v1/chat",
        )
    )

    decision = result.events[1].payload["decision"]
    assert isinstance(decision, dict)
    assert decision["decision"] == "allow"
    execution = next(
        event for event in result.events if event.kind == EventKind.TOOL_EXECUTION_RECORDED.value
    )
    assert execution.payload["exit_code"] == 1


def test_session_records_tool_call_approval(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    result = service.handle(
        _command(
            CommandKind.APPROVE,
            target_type="tool-call",
            target_id="session-synthetic-001-toolcall-0",
        )
    )

    approval = result.events[-1].payload["approval"]
    assert isinstance(approval, dict)
    assert approval["target_type"] == "tool-call"


def test_session_audit_export_writes_scrubbed_jsonl(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.RUN, prompt="sensitive prompt", approved=True))
    result = service.handle(_command(CommandKind.AUDIT_EXPORT))

    path = Path(str(result.events[-1].payload["path"]))
    exported = path.read_text(encoding="utf-8")
    assert "sensitive prompt" not in exported
    assert "audit.export.recorded" in exported


def test_session_chat_emits_agent_message(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    result = service.handle(_command(CommandKind.CHAT, prompt="summarize the cohort"))
    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.AGENT_MESSAGE_EMITTED.value,
    ]


def test_session_pause_and_resume_emit_lifecycle_events(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    paused = service.handle(_command(CommandKind.PAUSE))
    resumed = service.handle(_command(CommandKind.RESUME))
    assert paused.events[-1].kind == EventKind.SESSION_PAUSED.value
    assert resumed.events[-1].kind == EventKind.SESSION_RESUMED.value


def test_agent_message_content_is_scrubbed_from_audit_export(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.CHAT, prompt="summarize the cohort"))
    result = service.handle(_command(CommandKind.AUDIT_EXPORT))

    export_path = Path(str(result.events[-1].payload["path"]))
    exported = export_path.read_text(encoding="utf-8")
    events_text = (tmp_path / "session-synthetic-001" / "events.jsonl").read_text(encoding="utf-8")

    assert "Planned a synthetic aggregate analysis" in events_text
    assert "Planned a synthetic aggregate analysis" not in exported
    assert "[scrubbed]" in exported


def test_unimplemented_commands_emit_structured_error(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    result = service.handle(_command(CommandKind.REPLAY))
    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value


def test_session_store_rejects_session_path_escape(tmp_path: Path) -> None:
    with pytest.raises(SessionStoreBoundaryError):
        FileSessionStore(tmp_path, "../outside")


def test_session_rejects_mismatched_command_session(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    command = _command(CommandKind.DETECT).model_copy(update={"session_id": "other-session"})

    with pytest.raises(ValueError, match="does not match store session"):
        service.handle(command)


def test_local_default_uses_non_synthetic_clock(tmp_path: Path) -> None:
    command = _command(CommandKind.DETECT).model_copy(update={"session_id": "session-local"})
    result = SessionService.local_default(tmp_path, env={}).handle(command)

    assert result.events[0].occurred_at != "2026-01-01T00:00:00Z"


class _LoopbackPlatform(GenericPlatformAdapter):
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def default_policy_profile(self) -> PolicyProfile:
        return PolicyProfile(
            policy_id="generic-default",
            platform_id=self.adapter_id,
            allowed_model_endpoints=(self.endpoint,),
        )


class _LocalModelHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, JsonValue]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload = json.loads(body.decode("utf-8"))
        assert isinstance(payload, dict)
        metadata = payload["metadata"]
        assert isinstance(metadata, dict)
        self.requests.append(metadata)
        response = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "heartwood-local-demo",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "Synthetic local model response",
                    },
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _fmt: str, *_args: object) -> None:
        return None


class _RedirectModelHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.send_response(302)
        self.send_header("Location", "https://public.example.invalid/v1/chat")
        self.end_headers()

    def log_message(self, _fmt: str, *_args: object) -> None:
        return None


class _LocalModelServer:
    def __init__(self) -> None:
        _LocalModelHandler.requests = []
        self.server = HTTPServer(("127.0.0.1", 0), _LocalModelHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        host = self.server.server_address[0]
        port = self.server.server_address[1]
        if isinstance(host, bytes):
            host = host.decode("utf-8")
        return f"http://{host}:{port}/v1/chat"

    @property
    def requests(self) -> list[dict[str, JsonValue]]:
        return _LocalModelHandler.requests

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class _RedirectModelServer:
    def __init__(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), _RedirectModelHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        host = self.server.server_address[0]
        port = self.server.server_address[1]
        if isinstance(host, bytes):
            host = host.decode("utf-8")
        return f"http://{host}:{port}/v1/chat"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _loopback_service(tmp_path: Path, endpoint: str) -> SessionService:
    platform = _LoopbackPlatform(endpoint)
    policy = platform.default_policy_profile()
    return SessionService(
        store=FileSessionStore(tmp_path, "session-synthetic-001"),
        platform_adapter=platform,
        data_source_adapter=LocalFilesystemDataSourceAdapter.synthetic_omop(),
        model_provider_adapter=FakeLocalModelProviderAdapter(policy),
        backend=DeterministicAgentBackend(),
        env={},
        clock=lambda: "2026-01-01T00:00:00Z",
    )
