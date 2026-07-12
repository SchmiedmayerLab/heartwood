# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Stanford Carina platform adapter."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from heartwood.adapters import AdapterDetection
from heartwood.detector import Platform, detect_platform
from heartwood.schemas import PolicyProfile


class CarinaPlatformAdapter:
    """Platform contract for a synthetic-only Carina CLI deployment."""

    @property
    def adapter_id(self) -> str:
        """Return the stable platform adapter id."""
        return "carina"

    def detect(self, env: Mapping[str, str]) -> AdapterDetection:
        """Detect explicit or cluster-provided Carina evidence."""
        detection = detect_platform(env)
        if detection.platform is Platform.CARINA:
            return AdapterDetection(
                adapter_id=self.adapter_id,
                confidence=detection.confidence,
                evidence=detection.evidence,
            )
        return AdapterDetection(
            adapter_id=self.adapter_id,
            confidence=0.0,
            evidence=("Carina platform evidence not found", *detection.evidence),
        )

    def data_mounts(self) -> tuple[Path, ...]:
        """Return no implicit controlled-data mount."""
        return ()

    def credential_allowlist(self) -> tuple[str, ...]:
        """Return credentials permitted by the optional managed route."""
        return ("STANFORD_AI_API_KEY",)

    def default_policy_profile(self) -> PolicyProfile:
        """Return the conservative synthetic Carina policy."""
        return PolicyProfile(
            policy_id="carina-synthetic",
            platform_id=self.adapter_id,
            deny_egress_by_default=True,
            allowed_model_endpoints=("http://127.0.0.1:8765/v1/chat/completions",),
            allowed_model_catalog_endpoints=("http://127.0.0.1:8765/v1/models",),
            allowed_capability_tiers=("supervised", "experimental"),
            allowed_action_confirmation_modes=("always-confirm",),
            credential_allowlist=(),
            notes="Synthetic-only Carina policy with loopback inference and explicit actions.",
        )
