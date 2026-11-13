# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Operator-owned settings for the native Docker worker reference deployment."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from heartwood.core_adapter.worker_attempts import ImageDigest
from heartwood.gateway.worker_inputs import WorkerInputManifest
from heartwood.schemas.experiments import Digest

_Name = Annotated[str, Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")]


class OpenShellWorkerDeployment(BaseModel):
    """Select trusted native tools and a qualified route without embedding credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    driver: Literal["openshell-docker"]
    gateway: _Name
    native_config_home: Path
    openshell_cli: Path
    docker_cli: Path
    docker_host: str
    docker_socket_group: int | None = Field(default=None, ge=0, le=4294967294, strict=True)
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
        for path in (
            self.native_config_home,
            self.openshell_cli,
            self.docker_cli,
            self.policy_path,
        ):
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("Worker deployment paths must be absolute and canonical")
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
