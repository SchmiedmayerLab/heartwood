# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Caller-owned admission for native worker event connections."""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import UUID

import certifi
import httpx
from openhands.sdk.event import Event
from openhands.sdk.event.conversation_state import FULL_STATE_KEY, ConversationStateUpdateEvent
from openhands.sdk.workspace import RemoteWorkspace
from pydantic import Field, PrivateAttr, model_validator
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    InvalidHandshake,
    InvalidStatus,
)
from websockets.frames import Close, CloseCode

from heartwood.core_adapter.worker_attempts import WorkerAttemptLease
from heartwood.gateway._remote_workspace import (
    _MAX_RESPONSE_BYTES,
    AdmittedRemoteWorkspace,
    WorkerResponseBudget,
    WorkerResponseError,
    _admit_json,
    _admit_metadata,
    _worker_origin,
)


class _DirectConnection(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        if isinstance(exc, InvalidStatus):
            return WorkerResponseError("worker event handshake was rejected")
        return exc


class WorkerEventAdmission:
    """Share HTTP admission limits while retaining native WebSocket ownership."""

    def __init__(
        self, *, host: str, tool_modules: Mapping[str, str], budget: WorkerResponseBudget
    ) -> None:
        self._origin = _worker_origin(host)
        self._budget = budget
        self._tool_modules = dict(tool_modules)
        if budget.attempt is not None and self._origin != _worker_origin(
            str(budget.attempt.runtime.endpoint)
        ):
            raise WorkerResponseError("worker origin does not match its retained binding")

    def connect(self, url: str, *, max_size: int) -> AbstractAsyncContextManager[ClientConnection]:
        """Open only the configured worker's native event endpoint."""
        self._budget.charge(0, new_response=True)
        try:
            target = httpx.URL(url)
            prefix = "/sockets/events/"
            identity = UUID(target.path.removeprefix(prefix))
            if (
                self._budget.attempt is not None
                and identity != self._budget.attempt.intent.conversation_id
            ):
                raise ValueError
            expected = self._origin.copy_with(
                scheme="wss" if self._origin.scheme == "https" else "ws",
                path=f"{prefix}{identity}",
            )
            if target != expected or target.raw_path != expected.raw_path or max_size <= 0:
                raise ValueError
        except (ValueError, httpx.InvalidURL):
            self._budget.close()
            raise WorkerResponseError("worker event endpoint is not authorized") from None
        context = (
            ssl.create_default_context(cafile=certifi.where())
            if self._origin.scheme == "https"
            else None
        )
        connection = _DirectConnection(
            url,
            proxy=None,
            compression=None,
            ssl=context,
            max_size=min(max_size, _MAX_RESPONSE_BYTES),
            max_queue=4,
            open_timeout=10,
            close_timeout=5,
        )
        return self._open(connection)

    @asynccontextmanager
    async def _open(self, connection: _DirectConnection) -> AsyncIterator[ClientConnection]:
        try:
            async with connection as socket:
                self._budget.charge(0)
                yield socket
        except (WorkerResponseError, ssl.SSLError, InvalidHandshake):
            self._budget.close()
            raise WorkerResponseError("worker event connection failed admission") from None
        except ConnectionClosed as error:
            rejected_codes = {
                CloseCode.PROTOCOL_ERROR,
                CloseCode.INVALID_DATA,
                CloseCode.POLICY_VIOLATION,
                CloseCode.MESSAGE_TOO_BIG,
                CloseCode.MANDATORY_EXTENSION,
            }
            if any(
                frame is not None and frame.code in rejected_codes
                for frame in (error.sent, error.rcvd)
            ):
                self._budget.close()
                raise WorkerResponseError("worker event protocol or byte limit rejected") from None
            raise ConnectionClosedError(
                Close(error.rcvd.code, "") if error.rcvd else None,
                Close(error.sent.code, "") if error.sent else None,
                error.rcvd_then_sent,
            ) from None

    def validate_message(self, message: str | bytes) -> None:
        """Admit bounded text and native event schemas without exposing rejected data."""
        try:
            if not isinstance(message, str) or len(message) > _MAX_RESPONSE_BYTES:
                raise ValueError
            content = message.encode("utf-8")
            self._budget.charge(len(content), new_response=True)
            if len(content) > _MAX_RESPONSE_BYTES:
                raise ValueError
            value = _admit_json(content, self._tool_modules)
            if not isinstance(value, dict) or value.get("kind") == "LLMCompletionLogEvent":
                raise ValueError
            event = Event.model_validate(value)
            if isinstance(event, ConversationStateUpdateEvent):
                metadata = event.value if event.key == FULL_STATE_KEY else {event.key: event.value}
                if not isinstance(metadata, dict):
                    raise ValueError
                _admit_metadata(metadata, self._tool_modules)
                attempt = self._budget.attempt
                if (
                    attempt is not None
                    and (event.key == FULL_STATE_KEY or "id" in metadata)
                    and metadata.get("id") != str(attempt.intent.conversation_id)
                ):
                    raise ValueError
            self._budget.charge(0)
        except Exception:
            self._budget.close()
            raise WorkerResponseError("worker event failed admission") from None


class AdmittedWorkerWorkspace(AdmittedRemoteWorkspace):
    """HTTP and event admission, requiring the SDK's public transport hooks."""

    attempt: WorkerAttemptLease = Field(exclude=True, repr=False, frozen=True)
    _event_admission: WorkerEventAdmission | None = PrivateAttr(default=None)

    @model_validator(mode="before")
    @classmethod
    def require_event_hooks(cls, data: object) -> object:
        """Reject unsupported SDKs before parsing or retaining credentials."""
        if not all(
            callable(getattr(RemoteWorkspace, name, None))
            for name in ("connect_websocket", "validate_websocket_message")
        ):
            raise RuntimeError("installed SDK does not support required worker event admission")
        return data

    @property
    def _events(self) -> WorkerEventAdmission:
        if self._event_admission is None:
            self._event_admission = WorkerEventAdmission(
                host=self.host, tool_modules=dict(self.tool_modules), budget=self._response_budget
            )
        return self._event_admission

    def connect_websocket(
        self, url: str, *, max_size: int
    ) -> AbstractAsyncContextManager[ClientConnection]:
        """Supply the native SDK's connection with trusted transport policy."""
        return self._events.connect(url, max_size=max_size)

    def validate_websocket_message(self, message: str | bytes) -> None:
        """Apply the lifetime response budget and event schema before callbacks."""
        self._events.validate_message(message)
