# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident

import pytest

from heartwood.gateway import ProjectContext, ProjectStateError
from heartwood.persistence import write_private_text_atomic


def test_project_context_initializes_private_state_layout(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)

    project.initialize()

    assert json.loads(project.state_path.read_text(encoding="utf-8")) == {
        "formats": {
            "audit_event": "heartwood.audit-event.v1",
            "openhands_state": "heartwood.openhands-state.v1",
            "project_config": "heartwood.project-config.v1",
            "session_command_receipt": "heartwood.session-command-receipt.v1",
            "session_commit": "heartwood.session-commit.v1",
            "session_event": "heartwood.session-event.v1",
            "session_metadata": "heartwood.session-metadata.v1",
            "session_writer": "heartwood.session-writer.v1",
            "skill_metadata": "heartwood.skill-metadata.v1",
        },
        "schema_version": "heartwood.project-state.v2",
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


def test_project_context_serializes_concurrent_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = ProjectContext(tmp_path)
    first_write_started = Event()
    release_first_write = Event()
    competing_validation_started = Event()
    first_thread: list[int] = []
    original_write = write_private_text_atomic
    original_validate = ProjectContext._validate_state_root

    def pause_first_write(path: Path, content: str, *, secure_parent: bool = True) -> None:
        if path.name == ".gitignore" and not first_thread:
            first_thread.append(get_ident())
            first_write_started.set()
            assert release_first_write.wait(timeout=5)
        original_write(path, content, secure_parent=secure_parent)

    def observe_competing_validation(context: ProjectContext) -> None:
        if first_write_started.is_set() and get_ident() != first_thread[0]:
            competing_validation_started.set()
        original_validate(context)

    monkeypatch.setattr(
        "heartwood.gateway._project.write_private_text_atomic",
        pause_first_write,
    )
    monkeypatch.setattr(ProjectContext, "_validate_state_root", observe_competing_validation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(project.initialize)
        assert first_write_started.wait(timeout=5)
        second = executor.submit(project.initialize)
        assert competing_validation_started.wait(timeout=5)
        assert not second.done()
        release_first_write.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert project.state_exists()


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
    project.config_lock_path.write_text("synthetic\n", encoding="utf-8")
    project.config_lock_path.chmod(0o644)

    project.initialize()

    assert project.state_root.stat().st_mode & 0o777 == 0o700
    assert project.sessions_dir.stat().st_mode & 0o777 == 0o700
    assert project.state_path.stat().st_mode & 0o777 == 0o600
    assert (project.state_root / ".gitignore").stat().st_mode & 0o777 == 0o600
    assert project.config_path.stat().st_mode & 0o777 == 0o600
    assert project.config_lock_path.stat().st_mode & 0o777 == 0o600


def test_project_context_rejects_invalid_internal_ignore_rule(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    (project.state_root / ".gitignore").write_text("config.toml\n", encoding="utf-8")

    with pytest.raises(ProjectStateError, match="Git ignore rule is invalid"):
        project.initialize()


def test_project_context_migrates_supported_state_only_during_initialization(
    tmp_path: Path,
) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    legacy = '{"schema_version":"heartwood.project-state.v1"}\n'
    project.state_path.write_text(legacy, encoding="utf-8")

    assert project.state_exists()
    assert project.state_path.read_text(encoding="utf-8") == legacy

    project.initialize()

    state = json.loads(project.state_path.read_text(encoding="utf-8"))
    assert state["schema_version"] == "heartwood.project-state.v2"
    assert state["formats"]["openhands_state"] == "heartwood.openhands-state.v1"


def test_project_context_preserves_state_when_migration_fails(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    unsupported = '{"schema_version":"heartwood.project-state.v0","private":"value"}\n'
    project.state_path.write_text(unsupported, encoding="utf-8")

    with pytest.raises(ProjectStateError):
        project.initialize()

    assert project.state_path.read_text(encoding="utf-8") == unsupported


def test_project_context_does_not_relabel_incompatible_current_formats(
    tmp_path: Path,
) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    state = json.loads(project.state_path.read_text(encoding="utf-8"))
    state["formats"]["session_event"] = "heartwood.session-event.v2"
    incompatible = json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
    project.state_path.write_text(incompatible, encoding="utf-8")

    with pytest.raises(ProjectStateError, match=r"unsupported \.heartwood state schema"):
        project.initialize()

    assert project.state_path.read_text(encoding="utf-8") == incompatible


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


def test_project_context_initializes_around_a_fresh_model_mount(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.models_dir.mkdir(parents=True)
    model = project.models_dir / "mounted-model.gguf"
    model.write_bytes(b"synthetic")

    project.initialize()

    assert project.state_exists()
    assert model.read_bytes() == b"synthetic"


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


def test_project_context_rejects_config_lock_symlink(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    target = tmp_path / "outside.lock"
    target.write_text("synthetic\n", encoding="utf-8")
    project.config_lock_path.symlink_to(target)

    with pytest.raises(ProjectStateError, match=r"\.config\.lock must be a regular file"):
        project.initialize()


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
