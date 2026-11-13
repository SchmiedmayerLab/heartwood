# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from openhands.sdk.event.conversation_state import FULL_STATE_KEY, ConversationStateUpdateEvent
from openhands.sdk.tool.client_tool import ClientToolSpec

from heartwood.core_adapter.worker_attempts import WorkerAttemptLease
from heartwood.gateway._remote_workspace import (
    AdmittedRemoteWorkspace,
    WorkerResponseBudget,
    WorkerResponseError,
    WorkerResponseTransport,
)
from heartwood.gateway._worker_events import WorkerEventAdmission


def _spec() -> ClientToolSpec:
    return ClientToolSpec(
        name="request_synthetic_record",
        description="Propose a synthetic record for independent outside review.",
        parameters={
            "type": "object",
            "properties": {"record": {"type": "string", "maxLength": 128}},
            "required": ["record"],
            "additionalProperties": False,
        },
    )


def _request(tools: object, expected: tuple[ClientToolSpec, ...]) -> None:
    transport = WorkerResponseTransport(
        host="http://worker.test",
        tool_modules={},
        client_tools=expected,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"client_tools": tools})),
    )
    with httpx.Client(transport=transport, base_url="http://worker.test") as client:
        client.get("/api/conversations/synthetic")


@pytest.mark.parametrize("omit_default", [False, True])
def test_exact_native_client_spec_is_admitted(omit_default: bool) -> None:
    spec = _spec()
    _request([spec.model_dump(mode="json", exclude_none=omit_default)], (spec,))
    assert (
        "client_tools"
        not in AdmittedRemoteWorkspace(
            host="http://worker.test", working_dir="/workspace", client_tools=(spec,)
        ).model_dump()
    )


@pytest.mark.parametrize(
    "change",
    [
        "name",
        "description",
        "schema",
        "annotation",
        "unknown",
        "duplicate",
        "invalid",
        "kind",
        "missing",
    ],
)
def test_worker_cannot_register_or_redefine_client_tools(change: str) -> None:
    spec = _spec()
    value: dict[str, Any] = spec.model_dump(mode="json")
    expected: tuple[ClientToolSpec, ...] = (spec,)
    tools: object = [value]
    if change == "name":
        value["name"] = "unauthorized_client_tool"
    elif change == "description":
        value["description"] = "Perform an effect without review"
    elif change == "schema":
        value["parameters"]["properties"]["record"]["maxLength"] = 129
    elif change == "annotation":
        value["annotations"] = {"readOnlyHint": True}
    elif change == "unknown":
        value["execute"] = "synthetic-private-payload"
    elif change == "duplicate":
        tools = [value, copy.deepcopy(value)]
    elif change == "invalid":
        value["parameters"] = {"type": "array"}
    elif change == "kind":
        tools = {"name": spec.name}
    elif change == "missing":
        expected = ()
    with pytest.raises(WorkerResponseError, match="client tool specifications") as error:
        _request(tools, expected)
    assert "synthetic-private-payload" not in str(error.value)


@pytest.mark.parametrize("full_state", [False, True])
def test_state_update_cannot_replace_client_tool_contract(full_state: bool) -> None:
    spec = _spec()
    budget = WorkerResponseBudget()
    admission = WorkerEventAdmission(
        host="http://worker.test", tool_modules={}, budget=budget, client_tools=(spec,)
    )
    values = [spec.model_dump(mode="json")]
    key = FULL_STATE_KEY if full_state else "client_tools"
    admission.validate_message(
        ConversationStateUpdateEvent(
            key=key, value={"client_tools": values} if full_state else values
        ).model_dump_json()
    )
    values[0]["parameters"]["properties"]["record"]["maxLength"] = 129
    with pytest.raises(WorkerResponseError, match="failed admission"):
        admission.validate_message(
            ConversationStateUpdateEvent(
                key=key, value={"client_tools": values} if full_state else values
            ).model_dump_json()
        )
    with pytest.raises(WorkerResponseError, match="closed"):
        budget.charge(0)


def test_captured_specs_survive_mutation_and_client_reset() -> None:
    spec = _spec()
    original = spec.model_dump(mode="json")
    workspace = AdmittedRemoteWorkspace(
        host="http://worker.test", working_dir="/workspace", client_tools=(spec,)
    )
    spec.parameters["properties"]["record"]["maxLength"] = 999
    copied = workspace.authorized_client_tools
    copied[0].parameters["required"] = []
    workspace.reset_client()
    AdmittedRemoteWorkspace.model_validate(workspace)
    assert workspace.authorized_client_tools[0].model_dump(mode="json") == original
    _request([original], tuple(workspace.authorized_client_tools))
    with pytest.raises(WorkerResponseError, match="client tool specifications"):
        _request([spec.model_dump(mode="json")], tuple(workspace.authorized_client_tools))


def test_conflicting_trusted_names_are_rejected() -> None:
    spec = _spec()
    with pytest.raises(ValueError, match="names must be unique"):
        AdmittedRemoteWorkspace(
            host="http://worker.test",
            working_dir="/workspace",
            client_tools=(spec, spec),
        )


def test_creation_uses_captured_contract_and_attempt(
    worker_attempt: Callable[[str], WorkerAttemptLease],
) -> None:
    attempt = worker_attempt("http://worker.test")
    spec = _spec()
    calls: list[dict[str, Any]] = []

    def receive(request: httpx.Request) -> httpx.Response:
        value = json.loads(request.content)
        calls.append(value)
        return httpx.Response(
            200,
            json={
                "id": value["conversation_id"],
                "client_tools": value["client_tools"],
            },
        )

    transport = WorkerResponseTransport(
        host="http://worker.test",
        tool_modules={},
        client_tools=(spec,),
        budget=WorkerResponseBudget(attempt=attempt),
        transport=httpx.MockTransport(receive),
    )
    payload: dict[str, Any] = {
        "conversation_id": str(attempt.intent.conversation_id),
        "client_tools": [spec.model_dump(mode="json")],
    }
    with httpx.Client(transport=transport, base_url="http://worker.test") as client:
        client.post("/api/conversations", json=payload)
        payload["client_tools"][0]["description"] = "Changed after review"
        with pytest.raises(WorkerResponseError, match="client tool specifications"):
            client.post("/api/conversations", json=payload)
    assert len(calls) == 1
    assert not attempt.active
