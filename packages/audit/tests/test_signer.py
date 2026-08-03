# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Conformance tests for provider-neutral checkpoint signers."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.request import Request

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from heartwood.audit import (
    CheckpointSignerError,
    LocalEd25519CheckpointSigner,
    RemoteCheckpointSigner,
    checkpoint_public_key_fingerprint,
    checkpoint_signature_payload_bytes,
    verify_checkpoint_signature,
)
from heartwood.schemas import (
    AuditCheckpointSignature,
    AuditCheckpointSignRequest,
    AuditCheckpointStatement,
    AuditRetention,
)


class _SigningTransport:
    def __init__(self, signer: LocalEd25519CheckpointSigner) -> None:
        self.signer = signer
        self.calls: list[tuple[str, Mapping[str, str], float]] = []

    def post(
        self,
        endpoint: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> object:
        request = AuditCheckpointSignRequest.model_validate_json(body)
        self.calls.append((endpoint, headers, timeout_seconds))
        return self.signer.sign(request.statement).model_dump(mode="json")


class _InvalidTransport:
    def post(
        self,
        endpoint: str,  # noqa: ARG002
        *,
        body: bytes,  # noqa: ARG002
        headers: Mapping[str, str],  # noqa: ARG002
        timeout_seconds: float,  # noqa: ARG002
    ) -> object:
        return {"signature": "not a signature envelope"}


class _HttpResponse:
    def __init__(self, payload: bytes, *, content_type: str = "application/json") -> None:
        self.payload = payload
        self.content_type = content_type

    @property
    def headers(self) -> _HttpResponse:
        return self

    def get_content_type(self) -> str:
        return self.content_type

    def read(self, amount: int) -> bytes:  # noqa: ARG002
        return self.payload

    def __enter__(self) -> _HttpResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _HttpOpener:
    def __init__(self, response: _HttpResponse | OSError) -> None:
        self.response = response
        self.request: Request | None = None
        self.timeout: float | None = None

    def open(self, request: Request, *, timeout: float) -> _HttpResponse:
        self.request = request
        self.timeout = timeout
        if isinstance(self.response, OSError):
            raise self.response
        return self.response


def test_remote_signer_uses_canonical_typed_contract_and_bearer_auth(tmp_path: Path) -> None:
    signer = _local_signer(tmp_path)
    transport = _SigningTransport(signer)
    remote = RemoteCheckpointSigner(
        endpoint="https://signer.example/v1/checkpoints/sign",
        authorization_token="synthetic-token",
        timeout_seconds=9,
        transport=transport,
    )

    signature = remote.sign(_statement())

    assert signature == signer.sign(_statement())
    assert transport.calls == [
        (
            "https://signer.example/v1/checkpoints/sign",
            {
                "Accept": "application/json",
                "Authorization": "Bearer synthetic-token",
                "Content-Type": "application/json",
            },
            9,
        )
    ]


def test_remote_signer_http_transport_interoperates_with_the_wire_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = _local_signer(tmp_path)
    opener = _HttpOpener(_HttpResponse(signer.sign(_statement()).model_dump_json().encode()))
    monkeypatch.setattr(
        "heartwood.audit._signer.urllib.request.build_opener",
        lambda *_handlers: opener,
    )
    signature = RemoteCheckpointSigner(
        endpoint="https://signer.example/v1/checkpoints/sign",
        authorization_token="synthetic-token",
        timeout_seconds=7,
    ).sign(_statement())

    verify_checkpoint_signature(
        statement=_statement(),
        signature=signature,
        public_key=signer.public_key,
    )
    assert opener.request is not None
    assert opener.request.full_url == "https://signer.example/v1/checkpoints/sign"
    assert opener.request.get_header("Authorization") == "Bearer synthetic-token"
    assert isinstance(opener.request.data, bytes)
    assert (
        AuditCheckpointSignRequest.model_validate_json(opener.request.data).statement
        == _statement()
    )
    assert opener.timeout == 7


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("error", "request failed"),
        ("content-type", "content type"),
        ("invalid-json", "invalid JSON"),
        ("oversized", "exceeds 64 KiB"),
    ],
)
def test_remote_signer_http_transport_fails_closed_on_invalid_responses(
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["error", "content-type", "invalid-json", "oversized"],
    message: str,
) -> None:
    response: _HttpResponse | OSError
    if mode == "error":
        response = OSError("synthetic signer outage")
    elif mode == "content-type":
        response = _HttpResponse(b"{}", content_type="text/plain")
    elif mode == "invalid-json":
        response = _HttpResponse(b"{")
    else:
        response = _HttpResponse(b"x" * (64 * 1024 + 1))
    monkeypatch.setattr(
        "heartwood.audit._signer.urllib.request.build_opener",
        lambda *_handlers: _HttpOpener(response),
    )
    with pytest.raises(CheckpointSignerError, match=message):
        RemoteCheckpointSigner(
            endpoint="https://signer.example/v1/checkpoints/sign",
            authorization_token="synthetic-token",
        ).sign(_statement())


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://signer.example/v1/checkpoints/sign",
        "https://user:secret@signer.example/v1/checkpoints/sign",
        "https://signer.example/other",
        "https://signer.example/v1/checkpoints/sign?redirect=1",
        "https://signer.example:invalid/v1/checkpoints/sign",
        "https://signer.example:65536/v1/checkpoints/sign",
        "https://signer.example/v1/checkpoints/sign\nX-Test: injected",
    ],
)
def test_remote_signer_rejects_unsafe_endpoints(endpoint: str) -> None:
    with pytest.raises(CheckpointSignerError):
        RemoteCheckpointSigner(endpoint=endpoint)

    with pytest.raises(CheckpointSignerError):
        RemoteCheckpointSigner(endpoint=endpoint, allow_insecure_loopback=True)


