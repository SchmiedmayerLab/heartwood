# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import ipaddress
import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from uuid import uuid4

import certifi
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from openhands.sdk.event.conversation_state import FULL_STATE_KEY, ConversationStateUpdateEvent
from openhands.sdk.workspace import RemoteWorkspace
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosedError
from websockets.http11 import Request, Response

from heartwood.core_adapter.worker_attempts import WorkerAttemptLease
from heartwood.gateway._remote_workspace import (
    WorkerResponseBudget,
    WorkerResponseError,
    WorkerResponseTransport,
)
from heartwood.gateway._worker_events import AdmittedWorkerWorkspace, WorkerEventAdmission


def _event() -> str:
    return ConversationStateUpdateEvent(key="execution_status", value="idle").model_dump_json()


def test_worker_workspace_requires_supported_sdk(
    worker_attempt: Callable[[str], WorkerAttemptLease],
) -> None:
    if callable(getattr(RemoteWorkspace, "connect_websocket", None)):
        workspace = AdmittedWorkerWorkspace(
            host="http://worker.test",
            working_dir="/workspace",
            attempt=worker_attempt("http://worker.test"),
        )
        assert "_response_budget" not in workspace.model_dump()
    else:
        values = {
            "host": "http://worker.test",
            "working_dir": "/workspace",
            "api_key": "synthetic-private-key",
        }
        constructors: tuple[Callable[[], object], ...] = (
            lambda: AdmittedWorkerWorkspace(
                host="http://worker.test",
                working_dir="/workspace",
                api_key="synthetic-private-key",
                attempt=worker_attempt("http://worker.test"),
            ),
            lambda: AdmittedWorkerWorkspace.model_validate(values),
        )
        for construct in constructors:
            with pytest.raises(RuntimeError, match="required worker event admission") as error:
                construct()
            assert "synthetic-private-key" not in str(error.value)


@pytest.mark.parametrize(
    "message",
    [
        b"{}",
        "[]",
        '{"kind":"MissingEvent","private":"synthetic-secret"}',
        '{"x":1,"x":2}',
        '{"n":NaN}',
        '{"n":1e999}',
        "\ud800",
        "[" * 70 + "0" + "]" * 70,
        '{"kind":"LLMCompletionLogEvent","filename":"/private/synthetic"}',
        "x" * (4 * 1024 * 1024 + 1),
    ],
)
def test_rejected_events_close_shared_admission_without_payloads(message: str | bytes) -> None:
    budget = WorkerResponseBudget()
    policy = WorkerEventAdmission(host="http://worker.test", tool_modules={}, budget=budget)
    with pytest.raises(WorkerResponseError, match="worker event failed admission") as error:
        policy.validate_message(message)
    assert str(error.value) == "worker event failed admission"
    with pytest.raises(WorkerResponseError, match="closed"):
        budget.charge(0, new_response=True)


def test_event_bytes_and_http_bodies_share_one_budget() -> None:
    message = _event()
    budget = WorkerResponseBudget(max_bytes=len(message.encode()) + 2)
    policy = WorkerEventAdmission(host="http://worker.test", tool_modules={}, budget=budget)
    policy.validate_message(message)
    transport = WorkerResponseTransport(
        host="http://worker.test",
        tool_modules={},
        budget=budget,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )
    with httpx.Client(transport=transport, base_url="http://worker.test") as client:
        assert client.get("/").json() == {}
        with pytest.raises(WorkerResponseError):
            policy.validate_message(message)
        with pytest.raises(WorkerResponseError, match="closed"):
            client.get("/")


