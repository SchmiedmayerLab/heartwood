# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from heartwood.gateway import ProjectContext, WorkspaceInspectionError, WorkspaceInspector


def test_binary_and_large_files_are_streamed_without_preview_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"\x00synthetic data\xff" * 150_000
    (tmp_path / "data.bin").write_bytes(content)
    inspector = WorkspaceInspector(ProjectContext(tmp_path))
    observed_reads: list[int] = []
    original = os.read

    def bounded_read(descriptor: int, count: int) -> bytes:
        observed_reads.append(count)
        assert count <= 1024 * 1024
        return original(descriptor, count)

    monkeypatch.setattr(os, "read", bounded_read)
    result = inspector.fingerprint("data.bin", max_bytes=len(content))
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert result.size_bytes == len(content)
    assert result.path == "data.bin"
    assert len(observed_reads) >= 3
    assert not (tmp_path / ".heartwood").exists()


def test_empty_and_changed_file_identity(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.touch()
    inspector = WorkspaceInspector(ProjectContext(tmp_path))
    empty = inspector.fingerprint("data.csv", max_bytes=0)
    path.write_bytes(b"synthetic\n")
    modified = inspector.fingerprint("data.csv", max_bytes=10)
    assert empty.sha256 != modified.sha256
    assert empty.size_bytes == 0
    with pytest.raises(WorkspaceInspectionError, match="HW-WORKSPACE-007"):
        inspector.fingerprint("data.csv", max_bytes=0)


@pytest.mark.parametrize("path", ["../outside", "/absolute", ".heartwood/config.toml", ".git/HEAD"])
def test_fingerprint_reuses_public_path_boundary(tmp_path: Path, path: str) -> None:
    with pytest.raises(WorkspaceInspectionError, match="HW-WORKSPACE-00"):
        WorkspaceInspector(ProjectContext(tmp_path)).fingerprint(path, max_bytes=100)


@pytest.mark.parametrize("kind", ["file-link", "directory-link", "directory", "fifo", "missing"])
def test_non_regular_or_linked_files_never_supply_fingerprints(tmp_path: Path, kind: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_text("synthetic forbidden content")
    path = "data"
    if kind == "file-link":
        (project / path).symlink_to(outside / "data")
    elif kind == "directory-link":
        (project / "linked").symlink_to(outside, target_is_directory=True)
        path = "linked/data"
    elif kind == "directory":
        (project / path).mkdir()
    elif kind == "fifo":
        os.mkfifo(project / path)
    with pytest.raises(WorkspaceInspectionError, match="HW-WORKSPACE-007"):
        WorkspaceInspector(ProjectContext(project)).fingerprint(path, max_bytes=100)


@pytest.mark.parametrize("mutation", ["grow", "replace", "truncate", "rewrite"])
def test_changes_during_streaming_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    path = tmp_path / "data.csv"
    content = b"synthetic before\n"
    path.write_bytes(content)
    original = os.read
    changed = False

    def read_then_change(descriptor: int, count: int) -> bytes:
        nonlocal changed
        value = original(descriptor, count)
        if not changed:
            changed = True
            if mutation == "grow":
                path.write_bytes(content * 2)
            elif mutation == "replace":
                replacement = tmp_path / "replacement"
                replacement.write_bytes(content)
                replacement.replace(path)
            elif mutation == "truncate":
                path.write_bytes(b"")
            else:
                path.write_bytes(b"x" * len(content))
                # Force a changed mtime even on filesystems with coarse timestamp resolution.
                os.utime(path, ns=(1, 1))
        return value

    monkeypatch.setattr(os, "read", read_then_change)
    with pytest.raises(WorkspaceInspectionError, match="HW-WORKSPACE-007"):
        WorkspaceInspector(ProjectContext(tmp_path)).fingerprint("data.csv", max_bytes=len(content))


@pytest.mark.parametrize("limit", [-1, True])
def test_invalid_capture_budget(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        WorkspaceInspector(ProjectContext(tmp_path)).fingerprint("data.csv", max_bytes=limit)
