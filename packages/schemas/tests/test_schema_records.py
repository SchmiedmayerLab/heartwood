# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for versioned Heartwood schema records."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from heartwood.schemas import (
    ApprovalRecord,
    AuditEvent,
    ConfirmationRequest,
    DetectorEvidence,
    EgressAttestationRecord,
    ModelCallDecision,
    PolicyProfile,
    SkillMetadata,
    schema_for,
    schema_names,
)


def test_schema_inventory_is_versioned() -> None:
    expected = {
        "approval-record.v1",
        "audit-event.v1",
        "confirmation-request.v1",
        "detector-evidence.v1",
        "egress-attestation-record.v1",
        "model-call-decision.v1",
        "policy-profile.v1",
        "skill-metadata.v1",
    }
    assert set(schema_names()) == expected
    for name in expected:
        schema = schema_for(name)
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "schema_version" in schema["properties"]


def test_schema_for_accepts_fully_qualified_schema_version() -> None:
    policy = PolicyProfile(policy_id="generic-default", platform_id="generic")
    schema = schema_for(policy.schema_version)
    assert schema["properties"]["schema_version"]["const"] == "heartwood.policy-profile.v1"


def test_policy_profile_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PolicyProfile.model_validate(
            {
                "policy_id": "generic-default",
                "platform_id": "generic",
                "unexpected": True,
            }
        )


def test_policy_profile_enforces_aggregate_count_floor() -> None:
    with pytest.raises(ValidationError):
        PolicyProfile(
            policy_id="generic-default",
            platform_id="generic",
            aggregate_count_floor=19,
        )


def test_policy_profile_defaults_to_confirmation_for_every_action() -> None:
    policy = PolicyProfile(policy_id="generic-default", platform_id="generic")

    assert policy.allowed_action_confirmation_modes == ("always-confirm",)

    with pytest.raises(ValidationError):
        PolicyProfile.model_validate(
            {
                "policy_id": "generic-default",
                "platform_id": "generic",
                "allowed_action_confirmation_modes": ["never-confirm"],
            }
        )


def test_model_call_decision_requires_reason() -> None:
    decision = ModelCallDecision(
        decision_id="decision-1",
        policy_profile_id="generic-default",
        endpoint="https://model.example.invalid",
        capability_tier="supervised",
        decision="deny",
        reason="endpoint is not allowlisted",
    )
    assert decision.schema_version == "heartwood.model-call-decision.v1"

    with pytest.raises(ValidationError):
        ModelCallDecision.model_validate(
            {
                "decision_id": "decision-2",
                "policy_profile_id": "generic-default",
                "endpoint": "https://model.example.invalid",
                "capability_tier": "supervised",
                "decision": "deny",
                "reason": "",
            }
        )


def test_audit_and_attestation_records_are_hash_chain_ready() -> None:
    event = AuditEvent(
        event_id="event-1",
        session_id="session-1",
        sequence=0,
        event_type="model_call_decision",
        occurred_at="2026-01-01T00:00:00Z",
        payload={"decision_id": "decision-1"},
        previous_event_hash=None,
        event_hash="sha256:synthetic",
    )
    attestation = EgressAttestationRecord(
        record_id="attestation-1",
        session_id=event.session_id,
        decision_id="decision-1",
        policy_profile_id="generic-default",
        endpoint="https://model.example.invalid",
        decision="deny",
        occurred_at=event.occurred_at,
        reason="endpoint is not allowlisted",
    )
    assert event.payload["decision_id"] == attestation.decision_id


def test_detector_evidence_bounds_confidence() -> None:
    DetectorEvidence(
        detection_id="detect-1",
        detector_kind="platform",
        candidate_id="generic",
        confidence=1.0,
        evidence=("no managed-platform environment markers detected",),
    )
    with pytest.raises(ValidationError):
        DetectorEvidence(
            detection_id="detect-2",
            detector_kind="platform",
            candidate_id="generic",
            confidence=1.1,
            evidence=("invalid confidence",),
        )


