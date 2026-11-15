# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Machine-readable platform artifacts generated from existing contract owners."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, TypeAdapter

from heartwood.schemas._api import PlatformCapabilitiesResponse
from heartwood.schemas._connections import ModelConnectionsManifest
from heartwood.schemas._planning_contract import (
    BudgetObservation,
    OperationSetProposal,
    PlanningOverview,
    PlatformAuthorization,
    PlatformEstimate,
    PlatformOperationObservation,
    ResearcherDecision,
    WorkloadPrediction,
)
from heartwood.schemas._platform_metadata import (
    DataClassification,
    PlatformLaunchContext,
    PlatformMcpServiceProfile,
    PlatformModelMetadataManifest,
)
from heartwood.schemas._records import PolicyProfile
from heartwood.schemas._upstream import (
    MCP_TASKS_REVISION,
    MCP_TASKS_SCHEMA_SHA256,
    MCP_TASKS_VERSION,
    mcp_tasks_schema,
    mcp_tasks_schema_bytes,
    platform_contract_notices,
)

PLATFORM_CONTRACT_VERSION = "0.1.0"
MCP_REQUIRED_TOOL_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
_CURRENT_ARTIFACTS = frozenset(
    {
        "capabilities.schema.json",
        "model-connections.schema.json",
        "policy-profile.schema.json",
        "gateway.openapi.json",
    }
)
_DRAFT_MODELS: dict[str, type[BaseModel]] = {
    "launch-context.schema.json": PlatformLaunchContext,
    "model-metadata.schema.json": PlatformModelMetadataManifest,
    "mcp-service.schema.json": PlatformMcpServiceProfile,
    "workload-prediction.schema.json": WorkloadPrediction,
    "platform-estimate.schema.json": PlatformEstimate,
    "operation-set.schema.json": OperationSetProposal,
    "researcher-decision.schema.json": ResearcherDecision,
    "budget-observation.schema.json": BudgetObservation,
    "platform-authorization.schema.json": PlatformAuthorization,
    "operation-observation.schema.json": PlatformOperationObservation,
    "planning-overview.schema.json": PlanningOverview,
}


def validate_platform_document(name: str, document: str | bytes) -> BaseModel:
    """Validate a draft message's shape and cross-field invariants, never its authority.

    JSON Schema alone cannot establish cross-record consistency. Use the owning parser
    for those checks; authentication, current policy, and execution remain external.
    """
    try:
        model = _DRAFT_MODELS[name]
    except KeyError:
        raise ValueError("Unknown draft platform artifact") from None
    return model.model_validate_json(document)


def contract_json_bytes(document: Mapping[str, Any]) -> bytes:
    """Encode reproducible export bytes, not a canonical signing representation."""
    return (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def platform_artifact_bytes(name: str, document: Mapping[str, Any]) -> bytes:
    """Preserve upstream bytes exactly; format owned artifacts reproducibly."""
    if name == "mcp-tasks.schema.json":
        if document != mcp_tasks_schema():
            raise ValueError("Upstream MCP Tasks schema must not be modified")
        return mcp_tasks_schema_bytes()
    return contract_json_bytes(document)


def platform_contract_index(documents: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """Describe artifact versions and exact bytes without advertising runtime support."""
    return {
        "schema_version": "heartwood.platform-contract-index.v1",
        "contract_version": PLATFORM_CONTRACT_VERSION,
        "compatibility": {
            "scheme": "https://semver.org/spec/v2.0.0.html",
            "policy": (
                "Pre-1.0: minor versions may break compatibility; patches preserve it. "
                "From 1.0: major for incompatible changes, minor for compatible additions, "
                "patch for compatible fixes. Published versions are immutable."
            ),
            "scope": "Message structure, validation rules, and documented behavior.",
            "document_identifiers": (
                "Format-family identifiers, not independent SemVer releases. "
                "The bundle version and artifact digest identify the exact contract revision."
            ),
            "drafts": "Draft artifacts require an exact bundle version; reassess before upgrading.",
            "runtime": "Artifact availability does not advertise a runtime capability.",
            "upstream": "Upstream revisions and negotiated capabilities are versioned separately.",
        },
        "artifacts": [
            {
                "path": name,
                "media_type": (
                    "application/vnd.oai.openapi+json"
                    if name == "gateway.openapi.json"
                    else "application/schema+json"
                ),
                "maturity": (
                    "upstream"
                    if name == "mcp-tasks.schema.json"
                    else "current"
                    if name in _CURRENT_ARTIFACTS
                    else "draft"
                ),
                "sha256": hashlib.sha256(platform_artifact_bytes(name, document)).hexdigest(),
            }
            for name, document in sorted(documents.items())
        ],
        "notices": [
            {"path": name, "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in sorted(platform_contract_notices().items())
        ],
        "design_sources": [
            {
                "name": "MCP Tasks extension",
                "status": "published",
                "version": MCP_TASKS_VERSION,
                "revision": MCP_TASKS_REVISION,
                "source_sha256": MCP_TASKS_SCHEMA_SHA256,
                "url": "https://github.com/modelcontextprotocol/ext-tasks/blob/"
                f"{MCP_TASKS_REVISION}/schema/{MCP_TASKS_VERSION}/schema.json",
                "license": "Apache-2.0",
                "license_file": "mcp-tasks.LICENSE.txt",
            },
            {
                "name": "FOCUS SKU Price proposal",
                "status": "draft",
                "revision": "005a30566a754db4e0f346d20c7063daba579080",
                "url": "https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS_Spec/"
                "tree/005a30566a754db4e0f346d20c7063daba579080/specification/datasets/sku_price",
            },
            {
                "name": "FOCUS supported estimation features proposal",
                "status": "draft",
                "revision": "a2f9f93cb618ab490b90098d0456dfcdb813c5e7",
                "url": "https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS_Spec/"
                "tree/a2f9f93cb618ab490b90098d0456dfcdb813c5e7",
            },
        ],
    }


def platform_schema_documents() -> dict[str, dict[str, Any]]:
    """Export current and draft structures; validity does not establish runtime authority.

    Capability responses use serialization mode. Deployment inputs use validation
    mode, matching their owning parsers. Each document is independently usable.
    """
    documents = {
        "capabilities.schema.json": TypeAdapter(PlatformCapabilitiesResponse).json_schema(
            mode="serialization"
        ),
        "model-connections.schema.json": ModelConnectionsManifest.model_json_schema(
            mode="validation"
        ),
        "policy-profile.schema.json": PolicyProfile.model_json_schema(mode="validation"),
        "data-classification.schema.json": TypeAdapter(DataClassification).json_schema(),
        "mcp-tool-annotations.schema.json": {
            "title": "Heartwood required MCP annotation subset",
            "description": (
                "Integration-profile overlay: requires four explicit upstream boolean hints. "
                "This checks neither complete MCP conformance nor the truth of those hints. "
                "Annotations never establish authorization."
            ),
            "$comment": "Upstream definitions: "
            "https://modelcontextprotocol.io/specification/2025-06-18/server/tools",
            "type": "object",
            "properties": {hint: {"type": "boolean"} for hint in MCP_REQUIRED_TOOL_HINTS},
            "required": list(MCP_REQUIRED_TOOL_HINTS),
            "additionalProperties": True,
        },
        **{
            name: model.model_json_schema(mode="validation")
            for name, model in _DRAFT_MODELS.items()
        },
    }
    for schema in documents.values():
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    documents["mcp-tasks.schema.json"] = mcp_tasks_schema()
    return documents
