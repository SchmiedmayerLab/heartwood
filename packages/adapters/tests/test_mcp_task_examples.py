# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Offline Tasks transcripts preserve domain estimates and independent execution evidence."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from heartwood.adapters.mcp_tasks_conformance import validate_mcp_task_exchange
from heartwood.schemas import BudgetObservation, PlatformEstimate, PlatformOperationObservation
from heartwood.schemas._upstream import mcp_tasks_schema

_FIXTURES = Path(__file__).parents[3] / "fixtures" / "synthetic" / "platform-contract"
_TRANSCRIPT = json.loads((_FIXTURES / "mcp-tasks.json").read_text(encoding="utf-8"))
_EXCHANGES = {item["name"]: item for item in _TRANSCRIPT["exchanges"]}


def _validate_upstream(definition: str, value: object) -> None:
    schema = mcp_tasks_schema()
    Draft202012Validator({**schema, "$ref": f"#/$defs/{definition}"}).validate(value)


@pytest.mark.parametrize("exchange", _TRANSCRIPT["exchanges"], ids=lambda item: item["name"])
def test_published_tasks_transcript_passes_offline_exchange_conformance(
    exchange: dict[str, Any],
) -> None:
    report = validate_mcp_task_exchange(
        exchange["request"],
        exchange["response"],
        server_capabilities=_TRANSCRIPT["server_capabilities"],
    )
    assert report.limitations  # Passing structure is not runtime or authorization evidence.
    method = exchange["request"]["method"]
    result = exchange["response"]["result"]
    if method == "tasks/get":
        _validate_upstream("GetTaskRequest", exchange["request"])
        _validate_upstream("GetTaskResult", result)
    elif method == "tasks/cancel":
        _validate_upstream("CancelTaskRequest", exchange["request"])
        _validate_upstream("CancelTaskResult", result)
    elif result["resultType"] == "task":
        _validate_upstream("CreateTaskResult", result)


def test_immediate_and_deferred_estimates_have_identical_domain_content() -> None:
    immediate = _EXCHANGES["immediate-estimate"]["response"]["result"]
    deferred = _EXCHANGES["estimate-completed"]["response"]["result"]["result"]
    source = json.loads((_FIXTURES / _TRANSCRIPT["estimate_source"]).read_text(encoding="utf-8"))
    immediate_estimate = PlatformEstimate.model_validate_json(
        json.dumps(immediate["structuredContent"])
    )
    deferred_estimate = PlatformEstimate.model_validate_json(
        json.dumps(deferred["structuredContent"])
    )
    expected = PlatformEstimate.model_validate_json(json.dumps(source["estimates"][0]))
    assert immediate_estimate == deferred_estimate == expected
    assert immediate_estimate.total.expected == "4.10"
    # Task lifecycle stays in the protocol envelope, never in the price estimate.
    assert "taskId" not in immediate_estimate.model_dump()
    assert _EXCHANGES["estimate-working"]["response"]["result"]["status"] == "working"
    assert _EXCHANGES["estimate-completed"]["response"]["result"]["status"] == "completed"


def test_cancellation_acknowledgment_does_not_stop_backend_or_remove_cost() -> None:
    acknowledgment = _EXCHANGES["cancellation-acknowledged"]["response"]["result"]
    later_task = _EXCHANGES["cancellation-still-working"]["response"]["result"]
    observation = _TRANSCRIPT["after_cancellation"]
    backend = PlatformOperationObservation.model_validate_json(
        json.dumps(observation["backend_operation"])
    )
    budget = BudgetObservation.model_validate_json(json.dumps(observation["budget"]))
    source = json.loads((_FIXTURES / _TRANSCRIPT["estimate_source"]).read_text(encoding="utf-8"))
    earlier_budget = BudgetObservation.model_validate_json(json.dumps(source["budgets"][0]))
    assert acknowledgment == {"resultType": "complete"}
    assert later_task["status"] == "working"
    assert backend.status == "running"
    assert backend.backend_operation_ref != later_task["taskId"]
    assert backend.operation_id == source["operations"][0]["operation_id"]
    assert budget.spent is not None
    assert earlier_budget.spent is not None
    assert Decimal(budget.spent) > Decimal(earlier_budget.spent)
    assert budget.observed_at > earlier_budget.observed_at


def test_task_negotiation_uses_published_extension_and_per_request_capability() -> None:
    extension = "io.modelcontextprotocol/tasks"
    assert _TRANSCRIPT["extension_version"] == "2026-07-28"
    assert _TRANSCRIPT["server_capabilities"] == {"extensions": {extension: {}}}
    _validate_upstream("TasksExtensionCapability", {})
    for exchange in _TRANSCRIPT["exchanges"]:
        request = exchange["request"]
        assert request["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] == {
            "extensions": {extension: {}}
        }
        assert request["method"] not in {"tasks/result", "tasks/list"}
        assert "task" not in request["params"]