def test_skill_metadata_accepts_skill_md_aliases() -> None:
    metadata = SkillMetadata.model_validate(
        {
            "heartwood.dataset-types": "omop-cdm,fhir",
            "heartwood.platforms": "generic,terra",
            "heartwood.phi-risk": "none",
            "heartwood.trust-tier": "verified",
            "heartwood.requires-network": "false",
            "heartwood.version": "0.2.0-beta.9",
            "heartwood.sig": "sigstore:synthetic-bundle",
        }
    )
    assert metadata.dataset_types == ("omop-cdm", "fhir")
    assert metadata.platforms == ("generic", "terra")
    assert metadata.requires_network is False
    assert metadata.version == "0.2.0-beta.9"


@pytest.mark.parametrize(
    "version",
    [
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-01",
        "1.2.3-beta.01",
        "1.2",
        "v1.2.3",
    ],
)
def test_skill_metadata_rejects_invalid_semantic_versions(version: str) -> None:
    with pytest.raises(ValidationError):
        SkillMetadata.model_validate(
            {
                "heartwood.dataset-types": "omop-cdm",
                "heartwood.platforms": "generic",
                "heartwood.phi-risk": "none",
                "heartwood.trust-tier": "community",
                "heartwood.requires-network": "false",
                "heartwood.version": version,
            }
        )


def test_verified_skill_metadata_requires_signature() -> None:
    with pytest.raises(ValidationError):
        SkillMetadata.model_validate(
            {
                "heartwood.dataset-types": "omop-cdm",
                "heartwood.platforms": "generic",
                "heartwood.phi-risk": "none",
                "heartwood.trust-tier": "verified",
                "heartwood.requires-network": "false",
                "heartwood.version": "0.1.0",
            }
        )

    community = SkillMetadata.model_validate(
        {
            "heartwood.dataset-types": "omop-cdm",
            "heartwood.platforms": "generic",
            "heartwood.phi-risk": "none",
            "heartwood.trust-tier": "community",
            "heartwood.requires-network": "false",
            "heartwood.version": "0.1.0",
        }
    )
    assert community.signature is None


def test_approval_record_captures_human_decision() -> None:
    approval = ApprovalRecord(
        approval_id="approval-1",
        session_id="session-1",
        target_type="skill",
        target_id="heartwood.omop-summary",
        decision="approved",
        actor_id="synthetic-reviewer",
        occurred_at="2026-01-01T00:00:00Z",
        reason="synthetic fixture approval",
    )
    assert approval.decision == "approved"


def test_approval_record_accepts_tool_call_target() -> None:
    approval = ApprovalRecord(
        approval_id="approval-2",
        session_id="session-1",
        target_type="tool-call",
        target_id="session-1-toolcall-0",
        decision="approved",
        actor_id="synthetic-reviewer",
        occurred_at="2026-01-01T00:00:00Z",
    )
    assert approval.target_type == "tool-call"


def test_confirmation_request_bounds_risk_tier() -> None:
    request = ConfirmationRequest(
        request_id="confirm-1",
        session_id="session-1",
        tool_call_id="session-1-toolcall-0",
        tool_name="heartwood.synthetic.noop",
        risk="low",
        summary="run the synthetic aggregate no-op",
        arguments={"command": "python run.py --output cohort-summary.json"},
    )
    assert request.risk == "low"
    assert request.arguments["command"] == "python run.py --output cohort-summary.json"
    with pytest.raises(ValidationError):
        ConfirmationRequest.model_validate(
            {
                "request_id": "confirm-2",
                "session_id": "session-1",
                "tool_call_id": "session-1-toolcall-0",
                "tool_name": "heartwood.synthetic.noop",
                "risk": "catastrophic",
                "summary": "invalid risk tier",
            }
        )
