# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Bounded read-only inspection of the active Heartwood project."""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
from codecs import getincrementaldecoder
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from heapq import nsmallest
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from openhands.sdk.git.models import GitChange, GitDiff

from heartwood.gateway._project import ProjectContext
from heartwood.gateway._session_projection import SessionProjection
from heartwood.gateway._workspace_paths import (
    RESERVED_PROJECT_COMPONENTS,
    ProjectPathError,
    ProjectPathViolation,
    project_relative_path,
)
from heartwood.schemas import (
    WorkspaceChangeResponse,
    WorkspaceChangesResponse,
    WorkspaceDiffResponse,
    WorkspaceFileResponse,
    WorkspaceLimitsResponse,
    WorkspaceTreeEntryResponse,
    WorkspaceTreeResponse,
    api_response,
)


class WorkspaceInspectionError(ValueError):
    """Raised when a workspace request violates the project read boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


_GIT_ENVIRONMENT_LOCK = RLock()
_SAFE_GIT_ENVIRONMENT = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
}


class _OpenHandsWorkspace(Protocol):
    def git_changes(self, path: str | Path) -> list[GitChange]:
        """Return changed paths using OpenHands workspace semantics."""

    def git_diff(self, path: str | Path) -> GitDiff:
        """Return one file diff using OpenHands workspace semantics."""


@dataclass(frozen=True, slots=True)
class WorkspaceLimits:
    """Resource limits applied to every workspace response."""

    max_tree_entries: int = 2_000
    max_tree_depth: int = 8
    max_file_bytes: int = 512 * 1024
    max_file_lines: int = 10_000
    max_change_entries: int = 500
    max_diff_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")

    def response(self) -> WorkspaceLimitsResponse:
        """Return the public representation of these limits."""
        return {
            "max_tree_entries": self.max_tree_entries,
            "max_tree_depth": self.max_tree_depth,
            "max_file_bytes": self.max_file_bytes,
            "max_file_lines": self.max_file_lines,
            "max_change_entries": self.max_change_entries,
            "max_diff_bytes": self.max_diff_bytes,
        }


@dataclass(frozen=True, slots=True)
class _GitBaseline:
    status: Literal["available", "binary", "truncated", "unsupported", "unavailable"]
    content: str | None = None


type _WorkspaceFileStatus = Literal[
    "available",
    "binary",
    "truncated",
    "unavailable",
    "unsupported",
]
type _WorkspaceDiffStatus = Literal[
    "available",
    "binary",
    "truncated",
    "unavailable",
    "non-git",
    "unsupported",
]
type _WorkspaceDiffSource = Literal["git", "session-action", "unavailable"]


class WorkspaceInspector:
    """Expose one project through bounded, typed, read-only operations."""

    def __init__(
        self,
        project: ProjectContext,
        *,
        workspace: _OpenHandsWorkspace | None = None,
        limits: WorkspaceLimits | None = None,
    ) -> None:
        self.project = project
        self._workspace_client = workspace
        self.limits = limits or WorkspaceLimits()

    def tree(self, path: str = ".", *, depth: int | None = None) -> WorkspaceTreeResponse:
        """Return a deterministic bounded tree rooted at a project-relative directory."""
        relative = _relative_path(path)
        requested_depth = self.limits.max_tree_depth if depth is None else depth
        if requested_depth < 1 or requested_depth > self.limits.max_tree_depth:
            raise WorkspaceInspectionError(
                "HW-WORKSPACE-006",
                f"tree depth must be between 1 and {self.limits.max_tree_depth}",
            )

        entries: list[WorkspaceTreeEntryResponse] = []
        truncated = False
        entry_limit_reached = False

        def append_directory(
            descriptor: int,
            relative_parts: tuple[str, ...],
            parent_depth: int,
        ) -> None:
            nonlocal entry_limit_reached, truncated
            try:
                remaining = self.limits.max_tree_entries - len(entries)
                children, directory_truncated = _bounded_directory_entries(
                    descriptor,
                    limit=remaining,
                )
            except OSError:
                raise WorkspaceInspectionError(
                    "HW-WORKSPACE-005",
                    "directory contents are unavailable",
                ) from None
            if directory_truncated:
                truncated = True
            for child in children:
                if entry_limit_reached:
                    return
                if child.name.casefold() in RESERVED_PROJECT_COMPONENTS:
                    continue
                if len(entries) >= self.limits.max_tree_entries:
                    truncated = True
                    entry_limit_reached = True
                    return
                child_depth = parent_depth + 1
                child_parts = (*relative_parts, child.name)
                relative_child = PurePosixPath(*child_parts).as_posix()
                try:
                    project_relative_path(relative_child, allow_root=False)
                except ProjectPathError:
                    truncated = True
                    continue
                try:
                    metadata = child.stat(follow_symlinks=False)
                except OSError:
                    entries.append(
                        {
                            "path": relative_child,
                            "name": child.name,
                            "kind": "unsupported",
                            "depth": child_depth,
                            "size_bytes": None,
                        }
                    )
                    continue
                descend = False
                if stat.S_ISLNK(metadata.st_mode):
                    kind: Literal["directory", "file", "unsupported"] = "unsupported"
                    size = None
                elif stat.S_ISDIR(metadata.st_mode):
                    kind = "directory"
                    size = None
                    descend = child_depth < requested_depth
                elif stat.S_ISREG(metadata.st_mode):
                    kind = "file"
                    size = metadata.st_size
                else:
                    kind = "unsupported"
                    size = None
                entry: WorkspaceTreeEntryResponse = {
                    "path": relative_child,
                    "name": child.name,
                    "kind": kind,
                    "depth": child_depth,
                    "size_bytes": size,
                }
                entries.append(entry)
                if kind != "directory":
                    continue
                try:
                    child_descriptor = _open_child_directory(descriptor, child.name)
                except WorkspaceInspectionError:
                    entry["kind"] = "unsupported"
                    continue
                try:
                    if descend:
                        try:
                            append_directory(child_descriptor, child_parts, child_depth)
                        except WorkspaceInspectionError:
                            entry["kind"] = "unsupported"
                    else:
                        try:
                            if _directory_has_public_entry(child_descriptor):
                                truncated = True
                        except OSError:
                            entry["kind"] = "unsupported"
                finally:
                    os.close(child_descriptor)
            if directory_truncated:
                entry_limit_reached = True

        with self._open_directory(relative) as descriptor:
            append_directory(descriptor, relative.parts, 0)

        return api_response(
            WorkspaceTreeResponse,
            {
                "schema_version": "heartwood.workspace-tree.v1",
                "path": _display_path(relative),
                "status": "truncated" if truncated else "available",
                "entries": entries,
                "truncated": truncated,
                "limits": self.limits.response(),
            },
        )

    def file(self, path: str) -> WorkspaceFileResponse:
        """Return a bounded UTF-8 text file without following symbolic links."""
        relative = _relative_path(path)
        display_path = _display_path(relative)
        if not relative.parts:
            return self._file_response(
                path=display_path,
                status="unsupported",
                size_bytes=self.project.root.stat().st_size,
                message="Only regular text files can be inspected.",
            )
        with self._open_parent(relative) as (parent_descriptor, name):
            try:
                metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return self._file_response(
                    path=display_path,
                    status="unavailable",
                    message="File does not exist.",
                )
            except OSError:
                return self._file_response(
                    path=display_path,
                    status="unavailable",
                    message="File metadata is unavailable.",
                )
            if stat.S_ISLNK(metadata.st_mode):
                raise WorkspaceInspectionError(
                    "HW-WORKSPACE-003",
                    "symbolic links are not available through workspace inspection",
                )
            if not stat.S_ISREG(metadata.st_mode):
                return self._file_response(
                    path=display_path,
                    status="unsupported",
                    size_bytes=metadata.st_size,
                    message="Only regular text files can be inspected.",
                )
            try:
                descriptor = os.open(
                    name,
                    _file_open_flags(),
                    dir_fd=parent_descriptor,
                )
            except OSError:
                return self._file_response(
                    path=display_path,
                    status="unavailable",
                    message="File could not be opened.",
                )
            try:
                opened_metadata = os.fstat(descriptor)
                if not stat.S_ISREG(opened_metadata.st_mode):
                    return self._file_response(
                        path=display_path,
                        status="unsupported",
                        message="Only regular text files can be inspected.",
                    )
                return self._read_regular_file(
                    descriptor,
                    metadata=opened_metadata,
                    path=display_path,
                )
            finally:
                os.close(descriptor)

    def changes(self, projection: SessionProjection) -> WorkspaceChangesResponse:
        """Return Git changes or explicit structured session-derived changes."""
        git_error, repository_error = _git_error_types()
        try:
            with _openhands_git_context():
                git_changes = self._openhands_workspace().git_changes(".")
        except repository_error:
            return self._session_changes(projection)
        except (git_error, OSError):
            return api_response(
                WorkspaceChangesResponse,
                {
                    "schema_version": "heartwood.workspace-changes.v1",
                    "status": "unavailable",
                    "source": "unavailable",
                    "changes": [],
                    "truncated": False,
                    "message": "OpenHands could not inspect Git changes for this project.",
                    "limits": self.limits.response(),
                },
            )

        changes: list[WorkspaceChangeResponse] = []
        unsafe_path = False
        for change in git_changes:
            path = self._safe_sdk_path(change.path)
            if path is None:
                unsafe_path = True
                continue
            changes.append(
                {
                    "path": path,
                    "status": _git_status(change),
                    "source": "git",
                    "action_ids": [],
                }
            )
        changes = _deduplicate_changes(changes)
        truncated = len(changes) > self.limits.max_change_entries
        changes = changes[: self.limits.max_change_entries]
        status = "truncated" if truncated else ("unsupported" if unsafe_path else "available")
        message = (
            "Some OpenHands Git results were excluded because their paths were unsafe."
            if unsafe_path
            else None
        )
        return api_response(
            WorkspaceChangesResponse,
            {
                "schema_version": "heartwood.workspace-changes.v1",
                "status": status,
                "source": "git",
                "changes": changes,
                "truncated": truncated,
                "message": message,
                "limits": self.limits.response(),
            },
        )

    def diff(self, projection: SessionProjection, path: str) -> WorkspaceDiffResponse:
        """Return a bounded Git diff or an explicit non-Git current-file view."""
        relative = _relative_path(path)
        display_path = _display_path(relative)
        changes = self.changes(projection)
        change = next((item for item in changes["changes"] if item["path"] == display_path), None)
        if change is None:
            return self._diff_response(
                path=display_path,
                status="unavailable",
                message="The path is not present in the bounded changed-file list.",
            )
        if change["status"] == "deleted":
            return self._diff_response(
                path=display_path,
                status=("non-git" if changes["source"] == "session-actions" else "unsupported"),
                source=("session-action" if changes["source"] == "session-actions" else "git"),
                message=(
                    "No Git baseline is available for this session-derived deletion."
                    if changes["source"] == "session-actions"
                    else "The pinned OpenHands workspace API cannot render deleted files."
                ),
            )
        if changes["source"] == "session-actions":
            current = self.file(display_path)
            if current["status"] == "binary":
                return self._diff_response(
                    path=display_path,
                    status="binary",
                    source="session-action",
                    message=current["message"],
                )
            if current["content"] is None:
                return self._diff_response(
                    path=display_path,
                    status="unsupported",
                    source="session-action",
                    message=current["message"],
                )
            return self._diff_response(
                path=display_path,
                status="non-git",
                source="session-action",
                modified=current["content"],
                truncated=current["truncated"],
                message="No version-control baseline is available; showing the current file.",
            )

        current_before = self.file(display_path)
        if current_before["status"] == "binary":
            return self._diff_response(
                path=display_path,
                status="binary",
                source="git",
                message=current_before["message"],
            )
        if current_before["content"] is None:
            return self._diff_response(
                path=display_path,
                status="unavailable",
                source="git",
                message="Changed file is unavailable.",
            )
        if current_before["truncated"]:
            return self._diff_response(
                path=display_path,
                status="truncated",
                source="git",
                modified=current_before["content"],
                truncated=True,
                message=(
                    "The current file exceeds Heartwood's secure comparison limit; "
                    "showing its bounded contents without a Git baseline."
                ),
            )
        baseline = self._git_baseline(
            relative,
            allow_missing=change["status"] == "added",
        )
        if baseline.status == "binary":
            return self._diff_response(
                path=display_path,
                status="binary",
                source="git",
                message="Binary files are not displayed.",
            )
        if baseline.status == "truncated":
            return self._diff_response(
                path=display_path,
                status="truncated",
                source="git",
                modified=current_before["content"],
                truncated=True,
                message=(
                    "The Git baseline exceeds Heartwood's secure comparison limit; "
                    "showing the bounded current file without the baseline."
                ),
            )
        if baseline.content is None:
            return self._diff_response(
                path=display_path,
                status=baseline.status,
                source="git",
                message="The Git baseline is unavailable through the project boundary.",
            )
        git_error, _ = _git_error_types()
        try:
            with _openhands_git_context():
                diff = self._openhands_workspace().git_diff(display_path)
        except (git_error, OSError):
            return self._diff_response(
                path=display_path,
                status="unavailable",
                source="git",
                message="OpenHands could not produce a diff for this file.",
            )
        current_after = self.file(display_path)
        if (
            current_after["status"] != "available"
            or current_after["content"] != current_before["content"]
            or _openhands_current_text(current_before["content"]) != diff.modified
            or _openhands_original_text(baseline.content) != diff.original
        ):
            return self._diff_response(
                path=display_path,
                status="unavailable",
                source="git",
                message="Changed file changed or became unavailable during inspection.",
            )
        original = baseline.content
        modified = current_after["content"]
        if _contains_binary_marker(original) or _contains_binary_marker(modified):
            return self._diff_response(
                path=display_path,
                status="binary",
                source="git",
                message="Binary files are not displayed.",
            )
        bounded_original, original_truncated = _bounded_text(
            original,
            self.limits.max_diff_bytes // 2,
        )
        bounded_modified, modified_truncated = _bounded_text(
            modified,
            self.limits.max_diff_bytes // 2,
        )
        truncated = original_truncated or modified_truncated
        return self._diff_response(
            path=display_path,
            status="truncated" if truncated else "available",
            source="git",
            original=bounded_original,
            modified=bounded_modified,
            truncated=truncated,
            message="Diff content was truncated by Heartwood." if truncated else None,
        )

    def _git_baseline(
        self,
        relative: PurePosixPath,
        *,
        allow_missing: bool,
    ) -> _GitBaseline:
        """Read the SDK-selected Git baseline through an anchored project descriptor."""
        from openhands.sdk.git.utils import get_valid_ref

        git_error, _ = _git_error_types()
        try:
            with _openhands_git_context():
                reference = get_valid_ref(self.project.root, purpose="display")
        except (git_error, OSError):
            return _GitBaseline(status="unavailable")
        if reference is None or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", reference) is None:
            return _GitBaseline(status="unavailable")

        spec = f"{reference}:./{relative.as_posix()}"
        with self._open_directory(PurePosixPath()) as project_descriptor:
            size_result = _run_anchored_git(
                project_descriptor,
                "cat-file",
                "-s",
                spec,
            )
            if size_result is None:
                return (
                    _GitBaseline(status="available", content="")
                    if allow_missing
                    else _GitBaseline(status="unavailable")
                )
            try:
                size = int(size_result.decode("ascii").strip())
            except (UnicodeDecodeError, ValueError):
                return _GitBaseline(status="unavailable")
            limit = self.limits.max_diff_bytes // 2
            if size > limit:
                return _GitBaseline(status="truncated")
            content_result = _run_anchored_git(
                project_descriptor,
                "cat-file",
                "blob",
                spec,
            )
        if content_result is None or len(content_result) != size:
            return _GitBaseline(status="unavailable")
        if b"\x00" in content_result:
            return _GitBaseline(status="binary")
        try:
            content = content_result.decode("utf-8")
        except UnicodeDecodeError:
            return _GitBaseline(status="unsupported")
        return _GitBaseline(status="available", content=content)

    def _openhands_workspace(self) -> _OpenHandsWorkspace:
        if self._workspace_client is None:
            from openhands.sdk.workspace import LocalWorkspace

            self._workspace_client = LocalWorkspace(working_dir=self.project.root)
        return self._workspace_client

    def _safe_sdk_path(self, value: Path) -> str | None:
        try:
            relative = _relative_path(value.as_posix())
            if not self._safe_path(relative, allow_missing_final=True):
                return None
            return _display_path(relative)
        except WorkspaceInspectionError:
            return None

    @contextmanager
    def _open_directory(self, relative: PurePosixPath) -> Iterator[int]:
        descriptor = os.open(self.project.root, _directory_open_flags())
        try:
            for part in relative.parts:
                child_descriptor = _open_child_directory(descriptor, part)
                os.close(descriptor)
                descriptor = child_descriptor
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _open_parent(self, relative: PurePosixPath) -> Iterator[tuple[int, str]]:
        if not relative.parts:
            raise WorkspaceInspectionError(
                "HW-WORKSPACE-004",
                "path must identify a project file",
            )
        parent = PurePosixPath(*relative.parts[:-1])
        with self._open_directory(parent) as descriptor:
            yield descriptor, relative.parts[-1]

    def _safe_path(
        self,
        relative: PurePosixPath,
        *,
        allow_missing_final: bool,
    ) -> bool:
        try:
            with self._open_parent(relative) as (parent_descriptor, name):
                try:
                    metadata = os.stat(
                        name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return allow_missing_final
                if stat.S_ISLNK(metadata.st_mode):
                    return False
                return stat.S_ISREG(metadata.st_mode)
        except WorkspaceInspectionError:
            return False

    def _read_regular_file(
        self,
        descriptor: int,
        *,
        metadata: os.stat_result,
        path: str,
    ) -> WorkspaceFileResponse:
        raw = _read_at_most(descriptor, self.limits.max_file_bytes + 1)

        oversized = (
            metadata.st_size > self.limits.max_file_bytes or len(raw) > self.limits.max_file_bytes
        )
        raw = raw[: self.limits.max_file_bytes]
        if b"\x00" in raw:
            return self._file_response(
                path=path,
                status="binary",
                size_bytes=metadata.st_size,
                bytes_read=len(raw),
                message="Binary files are not displayed.",
            )
        try:
            decoder = getincrementaldecoder("utf-8")("strict")
            text = decoder.decode(raw, final=not oversized)
        except UnicodeDecodeError:
            return self._file_response(
                path=path,
                status="unsupported",
                size_bytes=metadata.st_size,
                bytes_read=len(raw),
                message="Only UTF-8 text files are displayed.",
            )
        lines = text.splitlines(keepends=True)
        too_many_lines = len(lines) > self.limits.max_file_lines
        if too_many_lines:
            lines = lines[: self.limits.max_file_lines]
            text = "".join(lines)
        truncated = oversized or too_many_lines
        return self._file_response(
            path=path,
            status="truncated" if truncated else "available",
            content=text,
            size_bytes=metadata.st_size,
            bytes_read=len(raw),
            line_count=len(lines),
            truncated=truncated,
            message="File content was truncated by Heartwood." if truncated else None,
        )

    def _session_changes(self, projection: SessionProjection) -> WorkspaceChangesResponse:
        by_path: dict[str, WorkspaceChangeResponse] = {}
        for action in projection.actions:
            if action.state != "succeeded":
                continue
            for affected in action.affected_paths:
                if affected.effect == "unknown":
                    by_path.pop(affected.path, None)
                    continue
                status: Literal["added", "deleted", "modified"]
                if affected.effect == "created":
                    status = "added"
                elif affected.effect == "deleted":
                    status = "deleted"
                else:
                    status = "modified"
                current = by_path.get(affected.path)
                action_ids = [] if current is None else list(current["action_ids"])
                if action.action_id is not None:
                    action_ids.append(action.action_id)
                by_path[affected.path] = {
                    "path": affected.path,
                    "status": (
                        "added" if current is not None and current["status"] == "added" else status
                    ),
                    "source": "session-action",
                    "action_ids": list(dict.fromkeys(action_ids)),
                }
        changes = sorted(by_path.values(), key=lambda item: item["path"])
        truncated = len(changes) > self.limits.max_change_entries
        return api_response(
            WorkspaceChangesResponse,
            {
                "schema_version": "heartwood.workspace-changes.v1",
                "status": "truncated" if truncated else "non-git",
                "source": "session-actions",
                "changes": changes[: self.limits.max_change_entries],
                "truncated": truncated,
                "message": (
                    "This project is not a Git repository. Changes include only successful "
                    "typed file-editor actions from this session."
                ),
                "limits": self.limits.response(),
            },
        )

    @staticmethod
    def _file_response(
        *,
        path: str,
        status: _WorkspaceFileStatus,
        content: str | None = None,
        size_bytes: int | None = None,
        bytes_read: int = 0,
        line_count: int = 0,
        truncated: bool = False,
        message: str | None = None,
    ) -> WorkspaceFileResponse:
        return api_response(
            WorkspaceFileResponse,
            {
                "schema_version": "heartwood.workspace-file.v1",
                "path": path,
                "status": status,
                "content": content,
                "size_bytes": size_bytes,
                "bytes_read": bytes_read,
                "line_count": line_count,
                "truncated": truncated,
                "message": message,
            },
        )

    @staticmethod
    def _diff_response(
        *,
        path: str,
        status: _WorkspaceDiffStatus,
        source: _WorkspaceDiffSource = "unavailable",
        original: str | None = None,
        modified: str | None = None,
        truncated: bool = False,
        message: str | None = None,
    ) -> WorkspaceDiffResponse:
        return api_response(
            WorkspaceDiffResponse,
            {
                "schema_version": "heartwood.workspace-diff.v1",
                "path": path,
                "status": status,
                "source": source,
                "original": original,
                "modified": modified,
                "truncated": truncated,
                "message": message,
            },
        )


def _relative_path(value: str) -> PurePosixPath:
    try:
        return project_relative_path(value)
    except ProjectPathError as error:
        code = (
            "HW-WORKSPACE-002"
            if error.reason == ProjectPathViolation.RESERVED
            else "HW-WORKSPACE-001"
        )
        raise WorkspaceInspectionError(
            code,
            str(error),
        ) from error


def _display_path(path: PurePosixPath) -> str:
    value = path.as_posix()
    return "." if value in {"", "."} else value


def _git_status(change: GitChange) -> Literal["added", "deleted", "modified"]:
    if change.status.value == "ADDED":
        return "added"
    if change.status.value == "DELETED":
        return "deleted"
    return "modified"


def _deduplicate_changes(
    changes: list[WorkspaceChangeResponse],
) -> list[WorkspaceChangeResponse]:
    by_identity: dict[str, WorkspaceChangeResponse] = {}
    for change in changes:
        by_identity[change["path"]] = change
    return sorted(by_identity.values(), key=lambda item: item["path"])


def _contains_binary_marker(value: str | None) -> bool:
    return value is not None and "\x00" in value


def _openhands_current_text(value: str) -> str:
    """Match the public OpenHands Git workspace normalization for comparison."""
    return "\n".join(value.splitlines())


def _openhands_original_text(value: str) -> str:
    """Match OpenHands command-output normalization without changing displayed content."""
    return value.strip()


def _bounded_text(value: str | None, limit: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _git_error_types() -> tuple[type[Exception], type[Exception]]:
    from openhands.sdk.git.exceptions import GitError, GitRepositoryError

    return GitError, GitRepositoryError


def _run_anchored_git(project_descriptor: int, *arguments: str) -> bytes | None:
    """Run one local Git object query from a descriptor-verified project root."""
    if os.name != "posix":
        return None
    directory = _descriptor_directory_path(project_descriptor)
    if directory is None or not _descriptor_matches_path(project_descriptor, directory):
        return None
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(_SAFE_GIT_ENVIRONMENT)
    environment["LC_ALL"] = "C"
    try:
        completed = subprocess.run(
            ["git", "--no-pager", *arguments],
            cwd=directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            env=environment,
            pass_fds=(project_descriptor,),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not _descriptor_matches_path(project_descriptor, directory):
        return None
    return completed.stdout


@contextmanager
def _openhands_git_context() -> Iterator[None]:
    """Scope process settings required by the public OpenHands Git API."""
    with _GIT_ENVIRONMENT_LOCK:
        # The pinned SDK has no per-call environment or logger arguments. Keep
        # this compatibility scope serialized and restore every process value.
        original = {key: value for key, value in os.environ.items() if key.startswith("GIT_")}
        sdk_logger = logging.getLogger("openhands.sdk.git")
        original_log_level = sdk_logger.level
        for key in tuple(original):
            os.environ.pop(key, None)
        os.environ.update(_SAFE_GIT_ENVIRONMENT)
        sdk_logger.setLevel(logging.CRITICAL)
        try:
            yield
        finally:
            for key in tuple(os.environ):
                if key.startswith("GIT_"):
                    os.environ.pop(key, None)
            os.environ.update(original)
            sdk_logger.setLevel(original_log_level)


def _descriptor_directory_path(descriptor: int) -> str | None:
    proc_path = f"/proc/self/fd/{descriptor}"
    try:
        if stat.S_ISDIR(Path(proc_path).stat().st_mode):
            return proc_path
    except OSError:
        # macOS does not expose /proc, so continue with its descriptor API.
        pass
    try:
        import fcntl

        get_path = getattr(fcntl, "F_GETPATH", None)
        if get_path is None:
            return None
        value = fcntl.fcntl(descriptor, get_path, b"\0" * 1024)
    except (ImportError, OSError, ValueError):
        return None
    encoded = bytes(value).split(b"\0", 1)[0]
    try:
        return os.fsdecode(encoded)
    except UnicodeDecodeError:
        return None


def _descriptor_matches_path(descriptor: int, path: str) -> bool:
    try:
        return os.path.samestat(
            os.fstat(descriptor),
            Path(path).stat(),
        )
    except OSError:
        return False


def _read_at_most(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _file_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _open_child_directory(parent_descriptor: int, name: str) -> int:
    try:
        metadata = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as error:
        raise WorkspaceInspectionError(
            "HW-WORKSPACE-005",
            "directory does not exist",
        ) from error
    except OSError as error:
        raise WorkspaceInspectionError(
            "HW-WORKSPACE-005",
            "directory metadata is unavailable",
        ) from error
    if stat.S_ISLNK(metadata.st_mode):
        raise WorkspaceInspectionError(
            "HW-WORKSPACE-003",
            "symbolic links are not available through workspace inspection",
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceInspectionError(
            "HW-WORKSPACE-004",
            "path is not a directory",
        )
    try:
        descriptor = os.open(
            name,
            _directory_open_flags(),
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise WorkspaceInspectionError(
            "HW-WORKSPACE-005",
            "directory could not be opened",
        ) from error
    if stat.S_ISDIR(os.fstat(descriptor).st_mode):
        return descriptor
    os.close(descriptor)
    raise WorkspaceInspectionError(
        "HW-WORKSPACE-004",
        "path is not a directory",
    )


def _bounded_directory_entries(
    descriptor: int,
    *,
    limit: int,
) -> tuple[list[os.DirEntry[str]], bool]:
    with os.scandir(descriptor) as iterator:
        entries = nsmallest(
            limit + 1,
            (
                entry
                for entry in iterator
                if entry.name.casefold() not in RESERVED_PROJECT_COMPONENTS
            ),
            key=lambda entry: entry.name,
        )
    return entries[:limit], len(entries) > limit


def _directory_has_public_entry(descriptor: int) -> bool:
    with os.scandir(descriptor) as iterator:
        return any(entry.name.casefold() not in RESERVED_PROJECT_COMPONENTS for entry in iterator)


__all__ = [
    "WorkspaceInspectionError",
    "WorkspaceInspector",
    "WorkspaceLimits",
]
