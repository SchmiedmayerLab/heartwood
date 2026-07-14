# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from heartwood.gateway import ProjectContext, ProjectStateError


def test_project_context_initializes_private_state_layout(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)

    project.initialize()

    assert json.loads(project.state_path.read_text(encoding="utf-8")) == {
        "schema_version": "heartwood.project-state.v1"
    }
    assert project.config_path == tmp_path / ".heartwood" / "config.toml"
    for directory in (
        project.sessions_dir,
        project.models_dir,
        project.skills_dir,
        project.audit_dir,
        project.runtime_dir,
        project.logs_dir,
        project.cache_dir,
    ):
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o777 == 0o700
    assert (project.state_root / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert project.state_path.stat().st_mode & 0o777 == 0o600


def test_project_state_is_hidden_from_git_status(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)

    ProjectContext(tmp_path).initialize()

    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--short"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""


def test_project_context_requires_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(ProjectStateError, match="project directory does not exist"):
        ProjectContext(tmp_path / "missing")


def test_project_context_repairs_private_state_permissions(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    project.state_root.chmod(0o755)
    project.sessions_dir.chmod(0o755)
    project.state_path.chmod(0o644)
    (project.state_root / ".gitignore").chmod(0o644)
    project.config_path.write_text("synthetic = true\n", encoding="utf-8")
    project.config_path.chmod(0o644)

    project.initialize()

    assert project.state_root.stat().st_mode & 0o777 == 0o700
    assert project.sessions_dir.stat().st_mode & 0o777 == 0o700
    assert project.state_path.stat().st_mode & 0o777 == 0o600
    assert (project.state_root / ".gitignore").stat().st_mode & 0o777 == 0o600
    assert project.config_path.stat().st_mode & 0o777 == 0o600


def test_project_context_rejects_invalid_internal_ignore_rule(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    (project.state_root / ".gitignore").write_text("config.toml\n", encoding="utf-8")

    with pytest.raises(ProjectStateError, match="Git ignore rule is invalid"):
        project.initialize()


@pytest.mark.parametrize("state_contents", ["{", '{"schema_version": "unknown"}'])
def test_project_context_rejects_invalid_state_marker(
    tmp_path: Path,
    state_contents: str,
) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    project.state_path.write_text(state_contents, encoding="utf-8")

    with pytest.raises(ProjectStateError, match=r"state schema|state\.json"):
        project.state_exists()


def test_project_context_rejects_old_or_unknown_state(tmp_path: Path) -> None:
    state = tmp_path / ".heartwood"
    state.mkdir()
    (state / "sessions").mkdir()

    with pytest.raises(ProjectStateError, match=r"incompatible \.heartwood layout"):
        ProjectContext(tmp_path).initialize()


def test_project_context_does_not_treat_empty_state_as_initialized(tmp_path: Path) -> None:
    (tmp_path / ".heartwood").mkdir()

    assert not ProjectContext(tmp_path).state_exists()


def test_project_context_rejects_incomplete_initialized_state(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    project.logs_dir.rmdir()

    with pytest.raises(ProjectStateError, match="logs must be a regular directory"):
        project.state_exists()


def test_project_context_rejects_state_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / ".heartwood").symlink_to(target, target_is_directory=True)

    with pytest.raises(ProjectStateError, match="must not be a symbolic link"):
        ProjectContext(tmp_path).initialize()


def test_project_path_boundary_excludes_state_and_escapes(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    source = tmp_path / "analysis.py"
    source.write_text("print('ok')\n", encoding="utf-8")

    assert project.require_project_path(source) == source
    assert project.require_project_path(Path("analysis.py")) == source
    assert not project.contains(project.config_path)
    assert project.contains(project.config_path, include_state=True)
    with pytest.raises(ProjectStateError, match=r"outside \.heartwood"):
        project.require_project_path(project.config_path)
    with pytest.raises(ProjectStateError, match="inside the project"):
        project.require_project_path(tmp_path.parent / "outside.txt", include_state=True)


def test_project_path_boundary_rejects_symbolic_link_escape(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    project = ProjectContext(project_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project.root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectStateError, match="inside the project"):
        project.require_project_path(Path("linked/result.txt"))


def test_current_project_is_exact_process_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "analysis" / "nested"
    nested.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(nested)

    assert ProjectContext.current().root == nested
