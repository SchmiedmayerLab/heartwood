# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Operator-owned project and policy binding for gateway launch."""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from heartwood.gateway._project import ProjectContext, ProjectStateError
from heartwood.model_policy import validate_policy_restriction
from heartwood.persistence import read_private_text
from heartwood.schemas import PolicyProfile

_SCHEMA = "heartwood.project-deployment.v1"
_FIELDS = {"schema_version", "project_id", "workspace", "state_root", "policy"}


@dataclass(frozen=True, slots=True)
class ProjectDeployment:
    """One trusted launch binding, never derived from research-project content."""

    project: ProjectContext
    policy: PolicyProfile


def load_project_deployment(path: Path, *, project_root: Path) -> ProjectDeployment:
    """Read a bounded operator manifest and reject missing or conflicting authority."""
    workspace = project_root.resolve(strict=True)
    if not path.is_absolute() or path.is_symlink():
        raise ProjectStateError("deployment manifest must be an absolute non-symlink path")
    resolved = path.resolve()
    if resolved != path:
        raise ProjectStateError("deployment manifest must not traverse symbolic links")
    if resolved == workspace or workspace in resolved.parents:
        raise ProjectStateError("deployment manifest must be outside the research directory")
    try:
        _validate_trusted_parents(resolved.parent)
        metadata = resolved.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or metadata.st_uid not in {0, os.geteuid()}
        ):
            raise ProjectStateError(
                "deployment manifest must be operator-owned and write-protected"
            )
        record = tomllib.loads(read_private_text(resolved, max_bytes=64 * 1024))
        if set(record) != _FIELDS or record["schema_version"] != _SCHEMA:
            raise ValueError("invalid deployment schema")
        identity = UUID(record["project_id"])
        if str(identity) != record["project_id"]:
            raise ValueError("noncanonical project identity")
        bound_workspace = Path(record["workspace"])
        state_root = Path(record["state_root"])
        if bound_workspace != workspace:
            raise ProjectStateError(
                "deployment manifest does not authorize this research directory"
            )
        project = ProjectContext(workspace, protected_state=state_root, project_id=identity)
        _validate_trusted_parents(project.state_root.parent)
        if resolved == project.state_root or project.state_root in resolved.parents:
            raise ProjectStateError("deployment manifest must remain separate from project state")
        policy = PolicyProfile.model_validate(record["policy"])
        validate_policy_restriction(policy, policy)
        # Fail before opening gateway stores, including when legacy state is present.
        project.state_exists()
        return ProjectDeployment(project=project, policy=policy)
    except ProjectStateError:
        raise
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ProjectStateError("deployment manifest is unavailable or invalid") from error


def _validate_trusted_parents(directory: Path) -> None:
    for parent in (directory, *directory.parents):
        metadata = parent.stat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, os.geteuid()}:
            raise ProjectStateError("deployment paths require operator-owned parent directories")
        writable = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        system_temporary_root = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        if writable and not system_temporary_root:
            raise ProjectStateError("deployment paths have a writable parent directory")
