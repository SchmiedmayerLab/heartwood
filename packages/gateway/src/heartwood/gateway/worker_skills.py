# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Content-only projections of selected Skill packages for isolated workers."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from heartwood_skill_catalog import CatalogBuildError, copy_skill_tree, inspect_skill

from heartwood.skills import SkillManifest

if TYPE_CHECKING:
    from openhands.sdk.skills import Skill


@dataclass(frozen=True, slots=True)
class WorkerSkillProjection:
    """Temporary content snapshot with native paths for the read-only worker mount."""

    root: Path
    bindings: tuple[tuple[str, str], ...]

    @property
    def sha256(self) -> str:
        """Bind the selected names and content identities, excluding host paths."""
        value = {
            "schema_version": "heartwood.worker-skills.v1",
            "mount": "/skills",
            "skills": sorted(self.bindings),
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def agent_skills(self) -> list[Skill]:
        """Load native Skill objects with only worker-visible source and resource paths."""
        from openhands.sdk.skills import Skill

        skills: list[Skill] = []
        for name, digest in self.bindings:
            source = self.root / name
            if inspect_skill(source).tree_sha256 != digest:
                raise CatalogBuildError("Staged worker Skill changed before loading")
            skill = Skill.load(source / "SKILL.md")
            target = PurePosixPath("/skills") / name
            resources = skill.resources
            if resources is not None:
                resources = resources.model_copy(update={"skill_root": str(target)})
            skills.append(
                skill.model_copy(
                    update={"source": str(target / "SKILL.md"), "resources": resources}
                )
            )
        return skills


@contextmanager
def stage_skill_projection(
    manifests: Sequence[SkillManifest], *, project_root: Path, staging_parent: Path
) -> Iterator[WorkerSkillProjection]:
    """Copy an already authorized selection using the catalog's complete-tree verifier."""
    parent = staging_parent.resolve(strict=True)
    if parent.is_relative_to(project_root.resolve()):
        raise ValueError("Worker Skill staging must be outside the project")
    metadata = parent.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("Worker Skill staging requires an operator-owned private directory")
    names = [manifest.name for manifest in manifests]
    if len(names) > 256 or len(names) != len(set(names)):
        raise ValueError("Worker Skills require at most 256 unique package names")
    with TemporaryDirectory(prefix="worker-skills-", dir=parent) as temporary:
        root = Path(temporary)
        bindings: list[tuple[str, str]] = []
        total = 0
        for manifest in sorted(manifests, key=lambda item: item.name):
            inspected = inspect_skill(manifest.root)
            if inspected.name != manifest.name or inspected.tree_sha256 != manifest.tree_sha256:
                raise CatalogBuildError("Skill changed after worker selection")
            total += sum(item.size for item in inspected.files)
            if total > 64 * 1024 * 1024:
                raise ValueError("Worker Skills exceed the transfer byte limit")
            copy_skill_tree(
                manifest.root, root / manifest.name, expected_tree_sha256=manifest.tree_sha256
            )
            bindings.append((manifest.name, manifest.tree_sha256))
        yield WorkerSkillProjection(root=root, bindings=tuple(bindings))
