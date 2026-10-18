# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Describe the existing gateway transport using its owning Python contracts.

This inventory describes routes; it neither dispatches requests nor authorizes them.
The REST and ASGI adapters remain the implementation of the transport.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any, Literal, NotRequired, TypedDict

from pydantic import TypeAdapter

from heartwood import schemas as api
from heartwood.gateway._access import CAPABILITY_COOKIE, CAPABILITY_HEADER
from heartwood.gateway._gateway import GatewaySessionSnapshot
from heartwood.gateway._session_projection import SessionProjection
from heartwood.schemas.experiments import ExperimentCollection, ExperimentExport
from heartwood.schemas.python_environment import PythonEnvironmentSnapshot
from heartwood.schemas.workflows import WorkflowCatalog
from heartwood.session import SessionCommand


class GatewayError(TypedDict):
    """An application error, optionally carrying a stable ingress diagnostic code."""

    error: str
    code: NotRequired[str]


@dataclass(frozen=True)
class _Operation:
    method: str
    path: str
    summary: str
    response: Any
    request: Any = None
    status: int = 200
    errors: tuple[int, ...] = ()


# Keep one row per implemented REST operation, in the order of the owning handler.
_REST_OPERATIONS = (
    _Operation("GET", "/sessions", "List project sessions", api.SessionListResponse),
    _Operation(
        "POST",
        "/sessions",
        "Create a session",
        api.SessionSummaryResponse,
        api.SessionCreateRequest,
        201,
        (422,),
    ),
    _Operation(
        "POST", "/sessions/default", "Get or create the default session", api.SessionSummaryResponse
    ),
    _Operation(
        "GET",
        "/project/readiness",
        "Inspect project readiness",
        api.ProjectReadinessResponse,
        errors=(422,),
    ),
    _Operation(
        "GET",
        "/project/capabilities",
        "Inspect platform capabilities",
        api.PlatformCapabilitiesResponse,
    ),
    _Operation(
        "GET",
        "/project/startup",
        "Inspect the next startup step",
        api.StartupPlanResponse,
        errors=(422,),
    ),
    _Operation(
        "POST",
        "/project/initialize",
        "Initialize project state",
        api.StartupPlanResponse,
        errors=(422,),
    ),
    _Operation(
        "GET",
        "/sessions/{session_id}",
        "Inspect a session",
        api.SessionSummaryResponse,
        errors=(404, 422),
    ),
    _Operation(
        "PATCH",
        "/sessions/{session_id}",
        "Rename a session",
        api.SessionSummaryResponse,
        api.SessionRenameRequest,
        errors=(404, 422),
    ),
    _Operation(
        "GET",
        "/settings/actions",
        "Inspect action review settings",
        api.ActionSettingsResponse,
        errors=(422,),
    ),
    _Operation(
        "PUT",
        "/settings/actions/confirmation",
        "Select an action review mode",
        api.ActionSettingsResponse,
        api.ActionConfirmationRequest,
        errors=(422,),
    ),
    _Operation(
        "GET",
        "/settings/models",
        "Inspect model settings",
        api.ModelSettingsResponse,
        errors=(422,),
    ),
    _Operation(
        "GET",
        "/settings/credentials",
        "Inspect credential status",
        api.CredentialSettingsResponse,
        errors=(422,),
    ),
    _Operation(
        "DELETE",
        "/settings/credentials/{connection_id}",
        "Forget a saved credential",
        api.CredentialSettingsResponse,
        errors=(422,),
    ),
    _Operation(
        "GET",
        "/settings/skills",
        "Inspect Skill settings",
        api.SkillSettingsResponse,
        errors=(422,),
    ),
    _Operation(
        "GET",
        "/research/environment",
        "Inspect the verification environment",
        PythonEnvironmentSnapshot,
        errors=(503,),
    ),
    _Operation("GET", "/research/workflows", "List research workflows", WorkflowCatalog),
    _Operation("GET", "/research/experiments", "Read experiment records", ExperimentCollection),
    _Operation(
        "GET", "/research/experiments/export", "Export experiment records", ExperimentExport
    ),
    _Operation(
        "GET",
        "/settings/specialists",
        "Inspect specialist settings",
        api.SpecialistSettingsResponse,
    ),
    _Operation(
        "POST",
        "/settings/skills/inspect",
        "Inspect a signed Skill",
        api.SkillSummaryResponse,
        api.SkillInspectRequest,
        errors=(422,),
    ),
    _Operation(
        "POST",
        "/settings/skills/install",
        "Install a reviewed signed Skill",
        api.SkillSettingsResponse,
        api.SkillInstallRequest,
        errors=(422,),
    ),
    _Operation(
        "POST",
        "/settings/skills/refresh",
        "Refresh signed Skill catalogs",
        api.SkillSettingsResponse,
        api.SkillRefreshRequest,
        errors=(422,),
    ),
    _Operation(
        "POST",
        "/settings/skills/local/inspect",
        "Inspect a local Skill",
        api.SkillSummaryResponse,
        api.SkillLocalInspectRequest,
        errors=(422,),
    ),
    _Operation(
        "POST",
        "/settings/skills/local/install",
        "Install a reviewed local Skill",
        api.SkillSettingsResponse,
        api.SkillLocalInstallRequest,
        errors=(422,),
    ),
    _Operation(
        "DELETE",
        "/settings/skills/{name}",
        "Remove a Skill",
        api.SkillSettingsResponse,
        errors=(422,),
    ),
    _Operation(
        "GET",
        "/settings/models/artifacts",
        "Inspect model artifacts and progress",
        api.ModelArtifactsResponse,
    ),
    _Operation(
        "POST",
        "/settings/models/catalog",
        "Discover authorized models",
        api.ModelCatalogResponse,
        api.ModelCatalogRequest,
        errors=(422,),
    ),
    _Operation(
        "POST",
        "/settings/models/subscription/device",
        "Start a subscription device login",
        api.SubscriptionDeviceLoginResponse,
        api.SubscriptionDeviceLoginRequest,
        201,
        (422,),
    ),
    _Operation(
        "POST",
        "/settings/models/subscription/device/poll",
        "Poll a subscription device login",
        api.SubscriptionDeviceLoginResponse,
        api.SubscriptionDevicePollRequest,
        errors=(422,),
    ),
    _Operation(
        "POST",
        "/settings/models/repository",
        "Inspect a model repository",
        api.ModelRepositoryPlanResponse,
        api.ModelRepositoryRequest,
        errors=(422,),
    ),
    _Operation(
        "PUT",
        "/settings/models/source",
        "Select a model source",
        api.ModelSettingsResponse,
        api.ModelSourceRequest,
        errors=(422,),
    ),
    _Operation(
        "POST",
        "/settings/models/downloads",
        "Start a catalog model download",
        api.ModelDownloadResponse,
        api.ModelDownloadRequest,
        202,
        (422,),
    ),
    _Operation(
        "POST",
        "/settings/models/downloads/custom",
        "Start an inspected repository download",
        api.ModelDownloadResponse,
        api.CustomLocalModelDownloadRequest,
        202,
        (422,),
    ),
    _Operation(
        "POST",
        "/settings/models/imports",
        "Import local model weights",
        api.LocalModelImportResponse,
        api.LocalModelImportRequest,
        201,
        (422,),
    ),
    _Operation(
        "POST",
        "/settings/models/transfers/inspect",
        "Inspect a portable model bundle",
        api.ModelTransferPlanResponse,
        api.ModelTransferInspectRequest,
        errors=(422,),
    ),
    _Operation(
        "POST",
        "/settings/models/transfers/exports",
        "Start a model bundle export",
        api.ModelTransferResponse,
        api.ModelTransferExportRequest,
        202,
        (422,),
    ),
    _Operation(
        "POST",
        "/settings/models/transfers/imports",
        "Import a reviewed model bundle",
        api.ModelTransferResponse,
        api.ModelTransferImportRequest,
        202,
        (422,),
    ),
    _Operation(
        "DELETE",
        "/settings/models/transfers/{transfer_id}",
        "Cancel a model transfer",
        api.ModelTransferResponse,
        errors=(404,),
    ),
    _Operation(
        "GET",
        "/settings/models/validation",
        "Validate a model profile",
        api.ModelValidationResponse,
        errors=(422,),
    ),
    _Operation(
        "POST",
        "/settings/models/profiles",
        "Save a model profile",
        api.ModelSettingsResponse,
        api.ModelProfileRequest,
        errors=(422,),
    ),
    _Operation(
        "POST",
        "/settings/models/connect",
        "Connect a selected model",
        api.ModelSettingsResponse,
        api.ModelConnectRequest,
        errors=(422,),
    ),
    _Operation(
        "PUT",
        "/settings/models/active",
        "Select the active model profile",
        api.ModelSettingsResponse,
        api.ModelSelectionRequest,
        errors=(422,),
    ),
    _Operation(
        "DELETE",
        "/settings/models/profiles/{profile_id}",
        "Remove a model profile",
        api.ModelSettingsResponse,
        errors=(422,),
    ),
    _Operation(
        "GET",
        "/sessions/{session_id}/workspace/tree",
        "Inspect the project file tree",
        api.WorkspaceTreeResponse,
        errors=(404, 422),
    ),
    _Operation(
        "GET",
        "/sessions/{session_id}/workspace/file",
        "Preview a project file",
        api.WorkspaceFileResponse,
        errors=(404, 422),
    ),
    _Operation(
        "GET",
        "/sessions/{session_id}/workspace/changes",
        "Inspect project changes",
        api.WorkspaceChangesResponse,
        errors=(404, 422),
    ),
    _Operation(
        "GET",
        "/sessions/{session_id}/workspace/diff",
        "Preview a file diff",
        api.WorkspaceDiffResponse,
        errors=(404, 422),
    ),
    _Operation(
        "GET",
        "/sessions/{session_id}/audit-export",
        "Read a generated audit export",
        api.AuditExportResponse,
        errors=(404, 422),
    ),
    _Operation(
        "POST",
        "/sessions/{session_id}/commands",
        "Submit a session command",
        GatewaySessionSnapshot,
        SessionCommand,
        errors=(422,),
    ),
    _Operation(
        "GET",
        "/sessions/{session_id}/projection",
        "Read the current session projection",
        SessionProjection,
        errors=(422,),
    ),
    _Operation(
        "GET",
        "/sessions/{session_id}/events",
        "Replay session events and current projection",
        GatewaySessionSnapshot,
        errors=(422,),
    ),
)

