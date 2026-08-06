# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Conformance tests for signed Skill catalogs and atomic installation."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from heartwood_skill_catalog import CatalogDocument, build_catalog
from pydantic import ValidationError
from securesystemslib.signer import CryptoSigner  # type: ignore[attr-defined]
from tuf.api.exceptions import DownloadError
from tuf.api.metadata import (  # type: ignore[attr-defined]
    Metadata,
    MetaFile,
    Root,
    Snapshot,
    TargetFile,
    Targets,
    Timestamp,
)

from heartwood.skills import (
    InstalledSkillRecord,
    SkillArtifactStore,
    SkillCatalogClient,
    SkillCatalogError,
    SkillCatalogSnapshot,
    SkillInstallationIndex,
    SkillSourceProfile,
    SkillStoreError,
    configured_skill_source_registry,
    load_skill_source_registry,
)
from heartwood.skills._catalog import _LocalRepositoryFetcher

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_CURATED_ROOT = _REPOSITORY_ROOT / "vendor" / "heartwood-skills"
_SKILLS_ROOT = _CURATED_ROOT / "skills" / "verified"
_SOURCE_REPOSITORY = "https://github.com/SchmiedmayerLab/heartwood-skills"
_SOURCE_REVISION = "feae115add2e858937c5093b4c519355231444e0"


def _catalog_targets(tmp_path: Path) -> Path:
    output = tmp_path / "catalog-targets"
    build_catalog(
        _SKILLS_ROOT,
        output,
        source_repository=_SOURCE_REPOSITORY,
        source_revision=_SOURCE_REVISION,
    )
    return output


def _write_tuf_repository(
    repository: Path,
    source_targets: Path,
    *,
    expires: datetime | None = None,
    signers: dict[str, CryptoSigner] | None = None,
    version: int = 1,
) -> Path:
    metadata_dir = repository / "metadata"
    targets_dir = repository / "targets"
    metadata_dir.mkdir(parents=True)
    targets_dir.mkdir(parents=True)
    expiry = expires or datetime.now(UTC) + timedelta(days=30)
    selected_signers = signers or {
        role: CryptoSigner.generate_ed25519()
        for role in ("root", "targets", "snapshot", "timestamp")
    }

    root = Root(expires=expiry)
    for role, signer in selected_signers.items():
        root.add_key(signer.public_key, role)
    root_metadata = Metadata(root)
    root_metadata.sign(selected_signers["root"])
    root_bytes = root_metadata.to_bytes()
    trusted_root = metadata_dir / "1.root.json"
    trusted_root.write_bytes(root_bytes)

    target_records: dict[str, TargetFile] = {}
    for source in sorted(path for path in source_targets.rglob("*") if path.is_file()):
        relative = source.relative_to(source_targets).as_posix()
        target = TargetFile.from_file(relative, str(source), ["sha256"])
        target_records[relative] = target
        physical = targets_dir / relative
        physical.parent.mkdir(parents=True, exist_ok=True)
        physical = physical.with_name(f"{target.hashes['sha256']}.{physical.name}")
        shutil.copyfile(source, physical)

    targets = Targets(expires=expiry, targets=target_records, version=version)
    targets_metadata = Metadata(targets)
    targets_metadata.sign(selected_signers["targets"])
    targets_bytes = targets_metadata.to_bytes()
    (metadata_dir / f"{version}.targets.json").write_bytes(targets_bytes)

    snapshot = Snapshot(
        expires=expiry,
        meta={"targets.json": MetaFile.from_data(version, targets_bytes, ["sha256"])},
        version=version,
    )
    snapshot_metadata = Metadata(snapshot)
    snapshot_metadata.sign(selected_signers["snapshot"])
    snapshot_bytes = snapshot_metadata.to_bytes()
    (metadata_dir / f"{version}.snapshot.json").write_bytes(snapshot_bytes)

    timestamp = Timestamp(
        expires=expiry,
        snapshot_meta=MetaFile.from_data(version, snapshot_bytes, ["sha256"]),
        version=version,
    )
    timestamp_metadata = Metadata(timestamp)
    timestamp_metadata.sign(selected_signers["timestamp"])
    (metadata_dir / "timestamp.json").write_bytes(timestamp_metadata.to_bytes())
    return trusted_root


