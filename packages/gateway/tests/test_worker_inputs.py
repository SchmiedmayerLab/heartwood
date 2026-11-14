# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from heartwood.gateway import ProjectContext, WorkspaceInspectionError, WorkspaceInspector
from heartwood.gateway.worker_inputs import WorkerInputManifest, stage_worker_inputs
from heartwood.schemas.experiments import ExperimentFile


def _file(path: str, content: bytes = b"synthetic") -> ExperimentFile:
    return ExperimentFile(
        path=path, sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content)
    )


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path, WorkspaceInspector]:
    project = tmp_path / "project"
    project.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    return project, staging, WorkspaceInspector(ProjectContext(project))


def test_manifest_digest_binds_version_paths_sizes_and_content() -> None:
    first = _file("a")
    second = _file("b")
    manifest = WorkerInputManifest(files=(first, second))
    reordered = WorkerInputManifest(files=(second, first))
    assert manifest.sha256 == reordered.sha256
    payload = {
        "schema_version": "heartwood.worker-inputs.v1",
        "files": [first.model_dump(), second.model_dump()],
    }
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    assert manifest.sha256 == expected.hexdigest()
    assert manifest.sha256 != WorkerInputManifest(files=(first, _file("b", b"different"))).sha256
    assert manifest.sha256 != WorkerInputManifest(files=(first, _file("c"))).sha256
    assert manifest.sha256 != WorkerInputManifest(files=(first,)).sha256
    with pytest.raises(ValidationError):
        manifest.files = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    "paths", [("a", "a"), ("a", "A"), ("a", "a/b"), ("A/b", "a"), ("é", "e\u0301")]
)
def test_manifest_rejects_duplicate_or_overlapping_paths(paths: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError, match="unique"):
        WorkerInputManifest(files=tuple(_file(path) for path in paths))


@pytest.mark.parametrize(
    "path",
    [
        "../a",
        "/a",
        "a//b",
        "a/./b",
        "a\\b",
        ".git/HEAD",
        ".HEARTWOOD/key",
        "a/.heartwood/state",
        "a\x00b",
    ],
)
def test_manifest_revalidates_nested_file_paths(path: str) -> None:
    invalid = _file("valid").model_copy(update={"path": path})
    with pytest.raises(ValidationError):
        WorkerInputManifest(files=(invalid,))


@pytest.mark.parametrize(
    ("count", "size"), [(129, 0), (1, 16 * 1024 * 1024 + 1), (5, 16 * 1024 * 1024)]
)
def test_manifest_bounds_file_count_and_bytes(count: int, size: int) -> None:
    with pytest.raises(ValidationError):
        WorkerInputManifest(
            files=tuple(
                ExperimentFile(path=f"{i}", sha256="a" * 64, size_bytes=size) for i in range(count)
            )
        )


def test_manifest_accepts_exact_limits_and_empty_selection() -> None:
    WorkerInputManifest(
        files=tuple(
            ExperimentFile(path=f"{i}", sha256="a" * 64, size_bytes=16 * 1024 * 1024)
            for i in range(4)
        )
    )
    WorkerInputManifest(files=tuple(_file(str(i), b"") for i in range(128)))
    assert len(WorkerInputManifest().sha256) == 64


def test_staging_copies_only_selected_verified_bytes_and_cleans_up(
    workspace: tuple[Path, Path, WorkspaceInspector],
) -> None:
    project, staging, inspector = workspace
    content = b"\x00\xffsynthetic\n" * 120_000
    (project / "nested").mkdir()
    (project / "nested/data.bin").write_bytes(content)
    (project / "empty").touch()
    (project / ".heartwood").mkdir()
    (project / ".heartwood/private").write_bytes(b"private synthetic sentinel")
    (project / "unselected").write_bytes(b"not selected")
    manifest = WorkerInputManifest(files=(_file("nested/data.bin", content), _file("empty", b"")))
    with stage_worker_inputs(inspector, manifest, staging_parent=staging) as snapshot:
        assert snapshot.parent == staging
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o700
        assert sorted(
            p.relative_to(snapshot).as_posix() for p in snapshot.rglob("*") if p.is_file()
        ) == ["empty", "nested/data.bin"]
        assert (snapshot / "nested/data.bin").read_bytes() == content
        assert (snapshot / "empty").read_bytes() == b""
        assert stat.S_IMODE((snapshot / "nested/data.bin").stat().st_mode) == 0o400
        (project / "nested/data.bin").write_bytes(b"later change")
        assert (snapshot / "nested/data.bin").read_bytes() == content
    assert not snapshot.exists()
    assert not list(staging.iterdir())
    assert (project / ".heartwood/private").exists()


