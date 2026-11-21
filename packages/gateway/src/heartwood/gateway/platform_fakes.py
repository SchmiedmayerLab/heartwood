# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deterministic in-process fakes of platform inference, data, job, budget, and identity services.

The fakes follow the platform contract's draft profile so pull request checks can run
the conformance kit and failure-state tests without a cluster, credentials, or outside
network access. They serve synthetic data on loopback only. The data service is shaped
after GA4GH DRS object and access operations and the job service after GA4GH TES task
operations; neither is a reference implementation of those standards or of a platform.
"""

from __future__ import annotations

import hashlib
import json
import socket
import time
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Lock, Thread
from typing import Any, Literal, cast

import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from fastmcp.server.dependencies import get_access_token
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from heartwood.gateway.platform_conformance import InferenceEndpoint, McpEndpoint
from heartwood.schemas import (
    BudgetObservation,
    CostDenomination,
    ElementEstimate,
    EstimatedCost,
    FocusEvidenceReference,
    OperationResourceLimit,
    PlatformEstimate,
    PlatformLaunchContext,
    PlatformMcpServiceProfile,
    PlatformOperationCapabilities,
    PlatformOperationCapability,
    PlatformOperationObservation,
    PlatformOperationProposal,
    PlatformOptionalCapability,
    WorkloadElement,
    WorkloadPrediction,
)

ISSUER = "https://identity.synthetic.invalid"
DATA_AUDIENCE = "heartwood-data"
JOBS_AUDIENCE = "heartwood-jobs"
BUDGETS_AUDIENCE = "heartwood-control"
PROJECT_ID = "research-project-a"
OTHER_PROJECT_ID = "research-project-b"
INFERENCE_API_KEY = "synthetic-inference-key"
MCP_PROTOCOL_VERSION = "2025-11-25"
# The scope that names a service at the identity provider: svc-data for heartwood-data.
_SERVICE_PREFIX = "heartwood-"
# A project's client at the identity provider; services derive the project from it.
_PROJECT_CLIENT_PREFIX = "hw-proj-"
_EPOCH = datetime(2026, 7, 1, tzinfo=UTC)

type InferenceFailure = Literal[
    "authentication", "quota", "rate-limit", "model-denied", "unavailable"
]
type TesState = Literal[
    "QUEUED", "RUNNING", "COMPLETE", "EXECUTOR_ERROR", "SYSTEM_ERROR", "CANCELED"
]

_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
# Submission changes platform state; a repeated operation identifier returns the same task.
_SUBMIT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
# Cancellation can end running work, and repeating it observes the same outcome.
_CANCEL = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
_TES_STATUS: dict[TesState, Literal["queued", "running", "completed", "failed", "cancelled"]] = {
    "QUEUED": "queued",
    "RUNNING": "running",
    "COMPLETE": "completed",
    "EXECUTOR_ERROR": "failed",
    "SYSTEM_ERROR": "failed",
    "CANCELED": "cancelled",
}
# OpenAI-compatible error responses; messages are fixed synthetic text.
_INFERENCE_ERRORS: dict[InferenceFailure, tuple[int, str, str]] = {
    "authentication": (401, "invalid_api_key", "Invalid API key for the synthetic route."),
    "quota": (429, "insufficient_quota", "The project budget has been exceeded."),
    "rate-limit": (429, "rate_limit_exceeded", "Rate limit reached; retry after 1 second."),
    "model-denied": (404, "model_not_found", "Model not found for this identity."),
    "unavailable": (503, "service_unavailable", "Service temporarily unavailable."),
}


def _scope(audience: str) -> str:
    return f"svc-{audience.removeprefix(_SERVICE_PREFIX)}"


class FakeTokenIssuer:
    """Issue exchanged tokens for one audience, project client and researcher with one key.

    Each service fake verifies the issuer, audience, signature and expiry before a tool
    runs, and derives the project from the authorized party, ``hw-proj-<project>``, to
    check it against the requested project in the tool.
    """

    def __init__(self, issuer: str = ISSUER) -> None:
        """Generate a new signing key for tokens of one issuer."""
        self._keys = RSAKeyPair.generate()
        self.issuer = issuer

    def exchange(
        self,
        *,
        audience: str,
        project_id: str = PROJECT_ID,
        subject: str = "researcher",
        lifetime_seconds: int = 300,
    ) -> str:
        """Return a token for one service audience and project; negative lifetimes expire."""
        return self._keys.create_token(
            subject=subject,
            issuer=self.issuer,
            audience=audience,
            scopes=[_scope(audience)],
            expires_in_seconds=lifetime_seconds,
            additional_claims={"azp": f"{_PROJECT_CLIENT_PREFIX}{project_id}"},
        )

    def verifier(self, audience: str) -> JWTVerifier:
        """Return the verifier a service with this audience uses."""
        return JWTVerifier(
            public_key=self._keys.public_key,
            algorithm="RS256",
            issuer=self.issuer,
            audience=audience,
            required_scopes=[_scope(audience)],
        )


class _Availability:
    """Answer 503 while a service is down, or drop the reply of a request that set the flag.

    The flag is shared, so a test that loses a reply sends its requests one at a time.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.available = True
        self.drop_reply = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not self.available:
            await JSONResponse({"error": "service unavailable"}, status_code=503)(
                scope, receive, send
            )
            return
        dropped = False

        async def sending(message: Message) -> None:
            nonlocal dropped
            if message["type"] == "http.response.start" and self.drop_reply:
                self.drop_reply = False
                dropped = True
                await send({"type": "http.response.start", "status": 503, "headers": []})
                await send({"type": "http.response.body", "body": b""})
            elif not dropped:
                await send(message)

        await self.app(scope, receive, sending)


