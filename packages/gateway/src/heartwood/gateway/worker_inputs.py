# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Bounded input snapshots prepared outside the worker by a trusted launcher."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from heartwood.gateway._workspace import WorkspaceInspector
from heartwood.schemas.experiments import ExperimentFile

_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024


class WorkerInputManifest(BaseModel):
    """Explicit selected content; neither permission to disclose nor runtime attestation."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always", hide_input_in_errors=True
    )

    schema_version: Literal["heartwood.worker-inputs.v1"] = "heartwood.worker-inputs.v1"
    files: tuple[ExperimentFile, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def _bounded_files(self) -> Self:
        paths = [unicodedata.normalize("NFC", item.path).casefold() for item in self.files]
        selected = set(paths)
        if len(selected) != len(paths) or any(
            parent.as_posix() in selected
            for path in paths
            for parent in PurePosixPath(path).parents
        ):
            raise ValueError("Worker input paths must be unique and cannot overlap")
        if (
            any(item.size_bytes > _MAX_FILE_BYTES for item in self.files)
            or sum(item.size_bytes for item in self.files) > _MAX_TOTAL_BYTES
        ):
            raise ValueError("Worker inputs exceed the transfer byte limit")
        return self

    @property
    def sha256(self) -> str:
        """Bind the version and selected file identities independent of selection order."""
        payload = self.model_dump(mode="json")
        payload["files"] = sorted(payload["files"], key=lambda item: item["path"])
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@contextmanager
def stage_worker_inputs(
    inspector: WorkspaceInspector, manifest: WorkerInputManifest, *, staging_parent: Path
) -> Iterator[Path]:
    """Copy verified bytes into a private temporary directory for a synchronous transfer.

    The operator must own and protect the staging parent and its ancestors outside
    the worker filesystem. The consumer must finish transfer before this context
    exits and independently verify the destination before admitting the worker.
    """
    manifest = WorkerInputManifest.model_validate(manifest)
    parent = staging_parent.resolve(strict=True)
    if parent.is_relative_to(inspector.project.root.resolve()):
        raise ValueError("Worker input staging must be outside the project")
    metadata = parent.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("Worker input staging requires an operator-owned private directory")
    with TemporaryDirectory(prefix="worker-inputs-", dir=parent) as temporary:
        root = Path(temporary)
        for artifact in manifest.files:
            content = inspector.read_verified(artifact, max_bytes=artifact.size_bytes)
            destination = root.joinpath(*PurePosixPath(artifact.path).parts)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(content)
            destination.chmod(0o400)
        yield root
