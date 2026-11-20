# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Submission, status and cancellation of batch tasks, authorized by the platform.

Every operation is authorized here, independently of what Heartwood approved: the caller
acts in the project of its token, as a member with a session; the project's entitlements
and limits and its budget decide admission; and an operation identifier names one task.
The executor runs the tasks and is reached only through :class:`TesExecutor`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import posixpath
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from heartwood.schemas import (
    OperationResourceLimit,
    PlatformOperationCapabilities,
    PlatformOperationCapability,
    PlatformOperationObservation,
    PlatformOperationProposal,
    PlatformOptionalCapability,
)
from heartwood_platform.jobs.objects import DRS_ID, ObjectNotFoundError, ProjectObjects
from heartwood_platform.jobs.platform import BudgetDeniedError, JobBudgets, JobUsage
from heartwood_platform.jobs.records import ConflictError, Operation, Records
from heartwood_platform.jobs.tes import (
    PROGRESS,
    TERMINAL,
    TaskFilter,
    TaskNotFoundError,
    TesExecutor,
    TesExecutorSpec,
    TesInput,
    TesOutput,
    TesResources,
    TesState,
    TesTask,
    TesTaskView,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_ID = "heartwood-jobs"
# The tag that carries the operation identifier on each task the service creates.
OPERATION_TAG = "heartwood-operation"
# Seconds after which a submission whose creation reply was lost and whose task cannot be
# found counts as not accepted.
SUBMISSION_GRACE_SECONDS = 120
# Seconds that a presigned URL stays valid beyond the latest moment the task needs it.
URL_MARGIN_SECONDS = 300
# Pages of tasks read in one listing, of 100 tasks each.
MAX_PAGES = 100
# The longest description and data scope of a proposal.
MAX_DESCRIPTION = 8192
# The states of tasks that may still run.
ACTIVE: tuple[TesState, ...] = ("QUEUED", "INITIALIZING", "RUNNING", "PAUSED", "CANCELING")

OperationId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
_NAME = r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$"
_FILE = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
_TAG_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
IMAGE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")

type Status = Literal[
    "submitted", "queued", "running", "completed", "failed", "cancelled", "denied", "unknown"
]
_STATUS: dict[str, Status] = {
    "SUBMITTING": "submitted",
    "UNKNOWN": "unknown",
    "QUEUED": "queued",
    "INITIALIZING": "queued",
    "RUNNING": "running",
    "PAUSED": "running",
    "CANCELING": "running",
    "COMPLETE": "completed",
    "EXECUTOR_ERROR": "failed",
    "SYSTEM_ERROR": "failed",
    "PREEMPTED": "failed",
    "CANCELED": "cancelled",
    "DENIED": "denied",
}


class InvalidRequestError(Exception):
    """The request is malformed or names something the service does not know."""


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class JobResources(_Request):
    """The hard limits of a task. ``max_runtime_seconds`` bounds the time it may run."""

    cpu_cores: int = Field(default=1, ge=1, le=64)
    ram_gb: float = Field(default=1.0, gt=0, le=512)
    disk_gb: float = Field(default=1.0, gt=0, le=1024)
    gpu_type: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    gpu_count: int = Field(default=0, ge=0, le=8)
    max_runtime_seconds: int = Field(default=3600, ge=1, le=7 * 24 * 3600)


def _contained(value: str, root: str) -> str:
    if not value.startswith(f"{root}/") or posixpath.normpath(value) != value:
        raise ValueError(f"a path is a normalized absolute path below {root}/")
    return value


class TaskInput(_Request):
    """A project object, by DRS identifier, and the path where the task reads it."""

    object_id: str = Field(pattern=DRS_ID.pattern)
    path: str = Field(max_length=512)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _contained(value, "/inputs")


class TaskOutput(_Request):
    """A file the task writes, and the name of the result object it becomes."""

    name: str = Field(pattern=_FILE)
    path: str = Field(max_length=512)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _contained(value, "/outputs")


class TaskRequest(_Request):
    """The arguments of one submission, apart from project and operation identifier."""

    name: str = Field(pattern=_NAME)
    image: str = Field(max_length=512)
    command: tuple[Annotated[str, StringConstraints(max_length=4096)], ...] = Field(
        min_length=1, max_length=64
    )
    resources: JobResources = JobResources()
    inputs: tuple[TaskInput, ...] = Field(default=(), max_length=32)
    outputs: tuple[TaskOutput, ...] = Field(default=(), max_length=32)
    tags: dict[str, Annotated[str, StringConstraints(max_length=256)]] = Field(
        default_factory=dict, max_length=16
    )

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            # Funnel reads tags that start with an underscore as instructions.
            if key.startswith("_"):
                raise ValueError("a task tag must not start with an underscore")
            if key.startswith("heartwood") or _TAG_KEY.fullmatch(key) is None:
                raise ValueError(f"invalid task tag {key!r}")
        return value

    @model_validator(mode="after")
    def _unique(self) -> TaskRequest:
        paths = [item.path for item in self.inputs] + [item.path for item in self.outputs]
        names = [item.name for item in self.outputs]
        if len(set(paths)) != len(paths) or len(set(names)) != len(names):
            raise ValueError("input and output paths and output names must be unique")
        return self

    def digest(self) -> str:
        """Identify the request's content, so a repeated identifier can be compared."""
        document = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(document.encode()).hexdigest()


@dataclass(frozen=True)
class ProjectPolicy:
    """What a project may run: its entitlements and the largest limits of one task."""

    gpu: bool
    max_cpu_cores: int
    max_ram_gb: float
    max_disk_gb: float
    max_runtime_seconds: int


@dataclass(frozen=True)
class ServiceSettings:
    """Settings that hold for every project."""

    # Task images the platform runs, each by digest.
    images: frozenset[str]
    # Seconds a task may wait to start before the service cancels it.
    start_deadline_seconds: int
    # The runtime profiles of the price sheet for CPU and GPU tasks.
    cpu_profile: str
    gpu_profile: str
    # The host of the data service's DRS URIs, for the data scope of a proposal.
    drs_host: str


@dataclass(frozen=True)
class Caller:
    """A member acting in one project, with the launch context its records carry."""

    subject: str
    project: str
    project_context_id: str


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _outcome(view: TesTaskView) -> str | None:
    """Describe why a task ended without success, from fixed texts only.

    System logs carry presigned URLs and executor output carries task data, so neither is
    passed on.
    """
    attempts = view.logs or ()
    system = " ".join(line for attempt in attempts for line in attempt.system_logs)
    if view.state == "EXECUTOR_ERROR":
        codes = [item.exit_code for attempt in attempts for item in attempt.logs]
        code = next((value for value in reversed(codes) if value is not None), None)
        if "OOMKilled" in system:
            return "The task ran out of memory."
        if code == 137:
            return (
                "The task was killed with exit code 137 without an out-of-memory report, "
                "as when its pod is deleted or evicted."
            )
        return "The task failed." if code is None else f"The task exited with code {code}."
    if view.state == "SYSTEM_ERROR":
        if "download failed" in system:
            return "An input could not be downloaded."
        if "upload failed" in system or "resolve output" in system:
            return "A declared output was not written or could not be uploaded."
        if "ErrImagePull" in system or "ImagePullBackOff" in system:
            return "The task image could not be pulled."
        if "is missing" in system:
            return "The worker of the task was lost."
        return "The executor reported a system error."
    if view.state == "PREEMPTED":
        return "The task was preempted."
    return None


class JobService:
    """The operations of the job service over the executors of the projects."""

    def __init__(
        self,
        records: Records,
        executors: dict[str, TesExecutor],
        policies: dict[str, ProjectPolicy],
        objects: ProjectObjects,
        budgets: JobBudgets | None,
        settings: ServiceSettings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Run each project's tasks on its executor; ``budgets`` None reserves nothing."""
        if set(executors) != set(policies):
            raise ValueError("every project has one executor and one policy")
        self._records = records
        self._executors = executors
        self._policies = policies
        self._objects = objects
        self._budgets = budgets
        self._settings = settings
        self._clock = clock
        # One replica runs the service; each project's operations are serialized.
        self._locks = {project: threading.Lock() for project in executors}

    def projects(self) -> list[str]:
        """Return the projects that have an executor."""
        return sorted(self._executors)

    def _policy(self, caller: Caller) -> ProjectPolicy:
        policy = self._policies.get(caller.project)
        if policy is None:
            raise InvalidRequestError("the project has no job executor")
        return policy

    # Discovery and proposals.

    def describe(self, caller: Caller) -> PlatformOperationCapabilities:
        """Declare the operations, their permissions and the optional capabilities."""
        policy = self._policy(caller)

        def operation(
            tool: str, kind: Literal["submit", "status", "cancel", "proposal"], permission: str
        ) -> PlatformOperationCapability:
            return PlatformOperationCapability(
                tool=tool, operation=kind, required_permission=permission, permitted=True
            )

        return PlatformOperationCapabilities(
            service_id=SERVICE_ID,
            operations=(
                operation("create_task", "submit", "jobs:submit"),
                operation("get_task", "status", "jobs:read"),
                operation("cancel_task", "cancel", "jobs:cancel"),
                operation("propose_task", "proposal", "jobs:read"),
            ),
            optional_capabilities=(
                PlatformOptionalCapability(
                    capability="gpu-resources",
                    status="available" if policy.gpu else "not-permitted",
                    reason=None if policy.gpu else "The project holds no GPU entitlement.",
                ),
            ),
            limitations=self._limitations(),
        )

    def _limitations(self) -> tuple[str, ...]:
        notes = ["Admission checks the project's entitlements, limits and budget at submission."]
        if self._budgets is None:
            notes.append("This deployment reserves no budget for jobs.")
        return tuple(notes)

    def _refusal(self, policy: ProjectPolicy, request: TaskRequest) -> str | None:
        """Return why the platform refuses a request, or None when the policy admits it."""
        resources = request.resources
        if request.image not in self._settings.images or IMAGE.fullmatch(request.image) is None:
            return "The task image is not one the platform runs."
        if resources.gpu_count and not policy.gpu:
            return "The project holds no GPU entitlement."
        if resources.gpu_type is not None:
            return "The platform does not select GPU types."
        exceeded = [
            name
            for name, value, limit in (
                ("CPU cores", resources.cpu_cores, policy.max_cpu_cores),
                ("memory", resources.ram_gb, policy.max_ram_gb),
                ("disk", resources.disk_gb, policy.max_disk_gb),
                ("run time", resources.max_runtime_seconds, policy.max_runtime_seconds),
            )
            if value > limit
        ]
        if exceeded:
            return f"The request exceeds the project's limit for {', '.join(exceeded)}."
        return None

    def _usage(self, resources: JobResources, seconds: int) -> JobUsage:
        profile = self._settings.gpu_profile if resources.gpu_count else self._settings.cpu_profile
        return JobUsage(
            profile, resources.cpu_cores, resources.ram_gb, resources.gpu_count, seconds
        )

    def propose(
        self, caller: Caller, tool: str, arguments: dict[str, object]
    ) -> PlatformOperationProposal:
        """Describe what ``create_task`` would run for these arguments; nothing is submitted."""
        policy = self._policy(caller)
        if tool != "create_task":
            raise InvalidRequestError("no proposal for this tool")
        try:
            # create_task takes null for its optional arguments, so a proposal does too.
            request = TaskRequest.model_validate(
                {key: value for key, value in arguments.items() if value is not None}
            )
        except ValueError:
            raise InvalidRequestError("invalid task arguments") from None
        resources = request.resources
        limits = [
            OperationResourceLimit(metric="cpu", unit="core", maximum=str(resources.cpu_cores)),
            OperationResourceLimit(metric="memory", unit="GB", maximum=f"{resources.ram_gb:g}"),
            OperationResourceLimit(metric="disk", unit="GB", maximum=f"{resources.disk_gb:g}"),
            OperationResourceLimit(
                metric="duration", unit="s", maximum=str(resources.max_runtime_seconds)
            ),
        ]
        if resources.gpu_count:
            limits.append(
                OperationResourceLimit(
                    metric="gpu", unit=resources.gpu_type or "any", maximum=str(resources.gpu_count)
                )
            )
        limitations = list(self._limitations())
        refusal = self._refusal(policy, request)
        if refusal is not None:
            limitations.append(f"{refusal} Admission denies it.")
        scope = (
            "Project objects "
            + ", ".join(
                f"drs://{self._settings.drs_host}/{item.object_id}" for item in request.inputs
            )
            if request.inputs
            else "No project data"
        )
        description = f"Run task {request.name} in {request.image}: {' '.join(request.command)}"
        # The researcher approves this text, so it is never shortened.
        if len(description) > MAX_DESCRIPTION or len(scope) > MAX_DESCRIPTION:
            raise InvalidRequestError("the task is too long to describe for review")
        return PlatformOperationProposal(
            service_id=SERVICE_ID,
            tool=tool,
            description=description,
            runtime_profile_ref=self._usage(resources, 0).runtime_profile_ref,
            resource_limits=tuple(limits),
            data_scope=scope,
            destination=(
                f"Project results {self._objects.output_location(caller.project, '<operation>')}"
                if request.outputs
                else "No results are written"
            ),
            limitations=tuple(limitations),
        )

    # Submission.

    def submit(
        self, caller: Caller, operation_id: str, request: TaskRequest
    ) -> PlatformOperationObservation:
        """Submit a task once per operation identifier and return its observed status."""
        policy = self._policy(caller)
        digest = request.digest()
        with self._locks[caller.project]:
            existing = self._records.get(caller.project, operation_id)
            if existing is not None:
                if existing.kind == "absent":
                    raise InvalidRequestError("operation identifier was reported as not received")
                if (
                    existing.kind != "submit"
                    or existing.subject != caller.subject
                    or existing.request_sha256 != digest
                ):
                    raise InvalidRequestError("operation identifier conflict")
                return self._observe(caller, self._refresh(existing))
            now = self._clock()
            record = Operation(
                project=caller.project,
                operation_id=operation_id,
                kind="submit",
                subject=caller.subject,
                request_sha256=digest,
                state="SUBMITTING",
                task_id=None,
                target=None,
                detail=None,
                failure=None,
                cpu_cores=request.resources.cpu_cores,
                ram_gb=request.resources.ram_gb,
                gpu_count=request.resources.gpu_count,
                max_runtime_seconds=request.resources.max_runtime_seconds,
                reservation=None,
                settled=False,
                created_at=now,
                started_at=None,
                ended_at=None,
            )
            refusal = self._refusal(policy, request)
            task = self._task(caller, operation_id, request) if refusal is None else refusal
            if isinstance(task, str):
                return self._observe(caller, self._deny(record, task))
            # Recorded before the budget and the executor are called, so a reservation or
            # a task whose reply is lost always has a record: the reservation is settled by
            # its holder, and the task is found by its tag.
            self._records.insert(record)
            if self._budgets is not None:
                try:
                    reservation = self._budgets.reserve(
                        caller.project,
                        _holder(caller.project, operation_id),
                        self._usage(request.resources, request.resources.max_runtime_seconds),
                    )
                except BudgetDeniedError as error:
                    record = self._records.update(
                        record,
                        state="DENIED",
                        detail=str(error) or "The budget denied it.",
                        settled=True,
                    )
                    return self._observe(caller, record)
                except httpx.HTTPError:
                    record = self._records.update(
                        record,
                        state="SYSTEM_ERROR",
                        failure="The budget could not be reserved.",
                        ended_at=self._clock(),
                    )
                    return self._observe(caller, self._settle(record))
                record = self._records.update(record, reservation=reservation)
            try:
                task_id = self._executors[caller.project].create(task)
            except httpx.HTTPStatusError as error:
                if not error.response.is_client_error:
                    # The task may exist; the status finds it by its tag.
                    _LOGGER.warning("the executor of %s failed a submission", caller.project)
                    return self._observe(caller, record)
                _LOGGER.warning("the executor of %s refused a submission", caller.project)
                record = self._records.update(
                    record,
                    state="SYSTEM_ERROR",
                    failure="The executor did not accept the task.",
                    ended_at=self._clock(),
                )
                return self._observe(caller, self._settle(record))
            except (httpx.HTTPError, ValueError):
                # No reply, or a reply that names no task; the status finds it by its tag.
                _LOGGER.warning("the executor of %s did not answer a submission", caller.project)
                return self._observe(caller, record)
            record = self._records.update(record, state="QUEUED", task_id=task_id)
            return self._observe(caller, record)

    def _deny(self, record: Operation, reason: str) -> Operation:
        denied = Operation(
            **{**record.__dict__, "state": "DENIED", "detail": reason, "settled": True}
        )
        try:
            self._records.insert(denied)
        except ConflictError:
            raise InvalidRequestError("operation identifier conflict") from None
        return denied

    def _task(self, caller: Caller, operation_id: str, request: TaskRequest) -> TesTask | str:
        """Build the TES task, or return why an input cannot be used."""
        resources = request.resources
        start = self._settings.start_deadline_seconds + URL_MARGIN_SECONDS
        inputs = []
        for item in request.inputs:
            try:
                url = self._objects.input_url(caller.project, item.object_id, start)
            except ObjectNotFoundError:
                return f"The project has no object {item.object_id}."
            inputs.append(TesInput(url=url, path=item.path))
        outputs = [
            TesOutput(
                url=self._objects.output_url(
                    caller.project, operation_id, item.name, start + resources.max_runtime_seconds
                ),
                path=item.path,
            )
            for item in request.outputs
        ]
        parameters = {"gpu_count": str(resources.gpu_count)} if resources.gpu_count else {}
        return TesTask(
            name=request.name,
            inputs=tuple(inputs),
            outputs=tuple(outputs),
            resources=TesResources(
                cpu_cores=resources.cpu_cores,
                ram_gb=resources.ram_gb,
                disk_gb=resources.disk_gb,
                backend_parameters=parameters,
                backend_parameters_strict=bool(parameters),
            ),
            executors=(TesExecutorSpec(image=request.image, command=request.command),),
            tags={**request.tags, OPERATION_TAG: operation_id},
        )

    # Status.

    def status(self, caller: Caller, operation_id: str) -> PlatformOperationObservation:
        """Report an operation; an identifier never received is reported failed for good."""
        self._policy(caller)
        with self._locks[caller.project]:
            record = self._records.get(caller.project, operation_id)
            if record is None:
                record = Operation(
                    project=caller.project,
                    operation_id=operation_id,
                    kind="absent",
                    subject=caller.subject,
                    request_sha256="",
                    state="NOT_RECEIVED",
                    task_id=None,
                    target=None,
                    detail=None,
                    failure=None,
                    cpu_cores=0,
                    ram_gb=0,
                    gpu_count=0,
                    max_runtime_seconds=0,
                    reservation=None,
                    settled=True,
                    created_at=self._clock(),
                    started_at=None,
                    ended_at=None,
                )
                self._records.insert(record)
            elif record.subject != caller.subject:
                raise InvalidRequestError("operation not found")
            return self._observe(caller, self._refresh(record))

    # Cancellation.

    def cancel(
        self, caller: Caller, operation_id: str, target: str
    ) -> PlatformOperationObservation:
        """Cancel the named operation's queued or running task and report what was observed."""
        self._policy(caller)
        with self._locks[caller.project]:
            existing = self._records.get(caller.project, operation_id)
            if existing is not None:
                if existing.kind == "absent":
                    raise InvalidRequestError("operation identifier was reported as not received")
                if (
                    existing.kind != "cancel"
                    or existing.subject != caller.subject
                    or existing.target != target
                ):
                    raise InvalidRequestError("operation identifier conflict")
                return self._observe(caller, existing)
            submitted = self._records.get(caller.project, target)
            if (
                submitted is None
                or submitted.kind != "submit"
                or submitted.subject != caller.subject
            ):
                raise InvalidRequestError("operation not found")
            submitted = self._refresh(submitted)
            if submitted.state == "SUBMITTING":
                raise InvalidRequestError("the executor has not confirmed the operation yet")
            if submitted.state not in TERMINAL and submitted.task_id is not None:
                self._cancel(submitted)
                submitted = self._refresh(submitted)
            record = Operation(
                project=caller.project,
                operation_id=operation_id,
                kind="cancel",
                subject=caller.subject,
                request_sha256=hashlib.sha256(target.encode()).hexdigest(),
                state="DONE",
                task_id=None,
                target=target,
                detail=None,
                failure=None,
                cpu_cores=0,
                ram_gb=0,
                gpu_count=0,
                max_runtime_seconds=0,
                reservation=None,
                settled=True,
                created_at=self._clock(),
                started_at=None,
                ended_at=None,
            )
            self._records.insert(record)
            return self._observe(caller, record)

    # Reconciliation.

    def _cancel(self, record: Operation) -> None:
        """Cancel a record's task; a task the executor no longer has is observed as such."""
        assert record.task_id is not None
        try:
            self._executors[record.project].cancel(record.task_id)
        except TaskNotFoundError:
            pass
        except httpx.HTTPStatusError as error:
            # A task that ended meanwhile may be refused; observing it shows its state.
            if not error.response.is_client_error:
                raise

    def _pages(self, project: str, filters: TaskFilter) -> Iterator[TesTaskView]:
        executor = self._executors[project]
        token: str | None = None
        for _ in range(MAX_PAGES):
            page = executor.list(token, view="BASIC", filters=filters)
            yield from page.tasks
            token = page.next_page_token
            if not token:
                return
        _LOGGER.warning("the task list of %s has more than %d pages", project, MAX_PAGES)

    def _find(self, record: Operation) -> TesTaskView | None:
        """Return the task that carries a submission's operation tag."""
        filters = TaskFilter(tag_key=OPERATION_TAG, tag_value=record.operation_id)
        for view in self._pages(record.project, filters):
            if (
                (view.tags or {}).get(OPERATION_TAG) == record.operation_id
                and _adoptable(view, record)
                and self._records.by_task(record.project, view.id) is None
            ):
                return view
        return None

    def _refresh(self, record: Operation) -> Operation:
        """Bring a submission up to date with its task, enforcing deadlines and settling."""
        if record.kind != "submit" or record.state == "DENIED":
            return record
        now = self._clock()
        try:
            if record.state == "SUBMITTING":
                found = self._find(record)
                if found is not None:
                    record = self._records.update(record, state="QUEUED", task_id=found.id)
                elif now - record.created_at > timedelta(seconds=SUBMISSION_GRACE_SECONDS):
                    record = self._records.update(
                        record,
                        state="SYSTEM_ERROR",
                        failure="The executor did not accept the task.",
                        ended_at=now,
                    )
                    return self._settle(record)
                else:
                    return record
            if record.state not in TERMINAL:
                record = self._observe_task(record)
                record = self._enforce(record, now)
        except (httpx.TimeoutException, httpx.NetworkError):
            _LOGGER.warning("the executor of %s is unavailable", record.project)
            return record
        return self._settle(record)

    def _observe_task(self, record: Operation) -> Operation:
        assert record.task_id is not None
        try:
            view = self._executors[record.project].get(record.task_id, view="FULL")
        except TaskNotFoundError:
            return self._records.update(
                record,
                state="SYSTEM_ERROR",
                failure="The executor no longer has the task.",
                ended_at=self._clock(),
            )
        # A state never moves back, whatever the executor reports later.
        state = (
            view.state if PROGRESS[view.state] >= PROGRESS.get(record.state, 0) else record.state
        )
        attempts = view.logs or ()
        started = record.started_at or next(
            (time for attempt in attempts if (time := _time(attempt.start_time)) is not None), None
        )
        if started is None and state in {"RUNNING", "PAUSED", "CANCELING"}:
            started = self._clock()
        ended = record.ended_at
        if state in TERMINAL and ended is None:
            ended = next(
                (
                    time
                    for attempt in reversed(attempts)
                    if (time := _time(attempt.end_time)) is not None
                ),
                self._clock(),
            )
        changes: dict[str, object] = {}
        if state != record.state:
            changes["state"] = state
        if started != record.started_at:
            changes["started_at"] = started
        if ended != record.ended_at:
            changes["ended_at"] = ended
        if state in TERMINAL and record.detail is None and (outcome := _outcome(view)) is not None:
            changes["detail"] = outcome
        return self._records.update(record, **changes) if changes else record

    def _enforce(self, record: Operation, now: datetime) -> Operation:
        """Cancel a task that did not start in time or ran past its limit."""
        if record.state in TERMINAL or record.task_id is None:
            return record
        if record.failure is not None:
            # An earlier cancellation has not ended the task yet.
            self._cancel(record)
            record = self._observe_task(record)
            if record.state == "COMPLETE":
                return self._records.update(record, failure=None)
            return record
        failure = None
        if record.started_at is None:
            if now - record.created_at > timedelta(seconds=self._settings.start_deadline_seconds):
                deadline = self._settings.start_deadline_seconds
                failure = f"The task did not start within {deadline} seconds."
        elif now - record.started_at > timedelta(seconds=record.max_runtime_seconds):
            failure = (
                f"The task exceeded its run time limit of {record.max_runtime_seconds} seconds."
            )
        if failure is None:
            return record
        # Recorded first, so the reason survives an interruption after the cancellation.
        record = self._records.update(record, failure=failure)
        self._cancel(record)
        record = self._observe_task(record)
        if record.state == "COMPLETE":
            # The task ended before the cancellation reached it.
            return self._records.update(record, failure=None)
        return record

    def _settle(self, record: Operation) -> Operation:
        if record.settled or record.state not in TERMINAL:
            return record
        # A reservation is settled by its holder, also when the reply to it was lost.
        if self._budgets is not None and record.kind == "submit":
            seconds = 0
            if record.started_at is not None and record.ended_at is not None:
                seconds = max(0, math.ceil((record.ended_at - record.started_at).total_seconds()))
            resources = JobResources(
                cpu_cores=record.cpu_cores,
                ram_gb=record.ram_gb,
                gpu_count=record.gpu_count,
                max_runtime_seconds=max(1, record.max_runtime_seconds),
            )
            try:
                self._budgets.settle(
                    _holder(record.project, record.operation_id), self._usage(resources, seconds)
                )
            except httpx.HTTPError:
                _LOGGER.warning("the reservation of %s is not settled yet", record.operation_id)
                return record
        return self._records.update(record, settled=True)

    def reconcile(self, project: str) -> list[str]:
        """Cancel the project's tasks that no operation recorded and refresh open operations.

        A task created outside the job service, such as with the executor's credential, has
        no record and is cancelled. The listing runs without the project's lock, and each
        task and record is then handled under it, so calls of the project wait for one
        step at most. One task or record that fails is logged and the others are still
        reconciled. Return the identifiers of the cancelled tasks.
        """
        cancelled: list[str] = []
        executor = self._executors[project]
        views = [view for state in ACTIVE for view in self._pages(project, TaskFilter(state=state))]
        for view in views:
            try:
                with self._locks[project]:
                    if self._records.by_task(project, view.id) is not None:
                        continue
                    tagged = (view.tags or {}).get(OPERATION_TAG)
                    record = None if tagged is None else self._records.get(project, tagged)
                    if (
                        record is not None
                        and record.kind == "submit"
                        and record.state == "SUBMITTING"
                        and _adoptable(view, record)
                    ):
                        self._records.update(record, state="QUEUED", task_id=view.id)
                        continue
                    _LOGGER.warning(
                        "cancelling task %s of %s, which no operation recorded", view.id, project
                    )
                    executor.cancel(view.id)
                    cancelled.append(view.id)
            except Exception:
                _LOGGER.exception("reconciling task %s of %s failed", view.id, project)
        for listed in self._records.open_submissions(project):
            try:
                with self._locks[project]:
                    record = self._records.get(project, listed.operation_id)
                    if record is not None:
                        self._refresh(record)
            except Exception:
                _LOGGER.exception("refreshing %s of %s failed", listed.operation_id, project)
        return cancelled

    # Observations.

    def _observe(self, caller: Caller, record: Operation) -> PlatformOperationObservation:
        limitations: list[str] = []
        reference = record.task_id or record.operation_id
        if record.kind == "absent":
            status: Status = "failed"
            limitations.append("The service never received this operation.")
        elif record.kind == "cancel":
            assert record.target is not None
            target = self._records.get(record.project, record.target)
            assert target is not None
            target = self._refresh(target)
            status = "completed"
            reference = target.task_id or target.operation_id
            if target.failure is None and target.state not in {"DENIED", "SUBMITTING"}:
                observed = f"TES state {target.state}"
            else:
                observed = f"status {_STATUS.get(target.state, 'unknown')}"
                if target.failure is not None:
                    observed = "status failed"
            limitations.append(f"Cancellation of {record.target} observed {observed}.")
        else:
            status = _STATUS.get(record.state, "unknown")
            if record.failure is not None:
                status = "failed"
                limitations.append(record.failure)
            if record.detail is not None:
                limitations.append(record.detail)
            # Only a state that the executor reported is named as a TES state.
            if record.failure is None and record.state not in {"DENIED", "SUBMITTING"}:
                limitations.append(f"TES state {record.state}.")
            if record.state == "SUBMITTING":
                limitations.append("The executor has not confirmed the task yet.")
        return PlatformOperationObservation(
            project_context_id=caller.project_context_id,
            operation_id=record.operation_id,
            service_id=SERVICE_ID,
            backend_operation_ref=reference,
            observed_at=self._clock(),
            status=status,
            platform_record_ref=f"{SERVICE_ID}:{record.project}/{record.operation_id}",
            limitations=tuple(limitations),
        )


def _adoptable(view: TesTaskView, record: Operation) -> bool:
    """Whether a tagged task can be a submission's task: it was created after the record.

    A holder of the executor's credential could plant a task with the tag of another
    operation before that operation is submitted; such a task is not adopted.
    """
    created = _time(view.creation_time)
    return created is not None and created >= record.created_at - timedelta(seconds=60)


def _holder(project: str, operation_id: str) -> str:
    return f"job/{project}/{operation_id}"
