# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for model-call policy evaluation."""

from __future__ import annotations

import pytest

from heartwood.model_policy import ModelPolicyEngine, PolicyInputError, filter_credentials
from heartwood.schemas import PolicyProfile


def _policy() -> PolicyProfile:
    return PolicyProfile(
        policy_id="generic-default",
        platform_id="generic",
        allowed_model_endpoints=("https://model.local.invalid/v1/chat",),
    )


def test_policy_denies_unlisted_endpoint_by_default() -> None:
    decision = ModelPolicyEngine(_policy()).evaluate(
        endpoint="https://public.example.invalid/v1/chat",
        capability_tier="supervised",
        decision_id="decision-1",
        purpose="synthetic model call",
    )
    assert decision.decision == "deny"
    assert decision.reason == "endpoint is not allowlisted for synthetic model call"


def test_policy_allows_exact_normalized_endpoint() -> None:
    decision = ModelPolicyEngine(_policy()).evaluate(
        endpoint="HTTPS://MODEL.LOCAL.INVALID/v1/chat",
        capability_tier="supervised",
        decision_id="decision-1",
        purpose="synthetic model call",
    )
    assert decision.decision == "allow"
    assert decision.endpoint == "https://model.local.invalid/v1/chat"


def test_policy_allows_unlisted_endpoint_when_default_deny_disabled() -> None:
    profile = PolicyProfile(
        policy_id="permissive",
        platform_id="generic",
        deny_egress_by_default=False,
        allowed_model_endpoints=("https://model.local.invalid/v1/chat",),
    )
    decision = ModelPolicyEngine(profile).evaluate(
        endpoint="https://public.example.invalid/v1/chat",
        capability_tier="supervised",
        decision_id="decision-1",
        purpose="synthetic model call",
    )
    assert decision.decision == "allow"
    assert decision.endpoint == "https://public.example.invalid/v1/chat"


def test_policy_allows_local_scheme_endpoint() -> None:
    profile = PolicyProfile(
        policy_id="local-default",
        platform_id="generic",
        allowed_model_endpoints=("local://agent/v1/invoke",),
    )
    decision = ModelPolicyEngine(profile).evaluate(
        endpoint="LOCAL://AGENT/v1/invoke",
        capability_tier="supervised",
        decision_id="decision-1",
        purpose="synthetic model call",
    )
    assert decision.decision == "allow"
    assert decision.endpoint == "local://agent/v1/invoke"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://model.local.invalid.evil/v1/chat",
        "http://model.local.invalid/v1/chat",
        "https://model.local.invalid:8443/v1/chat",
        "https://model.local.invalid:99999/v1/chat",
        "https://model.local.invalid/v1/chat/extra",
        "https://model.local.invalid/v1/chat?token=secret",
        "https://token@model.local.invalid/v1/chat",
        "local://token@agent/v1/invoke",
        "not-a-url",
    ],
)
def test_policy_rejects_endpoint_variants(endpoint: str) -> None:
    decision = ModelPolicyEngine(_policy()).evaluate(
        endpoint=endpoint,
        capability_tier="supervised",
        decision_id="decision-1",
        purpose="synthetic model call",
    )
    assert decision.decision == "deny"


def test_policy_masks_invalid_endpoint_in_decision_record() -> None:
    decision = ModelPolicyEngine(_policy()).evaluate(
        endpoint="https://token@model.local.invalid/v1/chat",
        capability_tier="supervised",
        decision_id="decision-1",
        purpose="synthetic model call",
    )
    assert decision.decision == "deny"
    assert decision.endpoint == "[invalid-endpoint]"


def test_policy_rejects_invalid_allowlist_at_construction() -> None:
    profile = PolicyProfile(
        policy_id="bad-allowlist",
        platform_id="generic",
        allowed_model_endpoints=("https://token@model.local.invalid/v1/chat",),
    )
    with pytest.raises(PolicyInputError):
        ModelPolicyEngine(profile)


def test_policy_denies_experimental_autonomy() -> None:
    decision = ModelPolicyEngine(_policy()).evaluate(
        endpoint="https://model.local.invalid/v1/chat",
        capability_tier="experimental",
        decision_id="decision-1",
        purpose="synthetic model call",
    )
    assert decision.decision == "deny"
    assert "capability tier experimental" in decision.reason


def test_policy_rejects_unknown_capability_tier() -> None:
    with pytest.raises(PolicyInputError):
        ModelPolicyEngine(_policy()).evaluate(
            endpoint="https://model.local.invalid/v1/chat",
            capability_tier="unknown",
            decision_id="decision-1",
            purpose="synthetic model call",
        )


def test_policy_builds_attestation_for_decision() -> None:
    engine = ModelPolicyEngine(_policy())
    decision = engine.evaluate(
        endpoint="https://model.local.invalid/v1/chat",
        capability_tier="supervised",
        decision_id="decision-1",
        purpose="synthetic model call",
    )
    attestation = engine.attestation(
        decision=decision,
        record_id="attestation-1",
        session_id="session-1",
        occurred_at="2026-01-01T00:00:00Z",
    )
    assert attestation.decision_id == decision.decision_id
    assert attestation.decision == "allow"


def test_filter_credentials_keeps_only_allowlisted_values() -> None:
    filtered = filter_credentials(
        {"ALLOWED_TOKEN": "ok", "AWS_SECRET_ACCESS_KEY": "blocked"},
        ("ALLOWED_TOKEN",),
    )
    assert filtered == {"ALLOWED_TOKEN": "ok"}
