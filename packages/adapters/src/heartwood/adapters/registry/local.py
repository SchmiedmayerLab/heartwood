# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Local registry adapter for checked-in synthetic skill fixtures."""

from __future__ import annotations

import re
from pathlib import Path

from heartwood.adapters import RegistryVerification, SkillReference

_LOCAL_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class RegistryBoundaryError(ValueError):
    """Raised when a local registry reference escapes the configured root."""


class LocalRegistryAdapter:
    """Resolve skill references under a local root without loading them."""

    def __init__(self, root: Path) -> None:
        """Initialize the registry with a local skill root boundary."""
        self.root = root.resolve()

    @classmethod
    def synthetic_skills(cls, repo_root: Path | None = None) -> LocalRegistryAdapter:
        """Return a registry over checked-in synthetic skill fixtures."""
        base = Path.cwd() if repo_root is None else repo_root
        return cls(base / "fixtures" / "synthetic" / "skills")

    @property
    def registry_id(self) -> str:
        """Return the stable registry id."""
        return "local-fixture"

    def resolve_skill(self, skill_id: str, version: str) -> SkillReference:
        """Resolve a skill id and version to a local source path."""
        local_name = skill_id.removeprefix("heartwood.synthetic.")
        if not _LOCAL_SKILL_NAME.fullmatch(local_name):
            msg = f"invalid local skill id: {skill_id}"
            raise RegistryBoundaryError(msg)
        path = (self.root / local_name).resolve()
        if not _is_relative_to(path, self.root):
            msg = f"local skill path escapes registry root: {skill_id}"
            raise RegistryBoundaryError(msg)
        return SkillReference(skill_id=skill_id, version=version, source=str(path))

    def verify_skill(self, reference: SkillReference) -> RegistryVerification:
        """Verify that the local skill reference exists without loading it."""
        path = Path(reference.source).resolve()
        if not _is_relative_to(path, self.root):
            return RegistryVerification(
                verified=False,
                reason="local skill source escapes registry root",
            )
        skill_file = path / "SKILL.md"
        metadata_file = path / "metadata.json"
        verified = path.is_dir() and skill_file.is_file() and metadata_file.is_file()
        reason = (
            "local skill metadata is present" if verified else "local skill metadata is missing"
        )
        return RegistryVerification(verified=verified, reason=reason)


def _is_relative_to(path: Path, root: Path) -> bool:
    return path == root or root in path.parents
