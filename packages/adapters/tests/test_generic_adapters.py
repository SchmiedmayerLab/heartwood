# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for concrete generic/local adapters."""

from __future__ import annotations

from pathlib import Path

import pytest

from heartwood.adapters import (
    ModelCallRequest,
    assert_data_source_adapter_conforms,
    assert_model_provider_adapter_conforms,
    assert_platform_adapter_conforms,
    assert_registry_adapter_conforms,
)
from heartwood.adapters.data import DataSourceBoundaryError, LocalFilesystemDataSourceAdapter
from heartwood.adapters.model import FakeLocalModelProviderAdapter
from heartwood.adapters.platform import GenericPlatformAdapter
from heartwood.adapters.registry import LocalRegistryAdapter, RegistryBoundaryError


def test_generic_platform_adapter_conforms() -> None:
    assert_platform_adapter_conforms(GenericPlatformAdapter())


def test_local_filesystem_data_adapter_conforms() -> None:
    assert_data_source_adapter_conforms(LocalFilesystemDataSourceAdapter.synthetic_omop())


def test_local_filesystem_data_adapter_uses_headers_for_fingerprint() -> None:
    adapter = LocalFilesystemDataSourceAdapter.synthetic_omop()
    fingerprint = adapter.fingerprint()
    assert fingerprint.dataset_type == "omop-cdm"
    assert fingerprint.confidence == 0.95
    assert all("headers" in item for item in fingerprint.evidence)


def test_local_filesystem_data_adapter_handles_missing_fingerprint_tables(
    tmp_path: Path,
) -> None:
    adapter = LocalFilesystemDataSourceAdapter(tmp_path)
    fingerprint = adapter.fingerprint()
    assert fingerprint.confidence == 0.0
    assert fingerprint.evidence == ("no OMOP-like CSV headers detected",)


def test_local_filesystem_data_adapter_blocks_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret.csv").write_text("person_id\n1\n", encoding="utf-8")
    adapter = LocalFilesystemDataSourceAdapter(root)
    with pytest.raises(DataSourceBoundaryError):
        adapter.read_table("../secret")


def test_local_filesystem_data_adapter_blocks_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("person_id\n1\n", encoding="utf-8")
    (root / "person.csv").symlink_to(outside)
    adapter = LocalFilesystemDataSourceAdapter(root)
    with pytest.raises(DataSourceBoundaryError):
        adapter.read_table("person")


def test_fake_local_model_provider_denies_default_conformance_request() -> None:
    policy = GenericPlatformAdapter().default_policy_profile()
    assert_model_provider_adapter_conforms(FakeLocalModelProviderAdapter(policy))


def test_fake_local_model_provider_allows_only_policy_endpoint() -> None:
    policy = GenericPlatformAdapter().default_policy_profile()
    provider = FakeLocalModelProviderAdapter(policy)
    decision = provider.evaluate_model_call(
        ModelCallRequest(
            endpoint="https://model.local.invalid/v1/chat",
            capability_tier="supervised",
            purpose="synthetic model call",
        )
    )
    assert decision.decision == "allow"


def test_local_registry_adapter_conforms() -> None:
    assert_registry_adapter_conforms(LocalRegistryAdapter.synthetic_skills())


def test_local_registry_adapter_blocks_path_escape(tmp_path: Path) -> None:
    registry = LocalRegistryAdapter(tmp_path)
    with pytest.raises(RegistryBoundaryError):
        registry.resolve_skill("heartwood.synthetic.../outside", "0.1.0")


def test_local_registry_adapter_rejects_outside_source(tmp_path: Path) -> None:
    registry = LocalRegistryAdapter(tmp_path / "registry")
    reference = registry.resolve_skill("heartwood.synthetic.missing", "0.1.0")
    outside = reference.__class__(
        skill_id=reference.skill_id,
        version=reference.version,
        source=str(tmp_path / "outside"),
    )
    verification = registry.verify_skill(outside)
    assert verification.verified is False
    assert verification.reason == "local skill source escapes registry root"
