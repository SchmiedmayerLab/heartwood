# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from heartwood_skill_catalog import CatalogEntry, build_catalog

from heartwood.gateway import SkillManager, SkillSettingsError
from heartwood.skills import (
    SkillArtifactStore,
    SkillCatalogError,
    SkillCatalogSnapshot,
    SkillSourceProfile,
    SkillSourceRegistry,
    SkillStoreError,
)

_SOURCE_REVISION = "feae115add2e858937c5093b4c519355231444e0"


@dataclass(slots=True)
class _CatalogClient:
    snapshot: SkillCatalogSnapshot
    archive: Path
    failure: Exception | None = None

    def refresh(self) -> SkillCatalogSnapshot:
        if self.failure is not None:
            raise self.failure
        return self.snapshot

    def download(self, entry: CatalogEntry) -> Path:
        if self.failure is not None:
            raise self.failure
        assert entry == self.snapshot.entries[0]
        return self.archive


def test_manager_lists_repository_reviewed_bundled_skills(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    summaries = manager.summaries()

    assert {summary.name for summary in summaries} == {
        "aggregate-export",
        "baseline-model",
        "omop-cohort-summary",
    }
    assert all(summary.source == "bundled" for summary in summaries)
    assert all(summary.review == "repository-reviewed" for summary in summaries)
    assert all(summary.status == "active" for summary in summaries)


def test_manager_refreshes_installs_and_audits_exact_catalog_revision(tmp_path: Path) -> None:
    entry, archive = _catalog_skill(tmp_path)
    client = _CatalogClient(_snapshot(entry), archive)
    manager = _manager(tmp_path, client=client)

    refreshed = manager.refresh()
    inspected = manager.inspect_catalog(entry.name)
    installed = manager.install_catalog(
        entry.name,
        source_id=None,
        expected_tree_sha256=inspected.tree_sha256,
        approved=True,
        actor_id="researcher",
    )

    assert any(summary.name == entry.name and summary.installable for summary in refreshed)
    assert installed.status == "active"
    assert installed.source == "installed"
    assert installed.source_revision == _SOURCE_REVISION
    assert manager.active_skill_roots() == (
        manager.store.artifact_path(manager.store.records()[0]).parent,
    )
    audit = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["event"] for record in audit] == ["installation-decision", "activated"]
    assert audit[0]["decision"] == "approved"
    assert audit[0]["actor_id"] == "researcher"
    assert audit[0]["tree_sha256"] == entry.tree_sha256
    assert audit[0]["source_revision"] == _SOURCE_REVISION
    assert all("path" not in record for record in audit)

    repeated = manager.install_catalog(
        entry.name,
        source_id=None,
        expected_tree_sha256=entry.tree_sha256,
        approved=True,
    )
    assert repeated == installed


def test_manager_rejects_unreviewed_digest_and_signed_revocation(tmp_path: Path) -> None:
    entry, archive = _catalog_skill(tmp_path)
    client = _CatalogClient(_snapshot(entry), archive)
    manager = _manager(tmp_path, client=client)

    with pytest.raises(SkillSettingsError, match="changed after review"):
        manager.install_catalog(
            entry.name,
            source_id="official",
            expected_tree_sha256="0" * 64,
            approved=True,
        )

    manager.install_catalog(
        entry.name,
        source_id="official",
        expected_tree_sha256=entry.tree_sha256,
        approved=True,
    )
    client.snapshot = _snapshot(
        entry.model_copy(update={"revoked": True, "revocation_reason": "Security review"})
    )
    summaries = manager.refresh()

    revoked = next(summary for summary in summaries if summary.name == entry.name)
    assert revoked.status == "revoked"
    assert revoked.revocation_reason == "Security review"
    assert manager.active_skill_roots() == ()


