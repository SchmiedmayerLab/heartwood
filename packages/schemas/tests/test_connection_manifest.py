# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Structural validation for platform-owned model connection manifests."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from heartwood.schemas import ModelConnectionsManifest


def connection() -> dict[str, object]:
    return {
        "connection_id": "research",
        "label": "Research Models",
        "protocol": "static",
        "model_prefix": "openai/",
        "credential_kind": "managed-identity",
        "policy_endpoint": "https://models.example/v1/chat/completions",
        "static_models": ["research-model"],
    }


def manifest(value: object) -> dict[str, object]:
    return {"schema_version": "heartwood.model-connections.v1", "connections": [value]}


def test_manifest_round_trip_preserves_supported_fields() -> None:
    item = {
        **connection(),
        "source": "platform",
        "catalog_endpoint": None,
        "base_url": "https://models.example/v1",
        "api_key_env": None,
        "api_key_file": None,
        "api_version": "v1",
        "aws_region_name": "region-1",
        "aws_profile_name": "research",
        "description": "Authorized research models.",
        "subscription_vendor": None,
    }
    parsed = ModelConnectionsManifest.model_validate(manifest(item))
    assert parsed.model_dump() == manifest(item)
    assert ModelConnectionsManifest.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize(
    "update",
    [
        {"source": "user"},
        {"source": []},
        {"protocol": "unknown"},
        {"credential_kind": True},
        {"connection_id": 5},
        {"connection_id": "unsafe/id"},
        {"model_prefix": "missing-slash"},
        {"label": "   "},
        {"label": ""},
        {"description": None},
        {"policy_endpoint": False},
        {"api_key_env": ""},
        {"api_key_env": "invalid-name"},
        {"api_version": "   "},
        {"static_models": ["contains whitespace"]},
        {"static_models": "model"},
        {"static_models": [1]},
        {"subscription_vendor": ["openai"]},
        {"unknown": "synthetic-secret-value"},
    ],
)
def test_manifest_rejects_invalid_fields_without_coercion(update: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as failure:
        ModelConnectionsManifest.model_validate(manifest({**connection(), **update}))
    assert "synthetic-secret-value" not in str(failure.value)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"connections": []},
        {"schema_version": "heartwood.model-connections.v2", "connections": []},
        {"schema_version": "heartwood.model-connections.v1", "connections": {}},
        {**manifest(connection()), "unknown": True},
    ],
)
def test_manifest_rejects_invalid_envelopes(payload: object) -> None:
    with pytest.raises(ValidationError):
        ModelConnectionsManifest.model_validate(payload)


def test_schema_requires_explicit_version_and_forbids_unknown_fields() -> None:
    schema = ModelConnectionsManifest.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["schema_version", "connections"]
    assert schema["properties"]["schema_version"]["const"] == "heartwood.model-connections.v1"
    item = schema["$defs"]["ModelConnectionManifest"]
    assert item["additionalProperties"] is False
    assert item["properties"]["source"]["const"] == "platform"
    assert "subscription_vendor" in item["properties"]
    assert json.loads(json.dumps(schema)) == schema


@pytest.mark.parametrize(
    ("field", "valid"),
    [
        ("connection_id", "research"),
        ("api_key_env", "RESEARCH_KEY"),
        ("model_prefix", "provider/"),
        ("static_models", "model"),
    ],
)
@pytest.mark.parametrize("suffix", ["", "\n", "\r\n", "\r", "\t", " "])
def test_schema_and_parser_agree_on_absolute_end_anchors(
    field: str, valid: str, suffix: str
) -> None:
    value: object = valid + suffix
    if field == "static_models":
        value = [value]
    payload = manifest({**connection(), field: value})
    schema = ModelConnectionsManifest.model_json_schema()
    Draft202012Validator.check_schema(schema)
    schema_accepts = Draft202012Validator(schema).is_valid(payload)
    try:
        ModelConnectionsManifest.model_validate(payload)
    except ValidationError:
        parser_accepts = False
    else:
        parser_accepts = True
    assert schema_accepts == parser_accepts == (suffix == "")