def _profile(repository: Path, trusted_root: Path) -> SkillSourceProfile:
    return SkillSourceProfile.model_validate(
        {
            "id": "synthetic-catalog",
            "kind": "offline",
            "trusted-root": trusted_root,
            "repository": repository,
        }
    )


def _client(tmp_path: Path) -> tuple[SkillCatalogClient, SkillCatalogSnapshot, Path]:
    catalog_targets = _catalog_targets(tmp_path)
    repository = tmp_path / "repository"
    trusted_root = _write_tuf_repository(repository, catalog_targets)
    client = SkillCatalogClient(_profile(repository, trusted_root), tmp_path / "cache")
    return client, client.refresh(), repository


def test_offline_tuf_catalog_refreshes_and_downloads_immutable_targets(tmp_path: Path) -> None:
    client, snapshot, _ = _client(tmp_path)
    assert snapshot.source_id == "synthetic-catalog"
    assert snapshot.offline is True
    assert [entry.name for entry in snapshot.entries] == [
        "aggregate-export",
        "baseline-model",
        "omop-cohort-summary",
    ]

    entry = snapshot.entry("aggregate-export")
    archive = client.download(entry)
    assert archive.stat().st_size == entry.archive_size
    assert snapshot.entry("aggregate-export") == entry
    with pytest.raises(SkillCatalogError, match="not available"):
        snapshot.entry("missing")


def test_catalog_target_tampering_and_metadata_expiry_fail_closed(tmp_path: Path) -> None:
    client, snapshot, repository = _client(tmp_path)
    entry = snapshot.entry("aggregate-export")
    physical = next((repository / "targets" / Path(entry.target).parent).glob("*.zip"))
    physical.write_bytes(physical.read_bytes() + b"tampered")
    with pytest.raises(SkillCatalogError, match="Unable to download"):
        client.download(entry)

    expired_targets = _catalog_targets(tmp_path / "expired")
    expired_repository = tmp_path / "expired-repository"
    trusted_root = _write_tuf_repository(
        expired_repository,
        expired_targets,
        expires=datetime.now(UTC) - timedelta(days=1),
    )
    expired = SkillCatalogClient(
        _profile(expired_repository, trusted_root), tmp_path / "expired-cache"
    )
    with pytest.raises(SkillCatalogError, match="Unable to verify"):
        expired.refresh()


def test_catalog_metadata_rollback_fails_closed(tmp_path: Path) -> None:
    targets = _catalog_targets(tmp_path)
    expiry = datetime.now(UTC) + timedelta(days=30)
    signers = {
        role: CryptoSigner.generate_ed25519()
        for role in ("root", "targets", "snapshot", "timestamp")
    }
    older = tmp_path / "older"
    newer = tmp_path / "newer"
    live = tmp_path / "live"
    _write_tuf_repository(older, targets, expires=expiry, signers=signers, version=1)
    _write_tuf_repository(newer, targets, expires=expiry, signers=signers, version=2)
    shutil.copytree(older, live)
    client = SkillCatalogClient(
        _profile(live, live / "metadata" / "1.root.json"),
        tmp_path / "rollback-cache",
    )
    client.refresh()
    shutil.copytree(newer / "metadata", live / "metadata", dirs_exist_ok=True)
    client.refresh()

    shutil.copytree(older / "metadata", live / "metadata", dirs_exist_ok=True)
    with pytest.raises(SkillCatalogError, match="Unable to verify"):
        client.refresh()


def test_catalog_and_tuf_target_manifests_must_agree(tmp_path: Path) -> None:
    targets = _catalog_targets(tmp_path)
    catalog_path = targets / "catalog.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    payload["entries"][0]["archive_sha256"] = "f" * 64
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")
    repository = tmp_path / "repository"
    trusted_root = _write_tuf_repository(repository, targets)
    client = SkillCatalogClient(_profile(repository, trusted_root), tmp_path / "cache")

    with pytest.raises(SkillCatalogError, match="disagrees"):
        client.refresh()


