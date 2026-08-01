# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest
from openhands.sdk.git.exceptions import GitRepositoryError
from openhands.sdk.git.models import GitChange, GitChangeStatus, GitDiff
from openhands.sdk.workspace import LocalWorkspace

import heartwood.gateway._workspace as workspace_module
from heartwood.core_adapter import SessionResult
from heartwood.gateway import (
    ProjectContext,
    ProjectionActionRecord,
    ProjectionAffectedPath,
    ProjectionFileEditorActionDetails,
    RestGateway,
    RestRequest,
    SessionGateway,
    SessionProjection,
    WorkspaceInspectionError,
    WorkspaceInspector,
    WorkspaceLimits,
    project_session,
)
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand, SessionEvent


@dataclass
class _WorkspaceFixture:
    changes: list[GitChange] | Exception = field(default_factory=list)
    diffs: dict[str, GitDiff] = field(default_factory=dict)
    requested_change_paths: list[str] = field(default_factory=list)
    requested_diff_paths: list[str] = field(default_factory=list)

    def git_changes(self, path: str | Path) -> list[GitChange]:
        self.requested_change_paths.append(str(path))
        if isinstance(self.changes, Exception):
            raise self.changes
        return self.changes

    def git_diff(self, path: str | Path) -> GitDiff:
        normalized = str(path)
        self.requested_diff_paths.append(normalized)
        return self.diffs[normalized]


@dataclass(frozen=True)
class _SyntheticDirectoryEntry:
    name: str


@dataclass(frozen=True)
class _UnreadableDirectoryEntry:
    name: str

    def stat(self, *, follow_symlinks: bool) -> os.stat_result:
        assert follow_symlinks is False
        raise PermissionError("synthetic unreadable entry")


class _BoundedDirectoryScan:
    def __init__(self, names: list[str]) -> None:
        self._names = iter(names)
        self.iterated = 0

    def __enter__(self) -> _BoundedDirectoryScan:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def __iter__(self) -> _BoundedDirectoryScan:
        return self

    def __next__(self) -> _SyntheticDirectoryEntry:
        name = next(self._names)
        self.iterated += 1
        return _SyntheticDirectoryEntry(name)


@dataclass
class _ReconcilingWorkspaceService:
    events: list[SessionEvent] = field(default_factory=list)
    started: bool = False
    reconciled: bool = False
    reconcile_calls: int = 0

    def handle(
        self,
        command: SessionCommand,
        *,
        unavailable_reason: str | None = None,
        reconcile_before_command: bool = True,
    ) -> SessionResult:
        assert unavailable_reason is None
        assert reconcile_before_command is False
        running = _event(
            len(self.events),
            EventKind.AGENT_LIFECYCLE_UPDATED,
            {"status": "running"},
        ).model_copy(update={"session_id": command.session_id})
        self.events.append(running)
        self.started = True
        return SessionResult(events=(running,))

    def replay_events(self) -> tuple[SessionEvent, ...]:
        return tuple(self.events)

    def reconcile(self) -> tuple[SessionEvent, ...]:
        self.reconcile_calls += 1
        if not self.started or self.reconciled:
            return ()
        proposed = _event(
            len(self.events),
            EventKind.TOOL_CALL_PROPOSED,
            {
                "tool_call_id": "reconciled-call",
                "action_id": "reconciled-action",
                "tool_name": "file_editor",
                "kind": "file-editor",
                "risk": "low",
                "summary": "Create reconciled.txt",
                "arguments": {"command": "create", "path": "reconciled.txt"},
                "affected_paths": ["reconciled.txt"],
            },
        )
        executed = _event(
            len(self.events) + 1,
            EventKind.TOOL_EXECUTION_RECORDED,
            {
                "tool_call_id": "reconciled-call",
                "action_id": "reconciled-action",
                "tool_name": "file_editor",
                "exit_code": 0,
            },
        )
        self.events.extend((proposed, executed))
        self.reconciled = True
        return proposed, executed

    def close(self) -> None:
        return None


def test_workspace_tree_is_bounded_and_excludes_private_state_at_every_depth(
    tmp_path: Path,
) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    (tmp_path / "analysis").mkdir()
    (tmp_path / "analysis" / "cohort.py").write_text("print('synthetic')\n", encoding="utf-8")
    (tmp_path / "analysis" / ".heartwood").mkdir()
    (tmp_path / "analysis" / ".heartwood" / "secret").write_text(
        "not public",
        encoding="utf-8",
    )
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("not public", encoding="utf-8")
    (tmp_path / "later.txt").write_text("later", encoding="utf-8")
    inspector = WorkspaceInspector(
        project,
        workspace=_WorkspaceFixture(),
        limits=WorkspaceLimits(max_tree_entries=2),
    )

    tree = inspector.tree()

    assert tree["status"] == "truncated"
    assert tree["truncated"] is True
    assert [entry["path"] for entry in tree["entries"]] == [
        "analysis",
        "analysis/cohort.py",
    ]
    assert ".heartwood" not in str(tree)
    assert ".git" not in str(tree)


@pytest.mark.parametrize("field_name", tuple(WorkspaceLimits.__dataclass_fields__))
def test_workspace_limits_require_positive_values(field_name: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        WorkspaceLimits(**{field_name: 0})


@pytest.mark.parametrize("depth", [0, 9])
def test_workspace_tree_rejects_depth_outside_configured_limits(
    tmp_path: Path,
    depth: int,
) -> None:
    inspector = WorkspaceInspector(ProjectContext(tmp_path), workspace=_WorkspaceFixture())

    with pytest.raises(WorkspaceInspectionError, match="HW-WORKSPACE-006"):
        inspector.tree(depth=depth)


def test_workspace_directory_enumeration_returns_the_deterministic_bounded_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan = _BoundedDirectoryScan(["z.txt", ".git", "a.txt", "b.txt", "c.txt"])
    monkeypatch.setattr(os, "scandir", lambda _descriptor: scan)

    entries, truncated = workspace_module._bounded_directory_entries(1, limit=2)

    assert [entry.name for entry in entries] == ["a.txt", "b.txt"]
    assert truncated is True
    assert scan.iterated == 5


def test_workspace_tree_marks_entries_with_unreadable_metadata_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable_entry(
        _descriptor: int,
        *,
        limit: int,
    ) -> tuple[list[_UnreadableDirectoryEntry], bool]:
        assert limit > 0
        return [_UnreadableDirectoryEntry("restricted.txt")], False

    monkeypatch.setattr(
        workspace_module,
        "_bounded_directory_entries",
        unreadable_entry,
    )
    inspector = WorkspaceInspector(ProjectContext(tmp_path), workspace=_WorkspaceFixture())

    tree = inspector.tree()

    assert tree["entries"] == [
        {
            "path": "restricted.txt",
            "name": "restricted.txt",
            "kind": "unsupported",
            "depth": 1,
            "size_bytes": None,
        }
    ]


def test_workspace_tree_marks_a_directory_that_cannot_be_opened_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "restricted").mkdir()
    original_open_child = workspace_module._open_child_directory

    def fail_restricted_directory(parent_descriptor: int, name: str) -> int:
        if name == "restricted":
            raise WorkspaceInspectionError(
                "HW-WORKSPACE-005",
                "directory could not be opened",
            )
        return original_open_child(parent_descriptor, name)

    monkeypatch.setattr(
        workspace_module,
        "_open_child_directory",
        fail_restricted_directory,
    )
    inspector = WorkspaceInspector(ProjectContext(tmp_path), workspace=_WorkspaceFixture())

    tree = inspector.tree()

    assert tree["entries"][0]["path"] == "restricted"
    assert tree["entries"][0]["kind"] == "unsupported"