def test_manager_installs_local_skill_without_repository_or_controlled_data_claims(
    tmp_path: Path,
) -> None:
    source = _local_skill(tmp_path)
    manager = _manager(tmp_path)

    inspected = manager.inspect_local(source)
    installed = manager.install_local(
        source,
        expected_tree_sha256=inspected.tree_sha256,
        approved=True,
    )

    assert installed.review == "local-unreviewed"
    assert not installed.controlled_data_ready
    assert installed.source_id == "local"
    artifact = manager.store.artifact_path(manager.store.records()[0])
    (artifact / "SKILL.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(SkillSettingsError, match="invalid"):
        manager.summaries()


def test_manager_requires_explicit_source_when_multiple_are_configured(tmp_path: Path) -> None:
    entry, archive = _catalog_skill(tmp_path)
    registry = SkillSourceRegistry(
        sources=(_profile(tmp_path, "official"), _profile(tmp_path, "institution"))
    )
    manager = SkillManager(
        bundled_dir=_skills_root(),
        store=SkillArtifactStore(tmp_path / "store"),
        source_registry=registry,
        cache_dir=tmp_path / "cache",
        audit_path=tmp_path / "audit.jsonl",
        platform_id="generic",
        client_factory=lambda profile, _cache: _CatalogClient(
            SkillCatalogSnapshot(
                source_id=profile.source_id,
                offline=True,
                entries=(entry,),
            ),
            archive,
        ),
    )

    with pytest.raises(SkillSettingsError, match="Choose a Skill source"):
        manager.inspect_catalog(entry.name)
    assert manager.inspect_catalog(entry.name, source_id="institution").source_id == "institution"


def test_manager_fails_closed_when_source_refresh_fails(tmp_path: Path) -> None:
    entry, archive = _catalog_skill(tmp_path)
    client = _CatalogClient(
        _snapshot(entry),
        archive,
        failure=SkillCatalogError("signed metadata expired"),
    )
    manager = _manager(tmp_path, client=client)

    with pytest.raises(SkillSettingsError, match="signed metadata expired"):
        manager.refresh()
    assert all(summary.source == "bundled" for summary in manager.summaries())


def test_manager_reverifies_signed_source_before_runtime_activation(tmp_path: Path) -> None:
    entry, archive = _catalog_skill(tmp_path)
    client = _CatalogClient(_snapshot(entry), archive)
    manager = _manager(tmp_path, client=client)
    manager.install_catalog(
        entry.name,
        source_id=None,
        expected_tree_sha256=entry.tree_sha256,
        approved=True,
    )

    client.failure = SkillCatalogError("signed metadata expired")
    with pytest.raises(SkillSettingsError, match="signed metadata expired"):
        manager.active_skill_roots()


def test_manager_audits_approved_installation_failure_without_activating(
    tmp_path: Path,
) -> None:
    entry, archive = _catalog_skill(tmp_path)
    archive.write_bytes(archive.read_bytes() + b"tampered")
    manager = _manager(tmp_path, client=_CatalogClient(_snapshot(entry), archive))

    with pytest.raises(SkillSettingsError, match="size does not match"):
        manager.install_catalog(
            entry.name,
            source_id=None,
            expected_tree_sha256=entry.tree_sha256,
            approved=True,
        )
    assert manager.store.records() == ()
    audit = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["event"] for record in audit] == [
        "installation-decision",
        "installation-failed",
    ]
    assert all("path" not in record for record in audit)


def test_manager_enforces_platform_and_deployment_owned_controlled_data_approval(
    tmp_path: Path,
) -> None:
    entry, archive = _catalog_skill(tmp_path)
    approved_registry = SkillSourceRegistry(
        sources=(_profile(tmp_path, "official", approved_digests=(entry.tree_sha256,)),)
    )
    manager = _manager(
        tmp_path,
        registry=approved_registry,
        client=_CatalogClient(_snapshot(entry), archive),
    )

    inspected = manager.inspect_catalog(entry.name)
    assert inspected.controlled_data_ready
    installed = manager.install_catalog(
        entry.name,
        source_id=None,
        expected_tree_sha256=entry.tree_sha256,
        approved=True,
    )
    assert installed.controlled_data_ready

    terra_only = entry.model_copy(
        update={"policy": entry.policy.model_copy(update={"platforms": ("terra",)})}
    )
    unsupported = _manager(
        tmp_path / "unsupported",
        client=_CatalogClient(_snapshot(terra_only), archive),
        platform_id="carina",
    )
    summary = unsupported.inspect_catalog(entry.name)
    assert summary.status == "unsupported"
    assert not summary.installable
    with pytest.raises(SkillSettingsError, match="Not supported on the carina platform"):
        unsupported.install_catalog(
            entry.name,
            source_id=None,
            expected_tree_sha256=entry.tree_sha256,
            approved=True,
        )


