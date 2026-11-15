# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import httpx
import pytest
import uvicorn
from openhands.sdk import Agent, Conversation, RemoteConversation
from openhands.sdk.conversation.base import BaseConversation
from openhands.sdk.conversation.exceptions import WebSocketConnectionError
from openhands.sdk.conversation.request import StartConversationRequest
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent
from openhands.sdk.testing import TestLLM
from openhands.sdk.tool.client_tool import ClientToolSpec
from openhands.sdk.workspace import LocalWorkspace, RemoteWorkspace
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from heartwood.core_adapter.worker_attempts import WorkerAttemptLease
from heartwood.gateway._remote_workspace import (
    AdmittedRemoteWorkspace,
    WorkerResponseBudget,
    WorkerResponseError,
    WorkerResponseTransport,
)
from heartwood.gateway._worker_events import AdmittedWorkerWorkspace


def _request(
    content: bytes,
    *,
    modules: dict[str, str] | None = None,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    transport = WorkerResponseTransport(
        host="http://worker.test",
        tool_modules=modules or {},
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status,
                stream=httpx.ByteStream(content),
                headers=headers or {"Content-Type": "application/json"},
            )
        ),
    )
    with httpx.Client(transport=transport) as client:
        return client.get("http://worker.test/api/conversations/fixture")


@pytest.mark.parametrize(
    "body",
    [
        b'{"tool_module_qualnames":{"terminal":"unlisted.module"}}',
        b'{"tool_module_qualnames":[]}',
        b'{"tool_module_qualnames":null}',
        b'{"tool_module_qualnames":{"terminal":1}}',
        b'{"tool_module_qualnames":{"x":"unlisted"},"tool_modules":{"x":"unlisted"}}',
        b'{"client_tools":[{"name":"forged"}]}',
        b'{"agent":{"kind":"ACPAgent"}}',
        b'{"agent":{"kind":"Agent","llm":{"log_completions":true}}}',
        b'{"agent":{"kind":"Agent","other_llms":[{"log_completions":"false"}]}}',
        b'{"tool_module_qualnames":{},"tool_module_qualnames":{}}',
        b'{"nested":{"x":1,"x":2}}',
        b'{"n":NaN}',
        b'{"n":1e9999}',
        b'{"n":Infinity}',
        b"\xff",
        b"[] trailing",
        b"[" * 70 + b"0" + b"]" * 70,
        b"[" * 2000 + b"0" + b"]" * 2000,
    ],
)
def test_response_rejects_unsafe_metadata(body: bytes) -> None:
    with pytest.raises(WorkerResponseError):
        _request(body)


def test_exact_tool_pair_is_admitted_without_changing_response() -> None:
    body = b'{"tool_module_qualnames":{"terminal":"trusted.terminal"}}'
    assert _request(body, modules={"terminal": "trusted.terminal"}).content == body
    with pytest.raises(WorkerResponseError):
        _request(body, modules={"different_tool": "trusted.terminal"})


def test_valid_metadata_and_exact_byte_limit_are_preserved() -> None:
    body = b'{"agent":{"kind":"Agent","llm":{"log_completions":false}}}'
    body += b" " * (4 * 1024 * 1024 - len(body))
    assert _request(body).content == body
    with pytest.raises(WorkerResponseError, match="byte limit"):
        _request(body + b" ")


@pytest.mark.parametrize(
    "host",
    [
        "file:///worker",
        "http://worker.test/prefix",
        "http://user:secret@worker.test",
        "http://worker.test/?q=1",
        "http://worker.test/#fragment",
    ],
)
def test_worker_host_requires_an_unambiguous_origin(host: str) -> None:
    with pytest.raises(ValueError, match="origin"):
        WorkerResponseTransport(
            host=host, tool_modules={}, transport=httpx.MockTransport(lambda _: httpx.Response(204))
        )


def test_transport_snapshots_caller_policy() -> None:
    modules: dict[str, str] = {}
    transport = WorkerResponseTransport(
        host="http://worker.test",
        tool_modules=modules,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"tool_module_qualnames": {"x": "unlisted"}})
        ),
    )
    modules["x"] = "unlisted"
    with httpx.Client(transport=transport) as client, pytest.raises(WorkerResponseError):
        client.get("http://worker.test/")