@pytest.mark.parametrize(
    "change",
    [
        "same-size",
        "grow",
        "shrink",
        "missing",
        "hardlink",
        "symlink",
        "directory",
        "fifo",
        "parent-link",
    ],
)
def test_staging_rejects_changed_or_aliased_files_and_cleans_partial_copy(
    workspace: tuple[Path, Path, WorkspaceInspector], change: str
) -> None:
    project, staging, inspector = workspace
    (project / "first").write_bytes(b"synthetic")
    (project / "nested").mkdir()
    path = project / "nested/data"
    path.write_bytes(b"synthetic")
    manifest = WorkerInputManifest(
        files=(_file("first"), inspector.fingerprint("nested/data", max_bytes=100))
    )
    if change == "hardlink":
        (project / ".heartwood").mkdir()
        os.link(path, project / ".heartwood/private")
    elif change == "parent-link":
        outside = staging / "outside"
        path.parent.rename(outside)
        path.parent.symlink_to(outside, target_is_directory=True)
    elif change in {"same-size", "grow", "shrink"}:
        path.write_bytes(
            {"same-size": b"different", "grow": b"synthetic extra", "shrink": b""}[change]
        )
    else:
        path.unlink()
        if change == "symlink":
            path.symlink_to(project / "first")
        elif change == "directory":
            path.mkdir()
        elif change == "fifo":
            os.mkfifo(path)
    with (
        pytest.raises(WorkspaceInspectionError),
        stage_worker_inputs(inspector, manifest, staging_parent=staging),
    ):
        pytest.fail("Unverified input escaped staging")
    assert not list(staging.glob("worker-inputs-*"))
    assert (project / "first").read_bytes() == b"synthetic"


def test_transfer_failure_also_removes_staged_files(
    workspace: tuple[Path, Path, WorkspaceInspector],
) -> None:
    project, staging, inspector = workspace
    (project / "input").write_bytes(b"synthetic")
    with (
        pytest.raises(RuntimeError, match="transfer failed"),
        stage_worker_inputs(
            inspector, WorkerInputManifest(files=(_file("input"),)), staging_parent=staging
        ) as snapshot,
    ):
        raise RuntimeError("transfer failed")
    assert not snapshot.exists()


@pytest.mark.parametrize("location", ["project", "project-alias", "shared", "file"])
def test_staging_requires_private_directory_outside_project(
    workspace: tuple[Path, Path, WorkspaceInspector], location: str
) -> None:
    project, staging, inspector = workspace
    if location == "project":
        staging = project
    elif location == "project-alias":
        staging = staging / "alias"
        staging.symlink_to(project, target_is_directory=True)
    elif location == "shared":
        staging.chmod(0o755)
    else:
        staging = staging / "file"
        staging.touch()
    with (
        pytest.raises(ValueError, match="Worker input staging"),
        stage_worker_inputs(inspector, WorkerInputManifest(), staging_parent=staging),
    ):
        pytest.fail("Unsafe staging parent accepted")


def test_staging_revalidates_constructed_manifest(
    workspace: tuple[Path, Path, WorkspaceInspector],
) -> None:
    _, staging, inspector = workspace
    invalid = WorkerInputManifest.model_construct(
        files=(_file("safe").model_copy(update={"path": "../outside"}),)
    )
    with (
        pytest.raises(ValidationError),
        stage_worker_inputs(inspector, invalid, staging_parent=staging),
    ):
        pytest.fail("Unvalidated manifest accepted")
    assert not list(staging.iterdir())


def test_changed_file_during_capture_never_reaches_destination(
    workspace: tuple[Path, Path, WorkspaceInspector], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, staging, inspector = workspace
    path = project / "input"
    path.write_bytes(b"synthetic")
    original = os.read
    changed = False

    def read_then_replace(descriptor: int, count: int) -> bytes:
        nonlocal changed
        data = original(descriptor, count)
        if not changed:
            changed = True
            replacement = project / "replacement"
            replacement.write_bytes(b"synthetic")
            replacement.replace(path)
        return data

    monkeypatch.setattr(os, "read", read_then_replace)
    with (
        pytest.raises(WorkspaceInspectionError),
        stage_worker_inputs(
            inspector, WorkerInputManifest(files=(_file("input"),)), staging_parent=staging
        ),
    ):
        pytest.fail("Concurrent replacement accepted")
    assert not list(staging.iterdir())