class _Service:
    """Shared authentication and availability for one FastMCP service fake."""

    service_id: str
    audience: str

    def __init__(self, issuer: FakeTokenIssuer, name: str) -> None:
        self.server = FastMCP(name, auth=issuer.verifier(self.audience), mask_error_details=True)
        self._lock = Lock()
        self._gate = _Availability(
            self.server.http_app(path="/mcp", json_response=True, stateless_http=True)
        )

    @property
    def app(self) -> ASGIApp:
        """Return the Streamable HTTP application served at ``/mcp``."""
        return self._gate

    @property
    def available(self) -> bool:
        """Return whether the service answers requests."""
        return self._gate.available

    @available.setter
    def available(self, value: bool) -> None:
        self._gate.available = value

    def profile(self, url: str) -> PlatformMcpServiceProfile:
        """Return the deployment profile for this service at ``url``."""
        return PlatformMcpServiceProfile.model_validate_json(
            json.dumps(
                {
                    "service_id": self.service_id,
                    "url": url,
                    "credential_ref": f"{self.service_id}-token",
                    "authority_ref": "synthetic-platform",
                    "policy_ref": "synthetic-policy-v1",
                    "supported_protocol_versions": [MCP_PROTOCOL_VERSION],
                }
            )
        )

    @staticmethod
    def _authorize(project_id: str) -> None:
        token = get_access_token()
        if token is None or token.claims.get("azp") != f"{_PROJECT_CLIENT_PREFIX}{project_id}":
            raise ToolError("project authorization denied")


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DrsChecksum(_Wire):
    """A DRS checksum entry."""

    type: Literal["sha-256"] = "sha-256"
    checksum: str


class DrsAccessMethod(_Wire):
    """A DRS access method; the URL is resolved separately with the access identifier."""

    type: Literal["https"] = "https"
    access_id: str


class DrsObject(_Wire):
    """The DRS object fields the catalog returns."""

    id: str
    self_uri: str
    name: str
    size: int
    created_time: datetime
    checksums: tuple[DrsChecksum, ...]
    access_methods: tuple[DrsAccessMethod, ...]


class DrsObjectList(_Wire):
    """Objects selected for one project."""

    objects: tuple[DrsObject, ...]


class DrsAccessUrl(_Wire):
    """A short-lived URL for one object."""

    url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SyntheticObject:
    """One synthetic data object and the project selection and access grant it holds."""

    object_id: str
    project_id: str
    name: str
    content: bytes
    selected: bool = True
    grant_expires_at: datetime = datetime(2026, 7, 2, tzinfo=UTC)


SYNTHETIC_OBJECTS = (
    SyntheticObject("obj-cohort", PROJECT_ID, "synthetic-cohort.csv", b"person_id\n1\n2\n"),
    SyntheticObject(
        "obj-unselected", PROJECT_ID, "synthetic-notes.csv", b"note\nexample\n", selected=False
    ),
    SyntheticObject(
        "obj-expired",
        PROJECT_ID,
        "synthetic-visits.csv",
        b"visit\n1\n",
        grant_expires_at=datetime(2026, 6, 30, tzinfo=UTC),
    ),
    SyntheticObject("obj-other", OTHER_PROJECT_ID, "synthetic-labs.csv", b"lab\n1\n"),
)


