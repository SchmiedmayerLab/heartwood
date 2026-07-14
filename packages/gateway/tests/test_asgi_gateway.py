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

from heartwood.gateway import GatewayAsgiApp, ProjectContext, SessionGateway
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand


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


def _gateway(workspace: Path) -> SessionGateway:
    workspace.mkdir(parents=True, exist_ok=True)
    return SessionGateway(
        project=ProjectContext(workspace),
        env={},
        backend_id="deterministic",
    )


def test_asgi_http_routes_rest_command(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        app = GatewayAsgiApp(_gateway(tmp_path))
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


def test_asgi_http_accepts_gateway_routes_under_proxy_prefix(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        app = GatewayAsgiApp(
            _gateway(tmp_path),
            static_base_path="/proxy/8767",
        )
        return await _http_call(
            app,
            method="POST",
            path="/proxy/8767/sessions/session-1/commands",
            body=_command(CommandKind.DETECT),
        )

    sent = asyncio.run(scenario())

    assert sent[0]["status"] == 200
    body = json.loads(cast(bytes, sent[1]["body"]).decode("utf-8"))
    assert [event["kind"] for event in body["events"]] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.DETECTION_PROPOSED.value,
    ]


def test_asgi_session_lifecycle_does_not_fall_through_to_static_assets(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        static_dir = tmp_path / "dist"
        static_dir.mkdir()
        (static_dir / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
        app = GatewayAsgiApp(
            _gateway(tmp_path / "sessions"),
            static_dir=static_dir,
            static_base_path="/proxy/8767",
        )
        created = await _http_call(
            app,
            method="POST",
            path="/proxy/8767/sessions",
            body=json.dumps({"title": "Proxy session"}).encode(),
        )
        listed = await _http_call(
            app,
            method="GET",
            path="/proxy/8767/sessions",
        )
        return created, listed

    created, listed = asyncio.run(scenario())
    created_body = json.loads(cast(bytes, created[1]["body"]).decode("utf-8"))
    listed_body = json.loads(cast(bytes, listed[1]["body"]).decode("utf-8"))

    assert created[0]["status"] == 201
    assert listed[0]["status"] == 200
    assert listed_body["sessions"] == [created_body]


def test_asgi_delivers_generated_audit_export(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        app = GatewayAsgiApp(_gateway(tmp_path))
        await _http_call(
            app,
            method="POST",
            path="/sessions/session-1/commands",
            body=_command(CommandKind.AUDIT_EXPORT),
        )
        return await _http_call(
            app,
            method="GET",
            path="/sessions/session-1/audit-export",
        )

    sent = asyncio.run(scenario())
    body = json.loads(cast(bytes, sent[1]["body"]).decode("utf-8"))

    assert sent[0]["status"] == 200
    assert body["filename"] == "session-1-audit.jsonl"
    assert "audit.export.recorded" in body["content"]


def test_asgi_http_replays_session_events(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        gateway = _gateway(tmp_path)
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
    assert [event["sequence"] for event in body["events"]] == [1, 2, 3, 4, 5]


def test_asgi_websocket_streams_live_gateway_events(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        gateway = _gateway(tmp_path)
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
        EventKind.USER_MESSAGE_RECORDED.value,
        EventKind.MODEL_CALL_DECISION_RECORDED.value,
        EventKind.AGENT_MESSAGE_EMITTED.value,
        EventKind.TOOL_CALL_PROPOSED.value,
        EventKind.CONFIRMATION_REQUESTED.value,
    ]


def test_asgi_websocket_replays_events_after_sequence(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        gateway = _gateway(tmp_path)
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
    assert [event["sequence"] for event in payload["events"]] == [1, 2, 3, 4, 5]


def test_asgi_websocket_accepts_gateway_routes_under_proxy_prefix(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        gateway = _gateway(tmp_path)
        gateway.handle(SessionCommand.model_validate_json(_command(CommandKind.CHAT, prompt="hi")))
        app = GatewayAsgiApp(gateway, static_base_path="/proxy/8767")
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
                "path": "/proxy/8767/sessions/session-1/events",
                "query_string": b"after=0",
            },
            receive,
            send,
        )
        return sent

    sent = asyncio.run(scenario())

    assert sent[0]["type"] == "websocket.accept"
    payload = json.loads(cast(str, sent[1]["text"]))
    assert [event["sequence"] for event in payload["events"]] == [1, 2, 3, 4, 5]


def test_asgi_sse_replays_events_after_sequence(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        gateway = _gateway(tmp_path)
        gateway.handle(SessionCommand.model_validate_json(_command(CommandKind.CHAT, prompt="hi")))
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
                    "type": "http",
                    "method": "GET",
                    "path": "/sessions/session-1/events/stream",
                    "query_string": b"after=0",
                },
                receive,
                send,
            )
        )
        await _wait_for_sent(sent, 2)
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, timeout=1)
        return sent

    sent = asyncio.run(scenario())

    assert sent[0]["status"] == 200
    headers = cast(list[tuple[bytes, bytes]], sent[0]["headers"])
    assert (b"content-type", b"text/event-stream") in headers
    body = cast(bytes, sent[1]["body"]).decode("utf-8")
    assert body.startswith("event: heartwood-session-events\n")
    data = json.loads(body.split("data: ", maxsplit=1)[1])
    assert [event["sequence"] for event in data["events"]] == [1, 2, 3, 4, 5]


def test_asgi_sse_rejects_invalid_session_id(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        return await _http_call(
            GatewayAsgiApp(_gateway(tmp_path)),
            method="GET",
            path="/sessions/invalid!session/events/stream",
        )

    sent = asyncio.run(scenario())

    assert sent[0]["status"] == 422
    assert json.loads(cast(bytes, sent[1]["body"])) == {
        "error": (
            "session id must start with a letter or number and contain at most 128 "
            "letters, numbers, dots, hyphens, or underscores"
        )
    }


def test_asgi_static_serves_web_assets_under_proxy_prefix(tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
    (assets_dir / "app.js").write_text("console.log('heartwood')", encoding="utf-8")

    async def scenario() -> list[dict[str, object]]:
        app = GatewayAsgiApp(
            _gateway(tmp_path / "sessions"),
            static_dir=static_dir,
            static_base_path="/proxy/8767",
        )
        return await _http_call(
            app,
            method="GET",
            path="/proxy/8767/assets/app.js",
        )

    sent = asyncio.run(scenario())

    assert sent[0]["status"] == 200
    assert cast(bytes, sent[1]["body"]).decode("utf-8") == "console.log('heartwood')"


def test_asgi_static_falls_back_to_index_for_client_routes(tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text('<div id="root"></div>', encoding="utf-8")

    async def scenario() -> list[dict[str, object]]:
        app = GatewayAsgiApp(
            _gateway(tmp_path / "sessions"),
            static_dir=static_dir,
        )
        return await _http_call(
            app,
            method="GET",
            path="/sessions-ui/session-local",
        )

    sent = asyncio.run(scenario())

    assert sent[0]["status"] == 200
    assert cast(bytes, sent[1]["body"]).decode("utf-8") == '<div id="root"></div>'


def test_asgi_unknown_settings_route_does_not_fall_back_to_spa(tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text('<div id="root"></div>', encoding="utf-8")

    async def scenario() -> list[dict[str, object]]:
        app = GatewayAsgiApp(
            _gateway(tmp_path / "sessions"),
            static_dir=static_dir,
        )
        return await _http_call(
            app,
            method="GET",
            path="/settings/unknown",
        )

    sent = asyncio.run(scenario())

    assert sent[0]["status"] == 404
    assert json.loads(cast(bytes, sent[1]["body"])) == {"error": "unknown gateway route"}


def test_asgi_websocket_rejects_invalid_route(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        app = GatewayAsgiApp(_gateway(tmp_path))
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "websocket.disconnect"}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await app({"type": "websocket", "path": "/unknown", "query_string": b""}, receive, send)
        return sent

    sent = asyncio.run(scenario())

    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_asgi_websocket_rejects_invalid_session_id(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        app = GatewayAsgiApp(_gateway(tmp_path))
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "websocket.disconnect"}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await app(
            {
                "type": "websocket",
                "path": "/sessions/invalid!session/events",
                "query_string": b"",
            },
            receive,
            send,
        )
        return sent

    sent = asyncio.run(scenario())

    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_asgi_lifespan_starts_and_stops_gateway_dependencies(tmp_path: Path) -> None:
    async def scenario() -> _LifecycleGateway:
        gateway = _LifecycleGateway(workspace=tmp_path)
        app = GatewayAsgiApp(gateway)
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
        return gateway

    gateway = asyncio.run(scenario())

    assert gateway.started is True
    assert gateway.stopped is True


class _LifecycleGateway(SessionGateway):
    def __init__(self, *, workspace: Path) -> None:
        super().__init__(project=ProjectContext(workspace), env={})
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


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
