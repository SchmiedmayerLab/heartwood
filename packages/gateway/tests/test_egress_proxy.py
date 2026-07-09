# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the model egress proxy."""

from __future__ import annotations

from heartwood.gateway import ModelEgressProxy
from heartwood.schemas import JsonValue, PolicyProfile


def _policy() -> PolicyProfile:
    return PolicyProfile(
        policy_id="generic-default",
        platform_id="generic",
        allowed_model_endpoints=("https://model.local.invalid/v1/chat",),
    )


def test_egress_proxy_blocks_denied_call_before_invocation() -> None:
    invoked = False
    proxy = ModelEgressProxy(_policy())

    def invoke() -> str:
        nonlocal invoked
        invoked = True
        return "should not run"

    result = proxy.call(
        endpoint="https://public.example.invalid/v1/chat",
        capability_tier="supervised",
        decision_id="decision-1",
        purpose="synthetic model call",
        record_id="attestation-1",
        session_id="session-1",
        occurred_at="2026-01-01T00:00:00Z",
        invoke=invoke,
    )

    assert result.decision.decision == "deny"
    assert result.attestation.decision == "deny"
    assert result.response is None
    assert invoked is False


def test_egress_proxy_invokes_allowed_call_and_records_attestation() -> None:
    invoked = False
    proxy = ModelEgressProxy(_policy())

    def invoke() -> JsonValue:
        nonlocal invoked
        invoked = True
        return {"status": "ok"}

    result = proxy.call(
        endpoint="https://model.local.invalid/v1/chat",
        capability_tier="supervised",
        decision_id="decision-1",
        purpose="synthetic model call",
        record_id="attestation-1",
        session_id="session-1",
        occurred_at="2026-01-01T00:00:00Z",
        invoke=invoke,
    )

    assert result.decision.decision == "allow"
    assert result.attestation.decision == "allow"
    assert result.response == {"status": "ok"}
    assert invoked is True