def test_manager_rejects_unknown_mismatched_missing_and_unapproved_catalog_entries(
    tmp_path: Path,
) -> None:
    entry, archive = _catalog_skill(tmp_path)
    client = _CatalogClient(_snapshot(entry), archive)
    manager = _manager(tmp_path, client=client)

    with pytest.raises(SkillSettingsError, match="not configured"):
        manager.refresh("missing")
    with pytest.raises(SkillSettingsError, match="not configured"):
        manager.inspect_catalog(entry.name, source_id="missing")
    with pytest.raises(SkillSettingsError, match="not available"):
        manager.inspect_catalog("missing")
    with pytest.raises(SkillSettingsError, match="not available"):
        manager.install_catalog(
            "missing",
            source_id=None,
            expected_tree_sha256=entry.tree_sha256,
            approved=True,
        )
    with pytest.raises(SkillSettingsError, match="explicit approval"):
        manager.install_catalog(
            entry.name,
            source_id=None,
            expected_tree_sha256=entry.tree_sha256,
            approved=False,
        )

    client.snapshot = SkillCatalogSnapshot(
        source_id="different-source",
        offline=True,
        entries=(entry,),
    )
    with pytest.raises(SkillSettingsError, match="different source identifier"):
        manager.refresh()

    no_sources = _manager(tmp_path / "no-sources")
    with pytest.raises(SkillSettingsError, match="No signed Skill source"):
        no_sources.inspect_catalog(entry.name)


def test_manager_rejects_revoked_and_unapproved_local_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry, archive = _catalog_skill(tmp_path)
    revoked = entry.model_copy(update={"revoked": True, "revocation_reason": "Security review"})
    catalog_manager = _manager(
        tmp_path / "catalog",
        client=_CatalogClient(_snapshot(revoked), archive),
    )
    with pytest.raises(SkillSettingsError, match="has been revoked"):
        catalog_manager.install_catalog(
            entry.name,
            source_id=None,
            expected_tree_sha256=entry.tree_sha256,
            approved=True,
        )

    local_manager = _manager(tmp_path / "local")
    source = _local_skill(tmp_path / "local-source")
    summary = local_manager.inspect_local(source)
    with pytest.raises(SkillSettingsError, match="changed after review"):
        local_manager.install_local(
            source,
            expected_tree_sha256="0" * 64,
            approved=True,
        )
    with pytest.raises(SkillSettingsError, match="explicit approval"):
        local_manager.install_local(
            source,
            expected_tree_sha256=summary.tree_sha256,
            approved=False,
        )
    with pytest.raises(SkillSettingsError, match=r"missing SKILL\.md"):
        local_manager.inspect_local(tmp_path / "missing")

    def fail_install(_source: Path) -> None:
        raise SkillStoreError("synthetic copy failure")

    monkeypatch.setattr(local_manager.store, "install_local", fail_install)
    with pytest.raises(SkillSettingsError, match="synthetic copy failure"):
        local_manager.install_local(
            source,
            expected_tree_sha256=summary.tree_sha256,
            approved=True,
        )


def test_manager_fails_closed_for_missing_source_and_corrupt_installed_state(
    tmp_path: Path,
) -> None:
    entry, archive = _catalog_skill(tmp_path)
    client = _CatalogClient(_snapshot(entry), archive)
    manager = _manager(tmp_path, client=client)
    manager.install_catalog(
        entry.name,
        source_id=None,
        expected_tree_sha256=entry.tree_sha256,
        approved=True,
    )

    manager_without_source = SkillManager(
        bundled_dir=_skills_root(),
        store=manager.store,
        source_registry=SkillSourceRegistry(),
        cache_dir=tmp_path / "unconfigured-cache",
        audit_path=tmp_path / "unconfigured-audit.jsonl",
        platform_id="generic",
    )
    with pytest.raises(SkillSettingsError, match="no longer configured"):
        manager_without_source.active_skill_roots()

    manager.store.index_path.write_text("{", encoding="utf-8")
    with pytest.raises(SkillSettingsError, match="index is invalid"):
        manager_without_source.active_skill_roots()