class FakeDataService(_Service):
    """Read-only catalog that lists a project's selected objects and resolves them like DRS."""

    service_id = "synthetic-data"
    audience = DATA_AUDIENCE

    def __init__(
        self,
        issuer: FakeTokenIssuer,
        objects: Iterable[SyntheticObject] = SYNTHETIC_OBJECTS,
        clock: Callable[[], datetime] = lambda: _EPOCH,
    ) -> None:
        """Serve ``objects``, judging access grants by ``clock``."""
        super().__init__(issuer, "Synthetic data catalog")
        self.objects = {item.object_id: item for item in objects}
        self.clock = clock
        self.server.tool(self.list_objects, annotations=_READ_ONLY)
        self.server.tool(self.get_object, annotations=_READ_ONLY)
        self.server.tool(self.get_access_url, annotations=_READ_ONLY)

    def list_objects(self, project_id: str) -> DrsObjectList:
        """List the objects selected for the project."""
        self._authorize(project_id)
        return DrsObjectList(
            objects=tuple(
                self._drs(item)
                for item in self.objects.values()
                if item.project_id == project_id and item.selected
            )
        )

    def get_object(self, project_id: str, object_id: str) -> DrsObject:
        """Return one selected object's DRS metadata."""
        self._authorize(project_id)
        return self._drs(self._selected(project_id, object_id))

    def get_access_url(self, project_id: str, object_id: str, access_id: str) -> DrsAccessUrl:
        """Resolve a short-lived URL for a selected object while its access grant is valid."""
        self._authorize(project_id)
        item = self._selected(project_id, object_id)
        if access_id != f"https-{item.object_id}":
            raise ToolError("access method not found")
        if self.clock() >= item.grant_expires_at:
            raise ToolError("access grant expired")
        return DrsAccessUrl(
            url=f"https://storage.synthetic.invalid/{item.project_id}/{item.name}",
            expires_at=item.grant_expires_at,
        )

    def _selected(self, project_id: str, object_id: str) -> SyntheticObject:
        item = self.objects.get(object_id)
        # Another project's object is indistinguishable from a missing one.
        if item is None or item.project_id != project_id:
            raise ToolError("object not found")
        if not item.selected:
            raise ToolError("object is not selected for this project")
        return item

    def _drs(self, item: SyntheticObject) -> DrsObject:
        return DrsObject(
            id=item.object_id,
            self_uri=f"drs://data.synthetic.invalid/{item.object_id}",
            name=item.name,
            size=len(item.content),
            created_time=_EPOCH,
            checksums=(DrsChecksum(checksum=hashlib.sha256(item.content).hexdigest()),),
            access_methods=(DrsAccessMethod(access_id=f"https-{item.object_id}"),),
        )


class JobResources(_Wire):
    """TES task resources, with GPU type and count for the platform's GPU node pool.

    ``max_runtime_seconds`` stands in for the platform's run time limit, which TES leaves
    to the implementation.
    """

    cpu_cores: int = 1
    ram_gb: float = 1.0
    gpu_type: str | None = None
    gpu_count: int = 0
    max_runtime_seconds: int = 3600


@dataclass(slots=True)
class _Task:
    task_id: str | None
    request_sha256: str
    state: TesState | Literal["DENIED", "NOT_RECEIVED", "CANCEL"]
    reason: str | None = None
    # The operation a cancellation names.
    target: str | None = None


@dataclass(slots=True)
class _Tasks:
    tasks: dict[tuple[str, str], _Task] = field(default_factory=dict)
    submitted: int = 0