_ERROR_DESCRIPTIONS = {
    400: "Malformed request, query, ingress metadata, JSON, or UTF-8 body.",
    401: "The launch capability is missing or invalid.",
    403: "The request does not satisfy the configured ingress boundary.",
    404: "The resource or configured ingress path is unavailable.",
    409: "Storage locking, session ownership, integrity recovery, or command conflict.",
    413: "The request body exceeds 1 MiB.",
    422: "The request or current configuration fails validation.",
    503: "The verification Python environment could not be inspected.",
}
_STREAM_DESCRIPTION = (
    "Each update contains durable events and the current projection. Transient text can change "
    "the projection without adding events. Reconnect with the last observed durable sequence "
    "in the after query parameter. SSE does not emit event IDs or consume Last-Event-ID. "
    "The transport currently starts the response before opening the backend stream; a backend "
    "failure after that point can close the connection rather than return a JSON error."
)


def gateway_openapi(*, api_version: str | None = None) -> dict[str, Any]:
    """Return an OpenAPI 3.1 description of implemented gateway HTTP operations.

    The document version identifies the application release, not an additional wire version.
    All paths are relative to the deployment's browser-visible gateway base URL.
    """
    security: list[dict[str, list[str]]] = [{"CapabilityHeader": []}, {"CapabilityCookie": []}]
    types: dict[tuple[Any, Literal["validation", "serialization"]], TypeAdapter[Any]] = {}
    for operation in _REST_OPERATIONS:
        types[operation.response, "serialization"] = TypeAdapter(operation.response)
        if operation.request is not None:
            types[operation.request, "validation"] = TypeAdapter(operation.request)
    types[GatewayError, "serialization"] = TypeAdapter(GatewayError)
    refs, definitions = TypeAdapter.json_schemas(
        [(record, mode, adapter) for (record, mode), adapter in types.items()],
        ref_template="#/components/schemas/{model}",
    )
    error_ref = refs[GatewayError, "serialization"]
    paths: dict[str, Any] = {}
    for operation in _REST_OPERATIONS:
        response = refs[operation.response, "serialization"]
        errors = sorted({400, 401, 403, 404, 409, 413, *operation.errors})
        description: dict[str, Any] = {
            "operationId": _operation_id(operation.method, operation.path),
            "summary": operation.summary,
            "tags": [operation.path.split("/")[1]],
            "responses": {
                str(operation.status): {
                    "description": operation.summary + ".",
                    "content": {"application/json": {"schema": response}},
                },
                **_errors(errors, error_ref),
            },
        }
        parameters = _parameters(operation.path)
        if parameters:
            description["parameters"] = parameters
        if operation.request is not None:
            description["requestBody"] = {
                "required": operation.request is not api.SessionCreateRequest,
                "content": {
                    "application/json": {
                        "schema": refs[operation.request, "validation"],
                    }
                },
            }
        if operation.path.endswith("/commands"):
            description["description"] = (
                "The gateway replaces client-supplied actor_id with the launching principal "
                "(currently human). Command IDs support idempotent retries; conflicting reuse "
                "is rejected. The payload is a generic JSON object; command-specific rules "
                "are enforced by the gateway. Approval-content binding remains tracked in #158."
            )
        paths.setdefault(operation.path, {})[operation.method.lower()] = description

    snapshot_ref = refs[GatewaySessionSnapshot, "serialization"]
    paths["/launch"] = {
        "get": {
            "operationId": "get_launch",
            "summary": "Exchange a one-time launch token for the capability cookie",
            "security": [],
            "parameters": [
                {
                    "name": "token",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "One-time secret from the launch link; never log it.",
                }
            ],
            "responses": {
                "303": {
                    "description": "Cookie established; redirect to the gateway browser root.",
                    "headers": {
                        "Location": {"schema": {"type": "string"}},
                        "Set-Cookie": {
                            "schema": {"type": "string"},
                            "description": (
                                "HttpOnly, SameSite=Strict capability cookie scoped to the base "
                                "path; Secure when the external origin uses HTTPS."
                            ),
                        },
                    },
                },
                **_errors([400, 404], error_ref),
                "403": {
                    "description": "Missing, invalid, or already consumed launch token, "
                    "or rejected ingress.",
                    "content": {"application/json": {"schema": error_ref}},
                },
            },
        }
    }
    stream_path = "/sessions/{session_id}/events/stream"
    paths[stream_path] = {
        "get": {
            "operationId": "get_sessions_session_id_events_stream",
            "summary": "Stream session snapshots as server-sent events",
            "description": _STREAM_DESCRIPTION,
            "parameters": _parameters(stream_path),
            "responses": {
                "200": {
                    "description": "A stream of heartwood-session-events events.",
                    "content": {"text/event-stream": {"schema": {"type": "string"}}},
                },
                **_errors([400, 401, 403, 404, 422], error_ref),
            },
            "x-sse-event": {"name": "heartwood-session-events", "dataSchema": snapshot_ref},
        }
    }
    return {
        "openapi": "3.1.0",
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "info": {
            "title": "Heartwood Gateway API",
            "version": api_version if api_version is not None else version("heartwood-gateway"),
            "description": (
                "Implemented per-project gateway operations. Set the server URL to the exact "
                "browser-visible gateway base path. Every API and stream request requires a "
                "capability and valid deployment ingress. The platform authenticates users; "
                "the current gateway records one human principal, not verified individual "
                "identities. Platform estimates and budgets are planned contracts, not these "
                "HTTP endpoints. Deployment-owned policy enforcement (#154) and exact action "
                "approval binding (#158) remain separate implementation work. A valid request "
                "schema, gateway capability, or researcher approval never grants platform "
                "authorization. Request bodies are bounded to 1 MiB UTF-8."
            ),
        },
        "servers": [{"url": "/", "description": "Replace with the configured gateway base URL."}],
        "security": security,
        "paths": paths,
        "components": {
            "schemas": definitions.get("$defs", {}),
            "securitySchemes": {
                "CapabilityHeader": {"type": "apiKey", "in": "header", "name": CAPABILITY_HEADER},
                "CapabilityCookie": {"type": "apiKey", "in": "cookie", "name": CAPABILITY_COOKIE},
            },
        },
        "x-websocket": {
            "path": "/sessions/{session_id}/events",
            "description": _STREAM_DESCRIPTION + " The stream sends snapshots only; submit "
            "commands through HTTP. The exact configured Origin header is required.",
            "parameters": _parameters("/sessions/{session_id}/events"),
            "security": security,
            "messageSchema": snapshot_ref,
            "rejectedCloseCode": 1008,
        },
    }