def test_source_registry_resolves_deployment_owned_paths_and_precedence(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / "metadata").mkdir(parents=True)
    trusted_root = repository / "metadata" / "1.root.json"
    trusted_root.write_text("{}", encoding="utf-8")
    config = tmp_path / "skill-sources.toml"
    config.write_text(
        """schema_version = "heartwood.skill-sources.v1"

[[sources]]
id = "official"
kind = "offline"
trusted-root = "repository/metadata/1.root.json"
repository = "repository"
""",
        encoding="utf-8",
    )
    registry = load_skill_source_registry(config)
    assert registry.sources[0].trusted_root == trusted_root
    assert registry.sources[0].repository == repository

    loaded, source_path = configured_skill_source_registry(
        {"HEARTWOOD_SKILL_SOURCES_FILE": str(config)},
        home=tmp_path / "home",
        system_path=tmp_path / "missing-system",
    )
    assert loaded == registry
    assert source_path == config

    empty, source_path = configured_skill_source_registry(
        {}, home=tmp_path / "empty-home", system_path=tmp_path / "missing-system"
    )
    assert empty.sources == ()
    assert source_path is None

    with pytest.raises(SkillCatalogError, match="absolute path"):
        configured_skill_source_registry(
            {"HEARTWOOD_SKILL_SOURCES_FILE": "relative.toml"}, home=tmp_path
        )


def test_source_registry_rejects_unsafe_remote_and_duplicate_sources(tmp_path: Path) -> None:
    root = tmp_path / "root.json"
    root.write_text("{}", encoding="utf-8")
    with pytest.raises(ValidationError, match="HTTPS"):
        SkillSourceProfile.model_validate(
            {
                "id": "remote",
                "kind": "remote",
                "trusted-root": root,
                "metadata-url": "http://example.test/metadata",
                "targets-url": "https://example.test/targets",
            }
        )
    with pytest.raises(ValidationError, match="embedded credentials"):
        SkillSourceProfile.model_validate(
            {
                "id": "remote",
                "kind": "remote",
                "trusted-root": root,
                "metadata-url": "https://token@example.test/metadata",
                "targets-url": "https://example.test/targets",
            }
        )
    with pytest.raises(ValidationError, match="HTTPS"):
        SkillSourceProfile.model_validate(
            {
                "id": "remote",
                "kind": "remote",
                "trusted-root": root,
                "metadata-url": "https://example.test/metadata?token=secret",
                "targets-url": "https://example.test/targets",
            }
        )
    with pytest.raises(ValidationError, match="exact SHA-256"):
        SkillSourceProfile.model_validate(
            {
                "id": "offline",
                "kind": "offline",
                "trusted-root": root,
                "repository": tmp_path,
                "controlled-data-approved-digests": ["latest"],
            }
        )
    with pytest.raises(ValidationError, match="must be unique"):
        SkillSourceProfile.model_validate(
            {
                "id": "offline",
                "kind": "offline",
                "trusted-root": root,
                "repository": tmp_path,
                "controlled-data-approved-digests": ["a" * 64, "A" * 64],
            }
        )
    with pytest.raises(ValidationError, match="require metadata-url and targets-url only"):
        SkillSourceProfile.model_validate(
            {
                "id": "remote",
                "kind": "remote",
                "trusted-root": root,
                "metadata-url": "https://example.test/metadata",
            }
        )
    with pytest.raises(ValidationError, match="require a repository path only"):
        SkillSourceProfile.model_validate(
            {
                "id": "offline",
                "kind": "offline",
                "trusted-root": root,
                "metadata-url": "https://example.test/metadata",
                "targets-url": "https://example.test/targets",
            }
        )
    config = tmp_path / "duplicate.toml"
    config.write_text(
        f"""schema_version = "heartwood.skill-sources.v1"
[[sources]]
id = "duplicate"
kind = "offline"
trusted-root = "{root}"
repository = "{tmp_path}"
[[sources]]
id = "duplicate"
kind = "offline"
trusted-root = "{root}"
repository = "{tmp_path}"
""",
        encoding="utf-8",
    )
    with pytest.raises(SkillCatalogError, match="invalid"):
        load_skill_source_registry(config)

    malformed = tmp_path / "malformed.toml"
    malformed.write_text("sources = {}\n", encoding="utf-8")
    with pytest.raises(SkillCatalogError, match="sources must be an array"):
        load_skill_source_registry(malformed)

    malformed.write_text('sources = ["not-an-object"]\n', encoding="utf-8")
    with pytest.raises(SkillCatalogError, match="must be an object"):
        load_skill_source_registry(malformed)

    malformed.write_text("sources = [\n", encoding="utf-8")
    with pytest.raises(SkillCatalogError, match="invalid"):
        load_skill_source_registry(malformed)