def test_workspace_tree_marks_symlinks_and_special_files_unsupported(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("synthetic", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(target)
    if hasattr(os, "mkfifo"):
        os.mkfifo(tmp_path / "events.pipe")
    inspector = WorkspaceInspector(project, workspace=_WorkspaceFixture())

    entries = {entry["path"]: entry for entry in inspector.tree()["entries"]}

    assert entries["linked.txt"]["kind"] == "unsupported"
    if hasattr(os, "mkfifo"):
        assert entries["events.pipe"]["kind"] == "unsupported"


def test_workspace_tree_reports_depth_truncation_without_hiding_siblings(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "analysis"
    nested.mkdir()
    (nested / "hidden-at-depth.py").write_text("answer = 42\n", encoding="utf-8")
    (tmp_path / "visible-sibling.txt").write_text("synthetic\n", encoding="utf-8")
    inspector = WorkspaceInspector(
        ProjectContext(tmp_path),
        workspace=_WorkspaceFixture(),
    )

    tree = inspector.tree(depth=1)

    assert tree["status"] == "truncated"
    assert tree["truncated"] is True
    assert [entry["path"] for entry in tree["entries"]] == [
        "analysis",
        "visible-sibling.txt",
    ]


def test_workspace_tree_does_not_report_empty_boundary_directories_as_truncated(
    tmp_path: Path,
) -> None:
    (tmp_path / "empty").mkdir()
    inspector = WorkspaceInspector(
        ProjectContext(tmp_path),
        workspace=_WorkspaceFixture(),
    )

    tree = inspector.tree(depth=1)

    assert tree["status"] == "available"
    assert tree["truncated"] is False
    assert [entry["path"] for entry in tree["entries"]] == ["empty"]


def test_workspace_tree_marks_a_boundary_directory_unsupported_when_it_cannot_be_scanned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "restricted").mkdir()

    def unreadable(_descriptor: int) -> bool:
        raise PermissionError("synthetic unreadable directory")

    monkeypatch.setattr(workspace_module, "_directory_has_public_entry", unreadable)
    inspector = WorkspaceInspector(ProjectContext(tmp_path), workspace=_WorkspaceFixture())

    tree = inspector.tree(depth=1)

    assert tree["status"] == "available"
    assert tree["entries"] == [
        {
            "path": "restricted",
            "name": "restricted",
            "kind": "unsupported",
            "depth": 1,
            "size_bytes": None,
        }
    ]


def test_workspace_tree_reports_unreadable_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restricted = tmp_path / "restricted"
    restricted.mkdir()
    restricted_inode = restricted.stat().st_ino
    original_entries = workspace_module._bounded_directory_entries

    def controlled_entries(
        descriptor: int,
        *,
        limit: int,
    ) -> tuple[list[os.DirEntry[str]], bool]:
        if os.fstat(descriptor).st_ino == restricted_inode:
            raise PermissionError("synthetic unreadable directory")
        return original_entries(descriptor, limit=limit)

    monkeypatch.setattr(workspace_module, "_bounded_directory_entries", controlled_entries)
    inspector = WorkspaceInspector(
        ProjectContext(tmp_path),
        workspace=_WorkspaceFixture(),
    )

    entries = {entry["path"]: entry for entry in inspector.tree()["entries"]}

    assert entries["restricted"]["kind"] == "unsupported"
    with pytest.raises(WorkspaceInspectionError, match="HW-WORKSPACE-005"):
        inspector.tree("restricted")


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("../outside.txt", "HW-WORKSPACE-001"),
        ("/tmp/outside.txt", "HW-WORKSPACE-001"),
        ("nested\\file.txt", "HW-WORKSPACE-001"),
        ("nested//file.txt", "HW-WORKSPACE-001"),
        ("nested/./file.txt", "HW-WORKSPACE-001"),
        ("nested/file.txt/", "HW-WORKSPACE-001"),
        ("nested/\nfile.txt", "HW-WORKSPACE-001"),
        ("nested/\x1bfile.txt", "HW-WORKSPACE-001"),
        ("nested/\x9bfile.txt", "HW-WORKSPACE-001"),
        ("nested/\u202efile.txt", "HW-WORKSPACE-001"),
        ("nested/\udcfffile.txt", "HW-WORKSPACE-001"),
        (".heartwood/config.toml", "HW-WORKSPACE-002"),
        ("nested/.heartwood/config.toml", "HW-WORKSPACE-002"),
        (".HEARTWOOD/config.toml", "HW-WORKSPACE-002"),
        ("nested/.HeArTwOoD/config.toml", "HW-WORKSPACE-002"),
        (".git/config", "HW-WORKSPACE-002"),
        ("nested/.git/config", "HW-WORKSPACE-002"),
        (".GIT/config", "HW-WORKSPACE-002"),
        ("nested/.GiT/config", "HW-WORKSPACE-002"),
    ],
)
def test_workspace_rejects_unsafe_and_private_paths(
    tmp_path: Path,
    path: str,
    code: str,
) -> None:
    inspector = WorkspaceInspector(ProjectContext(tmp_path), workspace=_WorkspaceFixture())

    with pytest.raises(WorkspaceInspectionError, match=code):
        inspector.file(path)