def test_remote_signer_bounds_endpoint_length() -> None:
    with pytest.raises(CheckpointSignerError, match="2048"):
        RemoteCheckpointSigner(
            endpoint=f"https://signer.example/{'x' * 2048}",
        )


def test_remote_signer_allows_only_explicit_numeric_loopback_http() -> None:
    RemoteCheckpointSigner(
        endpoint="http://127.0.0.1:8771/v1/checkpoints/sign",
        allow_insecure_loopback=True,
    )
    with pytest.raises(CheckpointSignerError, match="HTTPS"):
        RemoteCheckpointSigner(
            endpoint="http://localhost:8771/v1/checkpoints/sign",
            allow_insecure_loopback=True,
        )


def test_remote_signer_rejects_invalid_response_and_timeout() -> None:
    with pytest.raises(CheckpointSignerError, match="invalid response"):
        RemoteCheckpointSigner(
            endpoint="https://signer.example/v1/checkpoints/sign",
            transport=_InvalidTransport(),
        ).sign(_statement())
    for timeout in (0, 121, float("nan")):
        with pytest.raises(CheckpointSignerError, match="timeout"):
            RemoteCheckpointSigner(
                endpoint="https://signer.example/v1/checkpoints/sign",
                timeout_seconds=timeout,
            )
    for token in ("", "contains space", "contains\nnewline", "\x00"):
        with pytest.raises(CheckpointSignerError, match="authorization token"):
            RemoteCheckpointSigner(
                endpoint="https://signer.example/v1/checkpoints/sign",
                authorization_token=token,
            )


def test_p256_signature_verifies_with_algorithm_bound_to_key() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    statement = _statement()
    unsigned = AuditCheckpointSignature(
        algorithm="ecdsa-p256-sha256",
        signer_id="records",
        key_id="kms-signing",
        key_version="4",
        public_key_sha256=checkpoint_public_key_fingerprint(key.public_key()),
        value=base64.b64encode(b"unsigned").decode("ascii"),
    )
    value = key.sign(
        checkpoint_signature_payload_bytes(statement=statement, signature=unsigned),
        ec.ECDSA(hashes.SHA256()),
    )
    signature = unsigned.model_copy(
        update={"value": base64.b64encode(value).decode("ascii")},
    )

    verify_checkpoint_signature(
        statement=statement,
        signature=signature,
        public_key=key.public_key(),
    )

    mismatched = signature.model_copy(update={"algorithm": "ed25519"})
    with pytest.raises(CheckpointSignerError, match="algorithm does not match"):
        verify_checkpoint_signature(
            statement=statement,
            signature=mismatched,
            public_key=key.public_key(),
        )

    changed_identity = signature.model_copy(update={"key_version": "5"})
    with pytest.raises(CheckpointSignerError, match="signature is invalid"):
        verify_checkpoint_signature(
            statement=statement,
            signature=changed_identity,
            public_key=key.public_key(),
        )


def test_signature_payload_is_canonical_domain_separated_and_identity_bound(
    tmp_path: Path,
) -> None:
    signature = _local_signer(tmp_path).sign(_statement())
    payload = checkpoint_signature_payload_bytes(
        statement=_statement(),
        signature=signature,
    )

    assert payload.startswith(b"heartwood.audit-checkpoint-signature.v1\x00")
    decoded = json.loads(payload.split(b"\x00", maxsplit=1)[1])
    assert decoded["statement"] == _statement().model_dump(mode="json")
    assert decoded["signer_id"] == "records"
    assert decoded["key_id"] == "audit-signing"
    assert "value" not in decoded


def _statement() -> AuditCheckpointStatement:
    return AuditCheckpointStatement(
        deployment_id="research-environment",
        session_id="session-1",
        created_at="2026-08-02T12:00:00Z",
        audit_event_count=1,
        terminal_event_hash=f"sha256:{'a' * 64}",
        audit_content_sha256=f"sha256:{'b' * 64}",
        audit_size_bytes=128,
        retention=AuditRetention(
            policy_id="research-audit-7y",
            retain_until="2033-08-02",
        ),
    )


def _local_signer(root: Path) -> LocalEd25519CheckpointSigner:
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
        signer_id="records",
        key_id="audit-signing",
        key_version="v1",
    )
