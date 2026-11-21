# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The HTTP interface of the control service.

Every call is authorized here, independently of what Heartwood approved. Platform
components call with a service account token and name the researcher by identifier
and the project in the path.
A Heartwood gateway calls with a researcher's exchanged token, which names both, and
reaches the planning tools over MCP with the same token.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, StringConstraints, TypeAdapter, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from heartwood_platform.common.tokens import AuthenticationError, ResearcherTokens, ServiceTokens
from heartwood_platform.control.budgets import InsufficientBudgetError, Ledger
from heartwood_platform.control.launch import (
    LaunchService,
    NoClientError,
    NoSessionError,
    UnknownProjectError,
)
from heartwood_platform.control.planning import MCP_PATH, UNAVAILABLE, create_planning_app
from heartwood_platform.control.prices import PriceSheet
from heartwood_platform.control.registry import NotAMemberError
from heartwood_platform.control.researchers import RESEARCHER_ID_PATTERN
from heartwood_platform.control.sessions import SessionError

# Researcher identifiers and projects become part of redirect URIs and object names.
ResearcherId = Annotated[str, StringConstraints(pattern=RESEARCHER_ID_PATTERN)]
ProjectId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")]


_LOGGER = logging.getLogger(__name__)
_RESEARCHER = TypeAdapter(ResearcherId)
_PROJECT_ID = TypeAdapter(ProjectId)


class LaunchRequest(BaseModel):
    """The researcher and project of one launch."""

    model_config = ConfigDict(extra="forbid", strict=True)

    researcher: ResearcherId
    project: ProjectId


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("no bearer token")
    return token


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def create_app(
    launches: LaunchService,
    researchers: ResearcherTokens,
    services: ServiceTokens,
    prices: PriceSheet,
    ledger: Ledger,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    operators: ServiceTokens | None = None,
) -> Starlette:
    """Build the application around the launch service, the prices and the verifiers.

    ``operators`` accepts the platform operators' service accounts, which may rotate
    project client secrets; without it nobody may.
    """

    def handled(endpoint: Callable[[Request], Response]) -> Callable[[Request], Response]:
        def wrapper(request: Request) -> Response:
            try:
                return endpoint(request)
            except AuthenticationError:
                return _error(401, "authentication failed")
            except NotAMemberError:
                return _error(403, "not a member of the project")
            except NoSessionError:
                return _error(404, "no session")
            except UnknownProjectError:
                return _error(404, "unknown project")
            except NoClientError:
                return _error(409, "the project has no client yet")
            except InsufficientBudgetError:
                return _error(409, "the project's inference budget has nothing available")
            except ValidationError:
                return _error(422, "invalid request")
            except httpx.HTTPError:
                # Keycloak or the Kubernetes API did not answer as expected.
                return _error(502, UNAVAILABLE)
            except SessionError as error:
                return _error(503, str(error))

        return wrapper

    def launch_request(request: Request) -> LaunchRequest:
        return LaunchRequest.model_validate(
            {
                "researcher": request.path_params["researcher"],
                "project": request.path_params["project"],
            }
        )

    def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    def projects(request: Request) -> Response:
        services.verify(_bearer(request))
        researcher = _RESEARCHER.validate_python(request.path_params["researcher"])
        try:
            subject = launches.subject_of(researcher)
        except NotAMemberError:
            return JSONResponse({"projects": []})
        return JSONResponse({"projects": launches.projects(subject)})

    def prepare(request: Request) -> Response:
        services.verify(_bearer(request))
        launch = launch_request(request)
        prepared = launches.prepare(launches.subject_of(launch.researcher), launch.project)
        return JSONResponse(prepared.model_dump(mode="json"))

    def delete(request: Request) -> Response:
        services.verify(_bearer(request))
        launch = launch_request(request)
        try:
            subject = launches.subject_of(launch.researcher)
        except NotAMemberError:
            # An identifier that names nobody has no session, so there is nothing to remove.
            return Response(status_code=204)
        launches.delete(subject, launch.project)
        return Response(status_code=204)

    def rotate(request: Request) -> Response:
        if operators is None:
            raise AuthenticationError("no operator is configured")
        operator = operators.verify(_bearer(request))
        project = _PROJECT_ID.validate_python(request.path_params["project"])
        rotation = launches.rotate_client_secret(project)
        _LOGGER.info(
            "%s rotated the client secret of project %s: %d sessions rewritten, %d failed",
            operator,
            project,
            len(rotation.rewritten),
            len(rotation.failed),
        )
        return JSONResponse({"sessions": rotation.rewritten, "failed": rotation.failed})

    def launch_context(request: Request) -> Response:
        researcher = researchers.verify(_bearer(request))
        context = launches.context(researcher.subject, researcher.project)
        return JSONResponse(context.model_dump(mode="json"))

    planning = create_planning_app(launches, researchers, prices, ledger, clock)
    launch_path = "/v1/researchers/{researcher}/projects/{project}/launch"
    return Starlette(
        routes=[
            Route("/healthz", health),
            Route("/v1/researchers/{researcher}/projects", handled(projects)),
            Route(launch_path, handled(prepare), methods=["PUT"]),
            Route(launch_path, handled(delete), methods=["DELETE"]),
            Route("/v1/projects/{project}/client-secret", handled(rotate), methods=["POST"]),
            Route("/v1/launch-context", handled(launch_context)),
            Route(MCP_PATH, planning),
        ],
        lifespan=planning.lifespan,
    )