def test_workspace_rejects_internal_and_escaping_symlinks(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    (tmp_path / "inside.txt").write_text("synthetic", encoding="utf-8")
    (tmp_path / "inside-link.txt").symlink_to(tmp_path / "inside.txt")
    (tmp_path / "outside-link.txt").symlink_to(tmp_path.parent / "outside.txt")
    (tmp_path / "broken-link.txt").symlink_to(tmp_path / "missing.txt")
    inspector = WorkspaceInspector(project, workspace=_WorkspaceFixture())

    for path in ("inside-link.txt", "outside-link.txt", "broken-link.txt"):
        with pytest.raises(WorkspaceInspectionError, match="HW-WORKSPACE-003"):
            inspector.file(path)


def test_workspace_tree_excludes_names_with_terminal_control_characters(
    tmp_path: Path,
) -> None:
    (tmp_path / "safe.txt").write_text("synthetic\n", encoding="utf-8")
    (tmp_path / "unsafe\x1b]0;spoofed\x07.txt").write_text(
        "not displayed\n",
        encoding="utf-8",
    )
    inspector = WorkspaceInspector(
        ProjectContext(tmp_path),
        workspace=_WorkspaceFixture(),
    )

    tree = inspector.tree()

    assert [entry["path"] for entry in tree["entries"]] == ["safe.txt"]
    assert tree["status"] == "truncated"
    assert "\x1b" not in str(tree)
    assert "\x07" not in str(tree)


def test_workspace_file_open_does_not_block_when_a_regular_file_becomes_a_fifo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "result.txt"
    path.write_text("synthetic\n", encoding="utf-8")
    original_open = os.open
    swapped = False

    def swapping_open(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if target == "result.txt" and dir_fd is not None and not swapped:
            assert flags & os.O_NONBLOCK
            swapped = True
            path.unlink()
            os.mkfifo(path)
        return original_open(target, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)
    inspector = WorkspaceInspector(
        ProjectContext(tmp_path),
        workspace=_WorkspaceFixture(),
    )

    result = inspector.file("result.txt")

    assert result["status"] == "unsupported"
    assert result["content"] is None


def test_workspace_parent_path_swap_cannot_redirect_a_file_read(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_directory = tmp_path / "analysis"
    project_directory.mkdir()
    (project_directory / "summary.txt").write_text("project\n", encoding="utf-8")
    outside = tmp_path_factory.mktemp("outside-workspace")
    (outside / "summary.txt").write_text("outside-secret\n", encoding="utf-8")
    moved = tmp_path / "analysis-original"
    original_stat = os.stat
    swapped = False

    def swapping_stat(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal swapped
        metadata = original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == "analysis" and dir_fd is not None and not swapped:
            swapped = True
            project_directory.rename(moved)
            project_directory.symlink_to(outside, target_is_directory=True)
        return metadata

    monkeypatch.setattr(os, "stat", swapping_stat)
    inspector = WorkspaceInspector(
        ProjectContext(tmp_path),
        workspace=_WorkspaceFixture(),
    )

    with pytest.raises(WorkspaceInspectionError, match="HW-WORKSPACE-005"):
        inspector.file("analysis/summary.txt")


def test_workspace_excludes_git_results_reached_through_directory_symlinks(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    outside = tmp_path_factory.mktemp("outside-git")
    (outside / "summary.txt").write_text("outside-secret\n", encoding="utf-8")
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    workspace = _WorkspaceFixture(
        changes=[
            GitChange(
                path=Path("linked/summary.txt"),
                status=GitChangeStatus.UPDATED,
            )
        ]
    )
    inspector = WorkspaceInspector(ProjectContext(tmp_path), workspace=workspace)

    changes = inspector.changes(
        SessionProjection(session_id="session-1", event_count=0, revision=-1)
    )

    assert changes["status"] == "unsupported"
    assert changes["changes"] == []
    assert "unsafe" in str(changes["message"]).lower()


def test_workspace_cannot_inspect_a_sibling_project(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (second / "private.txt").write_text("other project\n", encoding="utf-8")
    inspector = WorkspaceInspector(
        ProjectContext(first),
        workspace=_WorkspaceFixture(),
    )

    with pytest.raises(WorkspaceInspectionError, match="HW-WORKSPACE-001"):
        inspector.file("../second/private.txt")
    with pytest.raises(WorkspaceInspectionError, match="HW-WORKSPACE-001"):
        inspector.file(str(second / "private.txt"))

    assert "private.txt" not in str(inspector.tree())


def test_workspace_file_distinguishes_text_binary_encoding_and_limits(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    (tmp_path / "text.txt").write_text("one\ntwo\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"abc\x00def")
    (tmp_path / "latin1.txt").write_bytes(b"\xff")
    (tmp_path / "lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (tmp_path / "unicode.txt").write_text("123456789012345éafter", encoding="utf-8")
    inspector = WorkspaceInspector(
        project,
        workspace=_WorkspaceFixture(),
        limits=WorkspaceLimits(max_file_bytes=16, max_file_lines=2),
    )

    text = inspector.file("text.txt")
    binary = inspector.file("binary.bin")
    unsupported = inspector.file("latin1.txt")
    limited = inspector.file("lines.txt")
    unicode_boundary = inspector.file("unicode.txt")
    missing = inspector.file("missing.txt")
    directory = inspector.file(".")

    assert text["status"] == "available"
    assert text["content"] == "one\ntwo\n"
    assert binary["status"] == "binary"
    assert binary["content"] is None
    assert unsupported["status"] == "unsupported"
    assert unsupported["content"] is None
    assert limited["status"] == "truncated"
    assert limited["content"] == "one\ntwo\n"
    assert unicode_boundary["status"] == "truncated"
    assert unicode_boundary["content"] == "123456789012345"
    assert missing["status"] == "unavailable"
    assert directory["status"] == "unsupported"


def test_workspace_file_reports_metadata_and_open_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_path = tmp_path / "metadata.txt"
    open_path = tmp_path / "open.txt"
    metadata_path.write_text("synthetic\n", encoding="utf-8")
    open_path.write_text("synthetic\n", encoding="utf-8")
    original_stat = os.stat
    original_open = os.open

    def controlled_stat(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if path == "metadata.txt" and dir_fd is not None:
            raise PermissionError("synthetic metadata failure")
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def controlled_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "open.txt" and dir_fd is not None:
            raise PermissionError("synthetic open failure")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "stat", controlled_stat)
    monkeypatch.setattr(os, "open", controlled_open)
    inspector = WorkspaceInspector(ProjectContext(tmp_path), workspace=_WorkspaceFixture())

    metadata = inspector.file("metadata.txt")
    opened = inspector.file("open.txt")

    assert metadata["status"] == "unavailable"
    assert metadata["message"] == "File metadata is unavailable."
    assert opened["status"] == "unavailable"
    assert opened["message"] == "File could not be opened."


def test_workspace_changes_reports_openhands_filesystem_failures(tmp_path: Path) -> None:
    inspector = WorkspaceInspector(
        ProjectContext(tmp_path),
        workspace=_WorkspaceFixture(changes=OSError("synthetic Git failure")),
    )

    changes = inspector.changes(
        SessionProjection(session_id="session-1", event_count=0, revision=-1)
    )

    assert changes["status"] == "unavailable"
    assert changes["source"] == "unavailable"
    assert changes["changes"] == []


def test_workspace_file_completes_short_regular_file_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"synthetic result\n"
    (tmp_path / "result.txt").write_bytes(content)
    original_read = os.read

    def short_read(descriptor: int, count: int) -> bytes:
        return original_read(descriptor, min(2, count))

    monkeypatch.setattr(os, "read", short_read)
    inspector = WorkspaceInspector(
        ProjectContext(tmp_path),
        workspace=_WorkspaceFixture(),
    )

    result = inspector.file("result.txt")

    assert result["status"] == "available"
    assert result["content"] == content.decode()
    assert result["bytes_read"] == len(content)


def test_workspace_helpers_fail_closed_for_unverified_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = os.open(tmp_path, workspace_module._directory_open_flags())
    monkeypatch.setattr(
        workspace_module,
        "_descriptor_directory_path",
        lambda _descriptor: None,
    )
    try:
        assert workspace_module._run_anchored_git(descriptor, "status") is None
    finally:
        os.close(descriptor)

    assert workspace_module._bounded_text(None, 4) == (None, False)
    assert workspace_module._bounded_text("synthetic", 4) == ("synt", True)


def test_open_child_directory_reports_each_filesystem_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "file.txt").write_text("synthetic\n", encoding="utf-8")
    (tmp_path / "directory").mkdir()
    descriptor = os.open(tmp_path, workspace_module._directory_open_flags())
    try:
        with pytest.raises(WorkspaceInspectionError, match="directory does not exist"):
            workspace_module._open_child_directory(descriptor, "missing")
        with pytest.raises(WorkspaceInspectionError, match="path is not a directory"):
            workspace_module._open_child_directory(descriptor, "file.txt")

        original_stat = os.stat

        def fail_metadata(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
            *,
            dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> os.stat_result:
            if path == "directory" and dir_fd is not None:
                raise PermissionError("synthetic metadata failure")
            return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(os, "stat", fail_metadata)
        with pytest.raises(WorkspaceInspectionError, match="metadata is unavailable"):
            workspace_module._open_child_directory(descriptor, "directory")
        monkeypatch.setattr(os, "stat", original_stat)

        original_open = os.open

        def fail_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if path == "directory" and dir_fd is not None:
                raise PermissionError("synthetic open failure")
            return original_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", fail_open)
        with pytest.raises(WorkspaceInspectionError, match="could not be opened"):
            workspace_module._open_child_directory(descriptor, "directory")
    finally:
        os.close(descriptor)


def test_descriptor_path_matching_follows_verified_directory_alias(tmp_path: Path) -> None:
    alias = tmp_path.parent / f"{tmp_path.name}-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    descriptor = os.open(tmp_path, workspace_module._directory_open_flags())
    try:
        assert workspace_module._descriptor_matches_path(descriptor, str(alias))
    finally:
        os.close(descriptor)


def test_workspace_uses_openhands_git_changes_and_diff(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    _initialize_git_file(
        tmp_path,
        "analysis.py",
        baseline="new = False\n",
        current="new = True\n",
    )
    workspace = _WorkspaceFixture(
        changes=[
            GitChange(status=GitChangeStatus.UPDATED, path=Path("analysis.py")),
            GitChange(status=GitChangeStatus.ADDED, path=Path(".heartwood/private")),
        ],
        diffs={
            "analysis.py": GitDiff(
                original="new = False",
                modified="new = True",
            )
        },
    )
    inspector = WorkspaceInspector(project, workspace=workspace)
    projection = project_session((), session_id="session-1")

    changes = inspector.changes(projection)
    diff = inspector.diff(projection, "analysis.py")

    assert workspace.requested_change_paths == [".", "."]
    assert workspace.requested_diff_paths == ["analysis.py"]
    assert changes["status"] == "unsupported"
    assert changes["changes"] == [
        {
            "path": "analysis.py",
            "status": "modified",
            "source": "git",
            "action_ids": [],
        }
    ]
    assert diff["status"] == "available"
    assert diff["source"] == "git"
    assert diff["original"] == "new = False\n"
    assert diff["modified"] == "new = True\n"
    assert ".heartwood" not in str(changes)


def test_workspace_preserves_leading_and_trailing_whitespace_in_git_baselines(
    tmp_path: Path,
) -> None:
    _initialize_git_file(
        tmp_path,
        "indented.txt",
        baseline="  baseline value  \n",
        current="  current value  \n",
    )
    workspace = _WorkspaceFixture(
        changes=[
            GitChange(status=GitChangeStatus.UPDATED, path=Path("indented.txt")),
        ],
        diffs={
            "indented.txt": GitDiff(
                original="baseline value",
                modified="  current value  ",
            )
        },
    )
    inspector = WorkspaceInspector(ProjectContext(tmp_path), workspace=workspace)

    diff = inspector.diff(project_session((), session_id="session-1"), "indented.txt")

    assert diff["status"] == "available"
    assert diff["original"] == "  baseline value  \n"
    assert diff["modified"] == "  current value  \n"


@pytest.mark.parametrize(
    ("responses", "expected_status"),
    [
        ([b"not-a-size"], "unavailable"),
        ([b"5"], "truncated"),
        ([b"2", b"x"], "unavailable"),
        ([b"2", b"x\x00"], "binary"),
        ([b"1", b"\xff"], "unsupported"),
    ],
)
def test_git_baseline_rejects_invalid_or_unsafe_object_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[bytes | None],
    expected_status: str,
) -> None:
    inspector = WorkspaceInspector(
        ProjectContext(tmp_path),
        workspace=_WorkspaceFixture(),
        limits=WorkspaceLimits(max_diff_bytes=8),
    )
    values = iter(responses)

    def valid_reference(_root: Path, *, purpose: str) -> str:
        assert purpose == "display"
        return "a" * 40

    monkeypatch.setattr(
        "openhands.sdk.git.utils.get_valid_ref",
        valid_reference,
    )
    monkeypatch.setattr(
        workspace_module,
        "_run_anchored_git",
        lambda _descriptor, *_arguments: next(values),
    )

    baseline = inspector._git_baseline(
        PurePosixPath("analysis.py"),
        allow_missing=False,
    )

    assert baseline.status == expected_status
    assert baseline.content is None


def test_git_baseline_handles_missing_and_invalid_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = WorkspaceInspector(ProjectContext(tmp_path), workspace=_WorkspaceFixture())

    def invalid_reference(_root: Path, *, purpose: str) -> str:
        assert purpose == "display"
        return "main"

    monkeypatch.setattr(
        "openhands.sdk.git.utils.get_valid_ref",
        invalid_reference,
    )
    assert (
        inspector._git_baseline(PurePosixPath("analysis.py"), allow_missing=False).status
        == "unavailable"
    )

    def valid_reference(_root: Path, *, purpose: str) -> str:
        assert purpose == "display"
        return "a" * 40

    monkeypatch.setattr("openhands.sdk.git.utils.get_valid_ref", valid_reference)
    monkeypatch.setattr(
        workspace_module,
        "_run_anchored_git",
        lambda _descriptor, *_arguments: None,
    )
    missing = inspector._git_baseline(
        PurePosixPath("analysis.py"),
        allow_missing=False,
    )
    added = inspector._git_baseline(
        PurePosixPath("analysis.py"),
        allow_missing=True,
    )

    assert missing.status == "unavailable"
    assert added.status == "available"
    assert added.content == ""


def test_git_baseline_reports_reference_resolution_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = WorkspaceInspector(ProjectContext(tmp_path), workspace=_WorkspaceFixture())

    def fail_reference(_root: Path, *, purpose: str) -> str:
        assert purpose == "display"
        raise OSError("synthetic reference failure")

    monkeypatch.setattr("openhands.sdk.git.utils.get_valid_ref", fail_reference)

    baseline = inspector._git_baseline(
        PurePosixPath("analysis.py"),
        allow_missing=False,
    )

    assert baseline.status == "unavailable"


def test_workspace_git_ignores_inherited_repository_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    _initialize_git_file(
        project,
        "analysis.py",
        baseline="project baseline\n",
        current="project current\n",
    )
    _initialize_git_file(
        outside,
        "outside.py",
        baseline="outside-secret baseline\n",
        current="outside-secret current\n",
    )
    monkeypatch.setenv("GIT_DIR", str(outside / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outside))
    sdk_logger = logging.getLogger("openhands.sdk.git")
    sdk_logger.setLevel(logging.WARNING)
    inspector = WorkspaceInspector(ProjectContext(project))
    projection = SessionProjection(session_id="session-1", event_count=0, revision=-1)

    changes = inspector.changes(projection)
    diff = inspector.diff(projection, "analysis.py")

    assert [change["path"] for change in changes["changes"]] == ["analysis.py"]
    assert diff["status"] == "available"
    assert diff["original"] == "project baseline\n"
    assert diff["modified"] == "project current\n"
    assert "outside-secret" not in str((changes, diff))
    assert os.environ["GIT_DIR"] == str(outside / ".git")
    assert os.environ["GIT_WORK_TREE"] == str(outside)
    assert sdk_logger.level == logging.WARNING


def test_workspace_git_diff_rejects_a_path_swapped_during_openhands_inspection(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    project_directory = tmp_path / "analysis"
    _initialize_git_file(
        tmp_path,
        "analysis/summary.txt",
        baseline="project baseline\n",
        current="project\n",
    )
    outside = tmp_path_factory.mktemp("outside-diff")
    (outside / "summary.txt").write_text("outside-secret\n", encoding="utf-8")
    moved = tmp_path / "analysis-original"

    @dataclass
    class SwappingWorkspace(_WorkspaceFixture):
        def git_diff(self, path: str | Path) -> GitDiff:
            normalized = str(path)
            self.requested_diff_paths.append(normalized)
            project_directory.rename(moved)
            project_directory.symlink_to(outside, target_is_directory=True)
            try:
                modified = (tmp_path / normalized).read_text(encoding="utf-8").rstrip("\n")
            finally:
                project_directory.unlink()
                moved.rename(project_directory)
            return GitDiff(original="project baseline", modified=modified)

    workspace = SwappingWorkspace(
        changes=[
            GitChange(
                status=GitChangeStatus.UPDATED,
                path=Path("analysis/summary.txt"),
            )
        ]
    )
    inspector = WorkspaceInspector(ProjectContext(tmp_path), workspace=workspace)

    diff = inspector.diff(
        SessionProjection(session_id="session-1", event_count=0, revision=-1),
        "analysis/summary.txt",
    )

    assert diff["status"] == "unavailable"
    assert "outside-secret" not in str(diff)


def test_workspace_git_diff_rejects_a_matching_content_repository_swap(
    tmp_path: Path,
) -> None:
    project_directory = tmp_path / "project"
    outside = tmp_path / "outside"
    moved = tmp_path / "project-original"
    project_directory.mkdir()
    outside.mkdir()
    _initialize_git_file(
        project_directory,
        "summary.txt",
        baseline="project baseline\n",
        current="matching current\n",
    )
    _initialize_git_file(
        outside,
        "summary.txt",
        baseline="outside-secret baseline\n",
        current="matching current\n",
    )

    @dataclass
    class SwappingWorkspace(_WorkspaceFixture):
        def git_diff(self, path: str | Path) -> GitDiff:
            normalized = str(path)
            self.requested_diff_paths.append(normalized)
            project_directory.rename(moved)
            outside.rename(project_directory)
            try:
                return LocalWorkspace(working_dir=project_directory).git_diff(normalized)
            finally:
                project_directory.rename(outside)
                moved.rename(project_directory)

    workspace = SwappingWorkspace(
        changes=[
            GitChange(
                status=GitChangeStatus.UPDATED,
                path=Path("summary.txt"),
            )
        ]
    )
    inspector = WorkspaceInspector(
        ProjectContext(project_directory),
        workspace=workspace,
    )

    diff = inspector.diff(
        SessionProjection(session_id="session-1", event_count=0, revision=-1),
        "summary.txt",
    )

    assert diff["status"] == "unavailable"
    assert "outside-secret" not in str(diff)


def test_non_git_changes_use_only_successful_structured_file_actions(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    (tmp_path / "created.txt").write_text("synthetic\n", encoding="utf-8")
    workspace = _WorkspaceFixture(changes=GitRepositoryError("not a repository"))
    inspector = WorkspaceInspector(project, workspace=workspace)
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "file-success",
                    "action_id": "action-success",
                    "tool_name": "file_editor",
                    "kind": "file-editor",
                    "risk": "low",
                    "summary": "Create a file",
                    "arguments": {"command": "create", "path": str(tmp_path / "created.txt")},
                    "affected_paths": ["created.txt"],
                },
            ),
            _event(
                1,
                EventKind.TOOL_EXECUTION_RECORDED,
                {
                    "tool_call_id": "file-success",
                    "action_id": "action-success",
                    "tool_name": "file_editor",
                    "exit_code": 0,
                    "summary": "file editor completed",
                },
            ),
            _event(
                2,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "terminal-success",
                    "action_id": "action-terminal",
                    "tool_name": "terminal",
                    "kind": "terminal",
                    "risk": "low",
                    "summary": "Run a command mentioning fake.txt",
                    "arguments": {"command": "touch fake.txt"},
                    "affected_paths": [],
                },
            ),
            _event(
                3,
                EventKind.TOOL_EXECUTION_RECORDED,
                {
                    "tool_call_id": "terminal-success",
                    "action_id": "action-terminal",
                    "tool_name": "terminal",
                    "exit_code": 0,
                },
            ),
            _event(
                4,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "file-failed",
                    "action_id": "action-failed",
                    "tool_name": "file_editor",
                    "kind": "file-editor",
                    "risk": "low",
                    "summary": "Create another file",
                    "arguments": {"command": "create", "path": "failed.txt"},
                    "affected_paths": ["failed.txt"],
                },
            ),
            _event(
                5,
                EventKind.TOOL_EXECUTION_RECORDED,
                {
                    "tool_call_id": "file-failed",
                    "action_id": "action-failed",
                    "tool_name": "file_editor",
                    "exit_code": 1,
                },
            ),
        ),
        session_id="session-1",
    )

    changes = inspector.changes(projection)
    diff = inspector.diff(projection, "created.txt")

    assert changes["status"] == "non-git"
    assert changes["source"] == "session-actions"
    assert changes["changes"] == [
        {
            "path": "created.txt",
            "status": "added",
            "source": "session-action",
            "action_ids": ["action-success"],
        }
    ]
    assert "fake.txt" not in str(changes)
    assert "failed.txt" not in str(changes)
    assert diff["status"] == "non-git"
    assert diff["original"] is None
    assert diff["modified"] == "synthetic\n"


def test_non_git_diff_preserves_binary_and_missing_file_states(tmp_path: Path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"synthetic\x00binary")
    inspector = WorkspaceInspector(
        ProjectContext(tmp_path),
        workspace=_WorkspaceFixture(changes=GitRepositoryError("not a repository")),
    )
    actions = (
        ProjectionActionRecord(
            tool_call_id="binary-call",
            action_id="binary-action",
            tool_name="file_editor",
            risk="low",
            summary="Create a binary file",
            arguments={"command": "create", "path": "binary.bin"},
            details=ProjectionFileEditorActionDetails(
                operation="create",
                path="binary.bin",
            ),
            affected_paths=(ProjectionAffectedPath(path="binary.bin", effect="created"),),
            state="succeeded",
            proposed_sequence=0,
            updated_sequence=1,
        ),
        ProjectionActionRecord(
            tool_call_id="missing-call",
            action_id="missing-action",
            tool_name="file_editor",
            risk="low",
            summary="Create a missing file",
            arguments={"command": "create", "path": "missing.txt"},
            details=ProjectionFileEditorActionDetails(
                operation="create",
                path="missing.txt",
            ),
            affected_paths=(ProjectionAffectedPath(path="missing.txt", effect="created"),),
            state="succeeded",
            proposed_sequence=2,
            updated_sequence=3,
        ),
    )
    projection = SessionProjection(
        session_id="session-1",
        event_count=4,
        revision=3,
        actions=actions,
    )

    binary = inspector.diff(projection, "binary.bin")
    missing = inspector.diff(projection, "missing.txt")

    assert binary["status"] == "binary"
    assert binary["source"] == "session-action"
    assert missing["status"] == "unsupported"
    assert missing["source"] == "session-action"


def test_git_diff_preserves_bounded_current_file_states(tmp_path: Path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"ab\x00cd")
    (tmp_path / "large.txt").write_text("too large\n", encoding="utf-8")
    workspace = _WorkspaceFixture(
        changes=[
            GitChange(status=GitChangeStatus.UPDATED, path=Path("binary.bin")),
            GitChange(status=GitChangeStatus.UPDATED, path=Path("large.txt")),
            GitChange(status=GitChangeStatus.UPDATED, path=Path("missing.txt")),
        ],
    )
    inspector = WorkspaceInspector(
        ProjectContext(tmp_path),
        workspace=workspace,
        limits=WorkspaceLimits(max_file_bytes=4),
    )
    projection = SessionProjection(session_id="session-1", event_count=0, revision=-1)

    binary = inspector.diff(projection, "binary.bin")
    truncated = inspector.diff(projection, "large.txt")
    missing = inspector.diff(projection, "missing.txt")

    assert binary["status"] == "binary"
    assert truncated["status"] == "truncated"
    assert truncated["modified"] == "too "
    assert missing["status"] == "unavailable"


@pytest.mark.parametrize(
    ("baseline_status", "expected_status"),
    [
        ("binary", "binary"),
        ("truncated", "truncated"),
        ("unavailable", "unavailable"),
        ("unsupported", "unsupported"),
    ],
)
def test_git_diff_preserves_bounded_baseline_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    baseline_status: str,
    expected_status: str,
) -> None:
    (tmp_path / "analysis.py").write_text("answer = 42\n", encoding="utf-8")
    inspector = WorkspaceInspector(
        ProjectContext(tmp_path),
        workspace=_WorkspaceFixture(
            changes=[
                GitChange(
                    status=GitChangeStatus.UPDATED,
                    path=Path("analysis.py"),
                ),
            ],
        ),
    )

    def baseline(
        _relative: PurePosixPath,
        *,
        allow_missing: bool,
    ) -> workspace_module._GitBaseline:
        assert allow_missing is False
        return workspace_module._GitBaseline(status=cast(Any, baseline_status))

    monkeypatch.setattr(
        inspector,
        "_git_baseline",
        baseline,
    )

    diff = inspector.diff(
        SessionProjection(session_id="session-1", event_count=0, revision=-1),
        "analysis.py",
    )

    assert diff["status"] == expected_status
    assert diff["source"] == "git"


def test_git_diff_reports_openhands_diff_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class _FailingDiffWorkspace(_WorkspaceFixture):
        def git_diff(self, path: str | Path) -> GitDiff:
            self.requested_diff_paths.append(str(path))
            raise OSError("synthetic diff failure")

    (tmp_path / "analysis.py").write_text("answer = 42\n", encoding="utf-8")
    workspace = _FailingDiffWorkspace(
        changes=[
            GitChange(
                status=GitChangeStatus.UPDATED,
                path=Path("analysis.py"),
            ),
        ],
    )
    inspector = WorkspaceInspector(ProjectContext(tmp_path), workspace=workspace)

    def baseline(
        _relative: PurePosixPath,
        *,
        allow_missing: bool,
    ) -> workspace_module._GitBaseline:
        assert allow_missing is False
        return workspace_module._GitBaseline(
            status="available",
            content="answer = 1\n",
        )

    monkeypatch.setattr(
        inspector,
        "_git_baseline",
        baseline,
    )

    diff = inspector.diff(
        SessionProjection(session_id="session-1", event_count=0, revision=-1),
        "analysis.py",
    )

    assert diff["status"] == "unavailable"
    assert "could not produce a diff" in str(diff["message"])


def test_non_git_changes_do_not_claim_an_unknown_undo_effect(tmp_path: Path) -> None:
    (tmp_path / "analysis.py").write_text("answer = 42\n", encoding="utf-8")
    inspector = WorkspaceInspector(
        ProjectContext(tmp_path),
        workspace=_WorkspaceFixture(changes=GitRepositoryError("not a repository")),
    )
    projection = project_session(
        (
            _event(
                0,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "create-call",
                    "action_id": "create-action",
                    "tool_name": "file_editor",
                    "kind": "file-editor",
                    "risk": "low",
                    "summary": "Create the analysis",
                    "arguments": {"command": "create", "path": "analysis.py"},
                    "affected_paths": ["analysis.py"],
                },
            ),
            _event(
                1,
                EventKind.TOOL_EXECUTION_RECORDED,
                {
                    "tool_call_id": "create-call",
                    "action_id": "create-action",
                    "tool_name": "file_editor",
                    "exit_code": 0,
                },
            ),
            _event(
                2,
                EventKind.TOOL_CALL_PROPOSED,
                {
                    "tool_call_id": "undo-call",
                    "action_id": "undo-action",
                    "tool_name": "file_editor",
                    "kind": "file-editor",
                    "risk": "low",
                    "summary": "Undo the previous edit",
                    "arguments": {"command": "undo_edit", "path": "analysis.py"},
                    "affected_paths": ["analysis.py"],
                },
            ),
            _event(
                3,
                EventKind.TOOL_EXECUTION_RECORDED,
                {
                    "tool_call_id": "undo-call",
                    "action_id": "undo-action",
                    "tool_name": "file_editor",
                    "exit_code": 0,
                },
            ),
        ),
        session_id="session-1",
    )

    changes = inspector.changes(projection)

    assert changes["status"] == "non-git"
    assert changes["changes"] == []


def test_gateway_reconciles_actions_before_deriving_non_git_changes(tmp_path: Path) -> None:
    (tmp_path / "reconciled.txt").write_text("synthetic\n", encoding="utf-8")
    service = _ReconcilingWorkspaceService()
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
        service_factory=lambda _root, _session_id: cast(Any, service),
        workspace_inspector=WorkspaceInspector(
            ProjectContext(tmp_path),
            workspace=_WorkspaceFixture(changes=GitRepositoryError("not a repository")),
        ),
    )
    gateway.handle(
        SessionCommand(
            command_id="start-reconciliation",
            session_id="session-1",
            kind=CommandKind.CHAT,
            actor_id="synthetic-user",
            created_at="2026-01-01T00:00:00Z",
            payload={"prompt": "Create the synthetic result"},
        )
    )
    assert service.reconcile_calls == 1

    changes = gateway.workspace_changes(session_id="session-1")

    assert service.reconciled is True
    assert service.reconcile_calls == 2
    assert changes["changes"] == [
        {
            "path": "reconciled.txt",
            "status": "added",
            "source": "session-action",
            "action_ids": ["reconciled-action"],
        }
    ]
    gateway.stop()


def test_non_git_change_evidence_is_scoped_to_the_selected_session(
    tmp_path: Path,
) -> None:
    inspector = WorkspaceInspector(
        ProjectContext(tmp_path),
        workspace=_WorkspaceFixture(changes=GitRepositoryError("not a repository")),
    )

    def projection_for(session_id: str, path: str) -> SessionProjection:
        proposed = _event(
            0,
            EventKind.TOOL_CALL_PROPOSED,
            {
                "tool_call_id": f"{session_id}-call",
                "action_id": f"{session_id}-action",
                "tool_name": "file_editor",
                "kind": "file-editor",
                "risk": "low",
                "summary": "Create a session result",
                "arguments": {"command": "create", "path": path},
                "affected_paths": [path],
            },
        ).model_copy(update={"session_id": session_id})
        executed = _event(
            1,
            EventKind.TOOL_EXECUTION_RECORDED,
            {
                "tool_call_id": f"{session_id}-call",
                "action_id": f"{session_id}-action",
                "tool_name": "file_editor",
                "exit_code": 0,
            },
        ).model_copy(update={"session_id": session_id})
        return project_session((proposed, executed), session_id=session_id)

    first = inspector.changes(projection_for("first", "first.txt"))
    second = inspector.changes(projection_for("second", "second.txt"))

    assert [change["path"] for change in first["changes"]] == ["first.txt"]
    assert [change["path"] for change in second["changes"]] == ["second.txt"]
    assert "second.txt" not in str(first)
    assert "first.txt" not in str(second)


def test_pinned_openhands_local_workspace_conforms_for_git_changes_and_diff(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project = ProjectContext(tmp_path)
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "heartwood@example.invalid")
    _git(tmp_path, "config", "user.name", "Heartwood Test")
    source = tmp_path / "analysis.py"
    removed = tmp_path / "removed.py"
    source.write_text("answer = 1\n", encoding="utf-8")
    removed.write_text("obsolete = True\n", encoding="utf-8")
    _git(tmp_path, "add", "analysis.py", "removed.py")
    _git(
        tmp_path,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Initial synthetic fixture",
    )
    source.write_text("answer = 2\n", encoding="utf-8")
    added = tmp_path / "added.py"
    added.write_text("new = True\n", encoding="utf-8")
    removed.unlink()
    inspector = WorkspaceInspector(
        project,
        workspace=LocalWorkspace(working_dir=tmp_path),
    )
    projection = project_session((), session_id="session-1")

    with caplog.at_level(logging.DEBUG, logger="openhands.sdk.git"):
        changes = inspector.changes(projection)
        diff = inspector.diff(projection, "analysis.py")
        added_diff = inspector.diff(projection, "added.py")
        deleted_diff = inspector.diff(projection, "removed.py")

    assert changes["source"] == "git"
    assert {(item["path"], item["status"]) for item in changes["changes"]} == {
        ("added.py", "added"),
        ("analysis.py", "modified"),
        ("removed.py", "deleted"),
    }
    assert diff["status"] == "available"
    assert diff["original"] == "answer = 1\n"
    assert diff["modified"] == "answer = 2\n"
    assert added_diff["status"] == "available"
    assert added_diff["original"] == ""
    assert added_diff["modified"] == "new = True\n"
    assert deleted_diff["status"] == "unsupported"
    assert deleted_diff["source"] == "git"
    assert [
        record for record in caplog.records if record.name.startswith("openhands.sdk.git")
    ] == []


def test_rest_exposes_the_same_bounded_workspace_contract(
    tmp_path: Path,
) -> None:
    _initialize_git_file(
        tmp_path,
        "analysis.py",
        baseline="answer = 1\n",
        current="answer = 42\n",
    )
    workspace = _WorkspaceFixture(
        changes=[
            GitChange(status=GitChangeStatus.UPDATED, path=Path("analysis.py")),
        ],
        diffs={
            "analysis.py": GitDiff(
                original="answer = 1",
                modified="answer = 42",
            )
        },
    )
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
        workspace_inspector=WorkspaceInspector(
            ProjectContext(tmp_path),
            workspace=workspace,
        ),
    )
    session_id = gateway.default_session()["session_id"]
    rest = RestGateway(gateway)

    tree = rest.handle(
        RestRequest(
            method="GET",
            path=f"/sessions/{session_id}/workspace/tree?depth=2",
        )
    )
    file = rest.handle(
        RestRequest(
            method="GET",
            path=f"/sessions/{session_id}/workspace/file?path=analysis.py",
        )
    )
    changes = rest.handle(
        RestRequest(
            method="GET",
            path=f"/sessions/{session_id}/workspace/changes",
        )
    )
    diff = rest.handle(
        RestRequest(
            method="GET",
            path=f"/sessions/{session_id}/workspace/diff?path=analysis.py",
        )
    )
    private = rest.handle(
        RestRequest(
            method="GET",
            path=f"/sessions/{session_id}/workspace/file?path=.heartwood/config.toml",
        )
    )
    unknown_session = rest.handle(
        RestRequest(
            method="GET",
            path="/sessions/missing-session/workspace/tree",
        )
    )

    assert tree.status_code == 200
    entries = tree.body["entries"]
    assert isinstance(entries, list)
    assert isinstance(entries[0], dict)
    assert entries[0]["path"] == "analysis.py"
    assert file.status_code == 200
    assert file.body["content"] == "answer = 42\n"
    assert changes.status_code == 200
    assert changes.body["source"] == "git"
    assert diff.status_code == 200
    assert diff.body["original"] == "answer = 1\n"
    assert diff.body["modified"] == "answer = 42\n"
    assert private.status_code == 422
    assert str(private.body["error"]).startswith("HW-WORKSPACE-002:")
    assert unknown_session.status_code == 404


def test_rest_workspace_routes_validate_methods_and_query_parameters(tmp_path: Path) -> None:
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
        workspace_inspector=WorkspaceInspector(
            ProjectContext(tmp_path),
            workspace=_WorkspaceFixture(),
        ),
    )
    session_id = gateway.default_session()["session_id"]
    rest = RestGateway(gateway)

    malformed_depth = rest.handle(
        RestRequest(
            method="GET",
            path=f"/sessions/{session_id}/workspace/tree?depth=deep",
        )
    )
    missing_file_path = rest.handle(
        RestRequest(
            method="GET",
            path=f"/sessions/{session_id}/workspace/file",
        )
    )
    explicit_root = rest.handle(
        RestRequest(
            method="GET",
            path=f"/sessions/{session_id}/workspace/file?path=.",
        )
    )
    wrong_method = rest.handle(
        RestRequest(
            method="POST",
            path=f"/sessions/{session_id}/workspace/changes",
        )
    )

    assert malformed_depth.status_code == 400
    assert missing_file_path.status_code == 422
    assert explicit_root.status_code == 200
    assert explicit_root.body["status"] == "unsupported"
    assert wrong_method.status_code == 405


