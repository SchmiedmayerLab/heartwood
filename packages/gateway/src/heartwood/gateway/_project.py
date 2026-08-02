# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Project-root and project-local state ownership."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from heartwood.persistence import (
    PERSISTENCE_MIGRATIONS,
    PROJECT_STATE_FORMATS,
    PROJECT_STATE_KIND,
    PROJECT_STATE_VERSION,
    MigrationError,
    NativeLockUnavailableError,
    native_file_lock,
    read_private_json,
    read_private_text,
    write_private_json_atomic,
    write_private_text_atomic,
)

_STATE_DIRECTORIES = ("sessions", "models", "skills", "audit", "runtime", "logs", "cache")


class ProjectStateError(ValueError):
    """Raised when a project or its Heartwood state boundary is invalid."""


@dataclass(frozen=True, slots=True)
class ProjectContext:
    """One invocation directory and its reserved Heartwood state paths."""

    root: Path

    def __post_init__(self) -> None:
        resolved = self.root.expanduser().resolve()
        if not resolved.is_dir():
            raise ProjectStateError(f"project directory does not exist: {resolved}")
        object.__setattr__(self, "root", resolved)

    @classmethod
    def current(cls) -> ProjectContext:
        """Bind Heartwood to the process working directory."""
        return cls(Path.cwd())

    @property
    def state_root(self) -> Path:
        """Return the reserved project-local Heartwood directory."""
        return self.root / ".heartwood"

    @property
    def config_path(self) -> Path:
        """Return the user-facing project configuration path."""
        return self.state_root / "config.toml"

    @property
    def config_lock_path(self) -> Path:
        """Return the project-scoped interprocess configuration lock path."""
        return self.state_root / ".config.lock"

    @property
    def state_path(self) -> Path:
        """Return the internal state-schema marker path."""
        return self.state_root / "state.json"

    @property
    def state_lock_path(self) -> Path:
        """Return the project-state initialization and migration lock path."""
        return self.state_root / ".state.lock"

    @property
    def sessions_dir(self) -> Path:
        """Return the persisted session directory."""
        return self.state_root / "sessions"

    @property
    def models_dir(self) -> Path:
        """Return the project model-artifact directory."""
        return self.state_root / "models"

    @property
    def skills_dir(self) -> Path:
        """Return the installed Skill directory."""
        return self.state_root / "skills"

    @property
    def audit_dir(self) -> Path:
        """Return the project audit-artifact directory."""
        return self.state_root / "audit"

    @property
    def runtime_dir(self) -> Path:
        """Return the local-runtime state directory."""
        return self.state_root / "runtime"

    @property
    def logs_dir(self) -> Path:
        """Return the runtime log directory."""
        return self.state_root / "logs"

    @property
    def cache_dir(self) -> Path:
        """Return the project-local cache directory."""
        return self.state_root / "cache"

    def initialize(self) -> None:
        """Create and validate the private project-local state structure."""
        self._validate_state_root()
        try:
            self.state_root.mkdir(mode=0o700, exist_ok=True)
            self.state_root.chmod(0o700)
            with native_file_lock(self.state_lock_path):
                self._validate_existing_state()
                for name in _STATE_DIRECTORIES:
                    directory = self.state_root / name
                    if directory.is_symlink():
                        raise ProjectStateError(
                            f"Heartwood state directory must not be a symlink: {name}"
                        )
                    directory.mkdir(mode=0o700, exist_ok=True)
                    directory.chmod(0o700)
                ignore_path = self.state_root / ".gitignore"
                if not ignore_path.exists():
                    write_private_text_atomic(ignore_path, "*\n")
                if not self.state_path.exists():
                    write_private_json_atomic(self.state_path, _current_state_payload())
                else:
                    self._migrate_state_marker()
                self._validate_existing_state()
                ignore_path.chmod(0o600)
                self.state_path.chmod(0o600)
                if self.config_path.exists():
                    self.config_path.chmod(0o600)
                if self.config_lock_path.exists():
                    self.config_lock_path.chmod(0o600)
        except (OSError, ValueError) as error:
            if isinstance(error, (NativeLockUnavailableError, ProjectStateError)):
                raise
            raise ProjectStateError(f"unable to initialize .heartwood state: {error}") from error

    def state_exists(self) -> bool:
        """Return whether a valid initialized state structure exists."""
        if not self.state_root.exists():
            return False
        self._validate_existing_state()
        return self.state_path.is_file()

    def contains(self, path: Path, *, include_state: bool = False) -> bool:
        """Return whether a path resolves within the project boundary."""
        resolved = self._resolve(path)
        if resolved != self.root and self.root not in resolved.parents:
            return False
        return include_state or (
            resolved != self.state_root and self.state_root not in resolved.parents
        )

    def require_project_path(self, path: Path, *, include_state: bool = False) -> Path:
        """Resolve a path or reject project escape and reserved state access."""
        resolved = self._resolve(path)
        if not self.contains(resolved, include_state=include_state):
            boundary = "project and outside .heartwood" if not include_state else "project"
            raise ProjectStateError(f"path must remain inside the {boundary}: {path}")
        return resolved

    def _resolve(self, path: Path) -> Path:
        expanded = path.expanduser()
        return (expanded if expanded.is_absolute() else self.root / expanded).resolve()

    def _validate_existing_state(self) -> None:
        self._validate_state_root()
        if not self.state_root.exists():
            return
        if not self.state_path.exists():
            entries = tuple(
                entry for entry in self.state_root.iterdir() if entry != self.state_lock_path
            )
            models = self.models_dir
            fresh_model_mount = entries == (models,) and models.is_dir() and not models.is_symlink()
            if entries and not fresh_model_mount:
                raise ProjectStateError(
                    "incompatible .heartwood layout: no project-state marker is present"
                )
            return
        if self.state_path.is_symlink() or not self.state_path.is_file():
            raise ProjectStateError(".heartwood/state.json must be a regular file")
        try:
            value = read_private_json(self.state_path)
            migrated = PERSISTENCE_MIGRATIONS.migrate(
                PROJECT_STATE_KIND,
                value,
            )
        except (OSError, ValueError, MigrationError) as error:
            raise ProjectStateError(f"unable to read .heartwood/state.json: {error}") from error
        if migrated.payload != _current_state_payload():
            raise ProjectStateError("unsupported .heartwood state schema")
        for name in _STATE_DIRECTORIES:
            directory = self.state_root / name
            if directory.is_symlink() or not directory.is_dir():
                raise ProjectStateError(
                    f"incompatible .heartwood layout: {name} must be a regular directory"
                )
        ignore_path = self.state_root / ".gitignore"
        if ignore_path.is_symlink() or not ignore_path.is_file():
            raise ProjectStateError(
                "incompatible .heartwood layout: the internal Git ignore rule is missing"
            )
        try:
            ignore_rule = read_private_text(ignore_path)
        except (OSError, ValueError) as error:
            raise ProjectStateError(f"unable to read .heartwood/.gitignore: {error}") from error
        if ignore_rule != "*\n":
            raise ProjectStateError(
                "incompatible .heartwood layout: the internal Git ignore rule is invalid"
            )
        if self.config_path.exists() and (
            self.config_path.is_symlink() or not self.config_path.is_file()
        ):
            raise ProjectStateError(".heartwood/config.toml must be a regular file")
        if self.config_lock_path.is_symlink() or (
            self.config_lock_path.exists() and not self.config_lock_path.is_file()
        ):
            raise ProjectStateError(".heartwood/.config.lock must be a regular file")

        if self.state_lock_path.is_symlink() or (
            self.state_lock_path.exists() and not self.state_lock_path.is_file()
        ):
            raise ProjectStateError(".heartwood/.state.lock must be a regular file")

    def _validate_state_root(self) -> None:
        if self.state_root.is_symlink():
            raise ProjectStateError(".heartwood must not be a symbolic link")
        if not self.state_root.exists():
            return
        if not self.state_root.is_dir():
            raise ProjectStateError(".heartwood must be a directory")

    def _migrate_state_marker(self) -> None:
        try:
            current = read_private_json(self.state_path)
            result = PERSISTENCE_MIGRATIONS.migrate(
                PROJECT_STATE_KIND,
                current,
            )
        except (OSError, ValueError, MigrationError) as error:
            raise ProjectStateError(f"unable to migrate .heartwood/state.json: {error}") from error
        if result.applied_versions:
            write_private_json_atomic(self.state_path, result.payload)


def _current_state_payload() -> dict[str, object]:
    return {
        "schema_version": PROJECT_STATE_VERSION,
        "formats": dict(PROJECT_STATE_FORMATS),
    }
