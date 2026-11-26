# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The HTTP interface of the control service.

Every call is authorized here, independently of what Heartwood approved. Platform
components call with a service account token and name the researcher by identifier
and the project in the path. The job service reserves and settles the compute budget
of each batch task by its project and operation identifier.
A Heartwood gateway calls with a researcher's exchanged token, which names both, and
reaches the planning tools over MCP with the same token.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from heartwood_platform.common.tokens import AuthenticationError, ResearcherTokens, ServiceTokens
from heartwood_platform.control.budgets import (
    EndedHolderError,
    InsufficientBudgetError,
    Ledger,
    Reservation,
    budget_id,
)
from heartwood_platform.control.inference import InferenceSetupError
from heartwood_platform.control.launch import (
    LaunchService,
    NoClientError,
    NoSessionError,
    UnknownProjectError,
)
from heartwood_platform.control.litellm import InferenceGatewayError
from heartwood_platform.control.openshell import ProviderError
from heartwood_platform.control.planning import MCP_PATH, UNAVAILABLE, create_planning_app
from heartwood_platform.control.prices import PriceSheet, UnpricedError, task_rate
from heartwood_platform.control.project_data import DatasetDeniedError
from heartwood_platform.control.registry import NotAMemberError
from heartwood_platform.control.researchers import RESEARCHER_ID_PATTERN, Subject
from heartwood_platform.control.sessions import SessionError

# Researcher identifiers and projects become part of redirect URIs and object names.
ResearcherId = Annotated[str, StringConstraints(pattern=RESEARCHER_ID_PATTERN)]
ProjectId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")]


_LOGGER = logging.getLogger(__name__)
_RESEARCHER = TypeAdapter(ResearcherId)
_PROJECT_ID = TypeAdapter(ProjectId)
# A launch context request names one subject of at most 255 characters, and a job
# reservation request a runtime profile and a duration.
_REQUEST_BYTES = 4096
# The operation identifiers of the job service, which name a job's reservation.
OperationId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
_OPERATION_ID = TypeAdapter(OperationId)
# The longest run of a task that the job service admits, in seconds.
_MAX_TASK_SECONDS = 7 * 24 * 3600


class ContextRequest(BaseModel):
    """The researcher whose launch context a platform service reads."""

    model_config = ConfigDict(extra="forbid", strict=True)

    subject: Subject


class JobUsage(BaseModel):
    """The run time of a batch task on a runtime profile: its limit or what it used."""

    model_config = ConfigDict(extra="forbid", strict=True)

    runtime_profile_ref: Annotated[str, StringConstraints(pattern=r"^\S{1,512}$")]
    seconds: Annotated[int, Field(ge=0, le=2**63 - 1)]


class JobLimit(JobUsage):
    """The run time limit admitted at submission."""

    seconds: Annotated[int, Field(ge=1, le=_MAX_TASK_SECONDS)]


