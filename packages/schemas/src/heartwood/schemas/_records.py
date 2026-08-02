# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Versioned Pydantic record schemas for Heartwood contracts."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

_SEMVER_PATTERN = (
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_SAFE_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"

__all__ = [
    "ActionConfirmationMode",
    "ApprovalRecord",
    "AuditCheckpoint",
    "AuditCheckpointStatement",
    "AuditEvent",
    "AuditRetention",
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

type CapabilityTier = Literal["autonomous", "supervised", "experimental"]
type ActionConfirmationMode = Literal["always-confirm", "confirm-risky"]
type Decision = Literal["allow", "deny"]


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
    allowed_model_catalog_endpoints: tuple[str, ...] = ()
    allowed_capability_tiers: tuple[CapabilityTier, ...] = Field(
        default=("supervised",),
        min_length=1,
    )
    allowed_action_confirmation_modes: tuple[ActionConfirmationMode, ...] = Field(
        default=("always-confirm",),
        min_length=1,
    )
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
    """Record an application-layer model-route decision for evidence export."""

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


class AuditRetention(_HeartwoodRecord):
    """Deployment retention declaration bound into an audit checkpoint."""

    schema_version: Literal["heartwood.audit-retention.v1"] = "heartwood.audit-retention.v1"
    policy_id: str = Field(pattern=_SAFE_IDENTIFIER_PATTERN)
    retain_until: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("retain_until")
    @classmethod
    def _validate_retention_date(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("retain_until must be a valid ISO 8601 date") from error
        return value


class AuditCheckpointStatement(_HeartwoodRecord):
    """Canonical audit identity and retention statement signed by a deployment."""

    schema_version: Literal["heartwood.audit-checkpoint-statement.v1"] = (
        "heartwood.audit-checkpoint-statement.v1"
    )
    deployment_id: str = Field(pattern=_SAFE_IDENTIFIER_PATTERN)
    session_id: str = Field(pattern=_SAFE_IDENTIFIER_PATTERN)
    created_at: str = Field(min_length=1)
    audit_filename: Literal["audit.jsonl"] = "audit.jsonl"
    audit_schema_version: Literal["heartwood.audit-event.v1"] = "heartwood.audit-event.v1"
    audit_event_count: int = Field(ge=0)
    terminal_event_hash: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    audit_content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    audit_size_bytes: int = Field(ge=0)
    retention: AuditRetention

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("created_at must be a valid ISO 8601 timestamp") from error
        if parsed.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return value

    @model_validator(mode="after")
    def _validate_terminal_hash(self) -> AuditCheckpointStatement:
        if (self.audit_event_count == 0) != (self.terminal_event_hash is None):
            raise ValueError("terminal_event_hash must match the audit event count")
        created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        if date.fromisoformat(self.retention.retain_until) < created.date():
            raise ValueError("retain_until cannot be earlier than created_at")
        return self


class AuditCheckpoint(_HeartwoodRecord):
    """Signed deployment checkpoint for one authoritative audit export."""

    schema_version: Literal["heartwood.audit-checkpoint.v1"] = "heartwood.audit-checkpoint.v1"
    statement: AuditCheckpointStatement
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signing_key_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    signature: str = Field(min_length=1)

    @field_validator("signature")
    @classmethod
    def _validate_signature(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except ValueError as error:
            raise ValueError("signature must be canonical Base64") from error
        if len(decoded) != 64 or base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("signature must be a canonical Ed25519 signature")
        return value


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
    version: str = Field(alias="heartwood.version", min_length=1, pattern=_SEMVER_PATTERN)
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

    @field_validator("version")
    @classmethod
    def _reject_leading_zero_prerelease_identifiers(cls, value: str) -> str:
        """Enforce the numeric prerelease rule from Semantic Versioning."""
        prerelease = value.split("+", maxsplit=1)[0].partition("-")[2]
        if prerelease and any(
            identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")
            for identifier in prerelease.split(".")
        ):
            msg = "numeric Semantic Versioning prerelease identifiers cannot have leading zeroes"
            raise ValueError(msg)
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
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


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
    "audit-checkpoint-statement.v1": AuditCheckpointStatement,
    "audit-checkpoint.v1": AuditCheckpoint,
    "audit-event.v1": AuditEvent,
    "audit-retention.v1": AuditRetention,
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
