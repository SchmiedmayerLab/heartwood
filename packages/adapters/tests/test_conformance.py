# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Conformance tests for deterministic fake adapter implementations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from heartwood.adapters import (
    AdapterDetection,
    DatasetFingerprint,
    PlatformCapabilities,
    assert_data_source_adapter_conforms,
    assert_platform_adapter_conforms,
)
from heartwood.schemas import JsonValue, PolicyProfile


class FakePlatformAdapter:
    """Deterministic platform adapter used by conformance tests."""

    @property
    def adapter_id(self) -> str:
        """Return the fake adapter id."""
        return "generic"

    def detect(self, env: Mapping[str, str]) -> AdapterDetection:
        """Return a generic proposal for synthetic environments."""
        evidence = ("synthetic env mapping inspected",) if env else ("no markers required",)
        return AdapterDetection(adapter_id=self.adapter_id, confidence=1.0, evidence=evidence)

    def capabilities(self) -> PlatformCapabilities:
        """Return deterministic synthetic capabilities."""
        return PlatformCapabilities(
            platform_id=self.adapter_id,
            display_name="Synthetic platform",
            interfaces=("terminal",),
            browser_route="unavailable",
            ingress_modes=("direct-loopback",),
            default_ingress_mode="direct-loopback",
            managed_runtimes=(),
            scheduler="none",
            persistent_storage="Synthetic project storage",
            credential_backends=("process",),
            model_sources=("heartwood", "openai"),
            platform_isolated_model_sources=(),
            managed_model_connections=(),
            validation_level="ci",
        )

    def data_mounts(self) -> tuple[Path, ...]:
        """Return a synthetic data mount."""
        return (Path("/workspace/fixtures"),)

    def credential_allowlist(self) -> tuple[str, ...]:
        """Return the empty generic credential allowlist."""
        return ()

    def default_policy_profile(self) -> PolicyProfile:
        """Return a deny-egress default policy."""
        return PolicyProfile(policy_id="generic-default", platform_id=self.adapter_id)


class FakeDataSourceAdapter:
    """Deterministic data-source adapter used by conformance tests."""

    @property
    def source_id(self) -> str:
        """Return the fake data-source id."""
        return "synthetic-omop"

    def fingerprint(self) -> DatasetFingerprint:
        """Return an OMOP-like synthetic fingerprint."""
        return DatasetFingerprint(
            dataset_type="omop-cdm",
            confidence=0.95,
            evidence=("found synthetic person table", "found synthetic condition table"),
        )

    def read_table(
        self,
        name: str,
        columns: Sequence[str] | None = None,
        limit: int = 20,
    ) -> Sequence[Mapping[str, JsonValue]]:
        """Return bounded synthetic rows."""
        assert name == "person"
        rows: list[Mapping[str, JsonValue]] = [
            {"person_id": 1, "year_of_birth": 1970},
            {"person_id": 2, "year_of_birth": 1980},
            {"person_id": 3, "year_of_birth": 1990},
        ]
        if columns is not None:
            rows = [{key: row[key] for key in columns if key in row} for row in rows]
        return rows[:limit]


def test_platform_adapter_conformance() -> None:
    assert_platform_adapter_conforms(FakePlatformAdapter())


def test_platform_capabilities_reject_contradictory_security_claims() -> None:
    capabilities = FakePlatformAdapter().capabilities()

    with pytest.raises(ValueError, match="at least one ingress"):
        replace(capabilities, ingress_modes=())
    with pytest.raises(ValueError, match="must be unique"):
        replace(
            capabilities,
            ingress_modes=("direct-loopback", "direct-loopback"),
        )
    with pytest.raises(ValueError, match="default ingress"):
        replace(capabilities, default_ingress_mode="trusted-proxy")
    with pytest.raises(ValueError, match="direct browser routing"):
        replace(
            capabilities,
            browser_route="direct",
            ingress_modes=("trusted-proxy",),
            default_ingress_mode="trusted-proxy",
        )
    with pytest.raises(ValueError, match="Jupyter browser routing"):
        replace(
            capabilities,
            browser_route="jupyter-proxy",
            ingress_modes=("direct-loopback",),
        )
    with pytest.raises(ValueError, match="must be unique"):
        replace(
            capabilities,
            platform_isolated_model_sources=("openai", "openai"),
            validation_level="ci-and-live-synthetic",
        )
    with pytest.raises(ValueError, match="live synthetic validation"):
        replace(
            capabilities,
            platform_isolated_model_sources=("openai",),
        )
    with pytest.raises(ValueError, match="supported by the platform"):
        replace(
            capabilities,
            platform_isolated_model_sources=("anthropic",),
            validation_level="ci-and-live-synthetic",
        )


def test_data_source_adapter_conformance() -> None:
    assert_data_source_adapter_conforms(FakeDataSourceAdapter())