class FakeJobService(_Service):
    """Job submission, status and cancellation shaped after TES, keyed by operation identifier.

    A repeated operation identifier with the same request returns the existing task, and a
    different request under the same identifier is a conflict. Status for an identifier the
    service never received reports it as failed and refuses that identifier afterwards.
    Cancellation is its own operation that names an earlier one. GPU requests are denied
    for projects without a GPU entitlement, and submissions are denied while a project's
    budget is exhausted. Tests move tasks between states with ``advance``.
    """

    service_id = "synthetic-jobs"
    audience = JOBS_AUDIENCE

    def __init__(
        self,
        issuer: FakeTokenIssuer,
        gpu_projects: frozenset[str] = frozenset(),
        clock: Callable[[], datetime] = lambda: _EPOCH,
        contexts: dict[str, str] | None = None,
    ) -> None:
        """Accept GPU requests only for ``gpu_projects``, observing time with ``clock``.

        ``contexts`` maps a platform project to the project context its records carry; a
        project without an entry uses its own identifier.
        """
        super().__init__(issuer, "Synthetic job service")
        self.gpu_projects = gpu_projects
        self.clock = clock
        self.contexts = dict(contexts or {})
        # Projects whose budget admits no more work, as other work or an administrator left it.
        self.exhausted_projects: set[str] = set()
        # Projects whose access the platform revoked; every call for them is refused.
        self.revoked_projects: set[str] = set()
        self._state = _Tasks()
        self._drop_after_submit = False
        self._delay_after_submit = 0.0
        # How often the service was asked to describe a submission.
        self.proposal_count = 0
        # Seconds the next proposal answer is held, to stand in for a slow service.
        self.proposal_delay = 0.0
        self.server.tool(self.create_task, annotations=_SUBMIT)
        self.server.tool(self.get_task, annotations=_READ_ONLY)
        self.server.tool(self.cancel_task, annotations=_CANCEL)
        self.server.tool(self.describe_operations, annotations=_READ_ONLY)
        self.server.tool(self.propose_task, annotations=_READ_ONLY)

    def drop_next_submission_reply(self) -> None:
        """Lose the reply of the next submission after the task was recorded."""
        self._drop_after_submit = True

    def delay_next_submission_reply(self, seconds: float) -> None:
        """Hold the reply of the next submission after the task was recorded."""
        self._delay_after_submit = seconds

    @property
    def task_count(self) -> int:
        """Return how many tasks the service started."""
        return self._state.submitted

    def describe_operations(self, project_id: str) -> PlatformOperationCapabilities:
        """Declare the operations, their permissions and the optional capabilities."""
        self._admit(project_id)
        gpu = project_id in self.gpu_projects
        return PlatformOperationCapabilities(
            service_id=self.service_id,
            operations=(
                PlatformOperationCapability(
                    tool="create_task",
                    operation="submit",
                    required_permission="jobs:submit",
                    permitted=True,
                ),
                PlatformOperationCapability(
                    tool="get_task",
                    operation="status",
                    required_permission="jobs:read",
                    permitted=True,
                ),
                PlatformOperationCapability(
                    tool="cancel_task",
                    operation="cancel",
                    required_permission="jobs:cancel",
                    permitted=True,
                ),
                PlatformOperationCapability(
                    tool="propose_task",
                    operation="proposal",
                    required_permission="jobs:read",
                    permitted=True,
                ),
            ),
            optional_capabilities=(
                PlatformOptionalCapability(
                    capability="gpu-resources",
                    status="available" if gpu else "not-permitted",
                    reason=None if gpu else "The project holds no GPU entitlement.",
                ),
            ),
        )

    def propose_task(
        self, project_id: str, tool: str, arguments: dict[str, Any]
    ) -> PlatformOperationProposal:
        """Describe what ``create_task`` would run for these arguments; nothing is submitted."""
        self._admit(project_id)
        self.proposal_count += 1
        delay, self.proposal_delay = self.proposal_delay, 0.0
        time.sleep(delay)
        if tool != "create_task":
            raise ToolError("no proposal for this tool")
        try:
            name, command = str(arguments["name"]), [str(item) for item in arguments["command"]]
            resources = JobResources.model_validate(arguments.get("resources") or {})
            inputs = [str(item) for item in arguments.get("inputs") or []]
        except (KeyError, TypeError, ValueError):
            raise ToolError("invalid task arguments") from None
        limits = [
            OperationResourceLimit(metric="cpu", unit="core", maximum=str(resources.cpu_cores)),
            OperationResourceLimit(metric="memory", unit="GB", maximum=f"{resources.ram_gb:g}"),
            OperationResourceLimit(
                metric="duration", unit="s", maximum=str(resources.max_runtime_seconds)
            ),
        ]
        limitations = ["Admission checks the project's budget and policy at submission."]
        if resources.gpu_count:
            limits.append(
                OperationResourceLimit(
                    metric="gpu",
                    unit=resources.gpu_type or "any",
                    maximum=str(resources.gpu_count),
                )
            )
            if project_id not in self.gpu_projects:
                limitations.append("The project holds no GPU entitlement, so admission denies it.")
        return PlatformOperationProposal(
            service_id=self.service_id,
            tool=tool,
            description=f"Run task {name}: {' '.join(command)}"[:512],
            runtime_profile_ref=(
                "synthetic-batch-gpu" if resources.gpu_count else "synthetic-batch-small"
            ),
            resource_limits=tuple(limits),
            data_scope=(
                "Project objects " + ", ".join(f"drs://data.synthetic.invalid/{i}" for i in inputs)
                if inputs
                else "No project data"
            ),
            destination=f"Project results prefix results/{project_id}/{name}/",
            limitations=tuple(limitations),
        )

    def create_task(
        self,
        project_id: str,
        operation_id: str,
        name: str,
        command: list[str],
        resources: JobResources,
        inputs: list[str] | None = None,
    ) -> PlatformOperationObservation:
        """Submit a task once per operation identifier and return its observed status."""
        self._admit(project_id)
        digest = hashlib.sha256(
            json.dumps(
                [name, command, resources.model_dump(mode="json"), inputs or []], sort_keys=True
            ).encode()
        ).hexdigest()
        with self._lock:
            key = (project_id, operation_id)
            task = self._state.tasks.get(key)
            if task is not None and task.state == "NOT_RECEIVED":
                raise ToolError("operation identifier was reported as not received")
            if task is not None and task.request_sha256 != digest:
                raise ToolError("operation identifier conflict")
            if task is None:
                if resources.gpu_count and project_id not in self.gpu_projects:
                    task = _Task(None, digest, "DENIED", "GPU request is outside the entitlement")
                elif project_id in self.exhausted_projects:
                    task = _Task(
                        None, digest, "DENIED", "Budget budget-compute has no availability"
                    )
                else:
                    self._state.submitted += 1
                    task = _Task(f"task-{self._state.submitted:04d}", digest, "QUEUED")
                self._state.tasks[key] = task
            if self._drop_after_submit:
                self._drop_after_submit = False
                self._gate.drop_reply = True
            delay, self._delay_after_submit = self._delay_after_submit, 0.0
            observation = self._observation(project_id, operation_id, task)
        time.sleep(delay)
        return observation

    def get_task(self, project_id: str, operation_id: str) -> PlatformOperationObservation:
        """Report an operation; an identifier never received is reported failed for good."""
        self._admit(project_id)
        with self._lock:
            key = (project_id, operation_id)
            task = self._state.tasks.get(key)
            if task is None:
                task = self._state.tasks[key] = _Task(None, "", "NOT_RECEIVED")
            return self._observation(project_id, operation_id, task)

    def cancel_task(
        self, project_id: str, operation_id: str, target_operation_id: str
    ) -> PlatformOperationObservation:
        """Cancel the named operation's queued or running work and report what was observed."""
        self._admit(project_id)
        with self._lock:
            key = (project_id, operation_id)
            existing = self._state.tasks.get(key)
            if existing is not None and existing.state == "NOT_RECEIVED":
                raise ToolError("operation identifier was reported as not received")
            if existing is not None and existing.target != target_operation_id:
                raise ToolError("operation identifier conflict")
            target = self._state.tasks.get((project_id, target_operation_id))
            if target is None or target.state in {"NOT_RECEIVED", "CANCEL"}:
                raise ToolError("operation not found")
            if existing is None:
                if target.state in {"QUEUED", "RUNNING"}:
                    target.state = "CANCELED"
                existing = self._state.tasks[key] = _Task(
                    None, target_operation_id, "CANCEL", target=target_operation_id
                )
            return self._observation(project_id, operation_id, existing)

    def advance(self, project_id: str, operation_id: str, state: TesState) -> None:
        """Move a task to the next backend state, as the executor would."""
        with self._lock:
            task = self._state.tasks.get((project_id, operation_id))
            if task is None or task.task_id is None:
                raise ValueError("a denied operation has no task")
            task.state = state

    def _admit(self, project_id: str) -> None:
        self._authorize(project_id)
        if project_id in self.revoked_projects:
            raise ToolError("project authorization revoked")

    def _observation(
        self, project_id: str, operation_id: str, task: _Task
    ) -> PlatformOperationObservation:
        reference, status, limitation = task.task_id or operation_id, "denied", task.reason
        if task.state == "NOT_RECEIVED":
            status, limitation = "failed", "The service never received this operation."
        elif task.state == "CANCEL":
            assert task.target is not None
            target = self._state.tasks[(project_id, task.target)]
            reference, status = target.task_id or task.target, "completed"
            limitation = f"Cancellation of {task.target} observed TES state {target.state}"
        elif task.state != "DENIED":
            status, limitation = _TES_STATUS[task.state], f"TES state {task.state}"
        return PlatformOperationObservation(
            project_context_id=self.contexts.get(project_id, project_id),
            operation_id=operation_id,
            service_id=self.service_id,
            backend_operation_ref=reference,
            observed_at=self.clock(),
            status=cast(Any, status),
            platform_record_ref=f"synthetic-audit:{operation_id}",
            limitations=() if limitation is None else (limitation,),
        )


