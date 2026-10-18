# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Adversarial captures for the pinned published Tasks protocol and profile semantics."""

from __future__ import annotations

import json
import socket
from copy import deepcopy
from dataclasses import asdict
from typing import Any

import pytest

from heartwood.adapters.mcp_tasks_conformance import validate_mcp_task_exchange
from heartwood.adapters.service_conformance import ServiceConformanceError
from heartwood.schemas._upstream import MCP_TASKS_EXTENSION

_CAPABILITIES: dict[str, Any] = {"extensions": {MCP_TASKS_EXTENSION: {}}}
_PRIVATE = "synthetic-private-data-not-for-reporting"
_DOMAIN = {
    "models": [{"connection_id": "synthetic", "model_id": "synthetic", "protocols": ["openai"]}]
}


def _request(method: str = "tools/call", *, opt_in: bool = True) -> dict[str, Any]:
    params: dict[str, Any] = {"name": "synthetic-estimate", "arguments": {}}
    if method != "tools/call":
        params = {"taskId": "synthetic-task"}
    if method == "tasks/update":
        params["inputResponses"] = {"confirmation": {"action": "accept"}}
    if opt_in:
        params["_meta"] = {"io.modelcontextprotocol/clientCapabilities": deepcopy(_CAPABILITIES)}
    return {"jsonrpc": "2.0", "id": 7, "method": method, "params": params}


def _task(status: str = "working") -> dict[str, Any]:
    return {
        "taskId": "synthetic-task",
        "status": status,
        "createdAt": "2026-10-17T12:00:00Z",
        "lastUpdatedAt": "2026-10-17T12:01:00Z",
        "ttlMs": 3600000,
        "pollIntervalMs": 1000,
    }


def _tool_result(*, error: bool = False) -> dict[str, Any]:
    return {
        "resultType": "complete",
        "content": [{"type": "text", "text": _PRIVATE}],
        "structuredContent": deepcopy(_DOMAIN),
        "isError": error,
    }


def _response(result: object) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 7, "result": result}


def test_task_creation_uses_published_flat_shape_and_current_request_opt_in() -> None:
    report = validate_mcp_task_exchange(
        _request(), _response({**_task(), "resultType": "task"}), server_capabilities=_CAPABILITIES
    )
    assert report.service == "mcp-tasks"
    assert any("opt-in on this request" in check for check in report.checks)
    assert any("has no domain result" in limit for limit in report.limitations)


@pytest.mark.parametrize(
    "capabilities",
    [
        {},
        {"tasks": {}},
        {"extensions": {}},
        {"extensions": {MCP_TASKS_EXTENSION: True}},
        {"extensions": {MCP_TASKS_EXTENSION: None}},
        {"extensions": {MCP_TASKS_EXTENSION: {"version": "2026-07-28"}}},
    ],
)
def test_task_creation_requires_valid_server_advertisement(capabilities: object) -> None:
    with pytest.raises(ServiceConformanceError):
        validate_mcp_task_exchange(
            _request(),
            _response({**_task(), "resultType": "task"}),
            server_capabilities=capabilities,
        )


def test_task_creation_does_not_infer_client_support_from_previous_requests() -> None:
    with pytest.raises(ServiceConformanceError):
        validate_mcp_task_exchange(
            _request(opt_in=False),
            _response({**_task(), "resultType": "task"}),
            server_capabilities=_CAPABILITIES,
        )


@pytest.mark.parametrize("declaration", [True, None, [], {"requests": {"tools": {"call": {}}}}])
def test_current_request_opt_in_is_the_upstream_empty_capability_object(
    declaration: object,
) -> None:
    request = _request()
    request["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"]["extensions"][
        MCP_TASKS_EXTENSION
    ] = declaration
    with pytest.raises(ServiceConformanceError):
        validate_mcp_task_exchange(
            request, _response({**_task(), "resultType": "task"}), server_capabilities=_CAPABILITIES
        )


def test_immediate_result_needs_no_tasks_negotiation_and_can_validate_domain() -> None:
    report = validate_mcp_task_exchange(
        _request(opt_in=False),
        _response(_tool_result()),
        server_capabilities={},
        result_schema="model-metadata.schema.json",
    )
    assert any("caller-selected platform parser" in check for check in report.checks)
    assert _PRIVATE not in json.dumps(asdict(report))


