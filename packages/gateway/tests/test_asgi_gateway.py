# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the ASGI gateway transport adapter."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path
from threading import Event, Timer
from typing import cast

import pytest

from heartwood.core_adapter import BackendLifecycle, BackendLifecycleEvent
from heartwood.gateway import (
    GatewayAsgiApp,
    GatewayEventStream,
    IngressPolicy,
    ProjectContext,
    RestResponse,
    SessionGateway,
)
from heartwood.gateway._asgi import _inject_browser_base_path, _wait_for_stream_signal
from heartwood.gateway._gateway import GatewaySessionSnapshot
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
            body=_command(CommandKind.PAUSE),
        )

    sent = asyncio.run(scenario())

    assert sent[0]["status"] == 200
    body = json.loads(cast(bytes, sent[1]["body"]).decode("utf-8"))
    assert [event["kind"] for event in body["events"]] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.ERROR_RECORDED.value,
    ]
    assert body["projection"]["lifecycle"]["status"] == "idle"


def test_asgi_http_keeps_the_event_loop_responsive_during_blocking_gateway_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> float:
        app = GatewayAsgiApp(_gateway(tmp_path))
        entered = Event()
        release = Event()

        def blocking_handle(_request: object) -> RestResponse:
            entered.set()
            release.wait(timeout=2)
            return RestResponse(status_code=200, body={"status": "complete"})

        monkeypatch.setattr(app.rest, "handle", blocking_handle)
        fallback_release = Timer(0.5, release.set)
        fallback_release.start()
        started = time.monotonic()
        request = asyncio.create_task(_http_call(app, method="GET", path="/settings/models"))
        while not entered.is_set():
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)
        elapsed = time.monotonic() - started
        release.set()
        response = await request
        fallback_release.cancel()
        assert response[0]["status"] == 200
        return elapsed

    assert asyncio.run(scenario()) < 0.25


def test_stream_wait_cancels_the_sibling_task_when_receive_fails() -> None:
    async def scenario() -> None:
        update_cancelled = asyncio.Event()

        async def receive_update() -> object:
            try:
                await asyncio.Event().wait()
            finally:
                update_cancelled.set()
            return object()

        async def receive_message() -> dict[str, object]:
            raise RuntimeError("synthetic receive failure")

        with pytest.raises(RuntimeError, match="synthetic receive failure"):
            await _wait_for_stream_signal(
                receive_update,
                receive_message,
                disconnect_type="websocket.disconnect",
            )
        assert update_cancelled.is_set()

    asyncio.run(scenario())


def test_asgi_websocket_closes_gateway_stream_when_send_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> bool:
        gateway = _gateway(tmp_path)
        app = GatewayAsgiApp(gateway)
        opened = []
        original_open = gateway.open_event_stream

        def open_event_stream(
            *,
            session_id: str,
            after_sequence: int | None = None,
        ) -> tuple[GatewayEventStream, GatewaySessionSnapshot]:
            stream, snapshot = original_open(
                session_id=session_id,
                after_sequence=after_sequence,
            )
            opened.append(stream)
            return stream, snapshot

        monkeypatch.setattr(gateway, "open_event_stream", open_event_stream)

        async def receive() -> dict[str, object]:
            return {"type": "websocket.disconnect"}

        async def send(message: dict[str, object]) -> None:
            if message["type"] == "websocket.send":
                raise RuntimeError("synthetic send failure")

        with pytest.raises(RuntimeError, match="synthetic send failure"):
            await app(
                _websocket_scope("/sessions/session-1/events"),
                receive,
                send,
            )
        assert len(opened) == 1
        return opened[0].closed

    assert asyncio.run(scenario()) is True


