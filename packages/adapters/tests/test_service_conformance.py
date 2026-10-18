# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Synthetic response and adversarial tests for offline service conformance."""

from __future__ import annotations

import subprocess
import sys
from copy import deepcopy
from typing import cast

import pytest

from heartwood.adapters.service_conformance import (
    ServiceConformanceError,
    validate_mcp_tools_list,
    validate_model_catalog,
)


def _tool() -> dict[str, object]:
    return {
        "name": "describe_resources",
        "description": "Describe authorized synthetic resource profiles.",
        "inputSchema": {
            "type": "object",
            "properties": {"profile": {"type": "string"}},
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"profiles": {"type": "array", "items": {"type": "string"}}},
            "required": ["profiles"],
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


def test_catalog_checks_identities_without_claiming_metadata_validation() -> None:
    report = validate_model_catalog({"data": [{"id": "synthetic-a"}, {"id": "synthetic-b"}]})

    assert report.service == "model-catalog"
    assert report.item_count == 2
    assert any("metadata" in limitation for limitation in report.limitations)
    assert any("authorization" in limitation for limitation in report.limitations)


@pytest.mark.parametrize("response", [None, [], {}, {"data": {}}, {"data": [None]}])
def test_catalog_rejects_invalid_envelopes(response: object) -> None:
    with pytest.raises(ServiceConformanceError):
        validate_model_catalog(response)


@pytest.mark.parametrize("identity", [None, "", "  ", 1, False, []])
def test_catalog_rejects_invalid_identifiers(identity: object) -> None:
    with pytest.raises(ServiceConformanceError, match=r"data\[0\]\.id"):
        validate_model_catalog({"data": [{"id": identity}]})


def test_catalog_rejects_duplicates_without_echoing_identifiers() -> None:
    sensitive = "synthetic-confidential-project"
    with pytest.raises(ServiceConformanceError, match="unique") as caught:
        validate_model_catalog({"data": [{"id": sensitive}, {"id": sensitive}]})
    assert sensitive not in str(caught.value)


def test_empty_pages_are_valid_and_do_not_claim_service_availability() -> None:
    catalog = validate_model_catalog({"data": []})
    tools = validate_mcp_tools_list({"tools": []})

    assert catalog.item_count == tools.item_count == 0
    assert any("availability" in item for item in tools.limitations)


def test_mcp_checks_complete_synthetic_tool_description() -> None:
    report = validate_mcp_tools_list({"tools": [_tool()]})

    assert report.service == "mcp-tools"
    assert report.item_count == 1
    assert any("not verified safety" in item for item in report.limitations)
    assert any("enforcement" in item for item in report.limitations)


@pytest.mark.parametrize("response", [None, [], {}, {"tools": {}}, {"tools": [None]}])
def test_mcp_rejects_invalid_envelopes(response: object) -> None:
    with pytest.raises(ServiceConformanceError):
        validate_mcp_tools_list(response)


@pytest.mark.parametrize(
    "field", ["name", "description", "inputSchema", "outputSchema", "annotations"]
)
def test_mcp_requires_complete_tool_contract(field: str) -> None:
    tool = _tool()
    del tool[field]

    with pytest.raises(ServiceConformanceError, match=field):
        validate_mcp_tools_list({"tools": [tool]})


@pytest.mark.parametrize("field", ["name", "description"])
@pytest.mark.parametrize("value", ["", " ", None, 1])
def test_mcp_requires_nonempty_text(field: str, value: object) -> None:
    tool = _tool()
    tool[field] = value
    with pytest.raises(ServiceConformanceError, match=field):
        validate_mcp_tools_list({"tools": [tool]})


def test_mcp_rejects_duplicate_names() -> None:
    with pytest.raises(ServiceConformanceError, match="unique"):
        validate_mcp_tools_list({"tools": [_tool(), _tool()]})


@pytest.mark.parametrize(
    "hint", ["readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"]
)
@pytest.mark.parametrize("value", [None, "true", 0, 1])
def test_mcp_requires_explicit_boolean_hints(hint: str, value: object) -> None:
    tool = _tool()
    annotations = cast(dict[str, object], tool["annotations"])
    if value is None:
        del annotations[hint]
    else:
        annotations[hint] = value
    with pytest.raises(ServiceConformanceError, match=hint):
        validate_mcp_tools_list({"tools": [tool]})


@pytest.mark.parametrize("field", ["inputSchema", "outputSchema"])
@pytest.mark.parametrize(
    "schema",
    [
        True,
        {},
        {"type": "array"},
        {"type": "object", "properties": {"value": {"type": "not-a-type"}}},
        {"type": "object", "required": "value"},
        {"type": "object", "$schema": 12},
        {"type": "object", "$schema": "https://invalid.example/schema"},
    ],
)
def test_mcp_rejects_invalid_schemas(field: str, schema: object) -> None:
    tool = _tool()
    tool[field] = schema
    with pytest.raises(ServiceConformanceError, match=field):
        validate_mcp_tools_list({"tools": [tool]})


def test_mcp_accepts_known_dialect_without_resolving_remote_references() -> None:
    tool = _tool()
    tool["inputSchema"] = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"profile": {"$ref": "https://nonexistent.invalid/profile.json"}},
    }

    report = validate_mcp_tools_list({"tools": [tool]})
    assert any("references" in item for item in report.limitations)


def test_provider_claimed_approval_has_no_effect() -> None:
    response: dict[str, object] = {"tools": [_tool()]}
    original = deepcopy(response)
    baseline = validate_mcp_tools_list(response)
    assert response == original
    tool = cast(list[dict[str, object]], response["tools"])[0]
    tool["approved"] = True
    tool["platform_authorized"] = True
    cast(dict[str, object], tool["annotations"])["approved"] = True
    response["approved"] = True

    assert validate_mcp_tools_list(response) == baseline
    assert original == {"tools": [_tool()]}
    assert validate_model_catalog({"data": [{"id": "a", "approved": True}]}) == (
        validate_model_catalog({"data": [{"id": "a"}]})
    )


def test_schema_failure_does_not_echo_provider_content() -> None:
    tool = _tool()
    tool["inputSchema"] = {"type": "object", "required": "synthetic-secret"}

    with pytest.raises(ServiceConformanceError) as caught:
        validate_mcp_tools_list({"tools": [tool]})
    assert "synthetic-secret" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_checks_remain_active_with_optimized_python() -> None:
    program = (
        "from heartwood.adapters.service_conformance import "
        "validate_model_catalog, ServiceConformanceError\n"
        "try:\n"
        "    validate_model_catalog({'data': [{'id': ''}]})\n"
        "except ServiceConformanceError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    result = subprocess.run([sys.executable, "-O", "-c", program], check=False, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
