# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Attempt-owned read-only staging through the Docker reference platform's native CLI."""

from __future__ import annotations

import hashlib
import json
import os
import re
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
    protected_paths: tuple[Path, ...] = ()
    # The worker's requested limits, in the deployment's quantity format.
    cpu: str = Field(pattern=r"^[1-9][0-9]*(?:m)?$", max_length=16)
    memory: str = Field(pattern=r"^[1-9][0-9]*(?:Mi|Gi)$", max_length=16)

    @model_validator(mode="after")
    def _explicit_locations(self) -> Self:
        if not self.docker_cli.is_absolute() or ".." in self.docker_cli.parts:
            raise ValueError("Docker executable must be an absolute canonical path")
        if any(not path.is_absolute() or ".." in path.parts for path in self.protected_paths):
            raise ValueError("protected locations must be absolute canonical paths")
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


def _path_forms(path: str) -> set[PurePosixPath]:
    """Return a host path as written and as resolved, so an alias cannot hide an overlap.

    Resolution only reads link targets; a link changed afterwards cannot remove the
    normalized form that is always compared.
    """
    return {PurePosixPath(os.path.normpath(path)), PurePosixPath(os.path.realpath(path))}


# Docker reports an empty mode for the container's own PID and user namespace, and
# `private` or `none` for an IPC namespace that nothing else can join.
_PRIVATE_NAMESPACE_MODES: dict[str, frozenset[str]] = {
    "PidMode": frozenset({""}),
    "IpcMode": frozenset({"", "private", "none"}),
    "UsernsMode": frozenset({""}),
}
# The container runtime's standard confined process type, as container-selinux v2.251.0
# (archive SHA-256 701271b9221eadec0f5f4c99a74a3ef4214123222b757d78d62ef73690f7d8e7)
# assigns it the container domain attributes. Its former name `svirt_lxc_net_t` is an alias
# that only the base policy defines. A user or role field, or any other type, could select an
# unconfined or privileged domain.
_CONFINED_SELINUX_TYPES = frozenset({"container_t"})
# One sensitivity with individual categories, as the runtime assigns them; a category range
# such as `c0.c1023` would give the worker every other container's categories.
_SELINUX_LEVEL = re.compile(r"s[0-9]+(?::c[0-9]+(?:,c[0-9]+)*)?")


def _confining_selinux_label(value: str) -> bool:
    field, _, setting = value.partition(":")
    if field == "type":
        return setting in _CONFINED_SELINUX_TYPES
    return field == "level" and _SELINUX_LEVEL.fullmatch(setting) is not None


def _no_new_privileges(option: object) -> bool:
    return option in ("no-new-privileges", "no-new-privileges:true", "no-new-privileges=true")


def _native_limits(cpu: str, memory: str) -> tuple[int, int]:
    """Return the CPU and memory limits Docker reports for the requested quantities."""
    nano_cpus = int(cpu[:-1]) * 1_000_000 if cpu.endswith("m") else int(cpu) * 1_000_000_000
    return nano_cpus, int(memory[:-2]) * (1024**3 if memory.endswith("Gi") else 1024**2)


def _confining_security_option(option: object) -> bool:
    """Accept only options that keep or add confinement, never one that replaces a default."""
    if not isinstance(option, str):
        return False
    # Docker accepts either separator after the option name.
    cut = min((i for i in (option.find("="), option.find(":")) if i >= 0), default=len(option))
    name, separator, value = option[:cut], option[cut : cut + 1], option[cut + 1 :]
    if name == "no-new-privileges":
        return not separator or value == "true"
    if name == "apparmor":
        return value == "docker-default"
    if name == "label":
        return _confining_selinux_label(value)
    return False


