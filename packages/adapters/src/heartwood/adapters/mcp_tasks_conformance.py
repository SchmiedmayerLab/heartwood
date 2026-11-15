# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Offline exchange checks using the unchanged published MCP Tasks extension schema.

Task message shapes belong to the pinned upstream resource, not the installed SDK's
older task types. Extra Heartwood profile checks cover correlation and sensible task
observations. No service calls, polling, cancellation, or authorization occur here.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from heartwood.adapters.service_conformance import (
    ServiceConformanceError,
    ServiceConformanceReport,
    validate_platform_message,
)
from heartwood.schemas._upstream import MCP_TASKS_EXTENSION, mcp_tasks_schema

_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
_TASK_METHODS = {
    "tasks/get": ("GetTaskRequest", "GetTaskResult"),
    "tasks/update": ("UpdateTaskRequest", "UpdateTaskResult"),
    "tasks/cancel": ("CancelTaskRequest", "CancelTaskResult"),
}
_LIMITATIONS = (
    "Captured capability declarations do not authenticate either party.",
    "No authority, approval, execution success, or spending guarantee is established.",
    "One exchange is checked; persistence, state transitions, and input lineage are unverified.",
    "Cancellation acknowledgement does not prove that the task, backend job, or spending stopped.",
    "Base envelope checks are limited; full core-protocol and live SDK conformance are untested.",
    "Nonnegative durations and timezone-aware ordered timestamps are additional profile checks.",
    "Nested tool results may omit resultType as in the extension examples; outer results may not.",
)


def validate_mcp_task_exchange(
    request: object,
    response: object,
    *,
    server_capabilities: object,
    result_schema: str | None = None,
) -> ServiceConformanceReport:
    """Check captured JSON-RPC messages and separately captured discovery capabilities.

    Creation requires per-request client opt-in and server advertisement. Follow-up
    methods require advertised server support; this checker does not infer how the
    requester obtained its task ID. ``result_schema`` is an optional caller-selected
    platform artifact filename for validating successful tool ``structuredContent``.
    A tool result with ``isError: true`` remains a valid failed tool outcome, including
    within a completed task; its success-domain payload is not validated.
    """
    try:
        # Refuse non-JSON/cyclic/non-finite captures before any protocol inspection.
        json.dumps((request, response, server_capabilities), allow_nan=False)
        checks, domain_limit = _check_exchange(
            _object(request),
            _object(response),
            _object(server_capabilities),
            result_schema=result_schema,
            schema=mcp_tasks_schema(),
        )
    except (TypeError, ValueError, ValidationError, RecursionError):
        raise ServiceConformanceError("MCP Tasks exchange failed contract validation") from None
    return ServiceConformanceReport(
        service="mcp-tasks",
        item_count=1,
        checks=(
            "Pinned published MCP Tasks message definitions and request/response correlation",
            *checks,
        ),
        limitations=(*_LIMITATIONS, domain_limit),
    )


