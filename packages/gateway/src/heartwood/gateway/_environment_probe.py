# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Explicit terminal probe writing one fresh, confined Python environment artifact."""

import argparse
import os
import sys

from heartwood.core_adapter.research_checks import MAX_RESEARCH_TEXT_BYTES
from heartwood.gateway._project import ProjectContext
from heartwood.gateway._workspace import WorkspaceInspector, _open_child_directory
from heartwood.gateway.python_environment import observe_python_environment
from heartwood.schemas.project_paths import project_relative_path
from heartwood.schemas.python_environment import PythonEnvironmentSnapshot


def _snapshot_bytes() -> bytes:
    payload = (observe_python_environment().model_dump_json(indent=2) + "\n").encode()
    if len(payload) > MAX_RESEARCH_TEXT_BYTES:
        raise ValueError("Python environment metadata exceeds the inspection limit")
    return payload


def capture_environment(project: ProjectContext, output_directory: str) -> None:
    """Never overwrite a prior observation or follow a project directory link."""
    relative = project_relative_path(output_directory, allow_root=False)
    payload = _snapshot_bytes()
    workspace = WorkspaceInspector(project)
    with workspace._open_directory(relative.parent) as parent:
        os.mkdir(relative.name, mode=0o700, dir_fd=parent)
        descriptor = _open_child_directory(parent, relative.name)
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            file_descriptor = os.open("environment.json", flags, mode=0o600, dir_fd=descriptor)
            with os.fdopen(file_descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(descriptor)
            os.fsync(parent)
        finally:
            os.close(descriptor)


def require_environment(project: ProjectContext, path: str) -> None:
    """Refuse analysis when the captured interpreter or package versions have changed."""
    record = WorkspaceInspector(project).file(path)
    if record["status"] != "available" or record["content"] is None:
        raise ValueError("The required Python environment record is unavailable")
    required = PythonEnvironmentSnapshot.model_validate_json(record["content"])
    if required.differences(observe_python_environment()):
        raise ValueError("Python environment changed; capture and review a new verification run")


def main() -> None:
    """Execute only when called explicitly, ordinarily through a reviewed terminal action."""
    parser = argparse.ArgumentParser(description="Capture this Python runtime without changing it.")
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--output-dir")
    output.add_argument("--stdout", action="store_true")
    output.add_argument("--require")
    args = parser.parse_args()
    if args.stdout:
        sys.stdout.buffer.write(_snapshot_bytes())
    elif args.require:
        require_environment(ProjectContext.current(), args.require)
    else:
        capture_environment(ProjectContext.current(), args.output_dir)


if __name__ == "__main__":
    main()
