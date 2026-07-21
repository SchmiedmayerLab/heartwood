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
    assert_data_source_adapter_conforms,
    assert_platform_adapter_conforms,
    assert_registry_adapter_conforms,
)
from heartwood.adapters.data import DataSourceBoundaryError, LocalFilesystemDataSourceAdapter
from heartwood.adapters.platform import (
    CarinaPlatformAdapter,
    GenericPlatformAdapter,
    TerraPlatformAdapter,
    select_platform_adapter,
)
from heartwood.adapters.registry import LocalRegistryAdapter, RegistryBoundaryError


def test_generic_platform_adapter_conforms() -> None:
    adapter = GenericPlatformAdapter()
    assert_platform_adapter_conforms(adapter)
    capabilities = adapter.capabilities()
    policy = adapter.default_policy_profile()
    assert capabilities.interfaces == ("terminal", "web", "notebook")
    assert capabilities.browser_route == "direct"
    assert capabilities.model_sources == ("heartwood", "openai", "anthropic", "custom")
    assert policy.credential_allowlist == ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    assert "https://api.openai.com/v1/chat/completions" in policy.allowed_model_endpoints
    assert "https://api.anthropic.com/v1/models" in policy.allowed_model_catalog_endpoints


def test_carina_platform_adapter_conforms_and_defaults_to_local_only() -> None:
    adapter = CarinaPlatformAdapter()
    assert_platform_adapter_conforms(adapter)
    detection = adapter.detect({"HEARTWOOD_PLATFORM": "carina"})
    policy = adapter.default_policy_profile()
    capabilities = adapter.capabilities()
    assert detection.adapter_id == "carina"
    assert detection.confidence > 0.0
    assert adapter.data_mounts() == ()
    assert policy.platform_id == "carina"
    assert policy.allowed_action_confirmation_modes == ("always-confirm", "confirm-risky")
    assert policy.credential_allowlist == ()
    assert capabilities.interfaces == ("terminal",)
    assert capabilities.scheduler == "slurm"
    assert capabilities.model_sources == ("heartwood", "stanford-ai-api-gateway")
    assert capabilities.validation_level == "ci"


def test_terra_platform_adapter_conforms_and_uses_provisioned_compute() -> None:
    adapter = TerraPlatformAdapter()
    assert_platform_adapter_conforms(adapter)
    detection = adapter.detect({"GOOGLE_PROJECT": "synthetic-project"})
    policy = adapter.default_policy_profile()
    capabilities = adapter.capabilities()
    assert detection.adapter_id == "terra"
    assert detection.confidence > 0.0
    assert policy.platform_id == "terra"
    assert policy.policy_id == "terra-default"
    assert policy.credential_allowlist == ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    assert "https://api.openai.com/v1/chat/completions" in policy.allowed_model_endpoints
    assert "https://api.anthropic.com/v1/models" in policy.allowed_model_catalog_endpoints
    assert capabilities.interfaces == ("terminal", "notebook")
    assert capabilities.browser_route == "unavailable"
    assert capabilities.model_sources == ("heartwood", "openai", "anthropic", "custom")
    assert capabilities.validation_level == "ci"
    assert select_platform_adapter({"GOOGLE_PROJECT": "synthetic-project"}).adapter_id == "terra"


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
