# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Entry point of the job service: python -m heartwood_platform.jobs CONFIGURATION."""

from __future__ import annotations

import logging
import ssl
import sys
import threading
from pathlib import Path
from typing import Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.applications import Starlette

from heartwood_platform.common.serving import TlsSettings, serve
from heartwood_platform.common.signing import MAX_EXPIRES_SECONDS, Credentials
from heartwood_platform.common.tokens import ResearcherTokens
from heartwood_platform.control.app import ProjectId
from heartwood_platform.jobs.app import create_jobs_app
from heartwood_platform.jobs.objects import ProjectBucket, StoreObjects
from heartwood_platform.jobs.platform import ControlContexts
from heartwood_platform.jobs.records import Records
from heartwood_platform.jobs.service import (
    IMAGE,
    URL_MARGIN_SECONDS,
    JobService,
    ProjectPolicy,
    ServiceSettings,
)
from heartwood_platform.jobs.tes import HttpTesExecutor, TesExecutor

_LOGGER = logging.getLogger(__name__)


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


class ExecutorSettings(_Settings):
    """How the job service reaches one project's TES executor."""

    url: str = Field(pattern=r"^https://[^/\s]+(/\S*)?$")
    ca_file: Path
    user: str
    password_file: Path


class StoreAccountSettings(_Settings):
    """The job service's object store account for one project's bucket."""

    bucket: str
    access_key_file: Path
    secret_key_file: Path


class ProjectSettings(_Settings):
    """One project with an executor, its entitlements and its limits."""

    id: ProjectId
    executor: ExecutorSettings
    store: StoreAccountSettings
    gpu: bool = False
    max_cpu_cores: int = Field(ge=1)
    max_ram_gb: float = Field(gt=0)
    max_disk_gb: float = Field(gt=0)
    max_runtime_seconds: int = Field(ge=1)


class StoreSettings(_Settings):
    """The object store that holds project data and results."""

    # The HTTPS address under which task pods reach the store; presigned URLs name it.
    endpoint: str = Field(pattern=r"^https://[^/\s]+$")
    region: str = "us-east-1"
    # The platform CA, which signs the store's serving certificate.
    ca_file: Path


class ControlSettings(_Settings):
    """How the job service reaches the control service."""

    url: str = Field(pattern=r"^https://[^/\s]+$")
    ca_file: Path
    token_file: Path


class Settings(_Settings):
    """The configuration file of the job service."""

    database: Path
    audience: str
    issuer: str
    # The keys of the identity provider, which signs researchers' tokens.
    keys_url: str = Field(pattern=r"^https://\S+$")
    keys_ca_file: Path
    tls: TlsSettings
    control: ControlSettings
    store: StoreSettings
    images: frozenset[str]
    start_deadline_seconds: int = Field(default=600, ge=30, le=86400)
    # Shorter than the margin of an input URL, so a missed start deadline is enforced
    # before the task's URLs expire.
    reconcile_seconds: float = Field(default=60, ge=5, lt=URL_MARGIN_SECONDS)
    cpu_profile: str
    gpu_profile: str
    drs_host: str
    projects: tuple[ProjectSettings, ...]

    @model_validator(mode="after")
    def _validate(self) -> Self:
        identifiers = [project.id for project in self.projects]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("project identifiers must be unique")
        if any(IMAGE.fullmatch(image) is None for image in self.images):
            raise ValueError("task images are named by digest")
        # An output URL must stay valid for the whole task, within the signature's limit.
        longest = max((project.max_runtime_seconds for project in self.projects), default=0)
        if self.start_deadline_seconds + URL_MARGIN_SECONDS + longest > MAX_EXPIRES_SECONDS:
            raise ValueError("start deadline and run time limit exceed the URL validity")
        return self


def build(settings: Settings) -> tuple[Starlette, JobService]:
    """Assemble the application and the service from the configuration."""
    executors: dict[str, TesExecutor] = {}
    policies: dict[str, ProjectPolicy] = {}
    buckets: dict[str, ProjectBucket] = {}
    for project in settings.projects:
        executors[project.id] = HttpTesExecutor(
            project.executor.url,
            project.executor.ca_file,
            project.executor.user,
            _read(project.executor.password_file),
        )
        policies[project.id] = ProjectPolicy(
            gpu=project.gpu,
            max_cpu_cores=project.max_cpu_cores,
            max_ram_gb=project.max_ram_gb,
            max_disk_gb=project.max_disk_gb,
            max_runtime_seconds=project.max_runtime_seconds,
        )
        buckets[project.id] = ProjectBucket(
            project.store.bucket,
            Credentials(_read(project.store.access_key_file), _read(project.store.secret_key_file)),
        )
    # Short, because a submission checks each of its inputs under the project's lock.
    store = httpx.Client(
        timeout=5, verify=ssl.create_default_context(cafile=settings.store.ca_file)
    )
    service = JobService(
        Records(settings.database),
        executors,
        policies,
        StoreObjects(settings.store.endpoint, settings.store.region, buckets, store),
        # The control service offers no job reservations to other services yet.
        None,
        ServiceSettings(
            images=settings.images,
            start_deadline_seconds=settings.start_deadline_seconds,
            cpu_profile=settings.cpu_profile,
            gpu_profile=settings.gpu_profile,
            drs_host=settings.drs_host,
        ),
    )
    keys_client = httpx.Client(
        timeout=30, verify=ssl.create_default_context(cafile=settings.keys_ca_file)
    )

    def keys() -> dict[str, Any]:
        response = keys_client.get(settings.keys_url)
        response.raise_for_status()
        document = response.json()
        if not isinstance(document, dict):
            raise httpx.DecodingError("the key set is not an object")
        return document

    contexts = ControlContexts(
        settings.control.url,
        settings.control.ca_file,
        lambda: _read(settings.control.token_file),
    )
    application = create_jobs_app(
        service, ResearcherTokens(settings.issuer, settings.audience, keys), contexts
    )
    return application, service


def reconcile_forever(service: JobService, interval: float, stop: threading.Event) -> None:
    """Reconcile every project's tasks with the records until ``stop`` is set."""
    # The first round runs at start, so tasks of a stopped service are handled at once.
    while True:
        for project in service.projects():
            try:
                cancelled = service.reconcile(project)
            except Exception:
                # The loop enforces deadlines and cancels unrecorded tasks, so it never stops.
                _LOGGER.exception("reconciling %s failed", project)
                continue
            if cancelled:
                _LOGGER.warning("cancelled %d unrecorded tasks of %s", len(cancelled), project)
        if stop.wait(interval):
            return


def main() -> None:
    """Serve the job service with the configuration file named on the command line."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = Settings.model_validate_json(Path(sys.argv[1]).read_text(encoding="utf-8"))
    application, service = build(settings)
    stop = threading.Event()
    worker = threading.Thread(
        target=reconcile_forever,
        args=(service, settings.reconcile_seconds, stop),
        name="reconcile",
        daemon=True,
    )
    worker.start()
    try:
        serve(application, settings.tls)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
