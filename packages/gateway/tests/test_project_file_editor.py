# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from openhands.tools.file_editor.definition import FileEditorAction
from openhands.tools.file_editor.impl import FileEditorExecutor

from heartwood.gateway import ProjectContext, ProjectStateError
from heartwood.gateway._project_file_editor import ProjectFileEditorExecutor, ProjectFileEditorTool


def _executor(project: ProjectContext) -> ProjectFileEditorExecutor:
    return ProjectFileEditorExecutor(
        project=project,
        delegate=FileEditorExecutor(workspace_root=str(project.root)),
    )


def test_project_file_editor_delegates_valid_project_action(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    target = tmp_path / "analysis.py"

    observation = _executor(project)(
        FileEditorAction(
            command="create",
            path=str(target),
            file_text="print('synthetic')\n",
        )
    )

    assert observation.is_error is False
    assert target.read_text(encoding="utf-8") == "print('synthetic')\n"


@pytest.mark.parametrize("reserved_path", [".heartwood/config.toml", ".heartwood/logs/run.log"])
def test_project_file_editor_blocks_reserved_state(
    tmp_path: Path,
    reserved_path: str,
) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()

    observation = _executor(project)(
        FileEditorAction(command="view", path=str(tmp_path / reserved_path))
    )

    assert observation.is_error is True
    assert "reserved .heartwood" in observation.text


def test_project_file_editor_blocks_parent_and_symbolic_link_escapes(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    project = ProjectContext(project_root)
    project.initialize()
    (project.root / "linked").symlink_to(outside, target_is_directory=True)
    executor = _executor(project)

    for target in (outside / "parent.txt", project.root / "linked" / "linked.txt"):
        observation = executor(
            FileEditorAction(command="create", path=str(target), file_text="blocked\n")
        )
        assert observation.is_error is True
        assert "outside the current project" in observation.text
        assert not target.exists()


def _conversation_state(working_dir: Path) -> object:
    return SimpleNamespace(
        workspace=SimpleNamespace(working_dir=str(working_dir)),
        agent=SimpleNamespace(llm=SimpleNamespace(vision_is_active=lambda: False)),
    )


def test_project_file_editor_tool_wraps_upstream_executor(tmp_path: Path) -> None:
    tool = ProjectFileEditorTool.create(
        cast(Any, _conversation_state(tmp_path)),
        project_root=str(tmp_path),
    )[0]

    assert tool.name == "file_editor"
    assert isinstance(tool.executor, ProjectFileEditorExecutor)
    assert str(tmp_path) in tool.description


def test_project_file_editor_tool_rejects_workspace_mismatch(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()

    with pytest.raises(ProjectStateError, match="workspace must match"):
        ProjectFileEditorTool.create(
            cast(Any, _conversation_state(tmp_path)),
            project_root=str(other),
        )
