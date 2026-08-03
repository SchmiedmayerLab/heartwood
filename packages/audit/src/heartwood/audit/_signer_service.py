# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Loopback-only service implementing the checkpoint signer protocol."""

from __future__ import annotations

import hmac
import ipaddress
import json
from collections.abc import Awaitable, Callable, Mapping

from pydantic import ValidationError

from heartwood.audit._signer import (
    CheckpointSigner,
    CheckpointSignerError,
    validate_checkpoint_signer_token,
)
from heartwood.schemas import AuditCheckpointSignRequest

type AsgiMessage = dict[str, object]
type AsgiReceive = Callable[[], Awaitable[AsgiMessage]]
type AsgiScope = Mapping[str, object]
type AsgiSend = Callable[[AsgiMessage], Awaitable[None]]

_MAXIMUM_REQUEST_BYTES = 64 * 1024


class LocalCheckpointSignerApp:
    """Expose one local signer over an authenticated loopback ASGI endpoint."""

    def __init__(self, signer: CheckpointSigner, *, authorization_token: str) -> None:
        self.signer = signer
        token = validate_checkpoint_signer_token(authorization_token)
        self._authorization = f"Bearer {token}".encode()

    async def __call__(self, scope: AsgiScope, receive: AsgiReceive, send: AsgiSend) -> None:
        """Handle one ASGI lifespan or HTTP connection."""
        scope_type = _scope_value(scope, "type")
        if scope_type == "lifespan":
            await _handle_lifespan(receive, send)
            return
        if scope_type != "http":
            raise ValueError(f"unsupported local signer scope: {scope_type}")
        await self._handle_http(scope, receive, send)

    async def _handle_http(
        self,
        scope: AsgiScope,
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        if not _is_loopback_client(scope):
            await _send_json(send, status_code=403, value={"error": "loopback access required"})
            return
        method = _scope_value(scope, "method")
        path = _scope_value(scope, "path")
        if method == "GET" and path == "/healthz":
            await _send_json(send, status_code=200, value={"status": "ready"})
            return
        if method != "POST" or path != "/v1/checkpoints/sign":
            await _send_json(send, status_code=404, value={"error": "unknown signer route"})
            return
        if not _authorized(scope, self._authorization):
            await _send_json(send, status_code=401, value={"error": "authorization required"})
            return
        if _header(scope, b"content-type") != b"application/json":
            await _send_json(send, status_code=415, value={"error": "application/json required"})
            return
        try:
            body = await _read_body(receive)
            request = AuditCheckpointSignRequest.model_validate_json(body)
            signature = self.signer.sign(request.statement)
        except _RequestTooLargeError:
            await _send_json(send, status_code=413, value={"error": "request exceeds 64 KiB"})
            return
        except (CheckpointSignerError, ValidationError, ValueError):
            await _send_json(send, status_code=422, value={"error": "invalid signing request"})
            return
        await _send_json(send, status_code=200, value=signature.model_dump(mode="json"))


class _RequestTooLargeError(ValueError):
    """Raised when a signer request exceeds the bounded protocol envelope."""


async def _handle_lifespan(receive: AsgiReceive, send: AsgiSend) -> None:
    while True:
        message = await receive()
        message_type = message.get("type")
        if message_type == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message_type == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


async def _read_body(receive: AsgiReceive) -> bytes:
    body = bytearray()
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            raise ValueError("invalid signer request body")
        chunk = message.get("body", b"")
        if not isinstance(chunk, bytes):
            raise ValueError("invalid signer request body")
        body.extend(chunk)
        if len(body) > _MAXIMUM_REQUEST_BYTES:
            raise _RequestTooLargeError
        if not message.get("more_body", False):
            return bytes(body)


async def _send_json(send: AsgiSend, *, status_code: int, value: object) -> None:
    body = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _scope_value(scope: AsgiScope, key: str) -> str:
    value = scope.get(key)
    if not isinstance(value, str):
        raise ValueError(f"invalid local signer {key}")
    return value


def _is_loopback_client(scope: AsgiScope) -> bool:
    client = scope.get("client")
    if client is None:
        return False
    if not isinstance(client, tuple) or not client or not isinstance(client[0], str):
        return False
    try:
        return ipaddress.ip_address(client[0]).is_loopback
    except ValueError:
        return False


def _authorized(scope: AsgiScope, expected: bytes) -> bool:
    authorization = _header(scope, b"authorization")
    return authorization is not None and hmac.compare_digest(authorization, expected)


def _header(scope: AsgiScope, name: bytes) -> bytes | None:
    headers = scope.get("headers", ())
    if not isinstance(headers, list | tuple):
        return None
    matches: list[bytes] = []
    for header in headers:
        if (
            isinstance(header, tuple | list)
            and len(header) == 2
            and header[0] == name
            and isinstance(header[1], bytes)
        ):
            matches.append(header[1])
    return matches[0] if len(matches) == 1 else None