@pytest.mark.parametrize("status", [301, 307, 400, 404, 500])
def test_errors_never_expose_response_content(status: int) -> None:
    with pytest.raises(WorkerResponseError) as result:
        _request(b"private worker content", status=status)
    assert "private worker content" not in str(result.value)


def test_rejects_compression_and_non_json() -> None:
    with pytest.raises(WorkerResponseError, match="encoded"):
        _request(b"compressed", headers={"Content-Encoding": "gzip"})
    with pytest.raises(WorkerResponseError, match="JSON"):
        _request(b"{}", headers={"Content-Type": "text/html"})
    with pytest.raises(WorkerResponseError, match="empty"):
        _request(b"")
    assert _request(b"", status=204).content == b""


class _Chunks(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.read_count = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(10):
            self.read_count += 1
            yield b" " * (1024 * 1024)

    def close(self) -> None:
        self.closed = True


def test_stream_stops_at_limit_and_closes_without_trusting_length() -> None:
    stream = _Chunks()
    transport = WorkerResponseTransport(
        host="http://worker.test",
        tool_modules={},
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=stream, headers={"Content-Length": "2"})
        ),
    )
    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(WorkerResponseError, match="byte limit"),
    ):
        client.get("http://worker.test/")
    assert stream.read_count == 5
    assert stream.closed


def test_other_origin_is_rejected_before_transport() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    transport = WorkerResponseTransport(
        host="http://worker.test", tool_modules={}, transport=httpx.MockTransport(handle)
    )
    with httpx.Client(transport=transport, follow_redirects=True) as client:
        for url in ("http://other.test/", "https://worker.test/", "http://worker.test:81/"):
            with pytest.raises(WorkerResponseError):
                client.get(url)
    assert not calls


@pytest.mark.parametrize(("max_bytes", "max_responses"), [(0, 1), (1, 0), (-1, 1), (True, 1)])
def test_response_budget_requires_positive_integer_limits(
    max_bytes: int, max_responses: int
) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        WorkerResponseBudget(max_bytes=max_bytes, max_responses=max_responses)


@pytest.mark.parametrize(
    ("status", "body", "max_bytes", "max_responses"), [(200, b"{}", 4, 5), (204, b"", 4, 2)]
)
def test_lifetime_response_budget_is_not_refunded(
    status: int, body: bytes, max_bytes: int, max_responses: int
) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, content=body, headers={"Content-Type": "application/json"})

    transport = WorkerResponseTransport(
        host="http://worker.test",
        tool_modules={},
        transport=httpx.MockTransport(handle),
        budget=WorkerResponseBudget(max_bytes=max_bytes, max_responses=max_responses),
    )
    with httpx.Client(transport=transport, base_url="http://worker.test") as client:
        assert client.get("/").content == body
        assert client.get("/").content == body
        with pytest.raises(WorkerResponseError, match="budget exhausted"):
            client.get("/")
        count_at_rejection = len(calls)
        with pytest.raises(WorkerResponseError, match="closed"):
            client.get("/")
        assert len(calls) == count_at_rejection
        assert len(calls) == (3 if body else 2)


def test_budget_closes_response_stream_at_cumulative_limit() -> None:
    stream = _Chunks()
    transport = WorkerResponseTransport(
        host="http://worker.test",
        tool_modules={},
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
        budget=WorkerResponseBudget(max_bytes=1024 * 1024),
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(WorkerResponseError, match="budget exhausted"):
            client.get("http://worker.test/")
        assert stream.read_count == 2
        assert stream.closed


def test_concurrent_rejection_prevents_pending_response_admission() -> None:
    reading = Event()
    resume = Event()
    closed = Event()

    class PendingBody(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"{}"
            reading.set()
            assert resume.wait(timeout=5)

        def close(self) -> None:
            closed.set()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=PendingBody()
            if request.url.path == "/pending"
            else httpx.ByteStream(b"invalid"),
            headers={"Content-Type": "application/json"},
        )

    transport = WorkerResponseTransport(
        host="http://worker.test", tool_modules={}, transport=httpx.MockTransport(handle)
    )
    with httpx.Client(transport=transport, base_url="http://worker.test") as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.get, "/pending")
            try:
                assert reading.wait(timeout=5)
                with pytest.raises(WorkerResponseError, match="invalid JSON"):
                    client.get("/invalid")
            finally:
                resume.set()
            with pytest.raises(WorkerResponseError, match="closed"):
                pending.result(timeout=5)
        assert closed.is_set()


