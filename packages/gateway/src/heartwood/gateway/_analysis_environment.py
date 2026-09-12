# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Explicit lock-backed analysis setup; uv owns dependency and wheel validation."""

import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import tomli_w

from heartwood.gateway._project import ProjectContext
from heartwood.gateway._workspace import WorkspaceInspector
from heartwood.gateway.python_environment import inspect_verification_environment
from heartwood.persistence import write_private_text_atomic
from heartwood.schemas.project_paths import project_relative_path
from heartwood.schemas.python_environment import PythonEnvironmentSnapshot


def environment_directory(project: ProjectContext, environment_id: str) -> Path:
    """Use one private, fresh directory per verification output binding."""
    if re.fullmatch(r"[0-9a-f]{64}", environment_id) is None:
        raise ValueError("Invalid analysis environment identifier")
    directory = project.runtime_dir / f"analysis-{environment_id}"
    for path in (project.state_root, project.runtime_dir, directory):
        if path.is_symlink():
            raise ValueError("Analysis environment paths must not be symbolic links")
    return directory


def environment_python(directory: Path) -> Path:
    """Return the interpreter created by uv, never the active application environment."""
    return directory / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def read_environment(project: ProjectContext, path: str) -> PythonEnvironmentSnapshot:
    """Read only a bounded public project record."""
    return PythonEnvironmentSnapshot.model_validate_json(_read(project, path))


def _read(project: ProjectContext, path: str) -> str:
    record = WorkspaceInspector(project).file(path)
    if record["status"] != "available" or record["content"] is None:
        raise ValueError("The analysis environment input is unavailable")
    return record["content"]


def _snapshot_lock(project: ProjectContext, lockfile: str, directory: Path) -> Path:
    """Restrict execution sources; leave PEP 751 semantics and hash checks to uv."""
    lock = tomllib.loads(_read(project, lockfile))
    if lock.get("lock-version") != "1.0" or not isinstance(lock.get("packages"), list):
        raise ValueError("Supply a PEP 751 pylock.toml dependency lock")
    for package in lock["packages"]:
        if not isinstance(package, dict):
            raise ValueError("Invalid dependency lock package")
        if any(key in package for key in ("directory", "vcs", "archive")):
            raise ValueError("Analysis reconstruction supports wheels, not source builds")
        wheels = package.get("wheels")
        if not isinstance(wheels, list) or not wheels:
            raise ValueError("Every analysis dependency needs a hash-pinned wheel")
        package.pop("sdist", None)
        for wheel in wheels:
            if not isinstance(wheel, dict) or not isinstance(wheel.get("hashes"), dict):
                raise ValueError("Every analysis wheel needs a SHA-256 hash")
            digest = wheel["hashes"].get("sha256", "")
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError("Every analysis wheel needs a SHA-256 hash")
            if "url" in wheel and "path" not in wheel:
                if not isinstance(wheel["url"], str):
                    raise ValueError("Invalid analysis wheel URL")
                url = urlsplit(wheel["url"])
                if url.scheme != "https" or not url.hostname or url.username or url.password:
                    raise ValueError("Analysis wheel URLs require HTTPS without credentials")
            elif "path" in wheel and "url" not in wheel:
                if not isinstance(wheel["path"], str):
                    raise ValueError("Invalid analysis wheel path")
                relative = project_relative_path(lockfile, allow_root=False).parent / wheel["path"]
                relative = project_relative_path(str(relative), allow_root=False)
                destination = directory / digest
                destination.mkdir(mode=0o700, exist_ok=True)
                target = destination / relative.name
                workspace = WorkspaceInspector(project)
                with workspace._open_directory(relative.parent) as parent:
                    descriptor = os.open(
                        relative.name,
                        os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent,
                    )
                    with os.fdopen(descriptor, "rb") as source, target.open("xb") as output:
                        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                            raise ValueError("Analysis wheels must be regular files")
                        shutil.copyfileobj(source, output)
                wheel["path"] = str(target)
            else:
                raise ValueError("Each analysis wheel needs exactly one URL or project path")
    snapshot = directory / "pylock.toml"
    write_private_text_atomic(snapshot, tomli_w.dumps(lock))
    return snapshot


def reconstruct_environment(
    project: ProjectContext, *, environment_id: str, lockfile: str, expected: str
) -> Path:
    """Create only after action approval; a failed attempt is retained, never reused."""
    required = read_environment(project, expected)
    current = inspect_verification_environment()
    if (required.implementation, required.system, required.machine) != (
        current.implementation,
        current.system,
        current.machine,
    ):
        raise ValueError("The recorded Python platform differs from this environment")
    project.initialize()
    directory = environment_directory(project, environment_id)
    directory.mkdir(mode=0o700)
    lock = _snapshot_lock(project, lockfile, directory)
    home = directory / "home"
    home.mkdir(mode=0o700)
    # No inherited credentials, uv overrides, user configuration, or application venv.
    environment = {
        "PATH": os.defpath,
        "HOME": str(home),
        "UV_CACHE_DIR": str(directory / "cache"),
        "UV_KEYRING_PROVIDER": "disabled",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    python = sys.executable if required.python == current.python else required.python
    uv = [sys.executable, "-I", "-m", "uv", "--no-config", "--no-python-downloads"]
    print("Creating an isolated analysis environment with uv...", flush=True)
    subprocess.run(
        [*uv, "venv", "--python", python, str(directory / "venv")],
        cwd=directory,
        env=environment,
        check=True,
        timeout=120,
    )
    print(
        "Installing and verifying the dependency lock; downloads may take several minutes...",
        flush=True,
    )
    subprocess.run(
        [
            *uv,
            "pip",
            "sync",
            str(lock),
            "--python",
            str(environment_python(directory)),
            "--require-hashes",
            "--no-build",
            "--no-index",
            "--strict",
            "--allow-empty-requirements",
            "--link-mode",
            "copy",
        ],
        cwd=directory,
        env=environment,
        check=True,
        timeout=900,
    )
    return environment_python(directory)
