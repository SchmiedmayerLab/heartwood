# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Policy-gated model-call proxy used by the session gateway."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from heartwood.model_policy import ModelPolicyEngine
from heartwood.schemas import EgressAttestationRecord, JsonValue, ModelCallDecision, PolicyProfile

ModelInvoker = Callable[[], JsonValue]


@dataclass(frozen=True, slots=True)
class ModelProxyResult:
    """Result of a policy-gated model call."""

    decision: ModelCallDecision
    attestation: EgressAttestationRecord
    response: JsonValue | None


class ModelEgressProxy:
    """Evaluate policy before any model-provider call is invoked."""

    def __init__(self, policy_profile: PolicyProfile) -> None:
        self.engine = ModelPolicyEngine(policy_profile)

    def call(
        self,
        *,
        endpoint: str,
        capability_tier: str,
        decision_id: str,
        purpose: str,
        record_id: str,
        session_id: str,
        occurred_at: str,
        invoke: ModelInvoker,
    ) -> ModelProxyResult:
        """Evaluate the proposed call and invoke the downstream provider only if allowed."""
        decision = self.engine.evaluate(
            endpoint=endpoint,
            capability_tier=capability_tier,
            decision_id=decision_id,
            purpose=purpose,
        )
        attestation = self.engine.attestation(
            decision=decision,
            record_id=record_id,
            session_id=session_id,
            occurred_at=occurred_at,
        )
        if decision.decision == "deny":
            return ModelProxyResult(decision=decision, attestation=attestation, response=None)
        return ModelProxyResult(
            decision=decision,
            attestation=attestation,
            response=invoke(),
        )
