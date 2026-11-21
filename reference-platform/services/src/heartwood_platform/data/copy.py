# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The copy job that fills a project data volume: python -m heartwood_platform.data.copy.

Arguments: the access plan file, the platform CA certificate and the volume's directory.
The job downloads each object through its presigned URL, verifies the object's size and
SHA-256 checksum, and writes it below the volume's directory, readable by every user.
It holds no credential of the object store; the plan's URLs read one object each and
expire. A failed object fails the job, and the control service runs a new job with new
URLs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import sys
from pathlib import Path, PurePosixPath
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from heartwood_platform.data.catalog import content_sha256_of

_CHUNK = 1024 * 1024
# Files the copy writes are readable by the worker, which runs as another user.
_FILE_MODE = 0o644
_DIRECTORY_MODE = 0o755


class CopyError(Exception):
    """The volume could not be filled with the planned objects."""


class PlannedObject(BaseModel):
    """One object of an access plan."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    path: Annotated[
        str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
    ]
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    url: Annotated[str, StringConstraints(pattern=r"^https://")]


class Plan(BaseModel):
    """The objects of a project data volume and the digest of its content."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    objects: tuple[PlannedObject, ...]


def _clear(directory: Path) -> None:
    """Remove what an earlier, failed job left in the volume.

    The directory a file system keeps for recovered files belongs to it and stays.
    """
    for child in directory.iterdir():
        if child.name == "lost+found":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _download(client: httpx.Client, planned: PlannedObject, target: Path) -> None:
    digest = hashlib.sha256()
    received = 0
    partial = target.with_name(target.name + ".partial")
    with client.stream("GET", planned.url) as response:
        if response.status_code != httpx.codes.OK:
            raise CopyError(
                f"object {planned.id}: the object store answered {response.status_code}"
            )
        with partial.open("xb") as file:
            for chunk in response.iter_bytes(_CHUNK):
                received += len(chunk)
                if received > planned.size:
                    raise CopyError(f"object {planned.id}: more bytes than its size")
                digest.update(chunk)
                file.write(chunk)
    if received != planned.size or digest.hexdigest() != planned.sha256:
        raise CopyError(f"object {planned.id}: size or checksum differs from the catalog")
    partial.chmod(_FILE_MODE)
    partial.rename(target)


def copy(plan: Plan, client: httpx.Client, directory: Path) -> None:
    """Fill ``directory`` with the objects of ``plan``, verified against the catalog."""
    objects = [(item.id, item.path, item.size, item.sha256) for item in plan.objects]
    if content_sha256_of(objects) != plan.content_sha256:
        raise CopyError("the plan's objects do not match its content digest")
    paths = [PurePosixPath(item.path) for item in plan.objects]
    if len(set(paths)) != len(paths):
        raise CopyError("the plan names a path twice")
    _clear(directory)
    for planned, path in zip(plan.objects, paths, strict=True):
        target = directory.joinpath(*path.parts)
        target.parent.mkdir(mode=_DIRECTORY_MODE, exist_ok=True)
        target.parent.chmod(_DIRECTORY_MODE)
        _download(client, planned, target)


def main() -> None:
    """Copy the plan named on the command line into the volume's directory."""
    plan_file, ca_file, directory = (Path(argument) for argument in sys.argv[1:4])
    plan = Plan.model_validate_json(plan_file.read_text(encoding="utf-8"))
    os.umask(0o022)
    context = ssl.create_default_context(cafile=str(ca_file))
    try:
        # The URLs name the object store; no redirect to another host is followed.
        with httpx.Client(verify=context, timeout=60, follow_redirects=False) as client:
            copy(plan, client, directory)
    except CopyError as error:
        # The message names the object, never its URL.
        print(f"project data copy failed: {error}", file=sys.stderr)
        sys.exit(1)
    except (httpx.HTTPError, OSError) as error:
        # Only the kind of error: its message could contain a URL.
        print(f"project data copy failed: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"content_sha256": plan.content_sha256, "objects": len(plan.objects)}))


if __name__ == "__main__":
    main()
