# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Conformance tests for deterministic fake adapter implementations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from heartwood.adapters import (
    AdapterDetection,
    DatasetFingerprint,
    ModelCallRequest,
    RegistryVerification,
    SkillReference,
    assert_data_source_adapter_conforms,
    assert_model_provider_adapter_conforms,
    assert_platform_adapter_conforms,
    assert_registry_adapter_conforms,
)
from heartwood.model_policy import normalize_endpoint
from heartwood.schemas import JsonValue, ModelCallDecision, PolicyProfile


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

    def data_mounts(self) -> tuple[Path, ...]:
        """Return a synthetic data mount."""
        return (Path("/workspace/fixtures"),)

    def credential_allowlist(self) -> tuple[str, ...]:
        """Return the empty generic credential allowlist."""
        return ()

    def default_policy_profile(self) -> PolicyProfile:
        """Return a deny-egress default policy."""
        return PolicyProfile(policy_id="generic-default", platform_id=self.adapter_id)


class FakeModelProviderAdapter:
    """Deterministic model-provider adapter used by conformance tests."""

    @property
    def provider_id(self) -> str:
        """Return the fake provider id."""
        return "fake-local"

    @property
    def capability_tier(self) -> str:
        """Return the fake provider capability tier."""
        return "supervised"

    def evaluate_model_call(self, request: ModelCallRequest) -> ModelCallDecision:
        """Deny the synthetic request by default."""
        return ModelCallDecision(
            decision_id="decision-1",
            policy_profile_id="generic-default",
            endpoint=normalize_endpoint(request.endpoint),
            capability_tier="supervised",
            decision="deny",
            reason="synthetic provider denies egress by default",
        )


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


class FakeRegistryAdapter:
    """Deterministic registry adapter used by conformance tests."""

    @property
    def registry_id(self) -> str:
        """Return the fake registry id."""
        return "local-fixture"

    def resolve_skill(self, skill_id: str, version: str) -> SkillReference:
        """Resolve a skill to a synthetic source path."""
        return SkillReference(
            skill_id=skill_id,
            version=version,
            source="fixtures/synthetic/skills/omop-cohort-summary",
        )

    def verify_skill(self, reference: SkillReference) -> RegistryVerification:
        """Verify the synthetic skill reference."""
        return RegistryVerification(
            verified=reference.version == "0.1.0",
            reason="synthetic fixture registry result",
        )


def test_platform_adapter_conformance() -> None:
    assert_platform_adapter_conforms(FakePlatformAdapter())


def test_model_provider_adapter_conformance() -> None:
    assert_model_provider_adapter_conforms(FakeModelProviderAdapter())


def test_data_source_adapter_conformance() -> None:
    assert_data_source_adapter_conforms(FakeDataSourceAdapter())


def test_registry_adapter_conformance() -> None:
    assert_registry_adapter_conforms(FakeRegistryAdapter())