@pytest.mark.parametrize(
    "url",
    [
        "ws://other.test/sockets/events/{id}",
        "wss://worker.test/sockets/events/{id}",
        "ws://worker.test:81/sockets/events/{id}",
        "ws://user:secret@worker.test/sockets/events/{id}",
        "ws://worker.test/sockets/events/{id}?x=1",
        "ws://worker.test/sockets/events/{id}#fragment",
        "ws://worker.test/other/{id}",
        "ws://worker.test/sockets/events/%31{id}",
        "ws://worker.test/sockets/events/not-an-id",
    ],
)
def test_event_endpoint_rejected_before_connect(url: str) -> None:
    budget = WorkerResponseBudget()
    policy = WorkerEventAdmission(host="http://worker.test", tool_modules={}, budget=budget)
    with pytest.raises(WorkerResponseError, match="not authorized"):
        policy.connect(url.format(id=uuid4()), max_size=4096)
    with pytest.raises(WorkerResponseError, match="closed"):
        budget.charge(0)


def test_direct_event_connection_ignores_proxy_and_disables_compression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "WS_PROXY",
        "WSS_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "ws_proxy",
        "wss_proxy",
    ):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    observed = []

    async def run() -> None:
        async def handler(socket: ServerConnection) -> None:
            assert socket.request is not None
            observed.append(socket.request.headers.get("Sec-WebSocket-Extensions"))
            await socket.send(_event())

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            policy = WorkerEventAdmission(
                host=f"http://127.0.0.1:{port}", tool_modules={}, budget=WorkerResponseBudget()
            )
            async with policy.connect(
                f"ws://127.0.0.1:{port}/sockets/events/{uuid4()}", max_size=4096
            ) as socket:
                policy.validate_message(await socket.recv())

    asyncio.run(run())
    assert observed == [None]


def test_redirect_never_reaches_second_origin_or_sends_credentials() -> None:
    reached = []
    budget = WorkerResponseBudget()

    async def run() -> None:
        async def target(socket: ServerConnection) -> None:
            reached.append(await socket.recv())

        async with serve(target, "127.0.0.1", 0) as destination:
            destination_port = destination.sockets[0].getsockname()[1]

            def redirect(socket: ServerConnection, _request: Request) -> Response:
                response = socket.respond(HTTPStatus.FOUND, "synthetic redirect")
                response.headers["Location"] = (
                    f"ws://127.0.0.1:{destination_port}/sockets/events/{uuid4()}"
                )
                return response

            async def unused(_socket: ServerConnection) -> None:
                pytest.fail("redirect was accepted")

            async with serve(unused, "127.0.0.1", 0, process_request=redirect) as source:
                port = source.sockets[0].getsockname()[1]
                policy = WorkerEventAdmission(
                    host=f"http://127.0.0.1:{port}", tool_modules={}, budget=budget
                )
                with pytest.raises(WorkerResponseError, match="connection failed admission"):
                    async with policy.connect(
                        f"ws://127.0.0.1:{port}/sockets/events/{uuid4()}", max_size=4096
                    ) as socket:
                        await socket.send("synthetic-worker-key")

    asyncio.run(run())
    assert reached == []
    with pytest.raises(WorkerResponseError, match="closed"):
        budget.charge(0)


def test_transport_receive_limit_closes_admission() -> None:
    budget = WorkerResponseBudget()

    async def run() -> None:
        async def handler(socket: ServerConnection) -> None:
            await socket.send("x" * 65)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            policy = WorkerEventAdmission(
                host=f"http://127.0.0.1:{port}", tool_modules={}, budget=budget
            )
            with pytest.raises(WorkerResponseError, match="byte limit"):
                async with policy.connect(
                    f"ws://127.0.0.1:{port}/sockets/events/{uuid4()}", max_size=64
                ) as socket:
                    await socket.recv()

    asyncio.run(run())
    with pytest.raises(WorkerResponseError, match="closed"):
        budget.charge(0)


