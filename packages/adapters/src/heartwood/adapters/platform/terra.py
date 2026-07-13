# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Minimal Terra interactive-runtime platform adapter."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from heartwood.adapters import AdapterDetection
from heartwood.detector import Platform, detect_platform
from heartwood.schemas import PolicyProfile


class TerraPlatformAdapter:
    """Platform contract for an already-provisioned Terra Jupyter runtime."""

    @property
    def adapter_id(self) -> str:
        """Return the stable platform adapter id."""
        return "terra"

    def detect(self, env: Mapping[str, str]) -> AdapterDetection:
        """Detect Terra from deterministic workspace markers."""
        detection = detect_platform(env)
        if detection.platform is Platform.TERRA:
            return AdapterDetection(self.adapter_id, detection.confidence, detection.evidence)
        return AdapterDetection(
            self.adapter_id,
            0.0,
            ("Terra platform evidence not found", *detection.evidence),
        )

    def data_mounts(self) -> tuple[Path, ...]:
        """Return no implicit controlled-data mount."""
        return ()

    def credential_allowlist(self) -> tuple[str, ...]:
        """Return no implicit provider credential."""
        return ()

    def default_policy_profile(self) -> PolicyProfile:
        """Return the conservative local-runtime Terra policy."""
        return PolicyProfile(
            policy_id="terra-local-default",
            platform_id=self.adapter_id,
            deny_egress_by_default=True,
            allowed_model_endpoints=("http://127.0.0.1:8765/v1/chat/completions",),
            allowed_model_catalog_endpoints=("http://127.0.0.1:8765/v1/models",),
            allowed_capability_tiers=("supervised", "experimental"),
            allowed_action_confirmation_modes=("always-confirm", "confirm-risky"),
            credential_allowlist=(),
            notes="Terra local-runtime policy; deployment controls remain authoritative.",
        )
