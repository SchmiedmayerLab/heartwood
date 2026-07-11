# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Service provider interfaces for platform-specific Heartwood boundaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from heartwood.schemas import JsonValue, PolicyProfile


@dataclass(frozen=True, slots=True)
class AdapterDetection:
    """A platform adapter's proposed environment detection result."""

    adapter_id: str
    confidence: float
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DatasetFingerprint:
    """A data adapter's proposed dataset fingerprint."""

    dataset_type: str
    confidence: float
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillReference:
    """Resolved skill reference before verification and activation."""

    skill_id: str
    version: str
    source: str


@dataclass(frozen=True, slots=True)
class RegistryVerification:
    """Registry verification result for a resolved skill."""

    verified: bool
    reason: str


class PlatformAdapter(Protocol):
    """Adapter surface for execution-platform integration."""

    @property
    def adapter_id(self) -> str:
        """Return the stable platform adapter id."""

    def detect(self, env: Mapping[str, str]) -> AdapterDetection:
        """Propose whether the provided environment belongs to this platform."""

    def data_mounts(self) -> tuple[Path, ...]:
        """Return data mount paths visible inside the platform boundary."""

    def credential_allowlist(self) -> tuple[str, ...]:
        """Return environment credential names sanctioned by this platform."""

    def default_policy_profile(self) -> PolicyProfile:
        """Return the default egress and credential policy for this platform."""


class DataSourceAdapter(Protocol):
    """Adapter surface for controlled data access and fingerprinting."""

    @property
    def source_id(self) -> str:
        """Return the stable data-source adapter id."""

    def fingerprint(self) -> DatasetFingerprint:
        """Return the dataset type proposal and visible evidence."""

    def read_table(
        self,
        name: str,
        columns: Sequence[str] | None = None,
        limit: int = 20,
    ) -> Sequence[Mapping[str, JsonValue]]:
        """Read a bounded table preview through the platform data boundary."""


class RegistryAdapter(Protocol):
    """Adapter surface for resolving and verifying skills."""

    @property
    def registry_id(self) -> str:
        """Return the stable registry adapter id."""

    def resolve_skill(self, skill_id: str, version: str) -> SkillReference:
        """Resolve a skill id and version to a concrete source reference."""

    def verify_skill(self, reference: SkillReference) -> RegistryVerification:
        """Verify provenance and integrity for a resolved skill reference."""
