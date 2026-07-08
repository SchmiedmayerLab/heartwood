# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Reusable conformance assertions for adapter implementations."""

from __future__ import annotations

from collections.abc import Mapping

from heartwood.adapters._protocols import (
    DataSourceAdapter,
    ModelCallRequest,
    ModelProviderAdapter,
    PlatformAdapter,
    RegistryAdapter,
)
from heartwood.model_policy import PolicyInputError, normalize_endpoint


def _assert_confidence(value: float) -> None:
    assert 0.0 <= value <= 1.0, f"confidence must be within [0, 1], got {value}"


def assert_platform_adapter_conforms(
    adapter: PlatformAdapter,
    env: Mapping[str, str] | None = None,
) -> None:
    """Assert the shared minimum contract for platform adapters."""
    assert adapter.adapter_id
    detection = adapter.detect({} if env is None else env)
    assert detection.adapter_id == adapter.adapter_id
    _assert_confidence(detection.confidence)
    assert detection.evidence
    assert isinstance(adapter.data_mounts(), tuple)
    assert isinstance(adapter.credential_allowlist(), tuple)
    profile = adapter.default_policy_profile()
    assert profile.platform_id == adapter.adapter_id
    assert profile.deny_egress_by_default is True


def assert_model_provider_adapter_conforms(
    adapter: ModelProviderAdapter,
    request: ModelCallRequest | None = None,
) -> None:
    """Assert the shared minimum contract for model-provider adapters."""
    assert adapter.provider_id
    assert adapter.capability_tier in {"autonomous", "supervised", "experimental"}
    default_request = request is None
    if request is None:
        request = ModelCallRequest(
            endpoint="https://model.example.invalid",
            capability_tier=adapter.capability_tier,
            purpose="synthetic conformance check",
        )
    decision = adapter.evaluate_model_call(request)
    if decision.endpoint != "[invalid-endpoint]":
        try:
            assert decision.endpoint == normalize_endpoint(request.endpoint)
        except PolicyInputError:
            assert decision.decision == "deny"
    else:
        assert decision.decision == "deny"
    assert decision.capability_tier == request.capability_tier
    assert decision.reason
    if default_request:
        assert decision.decision == "deny"


def assert_data_source_adapter_conforms(
    adapter: DataSourceAdapter,
    sample_table: str = "person",
) -> None:
    """Assert the shared minimum contract for data-source adapters."""
    assert adapter.source_id
    fingerprint = adapter.fingerprint()
    _assert_confidence(fingerprint.confidence)
    assert fingerprint.dataset_type
    assert fingerprint.evidence
    requested_columns = ("person_id",)
    rows = adapter.read_table(sample_table, columns=requested_columns, limit=2)
    assert len(rows) <= 2
    for row in rows:
        assert isinstance(row, Mapping)
        assert set(row).issubset(requested_columns)


def assert_registry_adapter_conforms(
    adapter: RegistryAdapter,
    skill_id: str = "heartwood.synthetic.omop-summary",
    version: str = "0.1.0",
) -> None:
    """Assert the shared minimum contract for registry adapters."""
    assert adapter.registry_id
    reference = adapter.resolve_skill(skill_id, version)
    assert reference.skill_id == skill_id
    assert reference.version == version
    assert reference.source
    verification = adapter.verify_skill(reference)
    assert verification.reason
