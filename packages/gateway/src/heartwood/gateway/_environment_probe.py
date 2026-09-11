# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Explicit terminal probe writing one fresh, confined Python environment artifact."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from heartwood.core_adapter.research_checks import MAX_RESEARCH_TEXT_BYTES
from heartwood.gateway._analysis_environment import (
    environment_directory,
    environment_python,
    read_environment,
    reconstruct_environment,
)
from heartwood.gateway._project import ProjectContext
from heartwood.gateway._workspace import WorkspaceInspector, _open_child_directory
from heartwood.gateway.python_environment import (
    inspect_verification_environment,
    observe_python_environment,
)
from heartwood.schemas.project_paths import project_relative_path


def _snapshot_bytes(python: Path | None = None) -> bytes:
    snapshot = inspect_verification_environment(python) if python else observe_python_environment()
    payload = (snapshot.model_dump_json(indent=2) + "\n").encode()
    if len(payload) > MAX_RESEARCH_TEXT_BYTES:
        raise ValueError("Python environment metadata exceeds the inspection limit")
    return payload


def capture_environment(
    project: ProjectContext, output_directory: str, python: Path | None = None
) -> None:
    """Never overwrite a prior observation or follow a project directory link."""
    relative = project_relative_path(output_directory, allow_root=False)
    payload = _snapshot_bytes(python)
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


def require_environment(project: ProjectContext, path: str, python: Path | None = None) -> None:
    """Refuse analysis when the captured interpreter or package versions have changed."""
    required = read_environment(project, path)
    observed = inspect_verification_environment(python) if python else observe_python_environment()
    if required.differences(observed):
        raise ValueError("Python environment changed; capture and review a new verification run")


def main() -> None:
    """Execute only when called explicitly, ordinarily through a reviewed terminal action."""
    parser = argparse.ArgumentParser(description="Capture or reconstruct an analysis environment.")
    parser.add_argument("--output-dir")
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument("--require")
    parser.add_argument("--environment-id")
    parser.add_argument("--lockfile")
    parser.add_argument("--expected")
    parser.add_argument("--program")
    parser.add_argument("--data")
    parser.add_argument("--python", type=Path)
    args = parser.parse_args()
    project = ProjectContext.current()
    if args.environment_id:
        if args.stdout or args.python or not args.output_dir:
            parser.error("Reconstruction requires its bound environment and output directory")
        if args.lockfile and args.expected and not (args.program or args.data or args.require):
            python = reconstruct_environment(
                project,
                environment_id=args.environment_id,
                lockfile=args.lockfile,
                expected=args.expected,
            )
            capture_environment(project, args.output_dir, python)
        elif args.program and args.data and args.require and not (args.lockfile or args.expected):
            python = environment_python(environment_directory(project, args.environment_id))
            require_environment(project, args.require, python)
            for path in (args.program, args.data, args.output_dir):
                project_relative_path(path, allow_root=False)
                project.require_project_path(Path(path))
            raise SystemExit(
                subprocess.run(
                    [
                        str(python),
                        "-I",
                        args.program,
                        "--data",
                        args.data,
                        "--output-dir",
                        args.output_dir,
                    ],
                    cwd=project.root,
                    env={"PATH": os.defpath},
                    check=False,
                ).returncode
            )
        else:
            parser.error("Supply either a dependency lock and expected record or guarded analysis")
    else:
        if (
            any((args.lockfile, args.expected, args.program, args.data))
            or sum(bool(value) for value in (args.stdout, args.require, args.output_dir)) != 1
        ):
            parser.error("Choose exactly one environment capture or comparison operation")
        if args.stdout:
            sys.stdout.buffer.write(_snapshot_bytes(args.python))
        elif args.require:
            require_environment(project, args.require, args.python)
        else:
            capture_environment(project, args.output_dir, args.python)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Analysis environment operation failed: {error}") from None
