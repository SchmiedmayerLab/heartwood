# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Operator-owned settings for the managed worker's sandbox driver."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from heartwood.core_adapter.worker_attempts import ImageDigest
from heartwood.gateway.worker_inputs import WorkerInputManifest
from heartwood.schemas.experiments import Digest

_Name = Annotated[str, Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")]


def _require_canonical(*paths: Path) -> None:
    for path in paths:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Worker deployment paths must be absolute and canonical")


_CONFIG = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class _WorkerSettings(BaseModel):
    """Select trusted native tools and a qualified route without embedding credentials."""

    model_config = _CONFIG

    gateway: _Name
    native_config_home: Path
    openshell_cli: Path
    workspace: _Name
    image_digest: ImageDigest
    sandbox_policy_sha256: Digest
    policy_path: Path
    policy_file_sha256: Digest
    profile_id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    model_endpoint: str = Field(min_length=1, max_length=2048)
    provider: _Name
    placeholder_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=128)
    inputs: WorkerInputManifest
    cpu: str = Field(pattern=r"^[1-9][0-9]*(?:m)?$", max_length=16)
    memory: str = Field(pattern=r"^[1-9][0-9]*(?:Mi|Gi)$", max_length=16)

    @model_validator(mode="after")
    def _explicit_locations(self) -> Self:
        _require_canonical(self.native_config_home, self.openshell_cli, self.policy_path)
        endpoint = urlsplit(self.model_endpoint)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("Worker inference requires an explicit HTTPS endpoint")
        return self


class OpenShellWorkerDeployment(_WorkerSettings):
    """The Docker reference deployment: one local daemon behind an explicit socket."""

    # The compute driver implementation the sandbox gateway must report for this driver.
    gateway_driver: ClassVar[str] = "openshell/docker"

    driver: Literal["openshell-docker"]
    docker_cli: Path
    docker_host: str
    docker_socket_group: int | None = Field(default=None, ge=0, le=4294967294, strict=True)

    @model_validator(mode="after")
    def _explicit_docker(self) -> Self:
        _require_canonical(self.docker_cli)
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
            raise ValueError("The Docker reference deployment requires an explicit local socket")
        return self


class KubernetesWorkerDeployment(_WorkerSettings):
    """A Kubernetes deployment: sandboxes run as pods in one namespace of one cluster."""

    gateway_driver: ClassVar[str] = "openshell/kubernetes"

    driver: Literal["openshell-kubernetes"]
    # The sandbox command-line tool accepts a workspace name of at most 19 characters with
    # this driver.
    workspace: Annotated[str, Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,17}[a-z0-9])?$")]
    kubectl_cli: Path
    kubeconfig: Path
    namespace: _Name
    # The registry repository of the worker image; pods reference it with `image_digest`.
    image_repository: str = Field(
        pattern=(
            r"^(?:localhost|[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+)(?::[0-9]{1,5})?"
            r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$"
        ),
        max_length=255,
    )
    # Images the driver adds to every sandbox: the supervisor pod's image and the sandbox
    # runtime that the workload pod's first init container installs.
    supervisor_image_digest: ImageDigest
    sandbox_image_digest: ImageDigest

    @model_validator(mode="after")
    def _explicit_kubernetes(self) -> Self:
        _require_canonical(self.kubectl_cli, self.kubeconfig)
        return self

    @property
    def image(self) -> str:
        """The worker image as a pod references it: by digest, never by a tag that could move."""
        return f"{self.image_repository}@{self.image_digest}"


WorkerDeployment = OpenShellWorkerDeployment | KubernetesWorkerDeployment
# The adapter needs its own configuration: the models' setting does not reach union errors.
_WORKER_DEPLOYMENT: TypeAdapter[WorkerDeployment] = TypeAdapter(
    Annotated[WorkerDeployment, Field(discriminator="driver")],
    config=ConfigDict(hide_input_in_errors=True),
)


def worker_deployment_from_mapping(value: object) -> WorkerDeployment:
    """Validate a manifest's worker table as the settings of the driver it selects."""
    return _WORKER_DEPLOYMENT.validate_python(value)
