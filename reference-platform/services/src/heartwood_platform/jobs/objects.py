# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Project objects that tasks read and write, through presigned URLs.

A task reads project data by DRS identifier and writes its results under the project's
results prefix. The job service holds one object store account per project, which may
read the project's bucket and write below ``results/``; tasks hold only URLs for single
objects that expire.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx

from heartwood_platform.common.signing import Credentials, presign

# A DRS identifier of the platform's data service: <dataset>.<file>, stored in the
# project's bucket as <dataset>/<file>.
DRS_ID = re.compile(r"^([a-z0-9][a-z0-9-]{0,62})\.([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")
RESULTS_PREFIX = "results"


class ObjectNotFoundError(Exception):
    """The project has no object with that identifier."""


class ProjectObjects(Protocol):
    """Presigned access to the objects of the projects."""

    def input_url(self, project: str, object_id: str, expires: int) -> str:
        """Return a URL that reads one existing object, or raise :class:`ObjectNotFoundError`."""

    def output_url(self, project: str, operation_id: str, name: str, expires: int) -> str:
        """Return a URL that writes one result object of an operation."""

    def output_location(self, project: str, operation_id: str) -> str:
        """Describe where the results of an operation are written."""


@dataclass(frozen=True)
class ProjectBucket:
    """A project's bucket and the job service's account for it."""

    bucket: str
    credentials: Credentials


class StoreObjects:
    """Presigned URLs from an S3-compatible store over HTTPS, in path style."""

    def __init__(
        self,
        endpoint: str,
        region: str,
        buckets: dict[str, ProjectBucket],
        client: httpx.Client,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Sign for ``endpoint`` with each project's account; ``client`` checks inputs exist."""
        # A presigned URL is a credential for its object, so it never travels in plain HTTP.
        if not endpoint.startswith("https://"):
            raise ValueError("the object store must be reached over HTTPS")
        self._endpoint = endpoint
        self._region = region
        self._buckets = buckets
        self._client = client
        self._clock = clock

    def _url(self, method: str, project: str, key: str, expires: int) -> str:
        bucket = self._buckets[project]
        return presign(
            method=method,
            endpoint=self._endpoint,
            path=f"/{bucket.bucket}/{key}",
            credentials=bucket.credentials,
            region=self._region,
            expires=expires,
            now=self._clock(),
        )

    def input_url(self, project: str, object_id: str, expires: int) -> str:
        """Return a URL that reads one existing object of the project."""
        match = DRS_ID.fullmatch(object_id)
        if match is None or project not in self._buckets:
            raise ObjectNotFoundError
        key = f"{match.group(1)}/{match.group(2)}"
        # The check uses its own short-lived URL, so the task's URL is never sent here.
        response = self._client.head(self._url("HEAD", project, key, 60))
        if response.status_code in {httpx.codes.NOT_FOUND, httpx.codes.FORBIDDEN}:
            raise ObjectNotFoundError
        response.raise_for_status()
        return self._url("GET", project, key, expires)

    def output_url(self, project: str, operation_id: str, name: str, expires: int) -> str:
        """Return a URL that writes ``results/<operation>/<name>`` in the project's bucket."""
        return self._url("PUT", project, f"{RESULTS_PREFIX}/{operation_id}/{name}", expires)

    def output_location(self, project: str, operation_id: str) -> str:
        """Describe where the results of an operation are written."""
        return f"s3://{self._buckets[project].bucket}/{RESULTS_PREFIX}/{operation_id}/"
