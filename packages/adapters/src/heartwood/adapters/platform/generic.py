# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Generic platform adapter for local synthetic development."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from heartwood.adapters import AdapterDetection
from heartwood.detector import Platform, detect_platform
from heartwood.schemas import PolicyProfile


class GenericPlatformAdapter:
    """Platform adapter for generic local execution."""

    @property
    def adapter_id(self) -> str:
        """Return the stable platform adapter id."""
        return "generic"

    def detect(self, env: Mapping[str, str]) -> AdapterDetection:
        """Detect whether the environment is the generic fallback."""
        detection = detect_platform(env)
        if detection.platform is Platform.GENERIC:
            return AdapterDetection(
                adapter_id=self.adapter_id,
                confidence=detection.confidence,
                evidence=detection.evidence,
            )
        return AdapterDetection(
            adapter_id=self.adapter_id,
            confidence=0.0,
            evidence=(
                f"detected managed platform candidate {detection.platform.value}",
                *detection.evidence,
            ),
        )

    def data_mounts(self) -> tuple[Path, ...]:
        """Return synthetic fixture mounts visible to the generic adapter."""
        return (Path("fixtures/synthetic"),)

    def credential_allowlist(self) -> tuple[str, ...]:
        """Return the generic credential allowlist."""
        return ()

    def default_policy_profile(self) -> PolicyProfile:
        """Return the default deny-egress policy for generic local execution."""
        return PolicyProfile(
            policy_id="generic-default",
            platform_id=self.adapter_id,
            deny_egress_by_default=True,
            allowed_model_endpoints=(
                "https://model.local.invalid/v1/chat",
                "http://127.0.0.1:8765/v1/chat",
            ),
            credential_allowlist=self.credential_allowlist(),
            notes="Generic local policy for synthetic development and replay.",
        )