def test_manager_skips_non_skill_entries_and_rejects_invalid_bundled_skills(
    tmp_path: Path,
) -> None:
    missing = SkillManager(
        bundled_dir=tmp_path / "missing",
        store=SkillArtifactStore(tmp_path / "missing-store"),
        source_registry=SkillSourceRegistry(),
        cache_dir=tmp_path / "missing-cache",
        audit_path=tmp_path / "missing-audit.jsonl",
        platform_id="generic",
    )
    assert missing.summaries() == ()

    bundled = tmp_path / "bundled"
    shutil.copytree(_skills_root(), bundled)
    (bundled / "README.txt").write_text("not a Skill\n", encoding="utf-8")
    (bundled / "aggregate-export" / "SKILL.md").write_text("invalid\n", encoding="utf-8")
    invalid = SkillManager(
        bundled_dir=bundled,
        store=SkillArtifactStore(tmp_path / "invalid-store"),
        source_registry=SkillSourceRegistry(),
        cache_dir=tmp_path / "invalid-cache",
        audit_path=tmp_path / "invalid-audit.jsonl",
        platform_id="generic",
    )
    with pytest.raises(SkillSettingsError, match="Invalid bundled Skill"):
        invalid.summaries()


def _manager(
    tmp_path: Path,
    *,
    registry: SkillSourceRegistry | None = None,
    client: _CatalogClient | None = None,
    platform_id: str = "generic",
) -> SkillManager:
    selected_registry = registry or (
        SkillSourceRegistry(sources=(_profile(tmp_path, "official"),))
        if client is not None
        else SkillSourceRegistry()
    )
    return SkillManager(
        bundled_dir=_skills_root(),
        store=SkillArtifactStore(tmp_path / "store"),
        source_registry=selected_registry,
        cache_dir=tmp_path / "cache",
        audit_path=tmp_path / "audit.jsonl",
        platform_id=platform_id,
        client_factory=(None if client is None else lambda _profile, _cache: client),
    )


def _profile(
    tmp_path: Path,
    source_id: str,
    *,
    approved_digests: tuple[str, ...] = (),
) -> SkillSourceProfile:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / f"{source_id}.root.json"
    root.write_text("{}\n", encoding="utf-8")
    repository = tmp_path / f"{source_id}-repository"
    repository.mkdir(exist_ok=True)
    return SkillSourceProfile.model_validate(
        {
            "id": source_id,
            "kind": "offline",
            "trusted-root": root,
            "repository": repository,
            "controlled-data-approved-digests": list(approved_digests),
        }
    )


def _catalog_skill(tmp_path: Path) -> tuple[CatalogEntry, Path]:
    source = _local_skill(tmp_path)
    output = tmp_path / "targets"
    document = build_catalog(
        source.parent,
        output,
        source_repository="https://github.com/SchmiedmayerLab/heartwood-skills",
        source_revision=_SOURCE_REVISION,
    )
    entry = document.entries[0]
    return entry, output / entry.target


def _snapshot(entry: CatalogEntry) -> SkillCatalogSnapshot:
    return SkillCatalogSnapshot(source_id="official", offline=True, entries=(entry,))


def _local_skill(tmp_path: Path) -> Path:
    source = tmp_path / "sources" / "community-summary"
    shutil.copytree(_skills_root() / "aggregate-export", source)
    skill_file = source / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8")
        .replace("name: aggregate-export", "name: community-summary")
        .replace(
            'heartwood.id: "heartwood.research.aggregate-export"',
            'heartwood.id: "example.community-summary"',
        ),
        encoding="utf-8",
    )
    return source


def _skills_root() -> Path:
    return _repo_root() / "vendor" / "heartwood-skills" / "skills" / "verified"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