def _docker(settings: WorkerVolumeSettings, *args: str) -> bytes:
    """Use the explicit local daemon without inherited Docker or proxy configuration."""
    try:
        with TemporaryDirectory(prefix="worker-docker-") as config:
            result = subprocess.run(
                [
                    str(settings.docker_cli),
                    "--config",
                    config,
                    "--host",
                    settings.docker_host,
                    *args,
                ],
                env={"PATH": os.defpath},
                cwd=config,
                capture_output=True,
                timeout=60,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        raise WorkerAttemptError("native Docker operation requires reconciliation") from None
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise WorkerAttemptError("native Docker operation requires reconciliation")
    return result.stdout


def container_runtime_sha256(settings: WorkerVolumeSettings) -> str:
    """Identify the Docker daemon behind the configured socket by the identity it reports.

    The daemon keeps its engine identity across restarts, so a socket re-pointed at another
    daemon, or another daemon behind the same socket path, yields a different identity.
    """
    try:
        daemon = json.loads(_docker(settings, "info", "--format", "{{json .ID}}"))
    except (ValueError, UnicodeError):
        daemon = None
    if not isinstance(daemon, str) or not daemon or len(daemon) > 256:
        raise WorkerAttemptError("container runtime identity was not observed")
    identity = json.dumps({"docker_host": settings.docker_host, "daemon": daemon}, sort_keys=True)
    return hashlib.sha256(identity.encode()).hexdigest()


def attachable_labels(workspace: str) -> dict[str, str]:
    """Labels by which the sandbox gateway approves a volume for one workspace's sandboxes."""
    return {
        "openshell.ai/sandbox-attachable": "true",
        "openshell.ai/sandbox-attachable-workspace": workspace,
    }


def require_private_snapshots(sources: dict[str, Path], names: dict[str, str]) -> None:
    """Require one canonical, operator-owned private directory for each projection."""
    if set(sources) != set(names):
        raise WorkerAttemptError("worker staging requires explicit inputs and Skills")
    for source in sources.values():
        if not source.is_absolute() or source.resolve(strict=True) != source or not source.is_dir():
            raise WorkerAttemptError("worker staging requires canonical private snapshots")
        metadata = source.stat()
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise WorkerAttemptError("worker staging requires operator-owned private snapshots")


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
        return _docker(self.settings, *args)

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
        require_private_snapshots(sources, self.names)
        mounts: list[str] = []
        for mount, name in self.names.items():
            if self._volume_present(name):
                raise WorkerAttemptError("worker projection volume already exists")
            self.command(
                "volume",
                "create",
                *self._label_arguments(role="projection"),
                *(
                    argument
                    for label, value in attachable_labels(self.settings.workspace).items()
                    for argument in ("--label", f"{label}={value}")
                ),
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
        """Observe the actual Docker image, network, confinement and mounts outside the worker."""
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
            or host.get("Privileged") is not False
        ):
            raise WorkerAttemptError(
                "native worker image, network or privilege does not match admission"
            )
        # Every field must be reported; an absent observation is never read as confinement.
        if any(
            name not in host
            for name in (
                "CapAdd",
                "CapDrop",
                "SecurityOpt",
                "PidMode",
                "IpcMode",
                "UsernsMode",
                "CgroupnsMode",
                "Devices",
                "DeviceRequests",
                "DeviceCgroupRules",
                "Binds",
            )
        ):
            raise WorkerAttemptError("native worker confinement was not observed")
        options = host["SecurityOpt"]
        if (
            host["CapAdd"] not in (None, [])
            or not isinstance(host["CapDrop"], list)
            or "ALL" not in host["CapDrop"]
            or not isinstance(options, list)
            or any(not _confining_security_option(option) for option in options)
            or not any(_no_new_privileges(option) for option in options)
            or host["CgroupnsMode"] != "private"
            or any(host[name] not in (None, []) for name in ("Devices", "DeviceRequests"))
            or host["DeviceCgroupRules"] not in (None, [])
            or host["Binds"] not in (None, [])
            # Only namespaces private to the container are accepted; `host`, another
            # container's namespace and any value not listed here are refused.
            or any(
                not isinstance(host[name], str) or host[name] not in allowed
                for name, allowed in _PRIVATE_NAMESPACE_MODES.items()
            )
        ):
            raise WorkerAttemptError("native worker confinement does not match admission")
        # Docker reports an unlimited value as zero, a negative number or null.
        nano_cpus, memory, processes = (host.get(n) for n in ("NanoCpus", "Memory", "PidsLimit"))
        if (
            type(nano_cpus) is not int
            or type(memory) is not int
            or type(processes) is not int
            or (nano_cpus, memory) != _native_limits(self.settings.cpu, self.settings.memory)
            or processes <= 0
        ):
            raise WorkerAttemptError("native worker resource limits do not match admission")
        mounts = observed.get("Mounts")
        if not isinstance(mounts, list) or any(not isinstance(m, dict) for m in mounts):
            raise WorkerAttemptError("native worker projection mounts were not observed")
        withheld = {
            form
            for path in (urlsplit(self.settings.docker_host).path, *self.settings.protected_paths)
            for form in _path_forms(str(path))
        }
        for observed_mount in mounts:
            source = observed_mount.get("Source")
            if isinstance(source, str) and any(
                path.is_relative_to(form) or form.is_relative_to(path)
                for form in _path_forms(source)
                for path in withheld
            ):
                raise WorkerAttemptError("native worker mount exposes a withheld host location")
            # The worker needs no host path; only named volumes are admitted.
            if observed_mount.get("Type") != "volume":
                raise WorkerAttemptError(
                    "native worker has a bind mount or another non-volume mount"
                )
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