def test_direct_workspace_reads_initialize_private_project_state(tmp_path: Path) -> None:
    (tmp_path / "analysis.py").write_text("answer = 42\n", encoding="utf-8")
    gateway = SessionGateway(
        project=ProjectContext(tmp_path),
        env={},
        backend_id="deterministic",
        workspace_inspector=WorkspaceInspector(
            ProjectContext(tmp_path),
            workspace=_WorkspaceFixture(),
        ),
    )

    tree = gateway.workspace_tree()
    file = gateway.workspace_file(path="analysis.py")

    assert (tmp_path / ".heartwood").is_dir()
    assert tree["entries"][0]["path"] == "analysis.py"
    assert file["content"] == "answer = 42\n"


def _event(
    sequence: int,
    kind: EventKind,
    payload: dict[str, JsonValue],
) -> SessionEvent:
    return SessionEvent(
        event_id=f"event-{sequence}",
        session_id="session-1",
        sequence=sequence,
        kind=kind,
        occurred_at="2026-01-01T00:00:00Z",
        payload=payload,
    )


def _git(path: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def _initialize_git_file(
    root: Path,
    relative_path: str,
    *,
    baseline: str,
    current: str,
) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "heartwood@example.invalid")
    _git(root, "config", "user.name", "Heartwood Test")
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline, encoding="utf-8")
    _git(root, "add", relative_path)
    _git(
        root,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Initial synthetic fixture",
    )
    path.write_text(current, encoding="utf-8")
