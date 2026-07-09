# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for gateway command handling and replayable event streams."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from heartwood.gateway import RestGateway, RestRequest, RestResponse, SessionGateway
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand


def _command(kind: CommandKind, *, session_id: str = "session-1", **payload: JsonValue) -> str:
    command = SessionCommand(
        command_id=f"{session_id}-{kind.value}",
        session_id=session_id,
        kind=kind,
        actor_id="synthetic-user",
        created_at="2026-01-01T00:00:00Z",
        payload=payload,
    )
    return command.model_dump_json()


def _events(response: RestResponse) -> list[Mapping[str, JsonValue]]:
    events = response.body["events"]
    assert isinstance(events, list)
    assert all(isinstance(event, dict) for event in events)
    return cast(list[Mapping[str, JsonValue]], events)


def test_rest_command_routes_through_session_service_and_streams_events(tmp_path: Path) -> None:
    gateway = SessionGateway(workspace=tmp_path)
    stream = gateway.websocket(session_id="session-1")
    rest = RestGateway(gateway)

    response = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.DETECT),
        )
    )

    assert response.status_code == 200
    assert [event["kind"] for event in _events(response)] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.DETECTION_PROPOSED.value,
    ]
    assert [event.kind for event in stream.receive()] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.DETECTION_PROPOSED.value,
    ]


def test_rest_event_replay_supports_reconnect_after_sequence(tmp_path: Path) -> None:
    gateway = SessionGateway(workspace=tmp_path)
    rest = RestGateway(gateway)
    rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.CHAT, prompt="summarize"),
        )
    )

    replay = rest.handle(RestRequest(method="GET", path="/sessions/session-1/events?after=0"))
    stream = gateway.websocket(session_id="session-1", after_sequence=0)

    assert replay.status_code == 200
    assert [event["sequence"] for event in _events(replay)] == [1]
    assert [event.sequence for event in stream.receive()] == [1]


def test_rest_command_persists_gateway_audit_log(tmp_path: Path) -> None:
    rest = RestGateway(SessionGateway(workspace=tmp_path))

    response = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.DETECT),
        )
    )

    audit_lines = (tmp_path / "session-1" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert response.status_code == 200
    assert len(audit_lines) == 2


def test_pause_and_resume_are_streamed(tmp_path: Path) -> None:
    gateway = SessionGateway(workspace=tmp_path)
    stream = gateway.websocket(session_id="session-1")
    rest = RestGateway(gateway)

    rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.PAUSE),
        )
    )
    rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.RESUME),
        )
    )

    assert [event.kind for event in stream.receive()] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.SESSION_PAUSED.value,
        EventKind.COMMAND_RECEIVED.value,
        EventKind.SESSION_RESUMED.value,
    ]


def test_run_streams_confirmation_request_when_policy_or_approval_blocks(tmp_path: Path) -> None:
    gateway = SessionGateway(workspace=tmp_path)
    rest = RestGateway(gateway)

    response = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(
                CommandKind.RUN,
                prompt="run",
                endpoint="https://public.example.invalid/v1/chat/completions",
            ),
        )
    )

    assert response.status_code == 200
    assert EventKind.CONFIRMATION_REQUESTED.value in [event["kind"] for event in _events(response)]


def test_run_streams_confirmation_resolution_after_model_call_approval(tmp_path: Path) -> None:
    gateway = SessionGateway(workspace=tmp_path)
    rest = RestGateway(gateway)
    rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(
                CommandKind.APPROVE,
                target_type="model-call",
                target_id="decision-synthetic-model-call",
            ),
        )
    )

    response = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(
                CommandKind.RUN,
                prompt="run",
                endpoint="https://model.local.invalid/v1/chat/completions",
            ),
        )
    )

    assert response.status_code == 200
    assert EventKind.CONFIRMATION_RESOLVED.value in [event["kind"] for event in _events(response)]


def test_rest_rejects_malformed_and_mismatched_commands(tmp_path: Path) -> None:
    rest = RestGateway(SessionGateway(workspace=tmp_path))

    malformed = rest.handle(
        RestRequest(method="POST", path="/sessions/session-1/commands", body="{")
    )
    invalid = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=json.dumps({"session_id": "session-1"}),
        )
    )
    mismatched = rest.handle(
        RestRequest(
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.DETECT, session_id="other-session"),
        )
    )

    assert malformed.status_code == 400
    assert invalid.status_code == 422
    assert mismatched.status_code == 409


def test_rest_rejects_unknown_routes_and_methods(tmp_path: Path) -> None:
    rest = RestGateway(SessionGateway(workspace=tmp_path))

    assert rest.handle(RestRequest(method="GET", path="/unknown")).status_code == 404
    method_response = rest.handle(RestRequest(method="PUT", path="/sessions/session-1/events"))
    assert method_response.status_code == 405


def test_rest_rejects_invalid_replay_cursor(tmp_path: Path) -> None:
    rest = RestGateway(SessionGateway(workspace=tmp_path))

    response = rest.handle(
        RestRequest(method="GET", path="/sessions/session-1/events?after=latest")
    )

    assert response.status_code == 400
