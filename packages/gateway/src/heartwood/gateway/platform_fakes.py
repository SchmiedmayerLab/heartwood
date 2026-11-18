# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deterministic in-process fakes of platform inference, data, job, and identity services.

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
from datetime import UTC, datetime
from threading import Lock, Thread
from typing import Literal

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
from heartwood.schemas import PlatformMcpServiceProfile, PlatformOperationObservation

ISSUER = "https://identity.synthetic.invalid"
DATA_AUDIENCE = "https://data.synthetic.invalid"
JOBS_AUDIENCE = "https://jobs.synthetic.invalid"
PROJECT_ID = "research-project-a"
OTHER_PROJECT_ID = "research-project-b"
INFERENCE_API_KEY = "synthetic-inference-key"
MCP_PROTOCOL_VERSION = "2025-11-25"
_SCOPE = "platform"
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


class FakeTokenIssuer:
    """Issue exchanged, audience-restricted, project-scoped tokens with one signing key.

    Each service fake verifies the issuer, audience, signature and expiry before a tool
    runs, and checks the project claim against the requested project in the tool.
    """

    def __init__(self) -> None:
        """Generate a new signing key."""
        self._keys = RSAKeyPair.generate()

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
            issuer=ISSUER,
            audience=audience,
            scopes=[_SCOPE],
            expires_in_seconds=lifetime_seconds,
            additional_claims={"project": project_id, "act": {"sub": "heartwood-gateway"}},
        )

    def verifier(self, audience: str) -> JWTVerifier:
        """Return the verifier a service with this audience uses."""
        return JWTVerifier(
            public_key=self._keys.public_key,
            algorithm="RS256",
            issuer=ISSUER,
            audience=audience,
            required_scopes=[_SCOPE],
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
        if token is None or token.claims.get("project") != project_id:
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
    """TES task resources, with GPU type and count for the platform's GPU node pool."""

    cpu_cores: int = 1
    ram_gb: float = 1.0
    gpu_type: str | None = None
    gpu_count: int = 0


@dataclass(slots=True)
class _Task:
    task_id: str | None
    request_sha256: str
    state: TesState | Literal["DENIED"]


@dataclass(slots=True)
class _Tasks:
    tasks: dict[tuple[str, str], _Task] = field(default_factory=dict)
    submitted: int = 0


class FakeJobService(_Service):
    """Job submission, status and cancellation shaped after TES, keyed by operation identifier.

    A repeated operation identifier with the same request returns the existing task, and a
    different request under the same identifier is a conflict. GPU requests are denied for
    projects without a GPU entitlement. Tests move tasks between states with ``advance``.
    """

    service_id = "synthetic-jobs"
    audience = JOBS_AUDIENCE

    def __init__(
        self,
        issuer: FakeTokenIssuer,
        gpu_projects: frozenset[str] = frozenset(),
        clock: Callable[[], datetime] = lambda: _EPOCH,
    ) -> None:
        """Accept GPU requests only for ``gpu_projects``, observing time with ``clock``."""
        super().__init__(issuer, "Synthetic job service")
        self.gpu_projects = gpu_projects
        self.clock = clock
        self._state = _Tasks()
        self._drop_after_submit = False
        self.server.tool(self.create_task, annotations=_SUBMIT)
        self.server.tool(self.get_task, annotations=_READ_ONLY)
        self.server.tool(self.cancel_task, annotations=_CANCEL)

    def drop_next_submission_reply(self) -> None:
        """Lose the reply of the next submission after the task was recorded."""
        self._drop_after_submit = True

    @property
    def task_count(self) -> int:
        """Return how many tasks the service started."""
        return self._state.submitted

    def create_task(
        self,
        project_id: str,
        operation_id: str,
        name: str,
        command: list[str],
        resources: JobResources,
    ) -> PlatformOperationObservation:
        """Submit a task once per operation identifier and return its observed status."""
        self._authorize(project_id)
        digest = hashlib.sha256(
            json.dumps([name, command, resources.model_dump(mode="json")], sort_keys=True).encode()
        ).hexdigest()
        with self._lock:
            key = (project_id, operation_id)
            task = self._state.tasks.get(key)
            if task is not None and task.request_sha256 != digest:
                raise ToolError("operation identifier conflict")
            if task is None:
                if resources.gpu_count and project_id not in self.gpu_projects:
                    task = _Task(None, digest, "DENIED")
                else:
                    self._state.submitted += 1
                    task = _Task(f"task-{self._state.submitted:04d}", digest, "QUEUED")
                self._state.tasks[key] = task
            if self._drop_after_submit:
                self._drop_after_submit = False
                self._gate.drop_reply = True
            return self._observation(project_id, operation_id, task)

    def get_task(self, project_id: str, operation_id: str) -> PlatformOperationObservation:
        """Return the observed status of an existing operation without submitting."""
        self._authorize(project_id)
        with self._lock:
            return self._observation(project_id, operation_id, self._task(project_id, operation_id))

    def cancel_task(self, project_id: str, operation_id: str) -> PlatformOperationObservation:
        """Cancel queued or running work and report the observed result."""
        self._authorize(project_id)
        with self._lock:
            task = self._task(project_id, operation_id)
            if task.state in {"QUEUED", "RUNNING"}:
                task.state = "CANCELED"
            return self._observation(project_id, operation_id, task)

    def advance(self, project_id: str, operation_id: str, state: TesState) -> None:
        """Move a task to the next backend state, as the executor would."""
        with self._lock:
            task = self._task(project_id, operation_id)
            if task.state == "DENIED":
                raise ValueError("a denied operation has no task")
            task.state = state

    def _task(self, project_id: str, operation_id: str) -> _Task:
        task = self._state.tasks.get((project_id, operation_id))
        if task is None:
            raise ToolError("operation not found")
        return task

    def _observation(
        self, project_id: str, operation_id: str, task: _Task
    ) -> PlatformOperationObservation:
        return PlatformOperationObservation(
            project_context_id=project_id,
            operation_id=operation_id,
            service_id=self.service_id,
            backend_operation_ref=task.task_id or operation_id,
            observed_at=self.clock(),
            status="denied" if task.state == "DENIED" else _TES_STATUS[task.state],
            platform_record_ref=f"synthetic-audit:{operation_id}",
            limitations=(
                ("GPU request is outside the project's entitlement",)
                if task.state == "DENIED"
                else (f"TES state {task.state}",)
            ),
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
    inference_url: str
    data_profile: PlatformMcpServiceProfile
    jobs_profile: PlatformMcpServiceProfile

    def endpoints(self) -> tuple[InferenceEndpoint, tuple[McpEndpoint, ...]]:
        """Return the conformance kit's endpoints with valid exchanged tokens."""
        return InferenceEndpoint(self.inference_url, INFERENCE_API_KEY), (
            McpEndpoint(self.data_profile, self.issuer.exchange(audience=DATA_AUDIENCE)),
            McpEndpoint(self.jobs_profile, self.issuer.exchange(audience=JOBS_AUDIENCE)),
        )


@contextmanager
def running_platform_fakes(*, gpu_projects: frozenset[str] = frozenset()) -> Iterator[RunningFakes]:
    """Start the inference, data and job fakes on loopback with one token issuer."""
    issuer = FakeTokenIssuer()
    inference = FakeInference()
    data = FakeDataService(issuer)
    jobs = FakeJobService(issuer, gpu_projects=gpu_projects)
    with (
        serve(inference.app) as inference_origin,
        serve(data.app) as data_origin,
        serve(jobs.app) as jobs_origin,
    ):
        yield RunningFakes(
            issuer=issuer,
            inference=inference,
            data=data,
            jobs=jobs,
            inference_url=f"{inference_origin}/v1",
            data_profile=data.profile(f"{data_origin}/mcp"),
            jobs_profile=jobs.profile(f"{jobs_origin}/mcp"),
        )


__all__ = [
    "DATA_AUDIENCE",
    "INFERENCE_API_KEY",
    "JOBS_AUDIENCE",
    "OTHER_PROJECT_ID",
    "PROJECT_ID",
    "SYNTHETIC_OBJECTS",
    "DrsObject",
    "FakeDataService",
    "FakeInference",
    "FakeJobService",
    "FakeTokenIssuer",
    "JobResources",
    "RunningFakes",
    "SyntheticObject",
    "running_platform_fakes",
    "serve",
]
