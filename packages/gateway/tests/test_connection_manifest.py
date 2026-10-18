# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Platform manifest parsing uses shared structure and existing runtime safety rules."""

from __future__ import annotations

import pytest

from heartwood.gateway._model_catalog import ModelCatalogError, model_connections_from_mapping


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


def test_subscription_vendor_reaches_runtime_validation() -> None:
    item = {**connection(), "protocol": "subscription", "static_models": []}
    parsed = model_connections_from_mapping(manifest({**item, "subscription_vendor": "openai"}))
    assert parsed[-1].subscription_vendor == "openai"
    with pytest.raises(ModelCatalogError, match="supported subscription_vendor"):
        model_connections_from_mapping(manifest({**item, "subscription_vendor": "unsupported"}))
    with pytest.raises(ModelCatalogError, match="only for subscription"):
        model_connections_from_mapping(manifest({**connection(), "subscription_vendor": "openai"}))


@pytest.mark.parametrize(
    "update",
    [
        {"source": []},
        {"description": 7},
        {"static_models": [True]},
        {"api_version": {"unexpected": "synthetic-secret-value"}},
        {"subscription_vendor": ["synthetic-secret-value"]},
    ],
)
def test_malformed_types_raise_domain_error_without_input_values(update: dict[str, object]) -> None:
    with pytest.raises(ModelCatalogError) as failure:
        model_connections_from_mapping(manifest({**connection(), **update}))
    assert "synthetic-secret-value" not in str(failure.value)


@pytest.mark.parametrize(
    "payload",
    [
        {**manifest(connection()), "unknown": "synthetic-secret-value"},
        {**manifest(connection()), "token": "synthetic-secret-value"},
        manifest({**connection(), "api_key": "synthetic-secret-value"}),
    ],
)
def test_unknown_envelope_and_secret_values_are_rejected(payload: object) -> None:
    with pytest.raises(ModelCatalogError) as failure:
        model_connections_from_mapping(payload)
    assert "synthetic-secret-value" not in str(failure.value)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"base_url": "http://models.example/v1"}, "HTTPS"),
        ({"base_url": "https://different.example/v1"}, "same origin"),
        ({"credential_kind": "file", "api_key_file": "relative/path"}, "absolute"),
        ({"static_models": ["repeated", "repeated"]}, "unique"),
        ({"connection_id": "heartwood"}, "ids must be unique"),
    ],
)
def test_structurally_valid_manifest_still_requires_runtime_safety(
    update: dict[str, object], message: str
) -> None:
    with pytest.raises(ModelCatalogError, match=message):
        model_connections_from_mapping(manifest({**connection(), **update}))