@pytest.mark.parametrize("trusted", [False, True])
def test_event_tls_uses_explicit_roots_not_ambient_certificates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted: bool
) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic worker")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / "ca.pem"
    key_path = tmp_path / "key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, key_path)
    for variable in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.setenv(variable, str(certificate_path))
    if trusted:
        monkeypatch.setattr(certifi, "where", lambda: str(certificate_path))
    budget = WorkerResponseBudget()

    async def run() -> None:
        async def handler(socket: ServerConnection) -> None:
            await socket.send(_event())

        async with serve(handler, "127.0.0.1", 0, ssl=context) as server:
            port = server.sockets[0].getsockname()[1]
            policy = WorkerEventAdmission(
                host=f"https://127.0.0.1:{port}", tool_modules={}, budget=budget
            )
            async with policy.connect(
                f"wss://127.0.0.1:{port}/sockets/events/{uuid4()}", max_size=4096
            ) as socket:
                policy.validate_message(await socket.recv())

    if trusted:
        asyncio.run(run())
    else:
        with pytest.raises(WorkerResponseError, match="connection failed admission"):
            asyncio.run(run())
        with pytest.raises(WorkerResponseError, match="closed"):
            budget.charge(0)


@pytest.mark.parametrize("full_state", [False, True])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("agent", {"kind": "ACPAgent"}),
        ("agent", {"kind": "Agent", "llm": {"log_completions": True}}),
        ("tool_module_qualnames", {"forged": "unlisted"}),
    ],
)
def test_state_events_cannot_bypass_metadata_admission(
    full_state: bool, key: str, value: dict[str, object]
) -> None:
    event = ConversationStateUpdateEvent(
        key=FULL_STATE_KEY if full_state else key, value={key: value} if full_state else value
    )
    policy = WorkerEventAdmission(
        host="http://worker.test", tool_modules={}, budget=WorkerResponseBudget()
    )
    with pytest.raises(WorkerResponseError, match="failed admission"):
        policy.validate_message(event.model_dump_json())


@pytest.mark.parametrize("code", [1007, 1012, 4001])
def test_close_reasons_are_sanitized_without_losing_retry_codes(code: int) -> None:
    budget = WorkerResponseBudget()

    async def run() -> None:
        async def handler(socket: ServerConnection) -> None:
            await socket.close(code=code, reason="synthetic-private-close")

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            policy = WorkerEventAdmission(
                host=f"http://127.0.0.1:{port}", tool_modules={}, budget=budget
            )
            async with policy.connect(
                f"ws://127.0.0.1:{port}/sockets/events/{uuid4()}", max_size=4096
            ) as socket:
                await socket.recv()

    if code == 1007:
        with pytest.raises(WorkerResponseError, match="protocol"):
            asyncio.run(run())
        with pytest.raises(WorkerResponseError, match="closed"):
            budget.charge(0)
    else:
        with pytest.raises(ConnectionClosedError) as error:
            asyncio.run(run())
        assert error.value.rcvd is not None
        assert error.value.rcvd.code == code
        assert "synthetic-private" not in str(error.value)
        budget.charge(0)


def test_malformed_handshake_does_not_expose_worker_headers() -> None:
    async def run() -> None:
        def malformed(socket: ServerConnection, _request: Request) -> Response:
            response = socket.respond(HTTPStatus.SWITCHING_PROTOCOLS, "")
            response.headers["Connection"] = "Upgrade"
            response.headers["Upgrade"] = "websocket"
            response.headers["Sec-WebSocket-Accept"] = "synthetic-private-handshake"
            return response

        async def unused(_socket: ServerConnection) -> None:
            pass

        async with serve(unused, "127.0.0.1", 0, process_request=malformed) as server:
            port = server.sockets[0].getsockname()[1]
            policy = WorkerEventAdmission(
                host=f"http://127.0.0.1:{port}", tool_modules={}, budget=WorkerResponseBudget()
            )
            with pytest.raises(WorkerResponseError) as error:
                async with policy.connect(
                    f"ws://127.0.0.1:{port}/sockets/events/{uuid4()}", max_size=4096
                ):
                    pytest.fail("invalid handshake accepted")
            assert str(error.value) == "worker event connection failed admission"

    asyncio.run(run())