def test_network_errors_consume_request_budget() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("synthetic connection failure")

    transport = WorkerResponseTransport(
        host="http://worker.test",
        tool_modules={},
        transport=httpx.MockTransport(handle),
        budget=WorkerResponseBudget(max_responses=1),
    )
    with httpx.Client(transport=transport, base_url="http://worker.test") as client:
        with pytest.raises(httpx.ConnectError):
            client.get("/")
        with pytest.raises(WorkerResponseError, match="budget exhausted"):
            client.get("/")
    assert len(calls) == 1


def test_client_reset_does_not_renew_request_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(handle))
    workspace = AdmittedRemoteWorkspace(host="http://worker.test", working_dir="/workspace")
    try:
        for _ in range(1024):
            workspace.client.get("/")
            workspace.reset_client()
        with pytest.raises(WorkerResponseError, match="budget exhausted"):
            workspace.client.get("/")
        workspace.reset_client()
        with pytest.raises(WorkerResponseError, match="closed"):
            workspace.client.get("/")
        assert len(calls) == 1024
    finally:
        workspace.reset_client()


@contextmanager
def _worker(metadata: dict[str, object]) -> Iterator[str]:
    payload = json.dumps(metadata).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.do_GET()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("entry", ["attach", "create", "resume", "factory"])
