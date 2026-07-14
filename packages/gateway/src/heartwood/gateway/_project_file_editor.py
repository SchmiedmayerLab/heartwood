# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Project-boundary enforcement for the OpenHands file editor."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

from openhands.sdk.tool import ToolExecutor, register_tool
from openhands.tools.file_editor.definition import (
    FileEditorAction,
    FileEditorObservation,
    FileEditorTool,
)
from openhands.tools.file_editor.impl import FileEditorExecutor

from heartwood.gateway._project import ProjectContext, ProjectStateError

if TYPE_CHECKING:
    from openhands.sdk.conversation import LocalConversation
    from openhands.sdk.conversation.state import ConversationState


PROJECT_FILE_EDITOR_SPEC = "heartwood_project_file_editor"


class ProjectFileEditorExecutor(ToolExecutor[FileEditorAction, FileEditorObservation]):
    """Validate a path, then delegate editing to the OpenHands implementation."""

    def __init__(self, *, project: ProjectContext, delegate: FileEditorExecutor) -> None:
        self._project = project
        self._delegate = delegate

    def __call__(
        self,
        action: FileEditorAction,
        conversation: LocalConversation | None = None,
    ) -> FileEditorObservation:
        try:
            self._project.require_project_path(Path(action.path))
        except ProjectStateError:
            return FileEditorObservation.from_text(
                text=(
                    "Heartwood blocked this file operation because its path is outside "
                    "the current project or inside the reserved .heartwood directory."
                ),
                command=action.command,
                is_error=True,
            )
        return self._delegate(action, conversation)


class ProjectFileEditorTool(FileEditorTool):
    """Expose the OpenHands file editor with Heartwood project confinement."""

    name: ClassVar[str] = FileEditorTool.name

    @classmethod
    def create(
        cls,
        conv_state: ConversationState,
        *,
        project_root: str | None = None,
    ) -> Sequence[ProjectFileEditorTool]:
        workspace = Path(conv_state.workspace.working_dir).resolve()
        resolved_project_root = (
            Path(project_root).expanduser().resolve() if project_root is not None else workspace
        )
        project = ProjectContext(resolved_project_root)
        if workspace != project.root:
            raise ProjectStateError("OpenHands workspace must match the Heartwood project")
        upstream = FileEditorTool.create(conv_state)[0]
        delegate = cast(FileEditorExecutor, upstream.executor)
        executor = ProjectFileEditorExecutor(project=project, delegate=delegate)
        return [
            cls(
                action_type=upstream.action_type,
                observation_type=upstream.observation_type,
                description=upstream.description,
                annotations=upstream.annotations,
                meta=upstream.meta,
                executor=executor,
            )
        ]


register_tool(PROJECT_FILE_EDITOR_SPEC, ProjectFileEditorTool)
