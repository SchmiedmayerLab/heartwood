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
from pathlib import Path
from typing import Any, cast

import pytest
from openhands.sdk.git.exceptions import GitRepositoryError
from openhands.sdk.git.models import GitChange, GitChangeStatus, GitDiff
from openhands.sdk.workspace import LocalWorkspace

import heartwood.gateway._workspace as workspace_module
from heartwood.core_adapter import SessionResult
from heartwood.gateway import (
    ProjectContext,
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

    def handle(self, command: SessionCommand) -> SessionResult:
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


def test_workspace_directory_enumeration_stops_after_the_bounded_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan = _BoundedDirectoryScan(["z.txt", ".git", "a.txt", "b.txt", "c.txt"])
    monkeypatch.setattr(os, "scandir", lambda _descriptor: scan)

    entries, truncated = workspace_module._bounded_directory_entries(1, limit=2)

    assert [entry.name for entry in entries] == ["a.txt", "z.txt"]
    assert truncated is True
    assert scan.iterated == 4


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
    assert diff["original"] == "new = False"
    assert diff["modified"] == "new = True\n"
    assert ".heartwood" not in str(changes)


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
    inspector = WorkspaceInspector(ProjectContext(project))
    projection = SessionProjection(session_id="session-1", event_count=0, revision=-1)

    changes = inspector.changes(projection)
    diff = inspector.diff(projection, "analysis.py")

    assert [change["path"] for change in changes["changes"]] == ["analysis.py"]
    assert diff["status"] == "available"
    assert diff["original"] == "project baseline"
    assert diff["modified"] == "project current\n"
    assert "outside-secret" not in str((changes, diff))
    assert os.environ["GIT_DIR"] == str(outside / ".git")
    assert os.environ["GIT_WORK_TREE"] == str(outside)


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
            "action_ids": ["file-success"],
        }
    ]
    assert "fake.txt" not in str(changes)
    assert "failed.txt" not in str(changes)
    assert diff["status"] == "non-git"
    assert diff["original"] is None
    assert diff["modified"] == "synthetic\n"


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

    changes = gateway.workspace_changes(session_id="session-1")

    assert service.reconciled is True
    assert changes["changes"] == [
        {
            "path": "reconciled.txt",
            "status": "added",
            "source": "session-action",
            "action_ids": ["reconciled-call"],
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
    assert diff["original"] == "answer = 1"
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
    assert diff.body["original"] == "answer = 1"
    assert diff.body["modified"] == "answer = 42\n"
    assert private.status_code == 422
    assert str(private.body["error"]).startswith("HW-WORKSPACE-002:")
    assert unknown_session.status_code == 404


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