@pytest.mark.parametrize("method", ["resources/read", "prompts/get", "tasks/list", "tasks/result"])
def test_only_supported_methods_can_participate_in_this_profile(method: str) -> None:
    with pytest.raises(ServiceConformanceError):
        validate_mcp_task_exchange(
            _request(method),
            _response({**_task(), "resultType": "task"}),
            server_capabilities=_CAPABILITIES,
        )


def test_followups_use_published_definitions_without_inventing_a_task_state_machine() -> None:
    for method in ("tasks/update", "tasks/cancel"):
        report = validate_mcp_task_exchange(
            _request(method, opt_in=False),
            _response({"resultType": "complete"}),
            server_capabilities=_CAPABILITIES,
        )
        assert any("acknowledgement" in check for check in report.checks)
    later = validate_mcp_task_exchange(
        _request("tasks/get"),
        _response({**_task(), "resultType": "complete"}),
        server_capabilities=_CAPABILITIES,
    )
    assert any(
        "Cancellation acknowledgement does not prove" in limit for limit in later.limitations
    )


@pytest.mark.parametrize("state", ["working", "cancelled"])
def test_noncompleted_get_observations_have_no_domain_success(state: str) -> None:
    report = validate_mcp_task_exchange(
        _request("tasks/get"),
        _response({**_task(state), "resultType": "complete"}),
        server_capabilities=_CAPABILITIES,
        result_schema="model-metadata.schema.json",
    )
    assert any("No completed domain result" in limit for limit in report.limitations)


def test_input_required_and_update_use_upstream_input_shapes() -> None:
    result = {
        **_task("input_required"),
        "resultType": "complete",
        "inputRequests": {
            "confirmation": {
                "method": "elicitation/create",
                "params": {
                    "message": "Synthetic confirmation",
                    "mode": "form",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"confirmed": {"type": "boolean"}},
                    },
                },
            },
        },
    }
    validate_mcp_task_exchange(
        _request("tasks/get"), _response(result), server_capabilities=_CAPABILITIES
    )
    bad = _request("tasks/update")
    bad["params"]["inputResponses"] = {"confirmation": {"action": "approve-tools"}}
    with pytest.raises(ServiceConformanceError):
        validate_mcp_task_exchange(
            bad, _response({"resultType": "complete"}), server_capabilities=_CAPABILITIES
        )


def test_completed_task_payload_uses_the_same_domain_parser_as_immediate_results() -> None:
    result = {**_task("completed"), "resultType": "complete", "result": _tool_result()}
    result["result"].pop("resultType")  # Published Tasks examples permit this nested form.
    report = validate_mcp_task_exchange(
        _request("tasks/get"),
        _response(result),
        server_capabilities=_CAPABILITIES,
        result_schema="model-metadata.schema.json",
    )
    assert any("caller-selected platform parser" in check for check in report.checks)
    result["result"]["structuredContent"] = {"models": [], "approved": True}
    with pytest.raises(ServiceConformanceError):
        validate_mcp_task_exchange(
            _request("tasks/get"),
            _response(result),
            server_capabilities=_CAPABILITIES,
            result_schema="model-metadata.schema.json",
        )


@pytest.mark.parametrize("nested", [False, True])
def test_tool_error_is_valid_protocol_but_never_domain_success(nested: bool) -> None:
    tool = _tool_result(error=True)
    tool["structuredContent"] = {"error": _PRIVATE}
    result = {**_task("completed"), "resultType": "complete", "result": tool} if nested else tool
    report = validate_mcp_task_exchange(
        _request("tasks/get" if nested else "tools/call"),
        _response(result),
        server_capabilities=_CAPABILITIES,
        result_schema="model-metadata.schema.json",
    )
    assert any("isError=true" in check for check in report.checks)
    assert any(
        "not validated because the tool reported an error" in limit for limit in report.limitations
    )
    assert _PRIVATE not in json.dumps(asdict(report))


def test_protocol_errors_remain_distinct_from_completed_tool_errors() -> None:
    request = _request(opt_in=False)
    response = {"jsonrpc": "2.0", "id": 7, "error": {"code": -32021, "message": _PRIVATE}}
    report = validate_mcp_task_exchange(request, response, server_capabilities={})
    assert any("JSON-RPC error" in check for check in report.checks)
    failed = {
        **_task("failed"),
        "resultType": "complete",
        "error": {"code": -32603, "message": _PRIVATE},
    }
    report = validate_mcp_task_exchange(
        _request("tasks/get"), _response(failed), server_capabilities=_CAPABILITIES
    )
    assert any("Task failed" in check for check in report.checks)


