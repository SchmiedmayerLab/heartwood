# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the ASGI gateway transport adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from heartwood.gateway import (
    AgentServerConfig,
    GatewayAsgiApp,
    ManagedAgentServer,
    SessionGateway,
)
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand


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


def _command(kind: CommandKind, *, session_id: str = "session-1", **payload: JsonValue) -> bytes:
    command = SessionCommand(
        command_id=f"{session_id}-{kind.value}",
        session_id=session_id,
        kind=kind,
        actor_id="synthetic-user",
        created_at="2026-01-01T00:00:00Z",
        payload=payload,
    )
    return command.model_dump_json().encode("utf-8")


def test_asgi_http_routes_rest_command(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        app = GatewayAsgiApp(SessionGateway(workspace=tmp_path))
        return await _http_call(
            app,
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.DETECT),
        )

    sent = asyncio.run(scenario())

    assert sent[0]["status"] == 200
    body = json.loads(cast(bytes, sent[1]["body"]).decode("utf-8"))
    assert [event["kind"] for event in body["events"]] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.DETECTION_PROPOSED.value,
    ]


def test_asgi_http_replays_session_events(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        gateway = SessionGateway(workspace=tmp_path)
        app = GatewayAsgiApp(gateway)
        await _http_call(
            app,
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.CHAT, prompt="hello"),
        )
        return await _http_call(
            app,
            method="GET",
            path="/sessions/session-1/events",
            query_string=b"after=0",
        )

    sent = asyncio.run(scenario())

    assert sent[0]["status"] == 200
    body = json.loads(cast(bytes, sent[1]["body"]).decode("utf-8"))
    assert [event["sequence"] for event in body["events"]] == [1]


def test_asgi_websocket_streams_live_gateway_events(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        gateway = SessionGateway(workspace=tmp_path)
        app = GatewayAsgiApp(gateway)
        incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return await incoming.get()

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        task = asyncio.create_task(
            app(
                {
                    "type": "websocket",
                    "path": "/sessions/session-1/events",
                    "query_string": b"",
                },
                receive,
                send,
            )
        )
        await _wait_for_sent(sent, 1)
        gateway.handle(SessionCommand.model_validate_json(_command(CommandKind.CHAT, prompt="hi")))
        await _wait_for_sent(sent, 2)
        await incoming.put({"type": "websocket.disconnect"})
        await task
        return sent

    sent = asyncio.run(scenario())

    assert sent[0]["type"] == "websocket.accept"
    payload = json.loads(cast(str, sent[1]["text"]))
    assert [event["kind"] for event in payload["events"]] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.AGENT_MESSAGE_EMITTED.value,
    ]


def test_asgi_websocket_replays_events_after_sequence(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        gateway = SessionGateway(workspace=tmp_path)
        gateway.handle(SessionCommand.model_validate_json(_command(CommandKind.CHAT, prompt="hi")))
        app = GatewayAsgiApp(gateway)
        incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return await incoming.get()

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await incoming.put({"type": "websocket.disconnect"})
        await app(
            {
                "type": "websocket",
                "path": "/sessions/session-1/events",
                "query_string": b"after=0",
            },
            receive,
            send,
        )
        return sent

    sent = asyncio.run(scenario())

    assert sent[0]["type"] == "websocket.accept"
    payload = json.loads(cast(str, sent[1]["text"]))
    assert [event["sequence"] for event in payload["events"]] == [1]


def test_asgi_websocket_rejects_invalid_route(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        app = GatewayAsgiApp(SessionGateway(workspace=tmp_path))
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "websocket.disconnect"}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await app({"type": "websocket", "path": "/unknown", "query_string": b""}, receive, send)
        return sent

    sent = asyncio.run(scenario())

    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_asgi_lifespan_starts_and_stops_gateway_dependencies(tmp_path: Path) -> None:
    async def scenario() -> _FakeProcess:
        process = _FakeProcess()

        def process_factory(command: tuple[str, ...]) -> _FakeProcess:
            assert command == ("agent-server", "--local")
            return process

        server = ManagedAgentServer(
            AgentServerConfig(command=("agent-server", "--local"), port=8765, enabled=True),
            process_factory=process_factory,
        )
        app = GatewayAsgiApp(SessionGateway(workspace=tmp_path, agent_server=server))
        message_values: tuple[dict[str, object], ...] = (
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        )
        messages: Iterator[dict[str, object]] = iter(message_values)

        async def receive() -> dict[str, object]:
            return next(messages)

        async def send(message: dict[str, object]) -> None:
            assert message["type"] in {
                "lifespan.startup.complete",
                "lifespan.shutdown.complete",
            }

        await app({"type": "lifespan"}, receive, send)
        return process

    process = asyncio.run(scenario())

    assert process.terminated is True


async def _http_call(
    app: GatewayAsgiApp,
    *,
    method: str,
    path: str,
    query_string: bytes = b"",
    body: bytes = b"",
) -> list[dict[str, object]]:
    messages = iter(({"type": "http.request", "body": body, "more_body": False},))
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return next(messages)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(
        {"type": "http", "method": method, "path": path, "query_string": query_string},
        receive,
        send,
    )
    return sent


async def _wait_for_sent(sent: list[dict[str, object]], count: int) -> None:
    for _ in range(10):
        if len(sent) >= count:
            return
        await asyncio.sleep(0)
    assert len(sent) >= count
