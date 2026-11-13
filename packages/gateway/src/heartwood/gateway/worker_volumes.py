# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Attempt-owned read-only staging through the Docker reference platform's native CLI."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from heartwood.core_adapter.worker_attempts import (
    ImageDigest,
    WorkerAttemptError,
    WorkerAttemptIntent,
)


class WorkerVolumeSettings(BaseModel):
    """Operator-selected tools and image for the local Docker reference platform."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )

    docker_cli: Path
    docker_host: str
    workspace: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    image_digest: ImageDigest

    @model_validator(mode="after")
    def _explicit_locations(self) -> Self:
        if not self.docker_cli.is_absolute() or ".." in self.docker_cli.parts:
            raise ValueError("Docker executable must be an absolute canonical path")
        socket = urlsplit(self.docker_host)
        if (
            socket.scheme != "unix"
            or socket.netloc
            or not socket.path.startswith("/")
            or socket.query
            or socket.fragment
            or ".." in Path(socket.path).parts
            or "%" in socket.path
        ):
            raise ValueError("Docker staging requires an explicit local socket")
        return self


class WorkerVolumes:
    """Own only resources carrying the complete project and attempt identity."""

    def __init__(self, settings: WorkerVolumeSettings, intent: WorkerAttemptIntent) -> None:
        """Retain identities before provisioning any native resource."""
        settings = WorkerVolumeSettings.model_validate(settings)
        if settings.image_digest != intent.image_digest:
            raise WorkerAttemptError("staging image does not match the retained worker intent")
        self.settings = settings
        self._staging_attempted = False
        self.labels = {
            "heartwood.project": str(intent.project_id),
            "heartwood.attempt": str(intent.attempt_id),
        }
        self.names = {
            mount: f"hw-{intent.attempt_id.hex}-{mount}" for mount in ("inputs", "skills")
        }
        self.staging_name = f"hw-stage-{intent.attempt_id.hex}"
        self.worker_name = f"hw-{intent.attempt_id.hex[:16]}"
        self._worker_prefix = f"openshell-{settings.workspace}--{self.worker_name}-"

    def command(self, *args: str) -> bytes:
        """Use the explicit local daemon without inherited Docker or proxy configuration."""
        try:
            with TemporaryDirectory(prefix="worker-docker-") as config:
                result = subprocess.run(
                    [
                        str(self.settings.docker_cli),
                        "--config",
                        config,
                        "--host",
                        self.settings.docker_host,
                        *args,
                    ],
                    env={"PATH": os.defpath},
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired):
            raise WorkerAttemptError("native Docker operation requires reconciliation") from None
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise WorkerAttemptError("native Docker operation requires reconciliation")
        return result.stdout

    def _label_arguments(self, *, role: str) -> list[str]:
        return [
            argument
            for name, value in {**self.labels, "heartwood.role": role}.items()
            for argument in ("--label", f"{name}={value}")
        ]

    def _inspect(self, kind: str, name: str) -> dict[str, Any]:
        try:
            value = json.loads(self.command(kind, "inspect", name))
        except (ValueError, UnicodeError):
            raise WorkerAttemptError("native resource inspection was incomplete") from None
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise WorkerAttemptError("native resource inspection was incomplete")
        return value[0]

    def _owned(self, observed: dict[str, Any], *, container: bool, role: str) -> None:
        config = observed.get("Config") if container else observed
        labels = config.get("Labels") if isinstance(config, dict) else None
        expected = {**self.labels, "heartwood.role": role}
        if not isinstance(labels, dict) or any(labels.get(k) != v for k, v in expected.items()):
            raise WorkerAttemptError("native resource does not belong to this worker attempt")

    def stage(self, sources: dict[str, Path]) -> None:
        """Copy verified private snapshots before the untrusted worker is created."""
        if self._staging_attempted:
            raise WorkerAttemptError("worker staging cannot repeat an uncertain transfer")
        self._staging_attempted = True
        if set(sources) != set(self.names):
            raise WorkerAttemptError("worker staging requires explicit inputs and Skills")
        mounts: list[str] = []
        for source in sources.values():
            if (
                not source.is_absolute()
                or source.resolve(strict=True) != source
                or not source.is_dir()
            ):
                raise WorkerAttemptError("worker staging requires canonical private snapshots")
            metadata = source.stat()
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise WorkerAttemptError("worker staging requires operator-owned private snapshots")
        for mount, name in self.names.items():
            if self._volume_present(name):
                raise WorkerAttemptError("worker projection volume already exists")
            self.command(
                "volume",
                "create",
                *self._label_arguments(role="projection"),
                "--label",
                "openshell.ai/sandbox-attachable=true",
                "--label",
                f"openshell.ai/sandbox-attachable-workspace={self.settings.workspace}",
                name,
            )
            self._owned(self._inspect("volume", name), container=False, role="projection")
            mounts.extend(("--mount", f"type=volume,source={name},target=/{mount}"))
        self.command(
            "container",
            "create",
            "--name",
            self.staging_name,
            "--pull=never",
            "--network=none",
            "--cap-drop=ALL",
            # Native copy preserves ownership; only this trusted preparation process
            # may make those files readable before the read-only worker mount exists.
            "--cap-add=FOWNER",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--pids-limit=32",
            "--memory=256m",
            "--cpus=1",
            "--user=0:0",
            *self._label_arguments(role="staging"),
            *mounts,
            "--entrypoint=/bin/chmod",
            self.settings.image_digest,
            "-R",
            "a+rX",
            "/inputs",
            "/skills",
        )
        staging = self._inspect("container", self.staging_name)
        self._owned(staging, container=True, role="staging")
        if staging.get("Name") != "/" + self.staging_name or not isinstance(staging.get("Id"), str):
            raise WorkerAttemptError("native staging container identity changed")
        for mount, source in sources.items():
            self.command("container", "cp", str(source) + "/.", f"{staging['Id']}:/{mount}")
        self.command("container", "start", "--attach", str(staging["Id"]))
        finished = self._inspect("container", self.staging_name)
        self._owned(finished, container=True, role="staging")
        state = finished.get("State")
        if (
            finished.get("Id") != staging.get("Id")
            or not isinstance(state, dict)
            or state.get("Running") is not False
            or state.get("ExitCode") != 0
        ):
            raise WorkerAttemptError("worker projection preparation did not complete")
        self._remove_staging()

    def mounts(self) -> list[dict[str, str | bool]]:
        """Supply only the two attempt-owned native read-only volume mounts."""
        return [
            {"type": "volume", "source": name, "target": f"/{mount}", "read_only": True}
            for mount, name in self.names.items()
        ]

    def inspect_worker(self, name: str) -> None:
        """Observe the actual Docker image, network and projection mounts outside the worker."""
        if not name.startswith(self._worker_prefix) or not name.removeprefix(self._worker_prefix):
            raise WorkerAttemptError("native worker name does not match the retained attempt")
        observed = self._inspect("container", name)
        if observed.get("Name") != "/" + name:
            raise WorkerAttemptError("native worker container identity changed")
        self._owned(observed, container=True, role="worker")
        host = observed.get("HostConfig")
        if (
            not isinstance(host, dict)
            or observed.get("Image") != self.settings.image_digest
            or host.get("NetworkMode") != "none"
        ):
            raise WorkerAttemptError("native worker image or network does not match admission")
        mounts = observed.get("Mounts")
        if not isinstance(mounts, list) or any(not isinstance(m, dict) for m in mounts):
            raise WorkerAttemptError("native worker projection mounts were not observed")
        for mount, volume in self.names.items():
            target = PurePosixPath("/" + mount)
            matching = []
            for observed_mount in mounts:
                destination = observed_mount.get("Destination")
                if not isinstance(destination, str):
                    raise WorkerAttemptError("native worker mount destination was not observed")
                path = PurePosixPath(destination)
                if path == target or path.is_relative_to(target) or target.is_relative_to(path):
                    matching.append(observed_mount)
            if len(matching) != 1 or (
                matching[0].get("Type") != "volume"
                or matching[0].get("Name") != volume
                or matching[0].get("RW") is not False
            ):
                raise WorkerAttemptError("native worker projection mount is not read-only")

    def _containers(self, *, role: str) -> list[str]:
        filters = [
            argument
            for key, value in {**self.labels, "heartwood.role": role}.items()
            for argument in ("--filter", f"label={key}={value}")
        ]
        return (
            self.command("container", "ls", "--all", "--quiet", "--no-trunc", *filters)
            .decode()
            .split()
        )

    def _named_containers(self, pattern: str) -> list[str]:
        return (
            self.command(
                "container", "ls", "--all", "--quiet", "--no-trunc", "--filter", "name=" + pattern
            )
            .decode()
            .split()
        )

    def _remove_staging(self) -> None:
        identities = set(self._containers(role="staging")) | set(
            self._named_containers("^/" + self.staging_name + "$")
        )
        for identity in sorted(identities):
            observed = self._inspect("container", identity)
            self._owned(observed, container=True, role="staging")
            if observed.get("Id") != identity or observed.get("Name") != "/" + self.staging_name:
                raise WorkerAttemptError("unrecognized native staging resource")
            self.command("container", "rm", "--force", identity)

    def remove_after_revocation(self) -> None:
        """Require independently observed worker absence before removing owned projections."""
        if self._containers(role="worker") or self._named_containers("^/" + self._worker_prefix):
            raise WorkerAttemptError("native worker remains after control-plane revocation")
        self._remove_staging()
        for name in self.names.values():
            if not self._volume_present(name):
                continue
            self._owned(self._inspect("volume", name), container=False, role="projection")
            self.command("volume", "rm", name)

    def _volume_present(self, name: str) -> bool:
        # A successful listing distinguishes absence from an unavailable daemon.
        present = (
            self.command("volume", "ls", "--quiet", "--filter", "name=^" + name + "$")
            .decode()
            .split()
        )
        if present and present != [name]:
            raise WorkerAttemptError("native volume identity is ambiguous")
        return bool(present)
