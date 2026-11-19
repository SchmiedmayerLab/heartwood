# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The planning tools of the control service, served over MCP.

A Heartwood gateway calls them with a researcher's exchanged token. Every request is
authenticated with the same verifier as the launch context and checked against the
registry; the project and its launch context come from the token and the registry.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.http import StarletteWithLifespan
from mcp.types import ToolAnnotations
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from heartwood.schemas import PlatformEstimate, WorkloadPrediction
from heartwood_platform.common.tokens import AuthenticationError, Researcher, ResearcherTokens
from heartwood_platform.control.keycloak import project_client
from heartwood_platform.control.launch import LaunchService, NoSessionError
from heartwood_platform.control.prices import EstimateRequestError, PriceSheet, estimate
from heartwood_platform.control.registry import NotAMemberError

MCP_PATH = "/mcp"
# An estimate reads the price sheet and changes nothing.
_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


class _ExchangedTokens(TokenVerifier):
    """Accepts exchanged researcher tokens of members of the token's project."""

    def __init__(self, researchers: ResearcherTokens, launches: LaunchService) -> None:
        super().__init__()
        self._researchers = researchers
        self._launches = launches

    def _member(self, token: str) -> Researcher | None:
        researcher = self._researchers.verify(token)
        if researcher.project not in self._launches.projects(researcher.subject):
            return None
        return researcher

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the researcher and project of a token of a project member."""
        try:
            researcher = await run_in_threadpool(self._member, token)
        except (AuthenticationError, httpx.HTTPError):
            return None
        if researcher is None:
            return None
        return AccessToken(
            token=token,
            client_id=project_client(researcher.project),
            scopes=[],
            claims={"sub": researcher.subject, "project": researcher.project},
        )


def create_planning_app(
    launches: LaunchService,
    researchers: ResearcherTokens,
    prices: PriceSheet,
    clock: Callable[[], datetime],
) -> StarletteWithLifespan:
    """Build the MCP application that serves the planning tools at ``MCP_PATH``."""
    server = FastMCP(
        "Heartwood control service",
        auth=_ExchangedTokens(researchers, launches),
        mask_error_details=True,
    )

    @server.tool(annotations=_READ_ONLY)
    def estimate_workload(
        project_id: str,
        scope: Literal["plan", "elements"],
        prediction: dict[str, Any],
        element_ids: list[str] | None = None,
    ) -> PlatformEstimate:
        """Price a workload prediction for the whole plan or for named elements.

        The estimate reserves nothing and authorizes nothing. Work without a price is
        reported as unknown cost with the reason.
        """
        token = get_access_token()
        if token is None:
            raise ToolError("authentication required")
        subject, project = token.claims["sub"], token.claims["project"]
        if project_id != project:
            raise ToolError("project_id is not the project of the token")
        try:
            context = launches.context(subject, project)
        except NotAMemberError:
            raise ToolError("not a member of the project") from None
        except NoSessionError:
            raise ToolError("no session in the project") from None
        try:
            parsed = WorkloadPrediction.model_validate_json(json.dumps(prediction))
        except ValidationError:
            raise ToolError("invalid workload prediction") from None
        if parsed.project_context_id != context.project_context_id:
            raise ToolError("the prediction belongs to another project context")
        try:
            return estimate(prices, parsed, scope=scope, element_ids=element_ids, now=clock())
        except EstimateRequestError as error:
            raise ToolError(str(error)) from None

    return server.http_app(path=MCP_PATH, json_response=True, stateless_http=True)