def test_catalog_client_rejects_missing_substituted_and_revoked_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, snapshot, _ = _client(tmp_path)
    entry = snapshot.entry("aggregate-export")
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        CatalogDocument(entries=(entry,)).model_dump_json(),
        encoding="utf-8",
    )
    catalog_info = SimpleNamespace(length=catalog_path.stat().st_size, hashes={"sha256": "unused"})
    valid_target = SimpleNamespace(
        length=entry.archive_size,
        hashes={"sha256": entry.archive_sha256},
    )

    class SyntheticUpdater:
        def __init__(self, targets: dict[str, object]) -> None:
            self.targets = targets

        def refresh(self) -> None:
            return None

        def get_targetinfo(self, name: str) -> object | None:
            return self.targets.get(name)

        def download_target(self, target: object) -> str:
            if target is catalog_info:
                return str(catalog_path)
            return str(tmp_path / "missing")

    monkeypatch.setattr(client, "_updater", lambda: SyntheticUpdater({}))
    with pytest.raises(SkillCatalogError, match=r"does not publish catalog\.json"):
        client.refresh()
    oversized_catalog = SimpleNamespace(
        length=9 * 1024 * 1024,
        hashes={"sha256": "unused"},
    )
    monkeypatch.setattr(
        client,
        "_updater",
        lambda: SyntheticUpdater({"catalog.json": oversized_catalog}),
    )
    with pytest.raises(SkillCatalogError, match="size limit"):
        client.refresh()
    monkeypatch.setattr(
        client,
        "_updater",
        lambda: SyntheticUpdater({"catalog.json": catalog_info}),
    )
    with pytest.raises(SkillCatalogError, match="references a missing target"):
        client.download(entry)

    changed = SimpleNamespace(
        length=entry.archive_size + 1, hashes={"sha256": entry.archive_sha256}
    )
    monkeypatch.setattr(
        client,
        "_updater",
        lambda: SyntheticUpdater({"catalog.json": catalog_info, entry.target: changed}),
    )
    with pytest.raises(SkillCatalogError, match=r"disagrees with catalog\.json"):
        client.download(entry)

    changed_entry = entry.model_copy(update={"description": "Changed after review"})
    catalog_path.write_text(
        CatalogDocument(entries=(changed_entry,)).model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        client,
        "_updater",
        lambda: SyntheticUpdater(
            {"catalog.json": catalog_info, changed_entry.target: valid_target}
        ),
    )
    with pytest.raises(SkillCatalogError, match="changed after review"):
        client.download(entry)

    revoked = entry.model_copy(update={"revoked": True, "revocation_reason": "Security review"})
    with pytest.raises(SkillCatalogError, match="has been revoked"):
        client.download(revoked)

    class FailingUpdater:
        def refresh(self) -> None:
            raise DownloadError("synthetic source unavailable")

    monkeypatch.setattr(client, "_updater", FailingUpdater)
    with pytest.raises(SkillCatalogError, match="Unable to verify"):
        client.refresh()


