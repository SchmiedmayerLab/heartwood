# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""TUF-backed Skill source discovery and immutable target acquisition."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, Self
from urllib.parse import unquote, urlparse

from heartwood_skill_catalog import CatalogDocument, CatalogEntry
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from tuf.api.exceptions import DownloadError, DownloadHTTPError, RepositoryError
from tuf.ngclient.config import UpdaterConfig
from tuf.ngclient.fetcher import FetcherInterface
from tuf.ngclient.updater import Updater

from heartwood.persistence import DurableFileError, read_private_bytes, read_private_text

_MAX_ROOT_BYTES = 512_000
_MAX_CATALOG_BYTES = 8 * 1024 * 1024
_SOURCE_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class SkillCatalogError(ValueError):
    """Raised when a signed Skill source or target fails closed."""


class _Record(BaseModel):
    """Strict immutable base for source-registry records."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class SkillSourceProfile(_Record):
    """One deployment-approved remote or offline TUF repository."""

    source_id: str = Field(alias="id", pattern=_SOURCE_ID_PATTERN)
    kind: Literal["remote", "offline"]
    trusted_root: Path = Field(alias="trusted-root")
    metadata_url: str | None = Field(default=None, alias="metadata-url")
    targets_url: str | None = Field(default=None, alias="targets-url")
    repository: Path | None = None
    controlled_data_approved_digests: tuple[str, ...] = Field(
        default=(), alias="controlled-data-approved-digests"
    )
    enabled: bool = True

    @field_validator("metadata_url", "targets_url")
    @classmethod
    def _remote_url_is_https(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("remote Skill source URLs must use HTTPS without embedded credentials")
        return value.rstrip("/") + "/"

    @field_validator("controlled_data_approved_digests")
    @classmethod
    def _controlled_data_digests_are_exact(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(digest.lower() for digest in value)
        if any(not re.fullmatch(_SHA256_PATTERN, digest) for digest in normalized):
            raise ValueError("controlled-data approvals must use exact SHA-256 tree digests")
        if len(normalized) != len(set(normalized)):
            raise ValueError("controlled-data approval digests must be unique")
        return normalized

    @model_validator(mode="after")
    def _source_mode_is_complete(self) -> Self:
        if self.kind == "remote":
            if self.metadata_url is None or self.targets_url is None or self.repository is not None:
                raise ValueError("remote Skill sources require metadata-url and targets-url only")
        elif (
            self.repository is None or self.metadata_url is not None or self.targets_url is not None
        ):
            raise ValueError("offline Skill sources require a repository path only")
        return self

    def repository_urls(self) -> tuple[str, str]:
        """Return normalized metadata and target base URLs."""
        if self.kind == "remote":
            if self.metadata_url is None or self.targets_url is None:  # pragma: no cover
                raise SkillCatalogError("remote Skill source is incomplete")
            return self.metadata_url, self.targets_url
        if self.repository is None:  # pragma: no cover
            raise SkillCatalogError("offline Skill source is incomplete")
        return (
            (self.repository / "metadata").resolve().as_uri() + "/",
            (self.repository / "targets").resolve().as_uri() + "/",
        )

    def controlled_data_ready(self, entry: CatalogEntry) -> bool:
        """Return whether this deployment approved the exact verified Skill tree."""
        return entry.tree_sha256 in self.controlled_data_approved_digests


class SkillSourceRegistry(_Record):
    """Deployment-owned set of independent Skill trust roots."""

    schema_version: Literal["heartwood.skill-sources.v1"] = "heartwood.skill-sources.v1"
    sources: tuple[SkillSourceProfile, ...] = ()

    @model_validator(mode="after")
    def _source_ids_are_unique(self) -> Self:
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("Skill source registry contains duplicate source identifiers")
        return self

    def enabled_sources(self) -> tuple[SkillSourceProfile, ...]:
        """Return enabled sources in deterministic configuration order."""
        return tuple(source for source in self.sources if source.enabled)


@dataclass(frozen=True, slots=True)
class SkillCatalogSnapshot:
    """Verified projection of one signed catalog target."""

    source_id: str
    offline: bool
    entries: tuple[CatalogEntry, ...]

    def entry(self, name: str) -> CatalogEntry:
        """Return one active or revoked entry by exact Agent Skill name."""
        matches = [entry for entry in self.entries if entry.name == name]
        if not matches:
            raise SkillCatalogError(f"Skill is not available from {self.source_id}: {name}")
        return matches[0]


def load_skill_source_registry(path: Path) -> SkillSourceRegistry:
    """Load deployment-owned source configuration and resolve its local paths."""
    config_path = path.resolve()
    try:
        payload = tomllib.loads(read_private_text(config_path))
    except (DurableFileError, OSError, tomllib.TOMLDecodeError) as error:
        raise SkillCatalogError(f"Skill source registry is invalid: {path}") from error
    source_payloads = payload.get("sources", [])
    if not isinstance(source_payloads, list):
        raise SkillCatalogError("Skill source registry sources must be an array")
    normalized_sources: list[dict[str, object]] = []
    for source_payload in source_payloads:
        if not isinstance(source_payload, dict):
            raise SkillCatalogError("Each Skill source must be an object")
        normalized = dict(source_payload)
        for key in ("trusted-root", "repository"):
            raw = normalized.get(key)
            if isinstance(raw, str):
                candidate = Path(raw).expanduser()
                normalized[key] = (
                    candidate if candidate.is_absolute() else config_path.parent / candidate
                ).resolve()
        normalized_sources.append(normalized)
    normalized_payload = dict(payload)
    normalized_payload["sources"] = normalized_sources
    try:
        return SkillSourceRegistry.model_validate(normalized_payload)
    except ValidationError as error:
        raise SkillCatalogError(f"Skill source registry is invalid: {path}") from error


def configured_skill_source_registry(
    env: Mapping[str, str],
    *,
    home: Path | None = None,
    system_path: Path = Path("/etc/heartwood/skill-sources.toml"),
) -> tuple[SkillSourceRegistry, Path | None]:
    """Load the explicit environment, deployment, or workstation source registry."""
    override = env.get("HEARTWOOD_SKILL_SOURCES_FILE")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise SkillCatalogError("HEARTWOOD_SKILL_SOURCES_FILE must be an absolute path")
        return load_skill_source_registry(path), path.resolve()
    candidates = (system_path, (home or Path.home()) / ".config/heartwood/skill-sources.toml")
    for candidate in candidates:
        if candidate.is_file():
            return load_skill_source_registry(candidate), candidate.resolve()
    return SkillSourceRegistry(), None


class SkillCatalogClient:
    """Verify one TUF repository and acquire catalog-declared targets."""

    def __init__(self, profile: SkillSourceProfile, cache_root: Path) -> None:
        self.profile = profile
        self.cache_root = (cache_root / profile.source_id).resolve()

    def refresh(self) -> SkillCatalogSnapshot:
        """Refresh signed metadata and return its validated catalog projection."""
        updater = self._updater()
        try:
            return self._refresh_with(updater)
        except (
            OSError,
            DownloadError,
            RepositoryError,
            DurableFileError,
            ValidationError,
        ) as error:
            raise SkillCatalogError(
                f"Unable to verify Skill source {self.profile.source_id}: {error}"
            ) from error

    def download(self, entry: CatalogEntry) -> Path:
        """Revalidate and download one approved entry from a single TUF snapshot."""
        if entry.revoked:
            raise SkillCatalogError(f"Skill has been revoked: {entry.name}")
        updater = self._updater()
        try:
            snapshot = self._refresh_with(updater)
            current_entry = snapshot.entry(entry.name)
            if current_entry != entry:
                raise SkillCatalogError(
                    f"Skill changed after review; inspect it again: {entry.name}"
                )
            if current_entry.revoked:  # pragma: no cover - equality documents the invariant
                raise SkillCatalogError(f"Skill has been revoked: {entry.name}")
            target_info = updater.get_targetinfo(current_entry.target)
            if target_info is None:
                raise SkillCatalogError(f"Signed Skill target is missing: {current_entry.target}")
            return Path(updater.download_target(target_info))
        except (
            OSError,
            DownloadError,
            RepositoryError,
            DurableFileError,
            ValidationError,
        ) as error:
            raise SkillCatalogError(
                f"Unable to download Skill target from {self.profile.source_id}: {error}"
            ) from error

    def _refresh_with(self, updater: Updater) -> SkillCatalogSnapshot:
        """Validate one catalog and all target declarations with one updater state."""
        updater.refresh()
        catalog_info = updater.get_targetinfo("catalog.json")
        if catalog_info is None:
            raise SkillCatalogError("Signed Skill source does not publish catalog.json")
        if catalog_info.length > _MAX_CATALOG_BYTES:
            raise SkillCatalogError("Signed Skill catalog exceeds the supported size limit")
        catalog_path = Path(updater.download_target(catalog_info))
        document = CatalogDocument.model_validate_json(
            read_private_text(catalog_path, max_bytes=_MAX_CATALOG_BYTES)
        )
        for entry in document.entries:
            target_info = updater.get_targetinfo(entry.target)
            if target_info is None:
                raise SkillCatalogError(
                    f"Signed Skill catalog references a missing target: {entry.target}"
                )
            if (
                target_info.length != entry.archive_size
                or target_info.hashes.get("sha256") != entry.archive_sha256
            ):
                raise SkillCatalogError(
                    f"Signed Skill target metadata disagrees with catalog.json: {entry.target}"
                )
        return SkillCatalogSnapshot(
            source_id=self.profile.source_id,
            offline=self.profile.kind == "offline",
            entries=document.entries,
        )

    def _updater(self) -> Updater:
        metadata_url, targets_url = self.profile.repository_urls()
        trusted_root = self.profile.trusted_root.resolve()
        try:
            if trusted_root.stat().st_size > _MAX_ROOT_BYTES:
                raise SkillCatalogError(f"Trusted root is too large: {trusted_root}")
            root_bytes = read_private_bytes(trusted_root, max_bytes=_MAX_ROOT_BYTES)
        except (DurableFileError, OSError) as error:
            raise SkillCatalogError(
                f"Trusted root is unavailable for {self.profile.source_id}: {trusted_root}"
            ) from error
        metadata_dir = self.cache_root / "metadata"
        targets_dir = self.cache_root / "targets"
        metadata_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        targets_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fetcher = (
            _LocalRepositoryFetcher(self.profile.repository)
            if self.profile.kind == "offline" and self.profile.repository is not None
            else None
        )
        return Updater(
            str(metadata_dir),
            metadata_url,
            str(targets_dir),
            targets_url,
            fetcher=fetcher,
            config=UpdaterConfig(app_user_agent="heartwood-skill-client/1"),
            bootstrap=root_bytes,
        )


class _LocalRepositoryFetcher(FetcherInterface):
    """Read a transferred TUF repository without network access."""

    def __init__(self, repository: Path) -> None:
        self.repository = repository.resolve()

    def _fetch(self, url: str) -> Iterator[bytes]:
        parsed = urlparse(url)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise DownloadHTTPError("Offline Skill source only accepts local files", 400)
        path = Path(unquote(parsed.path)).resolve()
        if not path.is_relative_to(self.repository):
            raise DownloadHTTPError("Offline Skill source path escapes its repository", 403)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as error:
            raise DownloadHTTPError(
                f"Offline Skill target does not exist: {path.name}", 404
            ) from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise DownloadHTTPError(
                    f"Offline Skill target is not a regular file: {path.name}", 400
                )
            while chunk := os.read(descriptor, 64 * 1024):
                yield chunk
        finally:
            os.close(descriptor)
