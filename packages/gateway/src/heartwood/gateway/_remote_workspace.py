# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""HTTP admission for the native remote workspace's untrusted responses."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from threading import Lock, Thread, local
from typing import Any
from uuid import UUID

import httpx
from openhands.sdk.conversation.conversation_stats import ConversationStats
from openhands.sdk.conversation.request import StartConversationRequest
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.event import Event
from openhands.sdk.event.conversation_state import FULL_STATE_KEY, ConversationStateUpdateEvent
from openhands.sdk.event.types import ROOT_PARENT_ID
from openhands.sdk.tool.client_tool import ClientToolSpec
from openhands.sdk.workspace import RemoteWorkspace
from pydantic import ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from heartwood.core_adapter.worker_attempts import WorkerAttemptError, WorkerAttemptLease

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# A snapshot carries the whole active branch in one response, so it alone may be larger.
_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
_MAX_RESPONSE_SECONDS = 120.0
_MAX_JSON_DEPTH = 64
# Parsed values, not bytes, decide the memory a response costs: an empty object takes
# three bytes of JSON and about a hundred bytes once parsed and validated. This bound
# holds one parse and its native validation below about 70 MiB, and a real session's
# 136 values per tool step allow about 3,800 steps in one snapshot.
_MAX_JSON_VALUES = 512 * 1024
# Each value is charged at least this many bytes, so dense responses spend the
# response budget as fast as the memory they cost.
_VALUE_CHARGE_BYTES = 16
_JSON_STRING = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"')
# One walk through paged history may use the burst allowance but none of its refill,
# so a worker cannot extend a walk by answering slowly.
_MAX_HISTORY_PAGES = 1024
_MAX_HISTORY_BYTES = 32 * 1024 * 1024
# The most events the agent server returns in one history page.
_HISTORY_PAGE_EVENTS = 100
# A request carrying this extension receives the values admission parsed and validated.
_ADMITTED = "heartwood.admitted"

# The gateway's conversation request as sent to the worker and as serialized without secrets.
RequestContract = tuple[dict[str, Any], dict[str, Any]]

# Conversation fields the worker reports that are plain data to the SDK and the gateway.
# Every other field must be one the gateway's request set, with the value it sent.
_REPORTED_FIELDS = frozenset(
    {
        "activated_knowledge_skills",
        "activated_path_rules",
        "agent_state",
        "ambient_plugin_mcp_servers",
        "ambient_plugin_skills",
        "available_efforts",
        "available_models",
        "blocked_actions",
        "blocked_messages",
        "created_at",
        "current_effort",
        "current_model_id",
        "execution_status",
        "forked_from_conversation_id",
        "forked_from_event_id",
        "head_is_empty",
        "id",
        "invoked_skills",
        "last_user_message_id",
        "launched_agent_profile",
        "leaf_event_id",
        "metrics",
        "persistence_dir",
        "runtime_info",
        "stats",
        "sub_conversation_ids",
        "supports_runtime_model_switch",
        "title",
        "updated_at",
    }
)
# Fields admitted against the launch's registered tools instead of the request.
_REGISTRATION_FIELDS = ("tool_module_qualnames", "client_tools")
# The fields checked in the gateway's own creation request before it is sent.
_SENT_FIELDS = ("agent", "confirmation_policy", "security_analyzer", *_REGISTRATION_FIELDS)


class WorkerResponseError(ValueError):
    """A remote response failed admission before SDK deserialization."""