def test_offline_fetcher_streams_local_targets_through_tuf_limits(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    target = repository / "targets" / "large.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * (3 * 64 * 1024 + 17))
    fetcher = _LocalRepositoryFetcher(repository)

    chunks = tuple(fetcher.fetch(target.resolve().as_uri()))

    assert b"".join(chunks) == target.read_bytes()
    assert max(map(len, chunks)) <= 64 * 1024


def test_catalog_client_rejects_missing_and_oversized_trusted_roots(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    missing = _profile(repository, tmp_path / "missing-root.json")
    with pytest.raises(SkillCatalogError, match="Trusted root is unavailable"):
        SkillCatalogClient(missing, tmp_path / "cache").refresh()

    oversized_root = tmp_path / "oversized-root.json"
    oversized_root.write_bytes(b"x" * 512_001)
    oversized = _profile(repository, oversized_root)
    with pytest.raises(SkillCatalogError, match="Trusted root is too large"):
        SkillCatalogClient(oversized, tmp_path / "oversized-cache").refresh()


def test_content_addressed_store_installs_retries_revokes_and_deactivates(tmp_path: Path) -> None:
    client, snapshot, _ = _client(tmp_path)
    entry = snapshot.entry("aggregate-export")
    archive = client.download(entry)
    store = SkillArtifactStore(tmp_path / "state" / "skills")

    record = store.install_catalog(
        entry,
        archive,
        source_id=snapshot.source_id,
        controlled_data_ready=True,
    )
    assert (
        store.install_catalog(
            entry,
            archive,
            source_id=snapshot.source_id,
            controlled_data_ready=True,
        )
        == record
    )
    assert record.controlled_data_ready
    assert store.records() == (record,)
    assert store.active_manifests()[0].tree_sha256 == entry.tree_sha256
    assert store.artifact_path(record).is_dir()

    revoked_entry = entry.model_copy(
        update={"revoked": True, "revocation_reason": "Synthetic security withdrawal"}
    )
    store.apply_catalog_snapshot(
        SkillCatalogSnapshot(
            source_id=snapshot.source_id,
            offline=True,
            entries=(revoked_entry,),
        )
    )
    revoked = store.records()[0]
    assert revoked.status == "revoked"
    assert store.active_manifests() == ()
    removed = store.remove(record.name)
    assert removed == revoked
    assert store.records() == ()
    assert store.artifact_path(record).is_dir()


def test_empty_store_reads_do_not_create_project_state(tmp_path: Path) -> None:
    root = tmp_path / ".heartwood" / "skills"
    store = SkillArtifactStore(root)

    assert store.records() == ()
    assert store.active_manifests() == ()
    assert not root.exists()


def test_local_install_is_unreviewed_content_addressed_and_tamper_evident(tmp_path: Path) -> None:
    source = tmp_path / "source" / "aggregate-export"
    shutil.copytree(_SKILLS_ROOT / "aggregate-export", source)
    store = SkillArtifactStore(tmp_path / "state" / "skills")
    record = store.install_local(source)
    assert record.review == "local-unreviewed"
    assert record.controlled_data_ready is False
    assert store.install_local(source) == record

    (store.artifact_path(record) / "SKILL.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(SkillStoreError, match="artifact is invalid"):
        store.active_manifests()


def test_installed_record_cannot_forge_catalog_or_controlled_data_provenance() -> None:
    common = {
        "name": "synthetic-skill",
        "skill_id": "heartwood.synthetic.skill",
        "version": "1.0.0",
        "tree_sha256": "a" * 64,
        "source_id": "local",
    }
    with pytest.raises(ValidationError, match="cannot claim"):
        InstalledSkillRecord.model_validate(
            {
                **common,
                "source_kind": "local",
                "review": "local-unreviewed",
                "controlled_data_ready": True,
            }
        )
    with pytest.raises(ValidationError, match="immutable repository provenance"):
        InstalledSkillRecord.model_validate(
            {**common, "source_kind": "catalog", "review": "repository-reviewed"}
        )
    with pytest.raises(ValidationError, match="require a reason"):
        InstalledSkillRecord.model_validate(
            {
                **common,
                "source_kind": "local",
                "review": "local-unreviewed",
                "status": "revoked",
            }
        )
    with pytest.raises(ValidationError, match="cannot declare a revocation reason"):
        InstalledSkillRecord.model_validate(
            {
                **common,
                "source_kind": "local",
                "review": "local-unreviewed",
                "revocation_reason": "not revoked",
            }
        )
    with pytest.raises(ValidationError, match="duplicate names"):
        SkillInstallationIndex(
            skills=(
                InstalledSkillRecord.model_validate(
                    {**common, "source_kind": "local", "review": "local-unreviewed"}
                ),
                InstalledSkillRecord.model_validate(
                    {**common, "source_kind": "local", "review": "local-unreviewed"}
                ),
            )
        )
    second = InstalledSkillRecord.model_validate(
        {
            **common,
            "name": "another-synthetic-skill",
            "tree_sha256": "b" * 64,
            "source_kind": "local",
            "review": "local-unreviewed",
        }
    )
    with pytest.raises(ValidationError, match="duplicate identifiers"):
        SkillInstallationIndex(
            skills=(
                InstalledSkillRecord.model_validate(
                    {**common, "source_kind": "local", "review": "local-unreviewed"}
                ),
                second,
            )
        )


def test_store_rejects_a_second_name_for_an_active_skill_identifier(tmp_path: Path) -> None:
    first = tmp_path / "first" / "aggregate-export"
    second = tmp_path / "second" / "renamed-export"
    shutil.copytree(_SKILLS_ROOT / "aggregate-export", first)
    shutil.copytree(first, second)
    skill_file = second / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8").replace(
            "name: aggregate-export", "name: renamed-export"
        ),
        encoding="utf-8",
    )
    store = SkillArtifactStore(tmp_path / "state" / "skills")
    store.install_local(first)

    with pytest.raises(SkillStoreError, match="identifier is already active"):
        store.install_local(second)


def test_store_rejects_revoked_conflicting_and_corrupt_state(tmp_path: Path) -> None:
    client, snapshot, _ = _client(tmp_path)
    entry = snapshot.entry("aggregate-export")
    archive = client.download(entry)
    store = SkillArtifactStore(tmp_path / "state" / "skills")

    revoked = entry.model_copy(update={"revoked": True, "revocation_reason": "Security review"})
    with pytest.raises(SkillStoreError, match="has been revoked"):
        store.install_catalog(revoked, archive, source_id=snapshot.source_id)

    record = store.install_catalog(entry, archive, source_id=snapshot.source_id)
    conflicting = entry.model_copy(
        update={"policy": entry.policy.model_copy(update={"version": "1.0.1"})}
    )
    with pytest.raises(SkillStoreError, match="already exists"):
        store.install_catalog(conflicting, archive, source_id=snapshot.source_id)

    store.remove(record.name)
    assert store.install_catalog(entry, archive, source_id=snapshot.source_id) == record

    store.index_path.write_text("{", encoding="utf-8")
    with pytest.raises(SkillStoreError, match="index is invalid"):
        store.records()


def test_store_marks_removed_and_replaced_catalog_revisions_revoked(tmp_path: Path) -> None:
    client, snapshot, _ = _client(tmp_path)
    entries = snapshot.entries[:2]
    store = SkillArtifactStore(tmp_path / "state" / "skills")
    for entry in entries:
        store.install_catalog(entry, client.download(entry), source_id=snapshot.source_id)

    replacement = entries[1].model_copy(update={"source_revision": "0" * 40})
    store.apply_catalog_snapshot(
        SkillCatalogSnapshot(
            source_id=snapshot.source_id,
            offline=True,
            entries=(replacement,),
        )
    )

    records = {record.name: record for record in store.records()}
    assert records[entries[0].name].revocation_reason == "Removed from the signed Skill catalog"
    assert records[entries[1].name].revocation_reason == (
        "Installed revision is no longer present in the signed Skill catalog"
    )
