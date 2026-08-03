# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Provider-neutral checkpoint signing and verification."""

from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import stat
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import ValidationError

from heartwood.persistence import DurableFileError, read_private_bytes
from heartwood.schemas import (
    AuditCheckpointSignature,
    AuditCheckpointSignRequest,
    AuditCheckpointStatement,
)

_CHECKPOINT_DOMAIN = b"heartwood.audit-checkpoint-signature.v1\x00"
_MAXIMUM_KEY_FILE_BYTES = 64 * 1024
_MAXIMUM_SIGNER_RESPONSE_BYTES = 64 * 1024

type CheckpointPublicKey = Ed25519PublicKey | ec.EllipticCurvePublicKey


class CheckpointSignerError(ValueError):
    """Raised when a checkpoint signer cannot safely complete a request."""


class CheckpointSigner(Protocol):
    """Sign canonical checkpoint statements without exposing private key material."""

    def sign(self, statement: AuditCheckpointStatement) -> AuditCheckpointSignature:
        """Sign one validated checkpoint statement."""


class CheckpointSignerTransport(Protocol):
    """Bounded transport used by the remote signer client."""

    def post(
        self,
        endpoint: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> object:
        """Post one request and return its decoded JSON value."""


class LocalEd25519CheckpointSigner:
    """Development signer that keeps an owner-only key in its own process."""

    def __init__(
        self,
        *,
        private_key: Path,
        signer_id: str,
        key_id: str,
        key_version: str,
    ) -> None:
        self._private_key = _load_private_key(private_key)
        self._signer_id = signer_id
        self._key_id = key_id
        self._key_version = key_version

    @property
    def public_key(self) -> Ed25519PublicKey:
        """Return the public half for local trust-bundle generation."""
        return self._private_key.public_key()

    def sign(self, statement: AuditCheckpointStatement) -> AuditCheckpointSignature:
        """Sign one statement with the isolated local development key."""
        unsigned = AuditCheckpointSignature(
            algorithm="ed25519",
            signer_id=self._signer_id,
            key_id=self._key_id,
            key_version=self._key_version,
            public_key_sha256=checkpoint_public_key_fingerprint(self.public_key),
            value=base64.b64encode(bytes(64)).decode("ascii"),
        )
        value = self._private_key.sign(
            checkpoint_signature_payload_bytes(statement=statement, signature=unsigned)
        )
        return unsigned.model_copy(
            update={"value": base64.b64encode(value).decode("ascii")},
        )


class RemoteCheckpointSigner:
    """Client for the provider-neutral checkpoint signer HTTP contract."""

    def __init__(
        self,
        *,
        endpoint: str,
        authorization_token: str | None = None,
        timeout_seconds: float = 15.0,
        allow_insecure_loopback: bool = False,
        transport: CheckpointSignerTransport | None = None,
    ) -> None:
        self.endpoint = _validate_signer_endpoint(
            endpoint,
            allow_insecure_loopback=allow_insecure_loopback,
        )
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or timeout_seconds > 120:
            raise CheckpointSignerError("signer timeout must be between 0 and 120 seconds")
        self._authorization_token = (
            None
            if authorization_token is None
            else validate_checkpoint_signer_token(authorization_token)
        )
        self.timeout_seconds = timeout_seconds
        self.transport = transport or _UrlLibSignerTransport()

    def sign(self, statement: AuditCheckpointStatement) -> AuditCheckpointSignature:
        """Submit one canonical statement and validate the signature envelope."""
        request = AuditCheckpointSignRequest(statement=statement)
        body = _canonical_json(request.model_dump(mode="json")).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._authorization_token is not None:
            headers["Authorization"] = f"Bearer {self._authorization_token}"
        try:
            response = self.transport.post(
                self.endpoint,
                body=body,
                headers=headers,
                timeout_seconds=self.timeout_seconds,
            )
            return AuditCheckpointSignature.model_validate(response)
        except CheckpointSignerError:
            raise
        except ValidationError as error:
            raise CheckpointSignerError("checkpoint signer returned an invalid response") from error


def checkpoint_signature_payload_bytes(
    *,
    statement: AuditCheckpointStatement,
    signature: AuditCheckpointSignature,
) -> bytes:
    """Return the canonical identity and statement bytes signed by every backend."""
    payload = {
        "algorithm": signature.algorithm,
        "key_id": signature.key_id,
        "key_version": signature.key_version,
        "public_key_sha256": signature.public_key_sha256,
        "signer_id": signature.signer_id,
        "statement": statement.model_dump(mode="json"),
    }
    return _CHECKPOINT_DOMAIN + _canonical_json(payload).encode("utf-8")


def validate_checkpoint_signer_token(value: str) -> str:
    """Validate one bounded RFC 6750 bearer token without logging its value."""
    if (
        len(value) > _MAXIMUM_SIGNER_RESPONSE_BYTES
        or re.fullmatch(r"[A-Za-z0-9\-._~+/]+=*", value) is None
    ):
        raise CheckpointSignerError("signer authorization token is invalid")
    return value


def checkpoint_public_key_fingerprint(key: CheckpointPublicKey) -> str:
    """Identify a public key by its canonical SubjectPublicKeyInfo encoding."""
    encoded = key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def load_checkpoint_public_key(path: Path) -> CheckpointPublicKey:
    """Load one bounded Ed25519 or P-256 public key from a trusted path."""
    _key_metadata(path, private=False)
    try:
        key = serialization.load_pem_public_key(read_private_bytes(path))
    except (DurableFileError, OSError, ValueError) as error:
        raise CheckpointSignerError("trusted checkpoint public key is not valid PEM") from error
    if isinstance(key, Ed25519PublicKey):
        return key
    if isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1):
        return key
    raise CheckpointSignerError("trusted checkpoint public key must use Ed25519 or P-256")