def _check_exchange(
    request: dict[str, Any],
    response: dict[str, Any],
    capabilities: dict[str, Any],
    *,
    result_schema: str | None,
    schema: dict[str, Any],
) -> tuple[tuple[str, ...], str]:
    _validate(schema, "JSONRPCRequest", request)
    method = request["method"]
    if method not in {"tools/call", *_TASK_METHODS}:
        raise ValueError("unsupported task exchange method")
    params = _object(request.get("params"))
    if method in _TASK_METHODS:
        _validate(schema, _TASK_METHODS[method][0], request)
        _nonempty(params["taskId"])
    else:
        _nonempty(params.get("name"))
        if "arguments" in params:
            _object(params["arguments"])
    _validate(schema, "RequestId", response.get("id"))
    if response.get("jsonrpc") != "2.0" or response["id"] != request["id"]:
        raise ValueError("response does not match request")
    if ("result" in response) == ("error" in response):
        raise ValueError("response must contain exactly one result or error")
    if "error" in response:
        _validate(schema, "Error", response["error"])
        return (
            ("Correlated JSON-RPC error response; no task or successful tool result accepted",),
            "Domain payload was not validated because the request returned a JSON-RPC error.",
        )
    result = _object(response["result"])
    if method == "tools/call":
        if result.get("resultType") == "task":
            if not _advertises_tasks(schema, capabilities):
                raise ValueError("server has not advertised Tasks")
            metadata = _object(params.get("_meta"))
            client = _object(metadata.get(_CLIENT_CAPABILITIES))
            if not _advertises_tasks(schema, client):
                raise ValueError("request has not advertised Tasks")
            _validate(schema, "CreateTaskResult", result)
            _task_observation(result)
            return (
                ("Task creation has client opt-in on this request and server support",),
                "A task handle has no domain result; retrieve and validate it separately.",
            )
        return _tool_result(schema, result, result_schema=result_schema, nested=False)
    if not _advertises_tasks(schema, capabilities):
        raise ValueError("server has not advertised Tasks")
    _validate(schema, _TASK_METHODS[method][1], result)
    if method != "tasks/get":
        return (
            ("Task update/cancellation acknowledgement; no backend completion inferred",),
            "No domain result is present in this acknowledgement.",
        )
    if result["taskId"] != params["taskId"]:
        raise ValueError("response belongs to a different task")
    _task_observation(result)
    # The upstream variants require their own fields but allow extra properties.
    # Reject contradictory status-specific payloads in this integration profile.
    for field, state in (
        ("result", "completed"),
        ("error", "failed"),
        ("inputRequests", "input_required"),
    ):
        if field in result and result["status"] != state:
            raise ValueError("status-specific payload conflicts with task status")
    if result["status"] == "completed":
        return _tool_result(
            schema, _object(result["result"]), result_schema=result_schema, nested=True
        )
    if result["status"] == "failed":
        return (
            ("Task failed with a JSON-RPC error; no successful domain result accepted",),
            "Domain payload was not validated because the task failed.",
        )
    return (
        ("Task observation matches the requested task identity",),
        "No completed domain result is present in this task observation.",
    )


def _tool_result(
    schema: dict[str, Any],
    result: dict[str, Any],
    *,
    result_schema: str | None,
    nested: bool,
) -> tuple[tuple[str, ...], str]:
    # The extension's own completed-result examples omit this discriminator inside
    # the nested original tool result. The outer response always supplies it.
    if not nested or "resultType" in result:
        _validate(schema, "Result", result)
        if result["resultType"] != "complete":
            raise ValueError("not a completed tool result")
    elif "_meta" in result:
        _validate(schema, "ResultMetaObject", result["_meta"])
    content = result.get("content")
    if not isinstance(content, list):
        raise ValueError("tool result content must be an array")
    for block in content:
        _validate(schema, "ContentBlock", block)
    if "structuredContent" in result:
        _object(result["structuredContent"])
    if "isError" in result and not isinstance(result["isError"], bool):
        raise ValueError("tool error flag must be boolean")
    if result.get("isError") is True:
        return (
            ("Tool result reports isError=true; a completed task does not mean tool success",),
            "Domain payload was not validated because the tool reported an error.",
        )
    if result_schema is not None:
        validate_platform_message(result_schema, result.get("structuredContent"))
        return (
            ("Successful tool structuredContent passed the caller-selected platform parser",),
            "Domain structure was checked; authority and current validity remain unverified.",
        )
    return (
        ("Immediate or completed tool result envelope checked",),
        "Domain payload was not validated; select a platform result schema or check it separately.",
    )


def _advertises_tasks(schema: dict[str, Any], capabilities: dict[str, Any]) -> bool:
    extensions = _object(capabilities.get("extensions", {}))
    if MCP_TASKS_EXTENSION not in extensions:
        return False
    _validate(schema, "TasksExtensionCapability", extensions[MCP_TASKS_EXTENSION])
    return True


def _task_observation(task: dict[str, Any]) -> None:
    _nonempty(task["taskId"])
    created = datetime.fromisoformat(task["createdAt"])
    updated = datetime.fromisoformat(task["lastUpdatedAt"])
    if created.utcoffset() is None or updated.utcoffset() is None or updated < created:
        raise ValueError("task timestamps must have timezones and preserve chronological order")
    for field in ("ttlMs", "pollIntervalMs"):
        value = task.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError("task durations must be nonnegative integer milliseconds")


def _validate(schema: dict[str, Any], name: str, value: object) -> None:
    Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": schema["$defs"]}).validate(value)


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("expected a JSON object")
    return value


def _nonempty(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected a nonempty identity")
