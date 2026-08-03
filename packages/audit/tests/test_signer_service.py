# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Boundary tests for the authenticated local checkpoint signer service."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from heartwood.audit import (
    CheckpointSignerError,
    LocalCheckpointSignerApp,
    LocalEd25519CheckpointSigner,
)
from heartwood.schemas import AuditCheckpointSignRequest, AuditCheckpointStatement, AuditRetention


def test_local_signer_service_authenticates_and_returns_typed_signature(tmp_path: Path) -> None:
    app = LocalCheckpointSignerApp(_signer(tmp_path), authorization_token="synthetic-token")
    request = AuditCheckpointSignRequest(statement=_statement()).model_dump_json().encode()

    status, response = _request(
        app,
        body=request,
        headers=[
            (b"authorization", b"Bearer synthetic-token"),
            (b"content-type", b"application/json"),
        ],
    )

    assert status == 200
    assert response["algorithm"] == "ed25519"
    assert response["signer_id"] == "local-development"


def test_local_signer_service_rejects_missing_auth_remote_clients_and_media_type(
    tmp_path: Path,
) -> None:
    app = LocalCheckpointSignerApp(_signer(tmp_path), authorization_token="synthetic-token")
    request = AuditCheckpointSignRequest(statement=_statement()).model_dump_json().encode()

    assert _request(app, body=request, headers=[])[0] == 401
    assert (
        _request(
            app,
            body=request,
            headers=[(b"authorization", b"Bearer synthetic-token")],
        )[0]
        == 415
    )
    assert (
        _request(
            app,
            body=request,
            client="192.0.2.10",
            headers=[
                (b"authorization", b"Bearer synthetic-token"),
                (b"content-type", b"application/json"),
            ],
        )[0]
        == 403
    )


def test_local_signer_service_bounds_requests_and_exposes_minimal_health(tmp_path: Path) -> None:
    app = LocalCheckpointSignerApp(_signer(tmp_path), authorization_token="synthetic-token")

    assert _request(app, method="GET", path="/healthz")[1] == {"status": "ready"}
    status, response = _request(
        app,
        body=b"x" * (64 * 1024 + 1),
        headers=[
            (b"authorization", b"Bearer synthetic-token"),
            (b"content-type", b"application/json"),
        ],
    )
    assert status == 413
    assert response == {"error": "request exceeds 64 KiB"}


def test_local_signer_service_fails_closed_on_invalid_protocol_inputs(tmp_path: Path) -> None:
    signer = _signer(tmp_path)
    with pytest.raises(CheckpointSignerError, match="authorization token is invalid"):
        LocalCheckpointSignerApp(signer, authorization_token=" ")
    app = LocalCheckpointSignerApp(signer, authorization_token="synthetic-token")
    auth = [
        (b"authorization", b"Bearer synthetic-token"),
        (b"content-type", b"application/json"),
    ]

    assert _request(app, path="/unknown", headers=auth)[0] == 404
    assert _request(app, body=b"{", headers=auth)[0] == 422
    assert _request(app, client=None, headers=auth)[0] == 403
    assert (
        _request(
            app,
            headers=[
                (b"authorization", b"Bearer synthetic-token"),
                (b"authorization", b"Bearer synthetic-token"),
                (b"content-type", b"application/json"),
            ],
        )[0]
        == 401
    )


def test_local_signer_service_completes_asgi_lifespan(tmp_path: Path) -> None:
    app = LocalCheckpointSignerApp(_signer(tmp_path), authorization_token="synthetic-token")
    messages: tuple[dict[str, object], ...] = (
        {"type": "lifespan.startup"},
        {"type": "lifespan.shutdown"},
    )
    received: Iterator[dict[str, object]] = iter(messages)
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return next(received)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    asyncio.run(app({"type": "lifespan"}, receive, send))

    assert sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]


def _request(
    app: LocalCheckpointSignerApp,
    *,
    method: str = "POST",
    path: str = "/v1/checkpoints/sign",
    body: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    client: str | None = "127.0.0.1",
) -> tuple[int, dict[str, object]]:
    received: Iterator[dict[str, object]] = iter(
        ({"type": "http.request", "body": body, "more_body": False},)
    )
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return next(received)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope: dict[str, object] = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [] if headers is None else headers,
    }
    if client is not None:
        scope["client"] = (client, 44000)
    asyncio.run(app(scope, receive, send))
    status = sent[0]["status"]
    response_body = sent[1]["body"]
    assert isinstance(status, int)
    assert isinstance(response_body, bytes)
    decoded = json.loads(response_body)
    assert isinstance(decoded, dict)
    return status, decoded


def _signer(root: Path) -> LocalEd25519CheckpointSigner:
    key = Ed25519PrivateKey.generate()
    path = root / "private.pem"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return LocalEd25519CheckpointSigner(
        private_key=path,
        signer_id="local-development",
        key_id="local-checkpoint-signing",
        key_version="v1",
    )


def _statement() -> AuditCheckpointStatement:
    return AuditCheckpointStatement(
        deployment_id="development",
        session_id="session-1",
        created_at="2026-08-02T12:00:00Z",
        audit_event_count=0,
        terminal_event_hash=None,
        audit_content_sha256=f"sha256:{'a' * 64}",
        audit_size_bytes=0,
        retention=AuditRetention(policy_id="development", retain_until="2026-08-02"),
    )