def test_asgi_http_accepts_gateway_routes_under_proxy_prefix(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        gateway = _gateway(tmp_path)
        gateway.project.initialize()
        gateway._service("session-1")._accept_backend_events(
            (
                BackendLifecycleEvent(
                    lifecycle=BackendLifecycle.RUNNING,
                    source_event_id="synthetic-running",
                ),
            )
        )
        app = GatewayAsgiApp(
            gateway,
            ingress=IngressPolicy.create(external_base_path="/proxy/8767"),
        )
        return await _http_call(
            app,
            method="POST",
            path="/proxy/8767/sessions/session-1/commands",
            body=_command(CommandKind.PAUSE),
        )

    sent = asyncio.run(scenario())

    assert sent[0]["status"] == 200
    body = json.loads(cast(bytes, sent[1]["body"]).decode("utf-8"))
    assert [event["kind"] for event in body["events"]] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.SESSION_PAUSED.value,
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
            ingress=IngressPolicy.create(external_base_path="/proxy/8767"),
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
    assert body["projection"]["revision"] == body["events"][-1]["sequence"]


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
                _websocket_scope("/sessions/session-1/events"),
                receive,
                send,
            )
        )
        await _wait_for_sent(sent, 1)
        gateway.handle(SessionCommand.model_validate_json(_command(CommandKind.CHAT, prompt="hi")))
        await _wait_for_sent(sent, 3)
        await incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(task, timeout=1)
        return sent

    sent = asyncio.run(scenario())

    assert sent[0]["type"] == "websocket.accept"
    initial = json.loads(cast(str, sent[1]["text"]))
    assert initial["events"] == []
    assert initial["projection"]["sessionId"] == "session-1"
    payload = json.loads(cast(str, sent[2]["text"]))
    assert [event["kind"] for event in payload["events"]] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.USER_MESSAGE_RECORDED.value,
        EventKind.MODEL_CALL_DECISION_RECORDED.value,
        EventKind.AGENT_MESSAGE_EMITTED.value,
        EventKind.TOOL_CALL_PROPOSED.value,
        EventKind.CONFIRMATION_REQUESTED.value,
    ]
    assert payload["projection"]["pendingApproval"] is not None
    assert payload["projection"]["revision"] == payload["events"][-1]["sequence"]


def test_asgi_websocket_streams_transient_tokens_with_monotonic_snapshots(
    tmp_path: Path,
) -> None:
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
                _websocket_scope("/sessions/session-1/events"),
                receive,
                send,
            )
        )
        await _wait_for_sent(sent, 2)
        service = gateway._service("session-1")
        service._accept_backend_events(
            (
                BackendLifecycleEvent(
                    lifecycle=BackendLifecycle.RUNNING,
                    source_event_id="synthetic-running",
                ),
            )
        )
        await _wait_for_sent(sent, 3)
        gateway._publish_token_delta(session_id="session-1", delta="Working")
        await _wait_for_sent(sent, 4)
        service._accept_backend_events(
            (
                BackendLifecycleEvent(
                    lifecycle=BackendLifecycle.FINISHED,
                    source_event_id="synthetic-finished",
                ),
            )
        )
        await _wait_for_sent(sent, 5)
        gateway._publish_token_delta(session_id="session-1", delta="late")
        await asyncio.sleep(0.05)
        await incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(task, timeout=1)
        return sent

    sent = asyncio.run(scenario())

    running = json.loads(cast(str, sent[2]["text"]))
    token = json.loads(cast(str, sent[3]["text"]))
    finished = json.loads(cast(str, sent[4]["text"]))
    assert running["projection"]["lifecycle"]["status"] == "running"
    assert token["events"] == []
    assert token["projection"]["streamingText"] == "Working"
    assert token["projection"]["streamRevision"] == 1
    assert finished["projection"]["lifecycle"]["status"] == "finished"
    assert finished["projection"]["streamingText"] == ""
    assert finished["projection"]["streamRevision"] == 2
    assert len(sent) == 5


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
            _websocket_scope(
                "/sessions/session-1/events",
                query_string=b"after=0",
            ),
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
        app = GatewayAsgiApp(
            gateway,
            ingress=IngressPolicy.create(external_base_path="/proxy/8767"),
        )
        incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return await incoming.get()

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await incoming.put({"type": "websocket.disconnect"})
        await app(
            _websocket_scope(
                "/proxy/8767/sessions/session-1/events",
                query_string=b"after=0",
            ),
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
                _http_scope(
                    "GET",
                    "/sessions/session-1/events/stream",
                    query_string=b"after=0",
                ),
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


def test_asgi_sse_streams_transient_projection_updates(tmp_path: Path) -> None:
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
                _http_scope("GET", "/sessions/session-1/events/stream"),
                receive,
                send,
            )
        )
        await _wait_for_sent(sent, 2)
        service = gateway._service("session-1")
        service._accept_backend_events(
            (
                BackendLifecycleEvent(
                    lifecycle=BackendLifecycle.RUNNING,
                    source_event_id="synthetic-running",
                ),
            )
        )
        await _wait_for_sent(sent, 3)
        gateway._publish_token_delta(session_id="session-1", delta="Working")
        await _wait_for_sent(sent, 4)
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, timeout=1)
        return sent

    sent = asyncio.run(scenario())

    body = cast(bytes, sent[3]["body"]).decode("utf-8")
    data = json.loads(body.split("data: ", maxsplit=1)[1])
    assert data["events"] == []
    assert data["projection"]["lifecycle"]["status"] == "running"
    assert data["projection"]["streamingText"] == "Working"
    assert data["projection"]["streamRevision"] == 1


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
            ingress=IngressPolicy.create(external_base_path="/proxy/8767"),
        )
        return await _http_call(
            app,
            method="GET",
            path="/proxy/8767/assets/app.js",
        )

    sent = asyncio.run(scenario())

    assert sent[0]["status"] == 200
    assert cast(bytes, sent[1]["body"]).decode("utf-8") == "console.log('heartwood')"


