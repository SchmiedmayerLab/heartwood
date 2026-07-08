# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Skill bundle catalog loading and verification."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from heartwood.schemas import SkillMetadata
from heartwood.skills._verification import LocalSkillVerifier, SkillManifest

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEMVER_REF_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


class SkillBundleError(ValueError):
    """Raised when a skill bundle catalog fails validation or resolution."""


class _BundleRecord(BaseModel):
    """Base model for immutable bundle catalog records."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class LocalSkillSource(_BundleRecord):
    """Checked-in local skill source."""

    type: Literal["local"] = "local"
    path: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _path_is_catalog_relative(cls, value: str) -> str:
        return _catalog_relative_path(value, "local skill source path")


class GitSkillSource(_BundleRecord):
    """Pinned external git skill source resolved at package-build time."""

    type: Literal["git"] = "git"
    repository: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    commit: str = Field(min_length=40, max_length=40)
    path: str = Field(min_length=1)
    content_sha256: str = Field(min_length=64, max_length=64)

    @field_validator("repository")
    @classmethod
    def _repository_has_no_whitespace(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            msg = "git repository must not contain whitespace"
            raise ValueError(msg)
        return value

    @field_validator("ref")
    @classmethod
    def _ref_is_semver(cls, value: str) -> str:
        if not _SEMVER_REF_RE.fullmatch(value):
            msg = "git source ref must be a semver tag"
            raise ValueError(msg)
        return value

    @field_validator("commit")
    @classmethod
    def _commit_is_full_sha(cls, value: str) -> str:
        normalized = value.lower()
        if not _COMMIT_RE.fullmatch(normalized):
            msg = "git source commit must be a full 40-character SHA-1 hash"
            raise ValueError(msg)
        return normalized

    @field_validator("path")
    @classmethod
    def _path_is_repository_relative(cls, value: str) -> str:
        return _catalog_relative_path(value, "git skill source path")

    @field_validator("content_sha256")
    @classmethod
    def _content_hash_is_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if not _SHA256_RE.fullmatch(normalized):
            msg = "git source content_sha256 must be a SHA-256 hash"
            raise ValueError(msg)
        return normalized


class SkillSourceProvenance(_BundleRecord):
    """Source review and signing metadata for a bundled skill."""

    signature: str = Field(min_length=1)
    reviewed_by: str | None = Field(default=None, min_length=1)
    license_id: str | None = Field(default=None, alias="license", min_length=1)

    @field_validator("signature")
    @classmethod
    def _signature_is_sigstore_placeholder(cls, value: str) -> str:
        if not value.startswith("sigstore:"):
            msg = "skill source provenance must declare a sigstore signature placeholder"
            raise ValueError(msg)
        return value


SkillSource = Annotated[LocalSkillSource | GitSkillSource, Field(discriminator="type")]


class BundledSkill(_BundleRecord):
    """A skill selected for packaging."""

    skill_id: str = Field(min_length=1)
    source: SkillSource
    metadata: SkillMetadata | None = None
    provenance: SkillSourceProvenance | None = None

    @model_validator(mode="after")
    def _external_sources_are_pinned_and_described(self) -> Self:
        if isinstance(self.source, GitSkillSource):
            if self.metadata is None:
                msg = "external bundled skills require heartwood metadata"
                raise ValueError(msg)
            if self.provenance is None:
                msg = "external bundled skills require source provenance"
                raise ValueError(msg)
            if self.metadata.signature and self.metadata.signature != self.provenance.signature:
                msg = "external skill metadata signature must match source provenance signature"
                raise ValueError(msg)
        return self


class SkillBundle(_BundleRecord):
    """Catalog of skills selected for a Heartwood package build."""

    schema_version: Literal["heartwood.skill-bundle.v1"] = "heartwood.skill-bundle.v1"
    skills: tuple[BundledSkill, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _skill_ids_are_unique(self) -> Self:
        skill_ids = [skill.skill_id for skill in self.skills]
        duplicate_ids = sorted(
            {skill_id for skill_id in skill_ids if skill_ids.count(skill_id) > 1}
        )
        if duplicate_ids:
            msg = f"skill bundle contains duplicate skill ids: {', '.join(duplicate_ids)}"
            raise ValueError(msg)
        return self


@dataclass(frozen=True, slots=True)
class BundledSkillResolution:
    """Resolved package-time view of one bundled skill entry."""

    entry: BundledSkill
    metadata: SkillMetadata
    manifest: SkillManifest | None = None
    source_path: Path | None = None


def load_skill_bundle(path: Path) -> SkillBundle:
    """Load and validate a TOML skill bundle catalog."""
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        msg = f"skill bundle catalog is invalid TOML: {path}"
        raise SkillBundleError(msg) from error
    try:
        return SkillBundle.model_validate(payload)
    except ValidationError as error:
        msg = f"skill bundle catalog is invalid: {path}: {error}"
        raise SkillBundleError(msg) from error


def resolve_skill_bundle(
    path: Path,
    *,
    verifier: LocalSkillVerifier | None = None,
) -> tuple[BundledSkillResolution, ...]:
    """Resolve local entries and validate external package pins for a bundle catalog."""
    bundle = load_skill_bundle(path)
    catalog_root = path.parent.resolve()
    local_verifier = verifier or LocalSkillVerifier(catalog_root)
    resolutions: list[BundledSkillResolution] = []
    for entry in bundle.skills:
        if isinstance(entry.source, LocalSkillSource):
            resolutions.append(_resolve_local_skill(entry, catalog_root, local_verifier))
            continue
        if entry.metadata is None:
            msg = f"external skill entry is missing metadata: {entry.skill_id}"
            raise SkillBundleError(msg)
        resolutions.append(BundledSkillResolution(entry=entry, metadata=entry.metadata))
    return tuple(resolutions)


def _resolve_local_skill(
    entry: BundledSkill,
    catalog_root: Path,
    verifier: LocalSkillVerifier,
) -> BundledSkillResolution:
    if not isinstance(entry.source, LocalSkillSource):
        msg = f"expected local source for skill: {entry.skill_id}"
        raise SkillBundleError(msg)
    source_path = (catalog_root / entry.source.path).resolve()
    verification = verifier.verify(source_path)
    if not verification.verified or verification.manifest is None:
        msg = f"local bundled skill failed verification: {entry.skill_id}: {verification.reason}"
        raise SkillBundleError(msg)
    manifest = verification.manifest
    if entry.skill_id != manifest.skill_id:
        msg = (
            "bundle skill id does not match local manifest: "
            f"{entry.skill_id} != {manifest.skill_id}"
        )
        raise SkillBundleError(msg)
    if entry.metadata is not None and entry.metadata != manifest.metadata:
        msg = f"bundle metadata does not match local manifest: {entry.skill_id}"
        raise SkillBundleError(msg)
    return BundledSkillResolution(
        entry=entry,
        metadata=manifest.metadata,
        manifest=manifest,
        source_path=source_path,
    )


def _catalog_relative_path(value: str, name: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        msg = f"{name} must be a clean relative path"
        raise ValueError(msg)
    return value


def skill_ids(resolutions: Sequence[BundledSkillResolution]) -> tuple[str, ...]:
    """Return the bundled skill ids in catalog order."""
    return tuple(resolution.entry.skill_id for resolution in resolutions)
