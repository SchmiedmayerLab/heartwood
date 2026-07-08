# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deny-by-default model-call policy evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypeAlias, cast
from urllib.parse import urlsplit

from heartwood.schemas import EgressAttestationRecord, ModelCallDecision, PolicyProfile

CapabilityTier: TypeAlias = Literal["autonomous", "supervised", "experimental"]

_VALID_CAPABILITY_TIERS = {"autonomous", "supervised", "experimental"}


class PolicyInputError(ValueError):
    """Raised when a policy request cannot be represented safely."""


def normalize_endpoint(endpoint: str) -> str:
    """Normalize an endpoint for exact allowlist comparison."""
    parsed = urlsplit(endpoint)
    if not parsed.scheme:
        msg = "endpoint must include a scheme"
        raise PolicyInputError(msg)
    if parsed.fragment:
        msg = "endpoint fragments are not allowed"
        raise PolicyInputError(msg)
    if parsed.username or parsed.password:
        msg = "endpoint userinfo is not allowed"
        raise PolicyInputError(msg)
    if parsed.query:
        msg = "endpoint queries are not allowed"
        raise PolicyInputError(msg)
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower() if parsed.hostname else ""
    if scheme in {"http", "https"}:
        if not host:
            msg = "HTTP endpoints must include a host"
            raise PolicyInputError(msg)
        try:
            parsed_port = parsed.port
        except ValueError as error:
            raise PolicyInputError(str(error)) from error
        port = f":{parsed_port}" if parsed_port is not None else ""
        path = parsed.path or "/"
        return f"{scheme}://{host}{port}{path}"
    if scheme == "local":
        if not parsed.netloc:
            msg = "local endpoints must include an authority"
            raise PolicyInputError(msg)
        path = parsed.path or "/"
        return f"{scheme}://{parsed.netloc.lower()}{path}"
    msg = f"unsupported endpoint scheme: {scheme}"
    raise PolicyInputError(msg)


def filter_credentials(
    env: Mapping[str, str],
    credential_allowlist: tuple[str, ...],
) -> dict[str, str]:
    """Return only sanctioned credential environment variables."""
    allowed = set(credential_allowlist)
    return {key: value for key, value in env.items() if key in allowed}


class ModelPolicyEngine:
    """Evaluate proposed model calls under a policy profile."""

    def __init__(
        self,
        profile: PolicyProfile,
        *,
        allowed_capability_tiers: tuple[str, ...] = ("autonomous", "supervised"),
    ) -> None:
        self.profile = profile
        self.allowed_capability_tiers = allowed_capability_tiers
        self._normalized_allowed_endpoints = tuple(
            normalize_endpoint(allowed) for allowed in self.profile.allowed_model_endpoints
        )

    def evaluate(
        self,
        *,
        endpoint: str,
        capability_tier: str,
        decision_id: str,
        purpose: str,
    ) -> ModelCallDecision:
        """Evaluate a proposed model call and return an auditable decision."""
        if capability_tier not in _VALID_CAPABILITY_TIERS:
            msg = f"unsupported capability tier: {capability_tier}"
            raise PolicyInputError(msg)
        checked_capability_tier = cast(CapabilityTier, capability_tier)
        try:
            normalized_endpoint = normalize_endpoint(endpoint)
        except PolicyInputError as error:
            return ModelCallDecision(
                decision_id=decision_id,
                policy_profile_id=self.profile.policy_id,
                endpoint="[invalid-endpoint]",
                capability_tier=checked_capability_tier,
                decision="deny",
                reason=str(error),
            )

        if capability_tier not in self.allowed_capability_tiers:
            return ModelCallDecision(
                decision_id=decision_id,
                policy_profile_id=self.profile.policy_id,
                endpoint=normalized_endpoint,
                capability_tier=checked_capability_tier,
                decision="deny",
                reason=f"capability tier {capability_tier} is not allowed for {purpose}",
            )

        if (
            self.profile.deny_egress_by_default
            and normalized_endpoint not in self._normalized_allowed_endpoints
        ):
            return ModelCallDecision(
                decision_id=decision_id,
                policy_profile_id=self.profile.policy_id,
                endpoint=normalized_endpoint,
                capability_tier=checked_capability_tier,
                decision="deny",
                reason=f"endpoint is not allowlisted for {purpose}",
            )

        return ModelCallDecision(
            decision_id=decision_id,
            policy_profile_id=self.profile.policy_id,
            endpoint=normalized_endpoint,
            capability_tier=checked_capability_tier,
            decision="allow",
            reason=f"endpoint and capability tier are allowed for {purpose}",
        )

    def attestation(
        self,
        *,
        decision: ModelCallDecision,
        record_id: str,
        session_id: str,
        occurred_at: str,
    ) -> EgressAttestationRecord:
        """Build an egress attestation record for a policy decision."""
        return EgressAttestationRecord(
            record_id=record_id,
            session_id=session_id,
            decision_id=decision.decision_id,
            policy_profile_id=decision.policy_profile_id,
            endpoint=decision.endpoint,
            decision=decision.decision,
            occurred_at=occurred_at,
            reason=decision.reason,
        )