SYNTHETIC_LAUNCH_CONTEXTS = tuple(
    PlatformLaunchContext.model_validate_json(
        json.dumps(
            {
                "project_context_id": context_id,
                "platform_id": "synthetic-platform",
                "project_id": project_id,
                "billing_attribution_ref": binding,
                "authority_ref": "synthetic-platform",
                "policy_ref": "synthetic-policy-v1",
                "issued_at": _EPOCH.isoformat(),
            }
        )
    )
    for context_id, project_id, binding in (
        ("launch-synthetic-1", PROJECT_ID, "binding-a"),
        ("launch-synthetic-2", OTHER_PROJECT_ID, "binding-b"),
    )
)


@dataclass(frozen=True, slots=True)
class SyntheticPrice:
    """One synthetic SKU price, named after the FOCUS SKU Price columns it stands in for.

    ``unit_price`` in USD applies to ``pricing_quantity`` of ``pricing_unit``, so a token
    count is converted to the million-token pricing quantity before it is priced.
    """

    sku_id: str
    runtime_profile_ref: str
    pricing_unit: Literal["second", "task-second", "input-token", "output-token"]
    unit_price: Decimal
    pricing_quantity: int = 1


SYNTHETIC_PRICES = (
    SyntheticPrice(
        "sku-exploration-small", "synthetic-exploration-small", "second", Decimal("0.0005")
    ),
    SyntheticPrice("sku-batch-small", "synthetic-batch-small", "task-second", Decimal("0.0003")),
    SyntheticPrice("sku-batch-gpu", "synthetic-batch-gpu", "task-second", Decimal("0.004")),
    SyntheticPrice("sku-coder-input", "synthetic-coder", "input-token", Decimal("0.50"), 1_000_000),
    SyntheticPrice(
        "sku-coder-output", "synthetic-coder", "output-token", Decimal("1.50"), 1_000_000
    ),
)