def test_native_sdk_rejects_whole_registration_set_before_import(
    entry: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"worker_import_probe_{uuid4().hex}"
    marker = tmp_path / "imported"
    (tmp_path / f"{module_name}.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    agent = Agent(llm=TestLLM(model="test-model"), tools=[])
    identity = uuid4()
    with _worker(
        {
            "id": str(identity),
            "tool_module_qualnames": {"permitted": module_name, "forged": module_name},
            "agent": {"kind": "invalid"},
        }
    ) as host:
        workspace = AdmittedRemoteWorkspace(
            host=host, working_dir="/workspace", tool_modules=(("permitted", module_name),)
        )

        def connect() -> None:
            if entry == "attach":
                RemoteConversation.attach(workspace, identity, visualizer=None)
            elif entry == "create":
                RemoteConversation.create(
                    workspace,
                    StartConversationRequest(
                        agent=agent, workspace=LocalWorkspace(working_dir="/workspace")
                    ),
                    visualizer=None,
                )
            elif entry == "resume":
                RemoteConversation(
                    agent=agent, workspace=workspace, conversation_id=identity, visualizer=None
                )
            else:
                Conversation(
                    agent=agent, workspace=workspace, conversation_id=identity, visualizer=None
                )

        try:
            with pytest.raises(WorkerResponseError, match="registrations"):
                connect()
            assert not marker.exists()
            assert module_name not in sys.modules
        finally:
            workspace.reset_client()


def test_client_reset_preserves_admission_and_policy_is_not_serialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with _worker({"tool_module_qualnames": {"forged": "unlisted"}}) as host:
        workspace = AdmittedRemoteWorkspace(
            host=host, working_dir="/workspace", api_key="synthetic-worker-key"
        )
        assert isinstance(workspace, RemoteWorkspace)
        assert "tool_modules" not in workspace.model_dump()
        assert "api_key" not in workspace.model_dump()
        assert "synthetic-worker-key" not in repr(workspace)
        first_client = workspace.client
        for _ in range(2):
            with pytest.raises(WorkerResponseError):
                RemoteConversation.attach(workspace, uuid4(), visualizer=None)
            workspace.reset_client()
        assert first_client.is_closed


@pytest.mark.parametrize("workspace_type", [AdmittedRemoteWorkspace, AdmittedWorkerWorkspace])
def test_native_create_attach_and_fork_preserve_admission(
    workspace_type: type[AdmittedRemoteWorkspace],
    caplog: pytest.LogCaptureFixture,
    worker_attempt: Callable[[str], WorkerAttemptLease],
) -> None:
    if workspace_type is AdmittedWorkerWorkspace and not callable(
        getattr(RemoteWorkspace, "connect_websocket", None)
    ):
        pytest.skip("installed SDK lacks required worker event hooks")
    agent = Agent(llm=TestLLM(model="test-model"), tools=[])
    poisoned = False
    reject_events = False
    worker_key = "synthetic-event-key"
    client_spec = ClientToolSpec(
        name="propose_review_operation",
        description="Propose an operation for outside review.",
        parameters={
            "type": "object",
            "properties": {"reference": {"type": "string"}},
            "required": ["reference"],
            "additionalProperties": False,
        },
    )

    async def metadata(request: Request) -> JSONResponse:
        assert request.headers.get("X-Session-API-Key") == worker_key
        if request.url.path.endswith("/events/search"):
            return JSONResponse({"items": [], "next_page_id": None})
        is_fork = request.url.path.endswith("/fork")
        identity = (
            (await request.json())["conversation_id"]
            if request.url.path == "/api/conversations"
            else str(uuid4())
            if is_fork
            else request.path_params["identity"]
        )
        return JSONResponse(
            {
                "id": identity,
                "agent": agent.model_dump(mode="json"),
                "max_iterations": 10,
                "execution_status": "idle",
                "tool_module_qualnames": {"forged": "unlisted"} if poisoned and is_fork else {},
                "client_tools": [
                    spec.model_dump(mode="json") for spec in workspace.authorized_client_tools
                ],
            }
        )

    async def events(socket: WebSocket) -> None:
        await socket.accept()
        assert await socket.receive_json() == {"type": "auth", "session_api_key": worker_key}
        try:
            while True:
                await socket.send_text(
                    '{"kind":"MissingEvent","private":"synthetic-private-event"}'
                    if reject_events
                    else ConversationStateUpdateEvent(
                        key="execution_status", value="idle"
                    ).model_dump_json()
                )
                await asyncio.sleep(0.02)
        except WebSocketDisconnect:
            return

    app = Starlette(
        routes=[
            Route("/api/conversations", metadata, methods=["POST"]),
            Route(
                "/api/conversations/{identity}/{operation:path}", metadata, methods=["GET", "POST"]
            ),
            Route("/api/conversations/{identity}", metadata),
            WebSocketRoute("/sockets/events/{identity}", events),
        ]
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        host = f"http://127.0.0.1:{listener.getsockname()[1]}"
        attempt = worker_attempt(host) if workspace_type is AdmittedWorkerWorkspace else None
        workspace = workspace_type(
            host=host,
            attempt=attempt,
            client_tools=(client_spec,) if attempt else (),
            working_dir="/workspace",
            api_key=worker_key,
        )
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            if attempt is not None:
                created = RemoteConversation.create(
                    workspace,
                    StartConversationRequest(
                        agent=agent,
                        conversation_id=attempt.intent.conversation_id,
                        workspace=LocalWorkspace(working_dir="/workspace"),
                        client_tools=workspace.authorized_client_tools,
                    ),
                    visualizer=None,
                )
                assert created.id == attempt.intent.conversation_id
                created.close()
            parent = RemoteConversation.attach(
                workspace, attempt.intent.conversation_id if attempt else uuid4(), visualizer=None
            )
            try:
                if attempt is None:
                    child = parent.fork()
                    try:
                        assert child.id != parent.id
                        assert child.workspace is workspace
                    finally:
                        child.close()
                if workspace_type is AdmittedWorkerWorkspace:
                    reject_events = True
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            _ = parent.state
                        except WebSocketConnectionError:
                            break
                        assert time.monotonic() < deadline, "event rejection was not surfaced"
                        time.sleep(0.01)
                    conversation: BaseConversation = parent
                    with pytest.raises(WebSocketConnectionError, match="admission"):
                        conversation.send_message("Must not reach the worker")
                    workspace.reset_client()
                    with pytest.raises(WorkerResponseError, match="closed"):
                        workspace.client.get("/api/conversations/fixture")
                    assert "synthetic-private-event" not in caplog.text
                else:
                    poisoned = True
                    with pytest.raises(WorkerResponseError, match="registrations"):
                        parent.fork()
            finally:
                parent.close()
        finally:
            workspace.reset_client()
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
