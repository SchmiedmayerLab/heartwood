# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The MCP interface of the job service.

A Heartwood gateway calls the tools with a researcher's token exchanged for this service.
The project comes from the token's authorized party; the control service's registry
decides membership and names the launch context that every observation carries. The
tools follow the operation profile that the gateway speaks: a capability tool, a proposal
tool, a submission tool, a status tool and a cancellation tool that names its target.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.http import StarletteWithLifespan
from mcp.types import ToolAnnotations
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from heartwood.schemas import (
    PlatformOperationCapabilities,
    PlatformOperationObservation,
    PlatformOperationProposal,
)
from heartwood_platform.common.tokens import AuthenticationError, ResearcherTokens
from heartwood_platform.jobs.platform import NoSessionError, NotAMemberError, ProjectContexts
from heartwood_platform.jobs.service import (
    Caller,
    InvalidRequestError,
    JobResources,
    JobService,
    OperationId,
    TaskInput,
    TaskOutput,
    TaskRequest,
)

MCP_PATH = "/mcp"
UNAVAILABLE = "a platform component is unavailable"
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


class _ExchangedTokens(TokenVerifier):
    """Accepts researcher tokens exchanged for this service; membership is checked per call."""

    def __init__(self, researchers: ResearcherTokens) -> None:
        super().__init__()
        self._researchers = researchers

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the researcher and project of a valid token."""
        try:
            researcher = await run_in_threadpool(self._researchers.verify, token)
        except (AuthenticationError, httpx.HTTPError):
            return None
        return AccessToken(
            token=token,
            client_id=f"hw-proj-{researcher.project}",
            scopes=[],
            claims={"sub": researcher.subject, "project": researcher.project},
        )


def create_jobs_app(
    service: JobService,
    researchers: ResearcherTokens,
    contexts: ProjectContexts,
) -> Starlette:
    """Build the HTTPS application: ``/healthz`` and the MCP tools at ``MCP_PATH``."""
    server = FastMCP(
        "Heartwood job service", auth=_ExchangedTokens(researchers), mask_error_details=True
    )

    def caller(project_id: str) -> Caller:
        token = get_access_token()
        if token is None:
            raise ToolError("authentication required")
        subject, project = token.claims["sub"], token.claims["project"]
        if project_id != project:
            raise ToolError("project_id is not the project of the token")
        try:
            context = contexts.context(subject, project)
        except NotAMemberError:
            raise ToolError("not a member of the project") from None
        except NoSessionError:
            raise ToolError("no session in the project") from None
        except httpx.HTTPError:
            raise ToolError(UNAVAILABLE) from None
        return Caller(subject, project, context)

    def handled[T](call: Callable[[], T]) -> T:
        try:
            return call()
        except InvalidRequestError as error:
            raise ToolError(str(error)) from None
        except httpx.HTTPError:
            raise ToolError(UNAVAILABLE) from None

    @server.tool(annotations=_READ_ONLY)
    def describe_operations(project_id: str) -> PlatformOperationCapabilities:
        """Declare the job operations, their permissions and the optional capabilities."""
        found = caller(project_id)
        return handled(lambda: service.describe(found))

    @server.tool(annotations=_READ_ONLY)
    def propose_task(
        project_id: str, tool: str, arguments: dict[str, Any]
    ) -> PlatformOperationProposal:
        """Describe what create_task would run for these arguments; nothing is submitted."""
        found = caller(project_id)
        return handled(lambda: service.propose(found, tool, arguments))

    @server.tool(annotations=_SUBMIT)
    def create_task(
        project_id: str,
        operation_id: OperationId,
        name: str,
        image: str,
        command: list[str],
        resources: JobResources | None = None,
        inputs: list[TaskInput] | None = None,
        outputs: list[TaskOutput] | None = None,
        tags: dict[str, str] | None = None,
    ) -> PlatformOperationObservation:
        """Run one container image on the platform's executor, once per operation identifier.

        Inputs are project objects by DRS identifier, read below /inputs; outputs are files
        below /outputs, written to the project's results. The platform checks the project's
        entitlements, limits and budget, and reports a refusal as denied.
        """
        found = caller(project_id)
        try:
            request = TaskRequest(
                name=name,
                image=image,
                command=tuple(command),
                resources=resources or JobResources(),
                inputs=tuple(inputs or ()),
                outputs=tuple(outputs or ()),
                tags=tags or {},
            )
        except ValidationError as error:
            raise ToolError(f"invalid task arguments: {error.errors()[0]['msg']}") from None
        return handled(lambda: service.submit(found, operation_id, request))

    @server.tool(annotations=_READ_ONLY)
    def get_task(project_id: str, operation_id: OperationId) -> PlatformOperationObservation:
        """Report an operation; an identifier never received is reported failed for good."""
        found = caller(project_id)
        return handled(lambda: service.status(found, operation_id))

    @server.tool(annotations=_CANCEL)
    def cancel_task(
        project_id: str, operation_id: OperationId, target_operation_id: OperationId
    ) -> PlatformOperationObservation:
        """Cancel the queued or running task of an earlier operation; report what was observed."""
        found = caller(project_id)
        return handled(lambda: service.cancel(found, operation_id, target_operation_id))

    mcp: StarletteWithLifespan = server.http_app(
        path=MCP_PATH, json_response=True, stateless_http=True
    )

    def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return Starlette(routes=[Route("/healthz", health), Mount("/", mcp)], lifespan=mcp.lifespan)