def _operation_id(method: str, path: str) -> str:
    return (
        method.lower()
        + "_"
        + re.sub(r"[{}]", "", path.strip("/")).replace("/", "_").replace("-", "_")
    )


def _errors(statuses: list[int], schema: dict[str, Any]) -> dict[str, Any]:
    return {
        str(status): {
            "description": _ERROR_DESCRIPTIONS[status],
            "content": {"application/json": {"schema": schema}},
        }
        for status in statuses
    }


def _parameters(path: str) -> list[dict[str, Any]]:
    parameters: list[dict[str, Any]] = []
    for name in re.findall(r"{([^}]+)}", path):
        schema: dict[str, Any] = {"type": "string"}
        if name == "session_id":
            schema = SessionCommand.model_json_schema()["properties"]["session_id"]
        parameters.append({"name": name, "in": "path", "required": True, "schema": schema})
    queries: list[tuple[str, dict[str, Any], bool]] = []
    if path == "/project/startup":
        queries = [
            (
                "interface",
                {"type": "string", "enum": ["terminal", "web", "notebook"], "default": "web"},
                False,
            ),
            (
                "port",
                {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 65535,
                    "default": 8767,
                    "description": "Port for the startup plan; zero selects the default.",
                },
                False,
            ),
        ]
    elif path == "/settings/models/validation":
        queries = [("profile_id", {"type": "string"}, False)]
    elif path.endswith(("/events", "/events/stream")):
        queries = [("after", {"type": "integer"}, False)]
    elif path.endswith("/workspace/tree"):
        queries = [
            ("path", {"type": "string", "default": "."}, False),
            (
                "depth",
                {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Maximum tree depth; must not exceed the limits reported "
                        "by the workspace response."
                    ),
                },
                False,
            ),
        ]
    elif path.endswith(("/workspace/file", "/workspace/diff")):
        queries = [("path", {"type": "string"}, True)]
    parameters.extend(
        {"name": name, "in": "query", "required": required, "schema": schema}
        for name, schema, required in queries
    )
    return parameters
