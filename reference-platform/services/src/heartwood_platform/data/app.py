# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The HTTP interface of the data service.

The DRS 1.5 read endpoints serve a Heartwood gateway, which calls them with a
researcher's exchanged token: the project comes from the token, and only objects of that
project exist for the caller. The control service asks for the access plan of a
project data volume with its service account token and passes researcher and project
as data, which the service checks against its own membership list.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from heartwood_platform.common.tokens import (
    AuthenticationError,
    Researcher,
    ResearcherTokens,
    ServiceTokens,
)
from heartwood_platform.data.access import ObjectAccess
from heartwood_platform.data.catalog import Catalog, Entry, ProjectId, content_sha256

DRS_PREFIX = "/ga4gh/drs/v1"
# The only access method: an HTTPS URL to the object store.
ACCESS_ID = "https"
DRS_VERSION = "1.5.0"
UNAVAILABLE = "a platform component is unavailable"
_MAX_BODY = 4096


class _RefusedError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class VolumePlanRequest(BaseModel):
    """The researcher and project whose project data volume is filled."""

    model_config = ConfigDict(extra="forbid", strict=True)

    subject: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,64}$")]
    project: ProjectId


def bearer(request: Request) -> str:
    """Return the bearer token of a request."""
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("no bearer token")
    return token


def drs_object(entry: Entry, hostname: str) -> dict[str, object]:
    """Describe a catalog object as a DRS object."""
    return {
        "id": entry.id,
        "name": entry.item.name,
        "self_uri": f"drs://{hostname}/{entry.id}",
        "size": entry.item.size,
        "created_time": entry.dataset.created.isoformat(),
        "mime_type": entry.item.mime_type,
        "description": entry.dataset.description,
        "checksums": [{"checksum": entry.item.sha256, "type": "sha-256"}],
        "access_methods": [{"type": "https", "access_id": ACCESS_ID}],
    }


def member(catalog: Catalog, researchers: ResearcherTokens, request: Request) -> Researcher:
    """Return the researcher of an exchanged token whose subject is a project member."""
    researcher = researchers.verify(bearer(request))
    if not catalog.is_member(researcher.subject, researcher.project):
        raise _RefusedError(403, "not a member of the project")
    return researcher


def create_app(
    catalog: Catalog,
    researchers: ResearcherTokens,
    services: ServiceTokens,
    access: ObjectAccess,
    *,
    hostname: str,
    platform_id: str,
) -> Starlette:
    """Build the application over a catalog, the token verifiers and object access.

    ``hostname`` is the DRS host name of this service, as it appears in DRS URIs.
    """

    def handled(endpoint: Callable[[Request], Response]) -> Callable[[Request], Response]:
        def wrapper(request: Request) -> Response:
            try:
                return endpoint(request)
            except AuthenticationError:
                return _error(401, "authentication failed")
            except _RefusedError as refused:
                return _error(refused.status, refused.message)
            except ValidationError:
                return _error(400, "invalid request")
            except httpx.HTTPError:
                # The keys of the identity provider or a token review were unavailable.
                return _error(502, UNAVAILABLE)

        return wrapper

    def entry(request: Request) -> Entry:
        researcher = member(catalog, researchers, request)
        found = catalog.entry(researcher.project, request.path_params["object_id"])
        if found is None:
            # An object of another project does not exist for the caller.
            raise _RefusedError(404, "no such object in the project")
        return found

    def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    def service_info(_: Request) -> Response:
        return JSONResponse(
            {
                "id": f"{platform_id}.drs",
                "name": "Project data service",
                "type": {"group": "org.ga4gh", "artifact": "drs", "version": DRS_VERSION},
                "description": "Read access to the synthetic and open project data of the "
                "reference platform.",
                "organization": {"name": platform_id, "url": f"https://{hostname}"},
                "version": DRS_VERSION,
            }
        )

    def get_object(request: Request) -> Response:
        return JSONResponse(drs_object(entry(request), hostname))

    def get_access(request: Request) -> Response:
        found = entry(request)
        if request.path_params["access_id"] != ACCESS_ID:
            raise _RefusedError(404, "no such access method")
        return JSONResponse({"url": access.url(found).url})

    def volume_plan(request: Request, content: bytes) -> Response:
        services.verify(bearer(request))
        body = VolumePlanRequest.model_validate_json(content)
        if not catalog.is_member(body.subject, body.project):
            raise _RefusedError(403, "not a member of the project")
        entries = catalog.entries(body.project)
        objects = []
        for found in entries:
            url = access.url(found)
            objects.append(
                {
                    "id": found.id,
                    "path": found.path,
                    "size": found.item.size,
                    "sha256": found.item.sha256,
                    "url": url.url,
                    "expires_at": url.expires_at.isoformat(),
                }
            )
        return JSONResponse(
            {
                "project": body.project,
                "content_sha256": content_sha256(entries),
                "objects": objects,
            }
        )

    async def read_body(request: Request) -> Response:
        # The body is read here, and the handler runs in a thread like the others. A
        # request names a researcher and a project, so its body is small.
        content = b""
        async for chunk in request.stream():
            content += chunk
            if len(content) > _MAX_BODY:
                return _error(413, "request too large")
        return await run_in_threadpool(handled(lambda r: volume_plan(r, content)), request)

    return Starlette(
        routes=[
            Route("/healthz", health),
            Route(f"{DRS_PREFIX}/service-info", service_info),
            Route(f"{DRS_PREFIX}/objects/{{object_id}}", handled(get_object)),
            Route(f"{DRS_PREFIX}/objects/{{object_id}}/access/{{access_id}}", handled(get_access)),
            Route("/v1/volume-plans", read_body, methods=["POST"]),
        ]
    )


def _error(status: int, message: str) -> JSONResponse:
    # The error form of DRS.
    return JSONResponse({"msg": message, "status_code": status}, status_code=status)
