# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Offline response checks for independently authenticated platform clients.

Pass decoded catalog or MCP ``tools/list`` results from the platform's existing
client. These checks do not contact services, execute tools, approve work, or
establish authentication, policy enforcement, or live-platform qualification.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from pydantic import ValidationError

from heartwood.schemas._platform_contract import (
    MCP_REQUIRED_TOOL_HINTS,
    validate_platform_document,
)

_COMMON_LIMITATIONS = (
    "Offline response validation does not test authentication, availability, or live execution.",
    "No platform authority, researcher approval, or spending authorization is established.",
    "Deployment-policy and approval-content enforcement require separate runtime tests.",
)


class ServiceConformanceError(ValueError):
    """A response violates the checked contract; messages exclude provider content."""


@dataclass(frozen=True)
class ServiceConformanceReport:
    """Describe exactly what an offline response check established."""

    service: Literal["model-catalog", "mcp-tools", "platform-message", "mcp-tasks"]
    item_count: int
    checks: tuple[str, ...]
    limitations: tuple[str, ...]


def validate_platform_message(name: str, response: object) -> ServiceConformanceReport:
    """Check a draft message with its owning parser, without trusting its claimed issuer.

    ``name`` is its bundle filename, for example ``platform-estimate.schema.json``.
    This checks cross-field relationships that JSON Schema alone cannot express.
    Exact reviewed bytes and authenticated sources must still be verified separately.
    """
    try:
        document = json.dumps(response, allow_nan=False)
        validate_platform_document(name, document)
    except (TypeError, ValueError, ValidationError, RecursionError):
        raise ServiceConformanceError("Draft platform message failed contract validation") from None
    return ServiceConformanceReport(
        service="platform-message",
        item_count=1,
        checks=("Draft message structure and owning parser's cross-field consistency checks",),
        limitations=(
            *_COMMON_LIMITATIONS,
            "Issuer identity, referenced document bytes, and current validity are not verified.",
            "Budget observations are not reservations or guaranteed available funds.",
        ),
    )


def validate_model_catalog(response: object) -> ServiceConformanceReport:
    """Validate one OpenAI-compatible catalog page, without implying model eligibility.

    Empty catalogs are valid: an identity may have no available models. Additional
    provider fields are ignored, including any field claiming approval. Declared
    context windows, protocols, tool support, and data classes need the separate
    platform model-metadata contract; this check does not validate those fields.
    """
    catalog = _object(response, "catalog")
    entries = _list(catalog.get("data"), "data")
    identities: set[str] = set()
    for index, entry in enumerate(entries):
        path = f"data[{index}]"
        model = _object(entry, path)
        identity = _text(model.get("id"), f"{path}.id")
        if identity in identities:
            raise ServiceConformanceError(f"{path}.id must be unique within the page")
        identities.add(identity)
    return ServiceConformanceReport(
        service="model-catalog",
        item_count=len(entries),
        checks=("Catalog page structure", "Nonempty, unique model identifiers within the page"),
        limitations=(
            *_COMMON_LIMITATIONS,
            "Model metadata, eligibility, protocol behavior, and other pages are not checked.",
        ),
    )


def validate_mcp_tools_list(response: object) -> ServiceConformanceReport:
    """Validate one decoded MCP tools/list result against the platform contract.

    This contract requires descriptions, object input/output schemas, and all four
    explicit boolean safety hints. Hints remain untrusted information, never
    authorization. JSON Schema syntax is checked offline using its declared known
    dialect, or Draft 2020-12 when omitted; references are not fetched or resolved.
    Pass the result object rather than the surrounding JSON-RPC response envelope.
    """
    result = _object(response, "tools/list result")
    entries = _list(result.get("tools"), "tools")
    identities: set[str] = set()
    for index, entry in enumerate(entries):
        path = f"tools[{index}]"
        tool = _object(entry, path)
        identity = _text(tool.get("name"), f"{path}.name")
        if identity in identities:
            raise ServiceConformanceError(f"{path}.name must be unique within the page")
        identities.add(identity)
        _text(tool.get("description"), f"{path}.description")
        annotations = _object(tool.get("annotations"), f"{path}.annotations")
        for hint in MCP_REQUIRED_TOOL_HINTS:
            if not isinstance(annotations.get(hint), bool):
                raise ServiceConformanceError(f"{path}.annotations.{hint} must be a boolean")
        for field in ("inputSchema", "outputSchema"):
            _object_schema(tool.get(field), f"{path}.{field}")
    return ServiceConformanceReport(
        service="mcp-tools",
        item_count=len(entries),
        checks=(
            "Tool page structure and nonempty, unique names within the page",
            "Descriptions and four explicit boolean tool annotations",
            "Object input/output schemas and JSON Schema syntax",
        ),
        limitations=(
            *_COMMON_LIMITATIONS,
            "Tool annotations are hints, not verified safety properties or authorization.",
            "Schema references, actual tool inputs/results, and other pages are not checked.",
        ),
    )


def _object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ServiceConformanceError(f"{path} must be an object with string keys")
    return value


def _list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise ServiceConformanceError(f"{path} must be an array")
    return value


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceConformanceError(f"{path} must be a nonempty string")
    return value


def _object_schema(value: object, path: str) -> None:
    schema = _object(value, path)
    if schema.get("type") != "object":
        raise ServiceConformanceError(f"{path}.type must be object")
    dialect = schema.get("$schema")
    if dialect is not None and not isinstance(dialect, str):
        raise ServiceConformanceError(f"{path} declares an unsupported JSON Schema dialect")
    validator = validator_for(schema, default=Draft202012Validator)
    declared_schema_id = validator.META_SCHEMA.get("$id", validator.META_SCHEMA.get("id"))
    if dialect is not None and (
        not isinstance(declared_schema_id, str)
        or dialect.rstrip("#") != declared_schema_id.rstrip("#")
    ):
        raise ServiceConformanceError(f"{path} declares an unsupported JSON Schema dialect")
    try:
        validator.check_schema(schema)
    except (SchemaError, RecursionError):
        raise ServiceConformanceError(f"{path} is not a valid JSON Schema") from None
