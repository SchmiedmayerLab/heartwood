# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Structural metadata validation without service calls or implicit authorization."""

from __future__ import annotations

import json
from typing import Any, Literal

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, TypeAdapter, ValidationError

from heartwood.schemas._platform_metadata import (
    DataClassification,
    DataEligibilityObservation,
    McpDiscoveryObservation,
    PlatformLaunchContext,
    PlatformMcpServiceProfile,
    PlatformModelMetadata,
    PlatformModelMetadataManifest,
)

_NOW = "2026-10-17T12:00:00Z"
_LATER = "2026-10-17T13:00:00Z"
_LAUNCH = {
    "project_context_id": "launch-synthetic",
    "platform_id": "synthetic-platform",
    "project_id": "project-a",
    "billing_attribution_ref": "binding-a",
    "authority_ref": "platform-authority",
    "policy_ref": "policy-v1",
    "issued_at": _NOW,
}
_ELIGIBILITY = {
    "classification": "restricted",
    "allowed": None,
    "regulatory_descriptors": ["synthetic-regulatory-label"],
    "authority_ref": "platform-authority",
    "policy_ref": "policy-v1",
    "source_ref": "synthetic-observation",
    "observed_at": _NOW,
}
_MODEL = {"connection_id": "connection-a", "model_id": "model-a", "protocols": ["openai"]}
_DISCOVERY = {
    "protocol_version": "2025-11-25",
    "capabilities": ["tools"],
    "extension_refs": [],
    "source_ref": "synthetic-initialize-result",
    "observed_at": _NOW,
}
_SERVICE = {
    "service_id": "service-a",
    "url": "https://services.example.org/mcp",
    "credential_ref": "service-credential",
    "authority_ref": "platform-authority",
    "policy_ref": "policy-v1",
    "supported_protocol_versions": ["2025-11-25"],
    "required_capabilities": ["tools"],
}
_CASES: list[tuple[type[BaseModel], dict[str, Any]]] = [
    (PlatformLaunchContext, _LAUNCH),
    (DataEligibilityObservation, _ELIGIBILITY),
    (PlatformModelMetadata, _MODEL),
    (PlatformModelMetadataManifest, {"models": [_MODEL]}),
    (McpDiscoveryObservation, _DISCOVERY),
    (PlatformMcpServiceProfile, _SERVICE),
]


@pytest.mark.parametrize(("record_type", "example"), _CASES)
def test_metadata_round_trip_and_schema_agree(
    record_type: type[BaseModel], example: dict[str, Any]
) -> None:
    record = record_type.model_validate_json(json.dumps(example))
    assert record_type.model_validate_json(record.model_dump_json()) == record
    modes: tuple[Literal["validation", "serialization"], ...] = ("validation", "serialization")
    for mode in modes:
        value = example if mode == "validation" else record.model_dump(mode="json")
        schema = record_type.model_json_schema(mode=mode)
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)
    with pytest.raises(ValidationError, match="frozen"):
        setattr(record, next(iter(record_type.model_fields)), "change")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        record_type.model_validate_json(json.dumps({**example, "api_key": "synthetic-secret"}))


def test_classification_is_an_exact_vocabulary_without_an_order() -> None:
    schema = TypeAdapter(DataClassification).json_schema()
    assert schema["enum"] == ["public", "internal", "confidential", "restricted"]
    for classification in schema["enum"]:
        observation = DataEligibilityObservation.model_validate_json(
            json.dumps(
                {
                    **_ELIGIBILITY,
                    "classification": classification,
                }
            )
        )
        assert observation.allowed is None
    with pytest.raises(ValidationError):
        DataEligibilityObservation.model_validate_json(
            json.dumps(
                {
                    **_ELIGIBILITY,
                    "classification": "synthetic-regulatory-label",
                }
            )
        )


def test_model_unknowns_and_exact_route_identity_remain_explicit() -> None:
    model = PlatformModelMetadata.model_validate_json(
        json.dumps(
            {
                **_MODEL,
                "data_eligibility": [{**_ELIGIBILITY, "allowed": True}],
            }
        )
    )
    assert model.context_window_tokens is None
    assert model.max_input_tokens is None
    assert model.max_output_tokens is None
    assert model.supports_tool_calls is None
    assert [(item.classification, item.allowed) for item in model.data_eligibility] == [
        ("restricted", True)
    ]
    assert len(model.data_eligibility) == 1  # No inferred lower-class permissions.
    distinct = PlatformModelMetadataManifest.model_validate_json(
        json.dumps(
            {
                "models": [_MODEL, {**_MODEL, "connection_id": "connection-b"}],
            }
        )
    )
    assert len(distinct.models) == 2
    with pytest.raises(ValidationError, match="pairs must be unique"):
        PlatformModelMetadataManifest.model_validate_json(json.dumps({"models": [_MODEL, _MODEL]}))


