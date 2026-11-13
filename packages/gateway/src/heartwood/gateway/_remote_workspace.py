# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""HTTP admission for the native remote workspace's untrusted responses."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from threading import Lock
from typing import Any

import httpx
from openhands.sdk.workspace import RemoteWorkspace
from pydantic import Field, PrivateAttr

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_JSON_DEPTH = 64


class WorkerResponseError(ValueError):
    """A remote response failed admission before SDK deserialization."""


class WorkerResponseBudget:
    """Lifetime admission shared by a workspace's response transports."""

    def __init__(self, *, max_bytes: int = 32 * 1024 * 1024, max_responses: int = 1024) -> None:
        if type(max_bytes) is not int or type(max_responses) is not int:
            raise ValueError("worker response limits must be positive integers")
        if max_bytes <= 0 or max_responses <= 0:
            raise ValueError("worker response limits must be positive integers")
        self._remaining_bytes = max_bytes
        self._remaining_responses = max_responses
        self._closed = False
        self._lock = Lock()

    def charge(self, size: int, *, new_response: bool = False) -> None:
        """Charge received bytes or reserve a request before contacting the worker."""
        if size < 0:
            raise ValueError("worker response size must not be negative")
        with self._lock:
            if self._closed:
                raise WorkerResponseError("worker response admission is closed")
            if size > self._remaining_bytes or (new_response and not self._remaining_responses):
                self._closed = True
                raise WorkerResponseError("worker response budget exhausted")
            self._remaining_bytes -= size
            if new_response:
                self._remaining_responses -= 1

    def close(self) -> None:
        """Refuse further admission without replenishing the lifetime budget."""
        with self._lock:
            self._closed = True


def _worker_origin(host: str) -> httpx.URL:
    origin = httpx.URL(host)
    if (
        origin.scheme not in {"http", "https"}
        or not origin.host
        or origin.userinfo
        or origin.path != "/"
        or origin.query
        or origin.fragment
    ):
        raise ValueError("worker host must be a complete server origin")
    return origin


class WorkerResponseTransport(httpx.BaseTransport):
    """Bound responses and admit module names before handing bytes to the SDK."""

    def __init__(
        self,
        *,
        host: str,
        tool_modules: Mapping[str, str],
        transport: httpx.BaseTransport,
        budget: WorkerResponseBudget | None = None,
    ) -> None:
        self._origin = _worker_origin(host)
        self._tool_modules = dict(tool_modules)
        self._transport = transport
        self._budget = budget if budget is not None else WorkerResponseBudget()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._budget.charge(0, new_response=True)
        if (
            request.url.scheme != self._origin.scheme
            or request.url.host != self._origin.host
            or request.url.port != self._origin.port
            or request.url.userinfo
            or request.url.fragment
        ):
            self._budget.close()
            raise WorkerResponseError("worker request left the configured origin")
        request.headers["Accept-Encoding"] = "identity"
        response = self._transport.handle_request(request)
        try:
            if response.is_redirect:
                raise WorkerResponseError("worker redirects are not permitted")
            if not response.is_success:
                raise WorkerResponseError("worker request did not succeed")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise WorkerResponseError("encoded worker responses are not permitted")
            body = bytearray()
            stream = response.stream
            assert isinstance(stream, httpx.SyncByteStream)
            for chunk in stream:
                self._budget.charge(len(chunk))
                if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise WorkerResponseError("worker response exceeds the byte limit")
                body.extend(chunk)
            content = bytes(body)
            if content:
                media_type = response.headers.get("content-type", "").split(";", 1)[0]
                if media_type.strip().lower() != "application/json":
                    raise WorkerResponseError("worker response must be JSON")
                _admit_json(content, self._tool_modules)
            elif response.status_code != 204:
                raise WorkerResponseError("worker response is empty")
            self._budget.charge(0)
            return httpx.Response(response.status_code, headers=response.headers, content=content)
        except WorkerResponseError:
            self._budget.close()
            raise
        finally:
            response.close()

    def close(self) -> None:
        self._transport.close()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkerResponseError("worker JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise WorkerResponseError("worker JSON contains a nonfinite number")


def _admit_json(content: bytes, tool_modules: Mapping[str, str]) -> object:
    try:
        value: object = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        raise WorkerResponseError("worker response is invalid JSON") from None
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > _MAX_JSON_DEPTH:
            raise WorkerResponseError("worker JSON exceeds the depth limit")
        if isinstance(item, dict):
            pending.extend((entry, depth + 1) for entry in item.values())
        elif isinstance(item, list):
            pending.extend((entry, depth + 1) for entry in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise WorkerResponseError("worker JSON contains a nonfinite number")
    if not isinstance(value, dict):
        return value
    _admit_metadata(value, tool_modules)
    return value


def _admit_metadata(value: dict[str, Any], tool_modules: Mapping[str, str]) -> None:
    registrations = value.get("tool_module_qualnames", {})
    if not isinstance(registrations, dict) or any(
        not isinstance(name, str) or not isinstance(module, str) or tool_modules.get(name) != module
        for name, module in registrations.items()
    ):
        raise WorkerResponseError("worker tool registrations are not authorized")
    if value.get("client_tools") not in (None, []):
        raise WorkerResponseError("worker-selected client tools are not authorized")
    agent = value.get("agent")
    if agent is not None:
        if not isinstance(agent, dict) or agent.get("kind") != "Agent":
            raise WorkerResponseError("worker agent kind is not authorized")
        _reject_completion_logging(agent)


def _reject_completion_logging(agent: dict[str, Any]) -> None:
    # RemoteConversation installs a local file-writing callback when any returned
    # LLM enables completion logging. Worker metadata cannot grant that authority.
    pending: list[Any] = [agent]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            setting = item.get("log_completions")
            if setting is not None and setting is not False:
                raise WorkerResponseError("worker-selected completion logging is not authorized")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


class AdmittedRemoteWorkspace(RemoteWorkspace):
    """Native workspace with caller-owned HTTP admission retained across resets."""

    tool_modules: tuple[tuple[str, str], ...] = Field(default=(), exclude=True, frozen=True)
    api_key: str | None = Field(default=None, exclude=True, repr=False, frozen=True)
    _admitted_client: httpx.Client | None = PrivateAttr(default=None)
    _response_budget: WorkerResponseBudget = PrivateAttr(default_factory=WorkerResponseBudget)

    @property
    def client(self) -> httpx.Client:
        if self._admitted_client is None:
            modules = dict(self.tool_modules)
            if len(modules) != len(self.tool_modules):
                raise ValueError("worker tool names must be unique")
            transport = WorkerResponseTransport(
                host=self.host,
                tool_modules=modules,
                budget=self._response_budget,
                transport=httpx.HTTPTransport(
                    trust_env=False, limits=httpx.Limits(max_connections=4)
                ),
            )
            self._admitted_client = httpx.Client(
                base_url=self.host,
                headers={"X-Session-API-Key": self.api_key} if self.api_key else {},
                timeout=httpx.Timeout(connect=10, read=self.read_timeout, write=10, pool=10),
                transport=transport,
                follow_redirects=False,
                trust_env=False,
            )
        return self._admitted_client

    def reset_client(self) -> None:
        client, self._admitted_client = self._admitted_client, None
        if client is not None:
            client.close()