@dataclass(slots=True)
class SyntheticBudget:
    """One platform allocation and its accounting, held by the fake platform only."""

    budget_id: str
    project_id: str
    allocated: Decimal
    reserved: Decimal = Decimal(0)
    spent: Decimal = Decimal(0)
    version: int = 1


def _synthetic_budgets() -> tuple[SyntheticBudget, ...]:
    return (
        SyntheticBudget("budget-compute", PROJECT_ID, Decimal(50), spent=Decimal(10)),
        SyntheticBudget(
            "budget-inference", PROJECT_ID, Decimal(20), reserved=Decimal(5), spent=Decimal(2)
        ),
        SyntheticBudget("budget-compute-b", OTHER_PROJECT_ID, Decimal(5)),
    )


class BudgetList(_Wire):
    """Budget observations for one project."""

    budgets: tuple[BudgetObservation, ...]


_USD = CostDenomination(kind="currency", unit="USD")
_SKU_PRICE_REVISION = "005a30566a754db4e0f346d20c7063daba579080"


class FakeBudgetService(_Service):
    """Budget observations and estimates priced from a synthetic price sheet.

    The platform side of planning: it applies its own prices to a workload prediction,
    keeps allocations, reservations and spending, and derives each record's project
    context from the token's project, so a caller cannot substitute another binding.
    Estimating reserves nothing and authorizes nothing. Tests change allocations and
    spending, as other work and administrators would, and restrict estimated kinds.
    """

    service_id = "synthetic-budgets"
    audience = BUDGETS_AUDIENCE

    def __init__(
        self,
        issuer: FakeTokenIssuer,
        *,
        prices: Iterable[SyntheticPrice] = SYNTHETIC_PRICES,
        estimate_kinds: frozenset[str] = frozenset({"exploration", "batch", "inference"}),
        validity: timedelta = timedelta(hours=1),
        clock: Callable[[], datetime] = lambda: _EPOCH,
    ) -> None:
        """Price with ``prices``, estimate only ``estimate_kinds``, observing ``clock``."""
        super().__init__(issuer, "Synthetic budget service")
        self.prices = tuple(prices)
        self.estimate_kinds = estimate_kinds
        self.validity = validity
        self.clock = clock
        self.contexts = {item.project_id: item for item in SYNTHETIC_LAUNCH_CONTEXTS}
        self.budgets = {item.budget_id: item for item in _synthetic_budgets()}
        self._quotes = 0
        self.server.tool(self.get_budgets, annotations=_READ_ONLY)
        self.server.tool(self.estimate_workload, annotations=_READ_ONLY)

    def record_spending(self, budget_id: str, amount: str) -> None:
        """Record spending by other work on the same allocation."""
        with self._lock:
            budget = self.budgets[budget_id]
            budget.spent += Decimal(amount)
            budget.version += 1

    def set_allocation(self, budget_id: str, allocated: str) -> None:
        """Change an allocation, as a platform administrator would."""
        with self._lock:
            budget = self.budgets[budget_id]
            budget.allocated = Decimal(allocated)
            budget.version += 1

    def get_budgets(self, project_id: str) -> BudgetList:
        """Return the project's allocations, reservations, spending and availability."""
        self._authorize(project_id)
        context = self.contexts[project_id].project_context_id
        with self._lock:
            return BudgetList(
                budgets=tuple(
                    BudgetObservation(
                        project_context_id=context,
                        budget_id=item.budget_id,
                        observed_at=self.clock(),
                        denomination=_USD,
                        allocated=_amount(item.allocated),
                        reserved=_amount(item.reserved),
                        spent=_amount(item.spent),
                        available=_amount(item.allocated - item.reserved - item.spent),
                        constraints=(
                            "Reported availability does not reserve funds or admit work.",
                        ),
                        platform_record_ref=f"synthetic-budget:{item.budget_id}:{item.version}",
                    )
                    for item in self.budgets.values()
                    if item.project_id == project_id
                )
            )

    def estimate_workload(
        self,
        project_id: str,
        scope: Literal["plan", "elements"],
        prediction: dict[str, Any],
        element_ids: list[str] | None = None,
    ) -> PlatformEstimate:
        """Price a workload prediction for the whole plan or selected elements."""
        self._authorize(project_id)
        try:
            parsed = WorkloadPrediction.model_validate_json(json.dumps(prediction))
        except ValueError:
            raise ToolError("invalid workload prediction") from None
        if parsed.project_context_id != self.contexts[project_id].project_context_id:
            raise ToolError("prediction belongs to another project context")
        by_id = {item.element_id: item for item in parsed.elements}
        if scope == "plan" and element_ids is None:
            selected = tuple(by_id)
        elif scope == "elements" and element_ids and set(element_ids) <= set(by_id):
            selected = tuple(dict.fromkeys(element_ids))
        else:
            raise ToolError("invalid estimate scope")
        components = tuple(self._element_estimate(by_id[item]) for item in selected)
        total = _total(tuple(item.cost for item in components))
        created = self.clock()
        # Each quote is new, so a repeated request yields another estimate identity.
        with self._lock:
            self._quotes += 1
            quote = self._quotes
        request = json.dumps([project_id, scope, prediction, selected, quote], sort_keys=True)
        return PlatformEstimate(
            project_context_id=parsed.project_context_id,
            estimate_id=f"est-{hashlib.sha256(request.encode()).hexdigest()[:16]}",
            scope=scope,
            prediction=parsed.prediction,
            plan=parsed.plan,
            created_at=created,
            valid_until=created + self.validity,
            total=total,
            covered_element_ids=selected,
            element_estimates=components,
            pricing_assumptions=("Synthetic list prices; no discounts or commitments apply.",),
            focus_evidence=(
                FocusEvidenceReference(
                    specification_version="1.5-draft",
                    dataset="SKU Price",
                    reference="synthetic-price-sheet",
                    draft_revision=_SKU_PRICE_REVISION,
                    content_sha256=hashlib.sha256(repr(self.prices).encode()).hexdigest(),
                ),
            ),
        )

    def _element_estimate(self, element: WorkloadElement) -> ElementEstimate:
        cost = (
            self._price(element)
            if element.kind in self.estimate_kinds
            else f"The platform does not estimate {element.kind} work."
        )
        if isinstance(cost, str):
            cost = EstimatedCost(denomination=_USD, coverage="unknown", limitations=(cost,))
        return ElementEstimate(element_ids=(element.element_id,), cost=cost, included_in_total=True)

    def _price(self, element: WorkloadElement) -> EstimatedCost | str:
        prices = {
            item.pricing_unit: item.unit_price / item.pricing_quantity
            for item in self.prices
            if item.runtime_profile_ref == element.runtime_profile_ref
        }
        if not prices:
            return f"Runtime profile {element.runtime_profile_ref} has no price for this project."
        quantities = {item.metric: item for item in element.quantities}

        def priced(bound: Literal["expected", "lower", "upper"]) -> Decimal | None:
            def value(metric: str) -> Decimal | None:
                item = quantities.get(metric)
                raw = None if item is None else getattr(item, bound) or item.expected
                return None if raw is None else Decimal(raw)

            try:
                if element.kind == "exploration":
                    return _product(value("duration"), prices["second"])
                if element.kind == "batch":
                    tasks, retries = value("tasks"), value("retry_allowance") or Decimal(0)
                    return _product(
                        None if tasks is None else tasks + retries,
                        value("duration_per_task"),
                        prices["task-second"],
                    )
                inputs = _product(value("input_tokens"), prices["input-token"])
                outputs = _product(value("output_tokens"), prices["output-token"])
                return None if inputs is None or outputs is None else inputs + outputs
            except KeyError:
                return None

        expected = priced("expected")
        if expected is None:
            return "The prediction lacks a quantity this runtime profile is priced by."
        lower, upper = priced("lower"), priced("upper")
        return EstimatedCost(
            denomination=_USD,
            coverage="complete",
            expected=_amount(expected),
            lower=None if lower == expected else _amount(lower),
            upper=None if upper == expected else _amount(upper),
        )


