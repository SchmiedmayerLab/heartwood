# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Versioned Pydantic record schemas for Phase 0B contracts."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, ClassVar, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

__all__ = [
    "ApprovalRecord",
    "AuditEvent",
    "ConfirmationRequest",
    "DetectorEvidence",
    "EgressAttestationRecord",
    "JsonValue",
    "ModelCallDecision",
    "PolicyProfile",
    "SkillMetadata",
    "schema_for",
    "schema_names",
]

CapabilityTier: TypeAlias = Literal["autonomous", "supervised", "experimental"]
Decision: TypeAlias = Literal["allow", "deny"]


class _HeartwoodRecord(BaseModel):
    """Base model for immutable versioned Heartwood records."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class PolicyProfile(_HeartwoodRecord):
    """Policy rules that gate model calls and egress in a platform boundary."""

    schema_version: Literal["heartwood.policy-profile.v1"] = "heartwood.policy-profile.v1"
    policy_id: str = Field(min_length=1)
    platform_id: str = Field(min_length=1)
    deny_egress_by_default: bool = True
    allowed_model_endpoints: tuple[str, ...] = ()
    credential_allowlist: tuple[str, ...] = ()
    aggregate_count_floor: int = Field(default=20, ge=20)
    notes: str | None = None


class ModelCallDecision(_HeartwoodRecord):
    """Auditable decision for one proposed model call."""

    schema_version: Literal["heartwood.model-call-decision.v1"] = "heartwood.model-call-decision.v1"
    decision_id: str = Field(min_length=1)
    policy_profile_id: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    capability_tier: CapabilityTier
    decision: Decision
    reason: str = Field(min_length=1)


class EgressAttestationRecord(_HeartwoodRecord):
    """Record exported to attest allowed or denied egress decisions."""

    schema_version: Literal["heartwood.egress-attestation-record.v1"] = (
        "heartwood.egress-attestation-record.v1"
    )
    record_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    policy_profile_id: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    decision: Decision
    occurred_at: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class AuditEvent(_HeartwoodRecord):
    """Hash-chainable audit event emitted by a session or adapter."""

    schema_version: Literal["heartwood.audit-event.v1"] = "heartwood.audit-event.v1"
    event_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    event_type: str = Field(min_length=1)
    occurred_at: str = Field(min_length=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    previous_event_hash: str | None = None
    event_hash: str | None = None


class DetectorEvidence(_HeartwoodRecord):
    """Visible evidence behind a detector proposal."""

    schema_version: Literal["heartwood.detector-evidence.v1"] = "heartwood.detector-evidence.v1"
    detection_id: str = Field(min_length=1)
    detector_kind: Literal["platform", "dataset", "skill"]
    candidate_id: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: tuple[str, ...] = Field(min_length=1)


class SkillMetadata(_HeartwoodRecord):
    """Validated namespaced ``heartwood.*`` metadata from a ``SKILL.md`` file."""

    schema_version: Literal["heartwood.skill-metadata.v1"] = "heartwood.skill-metadata.v1"
    dataset_types: tuple[str, ...] = Field(alias="heartwood.dataset-types", min_length=1)
    platforms: tuple[str, ...] = Field(alias="heartwood.platforms", min_length=1)
    phi_risk: Literal["none", "reads-phi", "writes-outside-boundary"] = Field(
        alias="heartwood.phi-risk"
    )
    trust_tier: Literal["verified", "community", "experimental"] = Field(
        alias="heartwood.trust-tier"
    )
    requires_network: bool = Field(alias="heartwood.requires-network")
    version: str = Field(alias="heartwood.version", min_length=1, pattern=r"^\d+\.\d+\.\d+$")
    signature: str | None = Field(default=None, alias="heartwood.sig")

    @field_validator("dataset_types", "platforms", mode="before")
    @classmethod
    def _split_comma_separated_values(cls, value: object) -> object:
        """Accept SKILL.md-style comma-separated strings as tuple values."""
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("requires_network", mode="before")
    @classmethod
    def _parse_bool_string(cls, value: object) -> object:
        """Accept YAML string booleans from skill metadata."""
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
        return value

    @model_validator(mode="after")
    def _verified_skills_require_signature(self) -> SkillMetadata:
        """Require provenance for verified skills."""
        if self.trust_tier == "verified" and not self.signature:
            msg = "verified skills require heartwood.sig provenance"
            raise ValueError(msg)
        return self


class ConfirmationRequest(_HeartwoodRecord):
    """Human-in-the-loop confirmation request for a proposed tool call."""

    schema_version: Literal["heartwood.confirmation-request.v1"] = (
        "heartwood.confirmation-request.v1"
    )
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    risk: Literal["low", "medium", "high", "unknown"]
    summary: str = Field(min_length=1)


class ApprovalRecord(_HeartwoodRecord):
    """Human approval or denial record for a proposed action."""

    schema_version: Literal["heartwood.approval-record.v1"] = "heartwood.approval-record.v1"
    approval_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    target_type: Literal["skill", "egress", "model-call", "tool-call"]
    target_id: str = Field(min_length=1)
    decision: Literal["approved", "denied"]
    actor_id: str = Field(min_length=1)
    occurred_at: str = Field(min_length=1)
    reason: str | None = None


_SCHEMA_MODELS: Mapping[str, type[_HeartwoodRecord]] = {
    "approval-record.v1": ApprovalRecord,
    "audit-event.v1": AuditEvent,
    "confirmation-request.v1": ConfirmationRequest,
    "detector-evidence.v1": DetectorEvidence,
    "egress-attestation-record.v1": EgressAttestationRecord,
    "model-call-decision.v1": ModelCallDecision,
    "policy-profile.v1": PolicyProfile,
    "skill-metadata.v1": SkillMetadata,
}


def schema_names() -> tuple[str, ...]:
    """Return the stable names of all exported schema versions."""
    return tuple(sorted(_SCHEMA_MODELS))


def schema_for(name: str) -> dict[str, Any]:
    """Return the JSON Schema for a named Heartwood schema version."""
    key = name.removeprefix("heartwood.")
    model = _SCHEMA_MODELS[key]
    return deepcopy(model.model_json_schema())