class WorkerResponseBudget:
    """Rate-bounded admission shared by a workspace's response transports.

    The allowance absorbs bursts such as reading retained history and refills at the
    rate of ordinary status polling and progress reads, so a run of any length stays
    admitted while a worker that answers faster than that is fenced.
    """

    def __init__(
        self,
        *,
        max_bytes: int = 32 * 1024 * 1024,
        max_responses: int = 1024,
        bytes_per_second: int = 1024 * 1024,
        responses_per_second: int = 4,
        attempt: WorkerAttemptLease | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        limits = (max_bytes, max_responses, bytes_per_second, responses_per_second)
        if any(type(limit) is not int or limit <= 0 for limit in limits):
            raise ValueError("worker response limits must be positive integers")
        self._max_bytes = max_bytes
        self._max_responses = max_responses
        self._bytes_per_second = bytes_per_second
        self._responses_per_second = responses_per_second
        self._remaining_bytes = float(max_bytes)
        self._remaining_responses = float(max_responses)
        self._clock = clock
        self._replenished = clock()
        self._closed = False
        self._lock = Lock()
        self._attempt = attempt

    @property
    def attempt(self) -> WorkerAttemptLease | None:
        """Return the trusted attempt shared by all transports using this budget."""
        return self._attempt

    def charge(self, size: int, *, new_response: bool = False) -> None:
        """Charge received bytes or reserve a request before contacting the worker."""
        if size < 0:
            raise ValueError("worker response size must not be negative")
        if self._attempt is not None:
            # Observing the runtime is a control-plane request, so it precedes the lock.
            try:
                self._attempt.ensure_active()
            except WorkerAttemptError:
                with self._lock:
                    self._closed = True
        with self._lock:
            if self._closed:
                raise WorkerResponseError("worker response admission is closed")
            now = self._clock()
            elapsed = max(0.0, now - self._replenished)
            self._replenished = now
            self._remaining_bytes = min(
                self._max_bytes, self._remaining_bytes + elapsed * self._bytes_per_second
            )
            self._remaining_responses = min(
                self._max_responses,
                self._remaining_responses + elapsed * self._responses_per_second,
            )
            if size > self._remaining_bytes or (new_response and self._remaining_responses < 1):
                self._closed = True
                if self._attempt is not None:
                    self._attempt.fence()
                raise WorkerResponseError("worker response budget exhausted")
            self._remaining_bytes -= size
            if new_response:
                self._remaining_responses -= 1

    def close(self) -> None:
        """Refuse further admission permanently."""
        with self._lock:
            self._closed = True
            if self._attempt is not None:
                self._attempt.fence()


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
    return origin.copy_with(path="/")


class WorkerResponseTransport(httpx.BaseTransport):
    """Bound responses and admit module names before handing bytes to the SDK."""

    def __init__(
        self,
        *,
        host: str,
        tool_modules: Mapping[str, str],
        transport: httpx.BaseTransport,
        budget: WorkerResponseBudget | None = None,
        client_tools: Sequence[ClientToolSpec] = (),
        request: Callable[[], RequestContract | None] = lambda: None,
    ) -> None:
        self._origin = _worker_origin(host)
        self._tool_modules = dict(tool_modules)
        self._client_tools = _client_tool_contract(client_tools)
        self._request = request
        self._transport = transport
        self._history = local()
        self._budget = budget if budget is not None else WorkerResponseBudget()
        if self._budget.attempt is not None and self._origin != _worker_origin(
            str(self._budget.attempt.runtime.endpoint)
        ):
            raise WorkerResponseError("worker origin does not match its retained binding")

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
        try:
            self._admit_request(request)
        except WorkerResponseError:
            self._budget.close()
            raise
        request.headers["Accept-Encoding"] = "identity"
        timeout = dict(request.extensions.get("timeout") or {})
        read = timeout.get("read")
        timeout["read"] = (
            _MAX_RESPONSE_SECONDS if read is None else min(read, _MAX_RESPONSE_SECONDS)
        )
        request.extensions["timeout"] = timeout
        path = request.url.path
        admitted: list[Event] = []
        value: object = None
        try:
            status, headers, content = self._exchange(request)
            if content:
                media_type = headers.get("content-type", "").split(";", 1)[0]
                if media_type.strip().lower() != "application/json":
                    raise WorkerResponseError("worker response must be JSON")
                value = _admit_json(content, self._budget)
                attempt = self._budget.attempt
                if isinstance(value, dict):
                    # The SDK reads these two responses as the conversation itself.
                    if (request.method, "/api/conversations") in {
                        ("POST", path),
                        ("GET", path.rpartition("/")[0]),
                    }:
                        _admit_metadata(
                            value, self._tool_modules, self._client_tools, self._request()
                        )
                    else:
                        _admit_registrations(value, self._tool_modules, self._client_tools)
                    # Event pages and snapshots are replayed to the same callbacks as
                    # socket messages, so their events pass the same admission.
                    for events in (value.get("items"), value.get("events")):
                        for event in events if isinstance(events, list) else ():
                            if isinstance(event, dict):
                                _admit_event(
                                    event,
                                    self._tool_modules,
                                    self._client_tools,
                                    self._request(),
                                    attempt,
                                )
                                # The SDK validates a snapshot as one list and records an
                                # error for every invalid event, so the first one ends here.
                                admitted.append(_validate_event(event))
                    if path.endswith("/events/search"):
                        self._admit_history_page(request, value, len(content))
                if (
                    attempt is not None
                    and (request.method, path)
                    in {
                        ("POST", "/api/conversations"),
                        ("GET", f"/api/conversations/{attempt.intent.conversation_id}"),
                        (
                            "GET",
                            f"/api/conversations/{attempt.intent.conversation_id}/events/snapshot",
                        ),
                    }
                    and (
                        not isinstance(value, dict)
                        or value.get("id") != str(attempt.intent.conversation_id)
                    )
                ):
                    raise WorkerResponseError("worker returned another conversation identity")
            elif status != 204:
                raise WorkerResponseError("worker response is empty")
            self._budget.charge(0)
            # The gateway's own reads take the admitted values, so nothing parses them twice.
            extensions = (
                {_ADMITTED: (value, tuple(admitted))} if request.extensions.get(_ADMITTED) else {}
            )
            return httpx.Response(status, headers=headers, content=content, extensions=extensions)
        except WorkerResponseError:
            self._budget.close()
            raise

    def _exchange(self, request: httpx.Request) -> tuple[int, httpx.Headers, bytes]:
        """Bound one exchange, from connection to the last body byte, by the wall clock.

        Transport timeouts restart with every byte received, so a worker sending status
        lines, headers or chunk framing slowly would otherwise hold the caller, and the
        locks it holds, without limit.
        """
        outcome: list[tuple[int, httpx.Headers, bytes] | BaseException] = []

        def read() -> None:
            try:
                outcome.append(self._read(request))
            except BaseException as error:
                outcome.append(error)

        reader = Thread(target=read, name="heartwood-worker-response", daemon=True)
        reader.start()
        reader.join(_MAX_RESPONSE_SECONDS)
        if not outcome:
            # Closing the connection ends the abandoned read at its next byte or timeout.
            self._transport.close()
            raise WorkerResponseError("worker response exceeded its deadline")
        if isinstance(outcome[0], BaseException):
            raise outcome[0]
        return outcome[0]

    def _read(self, request: httpx.Request) -> tuple[int, httpx.Headers, bytes]:
        limit = (
            _MAX_SNAPSHOT_BYTES
            if request.url.path.endswith("/events/snapshot")
            else _MAX_RESPONSE_BYTES
        )
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
                if len(body) + len(chunk) > limit:
                    raise WorkerResponseError("worker response exceeds the byte limit")
                body.extend(chunk)
            return response.status_code, response.headers, bytes(body)
        finally:
            response.close()

    def _admit_history_page(self, request: httpx.Request, page: dict[str, Any], size: int) -> None:
        """End a history walk that repeats a page or outgrows what one read may retain."""
        requested = request.url.params.get("page_id")
        walk: dict[str, Any] | None = getattr(self._history, "walk", None)
        if walk is None or requested is None or requested != walk["next"]:
            # Each history is read synchronously on the calling thread, from its first page
            # or from a known event, and continues only with the page the worker named.
            walk = self._history.walk = {"pages": 0, "bytes": 0, "seen": {requested}}
        walk["pages"] += 1
        walk["bytes"] += size
        following = page.get("next_page_id")
        if (
            walk["pages"] > _MAX_HISTORY_PAGES
            or walk["bytes"] > _MAX_HISTORY_BYTES
            or (
                following is not None
                and (not isinstance(following, str) or following in walk["seen"])
            )
        ):
            raise WorkerResponseError("worker history does not end within its bounds")
        walk["seen"].add(following)
        walk["next"] = following

    def _admit_request(self, request: httpx.Request) -> None:
        attempt = self._budget.attempt
        if attempt is None:
            return
        identity = str(attempt.intent.conversation_id)
        path = request.url.path
        canonical = request.url.raw_path.split(b"?", 1)[0] == path.encode("utf-8")
        if path == "/api/conversations" and request.method == "POST" and canonical:
            value = _admit_json(request.content, self._budget)
            if isinstance(value, dict) and value.get("conversation_id") == identity:
                # The gateway's own request may carry only what this workspace was bound to.
                sent = {key: value[key] for key in _SENT_FIELDS if key in value}
                _admit_metadata(sent, self._tool_modules, self._client_tools, self._request())
                return
        prefix = f"/api/conversations/{identity}"
        if (
            canonical
            and (path == prefix or path.startswith(prefix + "/"))
            and path != prefix + "/fork"
        ):
            return
        raise WorkerResponseError("worker request is outside its retained conversation")

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


def _json_values(content: bytes) -> int:
    """Return an upper bound on the values parsing would build, without building them."""
    structure = _JSON_STRING.sub(b"", content)
    return (
        2 * (structure.count(b"{") + structure.count(b"["))
        + structure.count(b",")
        + structure.count(b":")
        + 1
    )


def _validate_event(event: dict[str, Any]) -> Event:
    try:
        return Event.model_validate(event)
    except ValidationError:
        raise WorkerResponseError("worker event is not a native event") from None


def _admit_json(content: bytes, budget: WorkerResponseBudget) -> object:
    values = _json_values(content)
    if values > _MAX_JSON_VALUES:
        raise WorkerResponseError("worker JSON exceeds the value limit")
    budget.charge(max(0, values * _VALUE_CHARGE_BYTES - len(content)))
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
    return value


def _client_tool_contract(specs: Sequence[ClientToolSpec]) -> dict[str, str]:
    result: dict[str, str] = {}
    for spec in specs:
        if spec.name in result:
            raise ValueError("worker client tool names must be unique")
        result[spec.name] = json.dumps(
            spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
    return result


def _admit_client_tools(value: object, expected: Mapping[str, str]) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise WorkerResponseError("worker client tool specifications are not authorized")
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) - ClientToolSpec.model_fields.keys():
            raise WorkerResponseError("worker client tool specifications are not authorized")
        name = raw.get("name")
        if not isinstance(name, str) or name not in expected:
            raise WorkerResponseError("worker client tool specifications are not authorized")
        try:
            spec = ClientToolSpec.model_validate(raw)
            canonical = _client_tool_contract([spec])[spec.name]
        except (ValueError, TypeError):
            raise WorkerResponseError(
                "worker client tool specifications are not authorized"
            ) from None
        if spec.name in seen or expected.get(spec.name) != canonical:
            raise WorkerResponseError("worker client tool specifications are not authorized")
        seen.add(spec.name)


def _same(returned: object, sent: object, redacted: object) -> bool:
    # Requests omit unset fields that responses carry as null, so the two are equivalent.
    if isinstance(sent, dict):
        redacted = redacted if isinstance(redacted, dict) else sent
        return isinstance(returned, dict) and all(
            _same(returned.get(key), sent.get(key), redacted.get(key))
            for key in returned.keys() | sent.keys()
        )
    if isinstance(sent, list):
        redacted = redacted if isinstance(redacted, list) else sent
        return (
            isinstance(returned, list)
            and len(returned) == len(sent) == len(redacted)
            and all(map(_same, returned, sent, redacted))
        )
    if type(returned) is type(sent) and returned == sent:
        return True
    # Only a secret serializes differently; the worker may redact or omit its value.
    return sent != redacted and (
        returned is None or (type(returned) is type(redacted) and returned == redacted)
    )


def _with_server_context(
    returned: object, agent: tuple[Any, Any], client_tools: Mapping[str, str]
) -> tuple[Any, Any]:
    """Accept what the agent server adds to the gateway's agent in its responses.

    It appends one tool entry per authorized client tool, carrying its exact
    specification, and replaces two prompt-context values.
    """
    if not isinstance(returned, dict) or not all(isinstance(side, dict) for side in agent):
        return agent
    registered = [
        {"name": name, "params": {"spec": json.loads(spec)}} for name, spec in client_tools.items()
    ]
    tools, sent_tools = returned.get("tools"), agent[0].get("tools")
    if (
        registered
        and isinstance(tools, list)
        and isinstance(sent_tools, list)
        and tools[len(sent_tools) :] == registered
    ):
        sent, redacted = ({**side, "tools": [*side["tools"], *registered]} for side in agent)
        agent = sent, redacted
    context = returned.get("agent_context")
    if not isinstance(context, dict) or not isinstance(agent[0].get("agent_context"), dict):
        return agent
    owned: dict[str, Any] = {}
    started = context.get("current_datetime")
    if isinstance(started, str) and len(started) <= 64:
        try:
            datetime.fromisoformat(started)
        except ValueError:
            pass
        else:
            owned["current_datetime"] = started
    if context.get("skills") == []:
        owned["skills"] = []
    sent, redacted = (
        {**side, "agent_context": {**side["agent_context"], **owned}} for side in agent
    )
    return sent, redacted


def _admit_registrations(
    value: dict[str, Any], tool_modules: Mapping[str, str], client_tools: Mapping[str, str]
) -> None:
    registrations = value.get("tool_module_qualnames", {})
    if not isinstance(registrations, dict) or any(
        not isinstance(name, str) or not isinstance(module, str) or tool_modules.get(name) != module
        for name, module in registrations.items()
    ):
        raise WorkerResponseError("worker tool registrations are not authorized")
    _admit_client_tools(value.get("client_tools"), client_tools)


def _admit_metadata(
    value: dict[str, Any],
    tool_modules: Mapping[str, str],
    client_tools: Mapping[str, str] | None = None,
    request: RequestContract | None = None,
) -> None:
    """Admit conversation fields: reported data, or exactly what the gateway's request set.

    The SDK deserializes returned fields, and constructing an agent, an analyzer or a
    secret source can contact a route the field names. A field outside both groups is
    rejected, so a field added by a later SDK is not trusted before it is reviewed.
    """
    _admit_registrations(value, tool_modules, client_tools or {})
    sent, redacted = request or ({}, {})
    for key, returned in value.items():
        if key in _REGISTRATION_FIELDS or key in _REPORTED_FIELDS:
            continue
        if key not in sent and returned is not None:
            raise WorkerResponseError("worker conversation field is not authorized")
        sides = sent.get(key), redacted.get(key)
        if not _same(
            returned,
            *(
                _with_server_context(returned, sides, client_tools or {})
                if key == "agent"
                else sides
            ),
        ):
            raise WorkerResponseError(f"worker {key.replace('_', ' ')} is not authorized")


def _admit_event(
    value: dict[str, Any],
    tool_modules: Mapping[str, str],
    client_tools: Mapping[str, str],
    request: RequestContract | None,
    attempt: WorkerAttemptLease | None,
) -> None:
    """Reject events that would write completion logs or replace admitted state."""
    kind = value.get("kind")
    if kind == "LLMCompletionLogEvent":
        raise WorkerResponseError("worker completion-log events are not authorized")
    if kind != "ConversationStateUpdateEvent":
        return
    key, state = value.get("key"), value.get("value")
    metadata = state if key == FULL_STATE_KEY else {key: state}
    if not isinstance(metadata, dict):
        raise WorkerResponseError("worker state update is not authorized")
    _admit_metadata(metadata, tool_modules, client_tools, request)
    if (
        attempt is not None
        and (key == FULL_STATE_KEY or "id" in metadata)
        and metadata.get("id") != str(attempt.intent.conversation_id)
    ):
        raise WorkerResponseError("worker returned another conversation identity")


class AdmittedRemoteWorkspace(RemoteWorkspace):
    """Native workspace with caller-owned HTTP admission retained across resets."""

    model_config = ConfigDict(arbitrary_types_allowed=True, hide_input_in_errors=True)
    attempt: WorkerAttemptLease | None = Field(default=None, exclude=True, repr=False, frozen=True)
    client_tools: tuple[ClientToolSpec, ...] = Field(default=(), exclude=True, frozen=True)
    tool_modules: tuple[tuple[str, str], ...] = Field(default=(), exclude=True, frozen=True)
    api_key: str | None = Field(default=None, exclude=True, repr=False, frozen=True)
    _admitted_client: httpx.Client | None = PrivateAttr(default=None)
    _response_budget: WorkerResponseBudget = PrivateAttr(default_factory=WorkerResponseBudget)
    _client_tool_specs: tuple[str, ...] | None = PrivateAttr(default=None)
    _request_contract: RequestContract | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _bind_attempt_budget(self) -> AdmittedRemoteWorkspace:
        if self._client_tool_specs is None:
            self._client_tool_specs = tuple(_client_tool_contract(self.client_tools).values())
        if self.attempt is not None:
            self.attempt.ensure_active()
            if _worker_origin(self.host) != _worker_origin(str(self.attempt.runtime.endpoint)):
                raise ValueError("worker host does not match its retained binding")
            if self._response_budget.attempt is not self.attempt:
                self._response_budget = WorkerResponseBudget(attempt=self.attempt)
        return self

    @property
    def authorized_client_tools(self) -> list[ClientToolSpec]:
        """Copy the captured native specifications without sharing mutable schemas."""
        return [ClientToolSpec.model_validate_json(spec) for spec in self._client_tool_specs or ()]

    def authorize_request(self, request: StartConversationRequest) -> None:
        """Bind the request this workspace sends; worker metadata may return no other values."""
        sent = request.model_dump(mode="json", context={"expose_secrets": True})
        redacted = request.model_dump(mode="json")
        for side in (sent, redacted):
            # The server reports the request's secrets as the conversation's registry.
            side["secret_registry"] = {"secret_sources": side["secrets"]}
        self._request_contract = sent, redacted

    @property
    def client(self) -> httpx.Client:
        self._response_budget.charge(0)
        if self.attempt is not None and _worker_origin(self.host) != _worker_origin(
            str(self.attempt.runtime.endpoint)
        ):
            self._response_budget.close()
            raise WorkerResponseError("worker host does not match its retained binding")
        if self._admitted_client is None:
            modules = dict(self.tool_modules)
            if len(modules) != len(self.tool_modules):
                raise ValueError("worker tool names must be unique")
            transport = WorkerResponseTransport(
                host=self.host,
                tool_modules=modules,
                client_tools=self.authorized_client_tools,
                request=lambda: self._request_contract,
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


@dataclass(frozen=True, slots=True)
class WorkerBranchRead:
    """One admitted view of the worker's active branch, its status and its usage."""

    events: tuple[Event, ...]
    execution_status: ConversationExecutionStatus
    stats: ConversationStats


class WorkerBranch:
    """The worker's active branch, read whole once and afterwards only after its leaf.

    A later read must begin with the admitted leaf, unchanged, and every event after it
    must name its predecessor as its parent, so a repeated, missing, reordered or
    replaced event fences the attempt. The retained branch is bounded like the snapshot
    that would carry it, so the conversation can always be attached from one snapshot.
    """

    def __init__(self, workspace: AdmittedRemoteWorkspace, conversation_id: UUID) -> None:
        self._workspace = workspace
        self._identity = str(conversation_id)
        self._conversation = f"/api/conversations/{conversation_id}"
        self._lock = Lock()
        self._events: tuple[Event, ...] = ()
        self._positions: dict[str, int] = {}
        self._bytes = 0
        self._values = 0

    def read(self) -> WorkerBranchRead:
        """Read the worker's status and usage, then the events after the admitted leaf.

        The agent server persists an event before it publishes the status that follows
        it, so a status read first describes a prefix of the events read after it. An
        empty branch is read as one snapshot, which carries its own status.
        """
        try:
            with self._lock:
                base = self._events
            if not base:
                snapshot = self._snapshot()
                self._extend((), snapshot.events)
                return snapshot
            execution_status, stats = self._status(self._get(""))
            events = self._extend(base, self._after(base))
            return WorkerBranchRead(events, execution_status, stats)
        except WorkerResponseError:
            self._workspace._response_budget.close()
            raise

    def _get(self, path: str, **params: str | int) -> dict[str, Any]:
        response = self._workspace.client.get(
            self._conversation + path, params=params, extensions={_ADMITTED: True}
        )
        value, events = response.extensions.get(_ADMITTED, (None, ()))
        field = {"": None, "/events/snapshot": "events"}.get(path, "items")
        if not isinstance(value, dict) or (
            field is not None
            and (not isinstance(value.get(field), list) or len(value[field]) != len(events))
        ):
            raise WorkerResponseError("worker branch response is invalid")
        return {**value, field: events} if field is not None else value

    def _status(
        self, value: dict[str, Any]
    ) -> tuple[ConversationExecutionStatus, ConversationStats]:
        try:
            execution_status = ConversationExecutionStatus(value.get("execution_status"))
            stats = ConversationStats.model_validate(value.get("stats"))
        except ValueError:
            raise WorkerResponseError("worker branch response is invalid") from None
        if value.get("id") != self._identity:
            raise WorkerResponseError("worker branch response is invalid")
        return execution_status, stats

    def _snapshot(self) -> WorkerBranchRead:
        value = self._get("/events/snapshot")
        execution_status, stats = self._status(value)
        branch = _require_chain(None, value["events"], set())
        if value.get("leaf_event_id") != (branch[-1].id if branch else None):
            raise WorkerResponseError("worker branch response is invalid")
        return WorkerBranchRead(branch, execution_status, stats)

    def _after(self, base: tuple[Event, ...]) -> tuple[Event, ...]:
        leaf = base[-1]
        events: list[Event] = []
        seen: set[str] = set()
        page, first = leaf.id, True
        while True:
            value = self._get("/events/search", page_id=page, limit=_HISTORY_PAGE_EVENTS)
            items = value["items"]
            # A page starts at the event it was requested from; the first is the leaf itself.
            if not items or items[0].id != page or (first and items[0] != leaf):
                raise WorkerResponseError("worker history does not continue the admitted branch")
            new = _require_chain(
                events[-1] if events else leaf, items[1:] if first else items, seen
            )
            with self._lock:
                repeated = any(
                    self._positions.get(event.id, len(base)) < len(base) for event in new
                )
            if repeated:
                raise WorkerResponseError("worker history does not continue the admitted branch")
            events.extend(new)
            following = value.get("next_page_id")
            if following is None:
                return tuple(events)
            page, first = following, False

    def _extend(self, base: tuple[Event, ...], new: tuple[Event, ...]) -> tuple[Event, ...]:
        """Retain the longer of two consistent reads; concurrent readers share one branch."""
        candidate = (*base, *new)
        with self._lock:
            current = self._events
            shorter, longer = sorted((current, candidate), key=len)
            if longer[: len(shorter)] != shorter:
                raise WorkerResponseError("worker branch changed between reads")
            size, values = self._bytes, self._values
            for event in longer[len(current) :]:
                encoded = event.model_dump_json().encode()
                size += len(encoded)
                values += _json_values(encoded)
            if size > _MAX_SNAPSHOT_BYTES or values > _MAX_JSON_VALUES:
                raise WorkerResponseError("worker branch exceeds what a snapshot may carry")
            for index in range(len(current), len(longer)):
                self._positions[longer[index].id] = index
            self._events, self._bytes, self._values = longer, size, values
        return candidate


def _require_chain(
    previous: Event | None, events: Sequence[Event], seen: set[str]
) -> tuple[Event, ...]:
    """Return the branch events, each extending the branch from its predecessor once.

    The worker's log also holds state updates, which name the branch tip as parent but
    are not part of the branch.
    """
    branch: list[Event] = []
    for event in events:
        expected = (None, ROOT_PARENT_ID) if previous is None else (previous.id,)
        if event.parent_id not in expected or event.id in seen:
            raise WorkerResponseError("worker history does not continue the admitted branch")
        seen.add(event.id)
        if not isinstance(event, ConversationStateUpdateEvent):
            branch.append(event)
            previous = event
    return tuple(branch)