@pytest.mark.parametrize(
    "changes",
    [
        {"taskId": "different-task"},
        {"resultType": "task"},
        {"ttlMs": -1},
        {"pollIntervalMs": -1},
        {"ttlMs": True},
        {"pollIntervalMs": 1.5},
        {"createdAt": "invalid"},
        {"lastUpdatedAt": "2026-10-17T11:00:00Z"},
        {"createdAt": "2026-10-17T12:00:00"},
        {"taskId": ""},
        {"status": "done"},
        {"result": {"content": []}},
        {"error": {"code": -1, "message": _PRIVATE}},
        {"inputRequests": {}},
    ],
)
def test_task_identity_shape_and_additional_profile_semantics(changes: dict[str, object]) -> None:
    with pytest.raises(ServiceConformanceError) as caught:
        validate_mcp_task_exchange(
            _request("tasks/get"),
            _response({**_task(), "resultType": "complete", **changes}),
            server_capabilities=_CAPABILITIES,
        )
    assert str(caught.value) == "MCP Tasks exchange failed contract validation"
    assert _PRIVATE not in str(caught.value)
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "result",
    [
        {"task": {"taskId": "legacy", "status": "working", "ttl": 1000}},
        {"taskId": "legacy", "status": "working", "ttl": 1000},
        {"content": []},
        {"resultType": "complete", "content": "wrong"},
        {"resultType": "complete", "content": [], "isError": "false"},
        {"resultType": "complete", "content": [{"type": "text"}]},
    ],
)
def test_legacy_or_malformed_tool_results_are_not_upgraded_silently(result: object) -> None:
    with pytest.raises(ServiceConformanceError):
        validate_mcp_task_exchange(_request(), _response(result), server_capabilities=_CAPABILITIES)


@pytest.mark.parametrize(
    "change",
    [
        {"id": 8},
        {"id": True},
        {"id": None},
        {"jsonrpc": "1.0"},
        {"error": {"code": -32603, "message": _PRIVATE}},
    ],
)
def test_response_envelope_correlation_and_exclusivity(change: dict[str, object]) -> None:
    with pytest.raises(ServiceConformanceError):
        validate_mcp_task_exchange(
            _request(), {**_response(_tool_result()), **change}, server_capabilities=_CAPABILITIES
        )


def test_domain_schema_is_selected_by_the_caller_not_the_response() -> None:
    result = _tool_result()
    result["schema"] = "model-metadata.schema.json"
    report = validate_mcp_task_exchange(
        _request(), _response(result), server_capabilities=_CAPABILITIES
    )
    assert any("Domain payload was not validated" in limit for limit in report.limitations)


def test_captured_validation_is_offline_and_preserves_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_connection(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Offline task checks must not contact a service")

    monkeypatch.setattr(socket, "create_connection", forbid_connection)
    request = _request()
    response = _response({**_task(), "resultType": "task"})
    before = deepcopy((request, response, _CAPABILITIES))
    validate_mcp_task_exchange(request, response, server_capabilities=_CAPABILITIES)
    assert (request, response, _CAPABILITIES) == before


@pytest.mark.parametrize("state", ["completed", "failed", "input_required"])
def test_status_specific_payloads_are_required_by_the_pinned_schema(state: str) -> None:
    with pytest.raises(ServiceConformanceError):
        validate_mcp_task_exchange(
            _request("tasks/get"),
            _response({**_task(state), "resultType": "complete"}),
            server_capabilities=_CAPABILITIES,
        )


def test_unlimited_ttl_and_zero_intervals_are_valid_observations() -> None:
    task = {**_task(), "resultType": "complete", "ttlMs": None, "pollIntervalMs": 0}
    validate_mcp_task_exchange(
        _request("tasks/get"), _response(task), server_capabilities=_CAPABILITIES
    )
    task.pop("pollIntervalMs")
    validate_mcp_task_exchange(
        _request("tasks/get"), _response(task), server_capabilities=_CAPABILITIES
    )
