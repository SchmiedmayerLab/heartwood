# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The executor interface of the job service: a subset of the GA4GH TES 1.1 task API.

The job service calls an executor only through :class:`TesExecutor`, with five
operations, and sends only this subset: one executor per task, single-file inputs and
outputs by URL, resources, and tags. Any TES implementation that serves it can run the
platform's tasks; the deployed one is Funnel, reached over HTTPS with a CA file and a
Basic credential.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

type TesState = Literal[
    "UNKNOWN",
    "QUEUED",
    "INITIALIZING",
    "RUNNING",
    "PAUSED",
    "COMPLETE",
    "EXECUTOR_ERROR",
    "SYSTEM_ERROR",
    "CANCELED",
    "PREEMPTED",
    "CANCELING",
]
TERMINAL: frozenset[str] = frozenset(
    {"COMPLETE", "EXECUTOR_ERROR", "SYSTEM_ERROR", "CANCELED", "PREEMPTED"}
)
# The order in which a task moves; a later state never returns to an earlier one.
PROGRESS: dict[str, int] = {
    "UNKNOWN": 0,
    "QUEUED": 1,
    "INITIALIZING": 2,
    "RUNNING": 3,
    "PAUSED": 3,
    "CANCELING": 4,
    "COMPLETE": 5,
    "EXECUTOR_ERROR": 5,
    "SYSTEM_ERROR": 5,
    "CANCELED": 5,
    "PREEMPTED": 5,
}


class _Message(BaseModel):
    # Executors may add fields; the job service reads only the subset below.
    model_config = ConfigDict(extra="ignore", frozen=True)


class TesInput(_Message):
    """A file the executor downloads before the task runs."""

    url: str
    path: str
    type: Literal["FILE"] = "FILE"


class TesOutput(_Message):
    """A file the executor uploads after the task ran."""

    url: str
    path: str
    type: Literal["FILE"] = "FILE"


class TesResources(_Message):
    """The resources of a task."""

    cpu_cores: int
    ram_gb: float
    disk_gb: float | None = None
    backend_parameters: dict[str, str] = Field(default_factory=dict)
    backend_parameters_strict: bool = False


class TesExecutorSpec(_Message):
    """The one container of a task."""

    image: str
    command: tuple[str, ...]
    workdir: str | None = None


class TesTask(_Message):
    """A task to create."""

    name: str
    description: str | None = None
    inputs: tuple[TesInput, ...] = ()
    outputs: tuple[TesOutput, ...] = ()
    resources: TesResources
    executors: tuple[TesExecutorSpec, ...]
    tags: dict[str, str] = Field(default_factory=dict)


class TesExecutorLog(_Message):
    """What one executor reported."""

    start_time: str | None = None
    end_time: str | None = None
    exit_code: int | None = None


class TesTaskLog(_Message):
    """What one attempt of a task reported."""

    logs: tuple[TesExecutorLog, ...] = ()
    start_time: str | None = None
    end_time: str | None = None
    system_logs: tuple[str, ...] = ()


class TesTaskView(_Message):
    """A task as the executor reports it."""

    id: str
    state: TesState = "UNKNOWN"
    name: str | None = None
    tags: dict[str, str] | None = None
    creation_time: str | None = None
    logs: tuple[TesTaskLog, ...] | None = None


class TesServiceInfo(_Message):
    """The executor's service information."""

    id: str
    name: str


class TesTaskList(_Message):
    """One page of tasks."""

    tasks: tuple[TesTaskView, ...] = ()
    next_page_token: str | None = None


class TaskNotFoundError(Exception):
    """The executor has no task with that identifier."""


type View = Literal["MINIMAL", "BASIC", "FULL"]


class TaskFilter(_Message):
    """The TES 1.1 list filters the job service uses: one state, or one tag and its value."""

    state: TesState | None = None
    tag_key: str | None = None
    tag_value: str | None = None


class TesExecutor(Protocol):
    """The five TES operations the job service uses."""

    def service_info(self) -> TesServiceInfo:
        """Return the executor's service information."""

    def create(self, task: TesTask) -> str:
        """Create a task and return its identifier."""

    def get(self, task_id: str, view: View = "BASIC") -> TesTaskView:
        """Return a task, or raise :class:`TaskNotFoundError`."""

    def list(
        self, page_token: str | None = None, view: View = "BASIC", filters: TaskFilter | None = None
    ) -> TesTaskList:
        """Return one page of the tasks that match ``filters``."""

    def cancel(self, task_id: str) -> None:
        """Cancel a task; cancelling a finished task leaves its state."""


class HttpTesExecutor:
    """A TES 1.1 HTTP API, verified against one CA, with a Basic credential."""

    def __init__(
        self,
        url: str,
        ca_file: Path,
        user: str,
        password: str,
        *,
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Call the TES API at ``url``, the base before ``/v1``, such as ``https://host/ga4gh/tes``."""
        if not url.startswith("https://"):
            raise ValueError("the executor is reached over HTTPS only")
        self._client = httpx.Client(
            base_url=url.rstrip("/"),
            verify=ssl.create_default_context(cafile=ca_file),
            auth=(user, password),
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    def _checked(self, response: httpx.Response) -> httpx.Response:
        if response.status_code == httpx.codes.NOT_FOUND:
            raise TaskNotFoundError
        response.raise_for_status()
        return response

    def service_info(self) -> TesServiceInfo:
        """Return the executor's service information."""
        return TesServiceInfo.model_validate(
            self._checked(self._client.get("/v1/service-info")).json()
        )

    def create(self, task: TesTask) -> str:
        """Create a task and return its identifier."""
        body = task.model_dump(mode="json", exclude_none=True)
        answer = self._checked(self._client.post("/v1/tasks", json=body)).json()
        identifier = answer.get("id") if isinstance(answer, dict) else None
        if not isinstance(identifier, str) or not identifier:
            raise httpx.HTTPError("the executor returned no task identifier")
        return identifier

    @staticmethod
    def _path(task_id: str) -> str:
        # A task identifier is one path segment, whatever it contains.
        return f"/v1/tasks/{quote(task_id, safe='')}"

    def get(self, task_id: str, view: View = "BASIC") -> TesTaskView:
        """Return a task, or raise :class:`TaskNotFoundError`."""
        response = self._client.get(self._path(task_id), params={"view": view})
        return TesTaskView.model_validate(self._checked(response).json())

    def list(
        self, page_token: str | None = None, view: View = "BASIC", filters: TaskFilter | None = None
    ) -> TesTaskList:
        """Return one page of the tasks that match ``filters``."""
        parameters = {"view": view, "page_size": "100"}
        if page_token:
            parameters["page_token"] = page_token
        if filters is not None:
            parameters.update(filters.model_dump(exclude_none=True))
        response = self._client.get("/v1/tasks", params=parameters)
        return TesTaskList.model_validate(self._checked(response).json())

    def cancel(self, task_id: str) -> None:
        """Cancel a task; the colon of ``:cancel`` is sent encoded, as Funnel requires."""
        self._checked(self._client.post(f"{self._path(task_id)}%3Acancel"))
