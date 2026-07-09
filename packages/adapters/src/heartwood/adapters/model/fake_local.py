# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deterministic local model-provider adapter for tests and replay."""

from __future__ import annotations

from heartwood.adapters import ModelCallRequest, ModelInvocationRequest
from heartwood.model_policy import ModelPolicyEngine
from heartwood.schemas import JsonValue, ModelCallDecision, PolicyProfile


class FakeLocalModelProviderAdapter:
    """Model provider that delegates every decision to the policy engine."""

    def __init__(self, policy_profile: PolicyProfile) -> None:
        """Initialize the provider with the platform policy profile."""
        self.policy = ModelPolicyEngine(policy_profile)

    @property
    def provider_id(self) -> str:
        """Return the stable model-provider id."""
        return "fake-local"

    @property
    def capability_tier(self) -> str:
        """Return the fake provider capability tier."""
        return "supervised"

    def evaluate_model_call(self, request: ModelCallRequest) -> ModelCallDecision:
        """Evaluate a proposed model call without executing network egress."""
        return self.policy.evaluate(
            endpoint=request.endpoint,
            capability_tier=request.capability_tier,
            decision_id="decision-synthetic-model-call",
            purpose=request.purpose,
        )

    def invoke_model_call(self, request: ModelInvocationRequest) -> JsonValue:
        """Return a deterministic response for tests that do not need network I/O."""
        return {
            "id": "fake-local-model-response",
            "model": request.model,
            "object": "chat.completion",
            "usage": {"prompt_length": request.prompt_length},
        }
