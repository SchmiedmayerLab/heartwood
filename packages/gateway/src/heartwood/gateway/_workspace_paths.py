# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared project-relative path validation for action and workspace evidence."""

from __future__ import annotations

import unicodedata
from enum import StrEnum
from pathlib import PurePosixPath

RESERVED_PROJECT_COMPONENTS = frozenset({".git", ".heartwood"})


class ProjectPathViolation(StrEnum):
    """Stable categories used by public workspace diagnostics."""

    INVALID = "invalid"
    RESERVED = "reserved"


class ProjectPathError(ValueError):
    """Raised when a value cannot identify a public project path."""

    def __init__(self, reason: ProjectPathViolation, message: str) -> None:
        self.reason = reason
        super().__init__(message)


def project_relative_path(
    value: str,
    *,
    allow_root: bool = True,
) -> PurePosixPath:
    """Return one normalized public path without changing caller input."""
    if value in {"", "."}:
        if allow_root:
            return PurePosixPath()
        raise ProjectPathError(
            ProjectPathViolation.INVALID,
            "path must identify a project entry",
        )
    if "\\" in value or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    ):
        raise ProjectPathError(
            ProjectPathViolation.INVALID,
            "path contains unsupported characters",
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ProjectPathError(
            ProjectPathViolation.INVALID,
            "path must be normalized and project-relative",
        )
    if any(part.casefold() in RESERVED_PROJECT_COMPONENTS for part in path.parts):
        raise ProjectPathError(
            ProjectPathViolation.RESERVED,
            "private project state is not available",
        )
    return path


__all__ = [
    "RESERVED_PROJECT_COMPONENTS",
    "ProjectPathError",
    "ProjectPathViolation",
    "project_relative_path",
]