def job_holder(project: str, operation_id: str) -> str:
    """Return the holder of a batch job's reservation in the ledger."""
    return f"job/{project}/{operation_id}"


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
    contexts: ServiceTokens | None = None,
    jobs: ServiceTokens | None = None,
) -> Starlette:
    """Build the application around the launch service, the prices and the verifiers.

    ``operators`` accepts the platform operators' service accounts, which may rotate
    project client secrets; without it nobody may. ``contexts`` accepts the platform
    services that read the launch context of a researcher and project, such as the data
    service and the job service for their operation observations. ``jobs`` accepts the
    job service, which reserves and settles the compute budgets of batch tasks.
    """
    if prices.denomination != ledger.denomination:
        raise ValueError("Price sheet and ledger denominations differ")

    def handled(endpoint: Callable[[Request], Response]) -> Callable[[Request], Response]:
        def wrapper(request: Request) -> Response:
            try:
                return endpoint(request)
            except AuthenticationError:
                return _error(401, "authentication failed")
            except NotAMemberError:
                return _error(403, "not a member of the project")
            except DatasetDeniedError:
                return _error(403, "dataset version is not granted to this project")
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
            except (httpx.HTTPError, InferenceGatewayError, ProviderError) as error:
                # Keycloak, the Kubernetes API, LiteLLM or an OpenShell gateway did not
                # answer as expected.
                if isinstance(error, httpx.HTTPStatusError):
                    # The path names the component and the object; the query is left out.
                    _LOGGER.warning(
                        "a platform component failed: %s %s%s answered %d",
                        error.request.method,
                        error.request.url.host,
                        error.request.url.path,
                        error.response.status_code,
                    )
                else:
                    _LOGGER.warning("a platform component failed: %s", type(error).__name__)
                    if not isinstance(error, httpx.HTTPError):
                        _LOGGER.warning("%s", error)
                return _error(502, UNAVAILABLE)
            except InferenceSetupError as error:
                return _error(409, str(error))
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
        requested = request.query_params.getlist("dataset")
        if len(requested) > 1:
            raise DatasetDeniedError
        prepared = launches.prepare(
            launches.subject_of(launch.researcher),
            launch.project,
            requested[0] if requested else None,
        )
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

    def rotate_model_key(request: Request) -> Response:
        if operators is None:
            raise AuthenticationError("no operator is configured")
        operator = operators.verify(_bearer(request))
        launch = launch_request(request)
        rotated = launches.rotate_model_key(launches.subject_of(launch.researcher), launch.project)
        _LOGGER.info("%s rotated the model key of session %s", operator, rotated.session)
        return JSONResponse({"session": rotated.session})

    def launch_context(request: Request) -> Response:
        researcher = researchers.verify(_bearer(request))
        context = launches.context(researcher.subject, researcher.project)
        return JSONResponse(context.model_dump(mode="json"))

    def service_context(request: Request, body: bytes) -> Response:
        if contexts is None:
            raise AuthenticationError("no service may read launch contexts")
        contexts.verify(_bearer(request))
        project = _PROJECT_ID.validate_python(request.path_params["project"])
        read = ContextRequest.model_validate_json(body)
        context = launches.context(read.subject, project)
        # A service needs the identity of the session's launch context, and no billing data.
        return JSONResponse({"project_context_id": context.project_context_id})

    def job_request(
        request: Request, body: bytes, *, settlement: bool = False
    ) -> tuple[str, str, JobUsage]:
        if jobs is None:
            raise AuthenticationError("no service may reserve job budgets")
        jobs.verify(_bearer(request))
        project = _PROJECT_ID.validate_python(request.path_params["project"])
        operation = (
            _OPERATION_ID.validate_python(request.path_params["operation"])
            if "operation" in request.path_params
            else ""
        )
        model = JobUsage if settlement else JobLimit
        return project, operation, model.model_validate_json(body)

    def denomination() -> dict[str, str]:
        return ledger.denomination.model_dump(mode="json")

    def reservation_body(reservation: Reservation) -> dict[str, object]:
        return {
            "budget_id": reservation.budget_id,
            "amount": format(reservation.amount, "f"),
            "incurred": None if reservation.incurred is None else format(reservation.incurred, "f"),
            "state": reservation.state,
            "denomination": denomination(),
        }

    def quote_job(request: Request, body: bytes) -> Response:
        project, _, usage = job_request(request, body)
        try:
            amount = task_rate(prices, usage.runtime_profile_ref, clock()).cost(usage.seconds)
        except UnpricedError as error:
            return JSONResponse({"error": "unpriced", "reason": str(error)}, status_code=409)
        return JSONResponse(
            {
                "budget_id": budget_id(project, "compute"),
                "amount": format(amount, "f"),
                "denomination": denomination(),
            }
        )

    def reserve_job(request: Request, body: bytes) -> Response:
        project, operation, usage = job_request(request, body)
        try:
            reservation = ledger.reserve_once(
                project,
                job_holder(project, operation),
                usage.seconds,
                lambda: task_rate(prices, usage.runtime_profile_ref, clock()),
            )
        except UnpricedError as error:
            return JSONResponse({"error": "unpriced", "reason": str(error)}, status_code=409)
        except EndedHolderError:
            return JSONResponse({"error": "settled"}, status_code=409)
        except InsufficientBudgetError as error:
            return JSONResponse(
                {
                    "error": "insufficient",
                    "budget_id": error.budget_id,
                    "amount": format(error.amount, "f"),
                    "available": None if error.available is None else format(error.available, "f"),
                    "denomination": denomination(),
                },
                status_code=409,
            )
        return JSONResponse(reservation_body(reservation))

    def settle_job(request: Request, body: bytes) -> Response:
        project, operation, usage = job_request(request, body, settlement=True)
        settled = ledger.settle_once(project, job_holder(project, operation), usage.seconds)
        return JSONResponse(reservation_body(settled))

    def bounded(
        endpoint: Callable[[Request, bytes], Response],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def read(request: Request) -> Response:
            # The subject of a launch context travels in the body, so that no subject
            # appears in a URL.
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > _REQUEST_BYTES:
                    return _error(413, "request too large")
            return await run_in_threadpool(handled(lambda _: endpoint(request, body)), request)

        return read

    planning = create_planning_app(launches, researchers, prices, ledger, clock)
    launch_path = "/v1/researchers/{researcher}/projects/{project}/launch"
    return Starlette(
        routes=[
            Route("/healthz", health),
            Route("/v1/researchers/{researcher}/projects", handled(projects)),
            Route(launch_path, handled(prepare), methods=["PUT"]),
            Route(launch_path, handled(delete), methods=["DELETE"]),
            Route("/v1/projects/{project}/client-secret", handled(rotate), methods=["POST"]),
            Route(
                "/v1/researchers/{researcher}/projects/{project}/model-key",
                handled(rotate_model_key),
                methods=["POST"],
            ),
            Route("/v1/launch-context", handled(launch_context)),
            Route(
                "/v1/projects/{project}/launch-context",
                bounded(service_context),
                methods=["POST"],
            ),
            Route("/v1/projects/{project}/job-quotes", bounded(quote_job), methods=["POST"]),
            Route(
                "/v1/projects/{project}/job-reservations/{operation}",
                bounded(reserve_job),
                methods=["PUT"],
            ),
            Route(
                "/v1/projects/{project}/job-reservations/{operation}/settlement",
                bounded(settle_job),
                methods=["POST"],
            ),
            Route(MCP_PATH, planning),
        ],
        lifespan=planning.lifespan,
    )