@pytest.mark.parametrize(
    "field", ["context_window_tokens", "max_input_tokens", "max_output_tokens"]
)
@pytest.mark.parametrize("value", [0, -1, True, "100", 1.5])
def test_model_limits_reject_invalid_counts(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        PlatformModelMetadata.model_validate_json(json.dumps({**_MODEL, field: value}))


def test_model_limits_and_eligibility_do_not_accept_conflicting_declarations() -> None:
    cases: list[dict[str, object]] = [
        {"protocols": ["openai", "openai"]},
        {"data_eligibility": [_ELIGIBILITY, {**_ELIGIBILITY, "allowed": True}]},
        {"context_window_tokens": 100, "max_output_tokens": 101},
        {"supports_tool_calls": "yes"},
    ]
    for changes in cases:
        with pytest.raises(ValidationError):
            PlatformModelMetadata.model_validate_json(json.dumps({**_MODEL, **changes}))
    # Independent maxima need not sum to the context window; a request consumes both.
    model = PlatformModelMetadata.model_validate_json(
        json.dumps(
            {
                **_MODEL,
                "context_window_tokens": 100,
                "max_input_tokens": 100,
                "max_output_tokens": 100,
            }
        )
    )
    assert model.max_input_tokens == model.max_output_tokens == 100


@pytest.mark.parametrize(
    ("record_type", "example", "time_field"),
    [
        (PlatformLaunchContext, _LAUNCH, "issued_at"),
        (DataEligibilityObservation, _ELIGIBILITY, "observed_at"),
        (McpDiscoveryObservation, _DISCOVERY, "observed_at"),
    ],
)
def test_observation_times_require_timezones(
    record_type: type[BaseModel], example: dict[str, Any], time_field: str
) -> None:
    with pytest.raises(ValidationError):
        record_type.model_validate_json(json.dumps({**example, time_field: "2026-10-17T12:00:00"}))


@pytest.mark.parametrize(
    ("record_type", "example"),
    [(PlatformLaunchContext, _LAUNCH), (DataEligibilityObservation, _ELIGIBILITY)],
)
def test_expiry_must_follow_issue_or_observation(
    record_type: type[BaseModel], example: dict[str, Any]
) -> None:
    record_type.model_validate_json(json.dumps({**example, "expires_at": _LATER}))
    with pytest.raises(ValidationError, match="expires_at must follow"):
        record_type.model_validate_json(json.dumps({**example, "expires_at": _NOW}))


@pytest.mark.parametrize(
    "url",
    [
        "file:///project/service",
        "https:services.example.org/mcp",
        "https://user:secret@services.example.org/mcp",
        "https://services.example.org/mcp?token=secret",
        "https://services.example.org/mcp#secret",
        "https://services.example.org/mcp?",
        "https://services.example.org/mcp#",
        " https://services.example.org/mcp",
        "https://services.example.org/\nmcp",
        "https://services.example.org\\mcp",
    ],
)
def test_service_urls_reject_credentials_and_ambiguous_routes(url: str) -> None:
    with pytest.raises(ValidationError):
        PlatformMcpServiceProfile.model_validate_json(json.dumps({**_SERVICE, "url": url}))
    assert not Draft202012Validator(PlatformMcpServiceProfile.model_json_schema()).is_valid(
        {**_SERVICE, "url": url}
    )


@pytest.mark.parametrize("field", ["headers", "auth", "env", "command", "args", "access_token"])
def test_service_profile_cannot_embed_sdk_secrets_or_local_commands(field: str) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PlatformMcpServiceProfile.model_validate_json(json.dumps({**_SERVICE, field: "synthetic"}))


def test_discovery_is_optional_and_extensions_require_explicit_observations() -> None:
    service = PlatformMcpServiceProfile.model_validate_json(json.dumps(_SERVICE))
    assert service.discovery is None
    observed = PlatformMcpServiceProfile.model_validate_json(
        json.dumps(
            {
                **_SERVICE,
                "discovery": _DISCOVERY,
            }
        )
    )
    assert observed.discovery is not None
    assert observed.discovery.extension_refs == ()
    assert observed.discovery.capabilities == ("tools",)
    with pytest.raises(ValidationError, match="must be supported"):
        PlatformMcpServiceProfile.model_validate_json(
            json.dumps(
                {
                    **_SERVICE,
                    "discovery": {**_DISCOVERY, "protocol_version": "unknown-revision"},
                }
            )
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        McpDiscoveryObservation.model_validate_json(
            json.dumps({**_DISCOVERY, "task_state": "done"})
        )


@pytest.mark.parametrize("identity", ["", " ", "synthetic\n", "synthetic ref"])
def test_reference_schema_matches_parser_on_whitespace(identity: str) -> None:
    example = {**_LAUNCH, "project_context_id": identity}
    with pytest.raises(ValidationError):
        PlatformLaunchContext.model_validate_json(json.dumps(example))
    assert not Draft202012Validator(PlatformLaunchContext.model_json_schema()).is_valid(example)


def test_launch_context_does_not_choose_local_directory_or_expose_attribution() -> None:
    launch = PlatformLaunchContext.model_validate_json(json.dumps(_LAUNCH))
    assert launch.project_context_id == "launch-synthetic"
    assert launch.billing_attribution_ref == "binding-a"
    for field in ["working_directory", "billing_account", "grant", "credential"]:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            PlatformLaunchContext.model_validate_json(json.dumps({**_LAUNCH, field: "synthetic"}))
