# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The data catalog's MCP tools, which a Heartwood gateway calls as reviewed operations.

Each tool takes the gateway's project and operation identifiers and returns an operation
observation whose result is the catalog's answer. The service keeps every observation by
researcher, project and operation identifier: a repeated identifier with the same request
returns the kept observation, and one with another request is refused. The read-only tool
``get_operation`` reports a kept operation and records an identifier it has not seen as
failed, so that identifier is refused afterwards.

Calls come with a researcher's token exchanged for the data service; the project is the
token's, and the researcher must be a member. Each observation names the launch context
of the researcher's session, which the control service reports.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.http import StarletteWithLifespan
from mcp.types import ToolAnnotations
from starlette.concurrency import run_in_threadpool

from heartwood.schemas import PlatformOperationObservation
from heartwood_platform.common.tokens import AuthenticationError, ResearcherTokens
from heartwood_platform.data.app import drs_object
from heartwood_platform.data.catalog import Catalog, Entry

MCP_PATH = "/mcp"
SERVICE_ID = "heartwood-data"
OBSERVATION_TOOL = "get_operation"
# Every tool reads the catalog and changes nothing outside the service's own records.
_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS operation (
    subject TEXT NOT NULL,
    project TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    observation TEXT NOT NULL,
    PRIMARY KEY (subject, project, operation_id)
) STRICT
"""
# The request digest of an identifier first seen by get_operation, which no tool request has.
_UNSEEN = "unseen"


class OperationConflictError(Exception):
    """An operation identifier names another request."""


class Operations:
    """The kept observations, in one SQLite database."""

    def __init__(self, path: Path) -> None:
        """Open the database at a path and create its table."""
        self._path = path
        self._lock = threading.Lock()
        with self._transaction() as connection:
            connection.execute(_SCHEMA)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def record(
        self,
        key: tuple[str, str, str],
        request_sha256: str,
        observe: Callable[[], PlatformOperationObservation],
    ) -> PlatformOperationObservation:
        """Return the kept observation of an operation, or run and keep a new one."""
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT request_sha256, observation FROM operation"
                " WHERE subject = ? AND project = ? AND operation_id = ?",
                key,
            ).fetchone()
            if row is not None:
                if row[0] != request_sha256:
                    raise OperationConflictError
                return PlatformOperationObservation.model_validate_json(row[1])
            observation = observe()
            connection.execute(
                "INSERT INTO operation VALUES (?, ?, ?, ?, ?)",
                (*key, request_sha256, observation.model_dump_json()),
            )
            return observation

    def observe(
        self, key: tuple[str, str, str], failed: Callable[[], PlatformOperationObservation]
    ) -> PlatformOperationObservation:
        """Return the kept observation, or keep the identifier as a failed operation."""
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT observation FROM operation"
                " WHERE subject = ? AND project = ? AND operation_id = ?",
                key,
            ).fetchone()
            if row is not None:
                return PlatformOperationObservation.model_validate_json(row[0])
            observation = failed()
            connection.execute(
                "INSERT INTO operation VALUES (?, ?, ?, ?, ?)",
                (*key, _UNSEEN, observation.model_dump_json()),
            )
            return observation


class _ExchangedTokens(TokenVerifier):
    """Accepts exchanged researcher tokens of members of the token's project."""

    def __init__(self, researchers: ResearcherTokens, catalog: Catalog) -> None:
        super().__init__()
        self._researchers = researchers
        self._catalog = catalog

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the researcher and project of a token of a project member."""
        try:
            researcher = await run_in_threadpool(self._researchers.verify, token)
        except (AuthenticationError, httpx.HTTPError):
            return None
        if not self._catalog.is_member(researcher.subject, researcher.project):
            return None
        return AccessToken(
            token=token,
            client_id=f"hw-proj-{researcher.project}",
            scopes=[],
            claims={"sub": researcher.subject, "project": researcher.project},
        )


def _object(entry: Entry, hostname: str, mount_path: str) -> dict[str, Any]:
    return {**drs_object(entry, hostname), "path": f"{mount_path}/{entry.path}"}


def create_catalog_app(
    catalog: Catalog,
    researchers: ResearcherTokens,
    operations: Operations,
    launch_context: Callable[[str, str], str],
    *,
    hostname: str,
    mount_path: str,
    clock: Callable[[], datetime],
) -> StarletteWithLifespan:
    """Build the MCP application of the catalog tools at ``MCP_PATH``.

    ``launch_context`` returns the project context identifier of a researcher's session in
    a project, and raises ``LookupError`` when the researcher has none.
    """
    server = FastMCP(
        "Heartwood data catalog",
        auth=_ExchangedTokens(researchers, catalog),
        mask_error_details=True,
    )

    def caller(project_id: str) -> tuple[str, str, str]:
        token = get_access_token()
        if token is None:
            raise ToolError("authentication required")
        subject, project = token.claims["sub"], token.claims["project"]
        if project_id != project:
            raise ToolError("project_id is not the project of the token")
        try:
            context = launch_context(subject, project)
        except LookupError:
            raise ToolError("no session in the project") from None
        except httpx.HTTPError:
            raise ToolError("the control service is unavailable") from None
        return subject, project, context

    def observation(
        context: str,
        operation_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        limitations: tuple[str, ...] = (),
    ) -> PlatformOperationObservation:
        reference = f"{SERVICE_ID}:operations/{operation_id}"
        return PlatformOperationObservation.model_validate_json(
            json.dumps(
                {
                    "project_context_id": context,
                    "operation_id": operation_id,
                    "service_id": SERVICE_ID,
                    "backend_operation_ref": reference,
                    "observed_at": clock().isoformat(),
                    "status": status,
                    "platform_record_ref": reference,
                    "limitations": list(limitations),
                    **({} if result is None else {"result": result}),
                }
            )
        )

    def run(
        project_id: str,
        operation_id: str,
        request: dict[str, Any],
        answer: Callable[[str, str], PlatformOperationObservation],
    ) -> PlatformOperationObservation:
        subject, project, context = caller(project_id)
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        try:
            return operations.record(
                (subject, project, operation_id), digest, lambda: answer(project, context)
            )
        except OperationConflictError:
            raise ToolError("operation_id names another request") from None

    @server.tool(annotations=_READ_ONLY)
    def list_datasets(project_id: str, operation_id: str) -> PlatformOperationObservation:
        """List the project's datasets and their objects.

        Each object has its DRS identifier, size, SHA-256 checksum, media type and the
        path at which workers of the project's sessions read it.
        """

        def answer(project: str, context: str) -> PlatformOperationObservation:
            datasets = [
                {
                    "id": dataset.id,
                    "name": dataset.name,
                    "description": dataset.description,
                    "created": dataset.created.isoformat(),
                    "objects": [
                        {
                            key: value
                            for key, value in _object(entry, hostname, mount_path).items()
                            if key in {"id", "name", "size", "mime_type", "checksums", "path"}
                        }
                        for entry in catalog.entries(project)
                        if entry.dataset.id == dataset.id
                    ],
                }
                for dataset in catalog.datasets(project)
            ]
            return observation(context, operation_id, "completed", result={"datasets": datasets})

        return run(project_id, operation_id, {"tool": "list_datasets"}, answer)

    @server.tool(annotations=_READ_ONLY)
    def resolve_object(
        object_id: str, project_id: str, operation_id: str
    ) -> PlatformOperationObservation:
        """Resolve a DRS object identifier of the project to its DRS object and data path.

        An identifier that names no object of the project is denied.
        """

        def answer(project: str, context: str) -> PlatformOperationObservation:
            found = catalog.entry(project, object_id)
            if found is None:
                return observation(
                    context,
                    operation_id,
                    "denied",
                    limitations=("The project has no object with this identifier.",),
                )
            return observation(
                context,
                operation_id,
                "completed",
                result={"object": _object(found, hostname, mount_path)},
            )

        request = {"tool": "resolve_object", "object_id": object_id}
        return run(project_id, operation_id, request, answer)

    @server.tool(name=OBSERVATION_TOOL, annotations=_READ_ONLY)
    def get_operation(project_id: str, operation_id: str) -> PlatformOperationObservation:
        """Report an operation of the project by its identifier.

        An identifier the service has not seen is recorded as a failed operation, so no
        later request can use it.
        """
        subject, project, context = caller(project_id)
        return operations.observe(
            (subject, project, operation_id),
            lambda: observation(
                context,
                operation_id,
                "failed",
                limitations=("The service received no request with this identifier.",),
            ),
        )

    return server.http_app(path=MCP_PATH, json_response=True, stateless_http=True)