def verify_checkpoint_signature(
    *,
    statement: AuditCheckpointStatement,
    signature: AuditCheckpointSignature,
    public_key: CheckpointPublicKey,
) -> None:
    """Verify one signature against an independently trusted public key."""
    if signature.public_key_sha256 != checkpoint_public_key_fingerprint(public_key):
        raise CheckpointSignerError("checkpoint signer does not match the trusted public key")
    try:
        value = base64.b64decode(signature.value, validate=True)
        payload = checkpoint_signature_payload_bytes(
            statement=statement,
            signature=signature,
        )
        if signature.algorithm == "ed25519" and isinstance(public_key, Ed25519PublicKey):
            public_key.verify(value, payload)
            return
        if signature.algorithm == "ecdsa-p256-sha256" and isinstance(
            public_key, ec.EllipticCurvePublicKey
        ):
            public_key.verify(value, payload, ec.ECDSA(hashes.SHA256()))
            return
    except (InvalidSignature, ValueError) as error:
        raise CheckpointSignerError("audit checkpoint signature is invalid") from error
    raise CheckpointSignerError("checkpoint signature algorithm does not match the trusted key")


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    metadata = _key_metadata(path, private=True)
    if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CheckpointSignerError("local signer key permissions must be owner-only")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise CheckpointSignerError("local signer key must be owned by the current user")
    try:
        key = serialization.load_pem_private_key(read_private_bytes(path), password=None)
    except (DurableFileError, OSError, TypeError, ValueError) as error:
        raise CheckpointSignerError(
            "local signer key is not a valid unencrypted PEM key"
        ) from error
    if not isinstance(key, Ed25519PrivateKey):
        raise CheckpointSignerError("local signer key must use Ed25519")
    return key


def _key_metadata(path: Path, *, private: bool) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        label = "local signer key" if private else "trusted checkpoint public key"
        raise CheckpointSignerError(f"{label} is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAXIMUM_KEY_FILE_BYTES:
        label = "local signer key" if private else "trusted checkpoint public key"
        raise CheckpointSignerError(f"{label} must be a bounded regular file")
    if not private and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise CheckpointSignerError(
            "trusted checkpoint public key must not be writable by other users"
        )
    if not private and hasattr(os, "geteuid") and metadata.st_uid not in {0, os.geteuid()}:
        raise CheckpointSignerError(
            "trusted checkpoint public key must be owned by root or the current user"
        )
    return metadata


def _validate_signer_endpoint(endpoint: str, *, allow_insecure_loopback: bool) -> str:
    normalized = endpoint.strip()
    if len(normalized) > 2048:
        raise CheckpointSignerError("signer endpoint must not exceed 2048 characters")
    if any(ord(character) < 0x20 for character in normalized):
        raise CheckpointSignerError("signer endpoint is not a valid URL")
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as error:
        raise CheckpointSignerError("signer endpoint is not a valid URL") from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/v1/checkpoints/sign"
        or hostname is None
    ):
        raise CheckpointSignerError(
            "signer endpoint must be an HTTP(S) /v1/checkpoints/sign URL without credentials"
        )
    if parsed.scheme == "https":
        return normalized
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = False
    if not allow_insecure_loopback or not loopback:
        raise CheckpointSignerError("production signer endpoints must use HTTPS")
    return normalized


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class _UrlLibSignerTransport:
    def post(
        self,
        endpoint: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> object:
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers=dict(headers),
            method="POST",
        )
        opener = urllib.request.build_opener(_RejectRedirects())
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                content_type = response.headers.get_content_type()
                payload = response.read(_MAXIMUM_SIGNER_RESPONSE_BYTES + 1)
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as error:
            raise CheckpointSignerError("checkpoint signer request failed") from error
        if content_type != "application/json":
            raise CheckpointSignerError("checkpoint signer returned an invalid content type")
        if len(payload) > _MAXIMUM_SIGNER_RESPONSE_BYTES:
            raise CheckpointSignerError("checkpoint signer response exceeds 64 KiB")
        try:
            return cast(object, json.loads(payload))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CheckpointSignerError("checkpoint signer returned invalid JSON") from error


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _req: urllib.request.Request,
        _fp: object,
        _code: int,
        _msg: str,
        _headers: http.client.HTTPMessage,
        _newurl: str,
    ) -> None:
        return None