def _product(*values: Decimal | None) -> Decimal | None:
    if any(value is None for value in values):
        return None
    result = Decimal(1)
    for value in values:
        result *= cast(Decimal, value)
    return result


def _amount(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value.quantize(Decimal("0.0001")).normalize(), "f")
    return "0" if Decimal(text) == 0 else text


def _total(costs: tuple[EstimatedCost, ...]) -> EstimatedCost:
    known = [item for item in costs if item.coverage != "unknown"]
    limitations = tuple(dict.fromkeys(text for item in costs for text in item.limitations))
    if not known:
        return EstimatedCost(denomination=_USD, coverage="unknown", limitations=limitations)

    def summed(bound: Literal["expected", "lower", "upper"]) -> str | None:
        values = [cast(str, getattr(item, bound) or item.expected) for item in known]
        return _amount(sum((Decimal(value) for value in values), Decimal(0)))

    expected, lower, upper = summed("expected"), summed("lower"), summed("upper")
    return EstimatedCost(
        denomination=_USD,
        coverage="complete" if len(known) == len(costs) else "partial",
        expected=expected,
        lower=None if lower == expected else lower,
        upper=None if upper == expected else upper,
        limitations=limitations,
    )


class FakeInference:
    """OpenAI-compatible model list and streaming Chat Completions with scripted failures."""

    def __init__(
        self,
        models: tuple[str, ...] = ("synthetic-coder",),
        reply: str = "Synthetic inference reply.",
        api_key: str = INFERENCE_API_KEY,
    ) -> None:
        """Offer ``models`` to callers that present ``api_key``, answering with ``reply``."""
        self.models = models
        self.reply = reply
        self.api_key = api_key
        # A scripted failure answers Chat Completions; the model list stays available.
        self.failure: InferenceFailure | None = None
        self.available = True
        self.app = Starlette(
            routes=[
                Route("/v1/models", self._models, methods=["GET"]),
                Route("/v1/chat/completions", self._chat, methods=["POST"]),
            ]
        )

    async def _models(self, request: Request) -> Response:
        if (refused := self._refusal(request)) is not None:
            return refused
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": model, "object": "model", "created": 0, "owned_by": "synthetic"}
                    for model in self.models
                ],
            }
        )

    async def _chat(self, request: Request) -> Response:
        if (refused := self._refusal(request)) is not None:
            return refused
        if self.failure is not None:
            return _inference_error(self.failure)
        body = await request.json()
        model = body.get("model")
        if model not in self.models:
            return _inference_error("model-denied")
        if body.get("stream") is True:
            return StreamingResponse(self._chunks(model), media_type="text/event-stream")
        return JSONResponse(
            {
                "id": "chatcmpl-synthetic",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    def _refusal(self, request: Request) -> Response | None:
        if not self.available:
            return _inference_error("unavailable")
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return _inference_error("authentication")
        return None

    async def _chunks(self, model: str) -> AsyncIterator[bytes]:
        for delta, finish in (({"role": "assistant", "content": self.reply}, None), ({}, "stop")):
            chunk = {
                "id": "chatcmpl-synthetic",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            yield f"data: {json.dumps(chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"


def _inference_error(failure: InferenceFailure) -> JSONResponse:
    status, code, message = _INFERENCE_ERRORS[failure]
    return JSONResponse(
        {"error": {"message": message, "type": "synthetic_error", "code": code}},
        status_code=status,
    )


@contextmanager
def serve(app: ASGIApp) -> Iterator[str]:
    """Serve an application on a loopback port in this process and yield its origin."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            if not server.started:
                raise RuntimeError("synthetic platform service did not start")
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(5)
        # Checked after a clean exit only, so an error raised in the body stays visible.
        if thread.is_alive():
            raise RuntimeError("synthetic platform service did not stop")


@dataclass(frozen=True, slots=True)
class RunningFakes:
    """Served fakes with their endpoints and deployment profiles."""

    issuer: FakeTokenIssuer
    inference: FakeInference
    data: FakeDataService
    jobs: FakeJobService
    budgets: FakeBudgetService
    inference_url: str
    data_profile: PlatformMcpServiceProfile
    jobs_profile: PlatformMcpServiceProfile
    budgets_profile: PlatformMcpServiceProfile

    def endpoints(self) -> tuple[InferenceEndpoint, tuple[McpEndpoint, ...]]:
        """Return the conformance kit's endpoints with valid exchanged tokens."""
        return InferenceEndpoint(self.inference_url, INFERENCE_API_KEY), (
            McpEndpoint(self.data_profile, self.issuer.exchange(audience=DATA_AUDIENCE)),
            McpEndpoint(self.jobs_profile, self.issuer.exchange(audience=JOBS_AUDIENCE)),
            McpEndpoint(self.budgets_profile, self.issuer.exchange(audience=BUDGETS_AUDIENCE)),
        )


@contextmanager
def running_platform_fakes(*, gpu_projects: frozenset[str] = frozenset()) -> Iterator[RunningFakes]:
    """Start the inference, data, job and budget fakes on loopback with one token issuer."""
    issuer = FakeTokenIssuer()
    inference = FakeInference()
    data = FakeDataService(issuer)
    jobs = FakeJobService(
        issuer,
        gpu_projects=gpu_projects,
        contexts={item.project_id: item.project_context_id for item in SYNTHETIC_LAUNCH_CONTEXTS},
    )
    budgets = FakeBudgetService(issuer)
    with (
        serve(inference.app) as inference_origin,
        serve(data.app) as data_origin,
        serve(jobs.app) as jobs_origin,
        serve(budgets.app) as budgets_origin,
    ):
        yield RunningFakes(
            issuer=issuer,
            inference=inference,
            data=data,
            jobs=jobs,
            budgets=budgets,
            inference_url=f"{inference_origin}/v1",
            data_profile=data.profile(f"{data_origin}/mcp"),
            jobs_profile=jobs.profile(f"{jobs_origin}/mcp"),
            budgets_profile=budgets.profile(f"{budgets_origin}/mcp"),
        )


__all__ = [
    "BUDGETS_AUDIENCE",
    "DATA_AUDIENCE",
    "INFERENCE_API_KEY",
    "JOBS_AUDIENCE",
    "OTHER_PROJECT_ID",
    "PROJECT_ID",
    "SYNTHETIC_LAUNCH_CONTEXTS",
    "SYNTHETIC_OBJECTS",
    "SYNTHETIC_PRICES",
    "BudgetList",
    "DrsObject",
    "FakeBudgetService",
    "FakeDataService",
    "FakeInference",
    "FakeJobService",
    "FakeTokenIssuer",
    "JobResources",
    "RunningFakes",
    "SyntheticBudget",
    "SyntheticObject",
    "SyntheticPrice",
    "running_platform_fakes",
    "serve",
]