def test_asgi_base_path_cannot_be_bypassed_by_a_direct_api_route(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        app = GatewayAsgiApp(
            _gateway(tmp_path),
            ingress=IngressPolicy.create(external_base_path="/proxy/8767"),
        )
        return await _http_call(app, method="GET", path="/sessions")

    sent = asyncio.run(scenario())

    assert sent[0]["status"] == 404
    response_headers = cast(list[tuple[bytes, bytes]], sent[0]["headers"])
    assert (b"cache-control", b"no-store") in response_headers
    assert (b"x-content-type-options", b"nosniff") in response_headers
    assert json.loads(cast(bytes, sent[1]["body"])) == {
        "code": "HW-INGRESS-002",
        "error": "request path is outside the configured gateway base path",
    }


def test_asgi_injects_the_gateway_owned_jupyter_base_path(tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        '<html><HEAD lang="en"></HEAD><body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    external_base = "/proxy/project/runtime/jupyter/proxy/8767"

    async def scenario() -> list[dict[str, object]]:
        app = GatewayAsgiApp(
            _gateway(tmp_path / "project"),
            static_dir=static_dir,
            ingress=IngressPolicy.create(
                mode="jupyter-proxy",
                external_origin="https://notebooks.firecloud.org",
                external_base_path=external_base,
            ),
        )
        return await _http_call(
            app,
            method="GET",
            path="/",
            origin="https://notebooks.firecloud.org",
        )

    sent = asyncio.run(scenario())
    body = cast(bytes, sent[1]["body"]).decode("utf-8")

    assert sent[0]["status"] == 200
    assert f'<meta name="heartwood-gateway-base" content="{external_base}" />' in body


def test_browser_base_path_injection_is_idempotent_and_binary_safe() -> None:
    existing = (
        b'<html><head><meta name="heartwood-gateway-base" content="/existing" /></head></html>'
    )
    binary = b"<html><head>\xff</head></html>"

    assert _inject_browser_base_path(existing, browser_base_path="/new") == existing
    assert _inject_browser_base_path(binary, browser_base_path="/new") == binary


def test_asgi_jupyter_proxy_uses_one_stripped_route_for_rest_sse_and_websocket(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        gateway = _gateway(tmp_path)
        gateway.handle(SessionCommand.model_validate_json(_command(CommandKind.CHAT, prompt="hi")))
        app = GatewayAsgiApp(
            gateway,
            ingress=IngressPolicy.create(
                mode="jupyter-proxy",
                external_origin="https://notebooks.firecloud.org",
                external_base_path="/proxy/project/runtime/jupyter/proxy/8767",
            ),
        )
        rest = await _http_call(
            app,
            method="GET",
            path="/sessions",
            origin="https://notebooks.firecloud.org",
        )

        sse_incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        sse_sent: list[dict[str, object]] = []

        async def sse_receive() -> dict[str, object]:
            return await sse_incoming.get()

        async def sse_send(message: dict[str, object]) -> None:
            sse_sent.append(message)

        sse_task = asyncio.create_task(
            app(
                _http_scope(
                    "GET",
                    "/sessions/session-1/events/stream",
                    origin="https://notebooks.firecloud.org",
                ),
                sse_receive,
                sse_send,
            )
        )
        await _wait_for_sent(sse_sent, 2)
        await sse_incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(sse_task, timeout=1)

        websocket_incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        websocket_sent: list[dict[str, object]] = []

        async def websocket_receive() -> dict[str, object]:
            return await websocket_incoming.get()

        async def websocket_send(message: dict[str, object]) -> None:
            websocket_sent.append(message)

        await websocket_incoming.put({"type": "websocket.disconnect"})
        await app(
            _websocket_scope(
                "/sessions/session-1/events",
                origin="https://notebooks.firecloud.org",
            ),
            websocket_receive,
            websocket_send,
        )
        return rest, sse_sent, websocket_sent

    rest, sse, websocket = asyncio.run(scenario())

    assert rest[0]["status"] == 200
    assert sse[0]["status"] == 200
    assert websocket[0]["type"] == "websocket.accept"


def test_asgi_trusted_proxy_uses_one_preserved_route_for_api_and_assets(
    tmp_path: Path,
) -> None:
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        '<html><head></head><body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    forwarded = (
        (b"x-forwarded-for", b"198.51.100.22"),
        (b"x-forwarded-host", b"heartwood.example"),
        (b"x-forwarded-prefix", b"/research/heartwood"),
        (b"x-forwarded-proto", b"https"),
    )
    app = GatewayAsgiApp(
        _gateway(tmp_path / "project"),
        static_dir=static_dir,
        ingress=IngressPolicy.create(
            mode="trusted-proxy",
            bind_host="0.0.0.0",
            external_origin="https://heartwood.example",
            external_base_path="/research/heartwood",
            trusted_proxy_sources=("10.10.0.0/24",),
        ),
    )

    async def scenario() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        api = await _http_call(
            app,
            method="GET",
            path="/research/heartwood/sessions",
            headers=forwarded,
            client=("10.10.0.4", 43123),
            host="heartwood.example",
            origin="https://heartwood.example",
        )
        asset = await _http_call(
            app,
            method="GET",
            path="/research/heartwood/",
            headers=forwarded,
            client=("10.10.0.4", 43123),
            host="heartwood.example",
            origin="https://heartwood.example",
        )
        return api, asset

    api, asset = asyncio.run(scenario())

    assert api[0]["status"] == 200
    body = cast(bytes, asset[1]["body"]).decode("utf-8")
    assert asset[0]["status"] == 200
    assert '<meta name="heartwood-gateway-base" content="/research/heartwood" />' in body


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

        await app(_websocket_scope("/unknown"), receive, send)
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
            _websocket_scope("/sessions/invalid!session/events"),
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
    headers: tuple[tuple[bytes, bytes], ...] = (),
    client: tuple[str, int] = ("127.0.0.1", 43123),
    host: str = "127.0.0.1:8767",
    origin: str | None = "http://127.0.0.1:8767",
) -> list[dict[str, object]]:
    messages = iter(({"type": "http.request", "body": body, "more_body": False},))
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return next(messages)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(
        _http_scope(
            method,
            path,
            query_string=query_string,
            headers=headers,
            client=client,
            host=host,
            origin=origin,
        ),
        receive,
        send,
    )
    return sent


def _http_scope(
    method: str,
    path: str,
    *,
    query_string: bytes = b"",
    headers: tuple[tuple[bytes, bytes], ...] = (),
    client: tuple[str, int] = ("127.0.0.1", 43123),
    host: str = "127.0.0.1:8767",
    origin: str | None = "http://127.0.0.1:8767",
) -> dict[str, object]:
    request_headers = [(b"host", host.encode("ascii")), *headers]
    if origin is not None:
        request_headers.append((b"origin", origin.encode("ascii")))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query_string,
        "headers": request_headers,
        "client": client,
    }


def _websocket_scope(
    path: str,
    *,
    query_string: bytes = b"",
    headers: tuple[tuple[bytes, bytes], ...] = (),
    client: tuple[str, int] = ("127.0.0.1", 43123),
    host: str = "127.0.0.1:8767",
    origin: str | None = "http://127.0.0.1:8767",
) -> dict[str, object]:
    request_headers = [(b"host", host.encode("ascii")), *headers]
    if origin is not None:
        request_headers.append((b"origin", origin.encode("ascii")))
    return {
        "type": "websocket",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query_string,
        "headers": request_headers,
        "client": client,
    }


async def _wait_for_sent(sent: list[dict[str, object]], count: int) -> None:
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        if len(sent) >= count:
            return
        await asyncio.sleep(0.01)
    assert len(sent) >= count
