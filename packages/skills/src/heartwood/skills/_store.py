# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Content-addressed, atomic Agent Skill installation storage."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Literal, Self

from heartwood_skill_catalog import (
    CatalogBuildError,
    CatalogEntry,
    copy_skill_tree,
    extract_skill_archive,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from heartwood.persistence import (
    SKILL_INSTALLATIONS_VERSION,
    DurableFileError,
    fsync_directory,
    native_file_lock,
    read_private_json,
    write_private_json_atomic,
)
from heartwood.skills._catalog import SkillCatalogSnapshot
from heartwood.skills._verification import (
    LocalSkillVerifier,
    SkillManifest,
    SkillVerificationError,
)


class SkillStoreError(ValueError):
    """Raised when installed Skill state cannot be updated or verified safely."""


class _Record(BaseModel):
    """Strict immutable base for installed Skill state."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class InstalledSkillRecord(_Record):
    """Authoritative activation record for one content-addressed Skill tree."""

    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    skill_id: str
    version: str
    tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_kind: Literal["catalog", "local"]
    source_id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
    source_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    catalog_target: str | None = None
    review: Literal["repository-reviewed", "local-unreviewed"]
    controlled_data_ready: bool = False
    status: Literal["active", "revoked"] = "active"
    revocation_reason: str | None = None

    @model_validator(mode="after")
    def _provenance_and_status_are_consistent(self) -> Self:
        if self.source_kind == "catalog":
            if (
                self.source_revision is None
                or self.catalog_target is None
                or self.review != "repository-reviewed"
            ):
                raise ValueError("Catalog Skills require immutable repository provenance")
        elif (
            self.source_revision is not None
            or self.catalog_target is not None
            or self.review != "local-unreviewed"
            or self.controlled_data_ready
        ):
            raise ValueError("Local Skills cannot claim catalog or controlled-data provenance")
        if self.status == "revoked" and not self.revocation_reason:
            raise ValueError("Revoked installed Skills require a reason")
        if self.status == "active" and self.revocation_reason is not None:
            raise ValueError("Active installed Skills cannot declare a revocation reason")
        return self


class SkillInstallationIndex(_Record):
    """Atomic set of installed Skill activations."""

    schema_version: Literal["heartwood.skill-installations.v1"] = SKILL_INSTALLATIONS_VERSION
    skills: tuple[InstalledSkillRecord, ...] = ()

    @model_validator(mode="after")
    def _identities_are_unique(self) -> Self:
        names = [skill.name for skill in self.skills]
        skill_ids = [skill.skill_id for skill in self.skills]
        if len(names) != len(set(names)):
            raise ValueError("Installed Skill index contains duplicate names")
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("Installed Skill index contains duplicate identifiers")
        return self


class SkillArtifactStore:
    """Own installed Skill artifacts and their one authoritative activation index."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.artifacts_dir = self.root / "artifacts" / "sha256"
        self.index_path = self.root / "index.json"
        self.lock_path = self.root / ".install.lock"

    def records(self) -> tuple[InstalledSkillRecord, ...]:
        """Return the current activation records after validating persisted state."""
        if not self.root.exists():
            return ()
        with native_file_lock(self.lock_path):
            return self._read_index().skills

    def install_catalog(
        self,
        entry: CatalogEntry,
        archive: Path,
        *,
        source_id: str,
        controlled_data_ready: bool = False,
    ) -> InstalledSkillRecord:
        """Atomically activate one verified catalog target."""
        if entry.revoked:
            raise SkillStoreError(f"Skill has been revoked: {entry.name}")
        record = InstalledSkillRecord(
            name=entry.name,
            skill_id=entry.policy.skill_id,
            version=entry.policy.version,
            tree_sha256=entry.tree_sha256,
            source_kind="catalog",
            source_id=source_id,
            source_revision=entry.source_revision,
            catalog_target=entry.target,
            review="repository-reviewed",
            controlled_data_ready=controlled_data_ready,
        )
        return self._install_record(
            record,
            lambda destination: extract_skill_archive(entry, archive, destination),
        )

    def install_local(self, source: Path) -> InstalledSkillRecord:
        """Atomically copy and activate one explicitly approved local Skill tree."""
        source_root = source.resolve()
        try:
            manifest = LocalSkillVerifier(source_root.parent).load_manifest(source_root)
        except SkillVerificationError as error:
            raise SkillStoreError(str(error)) from error
        record = InstalledSkillRecord(
            name=manifest.name,
            skill_id=manifest.skill_id,
            version=manifest.version,
            tree_sha256=manifest.tree_sha256,
            source_kind="local",
            source_id="local",
            review="local-unreviewed",
        )
        return self._install_record(
            record,
            lambda destination: copy_skill_tree(
                source_root,
                destination,
                expected_tree_sha256=record.tree_sha256,
            ),
        )

    def remove(self, name: str) -> InstalledSkillRecord:
        """Deactivate one installed Skill while retaining its immutable artifact."""
        with native_file_lock(self.lock_path):
            index = self._read_index()
            existing = _record_named(index, name)
            if existing is None:
                raise SkillStoreError(f"Installed Skill does not exist: {name}")
            self._write_index(tuple(record for record in index.skills if record.name != name))
            return existing

    def apply_catalog_snapshot(self, snapshot: SkillCatalogSnapshot) -> None:
        """Persist revocations and removals from one freshly verified signed catalog."""
        with native_file_lock(self.lock_path):
            index = self._read_index()
            catalog_entries = {entry.name: entry for entry in snapshot.entries}
            changed = False
            records: list[InstalledSkillRecord] = []
            for record in index.skills:
                if record.source_kind != "catalog" or record.source_id != snapshot.source_id:
                    records.append(record)
                    continue
                entry = catalog_entries.get(record.name)
                reason: str | None = None
                if entry is None:
                    reason = "Removed from the signed Skill catalog"
                elif (
                    entry.tree_sha256 != record.tree_sha256
                    or entry.policy.version != record.version
                    or entry.source_revision != record.source_revision
                ):
                    reason = "Installed revision is no longer present in the signed Skill catalog"
                elif entry.revoked:
                    reason = entry.revocation_reason
                updated = record.model_copy(
                    update={
                        "status": "revoked" if reason else "active",
                        "revocation_reason": reason,
                    }
                )
                changed = changed or updated != record
                records.append(updated)
            if changed:
                self._write_index(tuple(records))

    def active_manifests(self) -> tuple[SkillManifest, ...]:
        """Verify and return active installed Skills in deterministic name order."""
        if not self.root.exists():
            return ()
        with native_file_lock(self.lock_path):
            records = self._read_index().skills
            return tuple(
                self._verified_manifest(record) for record in records if record.status == "active"
            )

    def active_skill_roots(self) -> tuple[Path, ...]:
        """Return verified parent directories consumable by OpenHands."""
        manifests = self.active_manifests()
        return tuple(manifest.root.parent for manifest in manifests)

    def artifact_path(self, record: InstalledSkillRecord) -> Path:
        """Return the confined content-addressed directory for one record."""
        return self._artifact_path(record)

    def manifest(self, record: InstalledSkillRecord) -> SkillManifest:
        """Reverify and return the immutable artifact described by one record."""
        return self._verified_manifest(record)

    def _artifact_path(self, record: InstalledSkillRecord) -> Path:
        path = (self.artifacts_dir / record.tree_sha256 / record.name).resolve()
        if not path.is_relative_to(self.artifacts_dir):  # pragma: no cover - model invariant
            raise SkillStoreError("Installed Skill artifact escapes content-addressed storage")
        return path

    def _install_record(
        self,
        record: InstalledSkillRecord,
        materialize: Callable[[Path], Path],
    ) -> InstalledSkillRecord:
        with native_file_lock(self.lock_path):
            index = self._read_index()
            existing = _record_named(index, record.name)
            if existing is not None:
                if existing == record:
                    self._verified_manifest(existing)
                    return existing
                raise SkillStoreError(f"Installed Skill already exists: {record.name}")
            identity_conflict = next(
                (item for item in index.skills if item.skill_id == record.skill_id), None
            )
            if identity_conflict is not None:
                raise SkillStoreError(
                    "Installed Skill identifier is already active as "
                    f"{identity_conflict.name}: {record.skill_id}"
                )
            destination = self._artifact_path(record)
            if destination.exists():
                self._verify_existing_artifact(record)
            else:
                try:
                    materialize(destination)
                    fsync_directory(destination.parent)
                except (CatalogBuildError, OSError) as error:
                    raise SkillStoreError(
                        f"Unable to install Skill {record.name}: {error}"
                    ) from error
            self._write_index((*index.skills, record))
            return record

    def _read_index(self) -> SkillInstallationIndex:
        if not self.index_path.exists():
            return SkillInstallationIndex()
        try:
            return SkillInstallationIndex.model_validate(read_private_json(self.index_path))
        except (DurableFileError, OSError, ValidationError) as error:
            raise SkillStoreError(f"Installed Skill index is invalid: {self.index_path}") from error

    def _write_index(self, records: tuple[InstalledSkillRecord, ...]) -> None:
        index = SkillInstallationIndex(skills=tuple(sorted(records, key=lambda item: item.name)))
        write_private_json_atomic(self.index_path, index.model_dump(mode="json"))

    def _verified_manifest(self, record: InstalledSkillRecord) -> SkillManifest:
        path = self._artifact_path(record)
        review = record.review
        try:
            manifest = LocalSkillVerifier(path.parent, review=review).load_manifest(path)
        except SkillVerificationError as error:
            raise SkillStoreError(
                f"Installed Skill artifact is invalid: {record.name}: {error}"
            ) from error
        if (
            manifest.tree_sha256 != record.tree_sha256
            or manifest.skill_id != record.skill_id
            or manifest.version != record.version
        ):
            raise SkillStoreError(
                f"Installed Skill artifact does not match its index: {record.name}"
            )
        return manifest

    def _verify_existing_artifact(self, record: InstalledSkillRecord) -> None:
        self._verified_manifest(record)


def _record_named(
    index: SkillInstallationIndex,
    name: str,
) -> InstalledSkillRecord | None:
    return next((record for record in index.skills if record.name == name), None)
